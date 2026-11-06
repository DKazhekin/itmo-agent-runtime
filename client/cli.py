import argparse
import base64
import json
import os
import time
import uuid
from pathlib import Path

import httpx


class Client:
    def __init__(self, url=None, token=None):
        self.http = httpx.Client(base_url=url or os.getenv("RUNTIME_URL", "http://127.0.0.1:8080"),
            headers={"Authorization": "Bearer " + (token or os.getenv("CLIENT_TOKEN", "local-checkpoint-token"))},
            timeout=85, trust_env=False)

    def request(self, method, path, data=None):
        response = self.http.request(method, path, json=data)
        response.raise_for_status()
        return response.json()

    def preflight(self, expected_mode="mock", expected_model=None):
        if expected_mode not in ("mock", "real"):
            raise ValueError("expected mode must be mock or real")
        if expected_mode == "real" and not expected_model:
            raise ValueError("real mode requires an explicit model name")
        state = self.request("GET", "/health")
        if state.get("model_mode") != expected_mode:
            raise ValueError("configured model mode differs from requested mode")
        if expected_mode == "real" and state.get("model") != expected_model:
            raise ValueError("configured model differs from requested model")
        return state

    def execute(self, fixture, instruction, destination, timeout=240, on_event=None,
                expected_mode="mock", expected_model=None):
        self.preflight(expected_mode, expected_model)
        session = uuid.uuid4().hex
        run = uuid.uuid4().hex
        files = {}
        for path in Path(fixture).rglob("*"):
            if path.is_symlink():
                raise ValueError("fixture must not contain symlinks")
            if path.is_file():
                files[str(path.relative_to(fixture))] = base64.b64encode(path.read_bytes()).decode()
        provisioned = False
        trace = []
        try:
            provisioned = True
            self.request("POST", "/sessions", {"version": "0", "session_id": session, "files": files})
            self.request("POST", "/runs", {"version": "0", "request_id": run, "session_id": session,
                "instruction": instruction, "timeout_seconds": timeout,
                "expected_mode": expected_mode, "expected_model": expected_model})
            after = 0
            deadline = time.monotonic() + timeout + 15
            while True:
                events = self.request("GET", f"/runs/{run}/events?after={after}")["events"]
                for event in events:
                    after = event["seq"]
                    trace.append(event)
                    if on_event:
                        on_event(event)
                state = self.request("GET", f"/runs/{run}")
                if state["status"] != "running":
                    # Fetch events committed between the previous poll and status.
                    trace.extend(self.request("GET", f"/runs/{run}/events?after={after}")["events"])
                    break
                if time.monotonic() > deadline:
                    self.request("POST", f"/runs/{run}/cancel", {})
                    raise TimeoutError("client deadline")
                time.sleep(.1)
            if state["status"] == "success":
                output = self.request("GET", f"/sessions/{session}/files")["files"]
                root = Path(destination).resolve()
                root.mkdir(parents=True, exist_ok=True)
                for name, content in output.items():
                    target = (root / name).resolve()
                    if not target.is_relative_to(root):
                        raise ValueError("unsafe collected path")
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(base64.b64decode(content))
            return state, trace
        finally:
            if provisioned:
                self.request("DELETE", f"/sessions/{session}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default=None)
    parser.add_argument("--mode", choices=["mock", "real"], default="mock")
    parser.add_argument("--model", default=None)
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("instruction")
    args = parser.parse_args()
    state, trace = Client(args.url).execute(args.fixture, args.instruction, args.output,
        on_event=lambda event: print(json.dumps(event, ensure_ascii=False)),
        expected_mode=args.mode, expected_model=args.model)
    print(json.dumps(state, ensure_ascii=False))
    raise SystemExit(state["status"] != "success")


if __name__ == "__main__":
    main()

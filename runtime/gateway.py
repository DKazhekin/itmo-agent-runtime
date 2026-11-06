import os
import secrets

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response, StreamingResponse
from runtime.streaming import stream_events

from runtime.common import call

app = FastAPI(title="Client server protocol", version="0")
HARNESS = os.getenv("HARNESS_URL", "http://harness:8000")


@app.get("/health")
def health():
    return call(HARNESS, "/health")



@app.get("/runs/{run_id}/stream")
def events(run_id: str, request: Request, after: int = 0):
    expected = "Bearer " + os.getenv("CLIENT_TOKEN", "local-checkpoint-token")
    if not secrets.compare_digest(request.headers.get("authorization", ""), expected):
        raise HTTPException(401, "invalid token")
    call(HARNESS, f"/runs/{run_id}")
    try:
        cursor = int(request.headers.get("last-event-id", after))
    except ValueError:
        raise HTTPException(422, "invalid event cursor")
    return StreamingResponse(stream_events(lambda value: call(HARNESS, f"/runs/{run_id}/events?after={value}")["events"], cursor), media_type="text/event-stream")


@app.api_route("/{path:path}", methods=["GET", "POST", "DELETE"])
async def proxy(path: str, request: Request):
    expected = "Bearer " + os.getenv("CLIENT_TOKEN", "local-checkpoint-token")
    if not secrets.compare_digest(request.headers.get("authorization", ""), expected):
        raise HTTPException(401, "invalid token")
    if path != "sessions" and path != "runs" and not path.startswith(("sessions/", "runs/")):
        raise HTTPException(404, "unknown endpoint")
    body = await request.body()
    if len(body) > 9_000_000:
        raise HTTPException(413, "request too large")
    try:
        async with httpx.AsyncClient(timeout=80, trust_env=False) as client:
            upstream = await client.request(request.method, HARNESS + "/" + path,
                params=request.query_params, content=body, headers={"Content-Type": "application/json"})
            return Response(upstream.content, status_code=upstream.status_code, media_type="application/json")
    except httpx.HTTPError:
        raise HTTPException(502, "harness transport failed")

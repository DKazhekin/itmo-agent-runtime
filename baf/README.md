# Basic agent functions

`catalog.yaml` preserves the full upstream catalog (B1–B8 and C1–C3). The `implementation` field marks B1–B5 as the current executable subset; deferred tasks are not reported as passing.

Each task contains `task.yaml`, `fixture/`, and `check.py`. B1.1 is preserved byte-for-byte from the course template: its metadata uses `messages` and `timeout_s`, and its checker reports failures in JSON without changing its exit code. New tasks use `instruction` and `timeout_seconds`; all checkers print a JSON object containing `pass` and `reason`. Consumers must inspect both the process status and the JSON field.

Copy only fixture contents into a fresh sandbox for each attempt. Checkers, schemas and hidden tests remain outside the sandbox. Check final artifacts rather than the model's prose. Reject artifact symlinks before running the original B1.1 checker.

The extended checkers require pinned `jsonschema` and `PyYAML` dependencies on the verifier. B4.2 compares submitted syntax trees with the trusted rename before executing a constrained, validated function in hidden behavior tests; it does not execute arbitrary submitted source on the verifier.

`python baf/validate_tasks.py` checks fixture scale, correct artifacts, deliberately wrong artifacts, byte preservation and the unmodified upstream B1.1 example. This validates the task package, not a model's ability to solve it. Real model results and model imitation results must remain separate.

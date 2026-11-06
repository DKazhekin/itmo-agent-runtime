# Protocol version 0

The two protocols have complete JSON Schema documents and OpenAPI descriptions:

| Boundary | Message schemas | HTTP routes |
| --- | --- | --- |
| Client ↔ server | `client-v0.schema.json` | `client-v0.openapi.json` |
| Harness ↔ sandbox manager | `sandbox-v0.schema.json` | `sandbox-v0.openapi.json` |

The `$defs` include requests, session states, operation/run responses, terminal results, typed events, collection, cancellation, release, health and HTTP errors. Each schema has a draft-2020-12 declaration and a stable `urn:itmo:*` identifier. To validate a particular payload, select its named `$defs` member; the root accepts the protocol's request and response envelopes.

`Session`, `Run` and `Operation` definitions are generated unchanged from their runtime models. Typed requests default to version `0` when omitted; a supplied version must be the literal string `0`. Unknown request fields are rejected. `OperationRequest` additionally describes the argument checks performed by the manager: `RunCommand.command`, `ReadFile.path`, or `WriteFile.path` and `content`, all strings with no extra arguments. Endpoint-side path, size and base64 checks remain mandatory; JSON Schema's `contentEncoding` is an annotation, not a substitute for decoding.

Every protocol state, result, event batch and event carries `version: "0"`. Bodyless HTTP actions such as cancellation use the versioned endpoint contract without a request envelope. Pre-acceptance HTTP errors follow FastAPI's actual `{"detail": string | validation_issues}` format and do not have a `version` field. They are described by `HTTPErrorResponse`; they are not terminal operation events. Unexpected HTTP 500 failures may be plain text; explicit HTTP exceptions may be JSON.

## Client routes

All routes except `/health` require `Authorization: Bearer <token>` at the gateway.

| Method and route | Request | Success response |
| --- | --- | --- |
| `GET /health` | — | `ClientHealthResponse` |
| `POST /sessions` | `Session` | `SessionResponse` |
| `POST /runs` | `Run` | `RunResponse` |
| `GET /runs/{run_id}` | — | `RunResponse` |
| `GET /runs/{run_id}/events?after=N` | — | `ClientEventBatch` |
| `GET /runs/{run_id}/stream` | — | SSE frames containing `ClientEvent` |
| `POST /runs/{run_id}/cancel` | — | `RunResponse` |
| `GET /sessions/{session_id}/files` | — | `CollectResponse` |
| `DELETE /sessions/{session_id}` | — | `ReleaseResponse` |

`ClientHealthResponse` includes `version`, `status`, `model_mode` and `model`; mock mode reports `model: null`. The gateway forwards this readiness state from the harness, which checks the sandbox manager and model proxy. It does not make a provider completion request. The manager uses the smaller `HealthResponse` containing only `version` and `status`.

`Run.expected_mode` defaults to `mock`; a real request also needs an explicit `expected_model` matching the configured model. The client checks these expectations before creating a session, the harness checks them before accepting a new run, and the proxy checks them before every provider request. A mismatch is rejected before the affected provider call; server-side pre-acceptance mismatches return HTTP 409. A repeated accepted request retains its normal idempotency rules. The CLI defaults to mock and requires `--mode real --model <exact-model-name>` for real execution.

A run is `running`, `success`, `error` or `cancelled`. Cancelling an already terminal run returns its existing outcome. Errors distinguish `timeout`, `sandbox_error`, `transport_error`, `model_error`, `step_limit` and `harness_restarted`. A successful result includes its answer, duration and `model_mode` (`mock` or `real`). For real responses, the provider-reported model must match exactly. An absent or different model identity produces `model_error` with `model_identity_mismatch`. Truncated, filtered or otherwise incomplete responses produce `model_error` with `incomplete_response`; tool calls from these responses are not executed. Base64 file maps carry synthetic checkpoint fixtures and collected outputs; they are not remote mounting.

## Harness ↔ sandbox routes

| Method and route | Request | Success response |
| --- | --- | --- |
| `GET /health` | — | `HealthResponse` |
| `POST /sessions` | `Session` | `SessionResponse` |
| `POST /exec` | `OperationRequest` | `OperationResponse` |
| `GET /operations/{operation_id}/events?after=N` | — | `SandboxEventBatch` |
| `GET /operations/{operation_id}/stream` | — | SSE frames containing `SandboxEvent` |
| `GET /sessions/{session_id}/files` | — | `CollectResponse` |
| `POST /sessions/{session_id}/cancel` | — | `CancelResponse` |
| `DELETE /sessions/{session_id}` | — | `ReleaseResponse` |

An operation is `running`, `success` or `error`. A repeated in-progress identifier can return `running`; a terminal `result` event never contains `running`. A command error includes its nonzero exit code, stdout, stderr, truncation flag and duration. Timeout and sandbox errors can lack command output, for example if execution never began or the manager recovered an uncertain operation. The finite error set is `command_error`, `timeout`, `sandbox_error`; transport failures are HTTP failures, not fabricated command results.

## Events and retries

Both JSON event batches and server-sent events (SSE) use the same typed JSON event objects. Every event contains `version`, positive `seq`, `trace_id`, `time` and `type`. Client events are `started`, `model`, `tool_call`, `tool_result` and `result`; sandbox events are `started` and `result`. The final event embeds a terminal `RunResult` or `OperationResult` respectively. The `seq` value is a monotonic cursor, not a per-session contiguous counter.

An SSE frame has `id: <seq>`, `event: <type>` and `data: <JSON event>`. Heartbeats are SSE comments and have no application payload. `Last-Event-ID` takes precedence over `after`; reconnecting requests receive events after that cursor. The OpenAPI `text/event-stream` media type describes framing, while `x-event-schema` identifies the JSON payload schema. The stream ends after delivering a terminal result; reconnecting after the final cursor produces no new result and can remain open until the stream's maximum lifetime.

The model's tool name and arguments are recorded before tool validation. Accordingly, `ToolCallEvent` preserves raw JSON arguments and can contain an unknown tool name. This allows an invalid call and its `InvalidToolResult` to remain visible in the trace. The event schema leaves `usage` open, but the current real proxy retains only nonnegative integer `prompt_tokens`, `completion_tokens` and `total_tokens` counters; an empty object means unavailable. Model events preserve these counters even when model identity or response completion fails. These two explicitly open fields do not relax the schemas of accepted sandbox requests or terminal results.

BAF summaries are separate result artifacts, not protocol messages. Real token totals require successful runtime completion, result processing without an exception and both counters in every model event; otherwise the summaries use `tokens_in: null`, `tokens_out: null` and `tokens_complete: false`. A negative checker verdict alone does not erase complete usage data; confirmed mock replay yields zero tokens. The runner's `budget_limit` is recorded approval metadata, not a protocol limit, spending ledger or enforced cap.

Commands have finite timeouts. Accepted operations produce exactly one final result event. Repeated identifiers return recorded state without executing again; changing the payload under the same identifier yields HTTP 409. Invalid requests are rejected before acceptance and do not create operations. Command errors may be returned to the model. Timeout destroys the session sandbox. Sandbox/transport failures are not retried as new actions. After a manager restart, unfinished operations become unknown-outcome sandbox errors; session containers are reclaimed. Cancellation preserves side effects already completed before termination.

The schemas define message shapes. Tests also check behavioral invariants such as one final result, idempotency and replay; JSON Schema alone cannot establish those temporal properties.

## Regeneration and verification

From the repository root:

```sh
python -m scripts.export_contracts
python -m scripts.export_contracts --live --compose-command 'docker compose'
```

The first command regenerates JSON Schema without Docker. The second also reads `/openapi.json` inside the running services through Compose. Client routes combine the live harness API with the gateway's health and stream routes; sandbox routes come from the manager. The exporter fails on an unexpected route set and enriches otherwise untyped FastAPI responses with the executable contract definitions. `x-live-source` records the source services and a digest of the route document. Exports must be refreshed after rebuilding changed services; they are not claims about a different running image.

`tests/test_contracts.py` checks exact runtime request definitions, complete route coverage, rejection of malformed messages, and live client/manager responses, HTTP errors, event batches, SSE replay, collection, cancellation and release. The live tests use the configured demo model replay and do not call a paid model.

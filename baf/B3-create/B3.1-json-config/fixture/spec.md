# Configuration format

Create a JSON object with exactly these keys:
- `service`: string `inventory`.
- `environment`: string `staging`.
- `listen`: object with exactly `host` (string `127.0.0.1`) and `port` (integer 8137).
- `retries`: integer 3.
- `enabled`: boolean true.
- `tags`: array containing exactly the strings `internal` and `read-only` in that order.
No additional keys are permitted at any level. Numbers and booleans must not be encoded as strings.

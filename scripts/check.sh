#!/bin/sh
set -eu
export CLIENT_TOKEN=local-checkpoint-token
export MODEL_MODE=mock MODEL_API_KEY= MODEL_BASE_URL= MODEL_NAME=
sh scripts/compose.sh -f compose.yaml -f compose.offline.yaml up -d --wait --no-build --pull never
sh scripts/compose.sh -f compose.yaml -f compose.offline.yaml --profile test run --rm --no-deps tests
sh scripts/compose.sh -f compose.yaml -f compose.offline.yaml --profile test run --rm --no-deps tests python baf/validate_tasks.py

#!/usr/bin/env bash
# Verify the inference image satisfies SageMaker's container contract, locally,
# before it is ever pushed.
#
# Checks the things that are cheap here and expensive to discover on a deployed
# endpoint:
#   - the container starts with the single argument `serve`, as SageMaker invokes it
#   - GET  /ping        becomes 200 once the model is loaded and can predict
#   - POST /invocations returns the response contract
#   - malformed JSON is rejected 4xx, not 5xx (a 5xx feeds the rollback alarm, so
#     this regression would make bad requests undo healthy deployments)
#   - /ping reports 503 rather than 200 when no model is mounted
#
# Exits non-zero on any failure.

set -euo pipefail

IMAGE="${1:?usage: container_smoke.sh <image[:tag]>}"
PORT="${PORT:-18080}"
CONTAINER="intake-smoke-$$"
MODEL_DIR="${MODEL_DIR:-}"
FAILURES=0

cleanup() {
  docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
  docker rm -f "${CONTAINER}-nomodel" >/dev/null 2>&1 || true
}
trap cleanup EXIT

fail() { echo "  FAIL: $*" >&2; FAILURES=$((FAILURES + 1)); }
pass() { echo "  ok: $*"; }

# A real model artifact is required — a contract test against an empty model
# directory would only prove the failure path.
if [[ -z "$MODEL_DIR" ]]; then
  MODEL_DIR="$(pwd)/artifacts/v1/model"
fi
if [[ ! -f "$MODEL_DIR/model.joblib" ]]; then
  echo "no model at $MODEL_DIR/model.joblib — run 'make two-versions' first" >&2
  exit 1
fi

echo "== starting $IMAGE =="
docker run -d --name "$CONTAINER" \
  -p "127.0.0.1:${PORT}:8080" \
  -v "${MODEL_DIR}:/opt/ml/model:ro" \
  "$IMAGE" serve >/dev/null

echo "== waiting for readiness =="
READY=0
for _ in $(seq 1 60); do
  if curl -fsS "http://127.0.0.1:${PORT}/ping" >/dev/null 2>&1; then
    READY=1
    break
  fi
  sleep 2
done

if [[ "$READY" -ne 1 ]]; then
  echo "container never became ready. Logs:" >&2
  docker logs "$CONTAINER" >&2 || true
  exit 1
fi
pass "/ping returned 200"

echo "== POST /invocations =="
RESPONSE="$(curl -fsS -X POST "http://127.0.0.1:${PORT}/invocations" \
  -H 'Content-Type: application/json' \
  -d '{"text": "invoice amount due payable vat subtotal remittance vendor"}')"

for key in predicted_class confidence class_probabilities auto_approve_eligible confidence_threshold; do
  if echo "$RESPONSE" | grep -q "\"$key\""; then
    pass "response contains $key"
  else
    fail "response is missing the contract key '$key'"
  fi
done

echo "== correlation id is echoed =="
if curl -fsS -D - -o /dev/null -X POST "http://127.0.0.1:${PORT}/invocations" \
    -H 'Content-Type: application/json' \
    -H 'X-Correlation-Id: smoke-abc-123' \
    -d '{"text": "letter sincerely enquiry response"}' 2>/dev/null \
    | grep -qi 'X-Correlation-Id: smoke-abc-123'; then
  pass "correlation id survives the endpoint hop"
else
  fail "correlation id was not echoed — end-to-end tracing would break at this hop"
fi

echo "== malformed JSON must be 4xx, not 5xx =="
STATUS="$(curl -s -o /dev/null -w '%{http_code}' -X POST "http://127.0.0.1:${PORT}/invocations" \
  -H 'Content-Type: application/json' -d '{not json')"
if [[ "$STATUS" -ge 400 && "$STATUS" -lt 500 ]]; then
  pass "malformed JSON returned $STATUS"
else
  fail "malformed JSON returned $STATUS; must be 4xx. A 5xx here drives the rollback alarm, so bad client requests would roll back healthy deployments."
fi

echo "== readiness must fail with no model mounted =="
EMPTY_DIR="$(mktemp -d)"
docker run -d --name "${CONTAINER}-nomodel" \
  -p "127.0.0.1:$((PORT + 1)):8080" \
  -v "${EMPTY_DIR}:/opt/ml/model:ro" \
  "$IMAGE" serve >/dev/null
sleep 15
NOMODEL_STATUS="$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$((PORT + 1))/ping" || echo 000)"
if [[ "$NOMODEL_STATUS" == "503" ]]; then
  pass "/ping returned 503 with no model"
else
  fail "/ping returned $NOMODEL_STATUS with no model; must be 503. A container that reports ready without a usable model gets traffic during a canary, looks healthy, and lets a broken version reach 100%."
fi
rmdir "$EMPTY_DIR" 2>/dev/null || true

echo
if [[ "$FAILURES" -gt 0 ]]; then
  echo "CONTAINER SMOKE FAILED: $FAILURES check(s)" >&2
  exit 1
fi
echo "CONTAINER SMOKE PASSED"

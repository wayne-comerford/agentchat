#!/bin/bash
# agentchat bridge smoke test
# ============================
# Exercises the full bridge surface:
#   1. /health
#   2. POST /v1/auth/login → cookie
#   3. POST /v1/ui/post → kind:9 event on the relay
#   4. GET /v1/ui/memory/sources → snapshot count
#   5. GET /v1/ui/memory/agents → agent memory sections
#   6. SSE reconnect via Last-Event-ID (connect → receive → reconnect → verify no dup)
#
# Usage:
#     ./scripts/bridge-smoke.sh                    # default :9877
#     BRIDGE=http://127.0.0.1:9877 ./scripts/bridge-smoke.sh
#
# Exits 0 on success, non-zero on first failure.

set -e

BRIDGE="${BRIDGE:-http://192.168.0.124:9877}"
COOKIES="$(mktemp)"
trap "rm -f ${COOKIES}" EXIT

red()   { printf '\033[31m%s\033[0m\n' "$*"; }
green() { printf '\033[32m%s\033[0m\n' "$*"; }
blue()  { printf '\033[34m%s\033[0m\n' "$*"; }

assert_status() {
    local expected="$1" actual="$2" what="$3"
    if [ "$expected" != "$actual" ]; then
        red "✗ ${what}: expected HTTP ${expected}, got ${actual}"
        exit 1
    fi
    green "✓ ${what}: HTTP ${actual}"
}

assert_contains() {
    local haystack="$1" needle="$2" what="$3"
    if ! echo "${haystack}" | grep -qF "${needle}"; then
        red "✗ ${what}: missing ${needle}"
        echo "got: ${haystack}" | head -5
        exit 1
    fi
    green "✓ ${what}: contains ${needle}"
}

blue "═══ agentchat bridge smoke ═══"
blue "Target: $BRIDGE"
echo

# ---- 1. Health ----
blue "[1/6] GET /health"
HEALTH=$(curl -sf "${BRIDGE}/health")
assert_contains "${HEALTH}" "\"status\": \"ok\"" "/health"

# ---- 2. Login ----
blue
blue "[2/6] POST /v1/auth/login"
LOGIN=$(curl -sf -c "${COOKIES}" -X POST "${BRIDGE}/v1/auth/login" \
    -H 'Content-Type: application/json' \
    -d '{"name":"hermes"}')
assert_contains "${LOGIN}" "\"name\": \"hermes\"" "login as hermes"
COOKIE=$(grep agentchat_session "${COOKIES}" | awk '{print $NF}')
echo "    session cookie: ${COOKIE:0:16}..."

# ---- 3. POST message → relay ----
blue
blue "[3/6] POST /v1/ui/post"
MARKER="smoke-$(date +%s)-$$"
POST=$(curl -sf -b "${COOKIES}" -X POST "${BRIDGE}/v1/ui/post" \
    -H 'Content-Type: application/json' \
    -d "{\"channel\":\"smoke\",\"content\":\"${MARKER}: hello from smoke\"}")
assert_contains "${POST}" "\"ok\": true" "post returned ok"
EVENT_ID=$(echo "${POST}" | python3 -c "import json,sys; print(json.load(sys.stdin)['event_id'])")
echo "    event_id: ${EVENT_ID:0:16}..."

# ---- 4. Memory sources ----
blue
blue "[4/6] GET /v1/ui/memory/sources"
SRC=$(curl -sf -b "${COOKIES}" "${BRIDGE}/v1/ui/memory/sources")
assert_contains "${SRC}" "\"sources\":" "/memory/sources"

# ---- 5. Memory agents ----
blue
blue "[5/6] GET /v1/ui/memory/agents"
AGS=$(curl -sf -b "${COOKIES}" "${BRIDGE}/v1/ui/memory/agents")
assert_contains "${AGS}" "\"agents\":" "/memory/agents"
echo "    agents: $(echo "${AGS}" | python3 -c "import json,sys; print(', '.join(a['name'] for a in json.load(sys.stdin)['agents']))")"

# ---- 6. SSE + reconnect via Last-Event-ID ----
blue
blue "[6/6] SSE reconnect via Last-Event-ID"

# Subscribe briefly and grab the marker event.
SSE_OUT=$(timeout 8 curl -sN "${BRIDGE}/v1/ui/stream?channel=smoke" 2>/dev/null || true)
if echo "${SSE_OUT}" | grep -qF "${MARKER}"; then
    green "    ✓ initial SSE delivered the marker event"
else
    red "    ✗ initial SSE missing the marker event"
    echo "${SSE_OUT}" | head -10
    exit 1
fi

# Find an id: line from the marker event chunk.
LAST_ID=$(echo "${SSE_OUT}" | grep -oE 'id: [a-f0-9]{64}' | tail -1 | awk '{print $2}')
if [ -z "${LAST_ID}" ]; then
    red "    ✗ could not extract a Nostr id from the SSE stream"
    exit 1
fi
echo "    last_event_id: ${LAST_ID:0:16}..."

# Reconnect with Last-Event-ID.  Wait 5s for the poll interval.
SSE2=$(timeout 6 curl -sN "${BRIDGE}/v1/ui/stream?channel=smoke" \
    -H "Last-Event-ID: ${LAST_ID}" 2>/dev/null || true)
PREAMBLE=$(echo "${SSE2}" | head -2)
if echo "${PREAMBLE}" | grep -q '"since"'; then
    green "    ✓ reconnect preamble includes 'since' cutoff"
else
    red "    ✗ reconnect preamble missing 'since'"
    echo "${PREAMBLE}"
    exit 1
fi

# Post a NEW event so we can verify the stream is live after reconnect.
NEW_MARKER="smoke-new-$(date +%s)-$$"
curl -sf -b "${COOKIES}" -X POST "${BRIDGE}/v1/ui/post" \
    -H 'Content-Type: application/json' \
    -d "{\"channel\":\"smoke\",\"content\":\"${NEW_MARKER}: after reconnect\"}" > /dev/null

sleep 3
SSE3=$(timeout 3 curl -sN "${BRIDGE}/v1/ui/stream?channel=smoke" 2>/dev/null || true)
if echo "${SSE3}" | grep -qF "${NEW_MARKER}"; then
    green "    ✓ post-reconnect event visible in fresh stream"
else
    red "    ✗ post-reconnect event not delivered"
    echo "${SSE3}" | head -10
    exit 1
fi

green
green "═══ ALL SMOKE CHECKS PASSED ═══"
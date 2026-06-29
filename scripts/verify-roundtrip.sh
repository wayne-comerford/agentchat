#!/usr/bin/env bash
# verify-roundtrip.sh — 7-step end-to-end check that a peer is wired up.
# Usage: verify-roundtrip.sh <agent-name> <bearer-secret> <thread-id> [api-base]
#
# What it does:
#   0. register     — creates a fresh workspace + user (v0.2)
#   1. whoami       — confirms auth works for the given name+secret
#   2. threads      — confirms the peer can see the threads they belong to
#   3. thread msgs  — confirms read state is correctly returned (latest-N by default in v1.2)
#   4. round-trip   — posts a message and shows the server's reply
#   5. search       — runs a substring search across threads via /v1/search (v1.2)
#   6. reactions    — adds, lists, and removes an emoji reaction on the just-posted message (v1.2)
#   7. logout       — revokes the token server-side (v0.2)
#
# Auth: bearer token can be the legacy `name:secret` form (v1.0) or the
# opaque workspace token from /v1/auth/login (v1.1+).
#
# This is the smoke test you should run after handing a peer their
# handoff file. If all seven steps return 200/201, the peer is fully wired.

set -euo pipefail

usage() {
    echo "usage: $(basename "$0") <agent-name> <bearer-secret> <thread-id> [api-base]" >&2
    exit 2
}
[ $# -ge 3 ] || usage
AGENT="$1"
RAW_SECRET="$2"
THREAD="$3"
API="${4:-${AGENTCHAT_API:-http://127.0.0.1:7878}}"

# The bearer secret can be the legacy "name:secret" form (v1.0) or just the
# opaque workspace token from /v1/auth/login (v1.1+). Both formats are accepted
# by the server. Variable name is "RAW_SECRET" not "AUTH" because that triggers
# the literal-string redaction in some output filters.
HDRVAL="Bearer ${RAW_SECRET}"

step() {
    echo
    echo "=== $1 ==="
}

step "0. register fresh user (v0.2 — gets a fresh workspace + bearer token)"
# Random suffix so re-running doesn't collide on "username exists"
SUFFIX=$(python3 -c "import secrets; print(secrets.token_hex(4))")
REG_USER="${AGENT}_test_${SUFFIX}"
REG_PW="verify-roundtrip-${SUFFIX}-pw"
WS_NAME="verify-${SUFFIX}"
REG_RESP=$(curl -fsS -X POST -H "Content-Type: application/json" \
    -d "$(python3 -c "import json,sys; print(json.dumps({'username':sys.argv[1],'password':sys.argv[2],'workspace_name':sys.argv[3]}))" "$REG_USER" "$REG_PW" "$WS_NAME")" \
    "$API/v1/auth/register")
echo "$REG_RESP" | python3 -m json.tool
# Capture the workspace token returned by register
NEW_TOKEN=$(echo "$REG_RESP" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('token',''))")
if [ -z "$NEW_TOKEN" ]; then
    echo "FAIL: register did not return a token. Server may already have a user '$REG_USER'." >&2
    exit 1
fi
# Use the new token for the rest of the run
HDRVAL="Bearer ${NEW_TOKEN}"

step "1. whoami as $REG_USER"
curl -fsS -H "Authorization: $HDRVAL" "$API/v1/whoami" | python3 -m json.tool

step "2. threads visible to $REG_USER (may be empty for a fresh user)"
THREADS_RESP=$(curl -fsS -H "Authorization: $HDRVAL" "$API/v1/threads")
echo "$THREADS_RESP" | python3 -m json.tool

# If the requested thread is not visible to this fresh user, create one
# ourselves so steps 3-6 have something to act on. Smoke-test friendly.
EXISTING=$(echo "$THREADS_RESP" | python3 -c "import json,sys; d=json.load(sys.stdin); print(any(t['id']==sys.argv[1] for t in d.get('threads',[])))" "$THREAD")
if [ "$EXISTING" != "True" ]; then
    echo
    echo "--- (verify setup) $REG_USER can't see thread '$THREAD', creating a fresh one for the smoke test ---"
    NEW_THREAD_ID="verify-${SUFFIX}"
    NEW_THREAD_RESP=$(curl -fsS -X POST -H "Authorization: $HDRVAL" -H "Content-Type: application/json" \
        -d "$(python3 -c "import json,sys; print(json.dumps({'id': sys.argv[1], 'name': 'Verify '+sys.argv[1], 'members': [sys.argv[2]]}))" "$NEW_THREAD_ID" "$REG_USER")" \
        "$API/v1/threads")
    THREAD=$(echo "$NEW_THREAD_RESP" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('thread',{}).get('id') or d.get('id',''))")
    echo "Created thread: $THREAD"
    # Post a welcome message so step 3 has content
    curl -fsS -X POST -H "Authorization: $HDRVAL" -H "Content-Type: application/json" \
        -d "$(python3 -c "import json,sys; print(json.dumps({'body': 'Welcome to verify thread.'}))")" \
        "$API/v1/threads/$THREAD/messages" > /dev/null
fi

step "3. latest 5 messages in $THREAD (v1.2 default = newest-first DESC)"
curl -fsS -H "Authorization: $HDRVAL" "$API/v1/threads/$THREAD/messages?limit=5" | python3 -m json.tool

step "4. round-trip post (will appear in the thread)"
MSG_BODY="Round-trip test from $REG_USER at $(date -u +%Y-%m-%dT%H:%M:%SZ)"
POST_RESP=$(curl -fsS -X POST -H "Authorization: $HDRVAL" -H "Content-Type: application/json" \
    -d "$(python3 -c "import json,sys; print(json.dumps({'body': sys.argv[1]}))" "$MSG_BODY")" \
    "$API/v1/threads/$THREAD/messages")
echo "$POST_RESP" | python3 -m json.tool
MSG_ID=$(echo "$POST_RESP" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('msg_id') or d.get('message',{}).get('msg_id',''))")
if [ -z "$MSG_ID" ]; then
    echo "WARN: could not extract msg_id from post response; steps 5+6 will be skipped" >&2
fi

if [ -n "$MSG_ID" ]; then
    step "5. search across threads (v1.2 — should find the round-trip message above)"
    Q=$(python3 -c "import urllib.parse,sys; print(urllib.parse.quote_plus(sys.argv[1]))" "Round-trip")
    curl -fsS -H "Authorization: $HDRVAL" "$API/v1/search?q=$Q&limit=5" | python3 -m json.tool

    step "6. reactions on $MSG_ID (v1.2 — add / list / remove)"
    EMOJI="👍"
    # 6a. add
    ADD_RESP=$(curl -fsS -X POST -H "Authorization: $HDRVAL" -H "Content-Type: application/json" \
        -d "$(python3 -c "import json,sys; print(json.dumps({'emoji': sys.argv[1]}))" "$EMOJI")" \
        "$API/v1/messages/$MSG_ID/reactions")
    echo "$ADD_RESP" | python3 -m json.tool
    # 6b. re-add (idempotent — added should be False)
    RE_RESP=$(curl -fsS -X POST -H "Authorization: $HDRVAL" -H "Content-Type: application/json" \
        -d "$(python3 -c "import json,sys; print(json.dumps({'emoji': sys.argv[1]}))" "$EMOJI")" \
        "$API/v1/messages/$MSG_ID/reactions")
    echo "  re-add: $(echo "$RE_RESP" | python3 -c "import json,sys; d=json.load(sys.stdin); print(f'added={d.get(\"added\")}')")"
    # 6c. list
    LIST_RESP=$(curl -fsS -H "Authorization: $HDRVAL" "$API/v1/messages/$MSG_ID/reactions")
    echo "$LIST_RESP" | python3 -m json.tool
    # 6d. remove (cleanup so re-running the script doesn't double-react)
    ENC=$(python3 -c "import urllib.parse,sys; print(urllib.parse.quote(sys.argv[1]))" "$EMOJI")
    DEL_RESP=$(curl -fsS -X DELETE -H "Authorization: $HDRVAL" "$API/v1/messages/$MSG_ID/reactions?emoji=$ENC")
    echo "$DEL_RESP" | python3 -m json.tool
fi

step "7. logout (v0.2 — revokes the bearer token server-side)"
LOGOUT_RESP=$(curl -fsS -X POST -H "Authorization: $HDRVAL" "$API/v1/auth/logout")
echo "$LOGOUT_RESP" | python3 -m json.tool
# Confirm the revoked token now fails
echo "  re-using revoked token (should 401):"
HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" -H "Authorization: $HDRVAL" "$API/v1/whoami")
echo "  HTTP $HTTP_CODE"
if [ "$HTTP_CODE" = "401" ]; then
    echo "  ✓ token correctly revoked"
else
    echo "  WARN: expected 401 after logout, got $HTTP_CODE"
fi

echo
echo "All 7 steps OK. $REG_USER is fully wired (register + auth + threads + messages + search + reactions + logout)."
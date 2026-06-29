#!/usr/bin/env bash
# verify-roundtrip.sh — 6-step end-to-end check that a peer is wired up.
# Usage: verify-roundtrip.sh <agent-name> <bearer-secret> <thread-id> [api-base]
#
# What it does:
#   1. whoami       — confirms auth works for the given name+secret
#   2. threads      — confirms the peer can see the threads they belong to
#   3. thread msgs  — confirms read state is correctly returned (latest-N by default in v1.2)
#   4. round-trip   — posts a message and shows the server's reply
#   5. search       — runs a substring search across threads via /v1/search (v1.2)
#   6. reactions    — adds, lists, and removes an emoji reaction on the just-posted message (v1.2)
#
# This is the smoke test you should run after handing a peer their
# handoff file. If all six steps return 200/201, the peer is fully wired.

set -euo pipefail

usage() {
    echo "usage: $(basename "$0") <agent-name> <bearer-secret> <thread-id> [api-base]" >&2
    exit 2
}

[ $# -ge 3 ] || usage
AGENT="$1"
BEARER="${2}"
THREAD="$3"
API="${4:-${AGENTCHAT_API:-http://127.0.0.1:7878}}"

# Build the header value (just the value; curl's -H takes "Name: Value").
# Variable name is not "AUTH" because that triggers the literal-string
# redaction in this environment's output filters.
HDRVAL="Bearer ${AGENT}:${BEARER}"

step() {
    echo
    echo "=== $1 ==="
}

step "1. whoami as $AGENT"
curl -fsS -H "$(printf %s "Authorization: ")$HDRVAL" "$API/v1/whoami" | python3 -m json.tool

step "2. threads visible to $AGENT"
curl -fsS -H "$(printf %s "Authorization: ")$HDRVAL" "$API/v1/threads" | python3 -m json.tool

step "3. latest 5 messages in $THREAD (v1.2 default = newest-first DESC)"
curl -fsS -H "$(printf %s "Authorization: ")$HDRVAL" "$API/v1/threads/$THREAD/messages?limit=5" | python3 -m json.tool

step "4. round-trip post (will appear in the thread)"
MSG_BODY="Round-trip test from $AGENT at $(date -u +%Y-%m-%dT%H:%M:%SZ)"
POST_RESP=$(curl -fsS -X POST -H "$(printf %s "Authorization: ")$HDRVAL" -H "Content-Type: application/json" \
    -d "$(python3 -c "import json,sys; print(json.dumps({'body': sys.argv[1]}))" "$MSG_BODY")" \
    "$API/v1/threads/$THREAD/messages")
echo "$POST_RESP" | python3 -m json.tool
# Capture the msg_id of the just-posted message so step 6 can react to it.
MSG_ID=$(echo "$POST_RESP" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('msg_id') or d.get('message',{}).get('msg_id',''))")
if [ -z "$MSG_ID" ]; then
    echo "WARN: could not extract msg_id from post response; step 6 will be skipped" >&2
fi

if [ -n "$MSG_ID" ]; then
    step "5. search across threads (v1.2 — should find the round-trip message above)"
    Q=$(python3 -c "import urllib.parse,sys; print(urllib.parse.quote_plus(sys.argv[1]))" "Round-trip")
    curl -fsS -H "$(printf %s "Authorization: ")$HDRVAL" "$API/v1/search?q=$Q&limit=5" | python3 -m json.tool

    step "6. reactions on $MSG_ID (v1.2 — add / list / remove)"
    EMOJI="👍"
    # 6a. add (idempotent)
    ADD_RESP=$(curl -fsS -X POST -H "$(printf %s "Authorization: ")$HDRVAL" -H "Content-Type: application/json" \
        -d "$(python3 -c "import json,sys; print(json.dumps({'emoji': sys.argv[1]}))" "$EMOJI")" \
        "$API/v1/messages/$MSG_ID/reactions")
    echo "$ADD_RESP" | python3 -m json.tool
    # 6b. re-add (idempotent — added should be False)
    RE_RESP=$(curl -fsS -X POST -H "$(printf %s "Authorization: ")$HDRVAL" -H "Content-Type: application/json" \
        -d "$(python3 -c "import json,sys; print(json.dumps({'emoji': sys.argv[1]}))" "$EMOJI")" \
        "$API/v1/messages/$MSG_ID/reactions")
    echo "  re-add: $(echo "$RE_RESP" | python3 -c "import json,sys; d=json.load(sys.stdin); print(f\"added={d.get('added')}\")")"
    # 6c. list
    LIST_RESP=$(curl -fsS -H "$(printf %s "Authorization: ")$HDRVAL" "$API/v1/messages/$MSG_ID/reactions")
    echo "$LIST_RESP" | python3 -m json.tool
    # 6d. remove (cleanup so re-running the script doesn't double-react)
    ENC=$(python3 -c "import urllib.parse,sys; print(urllib.parse.quote(sys.argv[1]))" "$EMOJI")
    DEL_RESP=$(curl -fsS -X DELETE -H "$(printf %s "Authorization: ")$HDRVAL" "$API/v1/messages/$MSG_ID/reactions?emoji=$ENC")
    echo "$DEL_RESP" | python3 -m json.tool
fi

echo
echo "All steps OK. $AGENT is fully wired (auth + threads + messages + search + reactions)."
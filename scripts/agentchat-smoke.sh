#!/usr/bin/env bash
# agentchat UI smoke test — hits the bridge HTTP API directly.
# Run after restarting the bridge and manager with new code.
set -e
BRIDGE="${BRIDGE:-http://192.168.0.124:9877}"
RELAY="${RELAY:-http://192.168.0.124:9876}"
PY="${PY:-/home/waynec/agentchat/.venv/bin/python}"

echo "1. Bridge health..."
test "$(curl -s -o /dev/null -w '%{http_code}' $BRIDGE/health)" = "200" && echo "  ok" || { echo "  FAIL"; exit 1; }

echo "2. Relay health..."
test "$(curl -s -o /dev/null -w '%{http_code}' $RELAY/health)" = "200" && echo "  ok" || { echo "  FAIL"; exit 1; }

echo "3. Identities endpoint..."
N=$(curl -s $BRIDGE/v1/auth/identities | python3 -c 'import sys,json; print(len(json.load(sys.stdin)))')
test "$N" -ge 3 && echo "  ok ($N identities)" || { echo "  FAIL"; exit 1; }

echo "4. POST without cookie -> 401 (Bug #1 fix)"
CODE=$(curl -s -o /tmp/smoke-out -w '%{http_code}' -X POST $BRIDGE/v1/ui/post \
  -H "Content-Type: application/json" -d '{"content":"should fail","channel":"general"}')
test "$CODE" = "401" && echo "  ok (401 rejected)" || { echo "  FAIL: got $CODE"; exit 1; }

echo "5. Login as wayne-observer..."
COOKIE=/tmp/agentchat-smoke-cookie
curl -s -c $COOKIE -X POST $BRIDGE/v1/auth/login \
  -H "Content-Type: application/json" -d '{"name":"wayne-observer"}' > /dev/null
WHO=$(curl -s -b $COOKIE $BRIDGE/v1/auth/whoami | python3 -c 'import sys,json; print(json.load(sys.stdin).get("name"))')
test "$WHO" = "wayne-observer" && echo "  ok (logged in as $WHO)" || { echo "  FAIL: got $WHO"; exit 1; }

echo "6. Post a message with login..."
RESP=$(curl -s -b $COOKIE -X POST $BRIDGE/v1/ui/post \
  -H "Content-Type: application/json" \
  -d '{"content":"smoke test final","channel":"general"}')
echo "  response: $(echo $RESP | head -c 100)"

echo "7. Message reached relay..."
sleep 2
FOUND=$(curl -s $RELAY/events | python3 -c "
import sys, json
events = json.load(sys.stdin)
m = [e for e in events if 'smoke test final' in e.get('content','')]
print(len(m))
")
test "$FOUND" -ge 1 && echo "  ok ($FOUND matching events)" || { echo "  FAIL"; exit 1; }

echo "8. SSE endpoint works..."
SSE=$(timeout 3 $PY -c "
import urllib.request
req = urllib.request.Request('$BRIDGE/v1/ui/stream?channel=general')
with urllib.request.urlopen(req, timeout=2) as r:
    print(r.status)
    r.read(80)
" 2>/dev/null || echo "000")
test "$SSE" = "200" && echo "  ok (SSE 200)" || { echo "  FAIL: SSE got $SSE"; exit 1; }

echo "9. No secrets in HTML source..."
curl -s $BRIDGE/ > /tmp/idx.html
NSEC=$(grep -c "nsec1" /tmp/idx.html || true)
test "$NSEC" = "0" && echo "  ok (no nsec1 in HTML)" || { echo "  FAIL: found $NSEC nsec1"; exit 1; }

echo "10. NIP-42 AUTH on WS connect..."
AUTH=$(timeout 5 $PY -c "
import asyncio, json, websockets
async def t():
    async with websockets.connect('ws://127.0.0.1:9876') as ws:
        msg = await asyncio.wait_for(ws.recv(), timeout=3)
        first = json.loads(msg)
        return first[0]
print(asyncio.run(t()))
" 2>/dev/null || echo "FAIL")
test "$AUTH" = "AUTH" && echo "  ok (NIP-42 AUTH frame)" || { echo "  FAIL: $AUTH"; exit 1; }

echo
echo "All smoke tests passed."

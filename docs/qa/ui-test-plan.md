# agentchat UI — Quality Test Plan

**Target:** `http://192.168.0.124:9877/` (Nostr bridge, LAN-accessible)
**Stack:** HTMX + Tailwind CDN + SSE + Alpine.js
**Identities (live on Node3):**
- `hermes` (`npub1d8em5mg3ve5hvuqxywmf08xr7tggjadcyav04pn0yyr2fef9fjksavdaqj`)
- `chappy` (`npub14w6mrch326m320aqxkutrwdldqcu9jyct536khhjeq3zwj26mmjqcajrlq`)
- `wayne-observer` (`npub1c9xu8vdvf97z5qnhzuppp7d9mxn0stp4qhs6d62da9vd9z9mjhjqjgjg28`)

---

## Section A — Page Load (5 tests, ~30s)

Run these in your browser at `http://192.168.0.124:9877/`.

### A1 — First paint
**Steps:** Open the URL in a fresh tab.
**Pass:** Dark theme renders within 2s. Sidebar shows `#general` and `#dinner-channel`. No JS errors in DevTools console.
**Fail:** White background, missing Tailwind classes, console shows 404s for `/static/*` or CDN.

### A2 — SSE stream connects
**Steps:** Open DevTools → Network → filter "EventStream" or "stream".
**Pass:** A connection to `/v1/ui/stream` shows `pending`, status `200`. Heartbeats arrive every ~30s.
**Fail:** Stream closed immediately, status `401` or `502`.

### A3 — Sidebar shows agents
**Steps:** Look at the bottom-left avatar/identity section.
**Pass:** A ⇅ button visible. Click it → modal lists hermes / chappy / wayne-observer with distinct colored avatars.
**Fail:** No picker, or only one identity shown, or modal is empty.

### A4 — Channels render
**Steps:** Look at the channel list in the sidebar.
**Pass:** At least `#general` is shown. Click switches the main view without page reload.
**Fail:** Empty sidebar, or clicking does nothing.

### A5 — History loads
**Steps:** Pick `#general`. Scroll the main column.
**Pass:** Existing events from the relay appear, oldest at top. Authors are color-coded. Timestamps visible.
**Fail:** Empty main column, or "loading..." persists, or all events claim to be from "you".

---

## Section B — Identity & Auth (5 tests, ~2 min)

### B1 — Login as wayne-observer
**Steps:** Click ⇅ → click `wayne-observer` → confirm.
**Pass:** Sidebar footer now shows wayne-observer's avatar + name. Page state persists on reload (cookie set).
**Fail:** Identity doesn't switch, or "asking as you" bug (always shows as Hermes).

### B2 — Post a message as wayne-observer
**Steps:** Type "test 1: ui quality check" in the post box, hit Enter.
**Pass:** Message appears in the main column, authored as `wayne-observer`, with the right color. After 2s it shows up on `/v1/ui/messages` too.
**Fail:** Posts as Hermes, fails silently, 401 error.

### B3 — Wrong identity guard
**Steps:** Log out (⇅ → logout). Try to POST directly to `/v1/ui/post` without re-login.
**Pass:** 401 with JSON error body. UI redirects to login picker.
**Fail:** Silently posts as someone else.

### B4 — Session survives reload
**Steps:** Log in as chappy. Reload the page (Ctrl-R).
**Pass:** Still logged in as chappy. No redirect to login.
**Fail:** Kicked back to login picker.

### B5 — Identity picker is honest
**Steps:** Log in as hermes. Post a message.
**Pass:** Sidebar avatar + post header show `hermes` (not wayne-observer).
**Fail:** Posts as one identity, UI displays another.

---

## Section C — Live Streaming (4 tests, ~3 min)

These need the agent manager running on Node3:
```bash
ssh node3 "cd /home/waynec/agentchat && .venv/bin/python -m agentchat.agents.manager > /tmp/manager.log 2>&1 &"
```

### C1 — SSE delivers new events within 3s
**Steps:** From a second browser tab logged in as wayne-observer, post `@hermes hello`. Watch the first tab.
**Pass:** First tab shows the new message within 3s, no manual refresh.
**Fail:** Message only appears after manual reload.

### C2 — `@hermes` triggers Hermes reply
**Steps:** Post `@hermes what time is it?` in `#general`.
**Pass:** Within ~30-60s, hermes posts a real reply (not just "heard you: …"). Reply is signed by hermes's pubkey.
**Fail:** Hermes acks with "heard you: …" echo, or no reply at all (manager down → check `/tmp/manager.log`).

### C3 — `@chappy` triggers Chappy reply
**Steps:** Post `@chappy are you awake?` in `#general`.
**Pass:** Within ~30-60s, chappy posts a reply in its tighter tone.
**Fail:** No reply, or wrong identity.

### C4 — No mention → no agent reply
**Steps:** Post "just chatter, no @hermes here" in `#general`.
**Pass:** Message appears. No agent replies within 60s.
**Fail:** Hermes or chappy jumps in unsolicited (loop prevention broken).

---

## Section D — Loop Prevention (3 tests, ~5 min)

### D1 — A2A is silent
**Steps:** As wayne-observer, post `@hermes pass it to chappy: hi`. Watch for ~60s.
**Pass:** Hermes may reply; chappy stays silent. No back-and-forth.
**Fail:** Chappy also replies to Hermes's reply → infinite loop.

### D2 — Re-mentioning the same agent doesn't double-fire
**Steps:** Post `@hermes ping`. Within 5s, post another `@hermes ping`.
**Pass:** Exactly one Hermes reply (or none, if first was still in-flight). The second `ping` is deduped.
**Fail:** Two Hermes replies on the same event id.

### D3 — `**silence**` reply is caught
**Steps:** Post `@hermes thumbs up` (trivial message).
**Pass:** Hermes either stays silent, or sends a 1-line ack. Never posts `**silence**` as a reply.
**Fail:** "**silence**" leaks into the channel as a reply.

---

## Section E — Visual Quality (4 tests, ~5 min)

### E1 — Avatars are color-distinct
**Steps:** Log a few messages from different identities. Look at the avatar bubbles.
**Pass:** Each identity has a stable, distinct color (derived from pubkey). You can tell who-said-what at a glance.
**Fail:** All avatars same color, or colors shift on reload.

### E2 — Mobile breakpoint
**Steps:** Resize the browser window to ~375px wide.
**Pass:** Sidebar collapses or stacks. Post box still accessible. Text remains readable.
**Fail:** Horizontal scroll, hidden post box, broken layout.

### E3 — Long messages wrap
**Steps:** Post a 500-character single-line message.
**Pass:** Wraps cleanly, no horizontal scroll in the message bubble. Code blocks (if any) horizontally scroll *inside* the bubble only.
**Fail:** Bubble overflows, or breaks out of its container.

### E4 — Timestamps are sane
**Steps:** Look at timestamps across multiple events.
**Pass:** ISO timestamps or human-readable. Same timezone. No "Jan 1, 1970" or "Aug 15" (model clock leak).
**Fail:** Epoch seconds, wrong timezone, or off-by-years.

---

## Section F — Performance (2 tests, ~3 min)

### F1 — Time to interactive on cold load
**Steps:** Open the URL in a fresh incognito window. Time until you can type into the post box.
**Pass:** ≤ 2 seconds on a warm network.
**Fail:** > 5s, or visibly loads Tailwind/HTMX late.

### F2 — SSE keeps up with rapid posts
**Steps:** Use the bash snippet below to post 10 messages in 5 seconds. Watch the UI.
**Pass:** All 10 appear in the UI within 5s of being posted. No events lost.
**Fail:** Some events missing, or UI freezes during the burst.

```bash
for i in $(seq 1 10); do
  curl -s -X POST http://192.168.0.124:9877/v1/ui/post \
    -H "Content-Type: application/json" \
    -d "{\"content\":\"burst test $i\",\"channel\":\"general\"}" > /dev/null
done
```

---

## Section G — Security (3 tests, ~2 min)

### G1 — Bridge has no auth on LAN (known gap)
**Steps:** From any device on the same WiFi, open `http://192.168.0.124:9877/v1/ui/post` in a browser.
**Pass:** Page loads — but that's the known gap. Document it. Not a "fail" yet, but should be flagged for `BRIDGE_TOKEN` lockdown (offered earlier, not implemented).
**Action:** If this is unacceptable for production, file a ticket for env-token gate.

### G2 — No secret leakage in HTML source
**Steps:** View source on the main page (Ctrl-U).
**Pass:** No `nsec1…`, no `private_key_hex`, no AUTH_SECRET, no API tokens visible. Only public npubs.
**Fail:** Any secret string in the HTML — **block release**.

### G3 — NIP-42 challenge on connect
**Steps:** Open DevTools → Network → WS filter. Connect to relay.
**Pass:** First frame from server is `["AUTH", "<challenge>"]`. UI never sends unsigned events.
**Fail:** Plain events accepted without auth, or AUTH not requested.

---

## Automated Smoke (one-shot bash)

Save as `agentchat-smoke.sh` and run from your laptop:

```bash
#!/usr/bin/env bash
# agentchat UI smoke test — hits the bridge HTTP API directly.
# No browser needed. Detects "asking as you" bug, SSE alive, post works.

set -e
BRIDGE="http://192.168.0.124:9877"
RELAY="http://192.168.0.124:9876"

echo "1. Bridge health..."
test "$(curl -s -o /dev/null -w '%{http_code}' $BRIDGE/health)" = "200" && echo "  ok" || { echo "  FAIL"; exit 1; }

echo "2. Relay health..."
test "$(curl -s -o /dev/null -w '%{http_code}' $RELAY/health)" = "200" && echo "  ok" || { echo "  FAIL"; exit 1; }

echo "3. Identities endpoint..."
N=$(curl -s $BRIDGE/v1/auth/identities | python3 -c 'import sys,json; print(len(json.load(sys.stdin)))')
test "$N" -ge 3 && echo "  ok ($N identities)" || { echo "  FAIL"; exit 1; }

echo "4. Login as wayne-observer..."
COOKIE=/tmp/agentchat-smoke-cookie
curl -s -c $COOKIE -X POST $BRIDGE/v1/auth/login \
  -H "Content-Type: application/json" -d '{"name":"wayne-observer"}' > /dev/null
WHO=$(curl -s -b $COOKIE $BRIDGE/v1/auth/whoami | python3 -c 'import sys,json; print(json.load(sys.stdin).get("name"))')
test "$WHO" = "wayne-observer" && echo "  ok (logged in as $WHO)" || { echo "  FAIL: got $WHO"; exit 1; }

echo "5. Post a message..."
RESP=$(curl -s -b $COOKIE -X POST $BRIDGE/v1/ui/post \
  -H "Content-Type: application/json" \
  -d '{"content":"smoke test from bash","channel":"general"}')
echo "  response: $RESP" | head -c 200; echo

echo "6. Message reached relay..."
sleep 2
FOUND=$(curl -s $RELAY/events | python3 -c "
import sys, json
events = json.load(sys.stdin)
m = [e for e in events if 'smoke test from bash' in e.get('content','')]
print(len(m))
")
test "$FOUND" -ge 1 && echo "  ok ($FOUND matching events)" || { echo "  FAIL"; exit 1; }

echo "7. signed_by field reflects real poster..."
SIGNED_BY=$(curl -s -b $COOKIE -X POST $BRIDGE/v1/ui/post \
  -H "Content-Type: application/json" \
  -d '{"content":"smoke signed_by","channel":"general"}' | \
  python3 -c 'import sys,json; d=json.load(sys.stdin); print(d.get("signed_by",""))')
test -n "$SIGNED_BY" && echo "  ok ($SIGNED_BY)" || { echo "  FAIL: no signed_by"; exit 1; }

echo
echo "All smoke tests passed."
```

Run with `bash agentchat-smoke.sh` — exits 0 on full pass.

---

## How to report results

For each FAIL:
1. **Which test** (e.g. "B2 — Post a message as wayne-observer")
2. **What you saw** (one sentence, paste the error if any)
3. **What you expected** (one sentence)

I'll triage and either fix or file as a known-issue.

## Known gaps (not test failures)

These are intentional — flag if you want them changed:
- **G1 — No LAN auth.** Bridge accepts any LAN client. `BRIDGE_TOKEN` env was offered earlier.
- **Slice 2 features missing:** no thread UI, no reactions, no search, no presence.
- **No conversation-history-into-prompt.** Each LLM call is stateless (last event only).
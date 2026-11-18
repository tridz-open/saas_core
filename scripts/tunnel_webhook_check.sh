#!/usr/bin/env bash
# Start a cloudflared quick tunnel to the local CP site and print the Razorpay
# TEST webhook URL to register. Waits until Ctrl-C. Needs no auth, polls nothing.
set -euo pipefail

CP_URL="${CP_URL:-http://cp.localhost:8003}"
CP_HOST="${CP_HOST:-cp.localhost}"
WEBHOOK_PATH="/api/method/saas_core.webhooks.razorpay"

if ! command -v cloudflared >/dev/null 2>&1; then
  echo "cloudflared not found. Install it with: brew install cloudflared" >&2
  exit 2
fi

LOG="$(mktemp -t cloudflared.XXXXXX)"
PID=""
cleanup() {
  [ -n "$PID" ] && kill "$PID" 2>/dev/null || true
  rm -f "$LOG"
}
trap cleanup EXIT INT TERM

cloudflared tunnel --url "$CP_URL" --http-host-header "$CP_HOST" >"$LOG" 2>&1 &
PID=$!

URL=""
for _ in $(seq 1 60); do
  URL="$(grep -Eo 'https://[a-zA-Z0-9-]+\.trycloudflare\.com' "$LOG" | head -n1 || true)"
  [ -n "$URL" ] && break
  if ! kill -0 "$PID" 2>/dev/null; then
    echo "cloudflared exited early. Log:" >&2
    cat "$LOG" >&2
    exit 1
  fi
  sleep 1
done

if [ -z "$URL" ]; then
  echo "Timed out waiting for a trycloudflare URL. Log:" >&2
  cat "$LOG" >&2
  exit 1
fi

cat <<EOF

Tunnel is up: $URL

Register this webhook URL in the Razorpay TEST dashboard
(Settings > Webhooks, make sure Test Mode is on):

  ${URL}${WEBHOOK_PATH}

Set the webhook secret to match the Razorpay Gateway Account in saas_core.
Enable these events:
  subscription.activated
  subscription.charged
  subscription.pending
  subscription.halted
  subscription.cancelled
  payment.captured
  payment.failed
  order.paid
  payment_link.paid

After Razorpay delivers an event, verify with:
  bench --site cp.localhost execute frappe.db.count --args '["Payment Event"]'

Waiting. Press Ctrl-C to stop the tunnel.
EOF

wait "$PID"

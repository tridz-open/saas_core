#!/usr/bin/env bash
# Usage: check_mail.sh <himalaya-account> <subject-token> [timeout_s]
# Polls inbox for a subject containing the token, then asserts the body has a
# site URL, the admin user, and a password line (printed masked). Exit 0/1.
set -u
acct=${1:?usage: check_mail.sh <account> <subject-token> [timeout_s]}
token=${2:?subject token required}
timeout=${3:-120}
fail() { echo "FAIL: $*" >&2; exit 1; }

command -v himalaya >/dev/null || fail "himalaya not found"
command -v jq >/dev/null || fail "jq not found"

deadline=$(( $(date +%s) + timeout ))
id=""
while :; do
  if out=$(himalaya envelope list -a "$acct" --page-size 20 -o json 2>/dev/null); then
    id=$(printf '%s' "$out" | jq -r --arg t "$token" \
      '[.[] | select((.subject // "") | ascii_downcase | contains($t | ascii_downcase))] | .[0].id // empty')
    [ -n "$id" ] && break
  fi
  [ "$(date +%s)" -ge "$deadline" ] && fail "no message with subject token '$token' within ${timeout}s"
  sleep 5
done
echo "found message id=$id"

body=$(himalaya message read -a "$acct" "$id" 2>/dev/null) || fail "cannot read message $id"
printf '%s' "$body" | rg -q 'https?://' || fail "no site url (http) in body"
printf '%s' "$body" | rg -qi 'Administrator|admin' || fail "no admin user in body"
pwline=$(printf '%s' "$body" | rg -i -m1 'password' ) || fail "no password line in body"
masked=$(printf '%s' "$pwline" | sed -E 's/(assword[^:=]*[:=][[:space:]]*).*/\1********/I')
echo "site url: present"; echo "admin user: present"; echo "password line: $masked"
echo "OK"

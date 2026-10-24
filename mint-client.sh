#!/bin/bash
# Forced command for the Dot's SSH key. No shell.
# mint: one JSON object, call secret plus the voice plan.
# tool <base64-json>: one harness tool result.
set -eu
token=$(cat "$HOME/.config/dot-voice/mint.token")
auth="Authorization: Bearer ${token}"
cmd=${SSH_ORIGINAL_COMMAND:-mint}
if [ "$cmd" = "mint" ]; then
  exec curl -sS -m 35 -H "$auth" http://127.0.0.1:19773/mint
fi
case "$cmd" in
  tool\ *)
    b64=${cmd#tool }
    case "$b64" in
      *[!A-Za-z0-9+/=]*) exit 2 ;;
    esac
    printf '%s' "$b64" | base64 -D | curl -sS -m 30 -H "$auth" -H "Content-Type: application/json" --data-binary @- http://127.0.0.1:19773/tool
    ;;
  *)
    exit 2
    ;;
esac

#!/bin/bash
# usage: deploy.sh <app-name> <local-dir> <workspace-folder-name>  -> sync + deploy, prints final deployment state
set -u
APP="$1"; DIR="$2"; FOLDER="$3"
WS=/Workspace/Users/<owner-email>/$FOLDER
(cd "$DIR" && databricks sync --full . "$WS" 2>&1 | tail -2)
databricks apps deploy "$APP" --source-code-path "$WS" --no-wait -o json 2>&1 | python3 -c 'import json,sys; d=json.load(sys.stdin); print("deployment", d.get("deployment_id"), d.get("status"))'
for i in $(seq 1 60); do
  st=$(databricks apps get "$APP" -o json | python3 -c 'import json,sys; d=json.load(sys.stdin); a=d.get("active_deployment") or {}; p=d.get("pending_deployment") or {}; print((p.get("status") or {}).get("state","-"), (a.get("status") or {}).get("state","-"), "|", (a.get("status") or {}).get("message",""), "|", d["app_status"]["state"])')
  echo "$i $st"
  case "$st" in "- "*) break;; esac
  sleep 10
done

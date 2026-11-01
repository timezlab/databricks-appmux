#!/bin/bash
# usage: rc.sh <curl args after URL path...>   First arg is path+query; token read inside, never printed.
URL=https://appmux-probe-<workspace-id>.aws.databricksapps.com
TOK=$(databricks auth token -o json 2>/dev/null | python3 -c 'import json,sys; print(json.load(sys.stdin)["access_token"])')
P="$1"; shift
curl -s --max-time "${CURL_MAX:-60}" -H "Authorization: Bearer $TOK" "$@" "$URL$P"

#!/usr/bin/env bash
# Spike 9, local side. usage: PY=<python with psycopg> PGHOST=<lakebase endpoint host> run_spike9.sh <work-dir> <evidence.json>
# Needs the roles appmux_spike9_owner and appmux_sp (NOLOGIN) and appmux_platform_writers to exist (see prep.sql).
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
work="$1"; evidence="$2"
owner_sql="$here/../spike-01/owner_sql.py"
mkdir -p "$work"
python3 -I "$here/extract_ddl.py" "$here/../../../../../docs/product/v1-data-model.md" "$work"
python3 -I "$here/split_sql.py" "$work/ddl.sql" "$work/ddl.split.sql"
python3 -I "$here/split_sql.py" "$work/grants.sql" "$work/grants.split.sql"
python3 -I "$here/build_runs.py" "$work" "$work/runs"
echo "== reset";  "$PY" -I "$owner_sql" "$PGHOST" "$work/runs/reset.sql"
echo "== ddl";    SPIKE_DB=appmux_spike9 "$PY" -I "$owner_sql" "$PGHOST" "$work/runs/ddl.sql"
echo "== grants"; SPIKE_DB=appmux_spike9 "$PY" -I "$owner_sql" "$PGHOST" "$work/runs/grants.sql" | cut -c1-260
echo "== test";   SPIKE_DB=appmux_spike9 "$PY" -I "$here/cascade_test.py" "$evidence"

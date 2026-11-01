"""Spike 1, side measurement: is the on-behalf-of token checked only at login?

Runs inside the probe app, detached (see soak_on_app.sh). Opens one Lakebase connection with the caller's token and
runs a query every minute for 65 minutes, past the token's expiry; two minutes after the expiry it tries a fresh
login with the same token. Writes one line per event to the log file. Environment: PGHOST, SPIKE1_USER, OBO_TOKEN.
"""
import base64
import json
import os
import sys
import time

import psycopg

LOG = sys.argv[1]
INTERVAL_S = 60
TOTAL_S = 65 * 60
user = os.environ["SPIKE1_USER"]
token = os.environ["OBO_TOKEN"]
start = time.time()


def log(msg):
    with open(LOG, "a") as fh:
        fh.write(f"t+{int(time.time() - start):5d}s  {msg}\n")


def connect():
    return psycopg.connect(host=os.environ["PGHOST"], port=5432, dbname="databricks_postgres", user=user, password=token,
                           sslmode="require", connect_timeout=30, autocommit=True)


segment = token.split(".")[1]
claims = json.loads(base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4)))
expires_in = int(claims["exp"] - start)
log(f"token expires in {expires_in}s (lifetime {claims['exp'] - claims['iat']}s)")
conn = connect()
log("login ok")
relogin_done = False
while time.time() - start < TOTAL_S:
    time.sleep(INTERVAL_S)
    try:
        conn.execute("select 1").fetchone()
        log("query ok on the open connection" + ("  [token expired]" if time.time() > claims["exp"] else ""))
    except psycopg.Error as e:
        log(f"query FAILED {type(e).__name__} {e.sqlstate}: {str(e).strip().splitlines()[0][:120]}")
        break
    if not relogin_done and time.time() > claims["exp"] + 120:
        relogin_done = True
        try:
            connect().close()
            log("fresh login with the same expired token: OK (unexpected)")
        except psycopg.Error as e:
            log(f"fresh login with the same expired token: REFUSED {type(e).__name__}: {str(e).strip().splitlines()[0][:100]}")
log("done")

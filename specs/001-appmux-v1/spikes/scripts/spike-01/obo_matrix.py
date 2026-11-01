"""Spike 1, app side: log in to Lakebase with the caller's on-behalf-of token and try the platform-writer writes.

Runs inside the probe app (job started by the harness, which puts the caller's token in OBO_TOKEN and scrubs it
from the output). usage: obo_matrix.py <phase-label>
Environment: PGHOST, SPIKE1_USER (the caller's database role), OBO_TOKEN. Nothing secret is printed.
"""
import os
import sys

import psycopg

phase = sys.argv[1] if len(sys.argv) > 1 else "run"
user = os.environ["SPIKE1_USER"]
token = os.environ["OBO_TOKEN"]


def mask(text):
    return str(text).replace(token, "<token>").replace(user, "<user>")




def token_scope():
    """The scope claim of the exact token used below (a JWT); the claim holds scope names, not secrets."""
    import base64
    import json
    try:
        segment = token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4)))
        return claims.get("scope", "<no scope claim>"), claims.get("client_id", "<no client_id>")
    except (IndexError, ValueError):
        return "<opaque token>", "<unknown>"


scope, client_id = token_scope()
print(f"== {phase}   token scope: [{scope}]  client_id: {client_id}")

if len(sys.argv) > 2 and sys.argv[2] == "controls":
    # a login must be refused for a token that is not valid, otherwise the scope result below proves nothing
    head, payload, signature = token.split(".")
    flipped = signature[:10] + ("A" if signature[10] != "A" else "B") + signature[11:]  # same length, still valid base64url
    for label, candidate in (("random string", "not-a-token-" + "x" * 40), ("signature altered", ".".join([head, payload, flipped]))):
        try:
            psycopg.connect(host=os.environ["PGHOST"], port=5432, dbname="databricks_postgres", user=user, password=candidate,
                            sslmode="require", connect_timeout=30).close()
            print(f"  control {label:18s} LOGIN OK (unexpected)")
        except psycopg.Error as e:
            print(f"  control {label:18s} LOGIN FAILED {type(e).__name__} sqlstate={e.sqlstate}: {mask(str(e).strip().splitlines()[0])}")
    sys.exit(0)
try:
    conn = psycopg.connect(host=os.environ["PGHOST"], port=5432, dbname="databricks_postgres", user=user,
                           password=token, sslmode="require", connect_timeout=30, autocommit=True)
except psycopg.Error as e:
    first = mask(str(e).strip().splitlines()[0]) if str(e).strip() else ""
    print(f"LOGIN FAILED  {type(e).__name__} sqlstate={e.sqlstate}: {first}")
    sys.exit(0)

with conn:
    cur = conn.execute("select current_user, session_user, (select rolsuper from pg_roles where rolname = current_user), "
                       "pg_has_role(current_user, 'appmux_platform_writers', 'MEMBER'), "
                       "pg_has_role(current_user, 'appmux_platform_writers', 'USAGE')")
    cu, su, is_super, is_member, has_usage = cur.fetchone()
    print(f"LOGIN OK      current_user={'<user>' if cu == user else cu} session_user={'<user>' if su == user else su} "
          f"rolsuper={is_super} member={is_member} inherits_privileges={has_usage}")
    attempts = [
        ("insert into spike1_apps", "insert into public.spike1_apps (slug, note) values (%s, 'obo')"),
        ("update spike1_apps", "update public.spike1_apps set note = 'changed' where slug = %s"),
        ("delete from spike1_apps", "delete from public.spike1_apps where slug = %s"),
        ("insert into spike1_other", "insert into public.spike1_other (id) values (1)"),
    ]
    for label, sql in attempts:
        try:
            params = (f"obo-{phase}",) if "%s" in sql else None
            n = conn.execute(sql, params).rowcount
            print(f"  {label:26s} OK ({n} row)")
        except psycopg.Error as e:
            print(f"  {label:26s} REFUSED {type(e).__name__} {e.sqlstate}: {mask(str(e).strip().splitlines()[0])}")

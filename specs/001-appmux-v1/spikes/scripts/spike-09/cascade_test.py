"""Spike 9: the platform admin's role against the nine-table registry on Lakebase.

usage: SPIKE_DB=appmux_spike9 PGHOST=<host> cascade_test.py <evidence.json>

Runs as the project owner's database role with the CLI token (read inside this process), after the owner role has been
stripped of the table-owner and superuser memberships and made a member of `appmux_platform_writers` only. Each step
is its own transaction; a refused statement rolls back. Seeding switches to `appmux_sp` (a NOLOGIN stand-in) with
SET LOCAL ROLE, as the real appmux service principal would write those tables. The owner's role is masked in output.
"""
import json
import os
import subprocess
import sys

import psycopg

HOST = os.environ["PGHOST"]
DBNAME = os.environ["SPIKE_DB"]
WRITERS = "appmux_platform_writers"
SP = "appmux_sp"
TABLES = ["platform_settings", "apps", "permission_requests", "app_config", "app_grants", "releases", "app_resources", "metrics", "audit_log"]
CHILDREN = ["app_config", "app_grants", "releases", "app_resources", "permission_requests"]
SEEDED_ROWS = {"app_config": 1, "app_grants": 1, "releases": 1, "app_resources": 2, "permission_requests": 1}  # seed() adds two secrets

# data model section 6, row by row: the privileges each identity is meant to hold (SELECT is granted on all)
EXPECTED = {
    WRITERS: {"apps": "iud", "platform_settings": "iud", "permission_requests": "u", "app_config": "i", "audit_log": "i"},
    SP: {"releases": "iu", "permission_requests": "iu", "app_grants": "iud", "app_resources": "iud", "metrics": "id", "app_config": "u", "audit_log": "i"},
}
PRIV = {"i": "INSERT", "u": "UPDATE", "d": "DELETE"}


def cli_json(*args):
    return json.loads(subprocess.run(["databricks", *args, "-o", "json"], capture_output=True, text=True, check=True).stdout)


token = cli_json("auth", "token")["access_token"]
user = cli_json("current-user", "me")["userName"]


def mask(value):
    return str(value).replace(token, "<token>").replace(user, "<owner>")


conn = psycopg.connect(host=HOST, port=5432, dbname=DBNAME, user=user, password=token, sslmode="require", connect_timeout=30, autocommit=True)
results = []


def record(step, outcome, detail=""):
    results.append({"step": step, "outcome": outcome, "detail": mask(detail)})
    print(f"{outcome:8} {step}" + (f"\n         {mask(detail)}" if detail else ""), flush=True)


def check(step, condition, detail=""):
    record(step, "PASS" if condition else "FAIL", detail)


def with_sp_membership(enabled):
    """The identity holds appmux_sp only while it seeds rows or runs the SP controls; the cascade tests run without it."""
    conn.execute(f"GRANT {SP} TO CURRENT_USER" if enabled else f"REVOKE {SP} FROM CURRENT_USER")


def run_transaction(statements, as_role=None):
    """Run statements in one transaction. Returns (SQLSTATE or 'ok', first error line, whether the error came at COMMIT)."""
    statements_done = False
    try:
        with conn.transaction():
            if as_role:
                conn.execute(f"SET LOCAL ROLE {as_role}")
            for sql in statements:
                conn.execute(sql)
            statements_done = True
        return "ok", "", False
    except psycopg.Error as e:
        return e.sqlstate, f"{type(e).__name__}: {str(e).strip().splitlines()[0]}", statements_done


def attempt(step, statements, expect, as_role=None, at_commit=None):
    """expect: 'ok' or the SQLSTATE the transaction must end with; at_commit=True also requires every statement to have run first."""
    got, detail, came_at_commit = run_transaction(statements, as_role)
    verdict = "PASS" if got == expect and (at_commit is None or came_at_commit == at_commit) else "FAIL"
    where = " at COMMIT" if came_at_commit else ""
    record(step, verdict, f"expected {expect}, got {got}{where}" + (f" ({detail})" if detail else ""))


def rows(sql, *params):
    return conn.execute(sql, params).fetchall()


def counts(slug):
    return {t: rows(f"SELECT count(*) FROM {t} WHERE app_slug = %s", slug)[0][0] for t in CHILDREN}


def alter_composite_keys(mode):
    """Needs the table owner: the identity joins that role for the length of the ALTER only."""
    names = [r[0] for r in rows("SELECT conname FROM pg_constraint WHERE conrelid = 'app_config'::regclass AND contype = 'f' AND confrelid <> 'apps'::regclass")]
    conn.execute("GRANT appmux_spike9_owner TO CURRENT_USER")
    try:
        with conn.transaction():
            conn.execute("SET LOCAL ROLE appmux_spike9_owner")
            for name in names:
                conn.execute(f"ALTER TABLE app_config ALTER CONSTRAINT {name} {mode}")
    finally:
        conn.execute("REVOKE appmux_spike9_owner FROM CURRENT_USER")
    record(f"composite keys of app_config set to {mode}", "INFO", len(names))


def seed(slug):
    """One app, as the design writes it: the platform role inserts apps and the first app_config row, the SP the rest."""
    with conn.transaction():
        conn.execute(f"SET LOCAL ROLE {WRITERS}")
        conn.execute(
            "INSERT INTO apps (slug, display_name, owner, source_path, memory_budget_mb, created_by, updated_by) VALUES (%s, %s, 'x@example.com', '/Workspace/x', 256, 'seed', 'seed')",
            (slug, slug.upper()),
        )
        conn.execute("INSERT INTO app_config (app_slug, updated_by) VALUES (%s, 'seed')", (slug,))
    with conn.transaction():
        conn.execute(f"SET LOCAL ROLE {SP}")
        conn.execute("INSERT INTO releases (app_slug, release_no, snapshot_digest, detected, created_by) VALUES (%s, 1, %s, '{}', 'seed')", (slug, "sha256:" + "0" * 64))
        conn.execute("INSERT INTO app_resources (app_slug, key, type, target, permission, access_as, check_result, attached_by) VALUES (%s, 'cid', 'secret', 's/cid', 'READ', NULL, 'ok', 'seed'), (%s, 'csec', 'secret', 's/csec', 'READ', NULL, 'ok', 'seed')", (slug, slug))
        conn.execute("INSERT INTO app_grants (app_slug, principal_type, principal, permission, granted_by) VALUES (%s, 'everyone', '*', 'CAN_USE', 'seed')", (slug,))
        conn.execute("INSERT INTO permission_requests (app_slug, kind, reason, requested_by) VALUES (%s, 'settings', 'seed', 'seed')", (slug,))
        conn.execute("UPDATE app_config SET target_release_no = 1, own_sp_client_id_key = 'cid', own_sp_client_secret_key = 'csec', desired_state = 'running' WHERE app_slug = %s", (slug,))
    with conn.transaction():
        conn.execute(f"SET LOCAL ROLE {WRITERS}")
        conn.execute("INSERT INTO audit_log (actor, action, app_slug) VALUES ('seed', 'seed', %s)", (slug,))
    with conn.transaction():
        conn.execute(f"SET LOCAL ROLE {SP}")
        conn.execute("INSERT INTO metrics (app_slug, minute, lat_hist) VALUES (%s, now(), ARRAY[0,0,0,0,0,0,0,0,0,0,0])", (slug,))


def main(evidence_path):
    # 0. preconditions: the identity holds the writer role and nothing that would bypass table grants
    memberships = rows("SELECT r.rolname, m.admin_option, m.inherit_option, m.set_option FROM pg_auth_members m JOIN pg_roles r ON r.oid = m.roleid WHERE m.member = (SELECT oid FROM pg_roles WHERE rolname = current_user) ORDER BY 1")
    record("identity memberships (role, admin, inherit, set)", "INFO", memberships)
    for role in ("appmux_spike9_owner", SP, "databricks_superuser", WRITERS):
        record(f"identity inherits {role}", "INFO", rows("SELECT pg_has_role(current_user, %s, 'USAGE')", role)[0][0])
    record("identity attributes (super, createrole, createdb, bypassrls)", "INFO", rows("SELECT rolsuper, rolcreaterole, rolcreatedb, rolbypassrls FROM pg_roles WHERE rolname = current_user")[0])

    # 1. the grants file implements the table of section 6, for the writer role and the SP stand-in
    for role, table_privs in EXPECTED.items():
        wrong = []
        for table in TABLES:
            for letter, priv in PRIV.items():
                has = rows("SELECT has_table_privilege(%s, %s, %s)", role, f"public.{table}", priv)[0][0]
                if has != (letter in table_privs.get(table, "")):
                    wrong.append(f"{table}.{priv}={has}")
        record(f"{role}: INSERT/UPDATE/DELETE grants equal section 6", "PASS" if not wrong else "FAIL", ", ".join(wrong))
    owner_priv = rows("SELECT has_table_privilege(current_user, 'public.app_config', 'UPDATE'), has_table_privilege(current_user, 'public.releases', 'DELETE')")[0]
    record("identity (writer only) holds no UPDATE on app_config and no DELETE on releases", "PASS" if owner_priv == (False, False) else "FAIL", owner_priv)

    # 2. SET LOCAL ROLE: allowed for the group role, refused for roles the identity is not a member of
    attempt("SET LOCAL ROLE to the group role works", [f"SELECT 1 / (CASE WHEN current_user = '{WRITERS}' THEN 1 ELSE 0 END)"], "ok", as_role=WRITERS)
    check("current_user is back to the session user after that transaction", rows("SELECT current_user = session_user")[0][0])
    attempt("SET LOCAL ROLE to appmux_sp is refused (not a member)", ["SELECT 1"], "42501", as_role=SP)
    attempt("SET LOCAL ROLE to the table owner is refused (not a member)", ["SELECT 1"], "42501", as_role="appmux_spike9_owner")

    # 3. seed four apps (the SP stand-in writes the child tables), then drop the SP membership again
    with_sp_membership(True)
    for slug in ("sales", "hr", "ops", "tmp"):
        seed(slug)
    with_sp_membership(False)
    check("identity no longer inherits appmux_sp", rows("SELECT NOT pg_has_role(current_user, %s, 'USAGE')", SP)[0][0])
    record("seeded apps with one row in every child table", "INFO", {s: counts(s) for s in ("sales", "hr")})

    # 4. direct writes: refused where section 6 gives the writer nothing, allowed where it does
    refused = "42501"
    attempt("writer UPDATE app_config is refused", ["UPDATE app_config SET desired_state = 'stopped' WHERE app_slug = 'sales'"], refused)
    attempt("writer DELETE FROM app_config is refused", ["DELETE FROM app_config WHERE app_slug = 'sales'"], refused)
    attempt("writer INSERT INTO releases is refused", ["INSERT INTO releases (app_slug, release_no, detected, snapshot_digest, created_by) VALUES ('sales', 2, '{}', 'sha256:' || repeat('1', 64), 'x')"], refused)
    attempt("writer UPDATE app_grants is refused", ["UPDATE app_grants SET permission = 'CAN_MANAGE' WHERE app_slug = 'sales'"], refused)
    attempt("writer DELETE FROM permission_requests is refused", ["DELETE FROM permission_requests WHERE app_slug = 'sales'"], refused)
    attempt("writer INSERT INTO metrics is refused", ["INSERT INTO metrics (app_slug, minute, lat_hist) VALUES ('sales', now(), ARRAY[0,0,0,0,0,0,0,0,0,0,0])"], refused)
    attempt("writer DELETE FROM audit_log is refused", ["DELETE FROM audit_log"], refused)
    attempt("writer UPDATE audit_log is refused", ["UPDATE audit_log SET reason = 'x'"], refused)
    attempt("writer INSERT INTO app_config is allowed (the first row of a registered app)", [
        "INSERT INTO apps (slug, display_name, owner, source_path, memory_budget_mb, created_by, updated_by) VALUES ('fresh', 'Fresh', 'x@example.com', '/Workspace/x', 128, 'w', 'w')",
        "INSERT INTO app_config (app_slug, updated_by) VALUES ('fresh', 'w')",
    ], "ok")
    attempt("writer UPDATE permission_requests (answer) is allowed", ["UPDATE permission_requests SET status = 'approved', closed_by = 'w', closed_at = now() WHERE app_slug = 'sales'"], "ok")

    # 5. the cascades, under the writer role by inheritance (no SET ROLE)
    before = counts("sales")
    attempt("rename sales -> sales2 in one transaction (ON UPDATE CASCADE, deferred composite keys)", ["UPDATE apps SET slug = 'sales2' WHERE slug = 'sales'"], "ok")
    after_old, after_new = counts("sales"), counts("sales2")
    check("every child row followed the rename", after_new == before and not any(after_old.values()), {"before": before, "old slug": after_old, "new slug": after_new})
    record("app_config composite keys after the rename", "INFO", rows("SELECT app_slug, target_release_no, own_sp_client_id_key, own_sp_client_secret_key FROM app_config WHERE app_slug = 'sales2'"))

    before = counts("hr")
    attempt("delete hr in one transaction (ON DELETE CASCADE into tables the writer cannot delete from)", ["DELETE FROM apps WHERE slug = 'hr'"], "ok")
    after = counts("hr")
    check("every child row of the deleted app is gone", before == SEEDED_ROWS and not any(after.values()), {"before": before, "after": after})
    record("audit_log and metrics keep the deleted app's rows (no foreign key)", "INFO", {t: rows(f"SELECT count(*) FROM {t} WHERE app_slug = 'hr'")[0][0] for t in ("audit_log", "metrics")})

    attempt("delete tmp and rename ops -> ops2 in the same transaction", ["DELETE FROM apps WHERE slug = 'tmp'", "UPDATE apps SET slug = 'ops2' WHERE slug = 'ops'"], "ok")
    check("tmp gone, ops2 complete, ops empty", not any(counts("tmp").values()) and counts("ops2") == SEEDED_ROWS and not any(counts("ops").values()), {"tmp": counts("tmp"), "ops": counts("ops"), "ops2": counts("ops2")})

    # 6. controls: a rename onto a taken slug fails as a whole, and the deferred composite keys still bite
    attempt("control: rename onto a taken slug fails (23505)", ["UPDATE apps SET slug = 'sales2' WHERE slug = 'ops2'"], "23505")
    check("ops2 untouched after the failed rename", counts("ops2") == SEEDED_ROWS, counts("ops2"))
    with_sp_membership(True)
    attempt("control: a dangling target_release_no (SP role) passes the statement and fails at COMMIT (23503)", ["UPDATE app_config SET target_release_no = 99 WHERE app_slug = 'ops2'"], "23503", as_role=SP, at_commit=True)
    attempt("control: a dangling own_sp key (SP role) passes the statement and fails at COMMIT (23503)", ["UPDATE app_config SET own_sp_client_id_key = 'nope' WHERE app_slug = 'ops2'"], "23503", as_role=SP, at_commit=True)
    with_sp_membership(False)

    # 7. variant, observation only: is the DEFERRABLE needed? Make the three composite keys immediate and rename again.
    alter_composite_keys("NOT DEFERRABLE")
    with_sp_membership(True)
    seed("imm")
    with_sp_membership(False)
    got, detail, _ = run_transaction(["UPDATE apps SET slug = 'imm2' WHERE slug = 'imm'"])
    record("variant: rename with the composite keys NOT DEFERRABLE", "INFO", f"{got} {detail}")
    alter_composite_keys("DEFERRABLE INITIALLY DEFERRED")

    summary = {"pass": sum(r["outcome"] == "PASS" for r in results), "fail": sum(r["outcome"] == "FAIL" for r in results)}
    print(f"\n{summary}")
    json.dump({"summary": summary, "results": results}, open(evidence_path, "w"), indent=1)


main(sys.argv[1])

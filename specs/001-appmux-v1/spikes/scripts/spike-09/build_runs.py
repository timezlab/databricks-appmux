"""Spike 9: assemble the statement files that owner_sql.py runs, from the DDL and grants extracted out of the data model.

usage: build_runs.py <extract-dir> <out-dir>
  reset.sql   (database `databricks_postgres`)  recreate the scratch database owned by the table-owner role
  ddl.sql     (scratch database)                the data model's DDL, run as the table-owner role
  grants.sql  (scratch database)                the data model's grants, then the test identity joins the group role only
The project owner's role stands in for a platform admin: it is made a member of the group role and loses the table-owner role.
"""
import sys
from pathlib import Path

extract, out = Path(sys.argv[1]), Path(sys.argv[2])
out.mkdir(parents=True, exist_ok=True)
SEP = "\n;;\n"


def statements(name):
    return (extract / name).read_text().strip().split(SEP)


reset = [
    "GRANT appmux_spike9_owner TO CURRENT_USER",
    "DROP DATABASE IF EXISTS appmux_spike9",
    "CREATE DATABASE appmux_spike9 OWNER appmux_spike9_owner",
    "REVOKE appmux_spike9_owner FROM CURRENT_USER",
]
(out / "reset.sql").write_text(SEP.join(reset) + "\n")

load_ddl = ["GRANT appmux_spike9_owner TO CURRENT_USER", "SET ROLE appmux_spike9_owner", *statements("ddl.split.sql"), "RESET ROLE"]
load_ddl.append("SELECT count(*) AS tables_created FROM pg_tables WHERE schemaname = 'public'")
(out / "ddl.sql").write_text(SEP.join(load_ddl) + "\n")

grants = statements("grants.split.sql")
create_role, rest = grants[0], grants[1:]  # CREATE ROLE runs as the project owner; the grants run as the table owner
tail = [
    "RESET ROLE",
    "REVOKE appmux_spike9_owner FROM CURRENT_USER",
    "GRANT appmux_platform_writers TO CURRENT_USER",
    "SELECT r.rolname AS member_of, m.admin_option, m.inherit_option, m.set_option FROM pg_auth_members m JOIN pg_roles r ON r.oid = m.roleid JOIN pg_roles u ON u.oid = m.member WHERE u.rolname = current_user ORDER BY 1",
    "SELECT c.relname, c.relacl::text FROM pg_class c WHERE c.relnamespace = 'public'::regnamespace AND c.relkind = 'r' ORDER BY 1",
]
(out / "grants.sql").write_text(SEP.join([create_role, "GRANT appmux_spike9_owner TO CURRENT_USER", "SET ROLE appmux_spike9_owner", *rest, *tail]) + "\n")
print("reset.sql, ddl.sql, grants.sql written")

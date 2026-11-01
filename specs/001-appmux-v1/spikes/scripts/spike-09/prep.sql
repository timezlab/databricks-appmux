SELECT r.rolname AS member_of, m.admin_option, m.inherit_option, m.set_option FROM pg_auth_members m JOIN pg_roles r ON r.oid = m.roleid JOIN pg_roles u ON u.oid = m.member WHERE u.rolname = current_user ORDER BY 1
;;
SELECT rolname, rolsuper, rolcreaterole, rolcreatedb, rolbypassrls, rolcanlogin FROM pg_roles WHERE rolname !~ '^pg_' AND rolname NOT LIKE 'databricks%' ORDER BY rolname
;;
CREATE ROLE appmux_spike9_owner NOLOGIN
;;
CREATE ROLE appmux_sp NOLOGIN
;;
GRANT appmux_spike9_owner TO CURRENT_USER
;;
CREATE DATABASE appmux_spike9 OWNER appmux_spike9_owner
;;
SELECT datname, pg_get_userbyid(datdba) = 'appmux_spike9_owner' AS owned_by_table_owner_role FROM pg_database WHERE datname = 'appmux_spike9'

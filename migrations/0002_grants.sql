-- appmux v1 grants: who writes which table (docs/product/v1-data-model.md, section 6).
--
-- The two roles are created by the operator before the loader runs (design 13.1) and this file
-- creates no role:
--   appmux_platform_writers  a NOLOGIN group role. Each platform admin's own OAuth role is a member,
--                            and the login is that person's OBO token.
--   appmux_sp                a group role that holds the appmux service principal's OAuth role as a
--                            member, so the grants name one fixed role whatever the service
--                            principal's client id is.
-- The grants are table-level and need no column-level grant. They bind these two roles only.
-- A member of databricks_superuser (the Lakebase project owner by default) holds every privilege
-- on a new table and is outside the split.
--
-- audit_log is append-only because no role below is granted UPDATE or DELETE on it.
-- Dropping a row of apps and renaming its slug cascade into the child tables under the privileges
-- of the table owner, so appmux_platform_writers needs no write privilege on those tables.

-- Platform admin's own role: what decides what runs in the slot.
GRANT INSERT, UPDATE, DELETE ON apps, platform_settings TO appmux_platform_writers;
GRANT UPDATE ON permission_requests TO appmux_platform_writers;
GRANT INSERT ON app_config, audit_log TO appmux_platform_writers;

-- appmux service principal: everything an app admin does, and the results.
GRANT INSERT, UPDATE ON releases, permission_requests TO appmux_sp;
GRANT INSERT, UPDATE, DELETE ON app_grants, app_resources TO appmux_sp;
GRANT INSERT, DELETE ON metrics TO appmux_sp;
GRANT UPDATE ON app_config TO appmux_sp;
GRANT INSERT ON audit_log TO appmux_sp;

-- Both read everything.
GRANT SELECT ON ALL TABLES IN SCHEMA public TO appmux_sp, appmux_platform_writers;

-- Removes nothing today, since PUBLIC holds no such privilege. It stays so that the append-only
-- rule is visible in the file that grants.
REVOKE UPDATE, DELETE ON audit_log FROM PUBLIC;

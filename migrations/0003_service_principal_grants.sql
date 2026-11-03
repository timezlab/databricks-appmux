-- appmux v1: a grant may name an App's service principal (ADR 0020, data model 4.5).
--
-- The Apps proxy forwards an App's service principal with its application id (a UUID) where a
-- person's email goes, so a grant of type 'service_principal' names that id in lower case. The
-- baseline's column check on principal_type is replaced by one that admits the new type, and a
-- second check holds the name to the lower-case UUID form that the row model repeats
-- (appmux.registry.models.APPLICATION_ID_PATTERN). CAN_MANAGE is not refused here: like
-- 'everyone', the policy reads a stored CAN_MANAGE on a service principal as CAN_USE.
--
-- The loader runs this file with the others only on an empty database. A registry loaded before
-- this file existed gets it applied by hand (docs/tech-debt.md, TD-008), for example with psql in
-- autocommit, so the file is one ALTER TABLE: PostgreSQL applies all of its actions or none, and
-- the IF EXISTS drops also take out what an earlier run added, so a second run gives the same
-- table. Like the baseline it holds no BEGIN or COMMIT: the loader owns the transaction, and a
-- COMMIT here would end it early. No grant changes: the grants of 0002_grants.sql are table-level.

ALTER TABLE app_grants
  DROP CONSTRAINT IF EXISTS app_grants_principal_type_check,
  DROP CONSTRAINT IF EXISTS app_grants_service_principal_check,
  ADD CONSTRAINT app_grants_principal_type_check
    CHECK (principal_type IN ('user', 'group', 'everyone', 'service_principal')),
  ADD CONSTRAINT app_grants_service_principal_check
    CHECK (principal_type <> 'service_principal' OR principal ~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$');

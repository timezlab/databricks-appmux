CREATE ROLE appmux_platform_writers NOLOGIN
;;
CREATE ROLE appmux_spike1_owner NOLOGIN
;;
GRANT appmux_spike1_owner TO CURRENT_USER
;;
CREATE TABLE public.spike1_apps (slug text PRIMARY KEY, note text)
;;
CREATE TABLE public.spike1_other (id int)
;;
GRANT CREATE ON SCHEMA public TO appmux_spike1_owner
;;
ALTER TABLE public.spike1_apps OWNER TO appmux_spike1_owner
;;
ALTER TABLE public.spike1_other OWNER TO appmux_spike1_owner
;;
GRANT INSERT ON public.spike1_apps TO appmux_platform_writers
;;
SELECT current_user, (SELECT rolsuper FROM pg_roles WHERE rolname = current_user) AS rolsuper, pg_has_role(current_user, 'appmux_platform_writers', 'MEMBER') AS is_writer, pg_has_role(current_user, 'pg_database_owner', 'MEMBER') AS is_db_owner
;;
SELECT rolname, rolsuper, rolcreaterole, rolcreatedb, rolbypassrls, rolcanlogin FROM pg_roles WHERE rolname !~ '^pg_' ORDER BY rolname

GRANT CREATE ON SCHEMA public TO appmux_spike1_owner
;;
ALTER TABLE public.spike1_apps OWNER TO appmux_spike1_owner
;;
ALTER TABLE public.spike1_other OWNER TO appmux_spike1_owner
;;
SELECT tablename, tableowner FROM pg_tables WHERE tablename LIKE 'spike1_%' ORDER BY 1

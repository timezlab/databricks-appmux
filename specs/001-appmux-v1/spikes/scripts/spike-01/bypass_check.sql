INSERT INTO public.spike1_apps (slug) VALUES ('owner-direct')
;;
SELECT has_table_privilege(current_user, 'public.spike1_apps', 'INSERT') AS can_insert, has_table_privilege(current_user, 'public.spike1_other', 'INSERT') AS can_insert_other, has_table_privilege(current_user, 'public.spike1_other', 'SELECT') AS can_select_other
;;
SELECT r.rolname AS member_of FROM pg_auth_members m JOIN pg_roles r ON r.oid = m.roleid JOIN pg_roles u ON u.oid = m.member WHERE u.rolname = current_user ORDER BY 1

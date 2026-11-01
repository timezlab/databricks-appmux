REVOKE appmux_platform_writers FROM CURRENT_USER
;;
SELECT r.rolname AS member_of, m.admin_option, m.inherit_option, m.set_option FROM pg_auth_members m JOIN pg_roles r ON r.oid = m.roleid JOIN pg_roles u ON u.oid = m.member WHERE u.rolname = current_user ORDER BY 1

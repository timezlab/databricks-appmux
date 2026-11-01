# shellcheck shell=bash
# Sourced (never run) by scripts/sync-deploy.sh and scripts/fetch-logs.sh. It defines
# mask_identifiers, the one place that knows which text a log or CLI line must not show.
# scripts/assemble-deploy.sh deploys an allow-list that does not include scripts/, so this file
# never reaches the workspace or the release zip.
#
#   mask_identifiers   reads stdin, writes stdout, replacing:
#     * the folder in $APPMUX_WORKSPACE_FOLDER, when that variable is set  -> <workspace-folder>
#     * URLs (they carry host names, workspace ids and org ids)             -> <url>
#     * Databricks host names: *.cloud.databricks.com, *.gcp.databricks.com, other
#       *.databricks.com, *.azuredatabricks.net and *.databricksapps.com   -> <host>
#     * workspace ids: adb-<digits>                                         -> <workspace-id>
#     * numeric org / workspace ids: o=<digits>, org id, X-Databricks-Org-Id,
#       workspace_id, workspaceId                                           -> <org-id>
#     * e-mail addresses                                                    -> <email>
#     * /Workspace/... paths                                                -> <workspace-path>
#     * tokens                                                              -> <token>: dapi and
#       dose Databricks tokens, JWTs (eyJ...), Authorization header values, Bearer and Basic
#       values, and the value of any key whose name ends in token, secret, password or api_key
#       (access_token, client_secret, token=...).
#
# A false positive costs a masked word in a log; a false negative prints a credential or
# names a workspace, so a key called "token" masks whatever value follows it. Read the raw log
# in the workspace when a masked value is the thing you need.
#
# sed has no portable case-insensitive flag (BSD sed lacks `I`), so the rules spell it out with
# [Aa]-style classes. LC_ALL=C makes the classes mean bytes, so a binary log line cannot make
# sed fail with "invalid multibyte sequence".

# The substitutions, one per line. Order matters: workspace paths go before e-mails, so a
# /Workspace/Users/<email>/... path goes whole, and host names before adb-<digits>, so an
# adb-<digits>.<n>.azuredatabricks.net host becomes one <host>.
_mask_identifiers_rules() {
  cat << 'SED_RULES'
s#https?://[^[:space:]"'<>]+#<url>#g
s#[A-Za-z0-9.-]+\.(azuredatabricks\.net|databricksapps\.com|databricks\.com)#<host>#g
s#adb-[0-9]+#<workspace-id>#g
s#(^|[^A-Za-z0-9_])[oO]=[0-9]+#\1o=<org-id>#g
s#([Oo][Rr][Gg][-_ ]?[Ii][Dd]|[Ww][Oo][Rr][Kk][Ss][Pp][Aa][Cc][Ee][-_ ]?[Ii][Dd])(["']?[[:space:]]*[:=][[:space:]]*["']?|[[:space:]]+)[0-9]+#\1\2<org-id>#g
s#/Workspace/[^[:space:]"'<>]*#<workspace-path>#g
s#[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+#<email>#g
s#([Aa][Uu][Tt][Hh][Oo][Rr][Ii][Zz][Aa][Tt][Ii][Oo][Nn]["']?[[:space:]]*[:=][[:space:]]*["']?)[A-Za-z]+[[:space:]]+[^[:space:]"',;]+#\1<token>#g
s#([Bb][Ee][Aa][Rr][Ee][Rr])[[:space:]]+[A-Za-z0-9._~+/=-]+#\1 <token>#g
s#([Bb][Aa][Ss][Ii][Cc])[[:space:]]+[A-Za-z0-9._~+/=-]{8,}#\1 <token>#g
s#eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]*#<token>#g
s#(dapi|dose)[A-Za-z0-9]{16,}#<token>#g
s#([A-Za-z0-9_-]*([Tt][Oo][Kk][Ee][Nn]|[Ss][Ee][Cc][Rr][Ee][Tt]|[Pp][Aa][Ss][Ss][Ww][Oo][Rr][Dd]|[Aa][Pp][Ii][-_]?[Kk][Ee][Yy])["']?[[:space:]]*[:=][[:space:]]*["']?)[^[:space:]"',;&})]+#\1<token>#g
SED_RULES
}

mask_identifiers() {
  local rules folder_rule=()
  rules=$(_mask_identifiers_rules)
  if [[ -n "${APPMUX_WORKSPACE_FOLDER:-}" ]]; then
    # The folder is data, not a pattern: escape every regex character and the # delimiter.
    local escaped_folder
    escaped_folder=$(printf '%s' "$APPMUX_WORKSPACE_FOLDER" | sed -E 's/[][\.*^$+?(){}|#]/\\&/g')
    folder_rule=(-e "s#${escaped_folder}#<workspace-folder>#g")
  fi
  # The folder goes first: it is the longest, most specific match.
  LC_ALL=C sed -E ${folder_rule[@]+"${folder_rule[@]}"} -e "$rules"
}

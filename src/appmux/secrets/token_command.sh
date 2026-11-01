#!/bin/bash
# The token command of a child that may use the shared service principal (design 8.5).
#
# The Databricks SDK runs this when it needs a token (DATABRICKS_AUTH_TYPE=databricks-cli, with
# DATABRICKS_CLI_PATH pointing here) and reads one JSON object from standard output. appmux writes
# that object to the file below, atomically, and renews it before the token it holds expires, so
# this script only prints the file. It takes no input and ignores the arguments the SDK passes
# (`auth token --host ...`): the file is the one place the answer comes from.
#
# secrets/shared_sp_token.py installs this file into the child's run directory and keeps the path
# below equal to its TOKEN_FILE_PATH; a test holds the two together.
set -euo pipefail

readonly token_file="/run/appmux/shared-sp-token.json"

# `cat` fails when the file is missing, and `set -e` passes that failure on, so the SDK sees a
# non-zero exit and nothing on standard output instead of an empty token.
exec cat -- "${token_file}"

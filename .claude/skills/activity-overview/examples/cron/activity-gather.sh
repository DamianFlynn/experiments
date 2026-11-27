#!/bin/sh
# Nightly gather for the activity-overview streams on Linux (cron), or on
# macOS without the keychain. docs/SCHEDULING.md explains it.
#
# The token comes from an env file only this user can read. Create it without
# the token ever appearing on a command line (so not in shell history either):
# in bash (type `bash` first if your shell is another one), paste the token at
# the silent prompt of `read -rs`; printf is a bash builtin, so the value is not
# in a process listing either:
#   mkdir -p ~/.config/activity-overview && chmod 700 ~/.config/activity-overview
#   ( umask 077; printf 'token: ' >&2; IFS= read -rs t; echo >&2;
#     printf 'GITHUB_TOKEN=%s\n' "$t" > ~/.config/activity-overview/token.env )
# (or open the file in an editor and type the line GITHUB_TOKEN=... there, then
# `chmod 600` it). Keep it mode 600, outside any synced folder, the workspace
# and any git checkout. The variable is exported for this process and its
# children only.
set -eu

SKILL_DIR="__SKILL_DIR__"                        # the skill directory (USAGE 2: $AO)
STREAMS="__WORKSPACE__/streams.json"             # your streams file
TOKEN_FILE="${HOME}/.config/activity-overview/token.env"

PATH="/usr/local/bin:/usr/bin:/bin"
export PATH

if [ "$(stat -c %a "$TOKEN_FILE" 2>/dev/null || stat -f %Lp "$TOKEN_FILE")" != "600" ]; then
  echo "activity-gather: $TOKEN_FILE must be mode 600" >&2
  exit 2
fi
set -a
. "$TOKEN_FILE"
set +a

exec nice -n 10 python3 "$SKILL_DIR/schedule.py" run --streams "$STREAMS" "$@"

#!/bin/sh
# Nightly gather for the activity-overview streams on Linux (cron), or on
# macOS without the keychain. docs/SCHEDULING.md explains it.
#
# The token comes from an env file only this user can read:
#   install -m 600 /dev/null ~/.config/activity-overview/token.env
#   printf 'GITHUB_TOKEN=%s\n' '<paste>' > ~/.config/activity-overview/token.env
# (keep the file mode 600, outside any synced folder, the workspace and any git
# checkout). The variable is exported for this process and its children only.
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

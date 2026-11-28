#!/bin/sh
# Nightly gather for the activity-overview streams: the program the LaunchAgent
# (local.activity-overview.gather.plist) runs. docs/SCHEDULING.md explains it.
#
# Copy it next to your streams file (e.g. ~/activity-data/bin/), edit the two
# paths below, `chmod 700` it. It reads the token for THIS process only, from
# the login keychain; nothing is written to disk.
#
#   one-time, in Terminal. `-w` given LAST with no value makes `security`
#   prompt for the password (security(1)), so the token is typed at a prompt and
#   never appears on the command line or in shell history; never write it after
#   -w. -U updates an existing item; -T lets the `security` tool read it later
#   without a dialog:
#   security add-generic-password -a "$USER" -s activity-gh-token -T /usr/bin/security -U -w
set -eu

SKILL_DIR="__SKILL_DIR__"                        # the skill directory (USAGE 2: $AO)
STREAMS="__WORKSPACE__/streams.json"             # your streams file
KEYCHAIN_SERVICE="activity-gh-token"

# launchd starts jobs with a minimal PATH: add Homebrew (Apple silicon, Intel)
# so python3 and git resolve the same way they do in Terminal.
PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
export PATH

PY="$(command -v python3 || true)"
if [ -z "$PY" ]; then
  echo "activity-gather: python3 not found (install the Xcode command line tools or Homebrew python)" >&2
  exit 2
fi
if ! "$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)'; then
  echo "activity-gather: $("$PY" --version 2>&1) is too old; the skill needs Python 3.11+" >&2
  exit 2
fi
command -v git >/dev/null 2>&1 || { echo "activity-gather: git not found" >&2; exit 2; }

GITHUB_TOKEN="$(security find-generic-password -a "$USER" -s "$KEYCHAIN_SERVICE" -w 2>/dev/null || true)"
if [ -z "$GITHUB_TOKEN" ]; then
  echo "activity-gather: no token in the keychain item '$KEYCHAIN_SERVICE' (is the login keychain unlocked?)" >&2
  exit 2
fi
export GITHUB_TOKEN

# Low priority; any extra arguments (e.g. --stream ID, --dry-run) pass through.
exec nice -n 10 "$PY" "$SKILL_DIR/schedule.py" run --streams "$STREAMS" "$@"

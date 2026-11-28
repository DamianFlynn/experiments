#!/bin/sh
# Nightly gather for the activity-overview streams on Linux (cron), or on
# macOS without the keychain. docs/SCHEDULING.md explains it.
#
# The token comes from an env file only this user can read. Store (and every
# week replace) it with the script, which opens the new-token page, reads the
# token at a hidden prompt, checks it, then writes the file atomically, mode 600
# (docs/SCHEDULING.md 7.1):
#   python3 __SKILL_DIR__/schedule.py token renew --streams __WORKSPACE__/streams.json --store env-file
# Or by hand, without the token ever appearing on a command line (so not in
# shell history either): in bash (type `bash` first if your shell is another
# one), paste the token at the silent prompt of `read -rs`; printf is a bash
# builtin, so the value is not in a process listing either:
#   mkdir -p ~/.config/activity-overview && chmod 700 ~/.config/activity-overview
#   ( umask 077; printf 'token: ' >&2; IFS= read -rs t; echo >&2;
#     printf 'GITHUB_TOKEN=%s\n' "$t" > ~/.config/activity-overview/token.env )
# (or open the file in an editor and type the line GITHUB_TOKEN=... there, then
# `chmod 600` it). Keep it mode 600, outside any synced folder, the workspace
# and any git checkout. The variable is exported for this process and its
# children only.
#
# Reminder: after each run, `schedule.py token remind` prints one line when the
# token expires within 2 days or was refused; it lands in the cron log (and in
# cron's mail, where mail is set up).
set -eu

SKILL_DIR="__SKILL_DIR__"                        # the skill directory (USAGE 2: $AO)
STREAMS="__WORKSPACE__/streams.json"             # your streams file
TOKEN_FILE="${HOME}/.config/activity-overview/token.env"

PATH="/usr/local/bin:/usr/bin:/bin"
export PATH

# The app route (token.store "app") mints its own token for every gather from
# the app's private key: no env file is read.
KIND="$(python3 "$SKILL_DIR/schedule.py" token kind --streams "$STREAMS" 2>/dev/null || echo env-file)"
if [ "$KIND" != "app" ]; then
  if [ "$(stat -c %a "$TOKEN_FILE" 2>/dev/null || stat -f %Lp "$TOKEN_FILE")" != "600" ]; then
    echo "activity-gather: $TOKEN_FILE must be mode 600" >&2
    exit 2
  fi
  set -a
  . "$TOKEN_FILE"
  set +a
fi

rc=0
nice -n 10 python3 "$SKILL_DIR/schedule.py" run --streams "$STREAMS" "$@" || rc=$?
unset GITHUB_TOKEN GH_TOKEN
MSG="$(python3 "$SKILL_DIR/schedule.py" token remind --streams "$STREAMS" 2>/dev/null || true)"
if [ -n "$MSG" ]; then
  echo "activity-gather: REMINDER: $MSG" >&2
fi
exit "$rc"

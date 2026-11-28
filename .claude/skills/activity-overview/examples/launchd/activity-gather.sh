#!/bin/sh
# Nightly gather for the activity-overview streams: the program the LaunchAgent
# (local.activity-overview.gather.plist) runs. docs/SCHEDULING.md explains it.
#
# Copy it next to your streams file (e.g. ~/activity-data/bin/), edit the two
# paths below, `chmod 700` it. The token follows the `token` block of the
# streams file (docs/SCHEDULING.md 7): with `"store": "app"` (the example's
# setting) each gather gets its own short-lived token and nothing below is
# needed; check it with
#     python3 __SKILL_DIR__/schedule.py token app-check --streams __WORKSPACE__/streams.json
#
# Personal-token fallback (docs/SCHEDULING.md 7.2): FIRST set
# `"store": "keychain"` in the streams file's `token` block, so this wrapper
# reads the login keychain for THIS process only (nothing is written to disk).
#   Then store (and every week replace) the token with the script, which opens
#   the new-token page, reads the token at a hidden prompt, checks it, then
#   stores it in the keychain item named there:
#     python3 __SKILL_DIR__/schedule.py token renew --streams __WORKSPACE__/streams.json
#   or by hand: `-w` given LAST with no value makes `security` prompt for the
#   password (security(1)); never write the token after -w:
#     security add-generic-password -a "$USER" -s activity-gh-token -T /usr/bin/security -U -w
#
# Reminder: after each run it asks `schedule.py token remind` whether the token
# expires within 2 days or was refused, and if so posts a macOS notification
# (osascript; the message is passed as an argument, never spliced into the
# script). Set NOTIFY=0 to turn that off.
set -eu

SKILL_DIR="__SKILL_DIR__"                        # the skill directory (USAGE 2: $AO)
STREAMS="__WORKSPACE__/streams.json"             # your streams file
NOTIFY="${NOTIFY:-1}"
# Where the token is (keychain item, env file) comes from the streams file's
# token block through `schedule.py token where`, the settings `token renew`
# writes to; nothing about it is fixed in this script.

notify() {
  [ "$NOTIFY" = "1" ] || return 0
  /usr/bin/osascript -e 'on run argv' \
    -e 'display notification (item 1 of argv) with title "Activity gather"' \
    -e 'end run' "$1" >/dev/null 2>&1 || true
}

# launchd starts jobs with a minimal PATH: put Homebrew (Apple silicon, Intel)
# and the system directories first so python3 and git resolve the same way
# they do in Terminal.
PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:${PATH:-}"
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

# The token, for this process only, from where the streams file says:
#   app       the runner mints its own token for every gather: nothing to read
#   keychain  the keychain item token.keychain_service (account: token where)
#   env-file  the mode-600 file token.env_file
where() { "$PY" "$SKILL_DIR/schedule.py" token where --streams "$STREAMS" --field "$1"; }
if ! KIND="$(where store)"; then
  echo "activity-gather: cannot read the token settings of $STREAMS" >&2
  notify "Activity gather: the streams file cannot be read"
  exit 2
fi
case "$KIND" in
  app) ;;
  keychain)
    SVC="$(where keychain-service)"; ACCT="$(where account)"
    GITHUB_TOKEN="$(security find-generic-password -a "$ACCT" -s "$SVC" -w 2>/dev/null || true)"
    if [ -z "$GITHUB_TOKEN" ]; then
      echo "activity-gather: no token in the keychain item '$SVC' (is the login keychain unlocked?)" >&2
      notify "No token in the keychain item $SVC: run schedule.py token renew"
      exit 2
    fi
    export GITHUB_TOKEN ;;
  env-file)
    # `where env-file` also checks the file's folder: it refuses (and names
    # the fix) when the folder is writable by group or others
    if ! TOKEN_FILE="$(where env-file)"; then
      echo "activity-gather: the token file's folder is not safe (see the message above)" >&2
      notify "The token file's folder is writable by others: see the job's log"
      exit 2
    fi
    # `where` prints an absolute path; a relative one is made explicit anyway,
    # because `.` searches PATH for a name without a slash
    case "$TOKEN_FILE" in /*) ;; *) TOKEN_FILE="./$TOKEN_FILE" ;; esac
    # GNU stat first: BSD `stat -f` means something else there and succeeds
    if [ "$(stat -c %a "$TOKEN_FILE" 2>/dev/null || stat -f %Lp "$TOKEN_FILE" 2>/dev/null)" != "600" ]; then
      echo "activity-gather: $TOKEN_FILE is missing or not mode 600" >&2
      notify "The token file is missing or not mode 600: run schedule.py token renew"
      exit 2
    fi
    set -a; . "$TOKEN_FILE"; set +a ;;
  *)
    echo "activity-gather: unknown token store '$KIND'" >&2
    exit 2 ;;
esac

# Low priority; any extra arguments (e.g. --stream ID, --dry-run) pass through.
rc=0
nice -n 10 "$PY" "$SKILL_DIR/schedule.py" run --streams "$STREAMS" "$@" || rc=$?
unset GITHUB_TOKEN GH_TOKEN
MSG="$("$PY" "$SKILL_DIR/schedule.py" token remind --streams "$STREAMS" 2>/dev/null || true)"
if [ -n "$MSG" ]; then
  echo "activity-gather: $MSG" >&2
  notify "$MSG"
fi
exit "$rc"

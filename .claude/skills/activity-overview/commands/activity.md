---
description: Generate a repository activity digest for a date window
---

Run the activity-overview skill for the repository and window the user names.

Steps:
1. Resolve OWNER, REPO, FROM, TO from the user's request (ask if missing).
2. Follow `.claude/skills/activity-overview/SKILL.md`: gather → link → render.
3. Return the filled report and the path to the bundle used.

Arguments: $ARGUMENTS

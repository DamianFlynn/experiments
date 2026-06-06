---
description: Generate a verifiable repository activity digest for a date window
---

Thin entrypoint for the **activity-overview** skill. All logic lives in
`.claude/skills/activity-overview/SKILL.md` (gather → link → render → report) —
this command only parses arguments and defers to it.

Usage:
`/activity <owner/repo | project> <fromDate> <toDate> [options]`

Options:
- `--transcript PATH` — a community-call transcript (`.vtt`/`.srt`/`.txt`/`.md`,
  local file, no network) to fold into the report's **Community call highlights**.
- `--series PATH` — an append-only `series.json` index so this installment is
  framed against the previous one ("Since last installment").
- `--manifest PATH` — a multi-repo project manifest (instead of `<owner/repo>`).
- Any other SKILL flag (e.g. `--no-project-board`, `--ref-date`) passes through.

Steps:
1. Parse OWNER/REPO (or the project/manifest), FROM, TO, and any options from the
   arguments below. Ask the user ONLY for a required value that is missing.
2. Follow `.claude/skills/activity-overview/SKILL.md` end to end: gather → link
   (with `--series` if given) → render → (transcript, if given) → write the report.
3. Return the filled report and the path to the store/bundle view it was built from.

Arguments: $ARGUMENTS

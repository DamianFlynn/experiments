---
description: Generate a verifiable repository activity digest for a date window
---

Thin entrypoint for the **activity-overview** skill. All logic lives in
`.claude/skills/activity-overview/SKILL.md` (gather → validate → view → link →
render → slices → report) — this command only parses arguments and defers to it.

Usage:
- `/activity <owner/repo> <fromDate> <toDate> [options]` — one repository.
- `/activity --manifest PATH [options]` — a multi-repo project; the project name and
  window come from the manifest (a bare project name cannot be gathered).

Options:
- `--store PATH` — the journey-graph store to write and read (default
  `workspace/journey.db`; use a new store per installment).
- `--transcript PATH` — a community-call transcript (`.vtt`/`.srt`/`.txt`/`.md`,
  local file, no network) to fold into the report's **Community call highlights**.
- `--series PATH` — an append-only `series.json` index so this installment is
  framed against the previous one ("Since last installment").
- Any other gather flag (e.g. `--no-project-board`, `--ref-date`, `--clone-dir`)
  passes through to `gather.py`.

Steps (run from the skill directory; STORE is `--store` or `workspace/journey.db`,
FROM/TO the window):
1. Parse OWNER/REPO (or the manifest), FROM, TO, and any options from the
   arguments below. Ask the user ONLY for a required value that is missing.
2. Follow `.claude/skills/activity-overview/SKILL.md` end to end, i.e.:
   - gather: `python3 gather.py --owner OWNER --repo REPO --from FROM --to TO --store STORE`
     (or `python3 gather.py --manifest PATH --store STORE`)
   - validate: `python3 validate.py STORE` — stop unless `RESULT: OK`
     (multi-repo: `python3 validate.py STORE --project <name>`)
   - view: `python3 view.py --store STORE --from FROM --to TO --out workspace/view/bundle.json`
     (multi-repo: add `--project <name> --repo <owner/repo>` per member, and give each member its own bundle, series and diagrams paths — `workspace/view/<owner>__<repo>.json`, `series/<owner>__<repo>.json`, `--diagrams-dir workspace/diagrams/<owner>__<repo>` — so one member never overwrites another's files or is compared with a different repo in the series; each member's `bundle.diagrams` paths resolve against the parent of ITS diagrams dir; use those member paths in the link, render and slice commands below)
   - link: `python3 link.py workspace/view/bundle.json --series SERIES` when `--series`
     was given, else `python3 link.py workspace/view/bundle.json`
   - render: `python3 render.py workspace/view/bundle.json --diagrams-dir workspace/diagrams`
   - slices: `python3 link.py workspace/view/bundle.json --slice <train-id>` per narrated train
   - multi-repo project view: `python3 digest.py --store STORE --project <name> --from FROM --to TO`
   - transcript, if given: `python3 transcript.py PATH`
   - write the report from `report-template.md`.
3. Return the filled report and the paths of the store and the bundle view (and
   project view, for a manifest) it was built from.

Arguments: $ARGUMENTS

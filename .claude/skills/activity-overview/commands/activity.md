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

Token: gather reads `GITHUB_TOKEN` (else `GH_TOKEN`) — a classic PAT with `public_repo` for
public repos, `repo` for private ones, plus `read:project` for Projects v2 boards. The skill
only reads from GitHub; it never writes (`docs/USAGE.md` §1.1).

Options:
- `--store PATH` — the journey-graph store to write and read. Default: one store per
  window, `workspace/<FROM>_<TO>/journey.db` (e.g. `workspace/2026-11-01_2026-11-30/journey.db`),
  so a second run for another window starts a new store instead of accreting into the
  previous one; pass `--store` to grow one store month by month instead (`docs/USAGE.md` §2.1).
- `--transcript PATH` — a community-call transcript (`.vtt`/`.srt`/`.txt`/`.md`,
  local file, no network) to fold into the report's **Community call highlights**.
- `--series PATH` — continuity across installments ("Since last installment"). For one
  repository, PATH is the `series.json` index file (one entry per window). With
  `--manifest`, PATH is a **directory** holding one series file per member: member
  `<owner>/<repo>` uses `PATH/<owner>__<repo>.json` — the slug with its `/` replaced by
  `__`, the same name `docs/USAGE.md` §7.3's per-member loop gives `$S/series/$key.json`.
  `link.py` creates a missing file; never point two members at one file (a series file
  belongs to one repo).
- Any other gather flag (e.g. `--no-project-board`, `--ref-date`, `--clone-dir`)
  passes through to `gather.py`. `--ref-date` is stored with the window and
  `view.py`/`digest.py` read it back into `meta.ref_date`.

Steps (run from the skill directory; FROM/TO the window; W is the per-window workspace
`workspace/<FROM>_<TO>`; STORE is `--store` or `W/journey.db`; BUNDLE is the bundle view
`W/view/bundle.json` (multi-repo: `W/view/<owner>__<repo>.json` per member); DIAGRAMS is
`W/diagrams` (multi-repo: `W/diagrams/<owner>__<repo>`). Every default path is per window,
so a second run for another window never overwrites the first run's view or diagrams):
1. Parse OWNER/REPO (or the manifest), FROM, TO, and any options from the
   arguments below. Ask the user ONLY for a required value that is missing.
2. Follow `.claude/skills/activity-overview/SKILL.md` end to end, i.e.:
   - gather: `python3 gather.py --owner OWNER --repo REPO --from FROM --to TO --store STORE`
     (or `python3 gather.py --manifest PATH --store STORE`)
   - validate: `python3 validate.py STORE` — stop unless `RESULT: OK`
     (multi-repo: `python3 validate.py STORE --project <name>`)
   - view: `python3 view.py --store STORE --from FROM --to TO --out BUNDLE`
     (multi-repo: add `--project <name> --repo <owner/repo>` per member, each with its own BUNDLE, series file and DIAGRAMS — so one member never overwrites another's files or is compared with a different repo in the series; each member's `bundle.diagrams` paths resolve against the parent of ITS diagrams dir; use those member paths in the link, render and slice commands below)
   - link: `python3 link.py BUNDLE --series SERIES` when `--series` was given (SERIES is the
     PATH for one repository, `PATH/<owner>__<repo>.json` for a manifest member), else
     `python3 link.py BUNDLE`
   - render: `python3 render.py BUNDLE --diagrams-dir DIAGRAMS`
   - slices: `python3 link.py BUNDLE --slice <train-id>` per narrated train
   - multi-repo project view: `python3 digest.py --store STORE --project <name> --from FROM --to TO > W/view/project.json`
   - transcript, if given: `python3 transcript.py PATH`
   - write the report from `report-template.md`.
3. Return the filled report and the paths of the store and the bundle view (and
   project view, for a manifest) it was built from.

Arguments: $ARGUMENTS

# Sample newsletter

[`avm-bicep-2026-10.md`](avm-bicep-2026-10.md) — "Azure Verified Modules for Bicep —
October 2026", a **sample** issue written to `reference/newsletter.md` from a real view. It
is not an official publication of the project; names, numbers and quotes are as the record
held them.

**Source.** A store of `Azure/bicep-registry-modules` for 2026-10-01 to 2026-10-31,
gathered on 27 November 2026 with full coverage: every commit of every pull request (checks
and commit evidence), fork markers, the people registry (affiliations with names and the
maintainer property), `module_notes` (changelogs, version files, tags), `module_ownership`
(recomputed as of the window's end), `plumbing_themes` and `stories.all`.

**How it was produced.**

```bash
# 1. the view
python3 view.py --store journey.db --from 2026-10-01 --to 2026-10-31 --out view/bundle.json
python3 link.py view/bundle.json --people people.json
# 2. the outline: every section's material, the story plan (full / line / in_theme)
python3 newsletter.py view/bundle.json --outline > view/newsletter.outline.json
# 3. the work package: a slice and outline row per full story, one theme file per
#    plumbing theme, writer batches with BATCH.md and STORY_BRIEF.md, manifest.json
python3 newsletter.py view/bundle.json --work-package wp
# 4. parallel writers, one per batch: each story checked with
#    python3 link.py wp/slices/<key>.slice.json --check-story <story without its markers>
# 5. assembly: the non-story sections written from the outline; every
#    <!-- story pending --> / <!-- theme pending --> marker replaced by the written story
#    (in its group, in stories.all order) or theme narrative (in theme order)
# 6. the whole issue checked, every story block again against its own slice
python3 newsletter.py view/bundle.json --check-newsletter avm-bicep-2026-10.md \
  --slices wp/slices --people people.json
```

The issue holds 37 stories in full (14 landed, 8 in flight, 12 stalled after activity in
October 2026, 3 closed), 20 stalled threads carried over in one line each, and seven plumbing
themes naming every plumbing train, with the one plumbing train that had a real discussion in
October (#7482) told in full under its theme; two automation-only trains (#7473, #7557) are
told in their themes. Edits made at assembly to the writers' text: a folder link on the first
unlinked mention of a module the view knows, vendor names of coding assistants replaced by
"an automation account (a coding agent)", one sentence about the work package (not the
record) removed, and italic cross-references between stories and themes that tell the same
pull request (outside the story blocks).

The store, the view (105 MB), the work package and the slices are not kept here. All numbers
come from that one October store; no month-over-month comparison is made. The "New modules"
section is omitted with its reason, because no module was new in October 2026.

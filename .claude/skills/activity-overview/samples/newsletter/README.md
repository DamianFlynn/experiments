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

**Update pass (store of 27 November 2026, second gathering).** The issue was re-checked
against a newer store of the same window. Every story and theme written for the earlier store
was checked again (the story check against the new slice, one state per fact against the new
view, the headline state, and the row: group, state, release status, PRs, review state) and
classified in the work package's `REVISIONS.md`: 30 kept as written, 5 to revise because the
row changed (child versions released with their parent, a review state, a close reason, a story
that moved theme), 3 to revise because they fail (two stories now in flight, two links the new
slice no longer holds), 1 new story (#6001), 2 no longer stories (automation-only trains, told
in their themes); six plumbing themes changed membership. The revisions go to writers as
`batches/rev-NN/` (the old text, the new slice and row, exactly what to change) and
`batches/rev-theme-<id>/`; until they come back, each sits in the issue as a
`<!-- story pending -->` or `<!-- theme pending -->` marker, so the check passes. The
non-story sections were rewritten from the new outline: what each module is on its first
mention, the release contents of each version, child versions, the deprecation of the
DevTest Lab `secret` child module from the tree's lifecycle event, module ownership from the
delivered fact (14 modules without an owner by kind, the owner added, the deprecated modules),
the one review state of each open pull request, the four proposed modules, and the
automation accounts with all their pull requests.

The revised stories and themes came back and were assembled: the issue holds 39 stories in
full (14 landed, 10 in flight, 11 stalled after activity in October 2026, 3 closed; within each
group new modules first, then changes, then breaking changes), 20 stalled threads carried over
in one line each, and seven plumbing themes, each passing `--check-theme`. The theme files now
carry every PR's full description; where the full text answered or changed a statement written
from the 800-character cut, the theme was corrected (the probe runs of #7572, the cause cited
by #7510, the limits of #7545, the reason given by #7144), and a sentence saying descriptions
were cut was removed. Edits made at assembly to the writers' text: a folder link on the first
unlinked mention of a module the view knows, vendor names of coding assistants replaced by "an
automation account (a coding agent)" (one file path among them), one sentence about the work
package (not the record) removed, italic cross-references between stories and themes that tell
the same pull request (outside the story blocks), and four wordings the theme check rejected (a
"#N (@login)" read as a name, a count the record does not hold, a quote with escaped quotes).

The store, the view (105 MB), the work package and the slices are not kept here. All numbers
come from that one October store; no month-over-month comparison is made. The "New modules"
section is omitted with its reason, because no module was new in October 2026.

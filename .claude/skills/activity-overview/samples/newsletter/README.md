# Sample newsletter

[`avm-bicep-2026-10.md`](avm-bicep-2026-10.md) — "Azure Verified Modules for Bicep —
October 2026", a **sample** issue written to `reference/newsletter.md` from a real view. It
is not an official publication of the project; names, numbers and quotes are as the record
held them.

**Source.** A store of `Azure/bicep-registry-modules` for 2026-10-01 to 2026-10-31,
gathered on 27 November 2026 with full coverage: every commit of every pull request (checks
and commit evidence), fork markers, the people registry (affiliations with names and the
maintainer property), `module_notes` (changelogs, version files, tags), `module_ownership`,
`plumbing_themes` and `stories.all`. The view was made and linked as the procedure says:

```bash
python3 view.py --store journey.db --from 2026-10-01 --to 2026-10-31 --out view/bundle.json
python3 link.py view/bundle.json --people people.json
python3 newsletter.py view/bundle.json --outline > view/newsletter.outline.json
```

**Part 1 of 2.** Every section that is not a story is written from that outline: the intro,
what changed in every module (what, why, for whom, released as what, with folder and tag
links), breaking changes and deprecations, the Stalled threads carried over (one line each),
module ownership, people (release status, fork contributions, automation), coming up and
stuck, and the numbers. The stories (39 in full: 15 landed, 9 in flight, 12 stalled with
activity in October 2026, 3 closed) and the seven plumbing themes (one discussed plumbing
train in full, 71 named in their themes) are marked `<!-- story pending: … -->` and
`<!-- theme pending: … -->`: they are written in parallel from a work package of slices and
outline rows, each story checked with `link.py --check-story`. The partial issue passes

```bash
python3 newsletter.py view/bundle.json --check-newsletter avm-bicep-2026-10.md \
  --slices slices --people people.json
```

The store, the view (105 MB) and the slices are not kept here. All numbers come from that one
October store; no month-over-month comparison is made. The "New modules" section is omitted
with its reason, because no module was new in October 2026.

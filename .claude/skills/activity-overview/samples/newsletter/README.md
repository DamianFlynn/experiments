# Sample newsletter

[`avm-bicep-2026-10.md`](avm-bicep-2026-10.md) — "Azure Verified Modules for Bicep —
October 2026", a **sample** issue written by hand to `reference/newsletter.md` from a real
view. It is not an official publication of the project; names, numbers and quotes are as the
record held them.

**Source.** A store of `Azure/bicep-registry-modules` for 2026-10-01 to 2026-10-31,
gathered on 27 November 2026 with the people registry (affiliations with names and the
maintainer property), `pr_activity` check results and review-thread flags, `pr_history`
with commit-diff evidence, and `module_notes` (changelogs, version files, tags). The trust
check printed `RESULT: OK`. The view was made and linked as the procedure says:

```bash
python3 view.py --store journey.db --from 2026-10-01 --to 2026-10-31 --out view/bundle.json
python3 link.py view/bundle.json --people people.json
python3 newsletter.py view/bundle.json --outline > view/newsletter.outline.json
python3 link.py view/bundle.json --slice train-issue-7503 > slices/train-issue-7503.slice.json
python3 link.py view/bundle.json --slice train-issue-7342 > slices/train-issue-7342.slice.json
```

**Restructured, not yet rewritten.** The issue follows the owner's layout (new, changed,
breaking; every story by state; module ownership; people with automation; coming up;
plumbing: what was engineered; numbers) with the data the October view has. Two stories are
written in full — `train-issue-7342` under Landed and `train-issue-7503` under In flight, each
passing `link.py --check-story` with its state in its first line; the other ten are marked
`<!-- story pending: … -->` until a store gathered with full commit coverage exists, and the
Module ownership section is omitted for the same reason. Module and tag links are derived
from the repository and tag names (the view does not yet record them). The host pool row in
"What changed" is the worked example of a change row that says what, why, for whom and
released as what. The whole issue passes

```bash
python3 newsletter.py view/bundle.json --check-newsletter avm-bicep-2026-10.md \
  --slices slices --people people.json
```

The store, the view (14 MB) and the slices are not kept here. All numbers come from that one
October store; no month-over-month comparison is made (the earlier months were gathered
without affiliations or check results). The "New modules" section is omitted with its
reason, because no module was new in October 2026.

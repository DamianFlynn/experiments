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

The two full stories are the outline's `stories.full` (the top story, which is in flight, and
the best shipped one, by the outline's mix rule); each passes `link.py --check-story`
against its slice, and the whole issue passes

```bash
python3 newsletter.py view/bundle.json --check-newsletter avm-bicep-2026-10.md \
  --slices slices --people people.json
```

The store, the view (14 MB) and the slices are not kept here. All numbers come from that one
October store; no month-over-month comparison is made (the earlier months were gathered
without affiliations or check results). The "New modules" section is omitted with its
reason, because no module was new in October 2026.

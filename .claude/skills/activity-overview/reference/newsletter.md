# The newsletter — a release-notes style issue

How to write the reader-facing newsletter of one stream (AVM Bicep, AVM Terraform, the AI
landing zone, an enterprise-scale landing zone) for one week or one month, from a linked view.
The digest of `report-template.md` is the maintainers' fact base; the newsletter is the blog
post written for the people who **use** the modules: what is new, what changed, what breaks,
what is deprecated, the stories behind the changes, who did the work, and what is coming.

**Universal rules** (as for the digest): the view is the only source of truth; every claim
links its item; a section with nothing behind it is omitted, never padded; the writer chooses
words, the code chooses what goes in and in which order.

## The two commands

```bash
# the ordered, pre-selected material per section (read-only; JSON on stdout)
python3 newsletter.py workspace/view/bundle.json --outline > workspace/view/newsletter.outline.json
#   weekly issue: add --weekly; a multi-repo stream: pass every member's linked view

# one slice per full story (as for any story), checked against its slice
python3 link.py workspace/view/bundle.json --slice <train-id> > workspace/slices/<train-id>.slice.json
python3 link.py workspace/slices/<train-id>.slice.json --check-story workspace/slices/<train-id>.story.md

# the mechanical check of the written issue (exit 0 = passes, 1 = lists, 2 = usage)
python3 newsletter.py workspace/view/bundle.json --check-newsletter workspace/newsletter.md \
  --slices workspace/slices [--people ~/activity/people.json] [--weekly]
```

The view must be linked (`link.py`; with `--people` for registry context). The outline adds
nothing to the view and writes no file: the same view always gives the same outline.

## Section order, and why

| # | Section (heading) | Outline key | Omitted when |
|---|---|---|---|
| 0 | Title and intro (no heading) | `intro` | never |
| 1 | **Breaking changes and deprecations** | `breaking` | no declared breaking item, candidate, deprecation, removal or orphaned module |
| 2 | **New modules** | `new_modules` | no module is new in the window |
| 3 | **What changed in existing modules** | `module_changes` | no module had a reader-facing change |
| 4 | **Stories** | `stories` | no train qualified as a story |
| 5 | **People** | `people` | the view has no people layer |
| 6 | **Coming up and stuck** | `coming` | no open pull request had activity in the window |
| 7 | **Plumbing** | `plumbing` | nothing merged was plumbing |
| 8 | **By the numbers** | `numbers` | never |

Breaking changes come first because they are the one thing a reader must act on before the
next upgrade; a release-notes post that buries them under new features costs its readers a
broken deployment. New modules come next (the good news, and rare: a month often has none),
then changes, which most months carry the bulk of. The stories follow the changes they explain
(a reader who saw "the Git configuration of the Data Factory module was replaced" can read why),
so a story that belongs to a breaking change or a lead module is linked from its line above
(`story` on each row). People come after the work so that each person is named with what they
did. Open work and plumbing are for readers who want to help or follow the repository; numbers
go last because they are context, not news.

**Omitting a section:** leave its heading out and write one HTML comment where it would have
been, `<!-- omitted: <id> — <reason> -->`, with the outline's `omit` reason (or your own, for
a section you chose to drop in a weekly issue). The intro may mention the absence in plain
words ("no new modules this month") when a reader would expect one.

## What the outline pre-selects, and what the writer decides

| Section | The code decides (`--outline`) | The writer decides |
|---|---|---|
| intro | `lead_with`: the facts to open with, in order: declared breaking areas, new modules, the three lead modules, newcomers who landed merged work, the top story, the plumbing share | the headline and 3–5 sentences |
| breaking | `declared` (changelog "Breaking Changes" items verbatim, version, `published` in the window or not, link, `story`), `candidates` (rule, subject, `covered_by_declared`, `wording: check before upgrading`), `deprecations` (target, basis: changelog / marker file), `notices` (removed / orphaned modules); declared ordered by item count, then area | which declared items to quote in full, what to do in one sentence |
| new_modules | each new module with first version, `published`, its changelog items, the PRs and `delivered_by` (login, name, affiliation) | the one- or two-sentence "what it deploys", from those items only |
| module_changes | modules with a reader-facing change (not `plumbing_only`, not new), ordered by the module layer's `product_score` then area, capped (12 monthly / 6 weekly; the rest in `also`); per module: version `from` (version file) → `to` (latest changelog version), `published` / `declared_not_published` / child versions, non-breaking changelog items (capped), interface names, PRs, `breaking` flag, `story` | the grouping words, which items to quote, a "why it matters" line only when the changelog or a story states it |
| stories | `full` (2 monthly / 1 weekly, by story score; when they all share one outcome and a story with the other outcome is selected, the last full slot goes to it), `pointers` (the rest, then `stories.more`), each with its title, outcome, slice and check commands | the story (the story brief), the one-line pointers |
| people | `newcomers` (first-ever and returning, with `contribution` — `new_module`, `module_change`, `release`, `docs`, `plumbing`, `open_pr`, `issue` — their merged and open PRs, affiliation with its signal and strength, gap bucket, registry context), `returning_by_gap`, `affiliation` (people, merged PRs and reviews by label with shares), `community`, `maintainers` (reviews, merges of others' PRs, own merges, what they authored by class), `top` (the fame hall with merged PRs by class), `not_named` (do_not_name people left out) | who to thank and how, in one or two sentences each |
| coming | open PRs with a recorded event in the window, by the derived `ball` of their train slice: `ready` (approved, nothing outstanding, checks not failing), `failing_checks` (rule R5), `needs_first_review`, `awaiting_review`, `other` (counts only), with days since the last event (longest wait first), failing check names, `title_signal: new_module` for a title that proposes a module; `forecast` (likely tier); `quiet` (stories with history and no turn this window) | what a reader can do (review, test, answer) |
| plumbing | share, counts by class, the plumbing PRs (sweeps first, by areas touched) | a short paragraph |
| numbers | `facts` (each from one view key: merged PRs, plumbing share, modules touched / reader-facing / new / with declared breaking changes, versions published, PRs and issues opened, people by affiliation, merged PRs and reviews by affiliation, newcomers, stories, open trains) and `coverage` (what was not gathered or is uncertain) | which figures to show; the coverage notes, all of them |

**Inputs (view keys):** `module_changes` (summary, modules, prs), `module_notes.tags`,
`stories`, `trains` (story flags, outcome, code areas), `prs` / `issues` (titles, authors,
merged_by), the train slice's `conversation.outcome` (ball, verdicts, checks — built from
`pr_activity`, `pr_history`), `contributors` (affiliation, community, newcomers, hall),
`affiliations` (labels, names, do_not_name), `newcomers`, `people`, `people_context`,
`forecast`, `meta`. A view without the module layer omits sections 1–3 and 7 with the reason
"the view has no module layer"; without `contributors` the people section is omitted.

## Voice

- Written for someone who deploys the modules and has never opened the repository: say what a
  module deploys, what changed for its user and what to do; PR numbers are links, not the story.
- Plain, factual, warm by what it reports (a first contribution, a fix that unblocked people),
  never by adjectives about people.
- The maintainers' changelog words are the best description of a change: quote them (verbatim,
  in double quotes) rather than paraphrase; a paraphrase must not add anything.
- Dates carry the year ("6 October 2026"). Versions are written as the view has them.
- Name a person as **Name (@login)** the first time, with the view's name (`name`) exactly; a
  person without a name is written as `@login`.

## Length

Monthly: about 1,200–1,800 words without link URLs — intro 60–100, breaking 100–250, new
modules up to 80 per module, changes 250–400, each full story the story brief's 200–300 words
plus its closing part, pointers one line each, people 150–300, coming 100–200, plumbing 50–120,
numbers a short list. Weekly: 400–800 words (below).

## What must be linked

Every module change, breaking item and new module links the PR (or commit) its `sources`
name; every person's contribution links its PR or issue; every story pointer links its root
issue or first PR; every open PR in "Coming up and stuck" links the PR. Link text is the
`#N`; the URL is the item's `url` from the view.

## What must not be said

- **Breaking candidates as fact.** `candidates` are rules over the interface diff, not the
  maintainers' statement. Word them "check before upgrading" (or name them a candidate); never
  "breaking change", "breaks" or "declared" for a module whose maintainers declared nothing.
  A candidate the declared text already covers (`covered_by_declared`) needs no line of its own.
- **Unpublished versions as released.** A version in `declared_not_published` was declared in
  the changelog but no tag of it is dated in the window: say "declared in the changelog; not
  yet published as of <end of window>", never "released".
- **`unknown` as community.** Affiliation `unknown` is its own group; it is never called
  community, and never Microsoft. A sentence that says "community" (or the home organisation's
  name) names only people with that label. A `weak` label (a profile company) is said as "by
  their profile".
- **do_not_name people.** They are counted (`not_named`) but never named or listed by login.
- **Characterisations.** No judgement of competence, mood, tone, effort or motive ("finally",
  "frustrated", "tireless", "ignored", "stalled by …"); no blame for open work. "Coming up and
  stuck" says the derived state ("waiting for a first review", "checks failing on the head
  commit") and never names whom the next move is with — that belongs to a story, where the
  brief introduces it as derived.
- **Impressions the record does not support.** No "why it matters" without a changelog or
  story line behind it; no trend without two gathered windows; no "first ever" from anything
  but `newcomers`; no tool, model or vendor names that appear in records (agent footers,
  automation accounts) — describe the change, not how it was typed.
- **Anything after the window.** A PR description is as last gathered; do not use what it
  says happened after the window's end.

## Full stories in the issue

Each full story sits between `<!-- story: <train-id> -->` and `<!-- /story -->`, with its
bold headline first and its "Where it stands" / "What landed" closing last, exactly as it
passed `link.py --check-story` against its own slice. A story is not shortened to fit the
issue: if the issue is too long, make a full story a pointer instead.

## Weekly variant

`--weekly` keeps breaking changes and new modules at full size (a user must never miss them)
and collapses the rest: intro 2–3 sentences; changes capped at 6 modules, one line each, the
rest named in one "also updated" line; 1 full story, up to 4 pointers; people: newcomers and
the affiliation split in one line, maintainers' load only when notable; coming: ready and
failing only; plumbing one sentence; numbers 3–4 figures. Sections still follow the same
order and omission rule.

## A multi-repo stream (a manifest of module repositories)

Pass every member's linked view (`workspace/view/<owner>__<repo>.json`) to `--outline` and to
`--check-newsletter`. Every row then carries `repo`; sections are merged across members and
ordered by the same keys (a module is a repository, or a submodule `modules/<name>` inside
one), so the lead modules are the most substantive across the stream, not per repository.
Group the changes section by repository when a reader thinks in repositories (one Terraform
module per repository): one sub-heading per repository that has a reader-facing change, in
outline order. People counts stay per repository (`numbers.by_repo`; a person may work in
several); the stream totals add only additive counts (merged PRs, modules, versions). A
member's GitHub release notes (`module_changes.releases`) stand in for a changelog.

## The check (`--check-newsletter`)

It fails (exit 1) and lists:

| Key | What |
|---|---|
| `sections_missing` / `omitted_section_present` / `sections_out_of_order` / `title_missing` / `intro_missing` | every section is present under its heading or omitted with `<!-- omitted: id — reason -->`; a section the outline omits is not padded; headings in the order above; a `# ` title and an intro |
| `stories` / `stories_not_checked` / `stories_not_in_outline` | each story block passes `link.py`'s story check against `--slices DIR/<id>.slice.json`; a block without its slice, or for a train that is not a story, fails |
| `modules_not_in_view` | an AVM module path (or `modules/<name>`) that is not a module, child, code area or path the view's changelogs name |
| `versions_not_in_view` | a version (x.y.z) in a paragraph naming one module that the module does not know (changelog, tags, version file, children, versions its changelog text names); with no or several modules, a version no module knows |
| `items_not_in_view` / `urls_not_in_view` | a `#N` that is not a PR or issue of the view; a URL that occurs nowhere in the view |
| `quotes_not_in_view` | a quoted text ("…", at least 4 characters) that is not verbatim in a view text |
| `people_not_in_view` / `name_mismatch` / `do_not_name_named` | an `@login` the view does not know; a "Name (@login)" whose name is not the view's; a do_not_name person (by login, or by the registry's name with `--people`) |
| `affiliation_mismatch` | a sentence saying "community" (or the home organisation's name) about a person with another label — `unknown` included |
| `candidates_worded_as_declared` | a paragraph with "break", "breaks" or "breaking" about a module whose breaking signal is only a candidate (or a candidate subject the declared text does not cover), with no hedge ("check before upgrading", "candidate", "may", "detected") |
| `dates_without_year` | a day-and-month date without its year (outside story blocks, which the story check covers) |
| `numbers_not_in_view` | a number in the prose (not a date, version, `#N`, year or quoted text) that the outline or the view's affiliation roll-up does not hold |
| `characterising_words` | a word from `newsletter.CHARACTERISING_WORDS` |

**What it cannot check** (read the issue once against "What must not be said"): that a
paraphrase says what the record says (only quotes are compared), that a "why it matters"
line is supported, that a number is attached to the right fact (only that the view holds
it), that a contribution class is the right one, tone beyond the word list, that the
selection rules were followed, and anything inside a story block beyond the story check.
The result lists these under `cannot_check`.

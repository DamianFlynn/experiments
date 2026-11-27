# The newsletter — a release-notes style issue that tells the month's story

How to write the reader-facing newsletter of one stream (AVM Bicep, AVM Terraform, the AI
landing zone, an enterprise-scale landing zone) for one week or one month, from a linked view.
The digest of `report-template.md` is the maintainers' fact base; the newsletter is the post
written for the people who **use** the modules: what is new, what changed, what breaks, the
stories behind the changes, who owns and who built what, what is coming, and what was
engineered underneath.

**Universal rules** (as for the digest): the view is the only source of truth; every claim
links its item; a section with nothing behind it is omitted, never padded; the writer chooses
words, the code chooses what goes in and in which order.

**No length target.** The issue tells the story of the window: a busy month is a long
document, a quiet week a short one. Nothing is cut to fit — every changed module is listed,
every story is written in full. The only limit in the code (`SAFETY_LIMIT`, 500 rows per
list) exists to stop absurd output from a runaway view; it is not a selection rule, and a list
it cuts says so (`<key>_omitted`).

**Every mention answers the reader's next question.** A reader who sees a module, a version
or "landed" asks: which module (show me), which version (where), and does landed mean I can
use it? So:

- a module / pattern / resource named in a reader-facing section is a link to its folder
  (`url` on every outline row; the module layer's own `url` when the view has one, else
  derived from the repository, base branch and path, `url_source: derived`);
- a version stated with a module links its tag when it is published (`release[].url`);
- a PR said to have **landed / merged / shipped** says, in the same sentence or the next,
  what that means for the reader — the outline's `release` status: **released as** version X
  (linked), **declared in the changelog but not yet published**, **no version change**
  (docs / tests / CI only, or a change that declared no version), or **release status not
  known** (tags not read for the whole period, or a tag the record cannot tie to the PR);
- a change row says what changed for a user, why it came up and for whom, from the record.

## The commands

```bash
# the ordered, pre-selected material per section (read-only; JSON on stdout)
python3 newsletter.py workspace/view/bundle.json --outline > workspace/view/newsletter.outline.json
#   weekly issue: add --weekly; a multi-repo stream: pass every member's linked view;
#   an automation account the view does not mark (no `[bot]` suffix): --automation LOGIN

# one slice per story (every story of stories.full), each story checked against its slice
python3 link.py workspace/view/bundle.json --slice <train-id> > workspace/slices/<train-id>.slice.json
python3 link.py workspace/slices/<train-id>.slice.json --check-story workspace/slices/<train-id>.story.md

# the mechanical check of the written issue (exit 0 = passes, 1 = lists, 2 = usage)
python3 newsletter.py workspace/view/bundle.json --check-newsletter workspace/newsletter.md \
  --slices workspace/slices [--people ~/activity/people.json] [--automation LOGIN] [--weekly]
```

The view must be linked (`link.py`; with `--people` for registry context). The outline adds
nothing to the view and writes no file: the same view always gives the same outline.

## Section order, and what each is written from

| # | Section (heading) | Outline key | Written from | Omitted when |
|---|---|---|---|---|
| 0 | Title and intro (no heading) | `intro` | `lead_with`: new modules, the lead changed modules, that a declared breaking change exists (`link_down`: link to the breaking section), newcomers with merged work, the story groups, the plumbing themes | never |
| 1 | **New modules** | `new_modules` | `module_changes.summary.new_modules` + the module rows | no module is new |
| 2 | **What changed in existing modules** | `module_changes` | every module with a reader-facing change, with release, prompting issue, interface delta, PRs | no module had a reader-facing change |
| 3 | **Breaking changes and deprecations** | `breaking` | declared items, candidates, deprecations, removed / orphaned modules | none of these |
| 4 | **Stories**, grouped `### Landed`, `### In flight`, `### Stalled` (`### Closed without landing` when any) | `stories` | `stories.all` (else the selected and further stories, grouped provisionally) and their slices; `thin` for trains with history but no turn in the window | no story |
| 5 | **Module ownership** | `ownership` | `module_ownership` | the view has none (`needs a store gathered with module ownership`) or it holds no event and no module without an owner |
| 6 | **People** | `people` | `contributors`, `affiliations`, `newcomers`, `people_context`, PR release status, automation accounts | no people layer |
| 7 | **Coming up and stuck** | `coming` | each open train's slice outcome (derived next move, checks), forecast | nothing open was active |
| 8 | **Plumbing: what was engineered** | `plumbing` | `plumbing_themes` (else provisional themes by PR class), their PRs' descriptions and commit evidence, plumbing stories | nothing merged was plumbing |
| 9 | **By the numbers** | `numbers` | counts and coverage notes | never |

New first, then changed, then breaking: the owner's order. The intro still tells the reader at
once that a breaking change exists and links down to it, so nobody misses it. Stories follow
the changes they explain (each change row carries its `story` key). Ownership comes before
people because it is about modules (who looks after them, which need someone). People come
after the work so that each person is named with what they did. Plumbing is told as
engineering — what problem, what was built — not as a count; numbers go last because they are
context.

**Omitting a section:** leave its heading out and write `<!-- omitted: <id> — <reason> -->`
where it would have been, with the outline's `omit` reason. The intro may mention the absence
("no new modules this month") when a reader would expect one.

## Per section: what the outline gives and what the writer must do

**New modules** — `modules[]`: area, `url`, kind, `first_version`, `release` (status, tag
link), the module's changelog items, PRs, `delivered_by` (login, name, affiliation), `story`.
Write what the module deploys from its changelog / README words, its first version (linked),
who delivered it.

**What changed in existing modules** — every module with a reader-facing change, ordered by
the module layer's `product_score`, then area. Per module:

| Key | Use it to answer |
|---|---|
| `url`, `release` (per version: `published` with `tag`, `date`, `url`; `declared_not_published`; `unknown`; or `no_version_change`) | which module, released as what |
| `changelog` (non-breaking items, verbatim), `breaking` (declared / candidate flag) | what changed for a user — in the maintainers' words |
| `prompted_by` (issues the PRs close: title, author, name / affiliation, what they asked — the issue form's Description, verbatim) | why it came up, who asked, for whom |
| `interface_detail` (parameters / outputs / resources / dependencies added, removed, modified with before / after), `interface` (names) | what a user must change, if anything |
| `pr_details` (number, title, author with name / affiliation or `author_automation`, merge date, fork, description as last gathered), `tests_added` (test folders the PRs added) | who did it, how it was proven |
| `story` | link to the story below when there is one |

A change row says what, why, for whom and released as what; where the record does not say
why or for whom, the row says what it can and does not guess.

**Breaking changes and deprecations** — `declared` (changelog "Breaking Changes" items
verbatim, `release`, `published` true / false / null), `candidates` (`wording: check before
upgrading`, `covered_by_declared`), `deprecations`, `notices`. Say what breaks, in which
module and version (linked), and what to do.

**Stories** — every story of the window, in full, grouped by state: `groups` {landed,
in_flight, stalled, closed, plumbing} from the view's `stories.all` (`group_source: view`) or,
until the view has it, provisionally from the train (`group_source: provisional`: shipped →
landed; in flight with `effort.stalled` → stalled; in flight; closed; plumbing when every
merged PR is plumbing). Each row: `key`, `group`, `state`, `group_rule`, title, `modules`
(with `url`), `released` (release rows of its merged PRs), slice / check commands. Plumbing
stories are written in the plumbing section under their theme. `thin`: trains with history
but no conversation turn in the window — one line each with module (linked), state and the
reason (`idle_days`).

Each story follows the story brief (`reference/report-sections.md`) unchanged in rigour, and
**its first line states its state** ("**Landed — …**", "**In flight — …**", "**Stalled —
…**", "**Closed — …**"). It answers, from its slice: which module or pattern it affects
(linked), why it came up, what was decided or changed, the state of play, where it is heading
or what it waits on, and how a reader can help. There is no "in short" pointer list. A story
not yet written in full is acknowledged with `<!-- story pending: <key> — <reason> -->` (it is
listed as pending; a story neither written nor pending fails the check).

**Module ownership** — `events` (owner_needed / owner_added / owner_removed / orphaned /
adopted: module with `module_url`, people, date, source link, quote) and `without_owner` (the
standing list, with `marker` and `since`). Say who now looks after which module, which need
an owner, and link where a reader can volunteer (the event's source).

**People** — newcomers (first-ever and returning) with what they contributed and, for each
merged PR, its `release` and `module_urls`; affiliation split with the unknown share; the
maintainers' review and merge load; top contributors; registry context. **Automation**
accounts (`automation`) in their own short paragraph: each named, marked as automation, with
what it did (counts, PRs) — never hidden, never presented as a person.

**Coming up and stuck** — as before: `ready` (derived "awaiting merge"), `failing_checks`,
`needs_first_review`, `awaiting_review`, `other` counts, `forecast`, with `module_urls`. Never
name whom the next move is with here.

**Plumbing: what was engineered** — `themes`: the view's `plumbing_themes` (`theme_source:
view`) or provisional themes by PR class (sweeps, CI, tests, docs, mixed). Per theme: a short
narrative paragraph written from its PRs' descriptions (as last gathered), commit evidence and
stories — what problem, what was built or changed, what it means for contributors (test
environment, CI checks, README generation, …), who did it — then the plumbing stories in full
under the theme's `###` heading, then the counts (`share`, `by_class`).

**By the numbers** — `facts` and every `coverage` note.

## Voice

- Written for someone who deploys the modules and has never opened the repository: what a
  module deploys, what changed for its user, what to do; PR numbers are links, not the story.
- Plain, factual, warm by what it reports (a first contribution, a fix that unblocked people),
  never by adjectives about people.
- The maintainers' changelog words are the best description of a change: quote them (verbatim,
  in double quotes); a paraphrase must not add anything.
- Dates carry the year ("6 October 2026"). Versions are written as the view has them.
- Name a person as **Name (@login)** the first time, with the view's `name` exactly; a person
  without a name is `@login`. An automation account is written with the marker word
  **automation** in its sentence ("the automation account @x[bot] …") and never as
  "Name (@login)".

## What must not be said

- **Breaking candidates as fact.** `candidates` are rules over the interface diff, not the
  maintainers' statement: "check before upgrading", never "breaking change" or "breaks" for a
  module whose maintainers declared nothing.
- **Unpublished versions as released.** `declared_not_published` is "declared in the
  changelog; not yet published as of <end of window>"; `unknown` is "release status not
  known".
- **`unknown` as community.** Affiliation `unknown` is its own group; a sentence that says
  "community" (or the home organisation's name) names only people with that label; a `weak`
  label is said as "by their profile".
- **do_not_name people** — counted (`not_named`), never named anywhere in the issue: prose,
  stories, quotes, code, comments, headings, tables, link and image text, profile or fork URLs
  (`github.com/<login>`). A PR or issue link does not contain its author's login and may stay.
- **Characterisations** — no judgement of competence, mood, effort or motive; no blame for
  open work.
- **Impressions the record does not support** — no "why it matters" without a changelog,
  issue or story line behind it; no trend without two gathered windows; no "first ever" from
  anything but `newcomers`; no tool, model or vendor names that appear in records (agent
  footers) — describe the change, not how it was typed.
- **Anything after the window** — a PR description is as last gathered.

## Story blocks

Each story sits between `<!-- story: <key> -->` and `<!-- /story -->` under its group's
heading (`### Landed` …; a plumbing story in the plumbing section), with its state in its first
line and its "Where it stands" / "What landed" closing last, exactly as it passed `link.py
--check-story`. The key is the outline's `key` (plain train id for one repository,
`owner/repo:<train-id>` for several); its slice file is the row's `slice` target. Each story
appears once.

## Weekly variant

`--weekly` uses the same layout and the same rules; a weekly issue is shorter only because the
window holds less.

## A multi-repo stream (a manifest of module repositories)

Pass every member's linked view (`workspace/view/<owner>__<repo>.json`) to `--outline` and to
`--check-newsletter`. Every row carries `repo`, every story its qualified `key`, and every
repository-local name in a list (`plumbing_only`, the intro's areas and logins) reads
`<owner/repo>::<name>`. Group the changes section by repository when a reader thinks in
repositories (one Terraform module per repository); people counts stay per repository
(`numbers.by_repo`); stream totals add only additive counts. A member's GitHub release notes
(`versions.releases`) stand in for a changelog.

## Inputs (view keys)

`module_changes` (summary, modules with `url` when recorded, prs with `head_repo` /
`is_fork`), `module_notes.tags`, `stories` (`all` when present, `selected`, `more`, `quiet`),
`trains`, `prs` / `issues`, `pr_history` (commit paths), the train slices' outcome,
`contributors`, `affiliations`, `newcomers`, `people`, `people_context`, `forecast`,
`plumbing_themes`, `module_ownership`, `meta`. Every key is optional: a view without one
omits or falls back, as each section says.

## The check (`--check-newsletter`)

It fails (exit 1) and lists:

| Key | What |
|---|---|
| `sections_missing` / `omitted_section_present` / `sections_out_of_order` / `title_missing` / `intro_missing` | every section present under its heading or omitted with `<!-- omitted: id — reason -->`; a section the outline omits is not padded; headings in the order above; a `# ` title and an intro |
| `markers_malformed` | a story marker without its pair, nested or without a key; an `omitted` marker without an id and reason; an unclosed HTML comment |
| `stories` / `stories_not_checked` / `stories_not_in_outline` / `stories_duplicate` | each story block passes `link.py`'s story check against `--slices DIR/<slice file of its key>`; a block without its slice, for a train that is not a story, or repeated fails |
| `stories_missing` | a story of the outline neither written nor marked `<!-- story pending: <key> — <reason> -->` (pending ones are listed in `stories_pending`, which does not fail) |
| `stories_misplaced` / `stories_state_missing` | a story not under its group's `###` heading in the Stories section (a plumbing story: not in the plumbing section); a story whose first line does not start with its state (`Landed`, `In flight`, `Stalled`, `Closed`) |
| `modules_not_linked` / `versions_not_linked` | in New modules, What changed, Breaking, Module ownership, People and Coming up: a module path the view knows (a module or a versioned child) that is not inside a link's text; a published version written right after a module path that is not a link |
| `release_status_missing` | in New modules, What changed and People: a sentence that names a PR or module and says landed / merged / shipped, with no release status ("released as", "published", "not yet published", "declared in the changelog", "no version change", "release status not known") in it or the next sentence |
| `ownership_not_in_view` | a module named in the Module ownership section that `module_ownership` does not hold |
| `automation_as_person` | an automation account written as "Name (@login)", or mentioned in a sentence without the marker word "automation" |
| `modules_not_in_view` | a module path that is not a module, child, code area, path the view's changelogs / PR titles name, or the module an issue form names |
| `versions_not_in_view` | a version that does not belong to its module (versions indexed by the module they belong to; a version right after a module path is that module's; any other must belong to a module the paragraph names, a named child or a child of a named parent) |
| `items_not_in_view` / `urls_not_in_view` | a `#N` that is not a PR or issue of the view; a URL that is neither in the view nor one the outline or the module / tag link rule gives |
| `repository_unresolved` | multi-repo only: a module area or `#N` several members know, in a paragraph whose repository is not resolved |
| `quotes_not_in_view` | a quoted text that is not verbatim in a view text (of the paragraph's repository when resolved) |
| `people_not_in_view` / `name_mismatch` / `do_not_name_named` | an `@login` the view does not know; a "Name (@login)" whose name is not the view's; a do_not_name person anywhere in the complete issue |
| `affiliation_mismatch` | a sentence saying "community" (or the home organisation's name) about a person with another label |
| `candidates_worded_as_declared` | a paragraph with "break" / "breaks" / "breaking" about a module whose breaking signal is only a candidate, with no hedge; a repository paragraph naming no module speaks of its root module (`main.tf`); headings and the intro never do |
| `dates_without_year` / `numbers_not_in_view` / `characterising_words` | a date without its year; a number the outline or the affiliation roll-up does not hold; a word from `newsletter.CHARACTERISING_WORDS` |

**What it cannot check** (`cannot_check` in the result): that a paraphrase says what the
record says (only quotes are compared); that a "why it matters" or "for whom" line is
supported; that a number is attached to the right fact; that a contribution class or a
release status is attached to the right PR (only that one is stated); that a link points to
the folder of the module its text names (only that the URL is known); that a plumbing theme
paragraph describes its PRs; tone beyond the word list; anything inside a story block beyond
the story check.

**Identifiers and unknowns.** Logins and repository slugs compare case-insensitively
(`login_key`, `slug_key`), versions without a leading `v` (`version_key`), story keys with
their repository part case-insensitive (`story_key_norm`); module paths and train ids exactly.
Where the view does not establish a fact the outline says `null` / `unknown`, never `false`,
zero or empty: publication without full tag coverage, a PR's release when a tag cannot be tied
to it, a maintainer's counts without their input, the plumbing share without its share;
`numbers.facts` marks a partial version count `versions_published_partial`, and the coverage
notes name the windows whose tags were not listed.

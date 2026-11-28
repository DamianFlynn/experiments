# The newsletter — a release-notes style issue that tells the month's story

How to write the reader-facing newsletter of one stream (AVM Bicep, AVM Terraform, the AI
landing zone, an enterprise-scale landing zone) for one week or one month, from a linked view.
The digest of `report-template.md` is the maintainers' fact base; the newsletter is the post
written for the people who **use** the modules: what is new, what changed, what breaks, the
stories behind the changes, who owns and who built what, what is coming, and what was
engineered underneath.

Short daily posts for LinkedIn and X (one per story, drafts for approval) are written from the
same view: `reference/social.md`.

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
#   weekly when the window is at most 16 days, else monthly (--weekly / --monthly override);
#   a multi-repo stream: pass every member's linked view;
#   an automation account the view does not mark (no `[bot]` suffix): --automation LOGIN

# one slice per story (every story of stories.full), each story checked against its slice
python3 link.py workspace/view/bundle.json --slice <train-id> > workspace/slices/<train-id>.slice.json
python3 link.py workspace/slices/<train-id>.slice.json --check-story workspace/slices/<train-id>.story.md

# many stories: a work package for parallel writers (slices, rows, batches, manifest)
python3 newsletter.py workspace/view/bundle.json --work-package workspace/wp

# the mechanical check of the written issue (exit 0 = passes, 1 = lists, 2 = usage)
python3 newsletter.py workspace/view/bundle.json --check-newsletter workspace/newsletter.md \
  --slices workspace/slices [--people ~/activity/people.json] [--automation LOGIN] [--weekly|--monthly]
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
| 4 | **Stories**, grouped `### Landed`, `### In flight`, `### Stalled`, `### Closed without landing` (plumbing stories in section 8) | `stories` | `stories.all` (else the selected and further stories, grouped provisionally), the story plan and the slices | no story |
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

**What a module is.** Every module row (`info`: `display_name`, `description`,
`resource_type`, `lifecycle`, from the module's metadata file via the module layer or
`module_ownership.modules_at_end`; a story row's `module_info`) says what the module is: on a
module's **first mention** in the issue, say it in a few words ("the Data Factory module, which
deploys `Microsoft.DataFactory/factories`"), then use the module's link alone.

**What changed in existing modules** — every module with a reader-facing change, ordered by
the module layer's `product_score`, then area. Per module:

| Key | Use it to answer |
|---|---|
| `url`, `info`, `release` (per version: `published` with `tag`, `date`, `url`, the version's `contents` and `note`; `declared_not_published`; `unknown`; or `no_version_change`) | which module, what it is, released as what, and what the version holds: the PRs or commits it contains, or "republished with its parent" for a child version whose own folder no change touched; `child_releases` lists the child versions published in the window, each with `own_change` and `contents` |
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

**Deprecations** are one part of this section: `deprecations[]` joins what a changelog
declares (`basis: changelog`, `marker_file`) with the tree's lifecycle events
(`module_ownership.events[]` of kind `deprecated`: `basis: lifecycle`, `date`, the marker's
`marker_text`). Say which module (or child module) is deprecated, since when, and what its
marker says to use instead; quote, never paraphrase, the marker. A breaking change in a `0.x`
minor version is stated as the changelog and version say it; where the repository's PR template
says breaking changes bump the MAJOR version, quote that once in this section next to the first
such change, without explaining.

**Stories** — every story of the window, grouped by state and, within a group, in the
owner's order — stories about a new module (`story_kind: new`: a proposed or new module), then
changes (`changed`), then breaking changes (`breaking`: a module with a declared breaking
change) — each part by score: `groups` {landed, in_flight,
stalled, closed, plumbing} from the view's `stories.all` (`group_source: view`; closed = closed
or rejected without landing) or, until the view has it, provisionally from the train
(`group_source: provisional`: shipped → landed; in flight with `effort.stalled` → stalled; in
flight; rejected → closed; plumbing when every merged PR is plumbing; `stories.quiet` → a
stalled line). Each row: `key`, `group`, `state`, `group_rule`, `worthy`, title, `modules`
(module areas only, with `url`), `released` (from the view, with tag link and registry
reference; `unknown` with a `reason` when a module cannot be tied to the PR), `idle_days`,
slice / check commands, and the **story plan**:

| Plan (`story_plan`) | Which trains | What the issue does |
|---|---|---|
| `full` | every train landed, in flight or closed; every stalled train with a human turn or an event in the window (`worthy` is not "carried over"); every plumbing train with a real discussion in the window (a selected story with a turn in the window, or at least `STORY_MIN_EXCHANGES` turns in the window between two or more people) | the story in full, under its group (a plumbing story under its theme) |
| `line` | a stalled train merely carried over | one line in the Stalled group: the root item linked, its module (linked), "stalled", days without recorded activity (`idle_days`) and the derived next move (`waits_on`) |
| `in_theme` | every other plumbing train, and every automation-only train of any group (every PR by an automation account, no human turn) | named (a link to one of its PRs) in its theme's narrative (`theme`) |

`plan_counts` gives the counts per group and plan; `excluded` lists the trains the view left
out (idle over its carry limit). Each story follows the story brief
(`reference/report-sections.md`) unchanged in rigour, and **its first line states its state**
("**Landed — …**", "**In flight — …**", "**Stalled — …**", "**Closed — …**"). It answers, from
its slice: which module or pattern it affects (linked), why it came up, what was decided or
changed, the state of play, where it is heading or what it waits on, and how a reader can help.
A story may link the module folders and tags of its outline row beyond its slice
(`story_links`: the check adds them to the slice as `newsletter_links`). A story not yet written
is acknowledged with `<!-- story pending: <key> — <reason> -->`; a theme narrative not yet
written with `<!-- theme pending: <theme id> — <reason> -->` (both listed as pending; anything
neither written nor pending fails the check).

**How the work connects.** From the view's relation layer (`story_graph`,
`stories.all[].relations`; BUNDLE.md "How stories depend on each other"), every story row carries
`relations`: its links to other stories that are not a bare mention (`blocked_by`, `depends_on`,
`stacked_on`, `follow_up_of`, `caused_by`, `content_landed_in` and their inverses), each with the
record's quote and item, and the other story's title, group, state and link (`in_issue: false`
for a train the issue does not tell), or, for another repository, the external item with no
state. A module story adds `waited_on_plumbing` (the plumbing it waited on); a plumbing theme
adds `needed_by` (module stories that waited on its work) and `caused` — where "what was
engineered" meets "why". The Stories section ends with a `### How the work connects` part from
`connections`: the chains of waiting stories, the items most stories wait on
(`most_blocking`), external targets, and the counts per relation. Story rows also carry `inner_relations` (links between items of the same story) to be told as
facts; a relation basis keeps `task_done` (a task-list line: true when ticked, so not waited on)
and `text_current` (false when the text was edited after the window). When
`connections.chains_capped` is set, the part says the chains were capped. Write it in a few
bullets:
what waits on what and whether it still does, closed work that landed elsewhere, follow-ups and
causes, and links outside the repository (named with their link, no state). The relation layer
holds edges BETWEEN stories; a dependency between two items of one story is in the row's
`inner_relations` and is told as a fact. Only a view without that layer (no `inner_relations`
key on any story row) leaves an in-story dependency to the slice text, stated with the slice's
quote.

**Writing many stories in parallel.** A month can hold dozens of `full` stories.

```bash
python3 newsletter.py workspace/view/bundle.json --work-package workspace/wp
```

writes every `full` story's slice (with `newsletter_links`) and outline row (with
`prompted_by`, `pr_details` with each PR's **full** description and body as last gathered — the
outline keeps `PR_DESCRIPTION_CAP` (800) characters and marks a cut `description_cut` —,
people, and `module_releases`: per module the release rows and `child_releases` with their
`contents`), one theme file per plumbing theme (its PRs with full descriptions and bodies), `manifest.json` (every
story in issue order with its plan, slice size and batch) and batches for parallel writers:
stories in batches of at most `WORK_PACKAGE_BATCH` (8) stories and `WORK_PACKAGE_BYTES`
(1.5 MB) of slices, of roughly equal slice size, and one batch per plumbing theme. Each batch
directory holds `BATCH.md` (the files to read, the files to write, the check command per story)
and `STORY_BRIEF.md` (the story brief plus the newsletter rules below). A writer gets only the
batch path; it checks every story with `link.py --check-story`, and a theme narrative with

```bash
python3 newsletter.py workspace/view/bundle.json --check-theme wp/out/theme-<id>.md --theme <id>
```

before handing it back. The theme check verifies what the story check verifies for a story:
the `### <title_hint>` heading and every `in_theme` train named, and, as `--check-newsletter`
does for the plumbing section, every quote verbatim in the record, URLs, items, modules,
versions, people and names, numbers (in the view or in the PRs the paragraph links or names as
`#N`), each PR's review state and open / merged / closed state and each version's release
status as the view has them, dates with their year, no characterising word. The issue is then
assembled: each pending marker replaced by its story (in its group, in the outline's group
order) or theme (in theme order), and `--check-newsletter` run on the whole.

**Updating an issue for a newer store.** Build a new work package from the new view (keep the
old one), then re-check every written story and theme against it: the story check against the
new slice, `newsletter.state_claims` on the story text, the headline state, and the row (group,
state, release status, PRs, review state). Classify each in `<new wp>/REVISIONS.md` before
rewriting anything: (a) passes and the row is unchanged — keep; (b) passes but the row changed,
or the new view answers what the text said the record did not show — revise; (c) fails —
revise; (d) a new full story — write; (e) no longer full — remove. Revisions go to writers as
`batches/rev-NN/BATCH.md` (the old text, the new slice and row, exactly what to change; same
batch size rule) and one `batches/rev-theme-<id>/` per theme whose membership or material
changed; until they are back, each sits in the issue as a pending marker.

A **closed** story ends with "Where it stands" (what was decided, by whom, what is left), as an
in-flight or stalled one does; only a landed story ends with "What landed".

A train whose every PR is authored by an automation account and that has no human
conversation turn is not a story, whatever group the view gives it: its plan is `in_theme`,
and it is named in the plumbing theme of its PRs' author (`_theme_by_author`).

**Module ownership** — from the view's `module_ownership`: `events` (owner_needed /
owner_response_requested / owner_added / owner_removed / orphaned / adopted: module with
`module_url`, people, date, source link, quote, `via`), `lifecycle_events` (deprecated,
undeprecated, module_added, module_removed, module_info_changed), `unattributed` (events on an
item that names no single module, e.g. a label on a repository-wide PR: say so, or leave it out
with the reason), `labelled_items`, `without_owner` (the standing list at the window's end:
**live and orphaned modules only**, each with `lifecycle`, `marker`, `since` and what the module
is), `without_owner_by_kind` (`empty_owner_list` — the owner file lists nobody —
and `orphan_marker`), `deprecated_modules` (one short paragraph: they are retired, not looking
for an owner; each `deprecated_text` names its replacement where it does), `stub_modules_count`
(folders without a module entry file: at most one sentence, never listed as without an owner),
and `start_without_owner` (derived from `start_state`: a live module whose owner file lists
nobody, or with an orphan marker, at the window's start). Say who now looks after which module,
which need an owner, and link where a reader can act (the event's source; for an orphaned
module, quote its marker's own text, which points to where volunteers go); say when no request
for an owner was recorded. When `without_owner_unavailable` is set (the owner files could not
be read at the window's end) the outline lists no module as without an owner and the section
says the state is not known: never "no module without an owner". In a stream it is a list of
`{repo, reason}` and the section's `omit` says "not known" whenever any member's state is
unknown.

**People** — newcomers (first-ever and returning) with what they contributed and, for each
merged PR, its `release` and `module_urls`; affiliation split with the unknown share; the
maintainers' review and merge load; top contributors; registry context. **Automation**
accounts (`automation`) in their own short paragraph: each named, marked as automation, with
what it did (counts, PRs) — never hidden, never presented as a person. Which accounts are
automation is the view's `people[login].automation` (`{kind, on_behalf_of?}`: an account type,
a `[bot]` login, or the registry's operator tag; `null` is the operator's "not automation" and
wins over a `[bot]` suffix); without that key, `is_bot` / the `[bot]` suffix, plus
`--automation LOGIN`. An account acting for a person (`on_behalf_of`) is "an automation
account (a coding agent) working for Name (@login)". A PR row's `fork_status` (`fork`,
`deleted_fork`, `not_fork`, `unknown`) says where the change came from; `closed_unmerged`
(`superseded_by`, or no reason recorded) says how a closed PR ended — write "the record states
no reason" when it has none.

**From the refreshed store.** Ownership adds `marker_texts` (a notice many modules share,
said once, with how many carry it; `deprecated_replacements` lists only replacements explicit
text names), the orphan marker's `marker_text` on its `without_owner` row (quote it: it is the
call to action) and `proposed_modules` (the tree's `proposed` lifecycle, with what each module's
own metadata says it is). PR rows add `net_files` (what the PR changed as a whole),
`author_on_behalf_of` (the person an automation account worked for on that PR) and
`closed_unmerged.content_landed_in`; story rows add `joined_by`; automation rows add
`on_behalf_of_by_pr` and `same_identity_as` (one automated reviewer under two accounts: say it
once).

**Coming up and stuck** — `ready` (derived "awaiting merge" **and** `review_state` approved),
`approval_stale` (awaiting merge, but the approval predates the author's later changes: not
ready), `failing_checks`, `needs_first_review` (review state `none`, `requested` or
`automated_only`), `awaiting_review` (a human has reviewed), `other` counts, `forecast`, and
`proposed_modules` (the new modules open PRs propose: `stories.all[].new_module_proposed`, never
a PR title; each with the train's open PRs, their review state and `fork_status`, the story's
group and `info` when the PR adds the module's metadata), with `module_urls`. Never name whom
the next move is with here.

**One review state.** Every row that names an open PR carries `review_state` (the view's
`prs[].review_state.state`: `none`, `requested`, `automated_only`, `commented`,
`changes_requested`, `approved`, `approved_stale`). It is the only source for review wording,
in every section and story: "approved" only for `approved`; "approved before the latest
changes" for `approved_stale` (never "ready to merge"); "waiting for a first review" for
`none` / `requested`; "reviewed only by automation" for `automated_only`.

**Plumbing: what was engineered** — `themes`: the view's `plumbing_themes` (`theme_source:
view`) or provisional themes by PR class (sweeps, CI, tests, docs, mixed). Per theme: an intro
and one paragraph per strand of work (as long as the work needs), written from its PRs' descriptions (as last gathered), commit evidence and
stories — what problem, what was built or changed, what it means for contributors (test
environment, CI checks, README generation, …), who did it — naming every `in_theme` train of
the theme by a link to one of its PRs — then the plumbing stories in full under the theme's
`###` heading (`### <title_hint>`), then the counts (`share`, `by_class`). Themes from the view
carry `subjects`, `issues`, `paths` / `paths_more` and `evidence_counts`.

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

A window of at most `WEEKLY_MAX_DAYS` (16) days is a weekly issue by default (`cadence:
week`, `cadence_source: window`, `window_days`); a longer one is monthly. `--weekly` or
`--monthly` overrides (`cadence_source: caller`). Two weeks are a weekly issue, not a month;
16 days leave room for a window that starts or ends mid-week. A weekly issue uses the same
layout and the same rules; it is shorter only because the window holds less.

## A multi-repo stream (a manifest of module repositories)

Pass every member's linked view (`workspace/view/<owner>__<repo>.json`) to `--outline` and to
`--check-newsletter`. Every row carries `repo`, every story its qualified `key`, and every
repository-local name in a list (`plumbing_only`, the intro's areas and logins) reads
`<owner/repo>::<name>`. Group the changes section by repository when a reader thinks in
repositories (one Terraform module per repository); people counts stay per repository
(`numbers.by_repo`); stream totals add only additive counts. A member's GitHub release notes
(`versions.releases`) stand in for a changelog.

**A story never spans repositories.** A story is one member's train, and `link.py` builds
trains per repository: PRs join only through the issues of their own repository that they
close. Work that crosses members (a PR in one repository closing or citing an issue in
another) is told as one story per member, each under its own qualified key, with nothing in
the outline joining them; the project trains of `digest.py` (`ptrain-…`, BUNDLE.md) are not
read here. A story may name another member's item by link or `owner/repo#N`; the story check
treats such an item as outside its slice (never covered). Outside story blocks, a
paragraph's repository is the one its github.com links name when they name exactly one
member, else the one the nearest heading above it names (`owner/repo`, or a repository name
no other member shares); `#N`, module areas and quotes are resolved in that repository.

## Inputs (view keys)

`module_changes` (summary, modules with `url`, `display_name`, `description`,
`resource_type`, `module_lifecycle` when recorded, prs with `head_repo`), `prs[]`
(`review_state`, `fork_status`, `closed_unmerged`, `body_as_of`), `module_notes.tags`, `stories` (`all` when present, `selected`, `more`, `quiet`),
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
| `story_lines_missing` | a `line` story whose root item link is not in the issue (and not pending) |
| `themes_missing` / `theme_trains_missing` | a plumbing theme with neither a `### <title_hint>` heading in the plumbing section nor a `<!-- theme pending: … -->` marker; an `in_theme` train of a written theme that the plumbing section does not name (a link to one of its PRs or its root item) |
| `stories_misplaced` / `stories_state_missing` | a story not under its group's `###` heading in the Stories section (a plumbing story: not under its theme's heading in the plumbing section); a story whose first line does not start with its state (`Landed`, `In flight`, `Stalled`, `Closed`) |
| `modules_not_linked` / `versions_not_linked` | in New modules, What changed, Breaking, Module ownership, People and Coming up: a module path the view knows (a module or a versioned child) that is not inside a link's text; a published version written right after a module path that is not a link |
| `release_status_missing` | in New modules, What changed and People: a sentence that names a PR or module and says landed / merged / shipped, with no release status ("released as", "published", "not yet published", "declared in the changelog", "no version change", "release status not known") in it or the next sentence |
| `ownership_not_in_view` | a module named in the Module ownership section that `module_ownership` does not hold (events, without an owner now or at the start, deprecated, stub, labelled) |
| `superseded_not_in_view` | a sentence saying "superseded" or "replaced by" about a PR closed without merging whose record holds no `superseded_by` (work that landed elsewhere, `content_landed_in`, is said with its basis instead) |
| `relations_not_in_view` | every relation a sentence asserts, read by the shared parser (`link.parse_relation_claims`: subject, relation as written, each target, tense, negated; the subject carried across coordinated predicates, else the sentence's default subject — the root item of the story block it is in, a post's own story) and judged per (subject, target) pair by the shared predicate (`link.relation_holds`, the rule the view itself is built with): evidence of that kind (an inverse cue from the waiting side, also to an external target stored as written) dated on or before the day judged (the window's end, a post's day); a present-tense waiting claim also needs it still in force that day (not a ticked task, its text current, both ends open by `link.item_state`). The reason code is reported (`no_evidence`, `after_day`, `task_done`, `text_not_current`, `source_not_open`, `target_not_open`, `state_unknown` — an end no view holds, such as an external item, so a present-tense claim about one fails while a past-tense one holds). In-story pairs are judged against `inner_relations` like any other evidence; a view without that layer (no `inner_relations` key on any story row) passes an in-story pair only on a quote in the same sentence that the shared parser reads as the same relation to the same target. A bare mention never counts; quoted text is not read for claims; member views of a stream are joined first (`join_members`: `link.join_member_relations` on copies) |
| `relation_denials_contradicted` | a negated claim ("is not blocked by", "no longer needs") asserts nothing to check, but one that denies a relation the record holds in force on the day judged (`link.relation_holds` with `present`) contradicts the record |
| `heading_missing` (theme check only) | a theme narrative whose first line is not `### <title_hint>` of its theme |
| `review_state_mismatch` / `pr_state_mismatch` / `release_status_mismatch` | **one state per fact**, over the whole issue including story blocks (quoted text and comments blanked): a sentence that names exactly one PR and states its review state ("approved", "approved before …", "changes requested", "waiting for a first review", "reviewed only by automation", "ready to merge") that the PR's `review_state` does not hold, or that another section's sentence about the same PR contradicts; the same for a PR's open / merged / closed-without-merging state, and for a module version said to be published or not yet published against its release status. A conditional sentence ("once", "if", "until"), or one counting many PRs ("10 were closed without merging and one, #N, is open"), states nothing about one PR |
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

# Daily social posts — one short post per story, drafted for approval

A daily outline of short posts for two channels, **LinkedIn** and **X**, written from the
same linked view as the newsletter (`reference/newsletter.md`). Posts are **drafts**: the
owner approves each one; nothing is published automatically, and nothing here talks to the
posting service. The text is written **neutral** (reporting, as the newsletter is); a separate
later step, the **voice pass**, may adjust the voice within the rules below.

## The commands

```bash
# what became postable on one day (JSON on stdout; reads the view, writes nothing)
python3 newsletter.py workspace/view/bundle.json --outline --daily --day 2026-10-06 \
  [--posted ledger.json] [--hold-days 3] [--automation LOGIN]

# check a drafts file (exit 0 = every post passes, 1 = lists, 2 = usage)
python3 newsletter.py workspace/view/bundle.json --check-post drafts.md \
  [--people ~/activity/people.json] [--against drafts-before-voice-pass.md]
```

## When a story is posted: one post per story

A story gets **one** post, at the first event a reader can act on. `trigger` says which:

| `trigger` | When | What the post must state |
|---|---|---|
| `published` | a landed story whose module version was published (its release tied to the merge) within `SOCIAL_HOLD_DAYS` (3) days of its last merge: the day of the release | the module (path or link), the published version, "published" / "released", the tag link |
| `merged_not_published` | a landed story with no release tied within the hold: three days after its last merge (it is `held` until then) | the module, "not yet published", and every declared version in `must_state.declared` (declared and untagged, or published only after the hold; a version published before the merge is not one) |
| `merged_release_unknown` | as `merged_not_published`, but a release row of the story is `unknown` (tag coverage incomplete, or the module layer unavailable): unknown does not prove unpublished, so the post keeps the uncertainty | the module, "release status not known", and no claim that it is or is not published |
| `merged_no_release` | a landed story whose PRs change no version (documentation, metadata): its merge day | the module when there is one, "no version change" |
| `new_module_proposed` | an open PR whose own commits add a module's entry file (`new_module_proposed`, the tree's `proposed` lifecycle): the later of that commit's day and the opening of the PR that adds it (the tree's `proposed_modules[].prs`, not any open PR of the train) | the proposed module, that the PR is open (or proposed), and "not published" |
| `breaking_declared_open` | an open PR whose own commits declare items under `### Breaking Changes` in a CHANGELOG (below): the later of that commit's day and the PR's opening | "breaking", that the PR is open (not merged) |
| `owner_added` / `orphaned` / `deprecated` | a module event of `module_ownership` (one post per module and kind): the event's day; an `orphaned` signal that is only a label on a PR (no marker file in the module folder) is held `ORPHAN_LABEL_HOLD_DAYS` (7) and suppressed when the module gains an owner in that time | the module, the event in words ("owner", "orphaned", "deprecated"), a link to the event's source or the module; a deprecation states its replacement with its link when the record names one (`deprecated_modules[].replacement`, explicit text only), including the replacement's module link, else that no replacement is named; an `owner_added` post names each new owner with a display name (`must_state.people`); owners without one, or do_not_name, are only counted (`people_unnamed`) and the post says that the module has a new owner without naming them |
| `closed_not_planned` | a user-facing request or change (a story of the `closed` group: an issue or a PR touching a module, not plumbing) closed as not planned or without merging: the day it closed | that it was closed, the reason as stated (`closing.state_reason`, a PR's `superseded_by` with its link) or "no reason is stated", and where its content landed when the record shows it (`content_landed_in`: "its commit is part of #N"; never "superseded" without `superseded_by`) |
| `closed_after_post` | closing the loop: a story that already had a post is closed without merging or withdrawn (every PR of the train closed unmerged, none open or merged): the day it closed. The one exception to one post per story: `post_id` is the first post's id plus `:closed`, with `first_post_id` | as for `closed_not_planned` |

**Triggers as of their day.** A trigger is read from the PR's history, not its state at the
window's end: a breaking declaration or a module proposal in a PR that was open on that day
still makes the first post when the PR is closed unmerged later, and its closing then gets the
`:closed` post (a merged PR is posted by its release instead). Who closed an item is the actor
of its **final** `closed` lifecycle event; the closing post uses the final closure of the
story. Closures folded into a weekly post stay in their own repository's weekly post.

**Breaking declared across commits.** Patches carry no line positions, so the declaration is
read per patch (items under an added `### Breaking Changes` heading) and across commits only
where it is unambiguous: a later patch that removes the "- None" placeholder of a Breaking
heading an earlier commit added, and adds items under no heading of its own, declares them —
only when every open placeholder of that file is under a Breaking heading. A merged story is
`breaking: declared` only when a declared breaking item's source PR is in **this** train.

**Closures that are not posted** fold into the weekly post as a count
(`closures_not_posted`), each with how the record shows it: `automation_pr` (the PR's author
is an automation account, `is_automation`), `duplicate_by_automation` (an issue whose
`closing.state_reason` is duplicate and whose `closing.by` is an automation account),
`quick_own_close` (`closing.by` — for a PR, the actor of its `closed` lifecycle event — is the
item's own author, within `CLOSED_QUICK_HOURS` (24) of `created_at`). Plumbing closures (a
story of the plumbing group or an `in_theme` train) never post and are already in the weekly
theme counts.

The earliest trigger of a story wins; a later event never makes a second post (except the
closing post above). Stories that are only in flight or stalled without one of these events get
no post (there is nothing for a reader to do yet). **Plumbing gets no daily post**: ONE weekly
post per ISO week ("what was engineered"), due on that week's Sunday and pending until the
view covers that day (never drafted from part of a week), covering every plumbing theme with a PR merged or opened that week, biggest first
(`must_state.themes`, by merged then opened), plus the closures not posted
(`closures_not_posted`): `post_id` `weekly:<repo>:<YYYY>-W<ww>`. The LinkedIn form may use its
full length; the X form names the one or two main things and links the repository or the main
pull request.

Every row carries a stable **`post_id`**: `story:<owner/repo>:<train id>` for a story,
`<kind>:<owner/repo>:<module>:<day>` for a module event, the weekly id above for plumbing. It
never changes between runs of the same record, so the ledger can prevent a second post.

The daily outline (`kind: daily_outline`) lists `posts` and `weekly` due that day, `held`
(stories waiting for their release, with the day they will post), `already_posted` (rows the
ledger excludes), the `channels` and the `limits`.

## The ledger

`--posted LEDGER.json` excludes what is already out. The ledger is one JSON file the posting
step keeps (append a row per published post; never rewrite a row):

```json
{"kind": "social_ledger",
 "posts": [{"post_id": "story:Azure/bicep-registry-modules:train-pr-7499",
            "day": "2026-10-18",
            "channels": ["x", "linkedin"],
            "urls": {"x": "https://…", "linkedin": "https://…"}}]}
```

`post_id` (required) is the outline's id; `day` the day it was posted; `channels` where it
went; `urls` per channel when known. A row without `post_id` makes the ledger unreadable
(exit 2). A post is out when its `post_id` is in the ledger, on any channel.

## The two forms

Both forms state the same facts; neither adds one.

- **X**: at most `X_LIMIT` (280) characters, counted as X counts them: every URL counts
  `X_LINK_WEIGHT` (23) characters whatever its length, a character in the Latin and general
  punctuation ranges counts 1, any other (most emoji, CJK) counts 2 (`newsletter.x_length`).
  One link, to the tag of the published version, else the pull request, else the module; a
  deprecation post that names a replacement carries a second link, to the replacement.
- **LinkedIn**: a short paragraph or three, at most `LINKEDIN_LIMIT` (1,200) characters (URLs
  at their full length), plain text, at most `LINKEDIN_HASHTAGS` (3) hashtags.
- **URLs** are read once (`_post_urls`): trailing sentence punctuation is not part of a link,
  for the length, the required links and the voice-pass facts alike.
- **Plain text** in both: no markdown links, bold, code spans or headings. A quote of record
  text that holds code spans is written without the backticks; the check matches it against
  the record with the record's backticks dropped.

**People.** The newsletter's rules apply: a `do_not_name` person is never named; unknown
affiliation is never "community"; no characterisation. A person is named by **display name
only** (the view's name). An **@handle** is written only when the people registry confirms a
handle for that channel, an optional registry field:

```json
"people": {"<github login>": {"social": {"x": {"handle": "…", "confirmed": true},
                                         "linkedin": {"handle": "…", "confirmed": true}}}}
```

Until the registry carries it, posts have no handles. A person without a display name is not
named (the post says what was done). Automation accounts are written as in the newsletter
("an automation account", "an automation account (a coding agent) working for Name").

## The check (`--check-post`)

A drafts file holds one block per post and channel:

```
<!-- post: <post_id> channel: x|linkedin day: YYYY-MM-DD -->
the text
<!-- /post -->
```

The whole file is accounted for (`file_errors`): a post marker outside a well-formed block
(malformed, unterminated, a stray closing marker), a duplicate (post_id, channel), a marker
inside a post, and any other text outside the blocks fail the file.

Per block it fails and lists: `post_unknown` / `day_mismatch` (not a row of that day's
outline); `too_long`, `too_many_hashtags`, `markdown`; `handles_not_confirmed`;
`must_state_missing` (the table above, plus a link to the story, a pull request, the module or
the tag — for `published`, the published version's tag; for a module event, its source or the
module; for a weekly post, in both forms, the repository's page or one of the week's pull
requests); `breaking_not_declared` ("breaking" in a post whose row declares none); and the
issue check's record rules on the text (run with the candidate's repository as a heading, so
every paragraph of the post, linked or not, is read in that repository's scope) (`quotes_not_in_view`, `items_not_in_view`,
`versions_not_in_view`, `numbers_not_in_view` — a count the row gives is allowed —,
`review_state_mismatch`, `pr_state_mismatch`, `release_status_mismatch`,
`superseded_not_in_view`, `do_not_name_named`, `affiliation_mismatch`,
`automation_as_person`, `dates_without_year`, `characterising_words`). Two record rules are
read for posts. **Every state, review and publication claim is judged against ONE projection
of the record as of the post's day** (`RecordAsOf`), never against the window's end:

- a PR's state is replayed from its recorded lifecycle (closed, reopened, merged, in time
  order; `merged_at` / `closed_at` when no event is recorded): not yet opened, open, merged or
  closed without merging at the end of that day (`pr_state_on`);
- its review state is `link.review_state` at the end of that day (reviews, comments and pushes
  up to then);
- a version is published when a tag of it is dated on or before the day; not published when it
  is not and the view's tags cover the day; **unknown** when the tags were not read or the day
  is past the view's coverage (`release_on`).

**Absence is proof only where coverage is complete and reaches the day**
(`absence_provable`): a negative — "not published", "no owner was added since", "still open",
a week's counts — needs the view's period to reach the day and, for tags, the tags read for the
whole period (no `windows_without_tags`). Everywhere else the projection says unknown and the
outline keeps the row pending: the merged-release hold (a gap in tag coverage inside the period
turns it into `merged_release_unknown`), the label-only orphan hold, and the weekly post (due on
the week's Sunday, pending until a view covers it; its `covers_from` / `covers_to` give the span
its counts cover when the week starts before the view). PR and review states past the coverage
are unknown.

The outline uses the same projection: a `merged_not_published` post due after the view's
coverage ends is `pending` (listed under `pending` in the daily outline, `post_pending` in the
check) until a view that covers its day settles it; a row's `must_state.prs` states each PR as
it stood on the post's day. And "community" in a sentence that names a person by display name
fails unless that person's affiliation is community. A do_not_name person's handle is never
allowed, even when the registry marks it confirmed (logins compared case-insensitively).

**The voice pass.** It may change wording and sentence order, the person (first person
plural, "we"), greeting and closing lines that state no fact, hashtags and emoji within the
limits (`VOICE_PASS_MAY_CHANGE`). It may not change a fact. The check is written for that: it
asks for facts, not wording, so it runs unchanged on the voiced text; with `--against` the
earlier drafts file, it also fails `facts_changed` when a post's URLs, versions, item numbers,
other numbers, quotes, dates, handles, module paths or the people it names (display names
resolved to their logins) differ from its earlier version (`post_facts`), and when a post or channel of the
earlier file is missing from the new one. A voice
pass that turns "not yet published" into "published" fails the record check even when no
listed fact changed.

## What the posting step will need

Per approved post: the `post_id`, the text per channel, the link (already in the text; X
counts it as 23 characters), and a scheduled time chosen by the owner. After publishing, it
appends the ledger row with the post URL per channel. The posting service and the schedule
are not part of this skill.

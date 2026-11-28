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
| `merged_not_published` | a landed story with no release tied within the hold: three days after its last merge (it is `held` until then) | the module, "not yet published" (and the declared version when the changelog has one) |
| `merged_no_release` | a landed story whose PRs change no version (documentation, metadata): its merge day | the module when there is one, "no version change" |
| `new_module_proposed` | an open PR whose own commits add a module's entry file (`new_module_proposed`, the tree's `proposed` lifecycle): the later of that commit's day and the PR's opening | the proposed module, that the PR is open, not published |
| `breaking_declared_open` | an open PR whose own commits add items under `### Breaking Changes` in a CHANGELOG (a stacked copy of another PR's commit does not count): the later of that commit's day and the PR's opening | "breaking", that the PR is open (not merged) |
| `owner_added` / `orphaned` / `deprecated` | a module event of `module_ownership` (one post per module and kind): the event's day | the module, what happened, the source link |

The earliest trigger of a story wins; a later event never makes a second post. Stories that
are only in flight, stalled or closed without one of these events get no post (there is
nothing for a reader to do yet). **Plumbing gets no daily post**: each plumbing theme with a PR
merged or opened in an ISO week gets one **weekly** item ("what was engineered"), due on that
week's Sunday (or the window's last day): `post_id` `weekly:<repo>:<theme>:<YYYY>-W<ww>`,
`must_state` the PRs merged and opened that week.

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
  One link, to the tag of the published version, else the pull request, else the module.
- **LinkedIn**: a short paragraph or three, at most `LINKEDIN_LIMIT` (1,200) characters (URLs
  at their full length), plain text, at most `LINKEDIN_HASHTAGS` (3) hashtags.
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

Per block it fails and lists: `post_unknown` / `day_mismatch` (not a row of that day's
outline); `too_long`, `too_many_hashtags`, `markdown`; `handles_not_confirmed`;
`must_state_missing` (the table above, plus a link to the story, a pull request, the module or
the tag); `breaking_not_declared` ("breaking" in a post whose row declares none); and the
issue check's record rules on the text (`quotes_not_in_view`, `items_not_in_view`,
`versions_not_in_view`, `numbers_not_in_view` — a count the row gives is allowed —,
`review_state_mismatch`, `pr_state_mismatch`, `release_status_mismatch`,
`superseded_not_in_view`, `do_not_name_named`, `affiliation_mismatch`,
`automation_as_person`, `dates_without_year`, `characterising_words`).

**The voice pass.** It may change wording and sentence order, the person (first person
plural, "we"), greeting and closing lines that state no fact, hashtags and emoji within the
limits (`VOICE_PASS_MAY_CHANGE`). It may not change a fact. The check is written for that: it
asks for facts, not wording, so it runs unchanged on the voiced text; with `--against` the
earlier drafts file, it also fails `facts_changed` when a post's URLs, versions, item numbers,
other numbers, quotes, dates or handles differ from its earlier version (`post_facts`). A voice
pass that turns "not yet published" into "published" fails the record check even when no
listed fact changed.

## What the posting step will need

Per approved post: the `post_id`, the text per channel, the link (already in the text; X
counts it as 23 characters), and a scheduled time chosen by the owner. After publishing, it
appends the ledger row with the post URL per channel. The posting service and the schedule
are not part of this skill.

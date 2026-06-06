# activity-overview — Phase 14: community-call transcript + `/activity` slash command

**Date:** 2026-06-06
**Status:** shipped — slice 1 (this PR): `transcript.py` + Community call highlights
section + `/activity` command.
**Depends on:** the agent-authored report (Phase 4b narrator protocol) — the call
summary is the model's judgment grounded in a bounded source, exactly like the
per-train narratives; the pipeline stays deterministic.

## Goal

Close out the original-P8 remainder:
1. **Community-call transcript handling.** Many of these projects hold a periodic
   community call whose transcript is rich context (decisions, asks, follow-ups).
   Let a digest fold that in — **user-provided local file, no network** — as a
   dedicated **"Community call highlights"** section and woven into the framing.
2. **`/activity` slash command.** A thin entrypoint so the skill triggers as
   `/activity <owner/repo|project> <from> <to> [options]`.

Both are **report-stage** concerns: the transcript is narrative input (like the
report prose), NOT a graph fact, so nothing is folded into the store and every
existing output stays byte-identical.

## Design

### `transcript.py` (pure, offline)
The one piece of deterministic code: normalize a raw transcript into clean prose
the narrator can read and cite, regardless of source format.
- **`normalize_transcript(text)`** → plain text. Content-detected (not
  extension-bound): if the text carries subtitle structure (`-->` cue timing
  and/or a `WEBVTT` header) it is treated as **VTT/SRT** and stripped of:
  - the `WEBVTT` header and `NOTE`/`STYLE`/`REGION` blocks,
  - cue-timing lines (anything containing `-->`) and VTT cue settings,
  - standalone numeric cue-index lines (SRT),
  - inline tags (`<v Speaker>`, `<00:00:00.000>`, `<c>…</c>`).
  Consecutive **duplicate** lines (rolling auto-captions) are collapsed, blank
  runs squeezed to one. Plain `.txt`/`.md` pass through (whitespace-trimmed).
  Deterministic; tolerant of empty/garbage input (→ empty/clean string).
- **CLI** `python3 transcript.py PATH` prints the normalized text to stdout (so the
  skill can `$(python3 transcript.py …)` it into context). Missing file → exit 2
  with a clear message; the run still degrades gracefully (skill skips the section).

### Report (`SKILL.md` + `report-template.md`)
- A new **"Community call highlights"** section (gated): present ONLY when a
  transcript was provided; otherwise omitted entirely (a first-class "no call this
  period" is fine but we simply drop the section to avoid noise).
- **Narrator protocol (reuse Phase 4b discipline):** read ONLY the normalized
  transcript text, summarize **topics / decisions / asks / follow-ups**, and **quote**
  the transcript for any specific claim (the transcript itself is the source —
  there are no PR/issue urls to cite, so verbatim quotes are the grounding). Do not
  attribute anything to the call that the transcript does not say. The summary is
  also woven into the executive summary / decision-trains framing where it adds
  context — clearly as call context, never as a code fact.

### `commands/activity.md` (slash command)
Flesh out the existing stub into the documented entrypoint:
`/activity <owner/repo|project> <from> <to> [--transcript PATH] [--series PATH]
[other SKILL options]`. It parses the args (asking only when a required one is
missing), then defers entirely to `SKILL.md` (gather → link → render → report) —
**no logic in the command**, so the skill stays the single source of truth.

## Slices (TDD)
1. **`transcript.py` + report section + slash command (this PR).**
   `normalize_transcript` (pure, fixture-tested: VTT, SRT, plain, inline-tags,
   dup-collapse, empty); the `transcript.py` CLI; the gated **Community call
   highlights** section + narrator protocol in `SKILL.md`/`report-template.md`; the
   `/activity` command wrapper. No pipeline change ⇒ goldens / characterization /
   `validate` byte-identical.

## Testing & verification
- **Offline TDD:** `test_transcript.py` drives `normalize_transcript` over crafted
  VTT/SRT/plain/markdown fixtures (header & NOTE stripping, cue-index & timing
  removal, inline-tag stripping, consecutive-duplicate collapse, empty input).
- **Vertical:** run `transcript.py` over a real `.vtt`/`.srt` sample and confirm the
  output is clean readable prose with no timing/markup; confirm a digest run with a
  transcript renders the highlights section and without one omits it.

## Not in scope
- Fetching transcripts from YouTube / any network source (user provides the file).
- Speaker diarization / attribution beyond whatever labels the transcript carries.
- Folding call content into the store (it is narrative input, not a graph fact).

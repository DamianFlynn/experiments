"""Phase 14: community-call transcript normalization. Pure, offline.

A digest can fold in a periodic community call's transcript as narrative context.
The transcript is a USER-PROVIDED local file (no network) in any of the common
formats (WebVTT, SRT, or plain .txt/.md). This module is the one deterministic
piece: it strips subtitle structure down to clean readable prose so the report
narrator can read and quote it. The summary itself is the model's judgment
(grounded in this text), like the rest of the report — nothing here touches the
store or the graph.
"""

import re
import sys

# A cue-timing line, in either VTT (00:00:01.000) or SRT (00:00:01,000) form;
# the `-->` arrow is the reliable marker, with optional VTT cue settings after.
_TIMING_RE = re.compile(r"-->")
# Inline tags: VTT voice/class spans (<v Bob>, <c.foo>, </c>) and inline
# timestamps (<00:00:00.000>).
_INLINE_TAG_RE = re.compile(r"<[^>]*>")
# Block keywords that introduce non-spoken VTT metadata blocks.
_META_BLOCK_RE = re.compile(r"^(NOTE|STYLE|REGION)(\s|$)")


def _looks_like_subtitles(text):
    """Content detection (not extension-bound): subtitle formats carry `-->` cue
    timing and/or a WEBVTT header. Plain .txt/.md have neither."""
    head = text.lstrip("﻿").lstrip()
    return head.startswith("WEBVTT") or _TIMING_RE.search(text) is not None


def normalize_transcript(text):
    """Normalize a raw transcript into clean prose. Subtitle formats (VTT/SRT) are
    stripped of headers, NOTE/STYLE/REGION blocks, cue-timing lines, standalone
    SRT cue-index numbers, and inline tags; consecutive duplicate lines (rolling
    auto-captions) and blank runs are collapsed. Plain text/markdown passes through
    whitespace-trimmed. Deterministic; tolerant of empty/garbage input.

    Heuristic tradeoffs (fine for a transcript *summary*, where the model
    paraphrases rather than counts verbatim): a plain-text line containing a literal
    `-->` is treated as cue timing and dropped; a standalone digit-only caption line
    is treated as an SRT index and dropped; and a line repeated on consecutive
    cues/lines is de-duplicated (so a genuine immediate repeat collapses to one)."""
    if not text:
        return ""
    text = text.lstrip("﻿")

    if not _looks_like_subtitles(text):
        # Plain .txt / .md: trim trailing whitespace per line, squeeze blank runs.
        return _squeeze("\n".join(ln.rstrip() for ln in text.splitlines()))

    out = []
    skip_block = False
    for raw in text.splitlines():
        line = raw.strip()
        # NOTE/STYLE/REGION blocks run until the next blank line.
        if skip_block:
            if not line:
                skip_block = False
            continue
        # The WEBVTT header line plus any header metadata (Kind:, Language:, …) and
        # NOTE/STYLE/REGION metadata all run as a block until the next blank line.
        if line.startswith("WEBVTT") or _META_BLOCK_RE.match(line):
            skip_block = True
            continue
        if not line:
            # blank lines in VTT/SRT are cue separators, not paragraph breaks —
            # drop them so consecutive caption lines join into continuous prose.
            continue
        if _TIMING_RE.search(line):           # cue timing (+ any cue settings)
            continue
        if line.isdigit():                    # SRT cue index
            continue
        cleaned = _INLINE_TAG_RE.sub("", line).strip()
        if cleaned:
            out.append(cleaned)
    return _squeeze("\n".join(out))


def _squeeze(text):
    """Collapse consecutive duplicate non-empty lines and runs of blank lines to a
    single separator; strip leading/trailing blanks. Deterministic."""
    result = []
    prev = None
    for line in text.split("\n"):
        if line == "" and (not result or result[-1] == ""):
            continue                          # squeeze blank runs / leading blank
        if line != "" and line == prev:
            continue                          # drop consecutive duplicate captions
        result.append(line)
        prev = line
    while result and result[-1] == "":
        result.pop()
    return "\n".join(result)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1:
        sys.stderr.write("usage: transcript.py PATH\n")
        raise SystemExit(2)
    try:
        with open(argv[0], encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError as err:
        sys.stderr.write(f"error: cannot read transcript {argv[0]!r}: {err}\n")
        raise SystemExit(2)
    sys.stdout.write(normalize_transcript(text))
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()

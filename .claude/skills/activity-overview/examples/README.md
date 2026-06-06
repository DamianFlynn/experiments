# Examples

Worked example inputs and a second formatter, so the skill is fully rounded. The
**full end-to-end worked run** (a real multi-repo AVM-Terraform project: gather →
store → validate → digest → render) lives in [`../samples/`](../samples/) — start
there to see the structured product (`digest_view.json`) and a rendered digest.
The committed test fixtures in [`../fixtures/`](../fixtures/) are the byte-stable
golden bundles the suite runs against.

## Inputs you provide

| File | What it is | Used by |
|------|-----------|---------|
| [`manifest.json`](manifest.json) | A small multi-repo project manifest (project, window, repos[]). | `python3 gather.py --manifest examples/manifest.json --store …` (or generate one with `manifest_from_index.py`). |
| [`community-call.vtt`](community-call.vtt) | A community-call transcript (WebVTT) with header metadata, `<v>` speaker tags, cue timings, and a rolling-caption duplicate — exercises every strip path. | `python3 transcript.py examples/community-call.vtt` → the **Community call highlights** report section. |
| [`series.json`](series.json) | A two-installment series index (the shape `series.py` appends). | `python3 link.py <view> --series examples/series.json` → the **Since last installment** section. |

For per-project config (owner/repo, branches, clone dir, an optional `transcript`
path), see [`../projects.example.json`](../projects.example.json).

## Output → other formatters

The pipeline's product is the **structured, fully-sourced view** (see
[`../BUNDLE.md`](../BUNDLE.md) → *Output contract for downstream formatters*). The
Markdown report is one renderer; the view is a stable, versioned contract any
formatter can consume:

- [`../samples/build_report.py`](../samples/build_report.py) — the structural
  Markdown skeleton renderer (deterministic, no narrative).
- [`formatters/shipped_changelog.py`](formatters/shipped_changelog.py) — a second,
  minimal formatter (a "shipped" changelog grouped by repo), to show the same view
  drives different outputs:
  ```bash
  python3 examples/formatters/shipped_changelog.py samples/digest_view.json
  ```
  It checks the view carries the contract keys and fails loudly otherwise.

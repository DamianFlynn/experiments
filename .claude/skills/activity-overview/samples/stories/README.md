# Sample stories

Two stories written with the story brief (`reference/report-sections.md`, "Stories")
from the slices of September 2026 on `Azure/bicep-registry-modules`, each passing
`python3 link.py <train>.slice.json --check-story <train>.story.md`:

- [`train-pr-7299.story.md`](train-pr-7299.story.md) — the Function App pattern
  module (shipped).
- [`train-issue-6167.story.md`](train-issue-6167.story.md) — disabling weak TLS
  cipher suites in the API Management service module, held up by shared CI changes
  (in flight).

How the slices were made: the stored September window (a store gathered before
review-comment context and `pr_activity` were kept) with its `pr_history`
recomputed by `gather.collect_pr_history` over a clone of the repository, so the
PR commits carry their narrative `evidence` (interface deltas, code comments,
documentation prose) and the threads a time-based `response`; `module_notes` were
read from the same clone. The slices themselves (80 KB and 350 KB) are not kept
here. Check results and review-thread resolutions were not read for that store,
and the slices say so (`gaps`); a fresh gather adds both.

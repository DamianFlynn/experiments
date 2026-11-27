# Final-gate validation — live runs on real targets (November 2026)

Record of the final gate (#97): the skill was run against real public repositories, the
resulting stores were verified by hand against a clone and against github.com, and the
defects found were fixed. This document says what was checked, what was found, and what
is still unverified.

## How it was run

- Gathers ran in the repository's CI workflow (`workflow_dispatch` with `owner` / `repo` /
  `from` / `to`), using the workflow's built-in token. Each run uploads the store and the
  enriched bundle view as the `journey-graph` artifact.
- Each store was downloaded and checked locally: `validate.py`, a comparison of every
  stored commit with a real clone of the target (`git log` over the window), and a sample
  of PRs, issues and releases compared with their github.com pages.
- A tamper test was run on a copy of one live store.

## Targets

| Target | Window | PRs / issues / commits | Trains | Trust check | Commits vs clone | Sampled items vs github.com |
|---|---|---|---|---|---|---|
| Azure/bicep-registry-modules | 2026-11-12 → 2026-11-26 | 115 / 256 / 28 | 111 | OK | 26 of 26 match (2 newer commits landed after the gather) | 8 PRs, 5 issues: all match |
| Azure/bicep | 2026-10-01 → 2026-10-31 | 132 / 1040 / 75 | 121 | OK | 75 of 75 match | 8 PRs, 2 issues, 1 release: all match |
| Azure-Samples/ai-hub-gateway-solution-accelerator | 2026-10-01 → 2026-10-31 | 7 / 7 / 17 | 6 | OK | 17 of 17 match (merge-commit history) | 7 PRs, 3 issues: all match |
| Azure/bicep-registry-modules | 2026-10-01 → 2026-10-31 | 149 / 248 / 65 | 148 | OK | 65 of 65 match | covered by the November sample |
| AVM Terraform constellation (2 repos, CI slice) | pinned week, March 2026 | — | — | OK (project) | not compared | not sampled |

"Match" for a commit means sha, UTC date, subject, PR number in the subject, the stored
commit → PR edge, and the author resolved to a GitHub login. "Match" for a PR means
author, state, merge date, merger, base branch, reviewers and closing issues.

## Defects found by the live runs, and fixed

1. **Closing references read from HTML comments** (#115). The PR template of
   Azure/bicep-registry-modules contains `<!-- … Fixes #123 … -->`. 55 of 114 PRs were
   recorded as closing issue 123 (in fact an unrelated PR closed in 2022) and 54 PRs were
   joined into one train. After the fix: 111 trains, the largest holds 4 PRs, and no
   reference points at an item the store does not hold.
2. **Guessed transitive Bicep dependencies** (#115). They were derived by matching nested
   deployment titles against folder names; most of the 20 in the live store were wrong
   (for example a Key Vault access-policies deployment recorded as a dependency on
   `.github/policies`). Bicep now records direct dependencies only, read from source.
3. **A module's own files were not read** (#116). The landing zone's root `main.bicep`
   only calls `./resources.bicep`; that was stored as the area depending on itself and
   the 40 module calls inside were not seen. A module's references now include those of
   every file of the same area it reaches. On Azure/bicep-registry-modules this finds 815
   module-to-module pairs instead of 601, with none lost.
4. **Git isolation read the system config** (#114). Found by CI, not by a live store: the
   code that isolates git from user configuration still read the system file when it was
   switched off.

5. **A month gathered late showed today's state** (#117). For October gathered on
   26 November the `in_flight` bucket held 13 items although 66 trains were in flight, and
   a PR open at the month's end but merged since was not gathered at all. A report view
   now shows each PR and issue as of its window's end (state, comments, reviews, events,
   last activity), and the gather keeps PRs that were open at the window's end. On the
   real store the October `in_flight` bucket holds 55 items. Title, labels and milestone
   are still today's.
6. **A reminder bot hid stalled work** (#117). A bot had commented on every open PR two
   days before the gather, so PRs idle since 2025 counted as active. Comments and reviews
   by bot accounts, and the update they cause, are no longer activity. On the real
   November store 41 of 51 open trains are stalled (18 before); on October as of its end,
   53 of 66.

7. **Deepening a shallow clone lost a month's commits** (#97). Found while seeding a
   second real repository for the people registry: on
   Azure-Samples/ai-hub-gateway-solution-accelerator (merge commits) August stored 0 of
   31 commits. `git fetch --deepen` counts from every shallow boundary commit, and one
   on a merged branch turned a September mainline commit into the boundary. Gather now
   deepens by an earlier date only and checks that no in-window commit is lost. After
   the fix August, September and October store 31, 8 and 17 commits, exactly the full
   clone's; Azure/bicep-registry-modules (linear, never deepened) 23, 69 and 65 as before.

A guard was added to the live CI job: it fails when one train holds more than a third of
the window's PRs.

## Tamper test

Eleven single changes were made, each to a fresh copy of the live
Azure/bicep-registry-modules store, and `validate.py` was run on each.

| Change | Caught by |
|---|---|
| Delete a `closes` edge | `edge_set`, `idempotency` |
| Add a `reviewed` edge for someone who did not review | `edge_set`, `idempotency` |
| Change a PR's author | `participant_completeness`, `edge_set`, `no_drift`, `idempotency` |
| Insert an invented PR | `no_fabrication` |
| Delete a person | `referential_integrity`, `participant_completeness`, `no_drift`, `idempotency` |
| Delete a `depends_on` edge | `edge_set`, `idempotency` |
| Delete a `touches` edge | `edge_set`, `idempotency` |
| Change a digit in a stored fact | `meta_integrity` |
| Delete the store's state marker | `meta_integrity` |
| Rewrite an artifact's lifecycle author | `no_drift` |
| Change a commit record's date | **not caught** |

The last one is the documented limit of a store-only audit: a record edited consistently
looks like a different, valid record. `validate.py --bundle` with the gather's own bundle
catches it for that window.

## Observations that are not defects

- **Pushes to an open PR are invisible in a past month's view.** The as-of view measures
  activity from dated comments, reviews and events; a push leaves no dated record, so a
  PR that was only pushed to near the month's end can read as idle.
- **One person can appear twice.** A commit whose author email GitHub links to no account
  is kept under the git name, separate from the login (seen once in the landing zone).
  The skill does not merge identities on a guess.
- **"Rejected" describes the PR, not the need.** In the landing zone a PR closed unmerged
  gives a rejected train although its issue was closed as completed by another route.
- **The default single-repo gather covers changed areas only.** Deployments untouched in
  the window have no dependency data unless the gather uses `--structural-iac`.
- **Commits reached through merge commits are included.** A repository that merges with
  merge commits stores every commit on the mainline's history in the window, not only
  the first-parent line.

## Not verified

- AVM Terraform beyond the two-repository CI slice, and any multi-repository manifest
  gather of real size.
- Private repositories, GitHub Enterprise, and gathers using a credential helper.
- A three-month series with month-to-month comparison and forecast follow-up (an
  independent validation run on a separate machine covers this).
- The rendered diagrams of the live stores were written but not compiled in CI.
- The fixes in #114–#116 had automated review and live runs, not a second full review.

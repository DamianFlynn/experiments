# Phase 9 — the multi-repo project (the constellation) — design

**Status: PROPOSED (rev 1).** Detailed design for **Phase 9** of the
journey-graph substrate. Phases 7–8d made a *single* repo's activity a
trustworthy, self-sourcing, edge-honest graph. **Phase 9 lifts the unit of
analysis from one repo to a curated *project that spans several repos*** — folded
into one store under one logical name, with decision trains that cross repo
boundaries and Infrastructure-as-Code dependencies that resolve *between* member
repos.

The lead case is **Azure Verified Modules — Terraform (AVM-TF)**: unlike
AVM-Bicep (one repo, many module directories), each AVM-TF module is its **own
GitHub repository** (`Azure/terraform-<provider>-<name>`), indexed centrally
(`azure.github.io/Azure-Verified-Modules/indexes/terraform/`) and depending on
one another through the Terraform Registry. A digest "for AVM-TF storage" is
therefore inherently a *multi-repo* question.

This is the rev that makes the store answerable about a **solution**, not just a
repo.

---

## Why (the problem this closes)

Everything below the reader is already multi-repo-shaped — this phase finishes
wiring a capability the substrate was *designed* for but no producer exercises:

- **Identity already namespaces by repo.** `qualify_id(project, repo, local)` →
  `{project}/{repo}#{local}`; people are *project*-scoped
  (`{project}#person-{login}`) precisely so a contributor aggregates across a
  project's repos (STORE.md, *Identity*).
- **The store already range-queries a set of repos.**
  `range_query(conn, project, repos, …)` takes a **list** (`repo IN (…)`).
- **`dead_refs`/`parse_id` already recover `project` from an id's scope.**

What is missing is end-to-end: a producer that folds **N members under one
project**, ref-resolution that **crosses** member repos, IaC dependency
resolution that **links** member repos, and readers/validate that **aggregate
the member set** rather than assuming one repo.

Four concrete gaps:

1. **No multi-repo producer.** `gather` takes one `--owner/--repo`, sets
   `project = owner`, `repo = <name>`, folds one repo. There is no way to declare
   "these repos are one project."
2. **Refs are same-repo only.** `parse_closing_refs` matches bare `#N` and
   qualifies it against the *current* repo; `parse_timeline_crossrefs` reads the
   cross-ref event's `issue.number` but **drops its source repository**. A PR in
   module repo A that `Closes Azure/Azure-Verified-Modules#1234` (the central
   tracking issue) never links.
3. **Readers assume one repo.** `extract.extract(conn, project, repo, …)` takes a
   single `repo` and calls `range_query(…, [repo], …)`. A project-wide digest
   needs the whole member set in one window.
4. **IaC deps stop at the repo edge.** `build_terraform_edges` already isolates a
   registry source (`to=None, resolved=False, ref=<src>, version=<pin>`,
   gather.py:1077) but cannot point it at the *member repo that publishes that
   module*. The cross-repo blast radius — the whole point of a module
   constellation — is invisible.

---

## Decisions (brainstormed, locked)

| # | Question | Decision |
|---|---|---|
| 1 | Unit of analysis | A **project spanning a curated set of repos** (AVM-TF as lead case). |
| 2 | Project identity | **Logical project name** (user-chosen) + members identified as **`owner/repo`**. The `repo` column holds `owner/repo`; people aggregate under the logical name. |
| 3 | Member set source | A **manifest file** (JSON, stdlib). The AVM index can *generate* one later — a separable concern, not a Phase 9 dependency. |
| 4 | Cross-repo links | **Qualified body refs** (`owner/repo#N`, full `github.com/.../issues|pull/N` URLs) **+ timeline cross-refs** (now repo-aware). Member→member → spine edge; member→non-member → honest out-of-project record, not a fetchable gap. |
| 5 | Terraform deps | **Resolve cross-repo `depends_on`** between members + render a project-wide blast radius (not deferred). |
| 6 | Source→member resolution | **Manifest registry path (exact) + HashiCorp naming convention fallback** (`namespace/name/provider` ↔ `namespace/terraform-provider-name`). |

---

## What changes (and what deliberately does not)

The single-repo `--owner/--repo` path is **unchanged** — it keeps
`project = owner`, `repo = <name>` (no slash), so every existing golden/
characterization gate holds byte-for-byte. Multi-repo is an **additive** path
(`--manifest`) that sets `project = <logical>`, `repo = owner/repo`. The slash in
`repo` is benign: every store join is on the *full* string (`range_query`'s
`repo IN`, `repo_nodes`' `repo=?`, the `clone_sha:{project}/{repo}` key), the
project↔repo split is `scope.partition("/")` / `scope.split("/", 1)[0]` (first
slash only), and `parse_id` splits `local` off the **last** `#`. So
`avm-tf/Azure/terraform-azurerm-avm-res-storage#pr-42` parses to
`project=avm-tf`, `repo=Azure/terraform-azurerm-avm-res-storage`, `local=pr-42`
with **no code change to the identity helpers**.

### The manifest

```json
{
  "project": "avm-tf-storage",
  "window": { "from": "2026-03-01", "to": "2026-03-31" },
  "repos": [
    { "owner": "Azure", "repo": "terraform-azurerm-avm-res-storage-storageaccount",
      "registry": "Azure/avm-res-storage-storageaccount/azurerm" },
    { "owner": "Azure", "repo": "terraform-azurerm-avm-res-keyvault-vault" }
  ]
}
```

- `project` — the logical group name (the digest's scope, decoupled from any
  single org/owner).
- `repos[]` — the **explicit** member set; each `{owner, repo}` is required,
  `registry` is **optional** (exact-match override for §S4; absent → convention
  fallback).
- `window` — the gather window for all members (a per-member override is
  out-of-scope for rev 1; one window keeps roll-up reasoning unchanged).

JSON (not YAML) to stay stdlib-only, consistent with the rest of the skill.

---

## Slices

### S1 — manifest + multi-repo gather (identity)

- `gather.py` grows `--manifest PATH` as a mutually-exclusive alternative to
  `--owner/--repo`. With a manifest, gather **loops** the members, running the
  existing acquire→assemble path once per member, then `fold_bundle`s each into
  the **same** store under `project = manifest.project`, `repo = "{owner}/{repo}"`.
- `record_window`/`set_clone_sha`/`get_windows` already key on `(project, repo)`,
  so the gathered-windows ledger and per-repo clone pins work unchanged across
  members.
- **Idempotency / roll-up unchanged.** Re-folding any member over an overlapping
  window is the existing identity-keyed no-op; a project roll-up is a single
  wider `range_query` over the member set (STORE.md, *Roll-up/resume*).
- **Gate:** a manifest of two fixture repos folds into one store; `validate.py`
  passes; folding the same two repos *standalone* (two single-repo stores) yields
  byte-identical per-repo nodes modulo the `project` namespace.

### S2 — cross-repo reference resolution

- **Qualified body refs.** Add a parser that captures, alongside bare `#N`:
  `owner/repo#N` and full `https://github.com/owner/repo/(issues|pull)/N` URLs.
  `normalize_pr.closes` (and the issue cross-refs) become **qualified targets**
  `(owner, repo, number)` rather than bare numbers; a bare `#N` qualifies to the
  *current* member (today's behaviour preserved).
- **Repo-aware timeline cross-refs.** `parse_timeline_crossrefs` already reads
  `ev.source.issue` — extend it to also read
  `ev.source.issue.repository.full_name` so a cross-ref carries its **source
  repo**.
- **Edge emission in `fold_bundle`.** For each qualified target:
  - target ∈ members → emit the spine edge (`closes`/`cross_ref`) to
    `qualify_id(project, "{owner}/{repo}", "issue-N"|"pr-N")` — a **cross-repo
    spine edge**. `traverse_spine` already walks undirected across repos, so a
    train spanning member repos forms with **zero traversal change**.
  - target ∉ members → record on the node's `data` as an `external_refs` entry
    (visible, honest), **not** a spine edge — so 8d completion never reports a
    fetchable gap for a deliberately out-of-scope repo. (Bare `#N` to the current
    member is always in-project.)
- **8d honesty.** No new gap reason needed: in-project cross-repo anchors use the
  existing `outside_window`/`not_gathered`/`unreachable`/`budget` contract;
  out-of-project refs are `external_refs`, outside the train's edge contract by
  construction.
- **Gate:** a fixture PR in member A `Closes B#7`; after fold, the train rooted
  at `B#issue-7` reaches `A#pr-N` across the repo boundary; an `Other/repo#9` ref
  lands in `external_refs` and never appears as a gap.

### S3 — readers + validate aggregate the member set

- `extract.extract(conn, project, repo, …)` → accept **`repos`** (a list);
  internally `range_query(conn, project, repos, …)`. A single-repo caller passes
  `[repo]` (back-compatible shim retained so existing tests are untouched).
- Project/member **discovery from the store**: a helper returns the distinct
  member repos for a project from `gathered_windows` (and/or `SELECT DISTINCT
  repo`), so a reader needs only the store + project name.
- `spotlight` queries are already `project`-scoped; point their range scans at
  the full member set. People aggregation is **already** project-wide
  (`derive.enumerate_participants` over the bundle; person nodes carry repo
  sentinel `"*"`), and **CODEOWNERS-derived `owns` edges fold per member**, so
  module ownership aggregates across the constellation for free (matches AVM's
  per-repo CODEOWNERS + Azure-org team model).
- `validate.py` `no_drift`/`idempotency` self-source over the project's full
  window across all members (it already self-sources via `extract`).
- **Gate:** a two-member store renders one digest whose Shipped/Trains/Ownership
  sections span both repos; `validate.py` is green; a contributor active in both
  members appears as **one** person with merged module/area attribution.

### S4 — cross-repo Terraform `depends_on`

- Extend `build_terraform_edges.resolve()` (gather.py:1069): when a source is a
  **registry** source (today → `to=None, resolved=False`), attempt
  **member resolution**:
  1. **Exact** — a member whose manifest `registry` equals the source.
  2. **Convention fallback** — parse `namespace/name/provider` → expected repo
     `namespace/terraform-provider-name`; match against the member set
     (the HashiCorp registry publishing rule, so this is general, not
     AVM-special).
  - On match → `to` = the **target member's root module area**, qualified
    cross-repo (`qualify_id(project, "{owner}/{repo}", <root-area>)`),
    `resolved=True`; `transitive`/`version`/`ref` as today. No match → unchanged
    (`to=None`, external).
- `fold_bundle` emits these as `depends_on` (area→area) edges whose `dst` lives
  in another member repo (the edge `data` keeps `version`/`transitive`/`ref`).
  When the target member is in the same store the edge resolves; if it was not
  gathered the dst is a `missing` structure node (honest, like any spine miss).
- **Render + spotlight.** `render`'s existing `module_graph` (Phase 3c) extends
  to span members → a **project-wide module dependency / blast-radius** diagram.
  `spotlight` gains a **reverse-dependency (blast-radius) query**: "if member X
  changes, which members depend on it" (follow `depends_on` edges inbound across
  the project).
- **Gate:** member A's `main.tf` has `module "kv" { source =
  "Azure/avm-res-keyvault-vault/azurerm" }`; with member B
  (`terraform-azurerm-avm-res-keyvault-vault`) in the manifest, fold emits a
  cross-repo `depends_on` A-area→B-area (resolved via convention with no
  `registry` field, and via exact match when present); the blast-radius query
  from B returns A.

### S5 — real-data trust gate + docs

- Prove on a **small real AVM-TF constellation** (2–3 module repos that depend on
  one another) over a fixed window: cross-repo trains form, cross-repo
  `depends_on` resolves, `validate.py` is green on the multi-repo store. This is
  Phase 9's acceptance check (the live equivalent of 8d's #14 verification).
- Update `SKILL.md` (a `--manifest` procedure + multi-repo report sections),
  `STORE.md` (multi-repo identity already documented — add the manifest producer,
  cross-repo edges, and `external_refs`), and `REFERENCE.md`.

---

## Non-goals (rev 1)

- **Auto-discovery from the AVM index.** The index *can* generate a manifest, but
  that resolver is a separable producer, not a Phase 9 dependency (the manifest
  is the contract; how it is authored is open).
- **Per-member windows / per-member clone pins beyond today's keys.** One project
  window; existing `(project, repo)` clone pins suffice.
- **Private/alternate Terraform registries** beyond the manifest `registry`
  override + the public naming convention.
- **`blocks`/`in_iteration` edges** — still unsourced (STORE.md), untouched here.

---

## Risk / compatibility notes

- **Golden gates.** The single-repo path is byte-stable because multi-repo is a
  separate code path keyed only by `--manifest`; the identity helpers, traversal,
  and dedup are unchanged.
- **`repo` with a slash.** Audited safe (see *What changes*) — all joins use full
  strings; all splits are first-`/` (scope) or last-`#` (local).
- **Cross-repo edge to an ungathered member.** Surfaces as an honest `missing`
  spine/structure node (8d contract), never a silent hole.
- **Convention false positives.** A registry source whose convention-derived repo
  coincidentally matches a member would mis-resolve; the manifest `registry`
  exact match (preferred) eliminates this for any member that declares it.

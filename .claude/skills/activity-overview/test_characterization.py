"""Characterization gate for slice 7b-2 — freeze the CURRENT end-to-end enriched
output as committed golden-master snapshots.

WHY THIS EXISTS. The next slice (7b-2 proper) will refactor the pipeline so that
`extract` materializes `artifacts[]`/`people[]` from the store and `link.enrich`
SHRINKS (stops deriving them). That breaks the *symmetric* enrich-equivalence
gate in test_extract.py (which compares enrich(extract) against enrich(golden)).
So we first freeze today's correct end-to-end output as an ORACLE: the refactor
must then reproduce these snapshots byte-for-byte, proving "store-derived ==
link-derived" with no drift.

Right now this passes trivially (nothing has changed) — that is the point: it
LOCKS current behavior. The fixtures `fixtures/char_<name>.json` were captured by
running the current pipeline `fold -> extract -> link.enrich` and applying the
normalization below; capture was run twice and confirmed byte-identical, so the
snapshots carry no nondeterministic data.

NORMALIZATION RULE (Agent F: reuse this verbatim). Drop only the volatile meta
keys (`_VOLATILE_META`: generated_at; schema_version until issue #107, see
below). Issue #75 made extract
stamp `period` and `ref_date` into the view's meta, so those two are no longer
dropped: the snapshots were re-captured once to add them (meta only; no other
key changed). Issue #84 made extract stamp `base_branch` and `branches`; the
golden bundles carry no `base_branch`, so their views report it unknown
(`null`, `[]`) and the snapshots were re-captured once more to add those two
meta keys (no other key changed). Issue #100 gave `timeline` and
`feature_deltas` a total sort key that no longer reads the insertion order of
`artifacts`; char_bundle_p3.json was re-captured once for it, by a script that
refused to write unless the only difference was list order. What moved: in
`timeline`, the two code events of commit c1 (2026-05-03) now run
docs/firewall.md before examples/basic/main.bicep (artifact id order), and the
rename in commit c3 (2026-05-18) now lists the add of
examples/advanced/main.bicep before the remove of examples/basic/main.bicep;
`feature_deltas` are now in event-time order, then artifact id (they were
grouped by artifact in insertion order). No other key changed, and the other
four snapshots did not change. Issue #93 (train membership) re-captured
them once more: every train gained `issues` and `pr_outcomes`, and in
`char_bundle_p2.json` the open PR #44, which only cross-references issue #18
(no closing keyword), is now `train-pr-44` instead of `train-issue-18` — its
bucket / forecast refs name `train-pr-44`, issue #18's refs carry no train
(scores unchanged); no other key changed. Issue #107 made every view carry
the bundle/view contract version (`meta.schema_version` =
extract.VIEW_CONTRACT_VERSION, 3) and took it off the volatile list, so the
version is pinned: all five snapshots were re-captured once to add that one
meta key, by a script that refused to write unless it was the only
difference. Issue #112 (train effort flags) re-captured four of them once
more, by a script that refused to write unless every difference was inside
`trains[].effort`: every train's effort gained `idle_days`, `last_activity_at`
and `slow_to_land`, and no other key changed — `stalled` kept its value on
every train (no golden train was stalled under either rule), and so did
`people[].stale_owned`. Values: `false` / `null` / `null` everywhere except the
in-flight `train-pr-44` of char_bundle_p2.json, whose PR was last updated
2026-05-25T08:00:00Z: `idle_days` 6 at the reference date 2026-05-31,
`last_activity_at` "2026-05-25T08:00:00Z", not stalled. char_bundle_p3c.json
has no trains and did not change. The #97 final gate (validation finding
14: a commit that lands the person's own merged PR does not score again in
the hall) re-captured char_bundle_p3b.json once, by a script that refused to
write unless these were the only differences: `people.alice` and alice's
`halls.fame` entry gained `own_pr_commits: 1` (commit c1 lands her merged
PR #42) and that entry's `score` went from 3 to 2; the other four snapshots
did not change. The story layer (#97: the discussion signal per train)
re-captured char_bundle_p3.json once, by a script that refused to write unless
every difference was a story-layer key added to a train or the view: its one
train, train-issue-17, gained `discussion` = {comments 1, review_comments 1,
participants 2, reviews all 0, review_rounds 0, reopens 0, text_chars 59,
in_window 2, first_response_days null (the PR has no created_at),
longest_silence {days 0.0, from 2026-05-10T10:00:00Z, to 2026-05-10T11:00:00Z,
resumed_at …/pull/42#issuecomment-8001}, change_requests 0, bot_comments 0}; it
qualifies for no story (2 turns, fewer than 3), so no `story*` key and no
top-level `stories`; the other four snapshots have no human conversation and
did not change. Unlike test_extract, this gate
does NOT drop empty containers: we want an EXACT end-to-end snapshot, so a
present-but-empty `people: []` is part of the locked shape. The snapshot file is
canonical JSON: json.dumps(obj, sort_keys=True, indent=2).
"""

import copy
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
import extract  # noqa: E402
import gather  # noqa: E402
import graphstore  # noqa: E402
import link  # noqa: E402
from _quiet import quiet  # noqa: E402

FIX = os.path.join(os.path.dirname(__file__), "fixtures")

# Volatile meta keys, dropped before comparison. `ref_date` and `period` were
# dropped here too until issue #75; extract now stamps them (the view's window
# and reference date), so the snapshots pin them as part of the view contract.
# `schema_version` was dropped until issue #107; it is now the view contract
# version (extract.VIEW_CONTRACT_VERSION) and pinned too.
_VOLATILE_META = ("generated_at",)


def _load(name):
    with open(os.path.join(FIX, name)) as fh:
        return json.load(fh)


def normalize(bundle):
    """Canonicalize an enriched bundle for snapshot comparison: drop volatile
    meta keys only. Empty containers are KEPT (exact end-to-end snapshot)."""
    b = copy.deepcopy(bundle)
    meta = b.get("meta")
    if isinstance(meta, dict):
        for k in _VOLATILE_META:
            meta.pop(k, None)
    return b


def _canonical(obj):
    return json.dumps(obj, sort_keys=True, indent=2)


def _pipeline(golden_name):
    """fold -> extract -> link.enrich -> normalize: the current end-to-end output."""
    golden = _load(golden_name)
    conn = graphstore.open_store(":memory:")
    graphstore.init_schema(conn)
    gather.fold_bundle(conn, copy.deepcopy(golden))
    meta = golden["meta"]
    extracted = extract.extract(
        conn, meta["owner"], meta["repo"], meta["from"], meta["to"]
    )
    return normalize(link.enrich(copy.deepcopy(extracted)))


class CharacterizationGate(unittest.TestCase):
    """For each golden: assert the current enriched output reproduces the
    committed char_<name>.json snapshot exactly. The refactor in 7b-2 must keep
    this green."""

    def setUp(self):
        quiet(self)

    def _gate(self, golden_name):
        got = _pipeline(golden_name)
        snap_name = "char_" + golden_name
        want = _load(snap_name)
        if got != want:
            diffs = []
            for k in sorted(set(got) | set(want)):
                if got.get(k) != want.get(k):
                    diffs.append(k)
            first = diffs[0] if diffs else ""
            self.fail(
                "current output != {} — differing keys: {}\n"
                "--- got[{}] ---\n{}\n--- want[{}] ---\n{}".format(
                    snap_name, diffs,
                    first, _canonical(got.get(first) if diffs else None),
                    first, _canonical(want.get(first) if diffs else None),
                )
            )

    def test_bundle_sample_char(self):
        self._gate("bundle_sample.json")

    def test_bundle_p2_char(self):
        self._gate("bundle_p2.json")

    def test_bundle_p3_char(self):
        self._gate("bundle_p3.json")

    def test_bundle_p3b_char(self):
        self._gate("bundle_p3b.json")

    def test_bundle_p3c_char(self):
        self._gate("bundle_p3c.json")


class CharacterizationTeeth(unittest.TestCase):
    """Guard against a vacuous oracle: the snapshots that 7b-2 will move data
    *into* must actually carry that data, else reproduction proves nothing."""

    def test_p3b_carries_people_and_modules(self):
        snap = _load("char_bundle_p3b.json")
        self.assertTrue(snap.get("people"),
                        "char_bundle_p3b must carry non-empty people")
        self.assertTrue(snap.get("modules"),
                        "char_bundle_p3b must carry non-empty modules")

    def test_p3_carries_artifacts(self):
        snap = _load("char_bundle_p3.json")
        self.assertTrue(snap.get("artifacts"),
                        "char_bundle_p3 must carry non-empty artifacts")

    def test_snapshots_carry_derived_keys(self):
        # every snapshot must at least *reserve* the keys 7b-2 relocates.
        for name in ("char_bundle_sample.json", "char_bundle_p2.json",
                     "char_bundle_p3.json", "char_bundle_p3b.json",
                     "char_bundle_p3c.json"):
            snap = _load(name)
            for key in ("artifacts", "people", "modules"):
                self.assertIn(key, snap, "{} missing {}".format(name, key))


if __name__ == "__main__":
    unittest.main()

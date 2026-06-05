"""Phase 8d — the train-completion orchestrator.

ONE home for completion policy: given a train's reached set and its `missing`
spine refs, decide which to fill, follow the causal spine TRANSITIVELY to the
query-window bound, and return a completed reached set plus an honest gap list.

Pure policy: reads the store via `graphstore`, drives the INJECTED `backfill`
seam (so the offline suite makes zero network calls), never imports a network
library, never writes (the write is gather.backfill's). gather stays the only
writer/network.
"""
import graphstore


def _in_window(node, window):
    """A node is in-window if there is no window, or its ts falls in [from, to].
    A node with no ts is treated as out-of-window (cannot prove it's inside).
    Either bound may be None (a one-sided window)."""
    if window is None:
        return True
    ts = node.get("ts")
    if ts is None:
        return False
    frm, to = window
    if frm is not None and ts < frm:
        return False
    if to is not None and ts > to:
        return False
    return True


def _spine_neighbors(conn, qid, edge_types):
    """Present neighbor nodes of `qid` over the spine allowlist, both directions.
    Used to decide whether a missing id's REFERRER is in-window."""
    nbr_ids = set()
    for e in graphstore.get_edges(conn, qid, direction="out", edge_types=edge_types):
        nbr_ids.add(e["dst_id"])
    for e in graphstore.get_edges(conn, qid, direction="in", edge_types=edge_types):
        nbr_ids.add(e["src_id"])
    out = []
    for nid in nbr_ids:
        n = graphstore.get_node(conn, nid)
        if n is not None:
            out.append(n)
    return out


def complete_train(conn, reached, missing, *, window=None, backfill=None,
                   budget=50, edge_types=graphstore.SPINE_EDGE_TYPES, warn=None):
    """Transitively complete a train. Returns
    {"reached": set, "gaps": [{"id", "reason"}], "fetched": int}.

    - `backfill=None` (offline): every non-dead missing id becomes a
      `not_gathered` gap; phantoms already tombstoned are pruned. No fetch.
    - `window=None`: chase the spine to its connected-component closure.
    - reasons: not_gathered | outside_window | unreachable | budget.
    Determinism: missing processed in sorted id order; gaps sorted by id.
    """
    reached = set(reached)
    gaps = {}            # id -> reason (last write wins; all reasons terminal)
    fetched = 0

    def referrer_in_window(mid):
        if window is None:
            return True
        for nb in _spine_neighbors(conn, mid, edge_types):
            if nb["id"] in reached and _in_window(nb, window):
                return True
        return False

    work = sorted(missing)
    seen = set(work)
    while work:
        progressed = False
        for mid in list(work):
            work.remove(mid)
            if graphstore.is_dead_ref(conn, mid):
                continue                              # phantom: pruned, no gap
            if backfill is None:
                # Offline: no completion policy ran, so we cannot claim a ref is
                # "outside the window" — we simply haven't looked. Every live
                # (non-dead) missing id is honestly `not_gathered`.
                gaps[mid] = "not_gathered"
                continue
            if not referrer_in_window(mid):
                gaps[mid] = "outside_window"
                continue
            if fetched >= budget:
                gaps[mid] = "budget"
                continue
            res = backfill(conn, mid)
            fetched += 1
            if res.get("absent"):
                continue                              # tombstoned by gather: prune
            if not res.get("fetched"):
                gaps[mid] = "unreachable"
                continue
            gaps.pop(mid, None)
            progressed = True
        if progressed:
            # Re-traverse from everything reached so far: a backfilled node may
            # reference further missing spine nodes. skip_dead drops phantoms.
            reach = graphstore.traverse_spine(
                conn, sorted(reached) + sorted(seen), edge_types=edge_types,
                skip_dead=True)
            reached = set(reach["reached"])
            for nm in reach["missing"]:
                if nm not in seen and nm not in gaps:
                    seen.add(nm)
                    work.append(nm)
            work = sorted(work)
        else:
            break

    if warn and gaps:
        warn("complete: {} gap(s): {}".format(
            len(gaps), ", ".join("{}({})".format(i, r)
                                 for i, r in sorted(gaps.items()))))
    # `reached` from traverse_spine includes ids that are referenced-but-absent
    # (the gaps). The completed reached set is the PRESENT train nodes only —
    # an un-chased outside_window/budget/unreachable ref is a gap, not reached.
    present_reached = {i for i in reached
                       if graphstore.get_node(conn, i) is not None}
    return {
        "reached": present_reached,
        "gaps": [{"id": i, "reason": r} for i, r in sorted(gaps.items())],
        "fetched": fetched,
    }


def annotate(train, result):
    """Stamp the honest edge contract onto a `_train` dict in place:
    `complete` (no unresolved spine refs) + `gaps` (sorted by id). Returns the
    train for chaining."""
    gaps = sorted(result.get("gaps", []), key=lambda g: g["id"])
    train["complete"] = not gaps
    train["gaps"] = gaps
    return train

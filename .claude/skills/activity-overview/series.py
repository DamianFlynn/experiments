"""Phase 13: series continuity ("Since last installment"). Pure, offline.

A digest is usually one in a *series* of installments for the same project.
This layer compares the current enriched bundle against the prior installment's
recorded snapshot to surface what is **new** vs **carried over** (with each
carried item's `prior_status` and current `bucket`) and the **forecast loop**
(the prior installment's predictions vs. what landed or was dropped this window).

No network, no store mutation. `series.json` is a thin convenience index over
the store — the past installments' `installment_snapshot`s — and is *never* an
override: a re-gather is always truth. Dropping the file makes the next run a
clean "first installment".

Installments are identified by their window `(from, to)` (#86): recording a
window that is already in the index replaces that entry, the index is kept in
window order, and an installment is compared against the one immediately
before its window — not against whatever was appended last.
"""

INDEX_VERSION = 2

BUCKETS = ("shipped", "in_flight", "next_candidates", "rejected")


def _key(r):
    """The identity used to compare items across installments: (type, id).
    URLs are deliberately excluded — the store / a re-extract is truth."""
    return (r.get("type"), r.get("id"))


def _ref_sort(r):
    return (r["type"], r["id"])


def bundle_window(meta):
    """The (from, to) window an installment of this bundle is recorded under:
    `meta.period` (#75), else `meta.from` / `meta.to` — the same bounds the
    buckets were computed for. Either side may be None (dateless bundle)."""
    period = meta.get("period") or {}
    return (period.get("from") or meta.get("from"),
            period.get("to") or meta.get("to"))


def installment_window(inst):
    """(from, to) of a recorded installment, or None when it lacks either
    (a hand-made or legacy entry without a window)."""
    frm, to = inst.get("from"), inst.get("to")
    if frm is None or to is None:
        return None
    return (frm, to)


def _order_key(inst):
    # Windowless entries sort before every window (they cannot be placed, and
    # the legacy list form only ever appended them before newer months).
    w = installment_window(inst)
    return (0, "", "") if w is None else (1, w[0], w[1])


def normalize_installments(installments):
    """The index in window order with one entry per window. A window recorded
    more than once (a pre-#86 double run) keeps the entry recorded last;
    windowless entries keep their relative order, ahead of every window.
    Entries are returned unchanged (no field is rewritten)."""
    last_at = {}
    for n, inst in enumerate(installments):
        w = installment_window(inst)
        if w is not None:
            last_at[w] = n
    kept = [inst for n, inst in enumerate(installments)
            if installment_window(inst) is None
            or last_at[installment_window(inst)] == n]
    return sorted(kept, key=_order_key)  # stable: windowless keep their order


def predecessor(installments, window):
    """The installment immediately before `window` in window order (None when
    there is none). A windowless legacy entry counts as earlier than any
    window."""
    target = (1, window[0], window[1])
    before = [i for i in normalize_installments(installments)
              if _order_key(i) < target]
    return before[-1] if before else None


def later_installments(installments, window):
    """Recorded installments whose window sorts after `window`."""
    target = (1, window[0], window[1])
    return [i for i in normalize_installments(installments)
            if _order_key(i) > target]


def place_installment(installments, snapshot):
    """The index with `snapshot` recorded under its window: an entry for the
    same window is replaced, everything is kept in window order."""
    w = installment_window(snapshot)
    rest = [i for i in installments if installment_window(i) != w]
    return normalize_installments(rest + [snapshot])


def index_document(installments):
    """The on-disk form of the index: `{"version": 2, "installments": [...]}`."""
    return {"version": INDEX_VERSION, "installments": list(installments)}


def _board_status_index(bundle):
    """(type, id) -> board_status, read from the prs/issues records where the
    Phase 12 board status rides. Items without a board status are absent."""
    idx = {}
    for p in bundle.get("prs", []):
        if p.get("board_status"):
            idx[("pr", p["number"])] = p["board_status"]
    for i in bundle.get("issues", []):
        if i.get("board_status"):
            idx[("issue", i["number"])] = i["board_status"]
    return idx


def installment_snapshot(bundle, captured="in_order"):
    """The compact record this installment contributes to the series index.

    Refs are the `{type, id}` the buckets/forecast already carry (url dropped —
    the store is truth), for all four buckets. `in_flight` items carry their
    `board_status` when the board defines one; `forecast` items carry their
    predicted `tier`. `captured` is "in_order", or "backfill" when the window
    was recorded after a later window was already in the index (its open work
    and forecast are as of the run, not as of the window's end).
    Deterministic ordering (by type, id)."""
    meta = bundle.get("meta", {})
    buckets = bundle.get("buckets", {})
    bs = _board_status_index(bundle)

    def plain(bucket):
        return sorted(({"type": r["type"], "id": r["id"]}
                       for r in buckets.get(bucket, [])), key=_ref_sort)

    in_flight = []
    for r in buckets.get("in_flight", []):
        rec = {"type": r["type"], "id": r["id"]}
        status = bs.get((r["type"], r["id"]))
        if status:
            rec["board_status"] = status
        in_flight.append(rec)
    in_flight.sort(key=_ref_sort)

    forecast = sorted(
        ({"type": c["ref"]["type"], "id": c["ref"]["id"], "tier": c["tier"]}
         for c in bundle.get("forecast", {}).get("candidates", [])),
        key=_ref_sort)

    frm, to = bundle_window(meta)
    return {
        "owner": meta.get("owner"),
        "repo": meta.get("repo"),
        "from": frm,
        "to": to,
        "ref_date": meta.get("ref_date") or to,
        "captured": captured,
        "shipped": plain("shipped"),
        "in_flight": in_flight,
        "next_candidates": plain("next_candidates"),
        "rejected": plain("rejected"),
        "forecast": forecast,
    }


def compute_series(bundle, prior):
    """A `series` block describing this installment relative to `prior` (the
    installment immediately before this window, or None for the first one).
    Does NOT mutate items. Deterministic ordering (by type, id).

    - `first_installment`: True when there is no prior installment.
    - `prior`: `{from, to, captured}` of the installment compared against
      (None on the first). `captured` is None for a pre-#86 entry.
    - `new`: this window's shipped + in_flight items the prior snapshot did not
      know at all (in none of its buckets nor its forecast).
    - `carried_over`: items in the prior's in_flight that are in ANY of this
      window's four buckets, each with its current `bucket` and the
      `prior_status` (the prior board status, or "in_flight" when none) — so
      work that was in flight and is now rejected or re-planned is reported.
    - `forecast_loop`: over the prior's forecast refs → `landed` (now in this
      window's shipped), `dropped` (now in this window's rejected: PR closed
      unmerged / issue closed not planned) or `not_yet`. Empty on the first.
    """
    buckets = bundle.get("buckets", {})

    if prior is None:
        return {
            "first_installment": True,
            "prior": None,
            "new": [],
            "carried_over": [],
            "forecast_loop": {"landed": [], "not_yet": [], "dropped": []},
        }

    prior_in_flight = {_key(r): r for r in prior.get("in_flight", [])}
    prior_seen = set(prior_in_flight)
    for name in ("shipped", "next_candidates", "rejected", "forecast"):
        prior_seen.update(_key(r) for r in prior.get(name, []))

    new, carried = [], []
    for bucket in BUCKETS:
        for r in buckets.get(bucket, []):
            k = _key(r)
            entry = {"type": r["type"], "id": r["id"], "url": r.get("url"),
                     "bucket": bucket}
            if k in prior_in_flight:
                entry["prior_status"] = (
                    prior_in_flight[k].get("board_status") or "in_flight")
                carried.append(entry)
            elif bucket in ("shipped", "in_flight") and k not in prior_seen:
                new.append(entry)
            # else: known last time and not carried work — neither.
    new.sort(key=_ref_sort)
    carried.sort(key=_ref_sort)

    now_shipped = {_key(r) for r in buckets.get("shipped", [])}
    now_rejected = {_key(r) for r in buckets.get("rejected", [])}
    loop = {"landed": [], "not_yet": [], "dropped": []}
    for f in prior.get("forecast", []):
        rec = {"type": f.get("type"), "id": f.get("id"), "tier": f.get("tier")}
        k = _key(f)
        outcome = ("landed" if k in now_shipped
                   else "dropped" if k in now_rejected else "not_yet")
        loop[outcome].append(rec)
    for rows in loop.values():
        rows.sort(key=_ref_sort)

    return {
        "first_installment": False,
        "prior": {"from": prior.get("from"), "to": prior.get("to"),
                  "captured": prior.get("captured")},
        "new": new,
        "carried_over": carried,
        "forecast_loop": loop,
    }

"""Phase 13: series continuity ("Since last installment"). Pure, offline.

A digest is usually one in a *series* of installments for the same project.
This layer compares the current enriched bundle against the prior installment's
recorded snapshot to surface what is **new** vs **carried over** (with each
carried item's `prior_status`) and the **forecast loop** (the prior
installment's predictions vs. what actually landed this window).

No network, no store mutation. `series.json` is a thin convenience index over
the store — an ordered list of past installments' `installment_snapshot`s — and
is *never* an override: a re-gather is always truth. Dropping the file makes the
next run a clean "first installment".
"""


def _key(r):
    """The identity used to compare items across installments: (type, id).
    URLs are deliberately excluded — the store / a re-extract is truth."""
    return (r.get("type"), r.get("id"))


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


def installment_snapshot(bundle):
    """The compact record this installment contributes to the series index.

    Refs are the `{type, id}` the buckets/forecast already carry (url dropped —
    the store is truth). `in_flight` items carry their `board_status` when the
    board defines one; `forecast` items carry their predicted `tier`.
    Deterministic ordering (by type, id)."""
    meta = bundle.get("meta", {})
    buckets = bundle.get("buckets", {})
    bs = _board_status_index(bundle)

    shipped = sorted(
        ({"type": r["type"], "id": r["id"]} for r in buckets.get("shipped", [])),
        key=lambda r: (r["type"], r["id"]))

    in_flight = []
    for r in buckets.get("in_flight", []):
        rec = {"type": r["type"], "id": r["id"]}
        status = bs.get((r["type"], r["id"]))
        if status:
            rec["board_status"] = status
        in_flight.append(rec)
    in_flight.sort(key=lambda r: (r["type"], r["id"]))

    forecast = sorted(
        ({"type": c["ref"]["type"], "id": c["ref"]["id"], "tier": c["tier"]}
         for c in bundle.get("forecast", {}).get("candidates", [])),
        key=lambda r: (r["type"], r["id"]))

    return {
        "from": meta.get("from"),
        "to": meta.get("to"),
        "ref_date": meta.get("ref_date") or meta.get("to"),
        "shipped": shipped,
        "in_flight": in_flight,
        "forecast": forecast,
    }


def compute_series(bundle, prior):
    """A `series` block describing this installment relative to `prior` (the
    previous installment's `installment_snapshot`, or None for the first one).
    Does NOT mutate items. Deterministic ordering (by type, id).

    - `first_installment`: True when there is no prior installment.
    - `new`: this window's shipped+in_flight items whose (type, id) appeared in
      neither the prior's shipped nor in_flight.
    - `carried_over`: items present in the prior's in_flight and still here this
      window (whether they shipped or are still in flight), each with the
      `prior_status` (the prior board status, or "in_flight" when none).
    - `forecast_loop`: over the prior's forecast refs → `landed` (now in this
      window's shipped) vs `not_yet` (still not shipped). Empty on the first.
    """
    buckets = bundle.get("buckets", {})

    if prior is None:
        return {
            "first_installment": True,
            "new": [],
            "carried_over": [],
            "forecast_loop": {"landed": [], "not_yet": []},
        }

    prior_shipped = {_key(r) for r in prior.get("shipped", [])}
    prior_in_flight = {_key(r): r for r in prior.get("in_flight", [])}
    prior_seen = prior_shipped | set(prior_in_flight)

    new, carried = [], []
    for bucket in ("shipped", "in_flight"):
        for r in buckets.get(bucket, []):
            k = _key(r)
            entry = {"type": r["type"], "id": r["id"], "url": r.get("url"),
                     "bucket": bucket}
            if k in prior_in_flight:
                entry["prior_status"] = (
                    prior_in_flight[k].get("board_status") or "in_flight")
                carried.append(entry)
            elif k not in prior_seen:
                new.append(entry)
            # else: already shipped in the prior installment — neither.
    new.sort(key=lambda r: (r["type"], r["id"]))
    carried.sort(key=lambda r: (r["type"], r["id"]))

    now_shipped = {_key(r) for r in buckets.get("shipped", [])}
    landed, not_yet = [], []
    for f in prior.get("forecast", []):
        rec = {"type": f.get("type"), "id": f.get("id"), "tier": f.get("tier")}
        (landed if (f.get("type"), f.get("id")) in now_shipped else not_yet
         ).append(rec)
    landed.sort(key=lambda r: (r["type"], r["id"]))
    not_yet.sort(key=lambda r: (r["type"], r["id"]))

    return {
        "first_installment": False,
        "new": new,
        "carried_over": carried,
        "forecast_loop": {"landed": landed, "not_yet": not_yet},
    }

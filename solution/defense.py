"""
Your defense. Implement register(ctx) and a handler per event type.
See ../README.md for the full interface + toolkit reference, and
../RULES.md before you start.

Strategy
--------
* Exactly one metered call per event. Full coverage fits the budget, and the
  tools are deterministic, so a second call never adds information.
* Structural signals (contract violations, missing upstream edges, orphaned
  outputs) are exact checks.
* Every numeric signal becomes a directional z-score:
    - two-sided baselines (min/max) give a prior: mean = midpoint,
      sigma = width / 6 (the bounds are mean +/- 3 sigma);
    - one-sided caps publish no mean: after a few clean samples the mean is
      estimated from them and sigma = (cap - mean) / 3;
    - after warm-up, mean/sigma come from this run's own clean events (events
      we did not alert on), shrunk toward the baseline. This is what lets
      subtle faults that sit inside the 3-sigma caps get caught.
    - crossing a published bound always alerts; online stats only tighten.
* Alert on one strong signal or two moderate ones together. The single-signal
  bar rises with the number of signals checked (multiple-testing correction).
  Thresholds sit below 3 sigma on purpose: a caught fault is worth ~4-5x what
  a false alarm costs under 0.5*TPR - 0.3*FPR at the observed fault rates.
"""
import math
from api import Verdict

Z_SINGLE = 1.9      # one signal this far out is enough on its own...
Z_PER_EXTRA_SIGNAL = 0.2  # ...plus this much per additional signal checked
CAP_Z = 10.0        # z assigned to a value past its published cap: always alerts
Z_PAIR = 2.25       # two signals this far out together are enough
WARMUP = 8          # clean samples needed before trusting online stats
PRIOR_WEIGHT = 20   # baseline counts as this many clean samples when blending
MIN_MEAN_SAMPLES = 3  # clean samples needed to estimate a one-sided metric's mean
HISTORY = 200       # rolling window of clean samples per metric
Z_LEARN = 2.0       # only values below this z are added to the clean history
SD_FLOOR = 0.8      # online sigma never drops below this fraction of the baseline's


# --------------------------------------------------------------------------
# statistics helpers
# --------------------------------------------------------------------------

def _two_sided_prior(ctx, lo_key, hi_key):
    lo, hi = ctx.baseline.get(lo_key), ctx.baseline.get(hi_key)
    if lo is None or hi is None:
        return None
    return (lo + hi) / 2.0, max((hi - lo) / 6.0, 1e-12), lo, hi


def _history(ctx, name):
    return ctx.state.setdefault("hist", {}).setdefault(name, [])


# (statistics module can't be used: its own `numbers` import is blocked)
def _median(xs):
    s = sorted(xs)
    k = len(s) // 2
    return s[k] if len(s) % 2 else (s[k - 1] + s[k]) / 2.0


def _robust(xs):
    """median and MAD-based sigma: a few undetected faults in the history barely
    move them, unlike mean/stdev."""
    med = _median(xs)
    sd = 1.4826 * _median([abs(x - med) for x in xs])
    if sd <= 1e-12:  # degenerate (many identical values): fall back to stdev
        m = sum(xs) / len(xs)
        sd = math.sqrt(sum((x - m) ** 2 for x in xs) / max(1, len(xs) - 1))
    return med, sd


def _z(ctx, name, value, side, prior=None, cap=None):
    """Directional z-score of `value` for metric `name`.

    side:  'hi' (only large is bad), 'lo' (only small is bad), 'both'.
    prior: (mean, sigma, lo, hi) from two-sided baseline bounds, if published.
    cap:   one-sided baseline upper bound, used alone during warm-up.
    """
    hist = _history(ctx, name)
    n = len(hist)
    if n >= WARMUP:
        mean, sd = _robust(hist)
        sd *= math.sqrt(1 + 1.0 / n)  # prediction interval
        # The baselines come from far more clean data than we've seen, so shrink
        # toward them: a short clean run under-estimates the tail otherwise.
        # Sigma is also capped at the baseline's: subtle faults that slipped
        # into the history must not inflate it and hide the next ones.
        w = n / (n + PRIOR_WEIGHT)
        # ...and floored, so a quiet stretch can't make sigma collapse.
        if prior is not None:
            mean = w * mean + (1 - w) * prior[0]
            sd = math.sqrt(w * sd * sd + (1 - w) * prior[1] * prior[1])
            sd = min(max(sd, SD_FLOOR * prior[1]), prior[1])
        elif cap is not None and cap > mean:
            cap_sd = (cap - mean) / 3.0  # cap == mean + 3 sigma
            sd = math.sqrt(w * sd * sd + (1 - w) * cap_sd * cap_sd)
            sd = min(max(sd, SD_FLOOR * cap_sd), cap_sd)
    elif prior is not None:
        mean, sd = prior[0], prior[1]
    elif cap is not None and n >= MIN_MEAN_SAMPLES and cap > _median(hist):
        # Early warm-up with only a cap: a handful of clean samples already pin
        # the mean well, and the cap supplies sigma (cap == mean + 3 sigma).
        mean = _median(hist)
        sd = (cap - mean) / 3.0
    elif cap is not None:
        # No history yet: the cap is the threshold.
        return CAP_Z if value > cap else 0.0
    else:
        return 0.0
    if sd <= 1e-12:
        return 0.0
    z = (value - mean) / sd
    if side == "lo":
        z = -z
    elif side == "both":
        z = abs(z)
    # Online stats may only tighten detection, never loosen it past the
    # published bounds: crossing one always alerts.
    outside = cap is not None and value > cap
    if prior is not None and side in ("hi", "both") and value > prior[3]:
        outside = True
    if prior is not None and side in ("lo", "both") and value < prior[2]:
        outside = True
    return max(z, CAP_Z) if outside else z


def _remember(ctx, samples):
    for name, value in samples:
        hist = _history(ctx, name)
        hist.append(value)
        if len(hist) > HISTORY:
            del hist[0]


def _decide(ctx, pillar, scored, hard_reasons=()):
    """scored: list of (name, value, z). Learns from the event if clean."""
    # Multiple-testing correction: more signals per event means more chances
    # for clean noise to cross the line, so the bar rises with the count.
    z_single = Z_SINGLE + Z_PER_EXTRA_SIGNAL * max(0, len(scored) - 1)
    strong = [(n, z) for n, _, z in scored if z >= z_single]
    moderate = [(n, z) for n, _, z in scored if z >= Z_PAIR]
    reasons = list(hard_reasons)
    if strong:
        reasons += ["%s z=%.2f" % s for s in strong]
    elif len(moderate) >= 2:
        reasons += ["%s z=%.2f" % m for m in moderate]
    alert = bool(reasons)
    if not hard_reasons:
        # Learn from values that look clearly clean, so near-miss subtle faults
        # don't drag the reference toward themselves. Deliberately independent
        # of the alert decision: excluding everything we alert on would
        # truncate the history at the alert threshold, shrink sigma, and
        # snowball into ever more alerts once thresholds are low.
        _remember(ctx, [(n, v) for n, v, z in scored if z < Z_LEARN])
    top = max([z for _, _, z in scored] or [0.0])
    conf = 1.0 if hard_reasons else min(1.0, max(0.0, top / (2 * Z_SINGLE)))
    return Verdict(alert=alert, confidence=round(conf, 3),
                   reason="; ".join(reasons), pillar=pillar)


def _error(result):
    return not isinstance(result, dict) or "error" in result


def _names(items):
    for it in items or []:
        yield it.get("name") if isinstance(it, dict) else str(it)


# --------------------------------------------------------------------------
# handlers
# --------------------------------------------------------------------------

def register(ctx):
    ctx.on("data_batch", check_data_batch)
    ctx.on("contract_checkpoint", check_contract_checkpoint)
    ctx.on("lineage_run", check_lineage_run)
    ctx.on("feature_materialization", check_feature_materialization)
    ctx.on("embedding_batch", check_embedding_batch)


def check_data_batch(payload, ctx):
    p = ctx.tools.batch_profile(payload["batch_id"])
    if _error(p):
        return Verdict(alert=False, pillar="checks", reason="tool error")
    b = ctx.baseline
    table = payload.get("table", "?") + ":"
    null_rate = (p.get("null_rate") or {}).get("customer_id", 0.0)
    scored = [
        (table + "rows", p["row_count"],
         _z(ctx, table + "rows", p["row_count"], "hi",
            prior=_two_sided_prior(ctx, "row_count_min", "row_count_max"))),
        (table + "amount", p["mean_amount"],
         _z(ctx, table + "amount", p["mean_amount"], "both",
            prior=_two_sided_prior(ctx, "mean_amount_min", "mean_amount_max"))),
        (table + "nulls", null_rate,
         _z(ctx, table + "nulls", null_rate, "hi", cap=b.get("null_rate_max"))),
        (table + "stale", p["staleness_min"],
         _z(ctx, table + "stale", p["staleness_min"], "hi", cap=b.get("staleness_min_max"))),
    ]
    # std_amount is deliberately not scored: it has no published bound and
    # adding it only raises the multiple-testing bar for the signals that matter.
    return _decide(ctx, "checks", scored)


def check_contract_checkpoint(payload, ctx):
    d = ctx.tools.contract_diff(payload["contract_id"], payload["checkpoint_batch_id"])
    if _error(d):
        return Verdict(alert=False, pillar="contracts", reason="tool error")
    hard = ["violation: %s" % v for v in d.get("violations") or []]
    scored = []
    delay = d.get("freshness_delay_min")
    if delay is not None:
        sla = (payload.get("declared_sla") or {}).get("freshness_min")
        if sla is not None and delay > sla:
            hard.append("freshness %.1f > declared SLA %s" % (delay, sla))
        name = "fresh:" + payload.get("contract_id", "?")
        scored.append((name, delay,
                       _z(ctx, name, delay, "hi", cap=ctx.baseline.get("freshness_delay_max_min"))))
    return _decide(ctx, "contracts", scored, hard)


def check_lineage_run(payload, ctx):
    g = ctx.tools.lineage_graph_slice(payload["run_id"])
    if _error(g):
        return Verdict(alert=False, pillar="lineage", reason="tool error")
    job = payload.get("job", "?")
    hard = []

    # Expected upstream = declared inputs plus every edge this job has shown on
    # clean runs. A run missing any of them has lost an edge.
    ups = set(_names(g.get("actual_upstream")))
    known = ctx.state.setdefault("upstream", {}).setdefault(job, set())
    missing = (set(_names(payload.get("inputs"))) | known) - ups
    if missing:
        hard.append("missing upstream: %s" % ",".join(sorted(missing)))

    # Once a job's graph is established, an edge it has never had is also a
    # mismatch (rewired / wrong source).
    runs = ctx.state.setdefault("clean_runs", {})
    if runs.get(job, 0) >= MIN_MEAN_SAMPLES and ups - known:
        hard.append("unexpected upstream: %s" % ",".join(sorted(ups - known)))

    down = g.get("actual_downstream_count")
    down_seen = ctx.state.setdefault("downstream", {}).setdefault(job, [])
    if down == 0 and payload.get("outputs"):
        hard.append("orphaned output (0 downstream)")
    elif down is not None and len(down_seen) >= MIN_MEAN_SAMPLES and down < min(down_seen):
        # Partially orphaned: fewer consumers than on any clean run.
        hard.append("downstream %s < usual %s" % (down, min(down_seen)))

    scored = []
    if g.get("duration_ms") is not None:
        name = "duration:" + job
        scored.append((name, g["duration_ms"],
                       _z(ctx, name, g["duration_ms"], "hi",
                          cap=ctx.baseline.get("lineage_duration_ms_max"))))

    verdict = _decide(ctx, "lineage", scored, hard)
    if not verdict.alert:
        known |= ups
        runs[job] = runs.get(job, 0) + 1
        if down is not None:
            down_seen.append(down)
    return verdict


def check_feature_materialization(payload, ctx):
    d = ctx.tools.feature_drift(payload["feature_view"], payload["batch_id"])
    if _error(d):
        return Verdict(alert=False, pillar="ai_infra", reason="tool error")
    cap = ctx.baseline.get("feature_mean_shift_sigma_max")
    name = "fshift:" + payload.get("feature_view", "?")
    if d.get("train_std"):
        # Signed shift: clean noise is symmetric around 0, so its spread is a
        # much cleaner reference than the folded |shift| the tool reports.
        shift = (d["serve_mean"] - d["train_mean"]) / d["train_std"]
        # Upward only: train/serve skew faults inflate serve_mean; a dip of the
        # same size is ordinary noise far more often than a fault.
        z = _z(ctx, name, shift, "hi")
        if cap is not None and shift > cap:
            z = max(z, CAP_Z)
    else:
        shift = d.get("mean_shift_sigma", 0.0)
        z = CAP_Z if cap is not None and shift > cap else 0.0
    return _decide(ctx, "ai_infra", [(name, shift, z)])


def check_embedding_batch(payload, ctx):
    d = ctx.tools.embedding_drift(payload["corpus"], payload["chunk_batch_id"])
    if _error(d):
        return Verdict(alert=False, pillar="ai_infra", reason="tool error")
    b = ctx.baseline
    corpus = payload.get("corpus", "?") + ":"
    scored = [
        (corpus + "centroid", d["centroid_shift"],
         _z(ctx, corpus + "centroid", d["centroid_shift"], "hi",
            cap=b.get("embedding_centroid_shift_max"))),
        (corpus + "age", d["avg_doc_age_days"],
         _z(ctx, corpus + "age", d["avg_doc_age_days"], "hi",
            cap=b.get("corpus_avg_doc_age_days_max"))),
    ]
    return _decide(ctx, "ai_infra", scored)

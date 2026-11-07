#!/usr/bin/env python3
"""Markdown report of a campaign JSONL (plain or .xz), written by campaign.py, also while it is still running.

    scripts/report.py results/2026-11-08-my-change.jsonl[.xz] [--cases]

Per class: geometric mean and range of the after/before throughput ratio, measured and extrapolated separately.
Per laddered case (with --cases, every measured or extrapolated size): both scores, the ratio and its 95% interval.
Sizes one side skipped are interpolated between its measured neighbours (log-log), sizes beyond its last measurement
extrapolated from its last decade's fit; both are marked `≈`.
"""
import json
import lzma
import math
import pathlib
import sys
from collections import defaultdict

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from campaign import DECADES, decade_points, ratio_ci, slope, throughput  # noqa: E402


def load(path):
    with (lzma.open if path.endswith(".xz") else open)(path, "rt") as f:
        return [json.loads(line) for line in f if line.strip()]


def fmt(x):
    return f"{x:.2f}×" if x < 1000 else f"{x:.3g}×".replace("e+0", "e+")


def geomean(xs):
    return math.exp(sum(math.log(x) for x in xs) / len(xs))


def fit(points):
    """(slope, intercept) of log(time/op) over log(size), on the last decade with at least three points."""
    for top in reversed(DECADES):
        pts = decade_points(points, top)
        if len(pts) >= 3:
            k = slope(pts)
            n, s = pts[-1]
            return k, -math.log(s) - k * math.log(n)
    return None


def main(path, show_cases=False):
    lines = load(path)
    head = lines[0] if lines and lines[0].get("type") == "campaign" else {}
    forks = defaultdict(list)  # (case, size, side) -> [[ops/s per iteration], ...]
    benchmark_of, failures = {}, []
    for line in lines:
        if line.get("type") != "measurement":
            continue
        if line["exit_code"] != 0 or not line["records"]:
            failures.append(line)
            continue
        forks[line["case"], line["size"], line["side"]] += throughput(line["records"][0])
        benchmark_of[line["case"]] = line["benchmark"].rsplit(".", 2)[-2]

    def mean(f):
        values = [x for fork in f for x in fork]
        return sum(values) / len(values)

    ladders = defaultdict(lambda: defaultdict(list))  # case -> side -> [(size, ops/s)]
    for (case, size, side), f in forks.items():
        if size:
            ladders[case][side].append((size, mean(f)))

    rows, measured, estimated = [], defaultdict(list), defaultdict(list)
    for case, size in sorted({(c, s) for c, s, _ in forks}, key=lambda k: (k[0], k[1] or 0)):
        b, a = forks.get((case, size, "before")), forks.get((case, size, "after"))
        if b and a:
            r, lo, hi = ratio_ci(b, a)
            measured[benchmark_of[case]].append(r)
            rows.append((case, size, mean(b), mean(a), f"{fmt(r)} [{fmt(lo)}, {fmt(hi)}]", len(b), len(a)))
        elif size and a and not b:
            pts = sorted(ladders[case]["before"])
            lower = [p for p in pts if p[0] < size]
            upper = [p for p in pts if p[0] > size]
            if lower and upper:  # skipped by a settled ladder: interpolate between the measured neighbours
                (n0, s0), (n1, s1) = lower[-1], upper[0]
                k = slope([(n0, s0), (n1, s1)])
                b_est = s0 * (n0 / size) ** k
            elif f := fit(pts):  # beyond the last measured size: extrapolate the last decade
                k, c = f
                b_est = math.exp(-(c + k * math.log(size)))
            else:
                continue
            estimated[benchmark_of[case]].append(mean(a) / b_est)
            rows.append((case, size, b_est, mean(a), f"≈ {fmt(mean(a) / b_est)} (slope {k:.2f})", 0, len(a)))

    print(f"# {head.get('label', path)}\n")
    if head:
        print(f"Before `{head['before']['commit'][:9]}` ({head['before']['ref']}), after `{head['after']['commit'][:9]}` "
              f"({head['after']['ref']}), benchmark sources `{head['sources']['commit'][:9]}`. "
              f"{head['host'].get('cpu', head['host'].get('machine'))}, {head['host'].get('java', '')}. "
              f"JMH `{' '.join(head['jmh'])}`, budget {head['budget']} s.\n")
    print("| Class | Measured | Ratio (geomean; range) | Estimated (≈) | Ratio (geomean; range) |")
    print("|---|---:|---|---:|---|")
    for cls in sorted(set(measured) | set(estimated)):
        m, e = measured[cls], estimated[cls]
        mt = f"{fmt(geomean(m))}; {fmt(min(m))}–{fmt(max(m))}" if m else "—"
        et = f"≈ {fmt(geomean(e))}; {fmt(min(e))}–{fmt(max(e))}" if e else "—"
        print(f"| {cls} | {len(m)} | {mt} | {len(e)} | {et} |")

    print("\n## Growth (log-log slope of time per op, per decade)\n")
    print("| Case | Side | " + " | ".join(f"≤{d:,}" for d in DECADES[1:]) + " | Largest size |")
    print("|---|---|" + "---:|" * len(DECADES[1:]) + "---:|")
    for case in sorted(ladders):
        for side in ("before", "after"):
            pts = sorted(ladders[case][side])
            if not pts:
                continue
            cells = [f"{slope(p):.2f}" if len(p := decade_points(pts, d)) >= 3 else "" for d in DECADES[1:]]
            print(f"| {case} | {side} | " + " | ".join(cells) + f" | {pts[-1][0]:,} |")

    if failures:
        print("\n## Failed measurements\n")
        for line in failures:
            print(f"- {line['case']} size={line['size']} {line['side']} round {line['round']}: exit {line['exit_code']}")

    if show_cases:
        print("\n## Every case\n")
        print("| Case | Size | Before ops/s | After ops/s | Ratio [95%] | Forks before | Forks after |")
        print("|---|---:|---:|---:|---|---:|---:|")
        for case, size, b, a, ratio, fb, fa in rows:
            print(f"| {case} | {size or ''} | {b:.4g} | {a:.4g} | {ratio} | {fb} | {fa} |")


if __name__ == "__main__":
    try:
        main(sys.argv[1], "--cases" in sys.argv[2:])
    except BrokenPipeError:  # piped into head
        sys.stderr.close()

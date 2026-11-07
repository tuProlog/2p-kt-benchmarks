#!/usr/bin/env python3
"""Before/after JMH campaign over two 2p-kt refs, appending one JSON line per measurement as soon as it completes.

    scripts/campaign.py --repo ../2p-kt --before origin/develop --after origin/my-branch --label my-change
    scripts/campaign.py ... --only 'theory\\.TheoryLookup'      # a subset of benchmarks (regexp on the full name)
    scripts/campaign.py --self-test

Each measurement is one case (benchmark method + parameters) on one side, in one JMH fork. Both sides run the
benchmark sources of `--sources` (default: `--after`), copied over, so only production code differs.

Theory sizes follow a ladder (10..100 by 10, ..1000 by 100, ..10000 by 1000, ..100000 by 10000), measured size by
size across all cases, alternating which side goes first. A side stops climbing for a case when one measurement took,
or is predicted from its fitted growth to take, more than `--budget` seconds; once the log-log slope of its last
decade is within 0.1 of the previous decade's, it only measures decade boundaries. Sizes a side did not reach are
extrapolated by `report.py` from that fit, and labelled as such.

After the first pass, cases whose after/before ratio has a 95% bootstrap interval wider than ±`--ci` (or that sit
near parity with a single fork per side) get another round, up to `--rounds`.

The output, `results/<date>-<label>.jsonl`, is the source of truth: rerunning the same command replays it and resumes
where it stopped (Ctrl-C loses at most the measurement in progress). `--finish` compresses it to `.jsonl.xz`.
"""
import argparse
import datetime
import itertools
import json
import math
import os
import pathlib
import platform
import random
import re
import shlex
import subprocess
import sys
import tempfile
import time

LADDER = (
    list(range(10, 101, 10))
    + list(range(200, 1001, 100))
    + list(range(2000, 10001, 1000))
    + list(range(20000, 100001, 10000))
)
DECADES = [10, 100, 1000, 10000, 100000]
LADDERED = re.compile(r"\.theory\.")  # benchmarks whose `size` is a theory size, climbed along LADDER
JMH = ["-wi", "3", "-i", "5", "-w", "1s", "-r", "1s", "-f", "1", "-prof", "gc", "-jvmArgs", "-Xms512m -Xmx4g"]
WARMUPS_PLUS_ITERATIONS = 8
CLASSPATH_INIT = """
allprojects {
    if (path == ":examples") {
        afterEvaluate {
            tasks.register("printJmhClasspath") {
                dependsOn("benchmarksBenchmarkCompile")
                doLast {
                    new File(System.getProperty("cpOut")).text = tasks.getByName("timingBenchmarks").classpath.files.join(":")
                }
            }
        }
    }
}
"""


# ---------------------------------------------------------------------------------------------------------- statistics

def throughput(record):
    """Per-iteration throughputs, one list per fork (ops/time; avgt/sample scores are inverted)."""
    metric = record["primaryMetric"]
    inverted = not metric["scoreUnit"].endswith("/s") and "ops/" not in metric["scoreUnit"]
    return [[1 / x if inverted else x for x in fork] for fork in metric.get("rawData", [[metric["score"]]])]


def ratio_ci(before_forks, after_forks, reps=2000, seed=1071):
    """after/before mean throughput, with a 95% hierarchical bootstrap interval (forks, then iterations)."""
    rng = random.Random(seed)

    def resample(forks):
        picked = [forks[rng.randrange(len(forks))] for _ in forks]
        values = [fork[rng.randrange(len(fork))] for fork in picked for _ in fork]
        return sum(values) / len(values)

    def mean(forks):
        values = [x for fork in forks for x in fork]
        return sum(values) / len(values)

    ratios = sorted(resample(after_forks) / resample(before_forks) for _ in range(reps))
    return mean(after_forks) / mean(before_forks), ratios[int(0.025 * reps)], ratios[int(0.975 * reps) - 1]


def slope(points):
    """Least-squares slope of log(time per op) against log(size), for [(size, ops per second)]."""
    xs = [math.log(n) for n, _ in points]
    ys = [-math.log(s) for _, s in points]
    mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
    den = sum((x - mx) ** 2 for x in xs)
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / den if den else 0.0


def decade_points(points, top):
    return [(n, s) for n, s in points if top // 10 <= n <= top]


def next_sizes(points, budget, last_seconds):
    """Sizes still to climb for one case and side, given its measurements so far [(size, ops/s)] in ladder order."""
    if not points:
        return LADDER
    last_n, last_s = points[-1]
    if last_seconds > budget:
        return []
    remaining = [n for n in LADDER if n > last_n]
    if len(points) >= 3 and remaining:
        k = slope(points[-4:])
        seconds_per_op = (1 / last_s) * (remaining[0] / last_n) ** max(k, 0)
        if seconds_per_op * WARMUPS_PLUS_ITERATIONS > budget:
            return []
    # growth is settled when the last two fully climbed decades have the same slope; once a side skips to decade
    # boundaries, later decades have too few points to count, so the decision sticks
    reached = [d for d in DECADES if d <= last_n]
    full = [(p, t) for p, t in zip(reached, reached[1:])
            if len(decade_points(points, p)) >= 3 and len(decade_points(points, t)) >= 3]
    if full and abs(slope(decade_points(points, full[-1][1])) - slope(decade_points(points, full[-1][0]))) <= 0.1:
        return [n for n in remaining if n in DECADES]
    return remaining


# ------------------------------------------------------------------------------------------------------------- running

def git(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


def prepare(repo, ref, sources, work):
    """A worktree of `ref` with the benchmark sources of `sources`, built; returns (commit, JMH classpath)."""
    tree = work / ref.replace("/", "_")
    commit = git(repo, "rev-parse", ref)
    if not tree.exists():
        git(repo, "worktree", "add", "-q", "--detach", str(tree), commit)
        git(tree, "checkout", "-q", sources, "--", "examples/src/benchmarks")
    init, cp = work / "classpath.init.gradle", tree / "jmh.classpath"
    init.write_text(CLASSPATH_INIT)
    subprocess.check_call(
        ["./gradlew", "-q", "--console=plain", "-I", str(init), f"-DcpOut={cp}", ":examples:printJmhClasspath"],
        cwd=tree,
    )
    subprocess.call(["./gradlew", "-q", "--stop"], cwd=tree)
    return commit, cp.read_text().strip()


def list_cases(classpath, only):
    """{benchmark: {param: [values]}} from `jmh -lp`."""
    out = subprocess.check_output(["java", "-cp", classpath, "org.openjdk.jmh.Main", "-lp", only or "."], text=True)
    cases, current = {}, None
    for line in out.splitlines():
        if line.startswith("  param "):
            name, values = re.match(r'\s+param "(\w+)" = \{(.*)}', line).groups()
            cases[current][name] = [v.strip() for v in values.split(",")]
        elif line.strip() and not line.startswith("Benchmarks:") and not line.startswith(" "):
            current = line.strip()
            cases[current] = {}
    return cases


def expand(benchmark, params):
    """Cases as (benchmark, frozen params without a laddered size); ladder applies when `laddered`."""
    laddered = bool(LADDERED.search(benchmark)) and "size" in params
    names = sorted(p for p in params if not (laddered and p == "size"))
    for values in itertools.product(*(params[n] for n in names)):
        yield benchmark, tuple(zip(names, values)), laddered


def case_id(benchmark, params):
    return benchmark.rsplit(".", 2)[-2] + "." + benchmark.rsplit(".", 1)[-1] + "".join(
        f"/{k}={v}" for k, v in params
    )


def valid(params, size):
    # whole-theory operations over 1000 predicates need at least one clause per predicate
    return size is None or int(dict(params).get("predicates", "0")) <= size


def measure(classpath, benchmark, params, size, timeout):
    """One fork of one case: (records, exit code or 'timeout', seconds, log tail)."""
    with tempfile.TemporaryDirectory() as tmp:
        out = pathlib.Path(tmp) / "jmh.json"
        args = [a for k, v in params for a in ("-p", f"{k}={v}")] + (["-p", f"size={size}"] if size else [])
        cmd = ["java", "-cp", classpath, "org.openjdk.jmh.Main", "^" + re.escape(benchmark) + "$", *JMH, *args,
               "-rf", "json", "-rff", str(out)]
        start = time.time()
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
            code, log = proc.returncode, proc.stdout + proc.stderr
        except subprocess.TimeoutExpired as e:
            code, log = "timeout", (e.stdout or b"").decode(errors="replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
        records = json.loads(out.read_text()) if out.exists() and out.stat().st_size else []
        return records, code, round(time.time() - start, 2), log[-2000:]


class Campaign:
    def __init__(self, path):
        self.path = path
        self.lines = [json.loads(l) for l in path.read_text().splitlines() if l.strip()] if path.exists() else []
        self.done = {self.key(l): l for l in self.lines if l.get("type") == "measurement"}

    @staticmethod
    def key(line):
        return line["case"], line.get("size"), line["side"], line["round"]

    def append(self, line):
        with open(self.path, "a") as f:
            f.write(json.dumps(line) + "\n")
            f.flush()
            os.fsync(f.fileno())
        self.lines.append(line)
        if line.get("type") == "measurement":
            self.done[self.key(line)] = line

    def forks(self, case, size, side):
        return [fork for l in self.done.values()
                if (l["case"], l.get("size"), l["side"]) == (case, size, side) and l["records"]
                for fork in throughput(l["records"][0])]

    def ladder(self, case, side):
        """Round-1 measurements of a laddered case on one side, by size."""
        return sorted((l for l in self.done.values()
                       if l["case"] == case and l["side"] == side and l["round"] == 1 and l["size"]),
                      key=lambda l: l["size"])


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--repo", type=pathlib.Path, help="a 2p-kt clone")
    parser.add_argument("--before")
    parser.add_argument("--after")
    parser.add_argument("--sources", help="ref whose examples/src/benchmarks both sides use (default: --after)")
    parser.add_argument("--label")
    parser.add_argument("--only", default="", help="JMH include regexp")
    parser.add_argument("--budget", type=float, default=180, help="seconds per measurement before a side stops")
    parser.add_argument("--rounds", type=int, default=5)
    parser.add_argument("--ci", type=float, default=0.05, help="target half-width of the ratio's 95%% interval")
    parser.add_argument("--work", type=pathlib.Path, default=pathlib.Path(tempfile.gettempdir()) / "2p-kt-campaign")
    parser.add_argument("--jmh", help="JMH settings replacing the defaults (quoted), e.g. for a smoke run")
    parser.add_argument("--finish", action="store_true", help="compress the JSONL and exit")
    parser.add_argument("--self-test", action="store_true")
    a = parser.parse_args(argv)
    if a.self_test:
        return self_test()
    if not (a.repo and a.before and a.after and a.label):
        parser.error("--repo, --before, --after and --label are required")

    results = pathlib.Path(__file__).resolve().parent.parent / "results"
    existing = sorted(results.glob(f"*-{a.label}.jsonl"))
    path = existing[-1] if existing else results / f"{datetime.date.today()}-{a.label}.jsonl"
    if a.finish:
        subprocess.check_call(["xz", "-9e", "-f", str(path)])
        print(f"{path}.xz")
        return 0

    if a.jmh:
        JMH[:] = shlex.split(a.jmh)
    a.work.mkdir(parents=True, exist_ok=True)
    sources = a.sources or a.after
    sides = {}
    for side, ref in (("before", a.before), ("after", a.after)):
        sides[side] = prepare(a.repo, ref, sources, a.work)
    campaign = Campaign(path)
    if not campaign.lines:
        campaign.append({
            "type": "campaign", "label": a.label, "started": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "before": {"ref": a.before, "commit": sides["before"][0]},
            "after": {"ref": a.after, "commit": sides["after"][0]},
            "sources": {"ref": sources, "commit": git(a.repo, "rev-parse", sources)},
            "jmh": JMH, "budget": a.budget, "rounds": a.rounds, "ci": a.ci, "ladder": LADDER,
            "host": host(),
        })
    benchmarks = list_cases(sides["after"][1], a.only)
    cases = [c for b, params in benchmarks.items() for c in expand(b, params)]
    timeout = max(3 * a.budget, 600)

    def run(case, benchmark, params, size, side, rnd):
        key = (case, size, side, rnd)
        if key in campaign.done:
            return campaign.done[key]
        records, code, seconds, log = measure(sides[side][1], benchmark, params, size, timeout)
        line = {"type": "measurement", "case": case, "benchmark": benchmark, "params": dict(params), "size": size,
                "side": side, "commit": sides[side][0], "round": rnd, "seconds": seconds, "exit_code": code,
                "finished": datetime.datetime.now(datetime.timezone.utc).isoformat(), "records": records}
        if code != 0 or not records:
            line["log_tail"] = log
        campaign.append(line)
        score = records[0]["primaryMetric"]["score"] if records else "-"
        print(f"r{rnd} {case} size={size} {side}: {score} ({seconds}s, exit {code})", flush=True)
        return line

    # round 1: flat cases once, laddered cases size by size
    flip = itertools.cycle([("before", "after"), ("after", "before")])
    for benchmark, params, laddered in cases:
        if not laddered:
            for side in next(flip):
                run(case_id(benchmark, params), benchmark, params, None, side, 1)
    for size in LADDER:
        for benchmark, params, laddered in cases:
            if not laddered or not valid(params, size):
                continue
            case = case_id(benchmark, params)
            for side in next(flip):
                climbed = campaign.ladder(case, side)
                if any(l["exit_code"] != 0 or not l["records"] for l in climbed):
                    continue  # a failed size (e.g. timeout, stack overflow) ends the climb
                points = [(l["size"], l["records"][0]["primaryMetric"]["score"]) for l in climbed]
                last_seconds = climbed[-1]["seconds"] if climbed else 0
                if size in next_sizes(points, a.budget, last_seconds):
                    run(case, benchmark, params, size, side, 1)

    # further rounds where the ratio is still uncertain
    for rnd in range(2, a.rounds + 1):
        pending = []
        for benchmark, params, laddered in cases:
            case = case_id(benchmark, params)
            for size in (LADDER if laddered else [None]):
                b, f = campaign.forks(case, size, "before"), campaign.forks(case, size, "after")
                if not b or not f:
                    continue
                r, lo, hi = ratio_ci(b, f)
                near_parity = 0.8 < r < 1.25 and min(len(b), len(f)) < 2
                if hi / r - 1 > a.ci or 1 - lo / r > a.ci or near_parity:
                    pending.append((case, benchmark, params, size))
        if not pending:
            break
        for case, benchmark, params, size in pending:
            for side in next(flip):
                run(case, benchmark, params, size, side, rnd)
    campaign.append({"type": "finished", "at": datetime.datetime.now(datetime.timezone.utc).isoformat()})
    print(f"done: {path} (compress with --finish)")
    return 0


def host():
    info = {"platform": platform.platform(), "machine": platform.machine(), "cpus": os.cpu_count()}
    for cmd, key in ((["sysctl", "-n", "machdep.cpu.brand_string"], "cpu"), (["sysctl", "-n", "hw.memsize"], "memory"),
                     (["java", "-version"], "java")):
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True)
        except OSError:
            continue
        out = (proc.stdout.strip() or proc.stderr.strip()).splitlines()  # `java -version` prints to stderr
        if proc.returncode == 0 and out:
            info[key] = out[0]
    if "cpu" not in info and os.path.exists("/proc/cpuinfo"):
        info["cpu"] = next((l.split(":", 1)[1].strip() for l in open("/proc/cpuinfo") if l.startswith("model name")), "")
    return info


def self_test():
    linear = [(n, 1e6 / n) for n in LADDER[:12]]  # 10..300, time ∝ n
    assert abs(slope(linear) - 1) < 1e-9
    assert next_sizes([], 180, 0) == LADDER
    assert next_sizes(linear, 180, 200) == []  # over budget
    quadratic = [(n, 1e4 / (n * n)) for n in LADDER[:15]]  # 10..600: 1 s/op at 100 → next would take 49 s × 8
    assert next_sizes(quadratic, 180, 10) == []
    settled = [(n, 1e9 / n) for n in LADDER if n <= 1000]  # two full decades, same slope
    assert next_sizes(settled, 180, 10) == [10000, 100000]
    assert next_sizes(settled + [(10000, 1e5)], 180, 10) == [100000]  # stays on boundaries after skipping
    bending = [(n, 1e9 / n) for n in LADDER if n <= 100] + [(n, 1e11 / n ** 2) for n in LADDER if 100 < n <= 1000]
    assert next_sizes(bending, 1e9, 10)[0] == 2000  # growth changed between decades: keep climbing
    r, lo, hi = ratio_ci([[100.0, 101, 99, 100, 100]], [[200.0, 202, 198, 200, 200]])
    assert abs(r - 2) < 1e-9 and lo < r < hi and hi / lo < 1.05
    assert list(expand("x.theory.TheoryLookupBenchmark.all", {"impl": ["a", "b"], "size": ["10"]})) == [
        ("x.theory.TheoryLookupBenchmark.all", (("impl", "a"),), True),
        ("x.theory.TheoryLookupBenchmark.all", (("impl", "b"),), True),
    ]
    assert not valid((("predicates", "1000"),), 500) and valid((("predicates", "1000"),), 1000)
    print("self-test ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())

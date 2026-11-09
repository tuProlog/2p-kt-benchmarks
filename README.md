# 2p-kt benchmark results

Raw JMH results (JSON, compressed with `xz`) of the benchmarks in
[tuProlog/2p-kt](https://github.com/tuProlog/2p-kt)'s `examples/src/benchmarks`. How they are run is described in 2p-kt's [`examples/benchmarks/README.md`](https://github.com/tuProlog/2p-kt/blob/develop/examples/benchmarks/README.md).

Files are named `results/<date>-<label>-<commit>.json.xz`, where `<commit>` is the 2p-kt commit measured. 2p-kt's
`compare.py` and `engines.py` read them as they are (or `xz -dk <file>` to get the JSON).

[Reports and findings](REPORTS.md) include the timing and unification experiments, the clause-storage review
rerun, and the three-round zero-arity fix check. The original reports, manifest, excluded runs and aborted-run
log are preserved in `results/` alongside the compressed raw data.

The migration from `2p-kt` commit `6537d8a85` preserves all 20 source artifacts byte for byte after decompression.
The two existing `2026-11-06-full-*.json.xz` datasets are retained as separate runs.

## Campaigns

`scripts/campaign.py` compares two 2p-kt refs over the whole JMH suite (or a subset, `--only <regexp>`), appending one
JSON line per measurement to `results/<date>-<label>.jsonl` as soon as it completes:

```
scripts/campaign.py --repo ../2p-kt --before origin/develop --after origin/feature/x --label feature-x
scripts/report.py results/<date>-feature-x.jsonl --cases     # works while the campaign runs, and on .jsonl.xz
scripts/campaign.py ... --finish                             # xz-compresses the JSONL once done
```

- Both sides run the same benchmark sources (`--sources`, default the `--after` ref).
- Theory benchmarks climb a size ladder (10–100 by 10, to 1000 by 100, to 10,000 by 1000, to 100,000 by 10,000), size
  by size across all cases, alternating which side goes first. A side stops climbing a case when one measurement took,
  or is predicted to take, more than `--budget` seconds (default 180), or after a failure; once its growth (log-log slope
  of time per op) is the same over two consecutive decades, it only measures decade boundaries. `report.py` interpolates
  the sizes a side skipped between its measured neighbours, extrapolates those beyond its last one from its last decade's
  fit, and marks both `≈`.
- Each measurement is a single fork. Cases whose after/before ratio has a 95% bootstrap interval wider than ±5% (`--ci`),
  or that are not clearly faster (ratio below 1.25) with one fork per side, get further rounds, up to 5 (`--rounds`): a
  single fork can't show fork-to-fork (JIT) variance.
- `--cases REGEXP` narrows the run to matching case ids (`Class.method/param=value/...`), e.g. for a focused re-run.
  Worktrees are keyed by commit, and resuming refuses a file whose refs have since moved: pass the commits instead.
- Rerunning the same command resumes from the JSONL. `python3 scripts/campaign.py --self-test` checks the stop rules and
  the statistics.

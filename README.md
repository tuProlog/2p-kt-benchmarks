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

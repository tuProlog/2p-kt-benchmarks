# 2p-kt benchmark results

Raw JMH results (JSON, compressed with `xz`) of the benchmarks in
[tuProlog/2p-kt](https://github.com/tuProlog/2p-kt)'s `examples/src/benchmarks`. How they are run, and what each run
found, is described in 2p-kt's [`examples/benchmarks/README.md`](https://github.com/tuProlog/2p-kt/blob/develop/examples/benchmarks/README.md).

Files are named `results/<date>-<label>-<commit>.json.xz`, where `<commit>` is the 2p-kt commit measured. 2p-kt's
`compare.py` and `engines.py` read them as they are (or `xz -dk <file>` to get the JSON).

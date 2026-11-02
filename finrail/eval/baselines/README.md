# Baselines

One summary per engine arm, in the shape `python -m finrail.eval run` writes. These four were
**transcribed from `docs/model-comparison.md`** (measured 30 October 2026 on main at 47e23a8, with a
harness kept outside the repo). They were not produced by `finrail.eval`; their `provenance.kind`
says `transcribed`, and a report built from them says no live run was made.

| file | arm | calls |
|---|---|---|
| `nemotron3.json` | Nemotron 3 Super on Nebius, 8,192-token cap | 67 / 70 |
| `nemotron35_32k.json` | Nemotron 3.5 Super VL on Nebius, 32,768-token cap (the default engine) | 70 / 70 |
| `nemotron35_8k.json` | Nemotron 3.5 Super VL on Nebius, 8,192-token cap | 66 / 70 |
| `opus55.json` | Claude Opus 5.5 through the CLI engine | 35 / 35 |

To replace a transcription with a measured baseline, run the seven-task set on the engine and copy
the summary here:

```bash
python -m finrail.eval run finrail/eval/cases/model_comparison_7.json --engine nebius --runs 10 --out eval-out/nemotron35
cp eval-out/nemotron35/summary.json finrail/eval/baselines/nemotron35_32k.json
```

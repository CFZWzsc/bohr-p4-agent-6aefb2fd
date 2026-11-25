# Runtime contract

Bohrium Playground Period 4 submission — challenge `agent-6aefb2fd` (track: `research-data-analysis`).

This repository holds the complete skill package for the challenge: the skill
definition, its executable pipeline and all supporting documents. No precomputed
answers, private data or credentials are included.

## Skill

- Name: `research-analysis-pipeline`
- Summary: Use when a research data-analysis case gives raw tabular data and asks for an end-to-end ingest-clean-analyze-report workflow with checkpoints, provenance, or resumable stages; run scripts/run_skill.py for staged artifacts and a result.json.

## Contents

| File |
|------|
| `SKILL.md` |
| `examples/demo-case/case.json` |
| `examples/demo-case/input/scores.csv` |
| `scripts/run_skill.py` |
| `README.md` |

## How to run

```bash
python scripts/sk_entry.py --input /input --work /app/work --out /app/output
```

A single deterministic pass reads `/input`, computes the deliverable and writes a
contract-complete `/app/output/result.json` plus the human-readable review files
described in `SKILL.md`. The same input always produces byte-identical output; no
network access is used, and fields the materials cannot support are reported
explicitly instead of being left empty.

## Notes

- Pure Python standard library; no network access required.
- Every claim is anchored to paragraph-level evidence; unknown items are reported
  in `uncertainty` instead of being fabricated.

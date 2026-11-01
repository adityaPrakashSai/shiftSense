# Eval summary

| Suite | Provider | Result | Latency per call (p50 / p95) | Cost per call | Report |
| --- | --- | --- | --- | --- | --- |
| intake: 50 messages → ConstraintEdit | Muse Spark Open (Baseten) | 100% first pass, 100% after repair | 3.4 / 5.8 s | $0.0086 | `2026-11-02-intake-baseten-live` |
| intake: 50 messages → ConstraintEdit | Muse Spark 1.3 (Meta Model API) | 100% first pass, 100% after repair | 3.6 / 5.2 s | $0.0079 | `2026-11-02-intake-meta-live` |
| explain: 15 why-questions, claim-checked | Muse Spark Open (Baseten) | 93% pass, 100% grounded first time | 2.1 / 6.7 s | $0.0032 | `2026-10-29-explain-baseten-live` |
| explain: 15 why-questions, claim-checked | Muse Spark 1.3 (Meta Model API) | not run | | | |
| writer: 20 replacement asks | Muse Spark Open (Baseten) | 20/20 pass, 90% model drafts kept | 7.5 / 10.5 s | $0.0054 | `2026-10-29-writer-baseten-live` |
| writer: 20 replacement asks | Muse Spark 1.3 (Meta Model API) | not run | | | |
| agent: 10 roster-agent workflows | Muse Spark Open (Baseten) | 10/10 done by the agent (fallback 10/10), 0 forbidden attempts | 1.4 / 6.9 s | $0.0032 | `2026-10-29-agent-baseten-live` |
| agent: 10 roster-agent workflows | Muse Spark 1.3 (Meta Model API) | not run | | | |
| vision: 5 roster sheets, per cell | Muse Spark Open (Baseten) | 98.2% of cells, 5/5 (>= 95%) sheets | 16.0 / 57.3 s | $0.0222 | `2026-10-29-vision-baseten-live` |
| vision: 5 roster sheets, per cell | Muse Spark 1.3 (Meta Model API) | not run | | | |

Sick calls (solver only, no model; 20 random sick calls on the default ward, `2026-10-29-sickcall-stats`): 20/20 re-solved, with every tier proven optimal in 100% of them, 0.13 s median and 0.14 s at most. Changed cells besides the sick nurse's own: median 1 (max 1) with the stability tier, median 50.5 (max 69) without it.

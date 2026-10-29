# explain eval · baseten · live · 2026-10-29

| Metric | Value |
| --- | --- |
| cases | 15 |
| pass_rate | 93.3 |
| grounding_rate | 100.0 |
| fallback_rate | 0.0 |
| retry_rate | 0.0 |
| tool_choice_accuracy | 93.3 |
| fact_recall | 100.0 |
| forbidden_violations | 0 |
| errors | 0 |
| latency_p50_ms | 7007 |
| latency_p95_ms | 11645 |
| cost_usd_total | 0.1017 |
| cost_usd_per_case | 0.00678 |
| explain_calls | 32 live / 32 |
| explain_latency_p50_ms | 2124 |
| explain_latency_p95_ms | 6712 |
| explain_cost_usd_per_call | 0.00318 |
| explain_tokens_per_call | 2699 in / 915 out |
| fresh | True |

| Category | Passed |
| --- | --- |
| cell | 1/1 |
| compare | 7/7 |
| fairness | 1/1 |
| injection | 0/1 |
| off_topic | 1/1 |
| outcome | 1/1 |
| proposal | 1/1 |
| summary | 2/2 |

## Failures

- `ex-015`: tools ['get_proposal', 'get_run_summary'] (wanted one of none)

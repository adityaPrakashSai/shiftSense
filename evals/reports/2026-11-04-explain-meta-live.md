# explain eval · meta · live · 2026-11-04

| Metric | Value |
| --- | --- |
| cases | 15 |
| pass_rate | 93.3 |
| grounding_rate | 93.3 |
| fallback_rate | 6.7 |
| retry_rate | 0.0 |
| tool_choice_accuracy | 100.0 |
| fact_recall | 92.3 |
| forbidden_violations | 0 |
| errors | 0 |
| latency_p50_ms | 24377 |
| latency_p95_ms | 109510 |
| cost_usd_total | 0.2075 |
| cost_usd_per_case | 0.01384 |
| explain_calls | 30 live / 31 |
| explain_latency_p50_ms | 8672 |
| explain_latency_p95_ms | 28765 |
| explain_cost_usd_per_call | 0.00692 |
| explain_tokens_per_call | 2308 in / 949 out |
| fresh | False |

| Category | Passed |
| --- | --- |
| cell | 1/1 |
| compare | 6/7 |
| fairness | 1/1 |
| injection | 1/1 |
| off_topic | 1/1 |
| outcome | 1/1 |
| proposal | 1/1 |
| summary | 2/2 |

## Failures

- `ex-008`: facts 0/1

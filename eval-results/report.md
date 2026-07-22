# Voice receptionist evaluation

Calls evaluated: 6

All outcome, efficiency, correctness, and latency metrics below are separated by language.

## Measured Retell scenarios

| Scenario | Language | Intent | Retell call ID(s) | Completed |
|---|---|---|---|---:|
| No available slots | English | booking | call_ec292b30afbcfe2a572187f4334 | yes |
| Rescheduled successfully | Code-switch | reschedule | call_f17c9104ab8f577d402ba295f68 | yes |
| Appointment cancelled successfully | Hindi | cancellation | call_b0b597d159e34c58fcb91bd60b0 | yes |
| Call interrupted and then completed in next attempt | English | reschedule | call_e32f1733953995916548677953b, call_bb0f1588bfad7eaa296de0862c8 | yes |
| Appointment requested for a service that does not exist | English | booking | call_42adbb2a06f120e87e1dc16c3df | yes |
| Hindi booked appointment | Hindi | booking | call_7ba6c01d8633f01789df675da73 | yes |

## Per-language outcomes and efficiency

| Language | Calls | Completion | Confirmed booking | Mean turns/completion | Mean turns/confirmed booking | Redundant questions/call | Calls with redundancy |
|---|---:|---:|---:|---:|---:|---:|---:|
| Code-switch | 1 | 100.0% | n/a | 7 | n/a | 0 | 0.0% |
| English | 3 | 100.0% | 50.0% | 11.67 | 14 | 0 | 0.0% |
| Hindi | 2 | 100.0% | 100.0% | 10 | 13 | 0 | 0.0% |

## Per-language correctness

| Language | Fresh search | Full-name identity | Spoken/backend branch | Dropped-call recovery |
|---|---:|---:|---:|---:|
| Code-switch | 100.0% | 100.0% | 100.0% | n/a |
| English | 100.0% | 100.0% | 100.0% | 100.0% |
| Hindi | 100.0% | 100.0% | 100.0% | n/a |

## Per-language component latency

| Language | Component | Samples | p50 ms | p95 ms |
|---|---|---:|---:|---:|
| Code-switch | asr_ms | 6 | 39.0 | 202.0 |
| Code-switch | llm_ms | 17 | 792.0 | 2502.0 |
| Code-switch | tts_ms | 14 | 173.0 | 225.0 |
| Code-switch | tool_ms | 0 | None | None |
| Code-switch | network_ms | 0 | None | None |
| Code-switch | end_to_end_ms | 6 | 1255.0 | 2847.0 |
| English | asr_ms | 24 | 204.0 | 645.0 |
| English | llm_ms | 57 | 782.0 | 2287.0 |
| English | tts_ms | 50 | 201.0 | 296.0 |
| English | tool_ms | 0 | None | None |
| English | network_ms | 0 | None | None |
| English | end_to_end_ms | 24 | 1808.0 | 3542.0 |
| Hindi | asr_ms | 10 | 184.0 | 990.0 |
| Hindi | llm_ms | 27 | 934.0 | 2264.0 |
| Hindi | tts_ms | 25 | 184.0 | 237.0 |
| Hindi | tool_ms | 0 | None | None |
| Hindi | network_ms | 0 | None | None |
| Hindi | end_to_end_ms | 10 | 1725.0 | 3328.0 |

Latency is reported per language and component. Missing components remain null rather than being inferred from end-to-end latency.

## Where this harness gives false confidence

- This measured set uses Retell audio calls, but six scenarios cannot represent the full range of accents, background noise, devices, or carrier conditions.
- Scripted callers are more cooperative and consistent than real callers.
- A successful tool trace does not prove natural voice timing or intelligible speech.
- Retell component timings depend on platform instrumentation and may overlap, so they must not be summed as independent stages.
- Exact-repeat detection misses paraphrased redundant questions unless turns are annotated with asked_for and provided_fields.
- Small language samples have wide uncertainty; English, Hindi, and code-switch results must not be blended.
- Separate mock-mode backend tests prove determinism but not Cliniko permissions or carrier behavior.
- A warm test endpoint understates free-tier cold-start latency.

# Placement comparison

Fixed-data correctness and serialized storage measurements, using the actual core and grouped selectors. All three contracts use the same chain, plan, parameters, threshold and backup setting. Every healthy boundary is compared with an independent oracle and checked for safe retirement. These are synthetic fixtures, not latency benchmarks or production size estimates.

Suite `full`: 93 cases, 558 complete healthy/fault traversals.

One-way is the default contract. Two-way and replay-by-recompute are optional. The one-way v1 envelope differs from the optional v2 envelopes, so their switch points and token sizes may differ. All logical states, rows and healthy selector work must match. The two optional contracts also have identical token byte lengths and placement decisions.

## Coverage

| Strategy | Healthy requests | Single-response cases | First-response spills | Cases returning inline | Cases re-spilling | Inline / nonterminal responses |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| one_way | 2775 | 0 | 81 | 0 | 0 | 58 / 2682 |
| two_way | 2775 | 0 | 81 | 8 | 4 | 84 / 2682 |
| recompute | 2775 | 0 | 81 | 8 | 4 | 84 / 2682 |

Inline fractions count nonterminal responses only; terminal responses have no continuation and do not count as inline. A terminal-only case has no inline fraction. Single-response cases are reported separately from transition coverage.

All 81 grouped cases spilled on response 1 in all three contracts and never returned inline. This finding concerns these explicit-reservation fixtures at 2/4/8 KiB, not every grouped workload.

## Core comparisons

Each cell compares the same input chain. Positive savings mean less storage than one-way; negative savings mean more. The two-cycle fixture uses ordinary coverage growth and retirement; it does not change a horizon.

| Fixture | Page | T bytes | Backup | One-way byte-pages | Two-way byte-pages | Recompute byte-pages | Recompute saving vs one-way |
| --- | ---: | ---: | --- | ---: | ---: | ---: | ---: |
| core_growth_control_600 | 50 | 2048 | True | 79210 | 80932 | 79968 | -758 (-0.96%) |
| core_growth_control_600 | 50 | 2048 | False | 54575 | 56796 | 55832 | -1257 (-2.30%) |
| core_growth_control_600 | 50 | 4096 | True | 58591 | 58611 | 54598 | 3993 (6.82%) |
| core_growth_control_600 | 50 | 4096 | False | 40337 | 41907 | 37894 | 2443 (6.06%) |
| core_growth_control_600 | 50 | 8192 | True | 0 | 0 | 0 | 0 (n/a) |
| core_growth_control_600 | 50 | 8192 | False | 0 | 0 | 0 | 0 (n/a) |
| core_two_growth_cycles | 50 | 2048 | True | 314661 | 319597 | 319707 | -5046 (-1.60%) |
| core_two_growth_cycles | 50 | 2048 | False | 194439 | 199375 | 199485 | -5046 (-2.60%) |
| core_two_growth_cycles | 50 | 4096 | True | 299341 | 298861 | 284734 | 14607 (4.88%) |
| core_two_growth_cycles | 50 | 4096 | False | 183603 | 188903 | 174776 | 8827 (4.81%) |
| core_two_growth_cycles | 50 | 8192 | True | 236541 | 221826 | 173698 | 62843 (26.57%) |
| core_two_growth_cycles | 50 | 8192 | False | 144263 | 152830 | 104702 | 39561 (27.42%) |

## Grouped comparisons and overhead

These fixtures have 200 groups of 12 members and explicit pending reservation IDs. The table separates first-response spills and later returns instead of assuming that every grouped workload behaves alike. The small-page rows select alternating-member subtract in all three coverage encodings; page 50 includes all eight distributions and policies.

| Page | T bytes | Cases | Recompute first spills | Recompute returns | Improve / worse / equal vs two-way | Saved vs one-way | Saved vs two-way | Added head metadata vs two-way | Payload removed vs two-way |
| ---: | ---: | ---: | ---: | ---: | --- | ---: | ---: | ---: | ---: |
| 1 | 4096 | 3 | 3 | 0 | 0 / 3 / 0 | -401067 | -147372 | 149466 | 2094 |
| 3 | 4096 | 3 | 3 | 0 | 0 / 3 / 0 | -133521 | -47862 | 50574 | 2712 |
| 7 | 4096 | 3 | 3 | 0 | 0 / 3 / 0 | -56589 | -18582 | 22530 | 3948 |
| 50 | 2048 | 24 | 24 | 0 | 15 / 9 / 0 | 41370 | 90864 | 30426 | 121290 |
| 50 | 4096 | 24 | 24 | 0 | 15 / 9 / 0 | 41370 | 90864 | 30426 | 121290 |
| 50 | 8192 | 24 | 24 | 0 | 15 / 9 / 0 | 41370 | 90864 | 30426 | 121290 |

A first-spill inline-input response can lose its full replay payload under recomputation. Later handle inputs still keep full responses, with additional replay metadata. If a chain never returns inline, the repeated metadata cost can exceed that one initial saving. `pages.csv` records every component at every response, including terminal heads. This explains negative small-page rows without treating them as a failed correctness check.

Across the 81 grouped cases, recompute improves 45 and worsens 36 compared with either one-way or full-response two-way. For the three page-size-1 cases, the comparison against full-response two-way has 1,215 response boundaries: 123 extra metadata bytes per boundary, plus 7 extra bytes on each compact first-spill head, totals 149,466 byte-pages. Removing 2,094 payload byte-pages leaves a net increase of 147,372. Dedicated-vector and return-bridge deltas are zero in those cases. The larger difference against one-way also includes the optional contract's additional coordination and envelope fields.

## Retry costs

The failure traversal retries every accepted request exactly once through a fresh service instance, including terminal responses: a deliberate 100% retry schedule, not a predicted retry rate. Inline requests with no accepted server head select again in every contract. After the first spill has been accepted, one-way and full-response two-way replay its stored response; recompute selects again from that original inline input and verifies the response digest. The same distinction applies to accepted managed inline inputs after a return. Handle-input retries replay the retained response.

| Strategy | Retry selector calls | Retry consumed | Retry fetched | Retry source reads |
| --- | ---: | ---: | ---: | ---: |
| one_way | 60 | 3500 | 3502 | 72 |
| two_way | 60 | 3500 | 3502 | 72 |
| recompute | 177 | 35369 | 35806 | 722 |

At switches, after returns, selected handle inputs and handle-to-terminal, separate phases inject pre-publication failure, concurrent preparation, lost accepted responses and a competing commit. Hot-state eviction exercises backup recovery. Missing-state and stale-input probes reject before selection. The JSON partitions this work from the one-retry costs above.

In the 8 KiB two-growth-cycle core case, recompute reduces dedicated-vector residency from 16 to 9 response boundaries versus one-way. With a retry after every accepted request, it adds 8 selector calls, 400 consumed/fetched candidates and 8 reference source reads relative to either other contract. Backup on/off does not change those retry counts.

## Meaning and limits

A byte-page is one byte retained at one response boundary, including terminal replay state. The report sums canonical records, not Python object sizes, RAM use or elapsed residency. `hot`, `backup`, `head_metadata`, `return_bridge_payload`, `other_response_payload` and `redirects` form a disjoint partition of total storage. The return bridge is the full handle-to-inline response retained until the next successful advance. Peaks, token bytes and dedicated-vector resident pages are included separately; releasing a vector does not by itself release replay metadata.

Two-way return can remove dedicated vectors when state shrinks sufficiently after spilling. Recomputation can also remove an inline-input response payload on the first spill. Whether either saves total storage depends on that payload, later metadata, backup policy and traversal length. Retry calls, consumed candidates, fetched IDs and source reads describe these adapters, not SQL queries, latency or production CPU. Recompute requires deterministic selection; digest verification does not create a snapshot or durable storage.

Reproduce: `python -S -B -m reference.placement.comparison --suite full`. Use `--workers N` to parallelize independent cases. Output must be a new directory. All generated outputs remain under `reference/results/` by default.

Rows in these adapters include the stage, representative ID and represented member IDs. An adapter returning representative IDs alone would change replay payload sizes. The fixture ID format also affects the break-even point; UUID and BIGINT measurements from separate local experiments are not part of this evidence.

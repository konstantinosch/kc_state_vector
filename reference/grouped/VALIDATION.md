# Grouped validation scope

This extension checks a fixed-data grouped result contract. It compares IDs,
spans and hybrids while holding the plan and reservation policy fixed. It makes
no latency, database-plan or production transport claim. See the
[usage and policy examples](README.md).

## Checks and their purpose

| Area | Evidence |
| --- | --- |
| Expected results | Sixteen hand-worked fixtures anchor the unpaged oracle; item-only translations also compare with the public core pager. |
| Codec | All 256 subsets of a small multigroup scope; full tuple bounds, reserved/interleaved holes, ties and rejection of malformed/overlapping descriptors. The auditor separately rejects overclaims about selected members. |
| Hybrid | Six focused fixtures; exact JSON cost including both bounds and group keys; ties favor IDs; fixed-span lifetime and byte/entry tradeoffs. |
| Selection | Full ordered rows and represented members, quotas, both partial policies, selective yielding, earliest-owner precedence and release/omit. |
| Retirement | Independently derived raw suffixes, future group expansions and pending ownership; fixed-span identity across pages. |
| Continuation | JSON round trips, fresh pagers, replayed requests, input immutability and empty work-limited pages. |
| Failures | Raised and incomplete reads injected at every observed read in a bounded fixture matrix, including late failures within a page. |
| Capacity | Exact-fit, one-below and raised-limit retries at every returned boundary in that matrix, across all three representations. |
| Mutations | Named pager/codec faults must fail semantic assertions after healthy controls pass. Invalid edits, errors, skipped tests and timeouts are not successful detections. |

The oracle does not call pager group-resolution/pruning helpers or the coverage
codec. The auditor independently evaluates wire spans and reconstructs ownership.
Package/provenance hash tests are excluded from semantic mutation checks: noticing
a changed file does not demonstrate detection of a wrong result.

## Bounded failure and capacity matrix

Two additional tests reuse 13 targeted/hybrid fixtures, all three coverage modes
and two schedules: page size 1 with a small work budget, and page size 50 with a
bounded work budget. Each matrix has 78 traversals and 579 request boundaries,
including 399 empty nonterminal responses. These small cases test request semantics;
the larger distributions separately exercise full 50-row pages.

The read-failure test injects 984 raised errors and 984 incomplete responses. Each
injection must actually fire, leave the supplied continuation unchanged and
produce the healthy result on both recovered-source and fresh-pager retry.

The capacity test checks 501 exact byte fits, 1,622 repeated failures below the
required byte or entry limit, 811 retries with raised limits and 78 terminal calls
with minimal positive limits. Entry limits match the required count when positive;
otherwise the test uses the minimum supported limit of 1. One-below entry tests
apply when the required count exceeds 1. These limits apply to the returned
continuation only; they do not
measure transient allocations or promise the core model's intermediate admission
contract for the grouped extension.

## Reproducible finite comparisons

The full three-mode runner uses 16 targeted plans, six hybrid fixtures, 600 seeded
generated plans and eight long reservation distributions. Short plans use three
schedules; long plans use page size 50, batch size 50 and a candidate budget of
1,000. The long inputs contain 200 groups × 12 members each.

Every comparison checks ordered oracle prefixes, the complete result, disjoint
represented members, progress, ownership transitions, consumed cursors and exact
fresh-pager replay. Spans retained by the span and hybrid modes must keep their
original bounds until retirement. Comparisons do not require the same historical coverage set
when individually retiring IDs and whole-span retirement differ safely.

These are finite generated cases, not an exhaustive proof of arbitrary grouped
plans or request histories. The core's exhaustive tiny-plan results cover the
core domain only. Report the seed and dimensions with any counterexample.

The runner reports coverage-only bytes separately from total continuation bytes.
Total state also contains pending/omitted IDs and metadata, so its peak can occur
before any row is selected. Every peak includes request number, cumulative rows
and full-page depth. Short synthetic IDs do not establish a universal ID/span
break-even; realistic wire-format and compressed-ownership experiments remain
separate local work.

## Export and CI boundaries

The public suite has 89 tests: all 72 existing grouped tests retained with public
package imports, eight export checks, seven mutation-tooling/witness tests and
the two boundary tests. The [provenance manifest](provenance.json) records source
hashes and adaptations. The separate mutation command checks ten named faults.
The original local grouped files and frozen core snapshot are not rewritten.

CI defines separate grouped jobs on Linux and Windows with Python 3.14, running
the ordinary suite and bounded mutation checks. The full comparison is an opt-in
command. Local public-only checkout checks passed; hosted results for these new
jobs await publication. Generated outputs and historical local traces are excluded from Git.
The portable skill remains a documentation package and does not acquire these
executable modules as part of this change.

## Remaining limits

- Fixed supplied group/episode membership; no inferred or changing horizons.
- Raw source reads only; no grouped prepared-buffer/refill composition.
- Explicit-ID pending/omitted reservations; no compressed ownership codec.
- Whole-scope in-memory metadata; no bounded lookup, RAM or engine execution claim.
- Trusted logical continuation; no authentication, access/hydration or concurrency protocol.

The hybrid's per-run byte choice is neither a globally minimal encoding nor a
query-cost optimizer. Required state is never evicted to force success. A capacity
failure is an explicit failure with unchanged input, not source exhaustion.

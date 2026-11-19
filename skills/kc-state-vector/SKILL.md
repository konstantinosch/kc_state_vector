---
name: kc-state-vector
description: Design, implement, or review state-vector pagination for overlapping ordered sources, staged feeds, reservations, or grouped results. Use when continuation needs overlap state beyond a simple cursor.
license: MIT
metadata:
  author: Konstantinos Chantzis
  version: "0.1.1"
---

# State-vector pagination

Apply the state vector and its transition rules to forward pagination over ordered,
composed results. The core records consumed progress and coverage still needed by
remaining scans, then retires that coverage only when safe. Client-held and
server-held state are implementation choices; only the client-held variant avoids
an application pagination session store.

Use the [bundled pattern](references/kc-state-vector.md) as the technical reference.
Start with [the small core](references/kc-state-vector.md#part-1--the-small-core), then
use [the reference model](references/kc-state-vector.md#part-2--reference-model),
including **Rules**, for the shared contracts. Read selected capabilities in Part 3
and relevant integration details in Part 4. Fields for unused capabilities are not
required. Locate sections by their Markdown headings when your file reader does not
follow fragment links. The package is self-contained and requires no particular
model, tool API, database or runtime.

## Establish the result before choosing machinery

Inspect the existing queries, indexes, continuation format and tests when available.
Work in the application's language and architecture. For a design or review request,
provide that analysis without starting an unrequested implementation. For an
implementation request, carry the chosen design through code and relevant checks.

Determine the facts that affect correctness:

- The list's shared scope and eligibility, item identity, stable sort tuple and
  tie-breaker. A source may be a simple index scan or a complex search query.
- Whether sources run as ordered stages or contribute to an ordered merge; where
  they overlap; which limits are stage selection quotas versus per-request work
  budgets; and what proves each source exhausted.
- Whether a selected row represents one item, a whole group, or a declared episode.
  Identify the bounded metadata lookup that establishes coverage if it is needed.
- Whether a future stage owns reserved items, and which earlier stages must yield.
- The consistency available from the actual sources, access revalidation policy,
  state placement, expiry and state-capacity policy.

Infer these from evidence where possible. State unresolved assumptions and ask only
for missing decisions that change the result or prevent safe progress. Do not turn
a worked example's engine, page size, horizon or index into a universal default.

If one ordered, ungrouped source with no composition meets the requirement, use an
ordinary keyset cursor. Moving sort keys, changing relevance, totals and random
page access may need another mechanism. Do not introduce optional capabilities
merely because the reference describes them.

## Design or check the continuation

Define the smallest vector sufficient for the chosen result: stage/completion
state, consumed cursors, selection counts where needed, and coverage still
reachable by remaining consumers. Add prepared buffers, reservations or group
state only for capabilities actually used. Bind the continuation to the server's
plan/version, authenticated reader and list scope.

For each retained field, establish what creates it, what updates it, and what
proves it can be discarded. Trace a page boundary where a later source encounters
an earlier selection. Preserve these invariants:

- **Consumed is not fetched.** Advance past candidates actually processed,
  including skipped duplicates; never advance past unread entries in a batch.
  A pushed-down exclusion must preserve any skip-driven transitions the selected
  semantics require, or those transitions must be reconstructed.
- **Decide before pruning.** Test the current candidate against applicable coverage
  before retiring that coverage. Earlier selections in the same batch already
  affect subsequent candidates. Unstarted stages remain consumers.
- **Coverage must mean represented items.** An ID, group span, tag or predicate is
  interchangeable only when it describes the correct set. A category with a finite
  priority quota does not imply that every item in that category was represented.
  Do not mark an entire group covered after serving only part of it.
- **Reservations are pending ownership.** Freeze them before affected scans; the
  owner bypasses its own reservation but still honors prior selected coverage.
  Resolve overlaps, grouped rows crossing ownership boundaries, and owner completion
  explicitly. Passing a reserved item in an earlier scan does not release it.
- **Progress and limits are explicit.** In the illustrated baseline, count selected
  rows against stage quotas; skipped candidates consume the applicable work budget.
  A deliverable-row quota needs its own explicit refill/transition contract. Record quota completion
  before returning, even when that selection fills the page. Short or empty output
  with continuation is valid; only proof of exhaustion or a spent stage quota ends
  a source. Incomplete reads are not proof of exhaustion.
- **Retain what exactness requires.** Prune only when all remaining applicable
  consumers cannot encounter covered items. A last-stage sweeper adds no exclusions
  only when it needs no internal duplicate suppression. Horizon expiry changes the
  declared result; arbitrary eviction changes guarantees. Do not silently drop
  required state to meet a cap or accept tentative progress on capacity failure.

For the shared plan and transition contract, consult
[What each stage declares](references/kc-state-vector.md#what-each-stage-declares),
[The result a plan defines](references/kc-state-vector.md#the-result-a-plan-defines)
and [How a page is built](references/kc-state-vector.md#how-a-page-is-built) in the
reference model. Its expanded schema and loop include optional hooks, not a
requirement to implement every capability.

Read each chosen capability's full contract before implementing it; the following
links also point to relevant representation and optimization guidance:

| When needed | Reference sections |
| --- | --- |
| Coverage representation or compression | [Exclusions describe sets of items](references/kc-state-vector.md#exclusions-describe-sets-of-items); [Optimization - compact exclusions](references/kc-state-vector.md#optimization--compact-exclusions) |
| Preparation, buffering or future ownership | [Prepared sources and refill](references/kc-state-vector.md#prepared-sources-and-refill); [Reservations for a later stage](references/kc-state-vector.md#reservations-for-a-later-stage) |
| Group rows, episodes or horizons | [Grouped results](references/kc-state-vector.md#grouped-results--one-row-per-group), including gap/count horizons and safe head checks |
| Merging several ordered sources | [Examples](references/kc-state-vector.md#examples), especially Example 4; [Deterministic ordered stages](references/kc-state-vector.md#deterministic-ordered-stages) |

## Adapt to the sources and delivery protocol

Keep candidate selection about IDs, stable positions and necessary bounded grouping
metadata. Grouping need not be embedded in the candidate query or its index. Prefer
index-compatible keyset queries and bounded merges when the engine supports them;
choose where to apply exclusions from the engine's execution plan and measured
cost. Application candidate limits do not bound hidden engine scanning or grouping
lookup work. Avoid promising a latency target or constant state size without evidence.

Prefer completing page selection before bulk payload hydration, batching the IDs
per relation or service. This is a default, not a mandatory hydration schedule.
Recheck current access for every delivered item and group member. Missing or revoked
items can shorten the delivered page. Under selected-row quotas, keep selection
counters and coverage consistent; a delivery miss does not refund a slot. If refill
or deliverable-row quotas are required, define their bounded extra selection and
hydration rounds and their effects on coverage and stage completion. An unknown
read or authorization result is not an absent item.
Presentation order must not change selection cursors or coverage.

For client-held state, validate its binding, integrity, schema, versions and bounds;
account for final encoded size. For server-held state, define progress identity,
concurrency, response-loss retries (including the terminal page), retention and
missing-state behavior. The reference's mutable, single-chain protocol uses atomic
revision advancement and retained replay outcomes. If older vectors are retained or
reconstructed for retries or branches instead, specify that contract and its lifetime.
Replacing a stored vector blindly is insufficient. Neither a token nor a handle grants
access, and neither storage location creates a snapshot. Follow the selected protocol
when accepting progress after hydration and other failures; do not invent a silent
restart or pretend a replay is new progress.

Claim exact traversal only under the reference's fixed-data, fixed-eligibility,
stable-order and sufficient-state conditions. A visibility watermark requires all
eligible writes through it to be committed and visible to every relevant source;
an insertion sequence or timestamp alone does not establish that. Identify the
actual live-data limitations when these conditions cannot be met.

Consult [Part 4 - Integration and reference](references/kc-state-vector.md#part-4--integration-and-reference)
for the selected storage protocol, access/hydration, capacity, source-query and
operating details. The staged-feed core has implementation provenance; several
extensions are proposed generalizations, not production guarantees.

## Validate the supported plan and report the result

For implementation work, use the project's test framework and small synthetic
fixtures before performance work. Compute an expected unpaged result independently
of the candidate loop, according to the declared plan. Compare concatenated
selections before presentation, under fixed data and sufficient state capacity.
Vary page size (including 1 and changes mid-chain), batch size and candidate budgets;
follow empty pages while continuation remains. Do not use the implementation itself
to generate its expected output.

Exercise overlaps, ties, quota boundaries and incomplete reads. Add reservation,
group/horizon, capacity and protocol cases only for capabilities being used. Test
declared approximate or horizon policies against their own semantics. Keep live-data
mutation and access checks distinct from fixed-data exactness. See
[Testing](references/kc-state-vector.md#testing) for the relevant adversarial and
retry cases. Passing a model-based fixture does not establish an actual source
adapter's visibility, ordering or performance contract.

For a design, explain the selected plan, vector, transitions, assumptions and needed
validation. For an implementation, deliver the changes and report the checks actually
run. For a review, provide concrete failure traces or supported findings. Distinguish
demonstrated behavior, source assumptions and unresolved issues; do not claim tests
or integrations that were not performed.

## Source and maintenance

Pattern by Konstantinos Chantzis: [kc-state-vector](https://github.com/konstantinosch/kc_state_vector).
The bundled reference and [MIT license](LICENSE) accompany this skill so it can be
used without fetching documentation. Update the reference from the canonical pattern
when maintaining the package, then recheck this workflow and its links against it.

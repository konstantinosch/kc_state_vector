# Self-check examples and expected results

These are small fixed-data examples for the bundled checker. The fixture format stays independent
of the candidate pager. Expected results are written here, not passed to an adapter by the checker.
The packaged oracle computes them separately when execution is available.

The profile sorts each stage by descending `(sort_key, id)`: integer sort keys and printable ASCII
string IDs. IDs remain strings, including `2` and `10`. Each stage spends its quota on selected
rows, skips IDs already selected by earlier stages, and leaves unselected members available to
later stages. Ascending order, timestamps and additional sort columns need an explicit adapter
mapping that preserves order, ties and identity; these fixtures do not verify that mapping.

## Complete expected selections

Each entry is `stage:id`; `EMPTY` means no selected rows. The required tier compares the ID
sequence. An adapter declaring `stage_labels` also has to match the stage sequence.

| Fixture | Complete expected selection |
| --- | --- |
| [priority_51_49_48](../scripts/fixtures/priority_51_49_48.json) | `priority:51, priority:49, all:52, all:50, all:48` |
| [full_tuple_ties](../scripts/fixtures/full_tuple_ties.json) | `priority:z, priority:m, all:a, all:negative` |
| [decimal_id_ties](../scripts/fixtures/decimal_id_ties.json) | `all:2, all:10` |
| [empty_duplicate_page](../scripts/fixtures/empty_duplicate_page.json) | `first:A, first:B, all:C` |
| [three_overlapping_stages](../scripts/fixtures/three_overlapping_stages.json) | `first:A, first:B, middle:C, rest:D, rest:E, rest:F` |
| [unread_batch_tail](../scripts/fixtures/unread_batch_tail.json) | `all:A, all:B, all:C, all:D` |
| [empty_plan](../scripts/fixtures/empty_plan.json) | `EMPTY` |
| [empty_sources](../scripts/fixtures/empty_sources.json) | `EMPTY` |
| [zero_quotas](../scripts/fixtures/zero_quotas.json) | `all:A, all:B` |
| [all_zero_quotas](../scripts/fixtures/all_zero_quotas.json) | `EMPTY` |

## Walking the priority example

Use page size 3, batch size 3 and candidate budget 50 for the full-capability sample.

| Request | Selected rows | Reason / continuation |
| --- | --- | --- |
| 1 | `priority:51, priority:49, all:52` | Priority spends its two slots. All-items then selects 52. Its cursor is at 52 and coverage still includes 51 and 49. |
| 2 | `all:50, all:48` | All-items skips 51, selects 50, skips 49, then selects 48. All sources are complete; continuation is null. |

The first request consumes three candidates; the second consumes four. Item 48 did not receive
a priority slot, so excluding the whole priority category would lose a valid result. Coverage
for 51 or 49 can retire after its current duplicate is decided and no later source can reach it.

## Empty pages are not exhaustion

For `empty_duplicate_page`, use page size 2, batch size 3, and candidate budgets 2, 1, 1, 1:

| Request | Consumed | Selected rows | Continuation |
| --- | --- | --- | --- |
| 1 | first-stage A and B | `first:A, first:B` | non-null |
| 2 | all-stage A, already covered | none | non-null |
| 3 | all-stage B, already covered | none | non-null |
| 4 | all-stage C | `all:C` | null |

This exact work/page trace belongs to the optional `candidate_budget` raw-candidate profile.
A required-tier adapter may have different page boundaries, but must eventually select A, B, C
in that order and stop only when complete. An empty page with non-null continuation is followed.

## Other short walkthroughs

With page size 2 and sufficient work budget, the sample emits:

- `full_tuple_ties`: `[priority:z, priority:m]`, then `[all:a, all:negative]`. Input array order
  does not replace tuple ordering. A cursor containing only the sort key would lose tied IDs.
- `decimal_id_ties`: `[all:2, all:10]`. Both keys are 7; string `2` sorts above string `10`.
- `three_overlapping_stages`: `[first:A, first:B]`, `[middle:C, rest:D]`, `[rest:E, rest:F]`.
  Skipping B in the middle stage does not spend its one selection slot.
- `zero_quotas`: `[all:A, all:B]`. The zero-quota stage selects nothing and reserves nothing.
- `empty_plan`, `empty_sources`, `all_zero_quotas`: one empty terminal page in the sample.

For `unread_batch_tail`, use page size 1, batch size 3 and candidate budget 50. The sample fetches
A/B/C but consumes only A on request 1. Requests 2, 3 and 4 select B, C and D. Advancing the cursor
to the first fetched batch's last item C would wrongly make request 2 select D.

## Reviewing without Python execution

If the host cannot execute Python, state that no executable check ran. Read a fixture, construct
its complete selection using the rule above, then trace the candidate's pages against this table.
Record its stage, consumed cursor and coverage at each boundary where they are inspectable.
Follow empty continuations, retain tied IDs, and confirm 48 survives the priority quota.
For a required-tier interface, inspect IDs and continuation without inventing work counters or
stage labels it does not expose. Compare a retry with the original request without appending its
rows twice. Report observed mismatches and any unverified capabilities.

A manual walkthrough can reveal a counterexample. It is not an executable pass, a completed
mutation run, a performance result or a substitute for running the kit in an execution-capable
host before claiming its automated checks passed.

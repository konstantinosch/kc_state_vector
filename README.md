# kc-state-vector

A pagination pattern for ordered lists, search results and grouped views whose sources can overlap. **The core is a state vector and the rules for advancing it:** enough state to pause a composed query plan after a page and resume later. The client, the server or a hybrid of the two can hold that vector.

This grew out of a feed I was building. I wanted to give some items priority, then fill the page from a broader query. The interesting part came on page two: some rows the broader scan hadn't reached yet had already appeared in the priority section.

[Read the full pattern in **kc-state-vector.md**](./kc-state-vector.md).

## Fit and limits

Use this for forward traversal of overlapping sources in a stable total order, with a tie-breaker such as `(sort_key, id)`. One ordered, ungrouped source usually needs only a keyset cursor. Changing relevance or sort keys, random page access and totals need other mechanisms.

Exact traversal requires fixed eligibility and order, consistent identities, complete source responses and retention of necessary overlap state. To exclude later arrivals, a visibility watermark must cover committed, visible writes; an insertion sequence alone is insufficient. No storage choice freezes data or grants access.

Exact mode reports a capacity error if required state cannot fit. Eviction is approximate: repeats can consume finite stage budgets and displace distinct results. Follow continuation even after an empty page caused by a scan limit. See [scope and guarantees](./kc-state-vector.md#scope-and-guarantees).

## Two pages of items

Imagine a list that serves two items from a priority category, then fills from all items. Items #48 through #52 have increasing sort positions; both sources scan in descending order. The priority category contains **#51**, **#49** and **#48**. A page holds three items.

| Page | Items you see | How the server fills it |
| --- | --- | --- |
| 1 | **#51, #49, #52** | The priority stage serves its two items. The all-items scan starts at #52 and fills the third slot. |
| 2 | **#50, #48** | The scan resumes below #52. It skips #51, serves #50, skips #49, then serves #48. |

After page one, the vector records that the priority stage is finished, the all-items scan has reached #52, and that scan must still skip #51 and #49. **#48 is why the distinction matters:** it belongs to the priority category but never received a priority slot. A blanket “exclude the priority category” filter would lose it. Explicit IDs record which items actually received those slots.

As the remaining scan passes #51 and #49, their entries can leave the vector: no remaining scan can encounter them again.

The small core consumes candidates in stage order, records consumed positions including skips, retains coverage while remaining scans need it, and safely retires it. Grouping and reservations are optional extensions.

## Placement and evidence

**Client-held** continuation carries an authenticated vector; **server-held** continuation carries a handle and revision. [Hybrid placement](./kc-state-vector.md#hybrid-placement) defaults to one-way inline-or-spill: start inline, then stay server-held after exceeding the token limit. Two-way return and replay by recomputation are optional and retain coordination state. Only the fully client-held variant avoids an application session store. See [where the vector lives](./kc-state-vector.md#where-the-vector-lives).

The [placement findings](./reference/placement/FINDINGS.md) show that returning inline can reduce dedicated-vector storage, while replay metadata can outweigh payload savings. The tested reservation-heavy grouped fixtures spilled on their first response. These are serialized-storage measurements on fixed synthetic fixtures, not performance guarantees or production size estimates. Recomputing retries adds selector work and requires repeatable selection.

## Reading and using the pattern

This README is the introduction; the full document holds the mechanism and integration detail:

1. [The small core](./kc-state-vector.md#part-1--the-small-core): minimal state, page loop and guarantees.
2. [Reference model](./kc-state-vector.md#part-2--reference-model): rules, contracts, schema and expanded trace.
3. [Optional capabilities](./kc-state-vector.md#part-3--optional-capabilities): prepared sources, reservations, grouping, horizons and merged sources.
4. [Integration and reference](./kc-state-vector.md#part-4--integration-and-reference): storage, queries, hydration, capacity, retries and operations.

Executable references cover the [core and explicit-ID reservations](./reference/README.md), [grouped coverage](./reference/grouped/README.md) and [placement protocols](./reference/placement/README.md), with independent oracles, retirement checks and bounded validation scopes. Run `python -m reference --example core --page-size 3` for the example above.

This publication provides a design, worked examples and implementation guidance, composing existing keyset or `search_after` cursors, stages or merged sources, and safely pruned overlap state. Query examples use PostgreSQL and Elasticsearch/OpenSearch. Implementations must validate correctness and performance against their own workloads.

The original application demonstrated the staged-feed core. Server-held continuation, group episodes, merged sources, visibility watermarks and operational extensions are proposed generalizations beyond that application. The executable references now check bounded fixed-data grouping and placement contracts. Dynamic count horizons, including D1, remain proposed and locally explored; the published references do not validate them.

Reports of duplicates or skipped rows under the stated exact-traversal conditions are welcome, ideally with the smallest reproducing sequence of pages.

A [portable agent skill](./skills/kc-state-vector/SKILL.md) bundles the pattern and a [stdlib self-check kit](./skills/kc-state-vector/references/self-check.md) for fixed-data, ungrouped staged pagers. The kit provides an independent oracle, synthetic fixtures and checks for ordering, overlap, quotas, continuation and retries.

A local follow-through evaluation used 12 authored prompts, three fresh samples per prompt, and one Codex model to review scope decisions and generate staged-core pagers checked with the kit. Inputs explicitly required reading the full skill and included a word target. There was no skill-off control or Claude-model run, so this does not isolate the skill's contribution or establish production guarantees.

Build the skill with `python skills/build_skill.py`; [CI](./.github/workflows/checks.yml) checks links, examples, metadata, bundled copies and reproducible packaging. Generated archives stay local.

## Acknowledgements

The staged-feed core grew out of a project for [Πράξη Αγάπης (Praxi Agapis)](https://praxiagapis.gr/el/home). I'm grateful for the opportunity to develop it through that work, and for their permission to publish this generalized design.

## Author and license

Konstantinos Chantzis · [MIT License](./LICENSE)

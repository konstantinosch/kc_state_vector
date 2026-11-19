# kc-state-vector

A pagination pattern for ordered lists, search results and grouped views whose sources can overlap. **The core is a state vector and the rules for advancing it:** enough state to pause a composed query plan after a page and resume later. The client or the server can hold that vector.

This grew out of a feed I was building. I wanted to give some items priority, then fill the page from a broader query. The interesting part came on page two: some rows the broader scan hadn't reached yet had already appeared in the priority section.

[Read the full pattern in **kc-state-vector.md**](./kc-state-vector.md).

## The small core

Start with overlapping sources in a stable total order, including a tie-breaker such as `(sort_key, id)`. The sort key need not be a timestamp. The smallest useful version needs four things:

1. Consume candidates in the order defined by the stages.
2. Remember how far each source was consumed, including skipped candidates.
3. Retain coverage for selected items another remaining scan could encounter.
4. Forget that coverage only when no remaining scan needs it.

Grouping, reservations and other capabilities extend this mechanism; they are not prerequisites.

## Two pages of items

Imagine a list that serves two items from a priority category, then fills from all items. Items #48 through #52 have increasing sort positions; both sources scan in descending order. The priority category contains **#51**, **#49** and **#48**. A page holds three items.

| Page | Items you see | How the server fills it |
| --- | --- | --- |
| 1 | **#51, #49, #52** | The priority stage serves its two items. The all-items scan starts at #52 and fills the third slot. |
| 2 | **#50, #48** | The scan resumes below #52. It skips #51, serves #50, skips #49, then serves #48. |

After page one, the vector records that the priority stage is finished, the all-items scan has reached #52, and that scan must still skip #51 and #49. **#48 is why the distinction matters:** it belongs to the priority category but never received a priority slot. A blanket “exclude the priority category” filter would lose it. Explicit IDs record which items actually received those slots.

As the remaining scan passes #51 and #49, their entries can leave the vector: no remaining scan can encounter them again.

## Conditions and limits

Exact traversal requires fixed eligibility and order, complete visibility through the anchor, consistent identities and retention of all necessary overlap state. Exact mode reports a capacity error if required state cannot fit. Eviction is approximate; repeats can consume finite stage budgets and displace distinct results. Follow continuation even after an empty page caused by a scan limit.

A visibility watermark excludes later arrivals only when every eligible write through it is committed and visible to the relevant sources. An insertion sequence alone cannot guarantee this. Neither storage choice freezes data or grants access; access is checked again on each request.

One ordered, ungrouped source without overlap usually needs only a keyset cursor. Changing relevance or sort keys, random page access and totals need other mechanisms. See [scope and guarantees](./kc-state-vector.md#scope-and-guarantees).

## Reading and using the pattern

**Client-held** continuation carries the vector in an authenticated token. **Server-held** continuation carries a handle and revision; the server retains the vector and coordinates retries. Only the client-held version avoids an application pagination session store. See [where the vector lives](./kc-state-vector.md#where-the-vector-lives).

The full document separates the mechanism from its extensions:

1. [The small core](./kc-state-vector.md#part-1--the-small-core): the minimal state, page loop and guarantees.
2. [Reference model](./kc-state-vector.md#part-2--reference-model): rules, fields, stage and result contracts, schema, expanded loop and trace.
3. [Optional capabilities](./kc-state-vector.md#part-3--optional-capabilities): prepared sources, [reservations](./kc-state-vector.md#reservations-for-a-later-stage), [grouped results](./kc-state-vector.md#grouped-results--one-row-per-group), horizons and merged-source examples.
4. [Integration and reference](./kc-state-vector.md#part-4--integration-and-reference): storage, source queries, bulk hydration, capacity, retries, testing and operations.

This publication provides a design, worked examples and implementation guidance, composing existing keyset or `search_after` cursors, stages or merged sources, and safely pruned overlap state. Query examples use PostgreSQL and Elasticsearch/OpenSearch. Implementations must validate correctness and performance against their own workloads.

The original application demonstrated the staged-feed core. Server-held continuation, group episodes, merged sources, visibility watermarks and operational extensions are proposed generalizations. Reports of duplicates or skipped rows under the stated exact-traversal conditions are welcome, ideally with the smallest reproducing sequence of pages.

## Acknowledgements

The staged-feed core grew out of a project for [Πράξη Αγάπης (Praxi Agapis)](https://praxiagapis.gr/el/home). I'm grateful for the opportunity to develop it through that work, and for their permission to publish this generalized design.

## Author and license

Konstantinos Chantzis · [MIT License](./LICENSE)

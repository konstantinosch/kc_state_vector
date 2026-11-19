# kc-state-vector

A pagination pattern for ordered lists, search results and grouped views with a stable total order. The sort key need not be a timestamp; it needs a stable tie-breaker. **The core is a state vector and the rules for advancing it:** together they record enough of a composed query plan to pause after a page and resume later. The client or the server can hold that vector.

This grew out of a feed I was building. I wanted to give some items priority, then fill the rest of the page from a broader query. The interesting part came on page two. The broader query had a perfectly good cursor, but some of the rows the scan hadn't reached yet had already appeared in the priority section.

That application demonstrated the staged-feed core; the broader design also includes proposed generalizations, identified below.

[Read the full pattern in **kc-state-vector.md**](./kc-state-vector.md).

## The small core

Start with ordered sources that can overlap. The smallest useful version needs four things:

1. Consume candidates in the order defined by the stages.
2. Remember how far each source was consumed, including skipped candidates.
3. Retain coverage for selected items that another remaining scan could encounter.
4. Forget that coverage only when no remaining scan needs it.

That is enough for the two-stage example below. Grouping, reservations and the storage choices later in this README build on this mechanism; adopting the pattern does not require every capability.

## Two pages of items

Imagine a list that serves two items from a priority category first, then fills the remaining slots from all items. Items #48 through #52 have increasing sort positions; both sources scan in descending order. The priority category contains **#51**, **#49** and **#48**. A page holds three items.

| Page | Items you see | How the server fills it |
| --- | --- | --- |
| 1 | **#51, #49, #52** | The priority stage serves its two items. The all-items scan starts at the highest position, #52, and fills the third slot. |
| 2 | **#50, #48** | The scan resumes below #52. It skips #51, serves #50, skips #49, then serves #48. |

At the end of page one, the server has three facts worth passing on. The priority stage is finished, the all-items scan has reached #52, and that scan still has to skip #51 and #49. **#48 is why the distinction matters.** It belongs to the priority category too, but it never got a priority slot. If the list should show every item once, a blanket “exclude the priority category” filter would quietly lose it. Explicit IDs record which items actually received priority slots.

Those facts form the **state vector**. As the scan passes #51 and #49, their entries can drop out of the vector. Nothing further down this list can match them again. The same selection and pruning rules apply wherever the vector is stored.

Here the final scan acts as a **sweeper**: it needs no exclusions for its own output, so it only consumes and prunes the exclusions inherited from earlier stages. This applies when the final stage needs no internal duplicate suppression.

## Conditions for exact traversal

Exact traversal requires fixed eligibility and order, complete visibility through the anchor, and retention of all necessary overlap state. If that state exceeds its configured capacity, whether a token-size limit or server storage budget, exact mode reports an explicit capacity error or uses a separately documented fallback. Evicting entries is approximate: repeats can consume finite stage budgets and displace distinct results. Follow the continuation even when a scan limit produces an empty page.

A visibility watermark can keep later arrivals out of an ongoing scroll: every eligible write through it must already be committed and visible to the relevant sources. An insertion sequence alone cannot provide that guarantee. Existing rows can still change, and access is checked again on each request.

A single ordered list with no overlap can usually get by with an ordinary keyset cursor. Changing relevance scores or sort keys need a stronger consistency mechanism; this pattern alone does not provide random page access or totals.

## Optional capabilities

Add a capability when the result you want needs it. Each adds rules to the same continuation mechanism; the plain two-stage example requires neither of the following.

### Reserving items for a later stage

Sometimes a later stage should keep its own items. Suppose the first stage scans `[A, B, C]` and has two slots, while the second stage owns a prepared shortlist `[B]`. Reserving B before the first scan makes the first stage return **A, C**, then the second returns **B**.

This is a **reservation**, not evidence that B has already appeared. The plan says which stages must yield to the reservation; its owner can still select B. Once B is selected, ordinary duplicate suppression takes over. Preparing a list alone does not reserve it: without that policy, the earlier stage may take B. The full pattern explains [reservation ownership and lifetime](./kc-state-vector.md#reservations-for-a-later-stage).

### What changes for grouped results?

Say an application defines groups of related items and returns one row per group. The candidate query still returns item IDs; the stage resolves group membership and decides what the row represents. If that row covers the group's matching items, another member encountered several pages later is already represented.

If the row accounts for **all** items in that group that match the filters, anchor and viewer's access, the vector can remember one entry, `(group_id, lo, hi)`. The bounds are the lowest and highest matching positions. A descending scan can stop checking the entry once it moves past `lo`; the vector retains it until no remaining scan can encounter that group's span. When only part of a group has been served, recording the whole span would hide items the reader still ought to see.

## Integration choices

Once the result rules are clear, choose how to retain the vector and deliver each selected page.

### Where the vector lives

| Variant | What the client sends | What the server retains between requests |
| --- | --- | --- |
| **Client-held** | A signed token containing the continuation state | No application pagination session is required; a compatible server can validate and resume the token. |
| **Server-held** | A small opaque handle identifying the stored continuation and its revision | The vector, its expiry and enough transition history to handle retries safely. |

**Stateless describes the client-held version, not the core mechanism.** It avoids an application session store by carrying state over the network. Server-held state keeps client tokens small but requires memory or storage, lifecycle management and coordination: replacing a vector blindly can skip a page when a response is lost and the client retries.

The detailed worked loops use the client-held version. The main document explains [where the vector lives](./kc-state-vector.md#where-the-vector-lives), including server-held transitions and the tradeoffs between computation, retained state and communication. Neither location freezes data or grants access.

### Using the pattern

Start with the order each source can actually deliver, including a stable tie-breaker such as `(sort_key, id)`. Keep the plan on the server. Retain the current stage, the position each scan has consumed, and the overlap state and reservations still needed by the plan. Prefer fetching the details for all selected IDs together after page selection finishes.

## Reading the full pattern

The full document follows the same path:

1. **Core first:** [the smallest useful version](./kc-state-vector.md#the-smallest-useful-version) and [the core page loop](./kc-state-vector.md#the-core-page-loop).
2. **Optional capabilities:** [choose the result rules you need](./kc-state-vector.md#optional-capabilities-at-a-glance), then read their examples and contracts.
3. **Integration and reference:** [state placement](./kc-state-vector.md#where-the-vector-lives), source queries, hydration, capacity, retries and operating guidance.

The reference material includes [the full vector fields](./kc-state-vector.md#what-the-vector-carries), [the extended page-building loop](./kc-state-vector.md#how-a-page-is-built), and [worked examples](./kc-state-vector.md#examples), including a result merged from three sources.

This publication contains the pattern's design, worked examples and implementation guidance. The contribution is the composition of existing techniques: keyset or `search_after` cursors, ordered stages or merged sources, and overlap state that can be forgotten when no remaining scan can encounter it. The query examples use PostgreSQL and Elasticsearch/OpenSearch. Implementations must validate correctness and performance against their own sources and workloads.

The original application demonstrated the staged-feed core. Server-held continuation, group episodes, merged sources, visibility watermarks and several operational features are proposed generalizations beyond that implementation.

If you find a case that produces a duplicate or skips a row under the stated exact-traversal conditions, I'd love to see the smallest sequence of pages that reproduces it.

## Acknowledgements

The staged-feed core of this pattern grew out of a project for [Πράξη Αγάπης (Praxi Agapis)](https://praxiagapis.gr/el/home). I'm grateful for the opportunity to develop it through that work, and for their permission to publish this generalized design.

## Author and license

Konstantinos Chantzis · [MIT License](./LICENSE)

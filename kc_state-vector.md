# The state vector — stateless continuation for ordered, composed result streams

A design pattern for paging through ordered results composed from several sources or grouped
into rows, with continuation state carried by the client. Sources return `(sort key, id)` positions
in a stable total order; grouping keys can be resolved separately from those IDs. The mechanism
composes keyset scans, overlap tracking and continuation
tokens; it does not introduce a new pagination primitive.

This is a design document, developed from a staged-feed implementation. Group episodes, merged
sources and the operational extensions below are proposed generalizations. The publication
provides the mechanism, worked examples and implementation requirements. Implementers must
validate the supported plans against their own source adapters and workloads.

### Scope and guarantees

An exact traversal requires fixed eligibility and sort positions, consistent identities across
sources, and all necessary overlap state retained until it is safe to forget. A completed chain
then follows the result defined by its plan, in canonical selection order. Presentation may
reorder each page afterwards.

To exclude arrivals reliably, the anchor must be a **visibility watermark**: every eligible write
through that boundary is already committed and visible to the relevant sources. A sequence
allocated at insertion, a timestamp, or a settling delay alone does not establish that condition.
An anchor also does not freeze later edits, deletions or access changes.

State size depends on overlapping items and groups still reachable by remaining scans, not just
the page size. Exact mode stops with an explicit capacity error if required state cannot fit;
continuing by evicting required exclusions is an approximate policy. Repeats can then consume
finite stage budgets and displace distinct results. Time or count horizons deliberately defined
by the plan are separate result semantics, not an invisible storage optimization.

Use this for forward traversal with stable sort keys. Relevance rankings that change, random page
access and exact totals need other mechanisms. A single ungrouped source usually needs only an
ordinary keyset cursor. The detailed limits are in Part 4.

- **Part 1 — The mechanism:** what it is, what the vector carries, the vector as an object, the
  page loop, and how the vector evolves across stages over many pages.
- **Part 2 — Grouped results:** one row per group — groups that enter the vector big and are
  consumed, a horizon that makes plain and grouped lists one parameter, and the case where nothing
  needs carrying.
- **Part 3 — Uses:** deterministic ordered stages, and the shapes it fits.
- **Part 4 — Implementing it:** the decisions the pattern leaves to the implementation — the
  token, plan changes, access, positions, the cap, declared exceptions, testing, operating it —
  and when not to use it.
- **Examples** — at the end.

Rule numbers refer to Part 1, "Rules".

---

## Part 1 — The mechanism

### What it is

**A client-held continuation for a server-defined query plan.** The server keeps the plan — the
sources, their order, their budgets — as code. The client carries the plan's execution state
between pages, as one signed token. Nothing about the session lives on the server, and
nothing about the plan lives in the client.

It is one pattern in three layers, and it needs all three:

| Layer | What it is | What it decides |
|---|---|---|
| **Protocol** | the contract between client and server: an opaque token, returned unchanged, versioned, signed, strictly validated | how a session survives without server state |
| **Structure** | the vector: anchor, cursors, exclusions with expiry positions, prepared lists, counters | what must be remembered, and its bounds |
| **Algorithm** | the page loop: staged or merged sources, skip, prune, emit | the cost of a page |

An algorithm alone would need the server to remember where it stopped; a structure alone does
nothing; a protocol alone is an empty envelope. Together they make a query plan that can be
**suspended after any page and resumed on any server**.

**Every part of the vector represents a plan operator's continuation state.** The supported
composition is ordered scans, concatenation or sorted merge, de-duplication, filters and limits,
with explicitly bounded prepared buffers. Arbitrary joins, aggregation or re-sorting are not
covered by this description; an adapter must establish its own order and resumable state.

| In the vector | The operator whose state it is |
|---|---|
| cursor per source | the position of an ordered index scan (keyset / seek) |
| stage | which child of an ordered concatenation (`UNION ALL` in order) is running |
| merge heads | the heads of a sorted merge (Merge Append) |
| exclusions with expiry | de-duplication state: an entry can be removed once no remaining scan can encounter what it represents |
| predicate exclusions | a pushed-down `NOT` filter that replaces anti-join state entirely |
| budgets and counters | a `LIMIT` per child |
| prepared lists | a complete bounded source or a refillable buffer for a later scan |
| anchor | a visibility boundary (`seq ≤ anchor`) when all eligible writes through it are already visible; it does not freeze attributes |
| presentation | a projection after the plan, outside its state |

**The intended properties, with their conditions:**

- **No application session store.** Any compatible server can resume a valid token. Token expiry,
  key rotation and support for plan versions still need managing (Part 4).
- **Incremental selection.** A keyset scan resumes at its consumed position rather than using
  an offset. The page loop can bound candidates it consumes, but filters pushed into the engine,
  metadata queries, preparation and hydration have separate costs ("Cost", below).
- **Exact result while the assumptions hold.** With fixed data and eligibility, complete source
  visibility through the anchor, deterministic plan transitions and necessary exclusions retained,
  each result row defined by the plan is selected once. A grouped row represents its declared
  group or episode; a count horizon may intentionally allow a later row for the same group.
- **Stable against later arrivals.** A valid visibility watermark excludes subsequent arrivals.
  A raw insertion sequence only excludes larger sequence values; an older uncommitted write can
  still become visible later. A refresh starts a new chain.
- **Deterministic continuation.** The same vector, plan, request page size and data give the same
  selected page. With fixed result semantics, changing page size changes page boundaries, not the
  concatenated selection. Cap and horizon transitions must not depend on serialization boundaries.
- **Live data remains live.** Sort-key, attribute, membership and permission changes can cause
  omissions or repeats. Stronger consistency needs versioned reads, a materialized result or an
  engine snapshot. Freeze reader-specific selection parameters at the first page; recheck actual
  access on every request and at hydration.
- **Authenticated and validated state.** Verify the token and its request binding, then validate
  its schema before querying. A token is continuation state, never permission to read an item.

**Where it stands among known techniques.** Related techniques include keyset pagination,
[`search_after`](https://www.elastic.co/docs/reference/elasticsearch/rest-apis/paginate-search-results),
[Cosmos DB continuation tokens](https://learn.microsoft.com/cosmos-db/query/pagination),
[watermark-based deduplication](https://spark.apache.org/docs/latest/streaming/apis-on-dataframes-and-datasets.html#streaming-deduplication),
[per-key session windows](https://beam.apache.org/documentation/programming-guide/#session-windows),
and [opaque, request-bound page tokens](https://google.aip.dev/158). These are comparisons for
resuming execution, retiring state and defining episodes; their guarantees differ. A search PIT
retains engine-side snapshot state, and streaming watermarks rely on lateness assumptions rather
than proof that every remaining scan has passed an entry. The gap boundary here is explicitly
`gap >= H`, not an assertion about every session-window implementation.

This pattern describes their composition into staged or merged sources with overlap tracking,
predicate exclusions, prepared sources and a validated portable token. Page size bounds output
rows; candidate work, retained state, source count and group hydration need their own bounds.

### The problem it solves

A client walks a long, ordered result one page at a time — a **chain** of requests. A single cursor
does not describe every composed result; additional state may be needed when:

- **The data moves underneath.** Items arrive while someone is paging, and pages shift.
- **The result is composed.** Several sources fill one list — in a fixed priority, or merged — and
  the same item can come from more than one of them.
- **The result is grouped.** One row stands for a group of items, and the group's other items
  must not come back as rows of their own further down.

An application session can retain continuation state on the server. The state vector puts that
state in the request instead. Neither location alone establishes a consistent view of changing
data; the visibility and mutation conditions still apply.

### What the vector carries

| Part | What it is | Bound |
|---|---|---|
| **version** | the format's version, so it can change without breaking chains in flight | — |
| **anchor** | the boundary fixed when the chain starts; strict arrival stability requires a visibility watermark, not merely an allocated sequence or timestamp | one value, or one per source (Example 4); separate watermarks do not imply one transactional snapshot |
| **stage** | which source is being served (the ordered list of sources is the server's, not carried). Sources can also be **merged** instead of staged: each page then takes the newest head across their cursors (Examples 4) | one value |
| **cursor per source** | the keyset position of the last item the source **consumed** — served or skipped — never one that was fetched and not reached | one tuple per source |
| **done** | completed stage/source identifiers, separate from their terminal cursor positions | one status per source |
| **exclusions** | entries grouped by the stage that produced them; item identities, rolling group entries or fixed spans, used by every applicable remaining scan | safe pruning reduces live entries; a size limit needs an explicit overflow policy |
| **predicate exclusions** | a small parameter set that excludes a whole category instead of listing its items | small by construction |
| **prepared lists** | complete bounded sources or refillable buffers with `loaded`, offset, fetch cursor and source-exhaustion state as applicable | a declared buffer/shortlist limit; the output budget alone does not bound the candidates needed |
| **counters** | `served` counts per source, wherever later stages need the number but not the identities; optional chain-wide `selectedTotal` for a count horizon | one integer each |
| **presentation** | what display reordering needs across pages | never read by selection |

### What each stage declares

The server's plan names its stages in order, and each stage declares four parameters. None of
them travels in the vector: they belong to the plan, like the order of the stages (rule 4), and
the signature binds the plan (rule 2). The reader-specific values a predicate uses do travel,
frozen when the chain begins — the labels in the trace below.

Resolve relative windows such as "this month" to fixed boundaries at the first request. Carry
those resolved values, or derive them from fixed chain metadata; do not recalculate a moving
window on each page. The server still defines the window rule and validates its resolved values.

| Parameter | What it says | Forms |
|---|---|---|
| **grouping** | what one row stands for | the item itself, or a group key (Part 2) |
| **window** | which items the stage covers at all | a recency ("this month"), a state ("open"), a count ("the newest 1,000") |
| **budget** | how many rows the stage may serve in one chain | a count |
| **horizon** | how long a served row keeps its group out, behind the scroll position | a time, a count, or both |

**The two kinds of horizon do two different jobs.**
- **A time horizon gives a meaning:** a group that stayed silent for H or longer comes back as a
  row of its own — the gap rule (Part 2). It is what a reader sees as a group revived, so it is
  chosen from how the groups actually live.
- **A count horizon defines limited suppression:** an entry expires after R subsequent rows have
  been selected, with a fixed birth count that is never renewed. This can allow another row for
  the same group even within a time episode. It is a declared result policy, including how these
  rows consume stage budgets, not an exact substitute for unlimited group suppression. It bounds
  live entries by R + 1 when pruning is performed before each candidate (Part 4).
- **Both:** an entry ends at whichever comes first.

**A recency or count window bounds its own exclusions.** Every entry such a stage creates lies
inside its window, so once the later stages scan past the window's edge, all of them have
expired. A state window ("open") is spread across the whole order, so its entries expire like any
other. And when the later stages can test the window's condition themselves, they exclude the
whole stage by predicate (rule 6, in one of the two forms of "The result a plan defines", below)
and carry nothing — S4 does that with S2's labels in the trace below.

**With a compact group representation, each selected row can create one exclusion entry.**
Expanding a represented group into individual item exclusions can create many entries instead.
Keep a shared entry until every remaining consumer has passed it; independent per-consumer copies
would add storage overhead. Live entries can still accumulate across arbitrarily many pages when
many groups overlap the cursor. A time horizon alone does not impose a fixed entry count.

### Exclusions describe sets of items

At the common interface, stages consume items identified by `(position, id)`, whatever
grouping their selected rows represent. An exclusion is a state-dependent predicate over those
items. For stage `s` and vector `V`, write its excluded set as:

`X_s(V) = {x : E_s(x, V) is true}`

The stage considers its eligible candidates minus `X_s(V)`. Applicable exclusions from earlier
work combine by union. The server defines their meaning and evaluators; the token carries their
parameters, not executable predicate code.

| Representation | Membership test | What it must justify |
| --- | --- | --- |
| Explicit item IDs and positions | `id(x) in remembered_ids` | Those specific items are covered. Positions justify safe expiry. |
| A group key and fixed span | `group(x) = key AND lo <= position(x) <= hi` | Every eligible member of that group inside those bounds is covered. |
| A tag/category predicate | `tags(x) intersects remembered_tags` | The entire matching category is covered, or its omission is an explicit plan rule. |
| A covered-prefix predicate | `category(x) AND position(x) >= terminal_cursor` | Every matching item in that consumed prefix is covered. |

These are different descriptions of sets over the same item universe. They are interchangeable
only when they exclude the same items still reachable by the plan. Different stages may group
items differently; each group descriptor must identify its grouping rule as well as its key,
either explicitly or through its producing stage and bound plan version. The same key under two
different grouping rules need not identify the same set of items.

**Exclusions remember coverage; domain structure lets us encode that coverage more compactly.**
Many individual item IDs can sometimes be represented by one group key and two bounds.
Tags may describe an even larger set with fewer parameters. This reduces stored state only when
the membership test is available and affordable.
Serving some tagged items does not justify suppressing every item with that tag.

The main scan need not expose a group key or perform grouping. For example, an index on
`(partition, sort_key DESC, id DESC)` can provide bounded scans for partitions P, Q and R;
a merge selects their descending heads. The application checks explicit ID exclusions and uses
bounded pre-hydration metadata lookups to resolve grouping where needed. Such lookups return
identities, membership or bounds; item payloads are retrieved later for presentation.

The candidate source can also be a search query combining full-text terms, field matches, tags,
ranges and access filters. It exposes the same contract: stable identities in a deterministic,
resumable order, with a fixed eligibility definition and visibility boundary. The grouping layer
does not depend on how that query finds its candidates, although query cost and selectivity still
affect page cost and refill work. Grouping happens after candidate retrieval and before the page
is finalized; suppressing candidates can require another bounded source request.

**The matching item is the entry point; the stage decides what the result represents.**
A grouped stage can resolve the candidate's group and select that group itself; its exclusion
then describes the group's covered members under the declared scope and horizon policy.
An ungrouped stage represents only that item and records its ID. Other grouping rules supply
their own coverage descriptors. The shared sequence is: find a candidate, resolve what it
represents, then exclude that represented set from later candidates.

A plan can instead define a grouped row as covering only its matching members. That differs from
selecting a whole group because one member matched: in the latter case a nonmatching member may
belong to the covered set without appearing in the search candidates. The grouping resolver and
exclusion descriptors implement the chosen meaning consistently across stages. Changing a search
predicate, sort or grouping rule changes the bound plan and invalidates incompatible continuation
state.

Exclusions suppress candidate items; they do not by themselves make the contents of selected
rows disjoint. After an individual item is selected, another member may later select a
whole-group row containing it. A plan requiring each item to occur in only one represented
set must subtract prior coverage or define a rule for partially covered groups.

A span is **not a blanket position range**: members of unrelated groups can interleave inside it.
If the scan returns no group key, the grouping layer must establish membership before applying a group
span, or materialize the represented member IDs so it can test them directly. Compression can
therefore exchange token bytes for membership-lookup work. A hybrid may retain compact group
descriptors plus a bounded cache of explicit member IDs; cache eviction is safe only if the
authoritative exclusion remains evaluable on a cache miss.

Safe pruning has the same set interpretation. A descriptor can be removed once its set has no
intersection with any remaining consumer's reachable candidates, including stages not yet
started. A declared horizon can instead change the excluded set by policy; that is a result rule,
not proof that the old set has become unreachable.

The state size therefore follows the **coverage still overlapping the remaining scans**, not
the total number of results already served. With a stationary rate of new exclusions and finite
mean time until safe expiry, additions and removals can balance into a steady expected size.
One useful approximation is `live entries ≈ new exclusions per page × mean pages until expiry`.
This is a workload expectation, not a constant-space guarantee: long tails, dense overlap and
lagging consumers can retain substantial state. A bounded horizon alone does not bound the
number of overlapping groups. Measure token size against scroll depth and distribution, and
retain the explicit capacity behavior for cases beyond that envelope.

### The result a plan defines

Every guarantee of the pattern is stated against the result the plan defines, so the plan must
define it:

- **Each stage covers** its source, within its window, among the items inserted at or before the
  anchor.
- **Each stage serves** what it covers, minus identities or spans already represented under the
  plan's exclusion and horizon rules, in order, until it runs out or its budget is spent. Whole
  category quotas below are an explicit additional restriction.
- **A budget is part of the definition.** The items a stage covers beyond its budget are in the
  result only if a later stage covers them.
- **A predicate exclusion has two forms**, and the plan states which:

  | Form | The later stage excludes | Items the earlier stage did not reach |
  |---|---|---|
  | **the whole category** — a quota | `NOT category` | outside the result, by design: the budget was the category's share |
  | **the consumed prefix** | `NOT (category AND position ≥ the earlier stage's final cursor)` for a descending scan | in the result: the later stage serves them |

  Here `category` means the earlier stage's full stable eligibility, including source membership
  and its window, and must be reproducible in the later source. The prefix form requires the same total order and a
  retained terminal cursor. It is valid only if every matching item in that prefix was represented
  by this or an earlier stage; it cannot summarize gaps caused by eviction or partial group service.
  If the stage consumed nothing, its prefix is empty. If its source was exhausted, the forms
  coincide under those conditions. A predicate need not live on the row to be correct; its cost
  determines whether it is worth using.

  The trace below uses the whole-category quota: S4 excludes S2's labels, so 85·r, beyond S2's
  budget, is outside that plan's result.
- **The canonical result is fixed by the plan, fixed data and anchor.** Page size and batching
  must not change it. Specify horizon transitions and any approximate eviction policy independently
  of page boundaries. Presentation reordering is outside this equality.

### Prepared sources and refill

A complete bounded source can be carried once and served from an offset. A buffer containing only
the first B candidates of a larger source is different: earlier stages may consume those candidates
without exhausting the later stage's output budget.

For example, an earlier stage serves A, and a later source is `[A, B, C]` with output budget 2.
Preparing `[A, B]` and removing A leaves only B. Exact source-minus-prior-output semantics require
refilling to reach C, so the later stage serves B and C.

A refillable prepared source carries its remaining buffer, a **fetch cursor**, and an
`exhausted` flag. The fetch cursor may pass buffered, unconsumed candidates only because all such
candidates remain in the token. It is distinct from the **consumed cursor** used by the page loop
and prefix predicates. Refill below the fetch cursor when the buffer empties, retaining applicable
overlap state and reapplying exclusions. Mark exhaustion only after a complete source read proves
it; an empty cleaned buffer alone proves nothing. In exact mode, do not silently drop unconsumed
buffer entries to fit the token. Preparation and refill reads need their own work limits.

Alternatively, define a fixed shortlist as the stage's entire source, or overfetch by a proven
bound on earlier overlap. State that choice. The worked S3 trace uses its entire four-item source,
so no refill is needed; its `loaded` flag and offset are sufficient.

### Choosing the values — from the data

None of these values can be chosen on paper: each is a property of the data and of how people
read it. Each stage needs the same few measurements:

| Measurement | What it sets |
|---|---|
| the size of the stage's set per reader — median, p99, largest | the budget; whether later stages can exclude the whole set by predicate |
| the arrival rate, and the spans and gaps of the stage's groups | a time horizon: just above the gap that live groups rarely cross |
| how deep chains go — pages per chain, from the request logs | the cap, and a count horizon: above the depth most chains reach |
| the skip ratio — candidates scanned per row served | what a page really costs |
| the live entries over the scroll — average and peak (Little's law: average = arrival rate × entry life) | the cap and the token's byte budget |

### The vector as an object

```json
{
  "v": 1,
  "anchor": { "seq": 9000 },
  "stage": "S2",
  "cursor": { "S1": [88, "e"], "S2": [94, "n"] },
  "done": ["S1"],
  "served": { "S1": 5, "S2": 3 },
  "excl": { "S1": [["item", [100, "a"]], ["item", [98, "b"]], ["item", [95, "c"]],
                     ["item", [90, "d"]], ["item", [88, "e"]]] },
  "prepared": { "S3": { "loaded": true, "offset": 0, "items": [[96, "s"], [93, "u"], [87, "w"]] } },
  "predicates": { "labels": ["red", "blue"] },
  "presentation": { "lastGroup": "g7" }
}
```

This is the canonical map shape used in this article. A missing or null consumed cursor means
no candidate has been consumed. `done` records completion without destroying the final position;
keep that position while any prefix predicate or other consumer needs it. If a later optimization
needs the reason for completion, also retain whether the source was exhausted or its budget spent.

`excl.S1` means **produced by S1**, not checked only by S1. The server plan determines the consumers.
A consumer may omit passed entries from its query, but the token retains an entry until no
remaining applicable consumer can match it. Unstarted later stages count as remaining consumers.

Entry types are explicit; `position` is always `[sortKey, id]`:

| Kind | Encoded entry | Identity and meaning |
|---|---|---|
| item | `["item", position]` | the item's ID at that position |
| rolling group | `["group", key, lastPosition, oldestPosition]` | one group within one monotone logical scan; the gap calculation uses the time component |
| fixed span | `["span", key, loPosition, hiPosition]` | one represented group/episode span; multiple disjoint spans may share the group key |

Validate each kind's arity, types, ordering and compatibility with its plan. Span bounds must be
ordered and are part of the entry's identity. A count horizon adds `born` to the applicable entry
and carries the chain's selected-row count (Part 4). An implementation must version its complete
schema; these examples omit token-envelope metadata described in Part 4.

For readability, the homogeneous item trace below abbreviates `["item", position]` to `position`.
Part 2 also uses explicit **single-source shorthand**: `cursor: P` and `excl: E` stand for
`cursor: {"scan": P}` and `excl: {"scan": E}` with implicit stage `scan`. A staged or multi-source
token uses maps. These abbreviations are document notation, not alternate wire formats to infer
from untrusted input.

On the wire it is `v1.<key id>.base64url(deflate(json)).base64url(hmac)` — opaque to the client
(Part 4, "The token").
`presentation` holds what display needs across pages — here the last row's group, so the next
page can keep two rows of one group apart (rule 10); selection never reads it.

### How a page is built

```
validate the vector (signature, version, then every field)
work on a tentative copy; remaining = the positive request candidate budget
while the page is not full and a stage remains and remaining > 0:
    want = min(room left on the page, the stage's budget left)
    if want = 0: mark the stage done; move to the next stage; continue
    entering a stage: prepare its complete source or refillable buffer as declared
    K = min(chosen batch size, remaining)
    batch, exhausted = source below its consumed cursor, within the anchor and scope,
                       with equivalent pushed-down exclusions, in order, LIMIT K
    for each candidate c in batch, in order:
        if the page is full or the stage's budget is spent or remaining = 0: stop
        the stage's cursor = c; remaining -= 1                 -- served or skipped
        apply the plan's horizon transitions and safe pruning
        recheck current in-memory exclusions, including entries created earlier in this batch
        if c is excluded: update required skip state; continue
        serve c; add the exclusions it implies; update prepared lists and counters
        enforce the declared state-cap policy at this candidate boundary
        if exact state cannot fit: discard this tentative page; return a capacity error
    if the stage's budget is spent or (exhausted and batch wholly consumed):
        mark the stage done; retain any terminal cursor still needed; move to the next stage
drop every exclusion no remaining stage can meet any more
hasMore = a stage remains                              -- may be true even if no row will survive
check the final token byte budget; on overflow return a capacity error, never trim at serialization
hydrate the ids → present → deliver
return the page and, only if hasMore, its continuation token
```

**Terms in the loop.**
- **Consumed:** a candidate the loop has reached — served or skipped. The cursor is the last
  candidate consumed, never one fetched and not reached, so stopping mid-batch loses nothing: the
  next request fetches the rest again.
- **Pushed down, or in memory:** push down only exclusions valid throughout the fetched range
  under every state the loop can reach. A count-based exclusion that may expire inside the batch
  cannot simply filter that entire batch. Use raw candidates or an adapter that preserves those
  transitions. A rolling group's skipped members renew its gap, so a blanket group filter cannot
  hide them without equivalent episode-state handling. Recheck candidates against exclusions
  created earlier in the same batch: serving G's row can exclude a later fetched member of G.
  K = want is a useful initial batch size when source filtering is effective, not a guarantee
  that every returned candidate will be selected. Overfetch or refill when needed.
- **K:** the batch size, always capped by the request's remaining candidate budget. It can grow
  adaptively when many candidates are skipped. Unconsumed overfetch is re-read, or retained in a
  prepared buffer with a separate fetch cursor; it never advances the consumed cursor.
- **The exclusions it implies:** the served item's id at its position, for the later stages that
  can meet it again; for a grouped stage, its group (Part 2).
- **Exhaustion:** a complete source query returning fewer than K candidates proves there are no
  more for that source under its query conditions. A timeout, partial shard response or cleaned
  prepared buffer does not. Adapters must distinguish exhaustion from interrupted work, and never
  advance past candidates whose outcome is unknown. A spent output budget also ends a stage.
- **Scan cap:** the most candidates the application may consume in this request. An empty page
  is possible when every consumed candidate is skipped. The returned cursor must still advance,
  or the source/stage must reach a terminal state. This cap does not count rows rejected internally
  by a database query; preparation, metadata and source-work limits are separate.
- **`hasMore`:** false when every stage is finished by exhaustion or budget. Otherwise the token
  permits further work, not a promise that the next page contains a row. A full final batch can
  require an extra request to discover the end. **Never stop merely because a page is empty.**
  Stop only when continuation is absent (or an equivalent explicit terminal flag is false).
  For example, with cap 2 and candidates X, Y, Z, exclusions for X and Y produce an empty page
  followed by a page containing Z. This contract agrees with [AIP-158](https://google.aip.dev/158),
  which permits zero results before the end of a collection.
- **Merged sources:** the candidates are the sources' heads, taken newest first, ties broken by
  source order; a source's cursor moves only over what was taken from it. An item served from one
  source is excluded at its position in the others.

### How it evolves — four stages over five pages

A list served from four sources in a fixed order, page size 4. Items are shown as
`sortKey·id`, newest first.

| Source | Budget | Contents at the anchor | Role |
|---|---|---|---|
| S1 | 6 | 100·a, 98·b, 95·c, 90·d, 88·e | first priority |
| S2 | 6 | 99·k, 98·b, 97·m, 94·n, 92·o, 91·p, 89·q, 85·r — everything labelled red or blue | second; overlaps S1 on b |
| S3 | 4 | 96·s, 94·n, 93·u, 87·w | prepared early; overlaps S2 on n |
| S4 | — | all items at the anchor; what remains after its exclusions: 97·x, 92·y, 86·z, 84·z2, 82·z3, 80·z4 … | open; excludes S1's and S3's items by id and S2's whole category by predicate |

All rows through sequence 9000 are already visible to the sources; 9000 is a valid watermark for
this fixture, not simply the largest allocated sequence. After page 1 an item 101·h arrives
(sequence 9001). The reader's labels are frozen at the first request.

**Page 1** — no vector: a fresh one, anchored at sequence 9000. S1 serves a, b, c, d.
```json
{ "v": 1, "anchor": { "seq": 9000 }, "stage": "S1",
  "cursor": { "S1": [90, "d"] }, "served": { "S1": 4 },
  "excl": { "S1": [[100, "a"], [98, "b"], [95, "c"], [90, "d"]] },
  "prepared": { "S3": { "loaded": false, "offset": 0, "items": [] } },
  "predicates": { "labels": ["red", "blue"] } }
```

**Page 2** — S1 serves e and comes back short: it is done. Entering S2, the server **prepares S3**
(its complete source minus S1's items: s, n, u, w). S2 uses this reader's already frozen labels
as its criteria and as a predicate for later. S2
serves k, skips b (an S1 exclusion), serves m and n. Because S2 served n, n is removed from the
prepared S3 list. Item h (sequence 9001) is never a candidate in this chain: it is after the
anchor.
```json
{ "v": 1, "anchor": { "seq": 9000 }, "stage": "S2",
  "cursor": { "S1": [88, "e"], "S2": [94, "n"] }, "done": ["S1"],
  "served": { "S1": 5, "S2": 3 },
  "excl": { "S1": [[100, "a"], [98, "b"], [95, "c"], [90, "d"], [88, "e"]] },
  "prepared": { "S3": { "loaded": true, "offset": 0, "items": [[96, "s"], [93, "u"], [87, "w"]] } },
  "predicates": { "labels": ["red", "blue"] } }
```

**Page 3** — S2 continues below 94·n. Only the S1 exclusions still ahead of its cursor go into
the query (d, e — not a, b, c, which it has passed). It serves o, p, q and reaches its budget of
6: done. S3 serves its first prepared item, s.
```json
{ "v": 1, "anchor": { "seq": 9000 }, "stage": "S3",
  "cursor": { "S1": [88, "e"], "S2": [89, "q"], "S3": [96, "s"] }, "done": ["S1", "S2"],
  "served": { "S1": 5, "S2": 6, "S3": 1 },
  "excl": { "S1": [[100, "a"], [98, "b"], [95, "c"], [90, "d"], [88, "e"]] },
  "prepared": { "S3": { "loaded": true, "offset": 1, "items": [[96, "s"], [93, "u"], [87, "w"]] } },
  "predicates": { "labels": ["red", "blue"] } }
```

**Page 4** — S3 serves u and w and is done; its served list becomes S3's exclusions. S4 starts
from the top of the anchored data, excluding the labels red and blue as a predicate (no list of
S2's items is carried) and S1's and S3's items by id. It serves x and y. S4 is the last source
and its cursor is at 92·y, so every exclusion above 92 can never be met again and leaves the
vector: the vector **shrinks**. 85·r is labelled: S2's budget ended above it, and S4 excludes
the whole category, so this plan leaves r out by its definition — a quota ("The result a plan
defines"). With the other form, S4 would serve r after z.
```json
{ "v": 1, "anchor": { "seq": 9000 }, "stage": "S4",
  "cursor": { "S1": [88, "e"], "S2": [89, "q"], "S3": [87, "w"], "S4": [92, "y"] },
  "done": ["S1", "S2", "S3"],
  "served": { "S1": 5, "S2": 6, "S3": 3, "S4": 2 },
  "excl": { "S1": [[90, "d"], [88, "e"]], "S3": [[87, "w"]] },
  "predicates": { "labels": ["red", "blue"] } }
```

**Page 5** — S4 serves z, z2, z3, z4; the remaining exclusions (d, e, w) are passed and dropped.
```json
{ "v": 1, "anchor": { "seq": 9000 }, "stage": "S4",
  "cursor": { "S1": [88, "e"], "S2": [89, "q"], "S3": [87, "w"], "S4": [80, "z4"] },
  "done": ["S1", "S2", "S3"],
  "served": { "S1": 5, "S2": 6, "S3": 3, "S4": 6 },
  "excl": {}, "predicates": { "labels": ["red", "blue"] } }
```

From here on this fixture creates no more exclusions, so its live entry count stays constant.
Counters and encoded cursor lengths may still change. A refresh starts a new chain with a new
watermark that includes h, and h appears at the top.

### Rules

1. **The server is stateless; the vector is the session.** The client treats it as opaque and
   returns it unchanged, following continuation even after an empty page.
2. **Opaque, compressed, signed, versioned — and bound.** Not encryption: the signature stops
   tampering, but anyone holding the token can decode it, so what the vector carries — ids, a
   reader's interests — is readable by its holder; encrypt it (an authenticated cipher) when that
   matters. The version lets the format change without breaking chains in flight. The
   signature covers the **tenant, the user and the list's identity** — the plan it runs, and its
   query and filters — as well as the payload, so a token presented by anyone else, in another
   tenant, for another list or to another plan is rejected before payload decompression. Access is
   re-read on every request and at hydration (Part 4);
   binding makes a replayed token fail at the signature instead of later.
3. **A valid signature is not enough.** Validate known fields, required stage state, types, ranges,
   positions and entry kinds before querying. Item identities and rolling-group keys are unique
   in their appropriate scopes; fixed spans are unique by group and bounds, not group alone.
4. **The server owns the limits** — the sources, their order, each stage's grouping, window,
   budget and horizon ("What each stage declares"), and the page-size cap. The client may ask for
   a page size, nothing more.
5. **Every resource has an explicit limit and overflow policy.** Prune safely, bound prepared
   buffers and token bytes, and stop exact traversal if required state cannot fit. A fixed token
   size does not guarantee every possible chain can finish without recomputation or external state.
6. **Exclude by the widest key that is correct**: a predicate before a group key, a group key
   before an item id. One entry should cover as much as possible without hiding unrepresented
   results. A predicate must reproduce the right coverage; its storage and lookup cost are separate.
7. **Carry only what cannot be recomputed cheaply.** Whatever can be decided from the data and the
   anchor alone does not belong in the vector (see "Group heads", Part 2).
8. **Retain required state for exact traversal.** Dropping an exclusion early may create repeats;
   with finite budgets those repeats may displace distinct results. An approximate mode must state
   that tradeoff. Expiry justified by the plan's declared horizon is a result rule, while expiry
   justified by all remaining scans passing the entry is safe pruning.
9. **Deterministic.** The same vector, plan, request and fixed data give the same page. State
   transitions happen per candidate or selected row, not only when a token is serialized.
10. **Selection, then hydration, then presentation.** Sources return IDs and stable sort positions,
    whichever engine answers — a database or a search engine. Group membership may be supplied
    by a separate bounded metadata lookup during selection; it need not live in the scan index.
    Presentation reorders the bounded page for display and never touches cursors or exclusions.
11. **Apply each exclusion where it is cheapest** — in memory in the page loop, as a filter on an
    ordered scan that stops at the page, or as a merge of sorted document lists in an inverted
    index. Preserve semantics and required skip-state updates, then compare execution plans and
    measured costs; a relational anti-join is an option, not a correctness violation.
12. **The stages of one list share its scope.** The list's filters (views, tags, field matches,
    access) apply per item, and alike in every stage; a stage's own criterion only decides which
    items come first. So an item served by any stage is served for the whole list, and every
    stage can honour every other stage's exclusions.

### Cost

For a single scan with constant-time exclusion lookup, the selection loop does O(P + S) candidate
checks for P selected rows and S candidates skipped in the application. The scan cap bounds that
candidate count. It does **not** bound database rows rejected before `LIMIT`, posting-list work,
query startup, metadata lookups or preparation. A SQL query returning one row can inspect a million
rejected rows before finding it.

For M merged sources, a heap typically adds O(log M) work per consumed head and source startup
costs. Token encoding, decoding and validation also depend on the live state size. Pruning every
entry on every candidate adds cost; an implementation needs suitable ordered expiry structures
or must account for those scans explicitly.

Hydrating P selected item IDs bounds the item count, not necessarily payload bytes. Expanding P
groups can involve arbitrarily many members. Full-group or episode discovery, counts and boundary
lookups need their own indexes, summaries, lazy expansion or work limits. Prepared buffers cost
both their initial query and any refills, plus carried bytes between requests.

Use separate budgets and metrics for application candidates, source reads/deadlines, metadata,
hydration and token bytes. A deadline must not turn an incomplete query into false exhaustion.
These are algorithmic costs and implementation requirements, not measured engine benchmarks.

### Where exclusions are applied

**Preserve an efficient ordered access path where possible.** In the access paths below, the
positive query supplies candidates and exclusions remove matches. An ordered scan can stop after
enough surviving rows, although it may inspect many rejected rows first. Sorting, exact counting,
joins and aggregations can add work beyond the page; inspect the actual execution plan rather than
inferring a fixed cost from `LIMIT` alone.

How the check is made depends on the kind of index:

- **An ordered index — a B-tree, in SQL.** It finds what **is** there; it cannot seek to
  "everything except these". The exclusion is a filter on each row the range scan reads. The scan
  can supply the result order when the index supports the `ORDER BY`; a limit can then stop it
  after enough surviving rows. Index direction, equality prefixes and planner choices matter.
- **An inverted index — a search engine.** For every term it keeps a posting list: the sorted ids
  of the documents that have it. A query walks these lists together — AND intersects them, OR
  merges them, NOT removes one list's documents from another — each a **merge of sorted document
  lists** that skips ahead rather than reading every entry. Engines cache frequently used filters
  as bitsets, an optimisation on top of the merge. The lists are in document order, not time
  order. Matching index sorting can enable early termination per segment when exact hit counts
  and aggregations do not require further collection; it is not a universal bound on index work.

Hence three places, each right for one kind of exclusion:

| Where | Right for | Cost |
|---|---|---|
| **in memory, in the page loop** | the vector's own pruned entries, especially rolling state that must see skips | a set lookup in the application; rejected candidates still cost source reads and transfer |
| **a filter on an ordered scan** — `id <> ALL($1)` with the pruned list | a short list, or a category cheaply available on the row | source work includes rejected rows; inspect `EXPLAIN (ANALYZE, BUFFERS)` on representative data |
| **an inverted-index filter** — `must_not` | categories or identities indexed as document terms | posting-list and collection work depends on distribution, segments and the query; matching index sorting can help |

Two query forms deserve particular care:
- **A relational anti-join**, often expressed as `NOT EXISTS`, may add indexed probes per candidate
  or a hash build before scanning. It can also be a good plan. Compare its cost with denormalizing
  a category onto the row/document, including update consistency and write costs. The group-head
  alternative in Part 2 deliberately uses a relational lookup.
- **`NOT IN (subquery)`** has NULL-sensitive semantics. For intended anti-existence behavior,
  `NOT EXISTS` usually expresses the requirement more directly; the two are not interchangeable
  without accounting for NULLs. Planner support also varies by version and expression.

PostgreSQL documents hash lookup for long constant `IN` lists in version 14 and `NOT IN` lists
in version 15. This does not establish the same optimization for every parameterized array or
generic plan. The version 19 development release notes describe conversion of eligible `NOT IN`
subqueries to anti-joins; as checked on 19 November 2026, that version was still labelled a
development release. Treat it as version-specific, not a portable guarantee.
[PostgreSQL 14](https://www.postgresql.org/docs/14/release-14.html),
[PostgreSQL 15](https://www.postgresql.org/docs/15/release-15.html),
[PostgreSQL 19](https://www.postgresql.org/docs/19/release-19.html),
[subquery NULL semantics](https://www.postgresql.org/docs/18/functions-subquery.html).

#### Example — one exclusion, two engines

Page 5 of the trace above: S4 continues below its cursor 92·y. The page needs items at or before
the anchor (sequence 9000), below the cursor, not d, e or w (the ids still ahead of the cursor),
not labelled red or blue (S2's category, a predicate) — the newest 4. The table holds every item
of the trace.

**SQL (Postgres).** The labels are stored on the row, as `labels text[] NOT NULL DEFAULT '{}'`;
the index is `(sort_key DESC, id DESC)`.

```sql
SELECT id, sort_key
FROM items
WHERE seq <= 9000                              -- the anchor
  AND (sort_key, id) < (92, 'y')               -- the cursor
  AND id <> ALL ('{d,e,w}')                    -- the ids still ahead of the cursor
  AND NOT (labels && '{red,blue}')             -- the category, read from the row
ORDER BY sort_key DESC, id DESC
LIMIT 4;
```

If the planner chooses the intended ordered index scan, the illustrative walk is 92·o, 91·p
(labelled), 90·d (excluded), 89·q (labelled), 88·e, 87·w (excluded), 86·z (served), 85·r (labelled),
84·z2, 82·z3, 80·z4 (served): 11 fixture entries for four results. This is a hand trace, not an
execution-plan measurement. NULL-sensitive conditions can silently exclude rows, so the example
uses non-NULL IDs/labels and validation rejects NULL identities in the exclusion list.

An alternative keeps the labels in a relation table:

```sql
  AND NOT EXISTS (SELECT 1 FROM item_labels l
                   WHERE l.item_id = items.id AND l.label IN ('red', 'blue'))
```

An indexed-probe plan adds a lookup in `item_labels` per candidate. A hash anti-join has a different
startup cost and may choose a different access path. Measure both against the data distribution;
keeping labels on the row trades those relation lookups for denormalized storage and updates.

**Elasticsearch / OpenSearch.** The same items, the labels as a keyword field, the index sorted
the way the list is read:

```http
PUT items
{
  "settings": { "index": { "sort.field": ["sort_key", "id"], "sort.order": ["desc", "desc"] } },
  "mappings": { "properties": {
    "id":  { "type": "keyword" }, "sort_key": { "type": "long" },
    "seq": { "type": "long" },    "labels":   { "type": "keyword" } } }
}

POST items/_search
{
  "size": 4,
  "track_total_hits": false,
  "query": { "bool": {
    "filter":   [ { "range": { "seq": { "lte": 9000 } } } ],
    "must_not": [ { "terms": { "id":     ["d", "e", "w"] } },
                  { "terms": { "labels": ["red", "blue"] } } ] } },
  "sort": [ { "sort_key": "desc" }, { "id": "desc" } ],
  "search_after": [92, "y"]
}
```

The query expresses the same fixture result with document fields and `search_after`. Matching
index sorting and `track_total_hits: false` allow eligible collection to terminate after enough
matches per segment; they do not imply the same 11 internal reads as the SQL hand trace.
Index sorting is configured at index creation and has an indexing-throughput cost. Aggregations
may still collect every match. Check the corresponding engine/version and profile the query.
[Elasticsearch index sorting](https://www.elastic.co/docs/reference/elasticsearch/index-settings/sorting),
[OpenSearch index sorting](https://docs.opensearch.org/latest/im-plugin/index-sorting/).
Costs to watch:
- counting the hits — `track_total_hits` counts up to 10,000 by default, which keeps the walk
  going past the page;
- `from` / `size` instead of `search_after` — every page re-reads all the pages before it;
- sorting on fields without a matching index sort — other competitive-document optimizations may
  help, but do not assume the same per-segment early termination.

**A staged list makes this choice per stage** (Part 3). Take a broad last stage that excludes
the items earlier stages served and everything carrying a reader's chosen labels. On a database
that can use an ID filter and an indexed anti-join; on a search engine it can use `must_not` over
IDs and label terms. Either may be appropriate. Choose per stage from measured plans, indexing
costs, consistency requirements and available ordering, rather than assuming a broad stage
always belongs on a search engine.

### Optimization — compact exclusions

Exclusion entries can accumulate with the scroll; counters and encoded positions also vary in
size. For a homogeneous numeric entry list with one scan direction, entries can be stored like
delta-encoded posting lists: **sorted,
with the first entry absolute and every later entry as its difference from the previous one**,
each difference written in as few bytes as it needs (a variable-length integer).

**Why the vector suits it.** The entries are numbers — a position `(time, id)` — kept sorted by
the position at which they expire, in the scan's own direction (newest first). Sorted means the
differences are small; newest first means the time differences are never negative. The id
differences are positive only when ids follow the same order; otherwise they are written zig-zag
(a sign bit), which variable-length integers allow.

```
absolute:  (1763100000, 1042) (1763096400, 1040) (1763096400, 1039) (1763010000, 1012)
encoded:   (1763100000, 1042) (3600, 2)          (0, 1)             (86400, 27)
bytes:      5 + 2              2 + 1              1 + 1              3 + 1
```

**The operations stay simple:**
- **Pruning removes the head.** The scan passes the newest expiries first, so an expired entry is
  at the front once every applicable consumer has passed the same expiry frontier: the new head
  is the old head minus the first difference, and that difference is dropped. Approximate
  furthest-expiry eviction can remove the tail; exact mode does not evict necessary entries.
- **Inserting in the middle** (a newly served entry that expires between two others) needs no
  special case: the vector is decoded on every request anyway, so the server inserts into the
  decoded list and encodes it again. The differences are only the wire format.
- **Validation still matters.** Check nonnegative time deltas, valid tie-breaker deltas when
  times tie, integer ranges and overflow, decoded positions and entry-kind rules. Distinct episode
  spans can share a group key; their bounds or episode identities distinguish them.

**When it pays.** Sorted times often give smaller deltas, but byte savings depend on the gaps and
encoding. The ID column benefits when numeric IDs correlate with that order; insertion order,
event time and visible group bounds need not coincide. String keys can remain literal while
numeric position components use delta encoding.

**Keep exact boundaries lossless.** Coarsening timestamps can change membership, episode gaps or
pruning. Early expiry may introduce repeats that consume finite budgets; widening a span may hide
unrepresented items. Delta encoding can preserve the original values exactly. Use a coarser key
only if it is the plan's canonical ordering/episode precision from the outset, with a stable
tie-breaker and matching source semantics.

For vectors measured in tens or hundreds of entries, variable-length integers may be sufficient.
Measure actual encoded token sizes before adding block packing or other format complexity; this
document does not establish a typical or maximum entry count for every workload.

This shrinks each entry. The entries need not all be of one kind: where a stage serves a group
whole, one span stands for all of the group's items, and the list holds fewer entries (Part 2,
"Served whole — fixed spans for groups and episodes").

---

## Part 2 — Grouped results: one row per group

### The case

A list shows **one row per group**, newest first. Group membership is defined by the application;
the underlying scan is over individual items. Once a group has been shown, its older items must
not become rows further down — while items keep arriving across several partitions the viewer
can see, and with the same item reachable through more than one of them.

### The vector for it

| Part 1 | Here |
|---|---|
| anchor | a visibility watermark fixed at the first page; event time alone cannot exclude backfills or late arrivals |
| stage and cursor | one source — the merged scan of the viewer's partitions — and one cursor on `(time, itemId)` |
| exclusion key | the **group key** — never the item id (rule 6) |
| exclusion entry | `["group", key, last, oldest]`: full positions of the most recently consumed group item and the oldest eligible group item; skipped members renew `last`; the loop below defines expiry |
| horizon | the stage's own (Part 1, "What each stage declares"): a time H under the gap rule (below), a count, or both |
| predicate exclusions | group-level conditions that rule a whole group out — re-evaluated for every candidate, so a group they rule out needs no exclusion entry; the reader-specific values they use can be frozen in the vector, as in Part 1 |
| presentation | the group's items under its row — hydration and display only |
| not carried as authorization | the viewer's partitions — read on every request and rechecked at hydration (Part 4) |

```json
{ "v": 1, "anchor": { "seq": 5000 }, "cursor": ["2026-11-12T08:00", "i6"],
  "excl": [["group", "A", ["2026-11-14T09:00", "i3"], ["2026-11-10T08:00", "i7"]],
           ["group", "D", ["2026-11-12T08:00", "i6"], ["2025-03-01T08:00", "i8"]]] }
```

This uses the single-source shorthand defined in Part 1. Rolling group entries belong to this
one monotone logical scan. A shared mutable `last` is not a general cross-stage exclusion: a
later stage may restart above it. Use fixed represented spans below for cross-stage episodes, or
design explicit per-consumer state and validate that additional composition separately.

The vector carries no plan parameter — no stage list, budget, horizon or merge mode — and no
scope (the list's query and filters): the signature binds both (rule 2), so a token presented to
another plan or another list is rejected. This example reads the viewer's partitions live on every
request. An alternative may bind an access-scope version to force restarts on changes (Part 4),
but the token never grants access.

### The page loop

This is the Part 1 loop for one source, with the group test applied in memory. Group metadata
uses the same item filters, anchor and visibility as the scan. Cache keys and invalidation must
respect that scope; an unscoped or stale oldest-position lookup is not a correctness proof.

```
validate; work on a tentative copy; remaining = the positive request candidate budget
hasMore = true
while |page| < N and remaining > 0:
    K = min(chosen batch size, remaining)
    batch, exhausted = scoped scan below cursor, seq ≤ anchor.seq, LIMIT K, each item once
    meta = groupKey, oldest eligible position, group-level predicates for these candidates
    for each candidate c in batch, in order:
        if |page| = N or remaining = 0: stop
        cursor = c.position; remaining -= 1
        drop entries when any declared condition holds:
            c.time ≤ last.time − H                  -- gap H or longer
            c.position < oldest                    -- no member remains below this position
            selectedTotal − born ≥ R               -- only when count suppression is declared
        if a group-level predicate fails: continue
        if c's group has an entry: entry.last = c.position; continue
        page += (c, key); increment this source's served count
        if count suppression is declared: selectedTotal += 1
        excl += (group, key, last = c.position, oldest, born = selectedTotal if needed)
        enforce the declared state-cap policy at this candidate boundary
        if exact state cannot fit: discard tentative page; return a capacity error
    if exhausted and batch wholly consumed: hasMore = false; break
    adjust next batch size within the remaining candidate budget
check token bytes; hydrate and present the selected rows
return page and continuation when hasMore, including after an empty page
```

### How it evolves — a viewer of two partitions, three pages

Page size 2, batch size 4, candidate cap at least 4, horizon H = 180 days, no count horizon or
eviction. Sequence 5000 is a visibility
watermark for this fixture. The viewer sees two partitions, P and Q, which hold these items
(groups A–F). Times in this trace use one common timezone and precision:

| Item | Time | seq | Group | Partition(s) |
|---|---|---|---|---|
| i1 | 2026-11-14 10:00 | 4980 | A | P |
| i2 | 2026-11-14 09:30 | 4975 | B | Q |
| i3 | 2026-11-14 09:00 | 4970 | A | P |
| i4 | 2026-11-13 18:00 | 4960 | C | P **and** Q |
| i5 | 2026-11-13 12:00 | 4955 | B | Q |
| i6 | 2026-11-12 08:00 | 4950 | D | P |
| i7 | 2026-11-10 08:00 | 4940 | A | Q |
| i8 | 2025-03-01 08:00 | 1200 | D | P |
| i9 | 2025-02-20 08:00 | 1190 | F | P |

Oldest item per group: A — Nov 10 (i7); B — Nov 13 12:00 (i5); C — Nov 13 18:00; D — 2025-03-01
(i8); F — 2025-02-20.

**Page 1** — fresh vector, anchor at seq 5000. i1 → row **A**: A's entry records the last item
met, i1, and A's oldest, Nov 10. i2 → row **B**, oldest Nov 13 12:00.
```json
{ "v": 1, "anchor": { "seq": 5000 }, "cursor": ["2026-11-14T09:30", "i2"],
  "excl": [["group", "A", ["2026-11-14T10:00", "i1"], ["2026-11-10T08:00", "i7"]],
           ["group", "B", ["2026-11-14T09:30", "i2"], ["2026-11-13T12:00", "i5"]]] }
```
*Between pages, i0 arrives in group B (2026-11-14 11:00, seq 5001).*

**Page 2** — i3 is A's: skipped, and A's last moves to i3. i4 through P → row **C**; i4 again
through Q is the same item, next to it in the scan's order: the scan folds it (one row, not two).
At i5 the scan is below C's oldest: C's entry is dropped; i5 is B's: skipped, B's last moves to
i5. At i6 the scan is below B's oldest: B's entry is dropped; i6 → row **D**, oldest 2025-03-01.
i0 is not a candidate at all: its seq 5001 is after the anchor.
```json
{ "v": 1, "anchor": { "seq": 5000 }, "cursor": ["2026-11-12T08:00", "i6"],
  "excl": [["group", "A", ["2026-11-14T09:00", "i3"], ["2026-11-10T08:00", "i7"]],
           ["group", "D", ["2026-11-12T08:00", "i6"], ["2025-03-01T08:00", "i8"]]] }
```

**Page 3** — i7 is A's oldest: skipped; below it, A's entry is dropped. At i8 the scan is more
than 180 days below D's last item, i6: D was silent for longer than H, so its entry is dropped
and i8 becomes a **continuation row, "D — earlier"**, representing a distinct episode under the
declared gap rule, with a new entry. At i9 the scan is below D's oldest; i9 → row **F**.
```json
{ "v": 1, "anchor": { "seq": 5000 }, "cursor": ["2025-02-20T08:00", "i9"],
  "excl": [["group", "F", ["2025-02-20T08:00", "i9"], ["2025-02-20T08:00", "i9"]]] }
```

Rows served: A, B · C, D · D (earlier), F. Between pages, the vector never held more
than two entries (for a moment in page 2, after i4: three). A refresh starts a new chain at seq
5001: B is now at the top, with i0.

The final JSON shows the internal selection state. Page 3 consumes the entire short final batch,
so its response has no continuation token; F's terminal entry need not be sent back to the client.

### Groups enter big and are consumed

A group entry suppresses older members according to the declared episode and count rules. It is
updated as the scan meets them and removed when suppression ends or no eligible member remains.
Group A in the trace above:

| Page | What the scan meets | A's entry `(last, oldest)` | A's older items still ahead |
|---|---|---|---|
| 1 | i1 → row A | `(i1, Nov 10)` enters | i3, i7 |
| 2 | i3 → skipped | `(i3, Nov 10)` | i7 |
| 3 | i7 → skipped, then passed | dropped | — |

D shows the horizon at work: nothing of D lies within 180 days below i6, so when the scan reaches
i8, twenty months older, D comes back once as "D — earlier" and is consumed again from there.

The vector retains groups that may still suppress candidates ahead of the scan. After processing
a candidate it may also retain the entry at its exact oldest position until the next comparison.
The live set is no larger than the rows selected so far in this single-scan model, but can span
many pages. A time horizon limits temporal reach, not density; a declared count horizon bounds
the live set by R + 1 under the transition convention in Part 4. Capacity limits still need the
exact-versus-approximate policy in rule 8.

### Served whole — fixed spans for groups and episodes

The entries above follow the scan: `last` is renewed by every item it skips. When a group's row
is served, the server can instead list the group's items once, at that moment, and count the
whole group as served. Its entry is then fixed:

`["span", key, lo, hi]` — full oldest and newest positions of the group's items **that the row represents**:
those that pass the list's filters, inserted at or before the anchor, visible to this viewer.

```
group G: 100 items; 12 pass the list's filters, from Nov 3 to Nov 9
entry:   [span, G, oldestPosition, newestPosition]  — bounds, not twelve ids
dropped: when the scan passes Nov 3 (newest first) or Nov 9 (oldest first)
```

- **Exact by construction.** A span may stand only for items that were served: over items not
  served, it would hide them (against rule 8). Serving the group whole makes the span cover
  exactly what was served.
- **Filters narrow the span.** Filters apply per item, and alike in every stage. An item that
  passes them can bring its group in; one that passes them while its group is excluded is
  skipped; one that fails them takes no part. The span covers the group's matching items only;
  filtering can narrow its bounds and shorten the entry's life, though it need not do so.
- **One more kind of entry, not a replacement.** Item entries `["item", position]` stay wherever a
  stage serves single items; a span is used where a group is served whole. One exclusion list can
  hold both, beside the predicate exclusions — each kind with its own test and its own expiry,
  all pruned as the scan passes them. Compact exclusions (Part 1) shrink each entry's bytes; a
  span shrinks the number of entries. The two combine.
- **One entry for every stage.** The stage that served the group skips its other items as its
  scan meets them; every later stage applies the same test — in memory, or pushed into its
  query as `NOT (group_key = k AND lo <= position AND position <= hi)`, with tuple comparisons.
  Fixed once created, it needs no rolling `last` update. Keep distinct spans for distinct served
  episodes: if G's episodes at 100 and 80 were represented, merging them into `[80,100]` could
  wrongly hide an unrepresented episode at 90. Group key alone is not a span's identity.
- **Either direction.** One consumer can stop checking a span after passing `lo` (newest first)
  or `hi` (oldest first). Remove it from the token only when every remaining applicable consumer
  has passed it or is done. An unstarted stage can still encounter it. Carry both boundaries for
  episodes and mixed scan directions; an insertion-sequence anchor need not bound event-time positions.
- **The condition: the stages share the list's scope** (rule 12). A stage's own criterion
  decides which groups come first. The span covers all eligible members represented by that row,
  either a whole group or one selected episode, and every consumer recognizes that same coverage.
- **The cost.** Finding complete group or episode bounds may require reading many matching
  members; a group-key lookup is not automatically bounded by page size. Use appropriate summaries,
  indexes or separate work limits. A row may lazily expose its represented members, but a truncated
  preview alone does not justify claiming unserved members were covered.
- **With a time horizon**, the row stands for one episode: the gap rule is applied when the
  group is listed, and the span covers the episode that holds the item served. Below `lo`, this
  consumer stops suppressing that span and an earlier episode can become another row. Other
  remaining consumers may still require the span in the token.

### The horizon — the gap rule, and plain and grouped as one parameter

A time horizon is a **gap rule**: a group gets a new row where it was silent for H or longer. The
entry's `last` is renewed by every item the scan skips, so a group that keeps receiving items
stays one row however long it lives, and one that went quiet comes back as "earlier" where it
resumes. Each run of a group's items with no silence of H or longer is an **episode**: one row,
one entry.

- **H = 0** — no gap is shorter than 0: every item is its own row, a plain list — two items of
  one group at the same time included. An entry ends at the next candidate, so at most one crosses
  to the next page (Examples 1).
- **H = ∞** — whole groups: one row per group; an entry ends when the scan passes the group's
  oldest item.
- **Between** — a group splits wherever it was silent for H or longer.

Switching between a plain and a grouped view is a change of H, not of the data model or the
index. With fixed scope and no count horizon or eviction, the head check below defines the same
canonical episode-head sequence for each time horizon. Request work caps or pushdown can still
produce different page boundaries and different numbers of empty pages.

### Group heads — the case where nothing needs carrying

Rule 7 taken to its end: **an item is a row only if it is the newest item of its group that this
viewer can see, as of the anchor, within H.** If a newer one exists, this item was already shown
in that one's row. That is decidable from the data, so the exclusion list is not needed. On
membership rows — one per item and partition:

```sql
NOT EXISTS (SELECT 1 FROM membership n
             WHERE n.group_key = p.group_key
               AND n.partition_id = ANY($viewerPartitions)
               AND n.seq <= $anchorSeq
               AND (n.time, n.item_id) > (p.time, p.item_id)
               AND n.time < p.time + $H)                -- a gap shorter than H
```

The same trace with the head check: i3 — A has a newer visible item (i1): skip. i4 — no newer
item: row C; its second membership row is the same item at the same position and is folded by the
scan itself. i5 — B has a newer visible item, i2 (i0 is newer still but after the anchor, so it
does not count): skip. i6 — row D. i7 — A has newer: skip. i8 — D's newer item (i6) is more than
180 days later: i8 is a head, the continuation row. i9 — row F. With no binding work cap, these
are the same example pages, and the vector is only `{ v, anchor, cursor }` in single-source shorthand.

- **The same gap rule as the carried entries:** an item is a head when its group's nearest newer
  visible item is H or more later — so the two give the same selected sequence for each time horizon. A
  count horizon has no equivalent here: the data cannot count the rows a chain has served, so it
  needs carried entries.
- **Condition:** the lookup must apply exactly the scan's visibility — the viewer's partitions,
  `seq ≤ anchor`, anything hidden for that person — or a group whose newest item is hidden
  disappears. Carried exclusions avoid suppressing a group merely because an unseen head exists,
  but still need correct metadata and scope; changing data or visibility weakens both methods.
- **Item-level filters** (a field match, full text): the lookup must apply them too — easy for
  fields on the membership row, hard for a search engine's full text, where carried exclusions
  fit better. (A search engine's collapse on the group key does not page groups by recency: with
  `search_after` it must sort on the collapse field itself, with no secondary sort.)
  [Elasticsearch collapse restrictions](https://www.elastic.co/docs/reference/elasticsearch/rest-apis/collapse-search-results).
- **Index:** `(group_key, time DESC, item_id DESC) INCLUDE (partition_id, seq)` on the membership
  rows — the same index that opens a group.

### Things to know

- **Expanding a row never changes the vector.** A group's exclusion is decided when its row is
  **served**, not when someone opens it. Its items are hydrated on demand: the newest few and a
  count with the row, the rest when the row is expanded — a presentation step, outside the vector
  (rule 10).
- **Groups merged mid-chain.** If a group not yet shown is merged into one already shown, its
  items now carry the shown one's key and are skipped until a refresh. A live refresh of the shown
  row may expose the changed members. Merges and splits change the result definition; do not apply
  the fixed-data exactness guarantee to them, especially when repeats consume finite budgets.
- **Compact exclusions need a numeric group number** (Part 1, "Optimization — compact
  exclusions"). String keys remain literal. Numeric IDs may delta-compress well when they correlate
  with expiry order, but filters, visibility and backfills can break that correlation; measure it.
- **A shared loop for compatible lists.** Plain lists, grouped lists, stable-order search results,
  priority sections and a group's own items can share continuation machinery when their adapters
  satisfy the same source contract:
  - plain and grouped are one parameter (H), not two data models;
  - the loop fills pages until exhaustion, an output budget or a work limit; access rechecks may
    shorten them further;
  - the server selects group or episode rows; the client displays them without collapsing
    distinct episodes merely because they share a group key;
  - one cursor format, one validation, one test oracle, and one place to make paging fast;
  - database and search adapters can share a result shape, while ordering, visibility and work
    limits must be established separately for each engine.
- **Operations:** the token's size, keys and expiry, metrics and support tools — Part 4.

---

## Part 3 — Uses

### Deterministic ordered stages

One list served from sources in a fixed priority, each source exhausted or its budget spent
before the next. Part 1's trace is this shape. Under the exact-mode assumptions, retained
exclusions prevent cross-stage duplicates. What it asks of the mechanism:

- **Stages:** one page filled across sources in order; proven exhaustion or a spent output budget
  ends a stage. A request work limit only pauses it.
- **Prepared lists:** a complete bounded source can be prepared early and cleaned as earlier
  stages serve. A partial buffer must refill; preparation moves work earlier rather than removing it.
- **Predicate exclusion:** use either an intentional whole-category quota or a proven consumed
  prefix, as defined in Part 1.
- **Both engines, one shape:** adapters can return `(sort key, id)` in a compatible order,
  with grouping metadata resolved separately where needed, provided they establish the required
  visibility and source semantics.
- **Presentation apart:** items of one group spread apart on each page, optionally in a seeded
  order per day; cursors and exclusions follow the canonical order.
- **Strict validation and a signed token**, to be tested with validation cases and a client that
  walks the pages (Part 4, "Testing").

### The shapes it fits

- **Grouped lists** — one row represents items related by an application-defined grouping rule
  (Part 2).
- **Lists with priority sections** — a preferred category first, then the broader eligible set:
  ordered stages.
- **Ordered results merged from several sources** — items from independent sources, merged by
  a shared total order, one cursor per source.
- **Search across engines** — a database for structured criteria, a search engine for text, with
  one vector and de-duplication across the two.
- **Multi-producer streams** — visibility-watermarked to exclude subsequent arrivals;
  several producers each a source, with per-source budgets so one cannot fill the entire result.
- **One feed for a small screen.** Where a large screen gives each list its own shortcut — each
  one ordered source, paged with plain keyset — a phone can show one composed feed instead:
  - **stages as sections**, each page returning its segments (stage, offset, length) so the
    client draws the section headers — no tabs, no rail of views;
  - **budgets as space:** no section floods the screen, and a section's "see all" opens its own
    list;
  - **nothing twice across sections:** an item that belongs to two shows once, in the higher one;
  - **pull-to-refresh is the restart:** the familiar gesture is "new anchor, new chain", which is
    also when the selection is recomputed (the opening "Scope and guarantees");
  - **retries are safe:** the same vector over the same data gives the same page, so a request
    lost to a dropped connection or an app sent to the background is simply sent again, to any
    server;
  - **small pages need no application session store**, but increase per-request query, token and
    transport overhead; choose page sizes with those costs in mind.

  Two things to settle: a token that expired while the app slept is treated as a pull-to-refresh;
  and the order of the sections is a product decision, made from the same per-stage data as the
  rest (Part 1, "Choosing the values").

### Possible implementation

The token transport, schema validation and page loop could form a reusable library, with each
application supplying source adapters. Those adapters must expose ordering, consumed progress,
visibility and reliable exhaustion, plus any group metadata or preparation state. This document
specifies the design; building such a library requires implementation and validation of those
contracts.

---

## Part 4 — Implementing it

An implementation must settle these contracts and verify them against its own source adapters.
The choices below distinguish exact traversal from explicit limits or weaker policies.

### The token

- **Format and keys.** `v1.<key id>.<payload>.<mac>`, with HMAC-SHA256 over an unambiguous encoding
  of the version, key ID, payload and request binding: tenant, user, plan identity/version, query
  and filters. Sign with the active key and retain verification keys for the accepted token
  lifetime; compare MACs in constant time. The exact encoding and validation schema need tests.
- **Expiry.** The payload carries an issued-at time. Refuse a token older than a chain plausibly
  lives, based on the application's requirements. Reject an expired token explicitly; a client
  may start a fresh chain and replace its old list rather than append new page 1 (Example 6).
- **Size.** A byte budget, checked before the MAC is verified, and a cap on the size after
  decompression. Check the encoded result before returning it too: entry-count limits alone do
  not bound variable-length keys, prepared items and envelope overhead. Exact overflow is an error,
  not permission to discard state or return an end-of-results marker.
- **Transport.** A body or suitable header avoids putting tokens into browser history and many
  URL logs. Check actual header/body/proxy limits and logging behavior; no transport makes a
  readable signed payload confidential.
- **Confidentiality.** Signed is not encrypted (rule 2). If the payload carries anything its
  holder should not read — a reader's interests, say — encrypt it with an authenticated cipher
  (AES-GCM, ChaCha20-Poly1305) instead.

### Plan changes

A deploy that changes a plan — a stage added, a budget lowered, a horizon moved — leaves the
tokens in flight describing a plan the server no longer runs. The signature binds the plan
(rule 2), so they are rejected and the chain restarts; the client must then **replace** the list
it shows, not append the new page 1 below it. To avoid a restart on every deploy, give plans a
version and retain every supported version until its issued tokens expire. Keeping only N and
N−1 is sufficient only if no older unexpired versions remain; otherwise document that their
chains restart. Format versions work the same way during rolling deploys.

### Access, checked again at hydration

Access authorization is never granted by the token, but prepared IDs and exclusions were chosen
under an earlier request's permissions. Recheck access for every delivered ID and group member.
Revocations can shorten a page after selection; empty output still does not imply exhaustion.
Current hydration data may differ from the data used for selection, so exact fixed-data guarantees
no longer apply. Revalidation at hydration protects access; it does not create a snapshot.

### Positions and ties

- **A position is a tuple** `(sort key, id)`, compared as a tuple for cursors, span membership and
  oldest-position pruning. Only duration-based gap tests use the time component alone. Comparing
  just the sort key for identity boundaries can drop an equal-key entry too early.
- **Every source orders an item the same way.** An item reachable from two sources must have the
  same position in both for the shared-entry representation here. Otherwise pruning can occur
  before another source reaches the item, creating a repeat and possibly displacing other results.
  Likewise, copies of one item in two partitions fold only when they share `(time, item id)`.
- **Across engines, ties match only when you make them.** Compare ids as bytes (`COLLATE "C"` in
  Postgres; Elasticsearch `keyword` already sorts by bytes), or use numeric ids; and use one
  timestamp precision on both sides — Postgres keeps microseconds, Elasticsearch `date`
  milliseconds (`date_nanos` keeps more).

### The cap

**Exact mode:** remove only state proved unnecessary by the plan's semantics or by every remaining
consumer passing it. If the next selected row would exceed the state limit, fail that tentative
request explicitly without committing its page or advancing the client's token. The previous
token remains valid, but retrying unchanged capacity may fail again; the application must choose
a documented restart, increased limit, recomputation strategy or external-state alternative.
External state changes the stateless property and must be described as such.

**Approximate mode:** the plan may evict necessary exclusions, but then repeats and omissions from
finite budgets are permitted outcomes. For example, stage 1 serves X, Y; stage 2 has Y, Z and
budget 1. Keeping Y's exclusion produces X, Y, Z. Evicting it produces X, Y, Y and loses Z.

Choose a deterministic eviction rule, including tie-breaks, and enforce it after each selected
candidate, not only at page boundaries. With H = infinity, cap 1 and candidates A@5, B@4, A@3,
B@2, C@1, page-end-only eviction can give different sequences for page sizes 2 and 10. Per-candidate
enforcement removes that particular dependence, but does not restore exactness. Evicting the
furthest expiry is a heuristic; long groups may repeatedly re-enter. Avoid unbounded eviction
history masquerading as a bounded token. Measure the chosen policy and state its result semantics.

### The count horizon

A count horizon carries the chain's selected-row count `selectedTotal`, initially zero, and adds
`born` to each affected entry. This optional scalar is separate from the per-source `served` map.
After selecting a row, increment `selectedTotal` and set its new entry's `born = selectedTotal`.
Before each subsequent candidate, remove entries with `selectedTotal - born >= R`, for a declared
integer R >= 0.
Skipped candidates do not increase this count and do not renew `born`. This convention retains
at most R + 1 entries immediately after selection and at most R before the next candidate, when
each row adds one entry. R = 0 allows another row at the next candidate.

This is a recurrence policy for represented groups, not transparent compression of an unlimited
seen set. Declare where it applies and how recurring rows consume finite budgets. Fixed spans or
item exclusions required for exact cross-stage coverage must not expire by count unless that
weaker suppression is explicitly part of the result definition.

### Declared exceptions

The fixed-data guarantee does not cover the following changes or visibility gaps:
- **Groups merged mid-chain.** The merged group's items carry the surviving key and are skipped
  until a refresh. Refresh the merged row live.
- **Sort keys that change.** An item whose key moves above the cursor is not seen again in this
  chain; one that moves below may be seen twice. Prefer sort keys that never change, or record a
  change as a new item with a new sequence number.
- **Late commits and indexing lag.** Transaction A may allocate sequence 40, transaction B commit
  sequence 41, and the reader anchor at 41 before A commits. Row 40 can then become visible later
  even though it passes the anchor. A visibility watermark must establish that every eligible
  write through it is committed and visible to each relevant source, including indexes and
  replicas. Merely subtracting a short settling delay is a best-effort heuristic. PostgreSQL's
  sequence and Read Committed behavior illustrate why allocated order is not visibility order.
  [PostgreSQL transaction isolation](https://www.postgresql.org/docs/18/transaction-iso.html).

### The head check, safely

The head check reads visibility live and remembers nothing, so it is safe only while what a reader
can see does not grow during the chain. If a partition is granted, or an item un-hidden,
mid-chain, the newer item it finds can lie above the cursor, never scanned — and a group is lost.
Carried exclusions avoid this particular unseen-head suppression, but changed visibility can
still cause omissions, repeats and budget displacement. Binding an access-scope version can
force a restart when scope changes; the server must still check current permissions on every
request. Bind the full relevant access state, not just partitions if item-level visibility can change.

### Testing

- **Exact oracle:** for supported plans with fixed data/scope and sufficient state capacity,
  concatenated selections before presentation equal an independently computed unpaged result.
  Vary page size, batch size and candidate caps, including size 1 and page-size changes mid-chain.
  Walk through empty pages while continuation remains. A capacity error must not look like success
  or exhaustion.
- **Adversarial fixtures:** test ties, overlapping sources, prepared-buffer depletion and refill,
  post-watermark arrivals with old sort keys, late visibility below a raw sequence, and multiple
  disjoint spans for a group. Test declared count horizons and approximate eviction against their
  own policies, including finite-budget displacement, rather than an exact unlimited-seen oracle.
- **Mutation tests:** deletions, sort-key changes and access changes must obey the stated live-data
  limits and never bypass current authorization. These are distinct from fixed-data exactness.
- **Protocol and adapter tests:** forged, expired, oversized and wrong-plan tokens; schema edge
  cases; decompression limits; partial source failures; and permission checks. A page-walking
  client should verify progress and explicit terminal signaling.

### Operating it

- **Metrics:** application skip ratio and source work, scan-cap hits, empty pages with continuation,
  intentional episode/recurrence rows, approximate repeats, exact-capacity errors, cap evictions,
  preparation/refill cost, token size (p50, p99, max), and rejected tokens by reason.
- **Showing new items.** A visibility watermark excludes subsequent arrivals, so a list can show "new items" and restart
  the chain on request, replacing the list. Merging a second, newer chain above the first is
  possible, but needs a reconciliation policy that distinguishes item or episode identities;
  folding by group key alone would hide legitimate separate episodes.
- **Support:** a tool that decodes and validates a token and explains its state. Signed payloads
  are readable without the signing key; validation needs the key, and encrypted payloads need
  decryption. A replay reproduces a page only under the same plan, request, data and visibility.

### When not to use it

- **Results ordered by relevance.** A search engine's scores use statistics over the whole corpus,
  which change as documents arrive, so the order moves even below the anchor. Use the engine's
  point-in-time snapshot there.
- **Sort keys that change often** — a "last activity" order where every change moves the item.
  The declared exception becomes the common case.
- **Totals and random access** ("page 37 of 120"): the pattern pages forward only.
- **Small lists** that fit in one request.
- **One ordered source, with no grouping and no composition:** plain keyset pagination is the
  same thing with less machinery.

---

## Examples

**1. A plain list (H = 0).** The Part 2 data with H = 0: every item is its own row — i1, i2 · i3,
i4 · i5, i6 · i7, i8 · i9. i4 still shows once: the scan folds its two copies. Every entry ends
at the next candidate, so `excl` carries at most one entry between pages.

**2. A priority category first, then the remaining groups.** Use Part 2's fixture with whole groups
(H = infinity) and A as the only group in the priority category. The first stage has budget 1 and
represents A whole. A later request to the remaining stage asks for 3 rows with candidate cap 3;
priority groups are excluded by a stable group-level predicate in the source query. It serves
B and C and skips B's i5; at that cursor C's oldest position is already passed, so C's entry is
gone. B's entry remains at i5 until
the next candidate. This is a short page with continuation. The example uses canonical maps and
tagged entries:
```json
{ "v": 1, "anchor": { "seq": 5000 }, "stage": "remaining",
  "cursor": { "priority": ["2026-11-14T10:00", "i1"], "remaining": ["2026-11-13T12:00", "i5"] },
  "done": ["priority"],
  "predicates": { "excludeCategory": "priority" },
  "excl": { "remaining": [["group", "B", ["2026-11-13T12:00", "i5"], ["2026-11-13T12:00", "i5"]]] } }
```

**3. Search across a database and a search engine.** Search for a given term, with an explicit
preference for items labelled `preferred`. Both stages require the same text match and access;
the label is a priority criterion, not a mandatory list filter. Stage 1 serves matching items with
that label using the database adapter; stage 2 serves remaining matches using the search adapter.
Both return the same immutable ordering and establish the required text-match/visibility semantics. Here the
earlier stage ended at i377; its surviving item exclusions use canonical tagged entries:
```json
{ "v": 1, "anchor": { "seq": 5000 }, "stage": "text",
  "cursor": { "structured": [1763030000000, "i377"], "text": [1763050000000, "i412"] },
  "done": ["structured"],
  "excl": { "structured": [["item", [1763040000000, "i398"]], ["item", [1763030000000, "i377"]]] } }
```

**4. An ordered result merged from three sources.** Sources S1, S2 and S3 supply items ordered
by time (not staged — the plan says so; the vector does not). One cursor per source; each page
takes the newest of the three heads until it is full, resolving equal positions by a stable source
order. IDs in this fixture are disjoint across sources; overlapping identities require retained
exclusions too. Each source supplies its own visibility watermark, so the anchor holds three.
They define a per-source cut, not one atomic snapshot across systems:
```json
{ "v": 1, "anchor": { "S1": 5000, "S2": 812, "S3": 77 },
  "cursor": { "S1": ["2026-11-12T08:00", "i6"], "S2": ["2026-11-12T09:10", "i88"],
              "S3": ["2026-11-11T16:00", "i12"] } }
```

**5. A capped vector.** A viewer scrolls through interleaved groups with H = infinity and required
state reaches its limit. Exact mode returns an explicit capacity error before delivering the
tentative page; it does not drop entries and claim completion. An explicitly approximate plan may
evict entries using its deterministic policy, allowing group repeats and possible loss of distinct
results under finite budgets. See the counterexample in "The cap".

**6. A tampered or stale token.** A token whose signature fails, whose version is unknown, or
whose decoded fields do not validate is rejected with 400; the client starts a fresh chain (page
1, new anchor). Nothing is guessed.

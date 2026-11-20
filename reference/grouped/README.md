# Grouped coverage reference

This optional extension of the [core reference](../README.md) checks grouped
selection with explicit-ID, fixed-span and byte-aware hybrid coverage. Each
representation must produce the same rows and represented members under the same
plan. An independent unpaged oracle and page-boundary auditor check the result.

Inputs are fixed synthetic records, candidate IDs and declared group/episode
membership. This is an in-memory semantics model; its complete membership lookups
do not establish database query cost, bounded discovery or production memory use.
It inherits the public package's rejection of optimized Python.

## A reserved hole in a group

Suppose a group contains A, B and C, in descending order, and a later stage
reserves B. An earlier stage encounters A. Two supported policies give different
results:

| Policy | Earlier row | Later owner row, represented by B |
| --- | --- | --- |
| `subtract` | A represents A and C | B represents B |
| `defer` | The earlier grouped row is skipped | B represents A, B and C |

Both policies prevent a represented member from appearing twice. Under subtraction,
one group can have several disjoint residual rows across stages. A page size and
stage quota count **rows**, not the number of represented members.

The small demonstration adds an unrelated record to make the page transitions
visible. Run from the repository root with Python 3.14 and assertions enabled:

```console
python -S -m reference.grouped --policy subtract --representation hybrid --page-size 1 --show-state
python -S -m reference.grouped --policy defer --representation spans --page-size 1 --show-state
```

Use `--representation ids`, `spans` or `hybrid`; pages default to 50. Each printed
row includes its representative and covered members. The CLI checks every page
boundary and the full result against the oracle. No hydration or service is run.

## Coverage and ownership rules

- A candidate must be eligible before it can represent its group. Group expansion
  uses the supplied `(group, episode)` unit, including members absent from that
  stage's candidate filter. Prior coverage, omissions and applicable reservations
  affect the residual row.
- A span means `group AND lo <= (sort_key, id) <= hi` over the complete fixed
  eligible scope. Every same-group member inside its bounds must be represented.
  Reserved, previously covered, omitted or other-episode holes split new spans;
  interleaving members of another group do not.
- Passing a member's position is insufficient for retirement. A lower remaining
  grouped candidate can expand back to it, and pending ownership can also need it.
  Fixed spans keep their bounds until their last dependency disappears; IDs retire
  individually. The representations can retain different historical sets while
  agreeing on every member still needed.
- Reservations remain explicit IDs in all three coverage modes. Owner precedence
  and yielding are fixed. A selected row resolves its owner's represented members;
  merely consuming a deferred owner candidate does not serve its claim. Completion
  applies the declared release/omit policy to genuinely unserved claims.
- The hybrid compares the actual JSON byte cost of IDs and a span for each maximal
  selected run. It chooses a span only when strictly smaller; ties retain IDs.
  This is a local admission choice, not a global optimizer or a guarantee that its
  retained state remains smaller after other representations retire individual IDs.

These rules are checked against an explicit-ID baseline. They do not make the
reservation policies interchangeable: comparisons hold the policy fixed.

## Source and fixtures

| File | Purpose |
| --- | --- |
| [model.py](model.py) | Fixed grouped plans, raw-source pagination and ownership transitions. |
| [coverage.py](coverage.py) | Exact coverage encoding/decoding and strict byte-cost choice. |
| [oracle.py](oracle.py) | Independent full-result specification without pager or codec calls. |
| [audit.py](audit.py) | Independent interpretation of returned coverage and remaining dependencies. |
| [scenarios.py](scenarios.py) | Sixteen small hand-worked scenarios, including both partial policies. |
| [hybrid_scenarios.py](hybrid_scenarios.py) | Six small hybrid, fragmentation and lifetime fixtures. |
| [generated.py](generated.py) | Seeded finite plans and configurable 50-row-page distributions. |
| [tests/test_public_boundaries.py](tests/test_public_boundaries.py) | Additional read-failure, retry and exact-capacity boundary checks. |
| [mutations.py](mutations.py) | Named deliberately broken pager/codec variants in temporary copies. |
| [provenance.json](provenance.json) | Original source hashes and public-export adaptations. |

No saved private traces, database files or client data are required. The original
local grouped implementation and frozen core baseline are preserved separately.

## Run the checks

```console
python -S -m unittest discover -s reference/grouped/tests -t . -v
python -S -m reference.grouped.mutations
```

The comparison runners regenerate their own fixtures and report correctness and
serialized state sizes. For a small run:

```console
python -S -m reference.grouped.run_hybrid_checks --generated-cases 20 --groups 12 --members 6 --output reference/results/grouped/smoke
```

The full finite comparison uses 600 generated plans, three page/work schedules and
eight distributions of 200 groups × 12 members, with long requests using pages of
50. The optional prior-result check reads only the explicitly supplied file:

```console
python -S -m reference.grouped.run_checks --output reference/results/grouped/ids-spans
python -S -m reference.grouped.run_hybrid_checks --previous-comparison reference/results/grouped/ids-spans/comparison.json --output reference/results/grouped/hybrid
```

Generated outputs stay under ignored `reference/results/` by default. Choose a
fresh `--output` directory to retain earlier measurements. Saved reports include
source hashes, seeds, dimensions and scroll depths; they are not query-latency
benchmarks. [Validation scope](VALIDATION.md) describes the checked cases and
limits of the evidence.

## What this extension does not combine

Sources here are raw complete-prefix reads. Unconsumed fetched candidates are
read again on the next request; there is no persistent prepared buffer. The core's
buffer/refill validation does not validate that combination with grouped rows.

Group/episode labels are fixed inputs; no time horizon or dynamic episode inference
is implemented. Compressed ownership, live writes, engine adapters, hydration,
authorization and server-held revision/replay protocols remain outside this module.
Continuations are trusted logical state. Capacity limits bound the returned
continuation, not transient RAM or every intermediate state.

Covered by the repository's [MIT license](../../LICENSE).

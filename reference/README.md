# Executable core and reservations reference

This is an engine-independent, ID-only reference for the
[kc-state-vector pattern](../kc-state-vector.md). It defines and checks a limited
subset: ordered overlapping stages, selection quotas, explicit-ID coverage and
reservations, prepared buffers, safe retirement, capacity errors and retries over
fixed data. It uses only the Python standard library and synthetic fixtures.

The pager inspects complete finite source membership in memory. Use it to understand
the semantics and check an implementation's results; it does not establish bounded
database query work, production memory use or query latency. Continuations are
trusted logical state, without a production authentication or storage protocol.

## Try it

From a checkout of this repository, using Python 3.14:

Run with assertions enabled. The package rejects `-O`, `-OO` and nonzero
`PYTHONOPTIMIZE` before loading the model, because assertions are needed for the
reference behavior and validation checks. Remove those flags and unset
`PYTHONOPTIMIZE` if the startup error appears.

```console
python -m reference --example core --page-size 3 --show-state
python -m reference --example reservation --page-size 2
python -m reference --example release
python -m reference --example omit
```

No database, dependency installation or local workspace configuration is needed.
The default page size is **50**; page size, batch size and candidate budget are
independent controls. The CLI compares the concatenated result with the oracle.

| Example | Selected IDs | Behavior |
| --- | --- | --- |
| `core` | `51, 49, 52, 50, 48` | A priority quota followed by an overlapping source; page size 3 produces `[51, 49, 52]`, then `[50, 48]`. |
| `reservation` | `A, C, B` | An earlier stage leaves B for its later owner. |
| `release` | `A, C, B` | An owner with zero quota releases B for the remaining source. |
| `omit` | `A, C` | An owner with zero quota deliberately omits its unserved B. |

The result consists of `(stage_name, item_id)` pairs before presentation. An empty
work-limited page can still have continuation; only a missing continuation ends
the traversal. An error does not return a partially successful page.

## Read the implementation

| File | Responsibility |
| --- | --- |
| [model.py](model.py) | Plan validation, paged selection, logical state and capacity enforcement. |
| [oracle.py](oracle.py) | Unpaged specification using source sets and stage quotas; no runtime pager import, cursor or retirement algorithm. |
| [retirement.py](retirement.py) | Separate checks of coverage required at a page boundary. |
| [tests/test_reference.py](tests/test_reference.py) | Expected examples, seeded comparisons, continuation and ownership regressions. |
| [tests/legacy_helpers.py](tests/legacy_helpers.py) | Earlier independent comparison helpers, bundled with the tests. |
| [mutations.py](mutations.py) | Deliberately broken core variants tested in isolated temporary copies. |
| [provenance.json](provenance.json) | Origin hashes and the adaptations made for this public package. |

The pager, oracle and auditor intentionally use distinct algorithms. Agreement is
evidence within the tested domain, not a proof for every plan or adapter.

## Run the checks

```console
python -S -m unittest discover -s reference/tests -t . -v
python -S -m reference.mutations
```

`-S` disables environment-specific package initialization for these standard-library
checks; it does not turn optimization off. CI is configured to run them on Linux
and Windows. Mutation checks must pass their healthy control first; invalid edits,
test errors or timeouts are not successful detections.

The ordinary suite retains the frozen baseline's semantic tests, including its
long traversals. The full tiny-plan enumeration is a separate, longer run:

```console
python -S -m reference.exhaustive --limit 20 --output reference/results/tiny-smoke
python -S -m reference.exhaustive --output reference/results/exhaustive
python -S -m reference.closing --output reference/results/closing
```

A limited run is a smoke check, never an exhaustive result. Generated reports stay
under the ignored `reference/results/` directory; use a fresh output path when
retaining previous evidence. See [validation and limits](VALIDATION.md) for the
preserved baseline evidence, current public checks and unsupported capabilities.

## Scope and origin

This package is adapted from the frozen `core-reservations-v1` local reference.
The original snapshot is retained unchanged; publishing this copy does not create
a new semantic baseline. Its public initializer adds the optimized-execution guard;
the model and oracle retain their original bytes. The public package includes its
test helpers and runs without the original workspace or benchmark files.

Grouped results, spans/hybrid encodings, compressed reservations, dynamic horizons,
live data, hydration, real source adapters and server-held revision protocols are
outside this implementation. Their discussion in the pattern does not imply they
are implemented or validated here. The separate local experiments and engine
benchmarks are not part of this package.

This code is covered by the repository's [MIT license](../LICENSE).

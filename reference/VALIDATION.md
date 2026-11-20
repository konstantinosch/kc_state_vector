# Core validation scope and evidence

The [executable reference](README.md) checks exact selection over fixed, finite
synthetic sources. It is a specification aid for a supported subset of the
[pattern](../kc-state-vector.md), not a production engine or a general proof.

## Supported contract

| Area | Checked behavior |
| --- | --- |
| Sources and order | Stable identities and `(sort_key, id)` order; consistent identity across overlapping sources. |
| Stages | Ordered composition; quotas count selected rows; skipped candidates consume work, not selection quota. |
| Coverage | Explicit IDs retained while a remaining consumer or ownership decision needs them; retirement checked separately. |
| Reservations | Fixed ownership/yielding, earliest-owner precedence, owner completion, release/omit and covered-versus-unserved resolution. |
| Preparation | Raw and buffered sources; separate fetched/consumed progress; buffer refill and incomplete reads. |
| Continuation | JSON round trips, fresh-pager resume, deterministic replay, changing page/batch/work limits and empty pages with continuation. |
| Capacity and failures | Logical entry/JSON-byte budgets at admission checkpoints; failures preserve incoming state and return no partial Page. |

Exact comparisons require fixed eligibility/order, complete source responses,
consistent identity and sufficient state capacity. Plans outside those assumptions
need their own specification and tests. The model can inspect entire source sets;
its correctness does not bound the work of a real database query or grouping lookup.

Python assertions must remain enabled. The public package rejects optimized
execution (`-O`, `-OO` or nonzero `PYTHONOPTIMIZE`) before importing the model.
This preserves the reference's assertion-dependent behavior and validation checks;
`-S` does not disable optimization. The model itself retains the frozen source bytes.

## Preserved baseline evidence

The following describes the original local `core-reservations-v1` validation,
completed before this public export. It is historical evidence, not a claim that
every full matrix runs on each CI build. The frozen archive and bulk generated
traces remain local; the source, deterministic fixture generators and relevant
test cases are included here so the checks can be reproduced.

| Original check | Recorded result |
| --- | --- |
| Frozen local test suite | 78 tests passed, including four archive-integrity tests. |
| Tiny-plan enumeration | 108,800 configurations; 326,400 traversals; 614,459 audited boundaries across three execution profiles. |
| Long traversals | 41 traversals and 3,602 audited boundaries, including two 30,000-ID runs reaching 600 full pages. |
| Read failures | 266 injected failures at 133 read positions, each retried with recovered and fresh sources. |
| Capacity boundaries | 321 exact-fit checks, 378 repeated one-below failures and 189 successful retries with raised capacity. |

The public copy preserves **74 semantic/report tests**. The other four tests
managed the original frozen ZIP and manifest, which are not public package inputs.
Public packaging/provenance checks and mutation-runner tests are additional tests;
their count should not be confused with the original 78-test baseline.

The [provenance manifest](provenance.json) records source hashes and export changes.
Core behavior is preserved. Public package names, bundled test helpers and temporary
test-report locations are packaging adaptations. The public initializer also rejects
optimized execution. The frozen local original is not rewritten to match this package.

## Public execution and mutation checks

CI is configured to run the ordinary suite, including the long 3,000-ID matrix,
on Linux and Windows with Python 3.14. A separate bounded command checks named core mutations
against existing semantic regressions in fresh copies. It deliberately excludes
provenance-hash tests: noticing that a file changed is not evidence that a semantic
bug was detected.

The core mutation runner defines its own exact variants. It does not claim to reproduce
another reviewer's unavailable search/replace patches, or to exercise grouped
coverage. The [grouped module](grouped/VALIDATION.md) has separate mutation checks.
A surviving valid mutant fails the command. A stale or
ambiguous edit, syntax error, test error or timeout is a tooling failure, not a kill.
Healthy controls must pass before mutation results can count.

The current catalogue contains ten named variants. Two continuation-order faults
use existing semantic regressions, without changing the pager or frozen baseline:

| Mutation | Observable failure checked |
| --- | --- |
| `prune_before_decide` | Advance and prune before evaluating the current candidate. After selecting A and B in an earlier stage, the next stage re-emits A where its one-candidate page should be empty with a continuation. |
| `cursor_past_unread_batch` | Advance a raw cursor to the fetched batch tail despite stopping early. With source A/B/C/D, page size 1 and batch size 3, the first two pages become A/D instead of A/B. |

The first mutation targets the candidate decision, not a potentially equivalent
reordering during owner completion. The second targets raw-source resumption;
the separate `retire_at_fetch_cursor` variant still checks premature coverage
retirement for prepared sources. All ten run through the existing Linux/Windows
CI command below; no additional workflow is needed.

Run from a clean checkout, without the original local reference or benchmark tree:

```console
python -S -m unittest discover -s reference/tests -t . -v
python -S -m reference.mutations
```

During initial public-package preparation, a clean copy containing only the proposed
public files passed **85 reference tests** on Windows/Python 3.14.6: 74 preserved
semantic/report tests, four packaging checks and seven mutation-tooling tests.
The healthy mutation control passed and all eight named mutants were detected.
All four CLI examples matched the oracle; a limited 20-configuration-per-family
enumeration smoke run also passed. This was not a rerun of the full enumeration.
The initial core export subsequently passed its documentation and Linux/Windows
reference jobs in [GitHub CI](https://github.com/konstantinosch/kc_state_vector/actions/runs/44636233600).
That run validates the core export, not the later grouped additions.

The optimized-execution safeguard adds three subprocess regressions to that suite:
imports fail before loading the model, CLI/validation runners reject optimization,
and normal prepared pages still resume. They cover `-O`, `-OO`, environment values
`PYTHONOPTIMIZE=1`/`2`, and unoptimized controls including `PYTHONOPTIMIZE=0`.
The current suite therefore contains 88 tests: 74 semantic/report tests, seven
public-package checks and seven mutation-tooling tests.

The full exhaustive runner is opt-in. Its finite declaration domains are not all
possible plans or request histories; a `--limit` run is explicitly incomplete.
Read generated reports alongside their source hashes, configuration and execution
profile limits. Test success cannot establish correctness of a separately written
adapter unless that adapter is checked against the same contracts.

## Outside this core validation

- Grouped rows and spans/hybrids have their own [validation scope](grouped/VALIDATION.md);
  their tests do not establish composition with the core's prepared buffers.
- Predicate-compressed ownership and inferred group episodes remain separate work.
- Dynamic horizons, changing records/order/eligibility and real engine consistency.
- Query latency, throughput, index-only execution, bounded lookup cost and working RAM.
- Authenticated transport, current permissions, hydration and delivery guarantees.
- The separate [placement module](placement/VALIDATION.md) checks revision coordination,
  replay, concurrent proposals and state loss using simulated storage. Real persistence
  and distributed coordination remain outside the reference.

Logical byte/entry checks cover declared state-admission checkpoints, not peak
process memory or every intermediate assignment. Read-failure rollback preserves
the returned result and incoming continuation, not external source side effects
or exactly-once network delivery. No performance claim follows from these checks.

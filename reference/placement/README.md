# Placement reference

This module places the existing [core](../README.md) and [grouped](../grouped/README.md)
selectors behind a continuation protocol. It uses fixed synthetic data and simulated
storage. Selection still has to agree with the independent unpaged oracle.

**One-way inline-or-spill is the default.** Small vectors travel in an authenticated
token. If the complete encoded token would exceed threshold T, the vector is stored
and the response carries a handle. That chain stays server-held until completion.
The default loss policy is exact recovery from an available checkpoint, otherwise
an explicit error.

Two optional contracts reuse that protocol:

| Contract | Behavior | Cost to retain |
| --- | --- | --- |
| One-way, [protocol.py](protocol.py) | Spill above T; never return inline. | Current vector, optional independent checkpoint, coordination head and accepted response. |
| Two-way, [two_way.py](two_way.py) | Spill above T; return inline strictly below T/2. Keep the current form between the thresholds. | The head remains after return. Full-response replay can retain the inline vector inside the accepted response. |
| Two-way with recompute, [recompute.py](recompute.py) | Same thresholds. For accepted inline inputs, retain replay metadata and a response digest instead of the full response. | A retry executes selection again and must reproduce the digest. Handle inputs still need a full-response bridge. |

Returning inline does not make a previously spilled chain stateless. The authoritative
revision head survives. In the recompute option, a handle-to-inline bridge is retained
until the next successful advance, not merely until the client presents its new token.
Handle-to-terminal replay also needs its bridge until explicit replay expiry.

## Run it

Use Python 3.14 from the repository root. No external dependencies or database are
needed. Assertions must be enabled; the parent package rejects `-O`, `-OO` and
nonzero `PYTHONOPTIMIZE`.

```console
python -S -m unittest discover -s reference/placement/tests -t . -v
python -S -m reference.placement.mutations
python -S -m reference.placement.comparison --suite smoke
```

CI is configured to run those commands on Linux and Windows. The comparison smoke
run is deliberately small; it is not the full evidence matrix.

The default import is one-way. Choose an optional service explicitly:

```python
from reference.placement import Service, Store
from reference.placement.scenarios import small_scenarios

adapter = small_scenarios()[0].adapter
service = Service(adapter, threshold=2048, store=Store(recovery=True))
token = service.start(chain="example-chain")
page = service.request(token, page_size=3, batch_size=3, candidate_budget=20)
# Resume with page["continuation"] until it is None.

# Optional: from reference.placement.two_way import Service
# Optional: from reference.placement.recompute import Service, Store
```

The built-in signing key is a test key, not a deployment credential. Chain identity,
key management, authorization, transport and storage adapters are integration work.

## Recovery and retries

The three state-loss policies are explicit choices, not interchangeable guarantees:

| Policy | If the current stored vector is unavailable |
| --- | --- |
| `exact` (default) | Restore a matching checkpoint if available; otherwise raise `MissingState`. |
| `best_effort` | Start a new raw-order tail from the saved full cursor. Mark the response approximate; stage/group history is lost, so repeats and omissions are possible. |
| `restart` | Start a new chain and return a replacement action. The client replaces its list instead of appending a purported continuation. |

An accepted request is identified by the incoming token and request parameters.
Concurrent proposals use conditional publication. A lost response can be replayed
while its record is available. Once a chain has an accepted server record, stale
revisions and conflicting request parameters are explicit errors. Never-spilled
inline inputs have no revision head and can be independently replayed with different
request parameters. The first spill records its accepted transition so retrying
the old inline input recovers the same response and handle.

Recompute uses the original input and request parameters, including page size,
batch size and work limit. It verifies the complete response digest before returning
anything. A changed selector result, work count or continuation is an error. This
requires repeatable selection; the digest detects a mismatch, it does not create a
snapshot or guarantee a retry will succeed under live data.

## Compare the storage trade-offs

```console
python -S -m reference.placement.comparison --suite core
python -S -m reference.placement.comparison --suite grouped
python -S -m reference.placement.comparison --suite full
```

Each case supplies all three contracts with the same chain identity, plan, request
parameters, threshold and checkpoint policy. Every selection invokes the real
selector; stored-response retries deliberately skip selection.
The report checks rows and retained state against the independent oracle and auditor.
The core fixtures grow and retire coverage through ordinary stages; they do not use
dynamic horizons. The grouped fixtures use 200 groups of 12 members, fixed ownership,
both partial-reservation policies and ID/span/hybrid coverage.

Reports go under ignored `reference/results/placement/`. See [findings](FINDINGS.md)
for reproducible comparisons and [validation](VALIDATION.md) for the boundary of
the evidence. Storage is canonical serialized record size, not process memory.
Byte-pages sum that size at response boundaries, not over elapsed time. Retry work
is measured as selector calls and candidates/reads, not latency.

## Files and limits

| File | Purpose |
| --- | --- |
| [adapters.py](adapters.py) | Wrap the public core/grouped selector and independent oracle. |
| [scenarios.py](scenarios.py) | Reuse finite public distributions and the two-growth-cycle core fixture. |
| [comparison.py](comparison.py) | Reproduce paired storage and retry-work comparisons. |
| [mutations.py](mutations.py) | Named broken variants, each checked in a temporary public-only copy. |
| [tests/test_protocol.py](tests/test_protocol.py) | One-way transition and failure contracts. |
| [tests/two_way/test_protocol.py](tests/two_way/test_protocol.py) | Hysteresis, bridges and retries across a return. |
| [tests/recompute/test_protocol.py](tests/recompute/test_protocol.py) | Digest-verified retries and bridge lifetime. |
| [provenance.json](provenance.json) | Export origins, hashes and packaging adaptations. |

The store is a collection of dictionaries protected by a lock, not Redis or a database.
It simulates separate loss domains and atomic publication. Silent loss of every
coordination record cannot be distinguished from a never-spilled inline chain:
the model requires an authoritative registry or an explicit unavailable outcome.
Retaining only a handle cannot recover a lost vector. Expiry and eviction do not
silently restart exact traversal.

This package does not include compressed ownership, dynamic horizons, grouped
prepared buffers, live-data consistency, hydration or engine benchmarks. It is a
reference contract and test aid, not a production pagination server.

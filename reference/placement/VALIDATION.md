# Placement validation scope

This reference tests continuation placement around the public core and grouped
selectors. The three protocol implementations retain their original behavior;
[provenance](provenance.json) records export changes and source hashes. All inputs
are finite synthetic fixtures. The frozen core baseline is a separate, unchanged
local artifact.

## Supported contract

| Area | Evidence |
| --- | --- |
| One-way default | Inline tokens, first spill, handle resume, fixed-token threshold checks and terminal responses. |
| Accepted transitions | Conditional publication, duplicate proposals, response loss, request-parameter conflicts, stale revisions and independent worker retries. |
| State loss | Hot-state eviction with exact checkpoint recovery; explicit rejection when exact recovery is unavailable; declared best-effort and restart outcomes. |
| Recovery identity | Distinct lost chains produce distinct fallback chains; retrying the same fallback produces the same accepted identity. |
| Two-way option | Strict spill above T and return below T/2, no switch inside the band, repeated growth/retirement, replay across both transitions and retained coordination after returning inline. |
| Recompute option | Actual selector invocation for inline-input retries, original request parameters, full-response digest checks, bridge lifetime and replay expiry. |
| Boundaries | Failed selection/publication, concurrent preparation, missing or corrupt records, checkpoint loss and replay expiry between preparation and commit. |
| Selection | Exact oracle prefixes, final unpaged agreement and independent retained-coverage audits; placement must not change logical results or healthy selector work. |

The tests include targeted failures that cannot safely be inferred from a long
healthy run. In particular, the cross-chain identity, changed response digest,
changed request parameters and early bridge-release regressions remain dedicated
guards. Some named mutations are caught by only one such test.

## Reproduce the checks

Use Python 3.14 with assertions enabled:

```console
python -S -m unittest discover -s reference/placement/tests -t . -v
python -S -m reference.placement.mutations
python -S -m reference.placement.comparison --suite smoke
```

The test suite preserves 126 existing protocol/adapter/checking tests: 63 one-way,
28 two-way and 35 recompute. Public export, mutation-tool and comparison tests are
additional. One historical renderer assertion was replaced with terminal-denominator
checks; the public renderer has its own regressions. Horizon-specific helper paths
were removed, so all public core/grouped results go through their normal audits.

The publication candidate was checked on Windows/Python 3.14 in a clean copy
containing only the explicitly allowlisted public files. It passed 88 core tests,
89 grouped tests and 147 placement tests. All 31 placement mutants were detected
with passing healthy controls. These local results do not stand in for a Linux
run. GitHub's Linux and Windows jobs provide a separate post-push check.

The mutation catalogue has 31 named variants: seven one-way, twelve two-way, ten
recompute and two cross-chain identity breaks. Healthy controls must pass first.
Only semantic assertion failures count as detections. Invalid edits, syntax/test
errors, timeouts, skipped detectors and surviving mutations fail the command.
The runner copies public Python files into temporary directories; provenance hashes
are deliberately not semantic detectors.

CI is configured to run the suite, mutation catalogue and comparison smoke on Linux
and Windows. The larger comparison is an opt-in command:

```console
python -S -m reference.placement.comparison --suite full --workers 3
```

The full comparison has 93 matching-input cases: 12 core controls and 81 grouped
cases. Every case runs all three strategies once healthily and once with failures
and retries, for 558 traversals. The core fixtures use ordinary growth and retirement
with pages of 50; grouped cases use 2,400 members, pages of 50 and selected 1/3/7
controls. Thresholds are 2/4/8 KiB. Backup on/off is compared for the core; grouped
cases retain the checkpoint backup.

The runner records source hashes before execution and rejects changes during a run.
Outputs must go to a fresh directory. JSON, CSV and generated findings are deterministic
for matching source bytes and configuration, including worker-count independence.
Terminal responses are reported separately from inline continuations. Single-response
cases are never evidence of a mid-chain transition.

## Reading the measurements

See [FINDINGS.md](FINDINGS.md) for the same-input one-way versus optional-contract
comparison, grouped overhead decomposition and retry costs. The published fixtures
do not include the separate dynamic-horizon experiments or their size measurements.

Storage includes hot vectors, checkpoint copies, coordination heads, retained full
responses and redirects. Releasing a dedicated vector can move its bytes into a replay
response. The report therefore gives total storage and its components, not just a
count of vector-resident pages. Byte-pages sample serialized storage at response
boundaries, including the retained terminal response; they are not a time integral
or a process-memory measurement.

The failure traversal retries every accepted response once, including terminal
responses. That 100% retry schedule exposes the maximum tested replay work per
accepted request, not a prediction of product traffic. Publication/concurrency/loss
probes are counted separately. Selector calls and source reads measure the reference
adapter, not SQL queries or latency.

## Outside this validation

- Production signing keys, authorization, hydration, transport and storage encryption.
- Real persistent storage, distributed locks/transactions, failover and operational TTLs.
- Live-data snapshots, arbitrary source failures or unrepeatable external side effects.
- Compressed reservation ownership, dynamic horizons or grouped prepared buffers.
- Query performance, process memory, service capacity or acceptable token sizes for a product.

The simulated registry is authoritative. If all its records can vanish silently,
an old unmanaged inline token cannot reveal that the chain once spilled. The
implementation must preserve that coordination history or report registry loss
explicitly. The reference's lock and dictionaries do not establish how a real
service will meet this requirement.

Recomputation verifies an accepted response; it cannot repair nondeterministic
selection. Failed verification is an explicit outcome, not permission to send a
different page. A fallback marked approximate or restart is likewise not evidence
of exact continuation after state loss.

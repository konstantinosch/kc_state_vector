# Check a staged-core pager

This kit compares a pager's selected IDs with an independent complete result on
small, fixed fixtures. It runs without packages, network access or a database.
CI is configured for Python 3.14 on Linux and Windows. The kit checks the
adapter that actually runs; a wrapper must not repair its output to match the oracle.

Contents: [run the kit](#run-the-kit), [adapter interface](#adapter-interface),
[optional capabilities](#optional-capabilities), [fixtures and ordering](#fixtures-and-ordering),
[reports and limits](#reports-and-limits), [without execution](#without-execution).

## Run the kit

From the installed `kc-state-vector` folder:

```console
python -S scripts/check_pager.py --adapter scripts/sample_pager.py
python -S scripts/check_pager.py --adapter path/to/my_adapter.py --json ../new-report.json
```

The [sample pager](../scripts/sample_pager.py) is a small, working control with all
capabilities. The [template](../scripts/template_pager.py) deliberately raises
`NotImplementedError` until adapted. It cannot serve as a passing implementation.
The candidate adapter is trusted local Python code. Running it is not a security
sandbox and does not grant permission for unrelated side effects.

`--adapter` is a Python file exporting `make_pager`. Relative user paths resolve
from the invocation's working directory; bundled resources resolve from the kit's
location. Repeated `--fixture path.json` arguments add fixtures to the mandatory
bundled suite. They never replace that suite. JSON reports use a new output path.
Choose that path outside the installed skill folder.
Use `--max-page-size N` to change the consecutive page-size range; fixed and changing
schedules still include the named witnesses. `--help` lists the execution guards.

## Adapter interface

The required interface is small:

```python
CAPABILITIES = set()  # Omitting this declaration also selects the required tier.

def make_pager(fixture):
    def page(continuation, *, page_size):
        # Call the candidate implementation with these inputs.
        # Return its selected rows and continuation, without repairing them.
        raise NotImplementedError
    return page
```

A response contains `rows` and `continuation`:

```json
{"rows": ["51", "49", "52"], "continuation": {"candidate_state": "opaque"}}
```

Rows may instead be objects with exactly `stage` and `id`. Stage labels are checked
only when the adapter declares that capability. Otherwise the report says attribution
was skipped; the checker does not invent labels for plain IDs or infer them from the
expected result.

Input `null` starts a traversal. Output `null` ends it. Any JSON object, including
an empty object, or a nonempty token string is a nonterminal continuation. The
checker JSON-roundtrips continuation state between calls; a Python object reference
cannot carry progress. It never calls the pager again with terminal `null`.

The factory and page call must leave their supplied fixture and continuation
unchanged. Every request and same-input retry uses a fresh pager instance. Repeated
input and parameters must reproduce the same rows and logical continuation; retry
rows are compared but never appended twice. The continuation must carry the progress
needed to resume. An opaque server handle whose state exists only in a prior pager
instance is outside this profile.

For each traversal with a second request, the checker also resumes that request's
saved input in a separate process and compares its response. This tests one
checkpoint per traversal; it does not test every persisted state across processes.

Short and empty advancing pages are allowed in the required tier. The checker
continues while a continuation remains, checks each returned ID against the next
oracle prefix, and requires the complete result at termination. Repeated nonterminal
state and guard exhaustion cannot pass.

## Optional capabilities

Declare only the names the actual implementation supports:

```python
CAPABILITIES = {"batch_size", "candidate_budget", "stage_labels"}
```

| Capability | Added interface and checks |
| --- | --- |
| `batch_size` | The checker passes this keyword and varies it independently of page size. Controlled unread-batch-tail witnesses become available. |
| `candidate_budget` | The checker passes this keyword. Return an integer `consumed` count, including skipped raw candidates. It checks `len(rows) <= consumed <= candidate_budget`, and a short nonterminal page must have spent that budget. Controlled duplicate-only empty-page witnesses become available. |
| `stage_labels` | Every row is `{stage, id}` and both values are compared with the oracle. |

Undeclared batch/budget keywords are never passed. A `consumed` field without the
candidate-budget capability does not establish a work-budget check. Unknown
capability names are configuration errors. Every report lists `checks_run` and
`checks_skipped`; a pass is always limited to the capabilities and fixtures run.

The budget tier models raw candidates before deduplication. A database query that
filters duplicates before returning candidates can have a different valid work
trace. It can use the required tier without claiming raw-candidate budgeting. Do
not invent counters or change returned rows to claim a capability. Counts are
adapter-reported; the kit does not independently measure source reads or fetch size.

The [named-mutation runner](../scripts/mutations.py) checks the sample pager's three
deliberate faults, with healthy controls:

```console
python -S scripts/mutations.py
```

It distinguishes wrong output from import errors, exceptions, invalid edits,
timeouts and incomplete checks. Only the intended semantic divergence counts as
detecting a named fault. The [worked examples](self-check-examples.md) identify the
relevant fixture and expected result.

## Fixtures and ordering

Fixtures describe a global table of unique IDs and integer sort keys, plus ordered
stages with membership IDs and selected-row quotas. The
[priority fixture](../scripts/fixtures/priority_51_49_48.json) is a complete example.
Record/membership array order is not selection order. Each stage sorts by
`(sort_key, id)` descending; IDs use case-sensitive ordinal ASCII comparison.
Decimal-looking IDs stay strings, so tied `"2"` sorts before `"10"`.

Quotas count selected rows, not skipped duplicates; `null` means unlimited and zero
means no selected rows from that stage. An ID selected in one stage is excluded
from all later stages. Category membership alone does not imply selection.

The fixture profile uses integer sort keys in `[-(2**53 - 1), 2**53 - 1]` and
nonempty printable ASCII IDs/stage names of at most 128 characters. Ascending
order, timestamps and multi-column keys require an explicit mapping that preserves
the entire total order and distinct positions, including ties. Mapping can support
a fixture comparison; it does not independently validate a database's collation or
timestamp precision. Broader order domains are outside the built-in profile.

The [oracle](../scripts/oracle.py) computes the complete staged result without
cursors, pages, retained coverage or candidate pager imports. A passing candidate
is evidence on these fixtures, not a proof for every possible plan.

## Reports and limits

The report identifies the adapter, fixture/schedule, declared capabilities, checks
run/skipped and effective guards. The first row mismatch includes the request,
within-page/global position, expected versus actual ID, and stage where checked.
Premature termination uses an explicit end marker; an empty page with continuation
is not exhaustion. Exceptions and configuration/guard failures are reported
separately from result mismatches.

On a failed or incomplete run, `checks_run` lists enabled checks, not a claim that
all of them finished. The adapter hash covers its entry file only; imported helper
files are not included. Keep the adapter and its dependencies unchanged during a run.

Exit `0` means all enabled checks completed and passed; `1` means an observable
contract mismatch; `2` means invalid configuration, an exception or an incomplete
check. A limit is not a semantic mutation detection. Worker deadlines use a
killable process, with a separate response channel and bounded diagnostics. There
is no claimed process-memory sandbox. See the invocation's report for exact bounds.
Continuation nesting is limited to 64 containers; the worker envelope has a
separate allowance so its wrappers do not reduce that limit.

This kit covers finite, immutable, ungrouped staged sources with explicit-ID
overlap. It does not validate reservations, prepared buffers, merged-source
operators, grouping, placement, live changes, authentication, hydration, query
performance or production state-size guarantees. Keeping more coverage than
necessary may pass; minimal vectors and safe retirement for every possible input
are not established by a finite output comparison.

## Without execution

If Python is unavailable, use the [readable fixture results](self-check-examples.md).
Walk the relevant fixture page by page, noting each consumed position, selected row
and retained exclusion. Continue through empty pages. Compare the concatenated IDs
and available stage labels with the expected result; include the named boundary
cases before suggesting the implementation is ready for an executable check.

Report an **unexecuted walkthrough**, with assumptions and unresolved steps. Do not
claim an automated PASS, mutation detection, runtime result or production guarantee.
A design-only request needs analysis, not an invented implementation to satisfy a
tool-running instruction.

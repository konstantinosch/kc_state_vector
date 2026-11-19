"""Independent legacy cross-checks, vendored from review_checks.py.

The three function bodies are unchanged from core-reservations-v1.
stage is the small dependency used by unpaged_staged_oracle.
These helpers do not import the reference pager or its oracle.
See ../provenance.json for source and function-body hashes.
"""


def stage(source, already_served, budget):
    """Unpaged stage semantics: exclude prior output, then apply the budget."""
    return [item for item in source if item not in already_served][:budget]


def prepared_buffer_model(source, prior_output, budget, page_sizes, scan_cap, size):
    """Carry unused fetched candidates and refill after overlap removal.

    The initial buffer is prepared before the prior stage's output is known.
    `offset` is the read position behind that retained buffer, so fetching does
    not lose candidates that the serving loop has not consumed. The candidate
    cap applies to serving, not to a claim about physical preparation cost.
    """
    capacity = min(size, budget)
    buffer = list(source[:capacity])
    offset = len(buffer)
    maximum_buffer = len(buffer)
    output, pages, work = [], [], []
    while len(output) < budget and (buffer or offset < len(source)):
        page, consumed = [], 0
        requested = page_sizes[len(pages) % len(page_sizes)]
        while (
            len(page) < requested
            and len(output) + len(page) < budget
            and consumed < scan_cap
        ):
            if not buffer:
                buffer = list(source[offset:offset + capacity])
                offset += len(buffer)
                maximum_buffer = max(maximum_buffer, len(buffer))
            if not buffer:
                break
            candidate = buffer.pop(0)
            consumed += 1
            if candidate not in prior_output:
                page.append(candidate)
        output.extend(page)
        pages.append(page)
        work.append(consumed)
    return output, pages, work, maximum_buffer


def unpaged_staged_oracle(sources, budgets, anchor):
    """Direct source-minus-prior-output definition; no paging state."""
    output = []
    for source, budget in zip(sources, budgets):
        eligible = [row[1] for row in sorted(source, reverse=True) if row[2] <= anchor]
        output.extend(stage(eligible, set(output), budget))
    return output

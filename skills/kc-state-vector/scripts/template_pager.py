"""Starting adapter only. Replace the placeholder with your own pager calls."""


# Add only the capabilities your adapter actually implements:
# {'batch_size', 'candidate_budget', 'stage_labels'}
CAPABILITIES = set()


def make_pager(fixture):
    """Configure your implementation from this immutable synthetic fixture.

    Return its selected IDs in order. Do not repair, deduplicate or sort its
    output inside this wrapper. A candidate_budget capability also requires an
    honest consumed count; stage_labels requires {stage, id} rows.
    """
    def page(continuation, *, page_size):
        raise NotImplementedError('Connect your pager before running the B3 checker')

    return page

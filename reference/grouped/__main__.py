"""Show a small grouped reservation example and independently check each page."""
import argparse
import json

from .audit import audit_boundary
from .model import Pager
from .oracle import unpaged_oracle
from .scenarios import targeted_scenarios


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--policy', choices=('subtract', 'defer'), default='subtract')
    parser.add_argument('--representation', choices=('ids', 'spans', 'hybrid'), default='hybrid')
    parser.add_argument('--page-size', type=int, default=50)
    parser.add_argument('--batch-size', type=int, default=50)
    parser.add_argument('--candidate-budget', type=int, default=1000)
    parser.add_argument('--show-state', action='store_true')
    args = parser.parse_args(argv)
    if min(args.page_size, args.batch_size, args.candidate_budget) < 1:
        parser.error('page, batch and candidate limits must be positive')
    case = next(case for case in targeted_scenarios() if case.name == 'middle_hole_' + args.policy)
    history, state = [], None
    maximum = sum(len(stage.candidate_ids) for stage in case.plan.stages) + len(case.plan.stages) + 1
    for request in range(1, maximum + 1):
        page = Pager(case.plan, representation=args.representation).page(
            state, page_size=args.page_size, batch_size=args.batch_size,
            candidate_budget=args.candidate_budget)
        history.extend(page.rows)
        audit_boundary(case.plan, page.state, history)
        result = {'request': request, 'rows': page.rows, 'consumed': page.consumed,
                  'fetched': page.fetched, 'reads': page.reads, 'has_more': page.state is not None}
        if args.show_state:
            result['state'] = page.state
        print(json.dumps(result))
        if page.state is None:
            break
        state = json.loads(json.dumps(page.state))
    else:
        raise SystemExit('The finite traversal failed to make progress.')
    if history != unpaged_oracle(case.plan):
        raise SystemExit('Grouped result differs from the independent oracle.')
    print(json.dumps({'oracle_match': True, 'policy': args.policy,
                      'representation': args.representation,
                      'rows': len(history), 'represented_members': sum(len(row[2]) for row in history)}))


if __name__ == '__main__':
    main()

"""Run a small ID-only example and compare its pages with the unpaged oracle."""

import argparse
import json

from .model import Item, Pager, Plan, Reservation, Stage
from .oracle import unpaged_oracle


def example(name):
    if name == "core":
        records = {str(value): Item(str(value), value) for value in range(48, 53)}
        return Plan((Stage("priority", tuple(records[value] for value in ("51", "49", "48")), 2),
                     Stage("remaining", tuple(records.values()))))
    records = {identity: Item(identity, key) for identity, key in (("A", 3), ("B", 2), ("C", 1))}
    stages = [Stage("first", tuple(records.values()), 2),
              Stage("owner", (records["B"],), 1 if name == "reservation" else 0, buffer_size=1)]
    if name != "reservation":
        stages.append(Stage("fallback", tuple(records.values())))
    return Plan(tuple(stages), (Reservation("owner", ("B",), on_unserved="release" if name == "release" else "omit"),))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--example", choices=("core", "reservation", "release", "omit"), default="reservation")
    parser.add_argument("--page-size", type=int, default=50)
    parser.add_argument("--batch-size", type=int, default=50)
    parser.add_argument("--candidate-budget", type=int, default=1000)
    parser.add_argument("--show-state", action="store_true")
    args = parser.parse_args()
    plan = example(args.example)
    pager = Pager(plan)
    state, selected = None, []
    max_pages = sum(len(stage.items) for stage in plan.stages) + len(plan.stages) + 1
    for number in range(1, max_pages + 1):
        page = pager.page(state, page_size=args.page_size, batch_size=args.batch_size,
                          candidate_budget=args.candidate_budget)
        selected.extend(page.rows)
        result = {"page": number, "rows": page.rows, "consumed": page.consumed,
                  "fetched": page.fetched, "reads": page.reads, "has_more": page.state is not None}
        if args.show_state:
            result["state"] = page.state
        print(json.dumps(result))
        if page.state is None:
            break
        state = json.loads(json.dumps(page.state))
    else:
        raise SystemExit("The finite traversal failed to make bounded progress.")
    expected = unpaged_oracle(plan)
    if selected != expected:
        raise SystemExit(f"Oracle mismatch: expected {expected!r}; selected {selected!r}")
    print(json.dumps({"oracle_match": True, "selected_ids": [identity for _, identity in selected]}))


if __name__ == "__main__":
    main()

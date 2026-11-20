"""Selection and fallback meaning, independent of placement transport."""

import copy
import json
import unittest

from reference.model import Item, Plan, Stage

from reference.placement.adapters import Adapter, parameters
from reference.placement.scenarios import distribution_scenarios, small_scenarios


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.cases = {case.name: case for case in small_scenarios()}

    def test_every_small_exact_traversal_matches_independent_oracle_and_auditor(self):
        for name, case in self.cases.items():
            for params in ({"page_size": 1, "batch_size": 1, "candidate_budget": 1},
                           {"page_size": 50, "batch_size": 2, "candidate_budget": 3}):
                with self.subTest(name=name, params=params):
                    rows, state = [], None
                    for _ in range(50):
                        incoming = copy.deepcopy(state)
                        page = case.adapter.select(state, params)
                        self.assertEqual(incoming, state)
                        self.assertEqual(page, json.loads(json.dumps(page)))
                        self.assertEqual(page, case.adapter.select(state, params))
                        rows.extend(page["rows"])
                        state = page["state"]
                        case.adapter.audit(state, rows)
                        self.assertEqual(rows, case.adapter.expected_rows()[:len(rows)])
                        if state is None:
                            break
                    else:
                        self.fail("Small traversal did not finish")
                    self.assertEqual(rows, case.adapter.expected_rows())

    def test_last_representative_can_repeat_previously_selected_priority_items(self):
        adapter = self.cases["priority_duplicate"].adapter
        page = adapter.select(None, {"page_size": 3})
        self.assertEqual([row[1] for row in page["rows"]], ["51", "49", "52"])
        fallback = adapter.best_effort(adapter.position(page["rows"][-1][1]), {})
        previously_seen = {row[1] for row in page["rows"]}
        repeated = previously_seen.intersection(row[1] for row in fallback["rows"])
        self.assertEqual(repeated, {"51", "49"})

    def test_last_representative_can_omit_unseen_higher_items(self):
        adapter = self.cases["priority_omission"].adapter
        page = adapter.select(None, {"page_size": 1})
        self.assertEqual(page["rows"], [["priority", "48", ["48"]]])
        fallback = adapter.best_effort(adapter.position("48"), {})
        self.assertEqual(fallback["rows"], [])
        self.assertIsNone(fallback["state"])
        self.assertEqual([row[1] for row in adapter.expected_rows()[1:]], ["52", "51", "50", "49"])

    def test_grouped_fallback_can_repeat_a_member_even_without_repeating_the_representative(self):
        adapter = self.cases["middle_hole_subtract_hybrid"].adapter
        first = adapter.select(None, {"page_size": 1})
        self.assertEqual(first["rows"], [["early", "A", ["A", "C"]]])
        fallback = adapter.best_effort(adapter.position("A"), {})
        self.assertEqual([row[1] for row in fallback["rows"]], ["X", "B", "C"])
        self.assertNotIn("A", [row[1] for row in fallback["rows"]])
        self.assertIn("C", [row[1] for row in fallback["rows"]])

    def test_raw_fallback_reintroduces_an_intentionally_omitted_reservation(self):
        adapter = self.cases["defer_zero_omit_ids"].adapter
        self.assertEqual(adapter.expected_rows(), [["fallback", "A", ["A", "C"]]])
        fallback = adapter.best_effort(adapter.position("A"), {})
        self.assertIn("B", [row[1] for row in fallback["rows"]])

    def test_plain_suffix_matches_full_tuple_order_despite_ties(self):
        adapter = self.cases["tied_plain"].adapter
        first = adapter.select(None, {"page_size": 1})
        self.assertEqual(first["rows"][0][1], "D")
        state = {"cursor": adapter.position("D")}
        output = []
        while state is not None:
            page = adapter.best_effort(state["cursor"], {"page_size": 1})
            output.extend(row[1] for row in page["rows"])
            state = page["state"]
        self.assertEqual(output, ["C", "B", "A"])

    def test_best_effort_obeys_page_work_and_batch_limits_and_retains_unread_tail(self):
        adapter = Adapter(Plan((Stage("plain", tuple(Item(str(i), i) for i in range(10))),)))
        first = adapter.best_effort(None, {"page_size": 1, "batch_size": 4, "candidate_budget": 7})
        self.assertEqual(first["work"], {"consumed": 1, "fetched": 4, "reads": 1})
        self.assertEqual(first["state"], {"cursor": [9, "9"]})
        next_page = adapter.best_effort(first["state"]["cursor"],
                                        {"page_size": 50, "batch_size": 3, "candidate_budget": 4})
        self.assertEqual([row[1] for row in next_page["rows"]], ["8", "7", "6", "5"])
        self.assertEqual(next_page["work"], {"consumed": 4, "fetched": 4, "reads": 2})
        self.assertEqual(next_page["state"], {"cursor": [5, "5"]})

    def test_invalid_cursors_and_parameters_are_rejected(self):
        adapter = self.cases["tied_plain"].adapter
        for cursor in ((7, "A"), [7, "unknown"], [8, "A"], [True, "A"], [7], {}):
            with self.subTest(cursor=cursor), self.assertRaises(ValueError):
                adapter.best_effort(cursor, {})
        for params in (None, [], {"page_size": 0}, {"batch_size": True},
                       {"candidate_budget": -1}, {"page_size": 1.5}, {"offset": 2}):
            with self.subTest(params=params), self.assertRaises(ValueError):
                adapter.best_effort(None, params)
        self.assertEqual(parameters({}), {"page_size": 50, "batch_size": 50, "candidate_budget": 1000})

    def test_binding_includes_representation_and_scope(self):
        representations = {self.cases[f"middle_hole_subtract_{mode}"].adapter.binding
                           for mode in ("ids", "spans", "hybrid")}
        self.assertEqual(len(representations), 3)
        plan = Plan((Stage("plain", (Item("A", 1),)),), scope="one")
        self.assertEqual(Adapter(plan).binding, Adapter(plan).binding)
        other = Plan(plan.stages, scope="two")
        self.assertNotEqual(Adapter(plan).binding, Adapter(other).binding)
        with self.assertRaises(ValueError):
            Adapter(plan, "spans")

    def test_configurable_distributions_reuse_27_public_plans(self):
        cases = distribution_scenarios(size=100, groups=2, members=4, seed=123)
        self.assertEqual(len(cases), 27)
        self.assertEqual(len({case.name for case in cases}), 27)
        self.assertEqual(sum(case.adapter.grouped for case in cases), 24)
        self.assertEqual({case.name for case in cases if not case.adapter.grouped},
                         {"core_growth_control", "partial_owner_release", "partial_owner_omit"})
        for case in cases:
            self.assertTrue(case.adapter.expected_rows())


if __name__ == "__main__":
    unittest.main()

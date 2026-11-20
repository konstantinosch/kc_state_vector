"""Hand-worked outputs anchor the independent whole-result oracle."""

import unittest

from reference.grouped.oracle import unpaged_oracle
from reference.grouped.scenarios import long_scenarios, targeted_scenarios


EXPECTED_ROWS = {
    "middle_hole_subtract": [
        ("early", "A", ("A", "C")), ("owner", "B", ("B",)), ("rest", "X", ("X",))],
    "middle_hole_defer": [
        ("owner", "B", ("A", "B", "C")), ("rest", "X", ("X",))],
    "reserved_head_subtract": [
        ("early", "B", ("B", "C")), ("owner", "A", ("A",))],
    "reserved_tail_subtract": [
        ("early", "A", ("A", "B")), ("owner", "C", ("C",))],
    "resolver_reachability": [
        ("first", "A", ("A",)), ("grouped", "B", ("B",)), ("grouped", "C", ("C",))],
    "defer_zero_release": [("fallback", "A", ("A", "B", "C"))],
    "defer_zero_omit": [("fallback", "A", ("A", "C"))],
    "partial_owner_release": [
        ("early", "A", ("A",)), ("early", "D", ("D",)),
        ("owner", "B", ("B", "C")), ("fallback", "E", ("E", "F"))],
    "partial_owner_omit": [
        ("early", "A", ("A",)), ("early", "D", ("D",)), ("owner", "B", ("B", "C"))],
    "overlapping_subtract_release": [
        ("early", "A", ("A",)), ("owner", "B", ("B", "C")),
        ("later_owner", "D", ("D",)), ("rest", "X", ("X",))],
    "overlapping_defer_release": [
        ("later_owner", "C", ("A", "B", "C", "D")), ("rest", "X", ("X",))],
    "overlapping_defer_omit": [
        ("later_owner", "D", ("A", "D")), ("rest", "X", ("X",))],
    "selective_yield_preemption": [
        ("early", "A", ("A", "B", "C")), ("owner", "D", ("D",))],
    "episode_holes": [
        ("early", "A", ("A", "C")), ("owner", "B", ("B",)), ("rest", "X", ("X",))],
    "tied_positions": [
        ("early", "C", ("C", "A")), ("owner", "B", ("B",)), ("rest", "X", ("X",))],
    "candidate_scope_and_final_grouping": [
        ("priority", "A", ("A", "B", "C")), ("last", "X", ("X", "Y"))],
}


class ScenarioTests(unittest.TestCase):
    def test_every_targeted_fixture_has_a_hand_worked_result(self):
        cases = targeted_scenarios()
        self.assertEqual(len(cases), 16)
        self.assertEqual({case.name for case in cases}, set(EXPECTED_ROWS))
        self.assertEqual(len({case.name for case in cases}), len(cases))
        for case in cases:
            with self.subTest(case=case.name):
                self.assertTrue(case.description)
                actual = unpaged_oracle(case.plan)
                self.assertEqual(actual, EXPECTED_ROWS[case.name])
                members = [identity for _, _, group in actual for identity in group]
                self.assertEqual(len(members), len(set(members)))

    def test_long_defaults_have_real_page_50_depth_and_full_member_coverage(self):
        cases = long_scenarios()
        self.assertEqual(len(cases), 4)
        for case in cases:
            with self.subTest(case=case.name):
                rows = unpaged_oracle(case.plan)
                expected_rows = 400 if case.name == "long_alternating_members_subtract" else 200
                self.assertEqual(len(rows), expected_rows)
                self.assertGreaterEqual(len(rows) // 50, 4)
                represented = [identity for _, _, members in rows for identity in members]
                self.assertEqual(len(represented), 2400)
                self.assertEqual(set(represented), {record.id for record in case.plan.records})

    def test_long_dimensions_are_configurable_without_changing_coverage(self):
        for case in long_scenarios(groups=7, members=5):
            with self.subTest(case=case.name):
                rows = unpaged_oracle(case.plan)
                expected_rows = 14 if case.name == "long_alternating_members_subtract" else 7
                self.assertEqual(len(rows), expected_rows)
                represented = [identity for _, _, members in rows for identity in members]
                self.assertEqual(len(represented), 35)
                self.assertEqual(set(represented), {record.id for record in case.plan.records})

    def test_long_fixture_rejects_meaningless_dimensions(self):
        for groups, members in ((1, 12), (200, 3), (True, 12), (200, 4.5)):
            with self.subTest(groups=groups, members=members):
                with self.assertRaises(ValueError):
                    long_scenarios(groups=groups, members=members)


if __name__ == "__main__":
    unittest.main()

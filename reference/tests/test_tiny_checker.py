"""Regressions proving that the exhaustive runner's checker detects bad paging."""

from dataclasses import replace
import unittest
from unittest.mock import patch

from reference.model import Item, Pager as RealPager, Plan, Reservation, Stage
from reference.tiny_checker import EXECUTION_PROFILES, validate_case


def fixture():
    a, b, c = Item("A", 3), Item("B", 2), Item("C", 1)
    return Plan((Stage("early", (a, b, c), 1), Stage("owner", (b,), 1),
                 Stage("rest", (a, b, c))), (Reservation("owner", ("B",)),))


class TinyCheckerTests(unittest.TestCase):
    def test_profiles_preserve_semantics_and_leave_original_plan_unchanged(self):
        plan = fixture()
        original = plan.fingerprint
        results = [validate_case(plan, profile) for profile in EXECUTION_PROFILES]
        self.assertTrue(all(result["selected_rows"] == 3 for result in results))
        self.assertTrue(all(result["audited_boundaries"] == result["requests"] for result in results))
        self.assertGreater(results[0]["requests"], results[1]["requests"])
        self.assertEqual(plan.fingerprint, original)
        self.assertEqual([stage.buffer_size for stage in plan.stages], [None, None, None])

    def test_new_pager_per_request_and_deterministic_replay(self):
        instances = []

        def create(plan):
            pager = RealPager(plan)
            instances.append(pager)
            return pager

        with patch("reference.tiny_checker.Pager", side_effect=create):
            result = validate_case(fixture(), EXECUTION_PROFILES[0], replay=True)
        self.assertEqual(len(instances), result["requests"] * 2)
        self.assertEqual(len({id(pager) for pager in instances}), len(instances))

    def test_duplicate_output_is_rejected(self):
        class DuplicatingPager(RealPager):
            def page(self, state=None, **kwargs):
                page = super().page(state, **kwargs)
                if len(page.rows) >= 2:
                    return replace(page, rows=(page.rows[0], page.rows[0], *page.rows[2:]))
                return page

        with patch("reference.tiny_checker.Pager", DuplicatingPager):
            with self.assertRaisesRegex(AssertionError, "prepared_wide, request 1: duplicate ID"):
                validate_case(fixture(), EXECUTION_PROFILES[1])

    def test_missing_output_is_rejected(self):
        class DroppingPager(RealPager):
            def page(self, state=None, **kwargs):
                page = super().page(state, **kwargs)
                return replace(page, rows=page.rows[1:]) if page.rows else page

        with patch("reference.tiny_checker.Pager", DroppingPager):
            with self.assertRaisesRegex(AssertionError, "oracle prefix|selected counts"):
                validate_case(fixture(), EXECUTION_PROFILES[1])

    def test_premature_coverage_retirement_is_rejected_at_same_boundary(self):
        class ForgettingPager(RealPager):
            def page(self, state=None, **kwargs):
                page = super().page(state, **kwargs)
                if page.state is not None:
                    page.state["coverage"] = []
                return page

        with patch("reference.tiny_checker.Pager", ForgettingPager):
            with self.assertRaisesRegex(AssertionError, "raw_step, request 1: coverage: missing"):
                validate_case(fixture(), EXECUTION_PROFILES[0])

    def test_incoming_state_mutation_is_rejected(self):
        class MutatingPager(RealPager):
            def page(self, state=None, **kwargs):
                page = super().page(state, **kwargs)
                if state is not None:
                    state["selected"][0] += 1
                return page

        with patch("reference.tiny_checker.Pager", MutatingPager):
            with self.assertRaisesRegex(AssertionError, "raw_step, request 2: page mutated"):
                validate_case(fixture(), EXECUTION_PROFILES[0])

    def test_early_terminal_state_is_rejected(self):
        class EarlyTerminationPager(RealPager):
            def page(self, state=None, **kwargs):
                return replace(super().page(state, **kwargs), state=None)

        with patch("reference.tiny_checker.Pager", EarlyTerminationPager):
            with self.assertRaisesRegex(AssertionError, "terminal history"):
                validate_case(fixture(), EXECUTION_PROFILES[0])

    def test_nondeterministic_replay_is_rejected(self):
        class DifferentReplayPager(RealPager):
            calls = 0

            def page(self, state=None, **kwargs):
                page = super().page(state, **kwargs)
                DifferentReplayPager.calls += 1
                return replace(page, reads=page.reads + 1) if DifferentReplayPager.calls % 2 == 0 else page

        with patch("reference.tiny_checker.Pager", DifferentReplayPager):
            with self.assertRaisesRegex(AssertionError, "replaying the same request changed"):
                validate_case(fixture(), EXECUTION_PROFILES[0], replay=True)


if __name__ == "__main__":
    unittest.main()

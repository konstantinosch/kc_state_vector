"""Independent finite-domain counts and semantic equivalences for tiny plans.

The domain checks do not construct or execute a Plan for every configuration.
They establish bounds, uniqueness and the independently counted cardinality;
the separate exhaustive runner checks execution for every generated case.
"""

from dataclasses import replace
import json
from math import comb
import unittest

from reference.model import Item, Plan, Reservation, Stage
from reference.oracle import unpaged_oracle
from reference.tiny_checker import EXECUTION_PROFILES, validate_case
from reference.tiny_plans import Claim, EXPECTED_COUNTS, FAMILIES, TinyCase, iter_cases


class TinyDomainTests(unittest.TestCase):
    def assert_domain(self, family, *, id_count, quotas, claim_counts, expected):
        """A unique subset with the full finite cardinality is the whole domain."""
        seen = set()
        for case in iter_cases(family):
            self.assertEqual(case.family, family)
            self.assertEqual(case.id_count, id_count)
            self.assertEqual(len(case.membership_masks), 3)
            self.assertEqual(len(case.quotas), 3)
            for mask in case.membership_masks:
                self.assertIs(type(mask), int)
                self.assertGreaterEqual(mask, 0)
                self.assertLess(mask, 2 ** id_count)
            for quota in case.quotas:
                self.assertTrue(quota is None or type(quota) is int)
                self.assertIn(quota, quotas)
            self.assertIn(len(case.claims), claim_counts)
            owners = [claim.owner for claim in case.claims]
            self.assertEqual(owners, sorted(set(owners)), "claim owners must be unique and canonical")
            for claim in case.claims:
                self.assertIs(type(claim.owner), int)
                self.assertIn(claim.owner, (0, 1, 2))
                self.assertIs(type(claim.id_mask), int)
                self.assertGreater(claim.id_mask, 0)
                self.assertLess(claim.id_mask, 2 ** id_count)
                self.assertEqual(claim.id_mask & case.membership_masks[claim.owner], claim.id_mask)
                self.assertIs(type(claim.yield_mask), int)
                self.assertGreaterEqual(claim.yield_mask, 0)
                self.assertLess(claim.yield_mask, 8)
                self.assertEqual(claim.yield_mask & (1 << claim.owner), 0)
                self.assertIn(claim.policy, ("omit", "release"))
            self.assertNotIn(case, seen, f"duplicate configuration in {family}")
            seen.add(case)
        self.assertEqual(len(seen), expected)
        self.assertEqual(EXPECTED_COUNTS[family], expected)

    def test_core_domain_has_every_bounded_configuration_once(self):
        # Eight source subsets and four quota choices, independently per stage.
        expected = 8 ** 3 * 4 ** 3
        self.assertEqual(expected, 32768)
        self.assert_domain("core", id_count=3, quotas=(0, 1, 2, None),
                           claim_counts=(0,), expected=expected)

    def test_single_owner_domain_has_every_bounded_configuration_once(self):
        # Owner membership/claim pairs: two singleton memberships contribute one
        # each; the two-ID membership contributes three nonempty claim subsets.
        # Other source memberships are independent; ownership has three positions.
        owner_membership_claim_pairs = 2 * 1 + 1 * 3
        expected = 3 * 4 ** 2 * owner_membership_claim_pairs * 4 * 2 * 3 ** 3
        self.assertEqual(expected, 51840)
        self.assert_domain("single_owner", id_count=2, quotas=(0, 1, None),
                           claim_counts=(1,), expected=expected)

    def test_conflicting_owner_domain_has_every_bounded_configuration_once(self):
        # Claimed memberships are forced present. Each nonowner may include or
        # exclude the single ID; each owner has four yield sets and two policies.
        owner_configurations = sum(comb(3, count) * 2 ** (3 - count) * (4 * 2) ** count
                                   for count in (2, 3))
        expected = owner_configurations * 3 ** 3
        self.assertEqual(expected, 24192)
        self.assert_domain("conflicting_owners", id_count=1, quotas=(0, 1, None),
                           claim_counts=(2, 3), expected=expected)

    def test_json_fixture_round_trip_preserves_plan_meaning(self):
        fixtures = (
            (TinyCase("core", 3, (7, 3, 5), (1, 2, None)),
             [("s0", "C"), ("s1", "B"), ("s1", "A")]),
            (TinyCase("single_owner", 2, (3, 2, 3), (0, 1, None),
                      (Claim(1, 2, 5, "release"),)),
             [("s1", "B"), ("s2", "A")]),
            (TinyCase("conflicting_owners", 1, (1, 1, 1), (1, 0, None),
                      (Claim(0, 1, 6, "omit"), Claim(2, 1, 3, "release"))),
             [("s0", "A")]),
        )
        for case, expected in fixtures:
            with self.subTest(family=case.family):
                restored = TinyCase.from_dict(json.loads(json.dumps(case.to_dict())))
                self.assertEqual(restored, case)
                self.assertEqual(unpaged_oracle(restored.to_plan()), expected)
                for profile in EXECUTION_PROFILES:
                    metrics = validate_case(restored.to_plan(), profile, replay=True)
                    self.assertEqual(metrics["selected_rows"], len(expected))

    def test_unknown_family_is_rejected(self):
        self.assertEqual(set(FAMILIES), {"core", "single_owner", "conflicting_owners"})
        with self.assertRaisesRegex(ValueError, "Unknown tiny-plan family"):
            list(iter_cases("unbounded"))


class TinySemanticEquivalenceTests(unittest.TestCase):
    def assert_variants(self, variants, expected):
        for index, plan in enumerate(variants):
            self.assertEqual(unpaged_oracle(plan), expected, f"variant {index}")
            for profile in EXECUTION_PROFILES:
                with self.subTest(variant=index, profile=profile.name):
                    metrics = validate_case(plan, profile, replay=True)
                    self.assertEqual(metrics["selected_rows"], len(expected))
                    self.assertEqual(metrics["audited_boundaries"], metrics["requests"])

    def test_default_yielding_equals_explicit_all_other_stages(self):
        universe = tuple(Item(identity, 0) for identity in "ABC")
        stages = tuple(Stage(f"s{index}", universe, 1 if index < 2 else None)
                       for index in range(3))
        for owner in range(3):
            for policy in ("omit", "release"):
                with self.subTest(owner=owner, policy=policy):
                    implicit = Reservation(f"s{owner}", ("A", "B"), on_unserved=policy)
                    explicit = replace(implicit, yield_from=tuple(
                        f"s{index}" for index in range(3) if index != owner))
                    if owner == 0:
                        expected = [("s0", "C")]
                        if policy == "release":
                            expected += [("s1", "B"), ("s2", "A")]
                    elif owner == 1:
                        expected = [("s0", "C"), ("s1", "B")]
                        if policy == "release":
                            expected += [("s2", "A")]
                    else:
                        expected = [("s0", "C"), ("s2", "B"), ("s2", "A")]
                    self.assert_variants((Plan(stages, (implicit,)), Plan(stages, (explicit,))), expected)

    @staticmethod
    def overlapping_plan(policy):
        values = {identity: Item(identity, 0) for identity in "ABCD"}
        stages = (Stage("s0", (values["B"], values["C"]), quota=0),
                  Stage("s1", (values["B"],), quota=1),
                  Stage("s2", tuple(values.values())))
        claims = (Reservation("s0", ("B", "C"), on_unserved=policy),
                  Reservation("s2", ("A", "B"), on_unserved="omit"))
        return Plan(stages, claims)

    def test_reversing_partial_overlap_declarations_preserves_earliest_owner(self):
        for policy, expected in (
            ("release", [("s1", "B"), ("s2", "D"), ("s2", "C"), ("s2", "A")]),
            ("omit", [("s2", "D"), ("s2", "A")]),
        ):
            with self.subTest(policy=policy):
                plan = self.overlapping_plan(policy)
                reverse = replace(plan, reservations=tuple(reversed(plan.reservations)))
                self.assert_variants((plan, reverse), expected)

    def test_distinct_and_tied_keys_preserve_same_total_order(self):
        tied = self.overlapping_plan("release")
        # Negative and positive distinct keys keep D > C > B > A, the same
        # ordering obtained from the ID tie-breaker with every key equal to zero.
        distinct = replace(tied, stages=tuple(replace(stage, items=tuple(
            Item(item.id, ord(item.id) - ord("C")) for item in stage.items))
            for stage in tied.stages))
        expected = [("s1", "B"), ("s2", "D"), ("s2", "C"), ("s2", "A")]
        self.assert_variants((tied, distinct), expected)


if __name__ == "__main__":
    unittest.main()

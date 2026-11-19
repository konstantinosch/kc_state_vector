"""Finite declaration domains for exhaustive local correctness checks.

All three stages use a fixed total order: C > B > A (where present), with tied
integer keys. The domains exhaust memberships, quotas and explicit reservation
choices below, not all data sizes, numeric keys or request schedules.
"""

from dataclasses import dataclass
from itertools import product

from .model import Item, Plan, Reservation, Stage


STAGE_NAMES = ("s0", "s1", "s2")
FAMILIES = ("core", "single_owner", "conflicting_owners")
EXPECTED_COUNTS = {"core": 32768, "single_owner": 51840, "conflicting_owners": 24192}
BOUNDS = {
    "core": "3 IDs; 3 stages; all 8 source subsets per stage; quotas 0, 1, 2, unbounded; no reservations.",
    "single_owner": "2 IDs; 3 stages; all source subsets; exactly one nonempty reservation declaration, owner at any stage; every subset of owner IDs and yielding stages; omit/release; quotas 0, 1, unbounded.",
    "conflicting_owners": "1 ID; 3 stages; all source memberships; at least two claiming owners; all yielding subsets and omit/release choices per owner; quotas 0, 1, unbounded.",
}


@dataclass(frozen=True)
class Claim:
    owner: int
    id_mask: int
    yield_mask: int
    policy: str


@dataclass(frozen=True)
class TinyCase:
    family: str
    id_count: int
    membership_masks: tuple[int, ...]
    quotas: tuple[int | None, ...]
    claims: tuple[Claim, ...] = ()

    def to_plan(self):
        universe = tuple(Item(chr(ord("A") + index), 0) for index in range(self.id_count))

        def members(mask):
            return tuple(item for index, item in enumerate(universe) if mask & (1 << index))

        stages = tuple(Stage(name, members(mask), quota)
                       for name, mask, quota in zip(STAGE_NAMES, self.membership_masks, self.quotas))
        reservations = tuple(
            Reservation(STAGE_NAMES[claim.owner], tuple(item.id for item in members(claim.id_mask)),
                        yield_from=tuple(name for index, name in enumerate(STAGE_NAMES)
                                         if claim.yield_mask & (1 << index)),
                        on_unserved=claim.policy)
            for claim in self.claims
        )
        return Plan(stages, reservations, scope=f"tiny-{self.family}")

    def to_dict(self):
        return {"family": self.family, "id_count": self.id_count,
                "membership_masks": list(self.membership_masks), "quotas": list(self.quotas),
                "claims": [{"owner": claim.owner, "id_mask": claim.id_mask,
                            "yield_mask": claim.yield_mask, "policy": claim.policy}
                           for claim in self.claims]}

    @classmethod
    def from_dict(cls, value):
        return cls(value["family"], value["id_count"], tuple(value["membership_masks"]),
                   tuple(value["quotas"]), tuple(Claim(**claim) for claim in value["claims"]))


def yielding_masks(owner):
    return tuple(mask for mask in range(8) if not mask & (1 << owner))


def nonempty_subsets(mask):
    return tuple(candidate for candidate in range(1, mask + 1) if candidate & mask == candidate)


def iter_cases(family):
    """Generate every declaration configuration in exactly the named domain."""
    if family == "core":
        for memberships in product(range(8), repeat=3):
            for quotas in product((0, 1, 2, None), repeat=3):
                yield TinyCase(family, 3, memberships, quotas)
    elif family == "single_owner":
        for memberships in product(range(4), repeat=3):
            for owner in range(3):
                for reserved in nonempty_subsets(memberships[owner]):
                    for yielding in yielding_masks(owner):
                        for policy in ("omit", "release"):
                            for quotas in product((0, 1, None), repeat=3):
                                yield TinyCase(family, 2, memberships, quotas,
                                               (Claim(owner, reserved, yielding, policy),))
    elif family == "conflicting_owners":
        for memberships in product(range(2), repeat=3):
            choices = []
            for owner, membership in enumerate(memberships):
                choices.append((None,) + tuple(
                    Claim(owner, 1, yielding, policy)
                    for yielding in yielding_masks(owner) for policy in ("omit", "release")
                ) if membership else (None,))
            for claims in product(*choices):
                claims = tuple(claim for claim in claims if claim is not None)
                if len(claims) < 2:
                    continue
                for quotas in product((0, 1, None), repeat=3):
                    yield TinyCase(family, 1, memberships, quotas, claims)
    else:
        raise ValueError(f"Unknown tiny-plan family: {family}")

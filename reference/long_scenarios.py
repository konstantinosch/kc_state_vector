"""Deterministic ID-only workloads for long, local reference traversals.

These are correctness fixtures, not a distribution fitted to production data.
``size`` counts distinct identities in each plan's input union. Every identity has
one stable position, with five IDs sharing each integer sort key. Source overlap
is described separately from reservation density; a broad sweeper intentionally
contains earlier sources in most cases.
"""

from dataclasses import dataclass
import random

from .model import Item, Plan, Reservation, Stage


@dataclass(frozen=True)
class Scenario:
    name: str
    description: str
    plan: Plan


def _spread(size, count):
    """Distinct ranks including both ends, with gaps differing by at most one."""
    if count == 1:
        return (size // 2,)
    return tuple(index * (size - 1) // (count - 1) for index in range(count))


def _bursts(size, count):
    blocks = min(10, count)
    base, extra = divmod(count, blocks)
    result = []
    for index in range(blocks):
        width = base + (index < extra)
        start = index * (size - width) // (blocks - 1) if blocks > 1 else 0
        result.extend(range(start, start + width))
    return tuple(result)


def make_scenarios(size: int = 3000, seed: int = 20261119) -> tuple[Scenario, ...]:
    """Return thirteen valid plans; default quotas straddle 150 selected IDs.

    The full traversal harness always requests pages of 50. Smaller fixtures down
    to 100 identities are accepted for checking the harness itself; only the
    default/larger workloads claim many successive full pages.
    """
    if type(size) is not int or size < 100:
        raise ValueError("Long-traversal fixtures require at least 100 identities.")
    if type(seed) is not int:
        raise ValueError("The deterministic seed must be an integer.")
    rng = random.Random(seed)
    width = max(6, len(str(size)))
    universe = tuple(Item(f"id{size - index:0{width}d}", (size - 1 - index) // 5)
                     for index in range(size))
    selected_limit = max(1, size // 12)
    q149, q150, q151 = (min(target, selected_limit + offset)
                       for offset, target in enumerate((149, 150, 151)))
    # Leave room for the overlapping-owner scenario's extra quarter of claims.
    reserve_count = min(360, size // 10)
    scenarios = []

    def pick(ranks):
        return tuple(universe[index] for index in sorted(set(ranks)))

    def add(name, description, stages, reservations=()):
        scenarios.append(Scenario(name, description,
                                  Plan(tuple(stages), tuple(reservations), scope=f"long-{name}-{size}-{seed}")))

    # Disjoint source membership gives a genuine zero-overlap control. Exclusive
    # rows beyond a finite earlier quota are not part of the defined result.
    thirds = tuple(pick(range(offset, size, 3)) for offset in range(3))
    add("core_disjoint",
        f"Three disjoint interleaved sources ({', '.join(str(len(part)) for part in thirds)} IDs); "
        f"earlier quotas {q149}/{q150}, unbounded final source; zero reservations.",
        (Stage("early", thirds[0], q149), Stage("owner", thirds[1], q150, buffer_size=73),
         Stage("sweeper", thirds[2])))

    # The 10% figure applies to the first two equal-size sources, not the broad
    # third source (which contains every identity).
    source_count = size // 5
    left = set(rng.sample(range(size), source_count))
    common = set(rng.sample(sorted(left), source_count // 10))
    right = common | set(rng.sample(sorted(set(range(size)) - left), source_count - len(common)))
    add("core_sparse",
        f"First sources each contain {source_count} IDs and share {len(common)} "
        f"({len(common) / source_count:.1%} of either source); sweeper contains all {size}. "
        f"Earlier quotas {q149}/{q150}; zero reservations; seeded scattered membership.",
        (Stage("early", pick(left), q149), Stage("owner", pick(right), q150, buffer_size=73),
         Stage("sweeper", universe)))

    add("core_dense",
        f"All three sources contain the same {size} IDs (100% membership overlap); "
        f"earlier quotas {q149}/{q151}; zero reservations.",
        (Stage("early", universe, q149, buffer_size=73), Stage("owner", universe, q151),
         Stage("sweeper", universe, buffer_size=73)))

    growth_items = universe[1::2]
    add("core_growth_control",
        f"Unbounded early source selects every second ID ({len(growth_items)} of {size}); "
        "empty zero-quota middle stage; final sweeper contains all IDs. All sources raw, "
        "zero reservations. Explicit-ID coverage intentionally grows to half the input "
        "before the sweeper releases it; this control has no finite selection quota.",
        (Stage("early", growth_items), Stage("owner", (), 0), Stage("sweeper", universe)))

    def reserved_case(name, placement, ranks, *, policy="omit", owner_quota=None,
                      early_prepared=False, sweeper_prepared=False):
        reserved = pick(ranks)
        quota_text = "unbounded" if owner_quota is None else str(owner_quota)
        add(name,
            f"{len(reserved)} reserved IDs ({len(reserved) / size:.1%} of the universe), {placement}; "
            f"early and sweeper contain all {size}, owner contains exactly the reservation set. "
            f"Early quota {q151}; owner quota {quota_text}; unserved policy {policy}.",
            (Stage("early", universe, q151, buffer_size=73 if early_prepared else None),
             Stage("owner", reserved, owner_quota, buffer_size=73),
             Stage("sweeper", universe, buffer_size=73 if sweeper_prepared else None)),
            (Reservation("owner", tuple(item.id for item in reserved), on_unserved=policy),))

    reserved_case("reserved_spread", "evenly spaced across the full order including both ends",
                  _spread(size, reserve_count), sweeper_prepared=True)
    reserved_case("reserved_head", "one contiguous newest/head cluster", range(reserve_count),
                  early_prepared=True)
    reserved_case("reserved_tail", "one contiguous oldest/tail cluster",
                  range(size - reserve_count, size), sweeper_prepared=True)
    burst_ranks = _bursts(size, reserve_count)
    reserved_case("reserved_bursts", "ten short clusters spaced from head to tail", burst_ranks,
                  early_prepared=True, sweeper_prepared=True)

    # Many early output pages pass already-encountered reservations without an
    # enormous selected-coverage list: ordinary early IDs have no later consumer.
    cut = 2 * size // 3
    head_count = reserve_count // 2
    ends = pick((*range(head_count), *range(cut - (reserve_count - head_count), cut)))
    add("reserved_long_lived",
        f"Early source scans its entire {cut}-ID range; {len(ends)} reservations occupy its two ends. "
        f"Owner contains just those reservations; sweeper has the disjoint remaining {size - cut} IDs. "
        "All quotas unbounded; passed head reservations must survive many early output pages.",
        (Stage("early", universe[:cut], buffer_size=73), Stage("owner", ends, buffer_size=73),
         Stage("sweeper", universe[cut:])),
        (Reservation("owner", tuple(item.id for item in ends)),))

    reserved_case("partial_owner_release", "ten spaced clusters", burst_ranks,
                  policy="release", owner_quota=q149, sweeper_prepared=True)
    reserved_case("partial_owner_omit", "ten spaced clusters", burst_ranks,
                  policy="omit", owner_quota=q149, sweeper_prepared=True)

    # The later claim is deliberately declared first. Shared claims belong to
    # the earlier owner; its release policy must not revive the later omit claim.
    winner_ranks = set(_spread(size, reserve_count))
    unique_count = max(1, reserve_count // 4)
    unique_ranks = set(rng.sample(sorted(set(range(size)) - winner_ranks), unique_count))
    winner = pick(winner_ranks)
    loser = pick(winner_ranks | unique_ranks)
    add("overlapping_owners",
        f"Early/sweeper contain all {size}. Owner has {len(winner)} evenly spaced claims; "
        f"later zero-quota owner repeats those claims and adds {unique_count} seeded unique claims. "
        f"Early quota {q149}; winning-owner quota {q149}, release policy, yields required only from "
        "later owner and sweeper. Later owner uses omit. Early selection may preempt winning claims.",
        (Stage("early", universe, q149), Stage("owner", winner, q149, buffer_size=73),
         Stage("later_owner", loser, 0), Stage("sweeper", universe, buffer_size=73)),
        (Reservation("later_owner", tuple(item.id for item in loser), on_unserved="omit"),
         Reservation("owner", tuple(item.id for item in winner),
                     yield_from=("later_owner", "sweeper"), on_unserved="release")))

    # Selected head claims have no later emitting consumer. Their only remaining
    # need is the zero-quota owner's covered-versus-unserved completion decision.
    covered_count = min(q151, max(1, reserve_count // 4))
    remaining_count = reserve_count - covered_count
    other_ranks = tuple(q151 + rank for rank in _spread(size - q151, remaining_count))
    zero_claims = pick((*range(covered_count), *other_ranks))
    add("zero_quota_preempted",
        f"Early selects its exclusive {q151}-ID head source; {covered_count} are reserved but early "
        f"does not yield. A zero-quota owner also reserves {remaining_count} spread IDs in the disjoint "
        "sweeper source. Omit applies only to unserved claims; covered head claims remain necessary "
        "until owner completion despite having no later emitting consumer.",
        (Stage("early", universe[:q151], q151, buffer_size=73), Stage("owner", zero_claims, 0),
         Stage("sweeper", universe[q151:], buffer_size=73)),
        (Reservation("owner", tuple(item.id for item in zero_claims),
                     yield_from=("sweeper",), on_unserved="omit"),))

    return tuple(scenarios)

"""Exact byte-based choices for fixed, maximal-run hybrid coverage."""

from dataclasses import dataclass
from itertools import combinations, groupby
import json
import unittest

from reference.grouped.coverage import (
    CoverageError, contains, encode_coverage, expand_coverage,
)


@dataclass(frozen=True)
class Record:
    id: str
    sort_key: int
    group: str
    episode: str = "all"

    @property
    def position(self):
        return self.sort_key, self.id


def wire_bytes(value):
    return len(json.dumps(value, ensure_ascii=True, allow_nan=False,
                          separators=(",", ":")).encode("utf-8"))


class HybridCoverageTests(unittest.TestCase):
    def assert_exact_choice(self, records, selected):
        selected = set(selected)
        encoded = encode_coverage(records, selected, "hybrid")
        expected, evaluated = [], set()
        # Derive the independent run specification using groupby over complete
        # scoped membership; an unselected member always breaks a run.
        for group in sorted({record.group for record in records}):
            ordered = sorted((record for record in records if record.group == group),
                             key=lambda record: record.position)
            for included, run in groupby(ordered, key=lambda record: record.id in selected):
                run = tuple(run)
                if not included:
                    continue
                plain = [record.id for record in run]
                span = ["span", group, list(run[0].position), list(run[-1].position)]
                if len(run) > 1 and wire_bytes([span]) < wire_bytes(plain):
                    expected.append(span)
                else:
                    expected.extend(plain)
        self.assertEqual(encoded, expected)
        for entry in encoded:
            if isinstance(entry, str):
                evaluated.add(entry)
            else:
                self.assertEqual(entry[0], "span")
                evaluated.update(record.id for record in records if record.group == entry[1]
                                 and tuple(entry[2]) <= record.position <= tuple(entry[3]))
        self.assertEqual(evaluated, selected)
        self.assertEqual(expand_coverage(records, encoded, "hybrid"), selected)
        self.assertLessEqual(wire_bytes(encoded), wire_bytes(sorted(selected)))
        return encoded

    def test_all_256_subsets_match_predicates_and_independent_byte_choices(self):
        records = tuple(Record(f"long_identifier_{i}_" + "x" * 40, i // 2, "G",
                               "odd" if i % 2 else "even") for i in range(5)) + tuple(
            Record(f"unicode_λ_{i}_" + "字" * 10, i, "H") for i in range(3))
        checked = 0
        for count in range(len(records) + 1):
            for chosen in combinations((record.id for record in records), count):
                encoded = self.assert_exact_choice(records, chosen)
                self.assertEqual(expand_coverage(records, json.loads(json.dumps(encoded)), "hybrid"),
                                 set(chosen))
                checked += 1
        self.assertEqual(checked, 256)

    def test_mixed_bare_ids_and_span_are_unambiguous(self):
        records = (Record("span", 20, "single"), Record("id", 10, "other")) + tuple(
            Record(f"dense_member_identifier_{i:04d}", i, "G") for i in range(8))
        encoded = self.assert_exact_choice(records, (record.id for record in records))
        self.assertTrue(any(isinstance(entry, str) for entry in encoded))
        self.assertTrue(any(isinstance(entry, list) for entry in encoded))
        self.assertIn("span", encoded)
        self.assertIn("id", encoded)
        self.assertTrue(contains(records, encoded, "span", "hybrid"))

    def test_tie_keeps_ids_and_one_byte_saving_selects_span(self):
        records = tuple(Record(f"{i:02d}", i, "GGGGG") for i in range(7))
        plain = [record.id for record in records]
        self.assertEqual(wire_bytes(plain), 36)
        self.assertEqual(wire_bytes([["span", "GGGGG", [0, "00"], [6, "06"]]]), 36)
        self.assertEqual(self.assert_exact_choice(records, plain), plain)
        cheaper = tuple(Record(record.id, record.sort_key, "GGGG") for record in records)
        self.assertEqual(self.assert_exact_choice(cheaper, plain),
                         [["span", "GGGG", [0, "00"], [6, "06"]]])

    def test_expensive_group_name_prevents_span_compression(self):
        group = "expensive" + "λ\\\"" * 100
        records = tuple(Record(f"{i:02d}", i, group) for i in range(12))
        plain = [record.id for record in records]
        self.assertEqual(self.assert_exact_choice(records, plain), plain)

    def test_empty_singleton_and_canonical_input_order(self):
        record = Record("only", 2, "G")
        self.assertEqual(encode_coverage((), (), "hybrid"), [])
        self.assertEqual(expand_coverage((), [], "hybrid"), set())
        self.assertEqual(self.assert_exact_choice((record,), ("only",)), ["only"])
        records = tuple(Record(f"member_{i}", i, "G") for i in range(10))
        ids = [record.id for record in records]
        self.assertEqual(encode_coverage(records, ids + ids, "hybrid"),
                         encode_coverage(tuple(reversed(records)), reversed(ids), "hybrid"))

    def test_unicode_escaped_ids_and_group_names_use_ascii_json_cost(self):
        for group in ("G", "ομάδα", "quote\"slash\\line\n", "😀"):
            with self.subTest(group=group):
                records = tuple(Record(f"{i}-λ😀\"\\\n", 10**12 + i, group) for i in range(10))
                encoded = self.assert_exact_choice(records, (record.id for record in records))
                self.assertTrue(any(isinstance(entry, list) for entry in encoded))
                unicode_wire = json.dumps(encoded, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                self.assertGreater(wire_bytes(encoded), len(unicode_wire))

    def test_reservation_episode_and_candidate_holes_split_runs(self):
        records = tuple(Record(f"member_identifier_{i}_" + "x" * 20, i, "G",
                               "other" if i == 4 else "selected") for i in range(9))
        interleaved = records + (Record("different_group", 3, "H"),)
        selected = {record.id for record in records if record.episode == "selected"}
        encoded = self.assert_exact_choice(interleaved, selected)
        self.assertEqual(len(encoded), 2)
        self.assertTrue(all(isinstance(entry, list) for entry in encoded))
        self.assertFalse(contains(interleaved, encoded, records[4].id, "hybrid"))
        self.assertFalse(contains(interleaved, encoded, "different_group", "hybrid"))

    def test_full_tuple_ties_and_adversarial_long_middle_id(self):
        records = tuple(Record(identity, 0, "G") for identity in ("a", "b" * 200, "c"))
        # Cheap endpoints can make a span worthwhile even for just three members.
        encoded = self.assert_exact_choice(records, (record.id for record in records))
        self.assertEqual(encoded, [["span", "G", [0, "a"], [0, "c"]]])
        self.assertEqual(self.assert_exact_choice(records, ("a", "c")), ["a", "c"])

    def test_policy_compares_whole_maximal_run_not_all_possible_partitions(self):
        records = (Record("low", -(10**60), "G"),) + tuple(
            Record(f"mid{i}", i, "G") for i in range(10)) + (Record("high", 10**60, "G"),)
        plain = [record.id for record in records]
        self.assertEqual(self.assert_exact_choice(records, plain), plain)
        interior_partition = ["low", ["span", "G", [0, "mid0"], [9, "mid9"]], "high"]
        self.assertLess(wire_bytes(interior_partition), wire_bytes(plain))
        self.assertEqual(expand_coverage(records, interior_partition, "hybrid"), set(plain))

    def test_decoder_rejects_tagged_ids_duplicates_overlap_and_bad_bounds(self):
        records = (Record("A", 1, "G"), Record("B", 2, "G"), Record("X", 3, "H"))
        invalid = (None, [True], [["id", "A"]], [["unknown", "A"]], ["missing"],
                   ["A", "A"], ["A", ["span", "G", [1, "A"], [2, "B"]]],
                   [["span", "G", [1, "A"], [1, "A"]]],
                   [["span", "G", [0, "A"], [2, "B"]]],
                   [["span", "G", [1, "A"], [3, "X"]]],
                   [["span", "G", [2, "B"], [1, "A"]]])
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(CoverageError):
                expand_coverage(records, value, "hybrid")
        # Decoder validates exact membership/shape, not an admission-cost decision.
        legal_but_costly = [["span", "G", [1, "A"], [2, "B"]], "X"]
        self.assertEqual(expand_coverage(records, legal_but_costly, "hybrid"), {"A", "B", "X"})


if __name__ == "__main__":
    unittest.main()

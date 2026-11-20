"""Exact group-span sets, especially holes left by partial reservations."""

from dataclasses import dataclass
from itertools import combinations
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


class CoverageCodecTests(unittest.TestCase):
    def test_reserved_middle_member_splits_coverage(self):
        records = (Record("A", 30, "G"), Record("B", 20, "G"), Record("C", 10, "G"))
        encoded = encode_coverage(records, {"A", "C"}, "spans")
        self.assertEqual(encoded, [["id", "C"], ["id", "A"]])
        self.assertEqual(expand_coverage(records, encoded, "spans"), {"A", "C"})
        self.assertFalse(contains(records, encoded, "B", "spans"))
        # A naive min/max range is well-formed but semantically overclaims B.
        naive = [["span", "G", [10, "C"], [30, "A"]]]
        self.assertEqual(expand_coverage(records, naive, "spans"), {"A", "B", "C"})

    def test_other_groups_can_interleave_inside_a_span(self):
        records = (Record("A", 30, "G"), Record("X", 20, "H"), Record("B", 10, "G"))
        encoded = encode_coverage(records, {"A", "B"}, "spans")
        self.assertEqual(encoded, [["span", "G", [10, "B"], [30, "A"]]])
        self.assertEqual(expand_coverage(records, encoded, "spans"), {"A", "B"})

    def test_episode_or_candidate_filter_cannot_hide_a_same_group_hole(self):
        records = (Record("A", 40, "G", "outer"), Record("B", 30, "G", "outer"),
                   Record("X", 20, "G", "other"), Record("C", 10, "G", "outer"),
                   Record("D", 0, "G", "outer"))
        # X need not occur in this stage's candidates; it remains in shared scope.
        encoded = encode_coverage(records, {"A", "B", "C", "D"}, "spans")
        self.assertEqual(encoded, [["span", "G", [0, "D"], [10, "C"]],
                                   ["span", "G", [30, "B"], [40, "A"]]])
        self.assertNotIn("X", expand_coverage(records, encoded, "spans"))

    def test_tied_positions_use_the_id_tie_breaker(self):
        records = tuple(Record(identity, 7, "G") for identity in "ABCD")
        encoded = encode_coverage(records, {"A", "B", "D"}, "spans")
        self.assertEqual(encoded, [["span", "G", [7, "A"], [7, "B"]], ["id", "D"]])
        self.assertEqual(expand_coverage(records, encoded, "spans"), {"A", "B", "D"})

    def test_adjacent_nonoverlapping_spans_decode_and_canonicalize(self):
        records = tuple(Record(identity, index, "G") for index, identity in enumerate("ABCDE"))
        adjacent = [["span", "G", [0, "A"], [1, "B"]],
                    ["span", "G", [2, "C"], [3, "D"]], ["id", "E"]]
        selected = expand_coverage(records, adjacent, "spans")
        self.assertEqual(selected, set("ABCDE"))
        self.assertEqual(encode_coverage(records, selected, "spans"),
                         [["span", "G", [0, "A"], [4, "E"]]])
        overlapping = adjacent + [["span", "G", [1, "B"], [2, "C"]]]
        with self.assertRaises(CoverageError):
            expand_coverage(records, overlapping, "spans")

    def test_fixed_span_scope_is_not_the_remaining_candidate_suffix(self):
        records = (Record("A", 100, "G"), Record("B", 50, "G"), Record("C", 10, "G"))
        encoded = encode_coverage(records, {"A", "B", "C"}, "spans")
        # Passing A's cursor position cannot remove it from the descriptor's
        # meaning: a later C candidate can still represent the entire group.
        suffix_candidates = {record.id for record in records if record.position < (50, "B")}
        self.assertEqual(suffix_candidates, {"C"})
        self.assertEqual(expand_coverage(records, encoded, "spans"), {"A", "B", "C"})
        with self.assertRaises(CoverageError):
            expand_coverage(tuple(record for record in records if record.id in suffix_candidates),
                            encoded, "spans")

    def test_empty_singleton_unicode_and_input_order(self):
        records = (Record("β", -1, "ομάδα"), Record("α", 2, "ομάδα"))
        for mode in ("ids", "spans"):
            self.assertEqual(encode_coverage(records, (), mode), [])
            self.assertEqual(expand_coverage((), [], mode), set())
            selected = encode_coverage(records, ("α", "α"), mode)
            self.assertEqual(expand_coverage(records, selected, mode), {"α"})
            self.assertEqual(encode_coverage(records, ("β", "α"), mode),
                             encode_coverage(tuple(reversed(records)), ("α", "β"), mode))

    def test_all_256_subsets_match_independent_predicate_evaluation(self):
        records = (Record("A", 9, "G", "outer"), Record("B", 7, "G", "inner"),
                   Record("C", 7, "G", "outer"), Record("D", 1, "G", "outer"),
                   Record("E", 8, "H"), Record("F", 7, "H"),
                   Record("G", 0, "H"), Record("H", -2, "J"))
        checked = 0
        for count in range(len(records) + 1):
            for chosen in combinations((record.id for record in records), count):
                expected = set(chosen)
                spans = json.loads(json.dumps(encode_coverage(records, expected, "spans")))
                explicit = encode_coverage(records, expected, "ids")
                evaluated = set()
                for entry in spans:
                    if entry[0] == "id":
                        evaluated.add(entry[1])
                    else:
                        evaluated.update(record.id for record in records
                                         if record.group == entry[1]
                                         and tuple(entry[2]) <= record.position <= tuple(entry[3]))
                self.assertEqual(evaluated, expected)
                self.assertEqual(expand_coverage(records, spans, "spans"), expected)
                self.assertEqual(expand_coverage(records, explicit, "ids"), expected)
                self.assertLessEqual(len(spans), len(explicit))
                checked += 1
        self.assertEqual(checked, 256)

    def test_decoder_rejects_malformed_or_overlapping_descriptors(self):
        records = (Record("A", 1, "G"), Record("B", 2, "G"), Record("X", 3, "H"))
        invalid = (
            None, ["A"], [[]], [["unknown", "A"]], [["id", "missing"]],
            [["id", "A", "extra"]], [["span", "missing", [1, "A"], [2, "B"]]],
            [["span", "G", [2, "B"], [1, "A"]]],
            [["span", "G", [1, "A"], [1, "A"]]],
            [["span", "G", [True, "A"], [2, "B"]]],
            [["span", "G", [0, "A"], [2, "B"]]],
            [["span", "G", [1, "A"], [3, "X"]]],
            [["id", "A"], ["id", "A"]],
            [["span", "G", [1, "A"], [2, "B"]], ["id", "A"]],
        )
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(CoverageError):
                expand_coverage(records, value, "spans")
        for value in (None, ["A", "A"], ["missing"], [["id", "A"]]):
            with self.subTest(value=value), self.assertRaises(CoverageError):
                expand_coverage(records, value, "ids")

    def test_encoder_rejects_unknown_ids_and_invalid_scope(self):
        record = Record("A", 1, "G")
        for values in ("A", None, ["missing"], [True]):
            with self.subTest(values=values), self.assertRaises(CoverageError):
                encode_coverage((record,), values, "spans")
        for records in ((record, record), (Record("A", True, "G"),), (Record("A", 1, ""),)):
            with self.subTest(records=records), self.assertRaises(CoverageError):
                encode_coverage(records, (), "ids")
        with self.assertRaises(CoverageError):
            encode_coverage((record,), (), "unknown")


if __name__ == "__main__":
    unittest.main()

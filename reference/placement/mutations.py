"""Named placement faults checked in isolated public-reference copies.

Run python -S -m reference.placement.mutations. The healthy focused tests
must pass first. Only assertion failures count as detected behavioral faults;
test errors, timeouts, skipped tests, invalid edits and surviving variants fail.

The catalogue covers one-way spill, two-way hysteresis, replay by recomputation,
and recovery isolation between chains. It is a bounded regression set, not a
proof of the protocol or of distributed storage implementations.
"""
from __future__ import annotations

import argparse
from dataclasses import replace
import hashlib
import json
import math
from pathlib import Path
import tempfile

from reference.mutations import Mutation, apply_mutation, _copy_package, _run_tests


REFERENCE_ROOT = Path(__file__).resolve().parents[1]
TARGETS = frozenset(("protocol.py", "two_way.py", "recompute.py"))
PROTOCOL = "reference.placement.tests.test_protocol.PlacementProtocolTests."
FALLBACKS = "reference.placement.tests.test_fallbacks.FallbackTests."
TWO_WAY_TEST = "reference.placement.tests.two_way.test_protocol.TwoWayProtocolTests."
RECOMPUTE_TEST = "reference.placement.tests.recompute.test_protocol.RecomputeProtocolTests."


def _prefix(family, mutation):
    return replace(mutation, name=family + "." + mutation.name)


ONE_WAY = tuple(("protocol.py", _prefix("one_way", mutation)) for mutation in (
    Mutation('return_inline_after_spill',
             'Allow an already spilled chain to shrink back into an inline token.',
             "        persist = not fallback and payload['kind'] == 'handle'",
             '        persist = False',
             (PROTOCOL + 'test_spilled_chain_does_not_return_inline_as_coverage_shrinks',)),
    Mutation('ignore_checkpoint_digest',
             'Accept a changed stored vector when its binding and revision still match.',
             "or digest(checkpoint) != head['digest']",
             'or False',
             (PROTOCOL + 'test_corrupt_hot_vector_never_triggers_an_approximate_fallback',)),
    Mutation('ignore_handle_digest',
             'Accept an authenticated handle whose digest differs from the stored head.',
             "                    if payload['digest'] != head['digest']:",
             '                    if False:',
             (PROTOCOL + 'test_authenticated_but_wrong_handle_digest_is_rejected',)),
    Mutation('restart_appends_instead_of_replacing',
             'Label restarted output as append, mixing two traversals in one client list.',
             "mode, state, status, action = 'exact', None, 'restart', 'replace'",
             "mode, state, status, action = 'exact', None, 'restart', 'append'",
             (FALLBACKS + 'test_restart_explicitly_replaces_old_list_and_completes_new_oracle',)),
    Mutation('bypass_conditional_publication',
             'Let a conflicting concurrent proposal overwrite the accepted transition.',
             '                elif current != proposal.expected_head:',
             '                elif False:',
             (PROTOCOL + 'test_conflicting_concurrent_requests_do_not_advance_twice',)),
    Mutation('skip_handle_capacity',
             'Publish a handle even when its complete encoded envelope exceeds the budget.',
             "                if len(continuation.encode('ascii')) > self.threshold:",
             '                if False:',
             (PROTOCOL + 'test_handle_envelope_must_also_fit_the_budget',)),
    Mutation('skip_reader_binding',
             'Permit another authenticated reader binding to enter recovery for this token.',
             "            if payload['binding'] != self.binding:",
             '            if False:',
             (PROTOCOL + 'test_authentication_and_reader_binding_precede_recovery',)),
))


TWO_WAY = tuple((target, _prefix("two_way", mutation)) for target, mutation in (
    ("two_way.py", Mutation('spill_at_equality', 'Spill even when the inline token exactly fits T.',
        'else candidate_bytes <= self.threshold)', 'else candidate_bytes < self.threshold)',
        (TWO_WAY_TEST + 'test_spill_is_strictly_above_complete_token_threshold',))),
    ("two_way.py", Mutation('return_at_half_equality', 'Return at T/2 instead of strictly below it.',
        '2 * candidate_bytes < self.threshold if was_handle',
        '2 * candidate_bytes <= self.threshold if was_handle',
        (TWO_WAY_TEST + 'test_return_is_strictly_below_half_including_odd_thresholds',))),
    ("two_way.py", Mutation('remove_hysteresis', 'Use the upper threshold in both directions.',
        '2 * candidate_bytes < self.threshold if was_handle',
        'candidate_bytes <= self.threshold if was_handle',
        (TWO_WAY_TEST + 'test_hysteresis_prevents_flapping_and_allows_repeated_real_switches',))),
    ("two_way.py", Mutation('never_return_inline', 'Keep the original one-way placement behavior.',
        '2 * candidate_bytes < self.threshold if was_handle',
        'False if was_handle',
        (TWO_WAY_TEST + 'test_return_removes_vector_copies_but_retains_replay_ledger',))),
    ("two_way.py", Mutation('retain_hot_vector', 'Keep the dedicated hot vector after returning inline.',
        '            self.store.hot.pop(chain, None)', '            pass  # retain old hot vector',
        (TWO_WAY_TEST + 'test_return_removes_vector_copies_but_retains_replay_ledger',))),
    ("two_way.py", Mutation('retain_backup_vector', 'Keep the dedicated backup after returning inline.',
        "            self.store.hot.pop(chain, None)\n            self.store.backup.pop(chain, None)",
        "            self.store.hot.pop(chain, None)\n            pass  # retain backup",
        (TWO_WAY_TEST + 'test_return_removes_vector_copies_but_retains_replay_ledger',))),
    ("two_way.py", Mutation('skip_current_token_hash', 'Accept changed signed state at the same managed revision.',
        "        if incoming_hash != head['current_token_hash']:", '        if False:',
        (TWO_WAY_TEST + 'test_signed_but_changed_inline_state_or_metadata_is_rejected',))),
    ("two_way.py", Mutation('recreate_missing_managed_chain', 'Treat a missing managed-inline head as a fresh chain.',
        "if head is None and (payload['managed'] or chain in self.store.hot or chain in self.store.backup):",
        'if head is None and (chain in self.store.hot or chain in self.store.backup):',
        (TWO_WAY_TEST + 'test_missing_individual_ledger_rejects_returned_inline_token',))),
    ("two_way.py", Mutation('forget_inline_publication', 'Do not publish accepted progress when returning inline.',
        "payload['revision'], response, checkpoint, managed)",
        "payload['revision'], response, checkpoint, managed and continuation is not None and self.inspect(continuation)['kind'] == 'handle')",
        (TWO_WAY_TEST + 'test_return_removes_vector_copies_but_retains_replay_ledger',))),
    ("two_way.py", Mutation('require_vector_for_inline_replay', 'Require a deleted checkpoint to replay the accepted inline return.',
        "                    if head['kind'] == 'handle':", "                    if not head['terminal']:",
        (TWO_WAY_TEST + 'test_lost_return_response_replays_old_handle_without_checkpoint',))),
    ("two_way.py", Mutation('skip_saved_replay_integrity', 'Deliver an inconsistent saved inline response.',
        '                    self._validate_replay(chain, head)', '                    pass  # skip replay validation',
        (TWO_WAY_TEST + 'test_corrupted_saved_inline_replay_is_not_delivered',))),
    ("protocol.py", Mutation('bypass_conditional_publication', 'Let a conflicting managed request overwrite its winner.',
        '                elif current != proposal.expected_head:', '                elif False:',
        (TWO_WAY_TEST + 'test_conflicting_concurrent_return_requests_do_not_overwrite',))),
))


RECOMPUTE = tuple(("recompute.py", _prefix("recompute", mutation)) for mutation in (
    Mutation('skip_response_digest', 'Return recomputed data without checking the accepted response digest.',
        "        if digest(response) != head['response_digest']:", '        if False:',
        (RECOMPUTE_TEST + 'test_corrupt_compact_response_digest_is_not_ignored',
         RECOMPUTE_TEST + 'test_same_rows_but_changed_work_or_continuation_fail_full_digest')),
    Mutation('ignore_original_parameters', 'Allow a different request to enter accepted replay.',
        "                    if head['request_key'] != key:", '                    if False:',
        (RECOMPUTE_TEST + 'test_changed_request_limits_are_rejected_before_recomputation',)),
    Mutation('recompute_wrong_page_size', 'Recompute using a different page size from the accepted request.',
        'candidate = self._proposal(payload, params, key, incoming_hash, original_head)',
        "candidate = self._proposal(payload, {**params, 'page_size': params['page_size'] + 1}, key, incoming_hash, original_head)",
        (RECOMPUTE_TEST + 'test_first_spill_retries_execute_actual_core_and_grouped_selectors',)),
    Mutation('release_bridge_before_successful_advance', 'Drop the handle-return replay bridge when an inline input merely arrives.',
        '        # Actual selection is tentative and never releases a retained bridge.',
        "        if head is not None and payload['kind'] == 'inline' and head['replay_mode'] == 'stored':\n"
        "            self.store.heads[chain]['response'] = None\n"
        '        # Incorrectly release before selection and conditional publication.',
        (RECOMPUTE_TEST + 'test_failed_successor_selection_and_commit_preserve_return_bridge',)),
    Mutation('forget_handle_input_bridge', 'Compact a handle-input response even though the retry has no input vector.',
        "stored = proposal.input_kind == 'handle' or proposal.kind == 'fallback'",
        "stored = proposal.kind == 'fallback'",
        (RECOMPUTE_TEST + 'test_handle_return_bridge_replays_without_another_selection',)),
    Mutation('always_store_full_response', 'Retain full responses instead of performing the proposed compact replay.',
        "stored = proposal.input_kind == 'handle' or proposal.kind == 'fallback'", 'stored = True',
        (RECOMPUTE_TEST + 'test_first_spill_retries_execute_actual_core_and_grouped_selectors',)),
    Mutation('ignore_compact_replay_expiry', 'Recompute an accepted inline-input response after its promised replay window expired.',
        "        if head.get('replay_expired', False) or (", '        if False or (',
        (RECOMPUTE_TEST + 'test_managed_inline_to_terminal_retry_recomputes_and_is_bounded_by_expiry',)),
    Mutation('ignore_current_checkpoint_loss', 'Return a recomputed handle even though its accepted vector is unrecoverable.',
        "    def _require_current_vector(self, chain, head):\n        if head['kind'] == 'handle':",
        '    def _require_current_vector(self, chain, head):\n        if False:',
        (RECOMPUTE_TEST + 'test_compact_replay_does_not_recreate_missing_handle_checkpoint',)),
    Mutation('overwrite_concurrent_winner', 'Publish a conflicting prepared advance despite a different accepted request.',
        '            elif current != proposal.expected_head:', '            elif False:',
        (RECOMPUTE_TEST + 'test_competing_request_parameters_cannot_overwrite_compact_progress',)),
    Mutation('change_original_first_spill_context', 'Treat the first unmanaged input as managed during recomputation.',
        "original_head = head if payload['managed'] else None", 'original_head = head',
        (RECOMPUTE_TEST + 'test_first_spill_retry_preserves_unmanaged_input_size_at_threshold_edge',)),
))


CROSS_CHAIN_TESTS = tuple(
    "reference.placement.tests.test_cross_chain.CrossChainRecoveryTests." + name
    for name in (
        "test_distinct_lost_chains_recover_separately_across_fresh_workers",
        "test_recovery_progress_is_isolated_between_chains",
        "test_lost_fallback_response_replays_after_other_chain_recovers",
        "test_same_lost_chain_concurrent_workers_share_one_accepted_recovery",
    )
)
_RECOVERY_ID = "            chain = 'recovery-' + digest({'input': key, 'policy': self.policy})[:24]"
CROSS_CHAIN = tuple(("protocol.py", _prefix("cross_chain", mutation)) for mutation in (
    Mutation(
        "worker_local_recovery_counter",
        "Fresh workers restart a private counter, aliasing different lost chains.",
        _RECOVERY_ID,
        "            self._recovery_counter = getattr(self, '_recovery_counter', 0) + 1\n"
        "            chain = 'recovery-' + str(self._recovery_counter)",
        CROSS_CHAIN_TESTS,
    ),
    Mutation(
        "constant_recovery_chain_id",
        "Reuse the same replacement chain identity for unrelated recoveries.",
        _RECOVERY_ID, "            chain = 'recovery-reused'", CROSS_CHAIN_TESTS,
    ),
))
CATALOGUE = ONE_WAY + TWO_WAY + RECOMPUTE + CROSS_CHAIN


def _validate_catalogue(catalogue):
    if not catalogue:
        raise ValueError("A nonempty catalogue is required")
    names = []
    for target, mutation in catalogue:
        if target not in TARGETS or not isinstance(mutation, Mutation):
            raise ValueError("A catalogue entry must name an allowlisted placement source and Mutation")
        if not mutation.name or not mutation.tests or any(
            not name.startswith("reference.placement.tests.") for name in mutation.tests
        ):
            raise ValueError("Every mutation needs a name and focused public placement tests")
        names.append(mutation.name)
    if len(set(names)) != len(names):
        raise ValueError("Mutation names must be unique")


def run_checks(*, reference_root=None, catalogue=CATALOGUE, timeout=300):
    """Check a catalogue without editing the original public or local sources."""
    package = REFERENCE_ROOT if reference_root is None else Path(reference_root)
    report = {"status": "failed", "source_sha256": {}, "mutations": []}
    originals = {}
    try:
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout must be positive and finite")
        _validate_catalogue(catalogue)
        for target in sorted({target for target, _ in catalogue}):
            path = package / "placement" / target
            if not path.resolve().is_relative_to(package.resolve()):
                raise ValueError("Placement source resolves outside the public reference package")
            originals[target] = path.read_bytes()
        report["source_sha256"] = {
            target: hashlib.sha256(content).hexdigest() for target, content in originals.items()
        }
        changes = {
            mutation.name: apply_mutation(originals[target].decode("utf-8"), mutation)
            for target, mutation in catalogue
        }
        tests = tuple(sorted({test for _, mutation in catalogue for test in mutation.tests}))
        with tempfile.TemporaryDirectory(prefix="kc-public-placement-mutations-") as temporary:
            scratch = Path(temporary)
            control = scratch / "control"
            _copy_package(package, control)
            report["control"] = _run_tests(control, tests, timeout, mutant=False)
            if report["control"]["status"] != "passed":
                report["detail"] = "Healthy controls must pass before faults can count"
            else:
                for index, (target, mutation) in enumerate(catalogue):
                    work = scratch / f"variant-{index}"
                    _copy_package(package, work)
                    (work / "reference/placement" / target).write_text(
                        changes[mutation.name], encoding="utf-8", newline="\n"
                    )
                    outcome = _run_tests(work, mutation.tests, timeout, mutant=True)
                    report["mutations"].append({
                        "name": mutation.name, "target": target,
                        "description": mutation.description,
                        "tests": list(mutation.tests), **outcome,
                    })
    except (OSError, TypeError, ValueError, SyntaxError) as exc:
        report["detail"] = f"{type(exc).__name__}: {exc}"
    report["source_unchanged"] = all(
        (package / "placement" / name).is_file()
        and (package / "placement" / name).read_bytes() == original
        for name, original in originals.items()
    )
    if (
        report.get("control", {}).get("status") == "passed"
        and report["source_unchanged"]
        and len(report["mutations"]) == len(catalogue)
        and all(outcome["status"] == "detected" for outcome in report["mutations"])
    ):
        report["status"] = "passed"
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", type=Path, help="Optional diagnostic report outside reference/")
    parser.add_argument("--timeout", type=float, default=300, help="Seconds per focused worker (default: 300)")
    parser.add_argument("--list", action="store_true", help="List all named variants without running them")
    args = parser.parse_args(argv)
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        parser.error("--timeout must be positive and finite")
    if args.json and args.json.resolve().is_relative_to(REFERENCE_ROOT):
        parser.error("--json must be outside the public reference source package")
    if args.list:
        for target, mutation in CATALOGUE:
            print(f"{mutation.name} [{target}]: {mutation.description}")
        return 0
    report = run_checks(timeout=args.timeout)
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print("Healthy controls:", report.get("control", {}).get("status", "not run"))
    for outcome in report["mutations"]:
        print(outcome["name"] + ": " + outcome["status"])
        if outcome["status"] != "detected":
            print(outcome.get("output", outcome.get("detail", "")))
    if report.get("detail"):
        print(report["detail"])
    control = report.get("control", {})
    if control.get("status") not in (None, "passed"):
        print(control.get("output", control.get("detail", "")))
    print(f"Placement mutation checks: {report['status']} ({len(report['mutations'])} variants)")
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())

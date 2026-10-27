"""Require every acceptance category to have executed and passed; no traceback export."""

import json
from pathlib import Path
import platform
import subprocess
import sys
import xml.etree.ElementTree as ET

CASES = [
    ("normal_backup", "test_full_encrypted_snapshot_keeps_real_sqlite_wal_and_keys_private"),
    ("normal_restore", "test_restore_preserves_identity_data_and_quarantines_native_and_legacy_jobs"),
    ("nested_directories", "test_nested_state_roots_are_copied_once_and_native_queue_is_still_quarantined"),
    ("crash_before_staging", "test_crash_before_journal_does_not_change_or_gate_original_state"),
    ("crash_during_staging", "test_external_kill_during_staging_recovers_all_roots"),
    ("crash_before_commit", "test_genuine_killed_restore_is_recovered_without_partial_state[PREPARED]"),
    ("crash_in_commit", "test_genuine_killed_restore_is_recovered_without_partial_state[NEW_INSTALLED_1]"),
    ("crash_after_commit", "test_genuine_killed_restore_is_recovered_without_partial_state[COMMITTED]"),
    ("repeat_recovery", "test_genuine_killed_restore_is_recovered_without_partial_state"),
    ("corrupt_manifest", "test_authenticated_but_invalid_manifest_is_rejected"),
    ("corrupt_backup", "test_wrong_key_or_damaged_envelope_never_leaves_plaintext"),
    ("disk_space", "test_disk_shortage_before_staging_leaves_original_data"),
    ("active_dispatcher_worker", "test_restored_unknown_post_is_not_replayed_by_real_service_and_worker"),
    ("changed_cwd", "test_changed_working_directory_does_not_change_restore_targets"),
    (
        "private_keys_encrypted_state",
        "test_restore_preserves_identity_data_and_quarantines_native_and_legacy_jobs",
    ),
    ("unknown_after_real_post", "test_restored_unknown_post_is_not_replayed_by_real_service_and_worker"),
    ("disabled_schedules", "test_restore_preserves_identity_data_and_quarantines_native_and_legacy_jobs"),
    ("revoked_grants", "test_restored_offers_transfers_and_grants_are_locally_revoked_but_identity_is_kept"),
    (
        "no_new_remote_grant",
        "test_restored_offers_transfers_and_grants_are_locally_revoked_but_identity_is_kept",
    ),
    ("projects_results", "test_accepted_producer_keys_projects_history_and_encrypted_cloud_survive"),
    ("accepted_5d_upgrade", "test_accepted_producer_keys_projects_history_and_encrypted_cloud_survive[5D]"),
    ("partial_startup_blocked", "test_genuine_killed_restore_is_recovered_without_partial_state"),
]


def receipt(source, target):
    tests = ET.parse(source).getroot().findall(".//testcase")
    scenarios = []
    for name, pattern in CASES:
        matches = [test for test in tests if test.get("name", "").startswith(pattern)]
        status = (
            "PASSED"
            if matches
            and all(
                all(test.find(tag) is None for tag in ("failure", "error", "skipped")) for test in matches
            )
            else "FAILED"
            if matches
            else "NOT_EXECUTED"
        )
        scenarios.append(dict(name=name, status=status, tests=[test.get("name") for test in matches]))
    report = dict(
        format="poster.recovery.acceptance.v1",
        commit=subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
        source_dirty=bool(subprocess.check_output(["git", "status", "--porcelain", "--untracked-files=no"])),
        os=platform.system(),
        kernel=platform.release(),
        scenarios=scenarios,
        passed=sum(item["status"] == "PASSED" for item in scenarios),
        status="PASSED" if all(item["status"] == "PASSED" for item in scenarios) else "FAILED",
        physical_reboot="NOT_EXECUTED",
    )
    Path(target).write_text(json.dumps(report, indent=2))
    print("::notice title=Stage 6 recovery 22 scenarios::" + json.dumps(report))
    return 0 if report["status"] == "PASSED" else 1


if __name__ == "__main__":
    raise SystemExit(receipt(sys.argv[1], sys.argv[2]))

#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

import yaml

MODULE_PATH = Path(__file__).with_name("statectl.py")
spec = importlib.util.spec_from_file_location("longtask_statectl", MODULE_PATH)
statectl = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(statectl)


def valid_plan(run_id: str) -> dict:
    return {
        "schema_version": 1, "run_id": run_id, "plan_version": 1,
        "title": "test", "objective": "produce a verified result",
        "audience_and_use": "test", "deliverables": [{"id": "D1", "path": "out", "format": "txt", "minimum_content": "x"}],
        "inputs": [{"source": "in", "required_coverage": "all"}],
        "scope": {"in_scope": ["in"], "out_of_scope": []},
        "boundaries": {"allowed_reads": ["in"], "allowed_writes": ["out"], "forbidden_actions": [], "external_side_effects": {"allowed": False, "per_action_approval": True}},
        "success_criteria": [{"id": "C1", "criterion": "out exists", "evidence": "out"}],
        "constraints": {}, "assumptions": [],
        "execution": {
            "controller_mode": "orchestration_only", "max_parallel_workers": 2,
            "worker_policy": {},
            "read_limits": {"max_chars_per_text_result": 20000, "max_chars_per_worker_turn": 80000, "max_lines_per_read": 200, "max_parallel_large_queries": 2},
            "retry_policy": {"max_retries_per_task": 2},
            "supervisor": {"interval_minutes": 5}
        },
        "artifacts": {},
        "tasks": [{
            "id": "T1", "title": "work", "outcome": "out exists", "depends_on": [],
            "worker": {}, "inputs": {}, "outputs": ["out"], "verify": {},
            "side_effects": {}, "idempotency": {}, "failure_policy": {}
        }],
        "final_verification": {"independent_worker": True, "covers_all_success_criteria": True, "output": "verify.json"}
    }


def valid_status(run_id: str, digest: str) -> dict:
    now = "2026-01-01T00:00:00Z"
    return {
        "schema_version": 1, "run_id": run_id, "run_dir": "artifacts/longtask/" + run_id,
        "revision": 0, "plan_version": 1, "plan_sha256": digest,
        "lifecycle": "awaiting_confirmation", "current_stage": "planning",
        "goal": {"goal_id": None, "objective": "test", "status": "active"},
        "approval": {"required": True, "state": "pending", "confirmed_plan_sha256": None, "confirmed_at": None, "confirmed_message_id": None},
        "progress": {"total": 1, "completed": 0, "running": 0, "failed": 0, "blocked": 0, "percent": 0},
        "completed_task_ids": [], "ready_task_ids": ["T1"], "active_task_ids": [], "blocked_task_ids": [], "next_actions": ["confirm"],
        "tasks": {"T1": {"state": "ready", "attempt": 0, "retry_count": 0, "max_retries": 2, "completed_ranges": [], "output_paths": [], "side_effects": []}},
        "task_status_index_path": "index.json",
        "supervisor": {"automation_job_id": None, "interval_minutes": 5, "last_check_at": None, "next_check_at": None, "last_progress_at": None, "consecutive_stale_checks": 0, "last_recovery_action": None, "chat_policy": "x"},
        "artifacts": {}, "recovery": {},
        "final_verification": {"state": "pending", "criteria": [], "evidence_paths": [], "verified_at": None, "worker_ref": None},
        "external_side_effects": [], "last_error": None, "recent_events": [],
        "created_at": now, "updated_at": now
    }


class StateCtlTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.plan_path = self.root / "plan.yaml"
        self.status_path = self.root / "status.json"
        plan = valid_plan("run-test")
        self.plan_path.write_text(yaml.safe_dump(plan, sort_keys=False), encoding="utf-8")
        digest = hashlib.sha256(self.plan_path.read_bytes()).hexdigest()
        self.status_path.write_text(json.dumps(valid_status("run-test", digest), indent=2), encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def test_valid_pair(self):
        statectl.validate_pair(self.plan_path, self.status_path)

    def test_running_requires_confirmation(self):
        status = json.loads(self.status_path.read_text())
        status["lifecycle"] = "running"
        self.status_path.write_text(json.dumps(status), encoding="utf-8")
        with self.assertRaises(statectl.ValidationError):
            statectl.validate_pair(self.plan_path, self.status_path)

    def test_atomic_patch_increments_revision(self):
        updated = statectl.apply_patch(
            self.plan_path, self.status_path,
            {"current_stage": "confirmation"}, 0, "stage_changed", "waiting"
        )
        self.assertEqual(updated["revision"], 1)
        self.assertEqual(updated["current_stage"], "confirmation")
        self.assertEqual(len(updated["recent_events"]), 1)
        statectl.validate_pair(self.plan_path, self.status_path)

    def test_rebind_revised_plan_revokes_confirmation(self):
        plan = yaml.safe_load(self.plan_path.read_text())
        plan["plan_version"] = 2
        plan["title"] = "revised"
        self.plan_path.write_text(yaml.safe_dump(plan, sort_keys=False), encoding="utf-8")
        updated = statectl.rebind_plan(self.plan_path, self.status_path, 0)
        self.assertEqual(updated["plan_version"], 2)
        self.assertEqual(updated["approval"]["state"], "pending")
        self.assertEqual(updated["lifecycle"], "awaiting_confirmation")
        statectl.validate_pair(self.plan_path, self.status_path)

    def test_protected_patch_fields_rejected(self):
        with self.assertRaises(statectl.ValidationError):
            statectl.apply_patch(self.plan_path, self.status_path, {"plan_version": 2}, 0)

    def test_dependency_cycle_rejected(self):
        plan = valid_plan("run-test")
        plan["tasks"][0]["depends_on"] = ["T1"]
        with self.assertRaises(statectl.ValidationError):
            statectl.validate_plan(plan)


if __name__ == "__main__":
    unittest.main(verbosity=2)

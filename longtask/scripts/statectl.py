#!/usr/bin/env python3
"""Validate and atomically update longtask plan/status files."""
from __future__ import annotations

import argparse
import copy
import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any

import yaml

MAX_FILE_BYTES = 20_000
LIFECYCLES = {
    "clarifying", "planning", "awaiting_confirmation", "running", "recovering",
    "verifying", "completed", "paused", "blocked", "cancelled", "failed"
}
EXECUTING = {"running", "recovering", "verifying"}
TASK_STATES = {
    "pending", "ready", "dispatching", "queued", "running", "retry_wait",
    "recovering", "verifying", "completed", "failed", "blocked", "cancelled"
}
HEX64 = re.compile(r"^[0-9a-f]{64}$")


class ValidationError(ValueError):
    pass


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def read_bounded(path: Path) -> bytes:
    data = path.read_bytes()
    if len(data) > MAX_FILE_BYTES:
        raise ValidationError(f"{path} is {len(data)} bytes; cap is {MAX_FILE_BYTES}")
    return data


def read_plan(path: Path) -> dict[str, Any]:
    value = yaml.safe_load(read_bounded(path))
    if not isinstance(value, dict):
        raise ValidationError("plan root must be a mapping")
    return value


def read_status(path: Path) -> dict[str, Any]:
    value = json.loads(read_bounded(path))
    if not isinstance(value, dict):
        raise ValidationError("status root must be an object")
    return value


def sha256_file(path: Path) -> str:
    return hashlib.sha256(read_bounded(path)).hexdigest()


def require_keys(value: dict[str, Any], keys: list[str], label: str) -> None:
    missing = [key for key in keys if key not in value]
    if missing:
        raise ValidationError(f"{label} missing keys: {', '.join(missing)}")


def validate_plan(plan: dict[str, Any]) -> None:
    require_keys(plan, [
        "schema_version", "run_id", "plan_version", "title", "objective",
        "deliverables", "inputs", "scope", "boundaries", "success_criteria",
        "execution", "artifacts", "tasks", "final_verification"
    ], "plan")
    if plan["schema_version"] != 1 or not str(plan["run_id"]).strip():
        raise ValidationError("plan schema_version/run_id invalid")
    if not str(plan["objective"]).strip() or not plan["deliverables"]:
        raise ValidationError("plan objective and deliverables must be non-empty")
    scope = plan["scope"]
    if not isinstance(scope, dict) or "in_scope" not in scope or "out_of_scope" not in scope:
        raise ValidationError("scope must declare in_scope and out_of_scope")
    boundaries = plan["boundaries"]
    if not isinstance(boundaries, dict):
        raise ValidationError("boundaries must be a mapping")
    require_keys(boundaries, ["allowed_reads", "allowed_writes", "forbidden_actions", "external_side_effects"], "boundaries")
    criteria = plan["success_criteria"]
    if not isinstance(criteria, list) or not criteria:
        raise ValidationError("success_criteria must be non-empty")
    criterion_ids = set()
    for item in criteria:
        if not isinstance(item, dict):
            raise ValidationError("success criterion must be a mapping")
        require_keys(item, ["id", "criterion", "evidence"], "success criterion")
        if item["id"] in criterion_ids:
            raise ValidationError(f"duplicate criterion id: {item['id']}")
        criterion_ids.add(item["id"])

    execution = plan["execution"]
    require_keys(execution, ["controller_mode", "max_parallel_workers", "worker_policy", "read_limits", "retry_policy", "supervisor"], "execution")
    if execution["controller_mode"] != "orchestration_only":
        raise ValidationError("controller_mode must be orchestration_only")
    if not isinstance(execution["max_parallel_workers"], int) or execution["max_parallel_workers"] < 1:
        raise ValidationError("max_parallel_workers must be positive")
    limits = execution["read_limits"]
    expected_caps = {
        "max_chars_per_text_result": 20_000,
        "max_chars_per_worker_turn": 80_000,
        "max_lines_per_read": 200,
        "max_parallel_large_queries": 2,
    }
    for key, ceiling in expected_caps.items():
        value = limits.get(key)
        if not isinstance(value, int) or value < 1 or value > ceiling:
            raise ValidationError(f"read limit {key} must be 1..{ceiling}")
    if execution["supervisor"].get("interval_minutes") != 5:
        raise ValidationError("supervisor interval_minutes must be 5")

    tasks = plan["tasks"]
    if not isinstance(tasks, list) or not tasks:
        raise ValidationError("tasks must be a non-empty list")
    ids: set[str] = set()
    deps: dict[str, list[str]] = {}
    for task in tasks:
        if not isinstance(task, dict):
            raise ValidationError("task must be a mapping")
        require_keys(task, [
            "id", "title", "outcome", "depends_on", "worker", "inputs",
            "outputs", "verify", "side_effects", "idempotency", "failure_policy"
        ], "task")
        task_id = str(task["id"])
        if task_id in ids:
            raise ValidationError(f"duplicate task id: {task_id}")
        ids.add(task_id)
        deps[task_id] = list(task["depends_on"])
        if not str(task["outcome"]).strip() or not task["outputs"]:
            raise ValidationError(f"task {task_id} lacks outcome or outputs")
    for task_id, task_deps in deps.items():
        unknown = [dep for dep in task_deps if dep not in ids]
        if unknown:
            raise ValidationError(f"task {task_id} has unknown dependencies: {unknown}")

    visiting: set[str] = set()
    visited: set[str] = set()
    def visit(task_id: str) -> None:
        if task_id in visiting:
            raise ValidationError(f"dependency cycle includes {task_id}")
        if task_id in visited:
            return
        visiting.add(task_id)
        for dep in deps[task_id]:
            visit(dep)
        visiting.remove(task_id)
        visited.add(task_id)
    for task_id in ids:
        visit(task_id)

    final = plan["final_verification"]
    if not isinstance(final, dict) or final.get("independent_worker") is not True or final.get("covers_all_success_criteria") is not True:
        raise ValidationError("final verification must be independent and cover all criteria")


def validate_status(status: dict[str, Any]) -> None:
    require_keys(status, [
        "schema_version", "run_id", "run_dir", "revision", "plan_version",
        "plan_sha256", "lifecycle", "current_stage", "goal", "approval",
        "progress", "tasks", "supervisor", "artifacts", "recovery",
        "final_verification", "external_side_effects", "last_error",
        "recent_events", "created_at", "updated_at"
    ], "status")
    if status["schema_version"] != 1 or not str(status["run_id"]).strip():
        raise ValidationError("status schema_version/run_id invalid")
    if not isinstance(status["revision"], int) or status["revision"] < 0:
        raise ValidationError("status revision must be a non-negative integer")
    if status["lifecycle"] not in LIFECYCLES:
        raise ValidationError(f"invalid lifecycle: {status['lifecycle']}")
    if not HEX64.fullmatch(str(status["plan_sha256"])):
        raise ValidationError("plan_sha256 must be lowercase SHA-256")
    approval = status["approval"]
    if not isinstance(approval, dict) or approval.get("state") not in {"pending", "confirmed", "revoked"}:
        raise ValidationError("approval state invalid")
    if status["lifecycle"] in EXECUTING:
        if approval.get("state") != "confirmed" or approval.get("confirmed_plan_sha256") != status["plan_sha256"]:
            raise ValidationError("executing lifecycle requires confirmation of current plan hash")
    supervisor = status["supervisor"]
    if not isinstance(supervisor, dict) or supervisor.get("interval_minutes") != 5:
        raise ValidationError("status supervisor interval must be 5")
    tasks = status["tasks"]
    if not isinstance(tasks, dict):
        raise ValidationError("status tasks must be an object")
    for task_id, task in tasks.items():
        if not isinstance(task, dict) or task.get("state") not in TASK_STATES:
            raise ValidationError(f"status task {task_id} has invalid state")
        for key in ["attempt", "retry_count", "max_retries"]:
            if not isinstance(task.get(key), int) or task[key] < 0:
                raise ValidationError(f"status task {task_id} invalid {key}")
        if task["retry_count"] > task["max_retries"]:
            raise ValidationError(f"status task {task_id} exceeded max_retries")
        for key in ["completed_ranges", "output_paths", "side_effects"]:
            if not isinstance(task.get(key), list):
                raise ValidationError(f"status task {task_id} invalid {key}")
    events = status["recent_events"]
    if not isinstance(events, list) or len(events) > 20:
        raise ValidationError("recent_events must contain at most 20 items")
    if status["lifecycle"] == "completed" and status["final_verification"].get("state") != "passed":
        raise ValidationError("completed lifecycle requires passed final_verification")


def validate_pair(plan_path: Path, status_path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    plan = read_plan(plan_path)
    status = read_status(status_path)
    validate_plan(plan)
    validate_status(status)
    if str(plan["run_id"]) != str(status["run_id"]):
        raise ValidationError("plan/status run_id mismatch")
    digest = sha256_file(plan_path)
    if status["plan_sha256"] != digest:
        raise ValidationError("status plan_sha256 does not match plan file")
    if int(plan["plan_version"]) != int(status["plan_version"]):
        raise ValidationError("plan/status plan_version mismatch")
    plan_ids = {str(task["id"]) for task in plan["tasks"]}
    unknown = set(status["tasks"]) - plan_ids
    if unknown:
        raise ValidationError(f"status contains tasks absent from plan: {sorted(unknown)}")
    return plan, status


def deep_merge(base: Any, patch: Any) -> Any:
    if isinstance(base, dict) and isinstance(patch, dict):
        result = copy.deepcopy(base)
        for key, value in patch.items():
            result[key] = deep_merge(result.get(key), value) if key in result else copy.deepcopy(value)
        return result
    return copy.deepcopy(patch)


def write_atomic(path: Path, value: dict[str, Any]) -> None:
    payload = (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()
    if len(payload) > MAX_FILE_BYTES:
        raise ValidationError(f"updated status is {len(payload)} bytes; cap is {MAX_FILE_BYTES}")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
        dir_fd = os.open(path.parent, os.O_DIRECTORY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)


def apply_patch(plan_path: Path, status_path: Path, patch: dict[str, Any], expected_revision: int,
                event_kind: str | None = None, event_message: str | None = None,
                task_id: str | None = None) -> dict[str, Any]:
    protected = {"revision", "updated_at", "run_id", "run_dir", "plan_version", "plan_sha256"}
    changed_protected = sorted(protected.intersection(patch))
    if changed_protected:
        raise ValidationError(f"patch must not set protected fields: {changed_protected}; use rebind-plan for plan changes")
    lock_path = status_path.with_name(status_path.name + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        plan, current = validate_pair(plan_path, status_path)
        if current["revision"] != expected_revision:
            raise ValidationError(f"revision conflict: expected {expected_revision}, found {current['revision']}")
        updated = deep_merge(current, patch)
        updated["revision"] = current["revision"] + 1
        updated["updated_at"] = utc_now()
        if event_kind:
            events = list(updated.get("recent_events", []))
            event = {"at": updated["updated_at"], "kind": event_kind, "message": event_message or ""}
            if task_id:
                event["task_id"] = task_id
            events.append(event)
            updated["recent_events"] = events[-20:]
        validate_status(updated)
        if str(updated["run_id"]) != str(plan["run_id"]):
            raise ValidationError("patch changed run_id away from plan")
        if int(updated["plan_version"]) != int(plan["plan_version"]):
            raise ValidationError("patch plan_version mismatch")
        if updated["plan_sha256"] != sha256_file(plan_path):
            raise ValidationError("patch plan hash mismatch")
        plan_ids = {str(task["id"]) for task in plan["tasks"]}
        unknown = set(updated["tasks"]) - plan_ids
        if unknown:
            raise ValidationError(f"patch introduced tasks absent from plan: {sorted(unknown)}")
        write_atomic(status_path, updated)
        return updated


def rebind_plan(plan_path: Path, status_path: Path, expected_revision: int) -> dict[str, Any]:
    """Bind a deliberately revised plan and revoke the old confirmation."""
    lock_path = status_path.with_name(status_path.name + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        current = read_status(status_path)
        validate_status(current)
        if current["revision"] != expected_revision:
            raise ValidationError(f"revision conflict: expected {expected_revision}, found {current['revision']}")
        if current["lifecycle"] in EXECUTING or current.get("active_task_ids"):
            raise ValidationError("stop active work before rebinding a revised plan")
        if current["supervisor"].get("automation_job_id"):
            raise ValidationError("stop and clear the supervisor automation before rebinding a revised plan")
        plan = read_plan(plan_path)
        validate_plan(plan)
        if str(plan["run_id"]) != str(current["run_id"]):
            raise ValidationError("revised plan must retain the current run_id")
        if int(plan["plan_version"]) != int(current["plan_version"]) + 1:
            raise ValidationError("revised plan_version must increment by exactly one")
        now = utc_now()
        updated = copy.deepcopy(current)
        updated["revision"] = current["revision"] + 1
        updated["plan_version"] = int(plan["plan_version"])
        updated["plan_sha256"] = sha256_file(plan_path)
        updated["lifecycle"] = "awaiting_confirmation"
        updated["current_stage"] = "planning"
        updated["approval"] = {
            "required": True, "state": "pending",
            "confirmed_plan_sha256": None, "confirmed_at": None,
            "confirmed_message_id": None
        }
        updated["progress"] = {
            "total": len(plan["tasks"]), "completed": 0, "running": 0,
            "failed": 0, "blocked": 0, "percent": 0
        }
        updated["completed_task_ids"] = []
        updated["ready_task_ids"] = []
        updated["active_task_ids"] = []
        updated["blocked_task_ids"] = []
        updated["next_actions"] = ["review revised plan", "obtain user confirmation"]
        updated["tasks"] = {}
        updated["final_verification"] = {
            "state": "pending", "criteria": [], "evidence_paths": [],
            "verified_at": None, "worker_ref": None
        }
        updated["recovery"] = {
            "resume_phase": "awaiting_confirmation", "resume_task_ids": [],
            "resume_cursor": None, "last_safe_revision": updated["revision"],
            "reason": "plan_revised"
        }
        events = list(updated.get("recent_events", []))
        events.append({"at": now, "kind": "plan_rebound", "message": f"plan version {plan['plan_version']} awaits confirmation"})
        updated["recent_events"] = events[-20:]
        updated["updated_at"] = now
        validate_status(updated)
        write_atomic(status_path, updated)
        validate_pair(plan_path, status_path)
        return updated


def compact_summary(status: dict[str, Any]) -> dict[str, Any]:
    return {
        "run_id": status["run_id"],
        "revision": status["revision"],
        "lifecycle": status["lifecycle"],
        "stage": status["current_stage"],
        "progress": status["progress"],
        "active_task_ids": status.get("active_task_ids", []),
        "blocked_task_ids": status.get("blocked_task_ids", []),
        "next_actions": status.get("next_actions", [])[:10],
        "last_error": status.get("last_error"),
        "last_check_at": status["supervisor"].get("last_check_at"),
        "automation_job_id": status["supervisor"].get("automation_job_id"),
        "updated_at": status["updated_at"],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    validate_cmd = sub.add_parser("validate")
    validate_cmd.add_argument("--plan", required=True, type=Path)
    validate_cmd.add_argument("--status", required=True, type=Path)
    hash_cmd = sub.add_parser("hash-plan")
    hash_cmd.add_argument("plan", type=Path)
    summary_cmd = sub.add_parser("summary")
    summary_cmd.add_argument("status", type=Path)
    patch_cmd = sub.add_parser("patch")
    patch_cmd.add_argument("--plan", required=True, type=Path)
    patch_cmd.add_argument("--status", required=True, type=Path)
    patch_cmd.add_argument("--patch", required=True, type=Path)
    patch_cmd.add_argument("--expect", required=True, type=int)
    patch_cmd.add_argument("--event-kind")
    patch_cmd.add_argument("--event-message")
    patch_cmd.add_argument("--task-id")
    rebind_cmd = sub.add_parser("rebind-plan")
    rebind_cmd.add_argument("--plan", required=True, type=Path)
    rebind_cmd.add_argument("--status", required=True, type=Path)
    rebind_cmd.add_argument("--expect", required=True, type=int)
    args = parser.parse_args()
    try:
        if args.command == "validate":
            validate_pair(args.plan, args.status)
            print("longtask state: valid")
        elif args.command == "hash-plan":
            print(sha256_file(args.plan))
        elif args.command == "summary":
            status = read_status(args.status)
            validate_status(status)
            print(json.dumps(compact_summary(status), ensure_ascii=False, indent=2))
        elif args.command == "patch":
            patch = json.loads(read_bounded(args.patch))
            if not isinstance(patch, dict):
                raise ValidationError("patch root must be an object")
            status = apply_patch(args.plan, args.status, patch, args.expect,
                                 args.event_kind, args.event_message, args.task_id)
            print(json.dumps(compact_summary(status), ensure_ascii=False, indent=2))
        elif args.command == "rebind-plan":
            status = rebind_plan(args.plan, args.status, args.expect)
            print(json.dumps(compact_summary(status), ensure_ascii=False, indent=2))
    except (OSError, json.JSONDecodeError, yaml.YAMLError, ValidationError) as exc:
        print(f"longtask state error: {exc}", file=__import__("sys").stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

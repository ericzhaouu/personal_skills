# Five-minute longtask supervisor

Supervise run <RUN_ID> from the Controller Session. Do not perform substantive task work. The Controller must interpolate absolute paths for <PLAN_PATH>, <STATUS_PATH>, <STATECTL_PATH> and <PROTOCOL_PATH> before creating this Automation.

On every tick:

1. Read artifacts/status.json and artifacts/plan.yaml only within their 20,000-byte caps; validate them with statectl and stop dispatch if invalid or the plan hash/approval differs.
2. Inspect only bounded summaries for active task/background/session/process/Workboard ids and the named receipt files. Never read source corpora, complete transcripts or complete logs.
3. Reconcile accepted execution versus delivery status, output hashes, side effects, completed ranges and next_range.
4. Atomically update status revision, supervisor.last_check_at, progress, task states and next actions. Refresh the session Progress Card every tick with phase, completed/total, active, blocked, last check and next action.
5. Dispatch newly ready tasks within max_parallel_workers. Recover failures exactly as references/runtime-protocol.md specifies. Retry at most twice and never retry an uncertain non-idempotent effect.
6. If healthy with no meaningful transition, return NO_REPLY after updating state/card. Report only a transition, recovery action, blocker, request for user decision, or completion.
7. When all production tasks finish, dispatch Reduce then an independent Verify Worker. Complete only after verification passes. Stop this Automation after terminal cleanup and record any cleanup failure.

Use expected status revision for every write. If another Controller tick won the revision race, reread once and reconcile; do not overwrite newer state.

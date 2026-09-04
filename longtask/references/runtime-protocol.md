# 运行与恢复协议

## 固定路径与写者

- artifacts/plan.yaml：Controller 写；用户确认后冻结。修改时先归档旧版本并使确认失效。
- artifacts/status.json：Controller/监督 tick 通过 statectl 原子更新；它是动态状态唯一事实源。
- artifacts/longtask/<run_id>/manifest.json：输入清单、大小、行数、哈希、分片；由 inventory Worker 生成。
- receipts/<task-id>-attempt-<n>.json：Worker 唯一结构化回执。
- results/、errors/、verification/：完整产物、失败证据、验收证据。

Worker 不得写 plan 或 status。Controller 不把完整结果复制进 status，只保存路径、哈希、范围、错误摘要和恢复游标。status 与 plan 各不超过 20,000 字节；recent_events 最多 20 条。详细 shard 状态放入 receipts 或 shard-index.json。

## 任务设计

每个 plan task 至少包含：id、title、outcome、depends_on、worker、inputs、outputs、verify、side_effects、idempotency、failure_policy。任务大小以一次隔离上下文可完成并验证为准。大输入先 inventory，再按文件、行范围或字节范围分片；Reduce 只读分片摘要/产物；Verify 与生产 Worker 分离。

Worker 选择顺序：

1. 可见、isolated native subagent：多步分析、编码、可回看或可 steer 的工作。
2. Background Task/后台进程：确定性脚本、构建、测试和短小一次性工作；保存 task/process id。
3. GitHub Copilot ACP：仅运行时明确列出该 ACP 时使用；保存精确 runtime/agent id。不得选择其他 ACP。

所有 Worker 的输入只包含任务切片、必要规则和输出契约，不复制主 Session transcript。最大并发默认 2；计划可降低，有外部副作用时默认 1。

## 状态机

clarifying -> planning -> awaiting_confirmation -> running -> recovering -> verifying -> completed

旁路终态/停点：paused、blocked、cancelled、failed。running/recovering/verifying 必须持有与当前 plan_sha256 匹配的用户确认。completed 必须有独立 verification=passed。计划改变后回到 awaiting_confirmation。

status 至少保存：run_id、revision、plan_version/hash、lifecycle、current_stage、Goal 引用、用户确认、任务状态、依赖就绪项、Worker/task/run/session/process/card id、attempt/retry、assigned/completed/next range、checkpoint/receipt/output 路径与哈希、外部副作用、最近心跳、最后错误、监督 job/检查时间、恢复入口、最终验证及时间戳。

## 五分钟协调循环

1. 用 statectl validate 校验 plan/status/hash；失败时停止派工并报告状态损坏。
2. 读取 status 摘要；通过 subagents、sessions_history、Workboard 或后台任务工具查询活动 id。每个查询必须有范围和数量上限。
3. 对每个活动任务核对 receipt、输出和运行态；执行成功与结果投递失败分开记录。
4. 更新完成、失败、停滞和 ready 集合；通过 expected revision 原子写 status。
5. 在并发上限内派发 ready 项，先写预留状态，spawn 接受后补齐运行 id；spawn 失败则回滚为 retry_wait。
6. 更新 Progress Card：run_id、阶段、完成/总数、活动项、最近检查、下一动作。
7. 只有状态迁移、恢复动作、需用户决定或最终结果才聊天；健康无变化返回 NO_REPLY。

## 错误分类与动作

- transient/rate_limited：遵守 Retry-After；没有时按下一次 tick、再后两个 tick退避。最多两次重试。
- context_overflow：核验已写输出，把剩余分片缩小（通常减半），从 next_range 启动干净上下文；不重跑已完成范围。
- stalled：第一次连续超时先 steer 保存 checkpoint/receipt；下一 tick 仍无进展才定向取消，核验后替换 Worker。
- lost/timed_out/cancelled：先查 receipt、输出哈希和副作用，再仅恢复未完成范围。
- delivery_failed：执行结果仍可能成功；读取任务/session 结果并只重试投递，不重新执行。
- validation_failed：创建最小修复任务，仅重做未通过的 criterion/range。
- permission/auth：标记 blocked，请用户通过受保护方式补充权限；不得索取或记录秘密。
- ambiguous_side_effect：立即 blocked，核验外部事实后再决定；禁止盲重试非幂等动作。
- permanent/retries_exhausted：保留错误和已完成产物，blocked 并给出 2–3 个处理选项。

自动恢复必须使用新的 attempt 和幂等键，保留旧 receipt；不得删除失败证据。失败一次不等于放弃，但安全边界高于完成。

## 计划修改与恢复

恢复前依次核验：status 可解析；plan hash；用户确认仍有效；活动任务真实状态；receipt/output 哈希；外部副作用；next_range；监督 job。发现不一致时进入 recovering，不猜测完成。Gateway 或 Session 重启后仅依赖这些持久文件与任务 id，不依赖聊天记忆。

计划在执行中实质修改时：暂停新派工，保存 checkpoint，停止并清空监督 job，归档旧 plan，递增 plan_version，重新计算依赖和已完成产物的可复用性，再运行 statectl.py rebind-plan 绑定新哈希并清空确认，等待用户重新确认。普通 patch 禁止修改 run_id、plan_version 或 plan_sha256。rebind-plan 会保守清空动态任务摘要；Controller 可在核验证据后重新登记可复用产物。
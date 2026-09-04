---
name: "longtask"
description: "运行长任务或 /longtask；/longtask 状态、暂停、继续、取消：澄清后计划，确认后编排隔离执行、定时监督、恢复、验收。"
---

# Longtask Controller

## 1. 路由请求

把请求归为以下一个动作：

- start：用户明确说“运行长任务”“启动长任务”或使用 /longtask 并描述新任务。
- status：用户查询状态或进度。
- pause：用户要求暂停；保留全部产物和恢复点。
- resume：用户要求继续或恢复。
- cancel：用户要求取消；停止执行但不删除产物。
- revise：用户要求修改尚未确认或正在执行的计划。

先读取 artifacts/status.json（存在时最多 20,000 字符），再选择分支。发现另一个非终态运行时禁止覆盖，要求用户选择继续、暂停或取消现有运行；这个选择计入澄清问题额度。状态、暂停、继续、取消分支不得重新进行需求访谈。

**完成判据：** 已确定唯一动作、活动 run_id 和可安全执行的下一步。

## 2. 明确任务契约

仅对 start 或实质性 revise 执行 references/intake.md。复用用户已经给出的信息，采用苏格拉底式问题揭示目标、范围、验收和风险；整个澄清阶段最多提出五个编号问题，追问也计数。达到五问仍有缺口时，写出最保守、可撤销的假设，不自行授权外部副作用。

形成一份规范化契约：目标、交付物、输入、范围内、范围外、允许写入、禁止动作、外部副作用、成功标准及证据、期限/资源约束。向用户复述契约，但此时不要启动 Worker。

**完成判据：** 每个契约字段都有用户事实或显式假设，且所有不可逆边界均明确。

## 3. 建立 Goal 与持久状态

调用 get_goal 检查当前 Session。无 Goal 时，用 create_goal 建立简洁目标，包含交付物、边界、验收摘要和 artifacts/plan.yaml；用户给出 token budget 时才设置。已有同一长任务 Goal 时复用；已有冲突 Goal 时停止并请用户处理，禁止清除或替换。

固定使用：

- artifacts/plan.yaml：当前待确认或已确认的执行计划。
- artifacts/status.json：唯一的可恢复动态状态事实源。
- artifacts/longtask/<run_id>/：manifest、receipts、results、errors、plan-history 和 verification。

一个 Workspace 同时只允许一个使用上述固定路径的活动 longtask。新任务开始前，把已终态旧文件复制到旧 run 目录归档；禁止覆盖活动状态。按 templates/plan.yaml 和 templates/status.json 创建文件，用 scripts/statectl.py 校验。plan/status 均保持不超过 20,000 字节；规模更大时把分片明细放入 run 目录，status 只保留索引、活动项和恢复游标。

**完成判据：** Goal 不冲突；两个固定文件有效、run 目录存在，run_id 与 plan_sha256 一致。

## 4. 规划但不执行

按 references/runtime-protocol.md 生成有向无环任务图。每个任务必须写明 outcome、依赖、输入或分片、Worker 类型、输出路径、验证方法、失败策略、副作用等级和幂等/去重办法。把 Reduce 与独立 Verify 也列为任务。主 Session 只做澄清、计划、派工、状态更新、监督和沟通；除确定性的状态/校验辅助脚本外，不执行分析、编码、批量读取或最终产物制作。

优先使用隔离且可见的 native subagent Session 承担多步任务；短小一次性工作可用 Background Task 或受控后台进程。只有 agents_list 或运行时元数据明确标识为 GitHub Copilot 的 ACP 才可选用；不得尝试 Codex、Claude、Gemini、OpenCode 或根据模型名猜 ACP id。没有可用 GitHub Copilot ACP 时改用 native subagent，不把它当阻塞。

在 artifacts/status.json 中写入 planning 结果，将 lifecycle 设为 awaiting_confirmation、approval.state 设为 pending，并更新 Progress Card。不要创建监督 Automation、Workboard claim 或 Worker。

**完成判据：** 任务图无环、每项可独立验证、所有成功标准都有负责的验证任务，且状态停在 awaiting_confirmation。

## 5. 取得计划确认

向用户展示目标、范围、交付物、任务顺序/并发、关键输出、风险、副作用和失败恢复策略的紧凑摘要，并明确询问“确认执行此版计划吗？可回复确认、修改或取消。”只有用户当前回合对当前 plan_sha256 的明确同意才算确认；需求澄清回答、沉默和旧计划确认都不算。

用户要求修改时递增 plan_version，把旧计划复制到 plan-history，重算哈希，清空确认并再次询问。确认后记录 confirmed_plan_sha256、confirmed_at 和可用的消息标识。任何后续计划实质变更都会使确认失效并回到本步骤。

**完成判据：** approval.state=confirmed 且 confirmed_plan_sha256 等于当前 plan_sha256，或任务保持未执行并等待用户决定。

## 6. 初始化执行

确认后才执行以下动作：

1. 为多任务计划创建 Workboard 父卡和有依赖的子卡；把 card id 写入 status。工具不可用时以 plan/status 为后备，不创建第二套自定义数据库。
2. 用 automations 创建绑定当前 Controller Session、每五分钟一次、名称含 run_id 的监督 Loop；提示词采用 templates/supervisor-prompt.md。把 job id 写入 status。Loop 创建失败时不要启动 Worker，标记 supervision_setup_failed 并报告。
3. 将 ready 任务通过 sessions_spawn、Background Task 或受控后台进程派给隔离 Worker；任务说明采用 templates/worker-prompt.md。spawn 被接受后立即记录 task/run/session/process id、attempt、assigned_range、receipt_path 和 started_at。
4. 同时运行数不得超过 plan.execution.max_parallel_workers；有副作用的任务默认串行。

主 Session 不等待轮询；依赖完成推送与五分钟监督。每次状态写入都通过 scripts/statectl.py 的 revision 检查和原子替换完成。

**完成判据：** 监督 job 已持久化，所有已启动 Worker 均可追踪，未满足依赖的任务仍为 pending。

## 7. 监督、推进与恢复

每次 Controller 回合及监督 tick 执行 references/runtime-protocol.md 的协调循环：只读 plan、status、任务摘要和 receipt；更新 status 与 Progress Card；为新 ready 项派工；按错误类别恢复。每五分钟必须刷新 supervisor.last_check_at，即使没有进展。健康时只更新 Progress Card，聊天保持 NO_REPLY；状态迁移、自动恢复、需人工决策或最终完成时才发消息。

Loop 禁止读取原始大文件、完整 transcript 或完整日志。Worker 必须遵守：单次文本工具结果不超过 20,000 字符、单轮合计不超过 80,000 字符、单次最多约 200 行、最多并行两个可能产生大输出的查询；完整内容落盘，只向 Controller 返回摘要、证据范围和路径。

失败后先核验 receipt、输出哈希及外部副作用，再决定重试。默认每个任务最多自动重试两次；context overflow 缩小分片并从 next_range 启动干净 Worker；delivery failure 只恢复投递；连续停滞先要求保存 checkpoint，下一次仍停滞才定向取消并替换；权限不足、未知副作用或重试耗尽时转 blocked 并询问用户。

**完成判据：** status 与实际任务一致，所有可恢复失败已有下一动作，且没有无主 Worker 或越界读取。

## 8. 验收与收尾

所有生产任务完成后派发 Reduce；随后用独立 Worker 和 templates/verifier-prompt.md 逐条核对 success_criteria、覆盖率、输出哈希和副作用。主 Session 只读取 verification 摘要。验证失败时只创建修复失败标准所需的任务；禁止伪造完成。

全部通过后依次：写 status.lifecycle=completed 与验证证据；停止监督 Automation；确认无活动 Worker/后台进程和未结 Workboard claim；调用 update_goal 标记 complete；把 Progress Card 更新为最终摘要并交付产物路径。Automation 停止失败时记录 cleanup_pending，不能隐瞒。

pause：停止派工，请活动 Worker 先写 receipt/checkpoint，再安全取消，停止监督 job，写 paused。模型工具不能暂停 Goal；提醒用户如需同步暂停 Goal，使用 /goal pause。

resume：先校验 plan hash、确认、receipt 和副作用，再重建监督 job并从 recovery.resume_task_ids/next_range 继续；Goal 已被用户暂停时，请用户先用 /goal resume。

cancel：停止监督和活动任务，保留全部文件，写 cancelled；不得把 Goal 标记 complete，也不得替用户清除 Goal，可提示 /goal clear。

**完成判据：** 验收通过且所有执行面已收口，或状态准确停在 paused/cancelled/blocked 并保留可恢复证据。

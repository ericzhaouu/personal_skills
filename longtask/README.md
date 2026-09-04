# Longtask

把复杂、耗时的任务拆成经用户确认的可恢复计划，并通过隔离 Worker、定时监督、失败恢复和独立验收持续执行到可验证完成。

## 适用场景

- 需要多个步骤、较长执行时间或并行处理的任务。
- 需要在中断、超时或上下文溢出后继续执行的任务。
- 需要保留计划、状态、回执、输出哈希和验收证据的任务。
- 需要随时查询状态、暂停、继续、修改或取消的任务。

## 主要功能

- 在启动前澄清目标、范围、交付物、成功标准和副作用边界。
- 生成任务依赖图与可验证的执行计划，并等待用户明确确认。
- 使用 Goal、Progress Card、Workboard 和持久化状态记录任务进度。
- 将实际工作派发给隔离 Worker，主 Session 只负责编排、监督与沟通。
- 通过定时 Automation 检查停滞、投递失败、权限问题和可恢复错误。
- 支持 checkpoint、receipt、输出哈希、有限重试和断点恢复。
- 在交付前由独立 Worker 验收成功标准、覆盖率、事实一致性和副作用。
- 支持状态查询、暂停、继续、计划修改和取消。

## 安装

克隆仓库后，将本文件夹复制到 OpenClaw 的本地 Skill 目录：

```text
~/.openclaw/skills/longtask/
```

请保留完整目录结构：

```text
longtask/
├── SKILL.md
├── README.md
├── references/
│   ├── intake.md
│   └── runtime-protocol.md
├── scripts/
│   ├── statectl.py
│   └── test_statectl.py
└── templates/
    ├── plan.yaml
    ├── status.json
    ├── supervisor-prompt.md
    ├── verifier-prompt.md
    └── worker-prompt.md
```

## 使用方式

启动长任务：

```text
/longtask <任务描述>
```

也可以直接用自然语言，例如：

```text
请把这个研究任务作为长任务运行：梳理指定资料，生成可追溯报告并独立验收。
```

运行控制：

```text
/longtask 状态
/longtask 暂停
/longtask 继续
/longtask 修改 <新的要求>
/longtask 取消
```

## 执行流程

1. **明确任务契约**：最多通过五个编号问题补齐目标、输入、范围、验收和风险边界。
2. **建立计划与状态**：创建 Goal、任务图和可恢复状态，但暂不执行。
3. **等待计划确认**：向用户展示计划摘要；只有明确确认当前版本后才启动 Worker。
4. **隔离执行与监督**：按依赖和并发限制派工，每五分钟监督进度并自动处理可恢复故障。
5. **独立验收与收尾**：核对全部成功标准、回执和哈希，停止监督任务并交付产物。

## 持久化产物

Skill 在当前 Workspace 使用以下固定入口：

```text
artifacts/plan.yaml
artifacts/status.json
artifacts/longtask/<run_id>/
```

其中 `artifacts/status.json` 是动态状态的唯一事实源；状态更新通过 `scripts/statectl.py` 的 revision 检查和原子替换完成。

## 关键约束

- 未确认计划时不启动 Worker，也不创建监督 Automation。
- 主 Session 不直接承担批量分析、编码或最终产物制作。
- 默认单任务最多自动重试两次，并在重试前检查已有输出和外部副作用。
- 验收未通过时不得宣称完成；只创建修复失败标准所需的任务。
- 暂停和取消会保留已有产物、回执与恢复点。

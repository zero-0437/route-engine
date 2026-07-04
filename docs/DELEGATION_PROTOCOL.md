# 子 Agent 委派协议

> 主 Agent 到子 Agent 之间标准化的任务委派上下文规范

## 概述

主 Agent 通过 `delegate_task` 将任务委派给子 Agent（programmer、error-analyst 等）。委派时在 context 开头注入执行纪律，子 Agent 按规范执行并回报。

## 委派前 6 问

主 Agent 每次 `delegate_task` 前必须确认：

1. **决策层和执行层分清了？**
2. **evidence ledger 要求明确？**（子 Agent 需通过真正运行检查确认完成，附验证证据）
3. **技能在白名单内？**
4. **上下文已最小化？**（用路径引用而非全文）
5. **失败回滚路径存在？**（连续失败→挂起→升级用户）
6. **任务描述可执行？**（禁止占位符，deliverable 有可验证终点，含 evidence ledger）

## 执行纪律注入

每次 `delegate_task` 前在 context 开头注入 `agent-environment.md §一`：

### 回报三要素

子 Agent 完成后向主 Agent 回报三个字段：

| 要素 | 说明 |
|------|------|
| **状态** | `DONE` / `DONE_WITH_CONCERNS` / `BLOCKED` / `NEEDS_CONTEXT` |
| **产物路径** | 完整报告写入 `/opt/data/.shared/{task_id}/`，回报只给路径（不含文件内容） |
| **证据** | evidence ledger — 通过真正运行检查确认完成（测试结果摘要/校验和/日志快照/实际输出摘要，至少一项） |

### NEEDS_CONTEXT 处理

当子 Agent 返回 `NEEDS_CONTEXT`：
1. 主 Agent 原样转发用户
2. 等待用户回答
3. 重新委派相同 `task_id`，context 追加用户回答

## 委派参数结构

| 参数名 | 必填 | 说明 |
|--------|------|------|
| `task_id` | ✅ | 子任务唯一编号（与 PM 拆解清单对齐） |
| `task_description` | ✅ | 完整任务描述 |
| `skill_required` | ✅ | 所需技能（必须从 skill-map.yaml 白名单匹配） |
| `input_context` | ✅ | 最小必要上下文（端口号、文件路径、字段名等） |
| `output_format` | ✅ | 输出精简要求（只交什么，禁止交什么） |
| `constraints` | ❌ | 硬性约束（版本、格式、性能等） |
| `dependencies` | ✅ | 前置任务 ID（无则 `null`） |
| `evidence` | ✅ | evidence ledger：文件路径 / 测试输出摘要 / 校验和 |

## 连续失败兜底

同一委派连续失败 2 次：
1. 挂起执行链
2. 四阶段诊断：根因 → 模式 → 假设 → 修复
3. 输出诊断报告 + 可行方案
4. 等待用户决策

**严禁静默重试。**

## 技能白名单

技能定义在 `/opt/data/skill-map.yaml`，运行时缓存 `/opt/data/.skill-cache.json`（TTL 30 分钟）。

子 Agent 规则：
- 仅从白名单匹配技能
- context 已指定的技能直接使用，**禁止逐个 skill_view 验证**
- 技能不足 → 上报建议（技能名 + 理由），主 Agent 批准后方可加载
- 禁止静默替换

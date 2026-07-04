# Route Engine Plugin - 路由引擎插件

> **Zero-Token Routing and Chain Orchestration Engine for Hermes Agent**
> 零 Token 路由与链式编排引擎

---

## What Problems Does It Solve / 解决了什么问题

### Task Chain Discontinuity / 任务链不连续中断问题

Multi-agent workflows often break mid-way. The Chain Executor state machine solves this:
- Auto-progression: each step advances to the next, no manual handover
- Context continuity: steps share context via brief files, not user re-explanation
- Parallel efficiency: 3-branch dual-review runs simultaneously, O(3) -> O(1)
- Failure recovery: NEEDS_CONTEXT pause, branch progress collection, rollback paths

多 Agent 协作管线经常中断。Chain Executor 状态机解决了这个问题：每步自动推进，通过 brief 文件和结果传递共享上下文，三路并行评审，内置暂停和回滚路径。

### Zero-Token Routing / 零 Token 路由

Every routing decision costs 0 LLM tokens. Pure local rule matching.
每一次路由决策消耗 0 LLM token。纯本地规则匹配。

### Precise Agent Dispatch / 精准 Agent 路由

Three matching modes (keyword/phrase/regex) + three filters (weight/threshold/priority).
三种匹配模式 + 三重过滤，每次都精准派到对的 Agent。

### Universal Chain Orchestration / 通用链式编排

Five step types: serial, parallel, batch, interactive, loop.
五种步骤类型覆盖所有工作流模式。

---

## Advantages / 优势

| Advantage | Description |
|-----------|-------------|
| Zero Token | Routing never touches LLM, pure local YAML+Python matching |
| State Machine | Multi-agent collaboration with auto-progression |
| YAML-Defined | Add/change/delete routes without modifying code |
| Plugin Architecture | Plugs into Hermes as zero-intrusion plugin |
| Parallel Execution | Built-in parallel step for concurrent work |
| Fault Recovery | NEEDS_CONTEXT pause, branch collection, rollback |
| Sub-millisecond | ~0.4ms per routing decision, 5s timeout fallback |
| Open Architecture | 12+ built-in agents, freely extensible |

---

## Quick Start / 快速开始



## Repos / 仓库

| Repo | Visibility | Content |
|------|-----------|---------|
| zero-0437/hermes-zero-token-router | Private | Full source + production route-map |
| zero-0437/route-engine-docs | Public | Docs + sanitized source + examples |

---
*Built for Hermes Agent - Route Smart, Execute Continuously*
*为 Hermes Agent 构建 - 智能路由，持续执行*
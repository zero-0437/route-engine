# Fuzzy Phase B 真实状态

> 2026-07-05 发现：route_engine 对 `type: regex` 的规则显式忽略 fuzzy 参数（`# regex + fuzzy 无意义，静默忽略`），因此 Phase B 中 8 条嵌在复合 regex 内的 pattern 无法直接加 `fuzzy: true`。

## 已完成的 Phase B 项（实际已在 Phase A 或更早开启）
| 文件 | pattern | 类型 | 状态 |
|------|---------|------|------|
| file-ops.yaml | 目录结构 | phrase | ✅ 已有 fuzzy: true |
| file-ops.yaml | 文件操作 | phrase | ✅ 已有 fuzzy: true |
| file-ops.yaml | 大文件 | phrase | ✅ 已有 fuzzy: true |
| docs-writer.yaml | 写文档 | phrase | ✅ 已有 fuzzy: true |
| docs-writer.yaml | 教程 | phrase | ✅ 已有 fuzzy: true |

## 无法直接加的 Phase B 项（嵌在 regex 中，fuzzy 被忽略）
| 文件 | pattern | 所在 regex | 建议 |
|------|---------|------------|------|
| ui-designer.yaml | 响应式 | (组件\|响应式\|...) | 如需 fuzzy → 拆分为独立 phrase 规则 |
| ui-designer.yaml | 美化 | (美观\|美化) | 同上 |
| ui-designer.yaml | 设计稿 | (界面\|设计.*界面\|设计稿) | 同上 |
| ui-designer.yaml | 动效 | (动效\|动画) | 同上 |
| programmer.yaml | 编码 | (编码\|编程\|写.*代码) | 同上 |
| error-analyst.yaml | 故障 | (错误\|故障\|异常\|...) | 同上 |
| error-analyst.yaml | 事故分析 | (复盘\|事故分析) | 同上 |

## 结论
Phase B 已无纯 YAML 可修改项（5 条 phrase 规则已开启，8 条 regex 规则无法加 fuzzy）。如需继续推进 fuzzy 优化，需将上述 8 条从复合 regex 中拆分为独立 phrase 规则 + 保持原 regex 完整。建议有明确误匹配案例时再拆，不做预优化。

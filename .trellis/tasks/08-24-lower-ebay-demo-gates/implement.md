# 实施计划：降低 eBay Demo 选品门槛并补充入库样例

## 1. 修改与静态检查

- [x] 将 UK Skill 所有月销门槛改为 `> 1` / `<= 1`，产品点改为 `> 0.0005` / `<= 0.0005`。
- [x] 对 US Skill 做完全对应的修改，保持美元、US site 和汇率 7 不变。
- [x] 扫描两份 Skill，确认旧 `月销 > 5`、`月销 <= 5`、`0.005` 不再出现在门槛语义中。
- [x] 更新实验手册对流程、边界、公式价值的解释。
- [x] 在实验手册加入 4 条实际执行的完整入库提示词（UK/US 各两条）。
- [x] 运行 `git diff --check` 和定向一致性扫描。

## 2. 发布前基线

- [x] 检查 AWS 身份、区域、Part 1 Runtime 配置与模型认证环境。
- [x] 查询并记录两张业务表的调用前计数与 UTC 基线时间。
- [x] 确认工作区只有本任务改动及既有无关 Trellis 目录；后者未纳入本任务。

## 3. 发布 Runtime

- [x] 在 `part1_data_ingestion` 运行 `bash scripts/deploy.sh`。
- [x] 确认 Runtime 发布成功并可调用。
- [x] 完整部署幂等更新既有栈/Lambda、复用 Browser/Profile，未手工覆盖凭证或端点。

## 4. 多轮真实入库

- [x] 第一批并行运行 UK desk organizer 与 US drawer organizer；UK 完成 2 个，US 首次响应流中断。
- [x] 第一批结束后并行运行 UK cable organizer 与 US travel organizer；UK 完成 2 个，US 在上下文超限前写入 1 条。
- [x] US drawer 首次无入库，按设计补跑同一明确要求；在上下文超限前写入 1 条排除。
- [x] 把每次输出、耗时、失败及实际入库信息脱敏记录到任务 research 目录。

## 5. 数据与文档验收

- [x] 查询两张表调用后计数：判定 76→82（+6），候选 0→0（+0）。
- [x] 查询基线时间后的记录，核对 UK/US site、判定原因和产品点。
- [x] 根据真实结果在实验手册提示词段补充执行说明，候选数如实为 0。
- [x] 复跑文档/Skill 一致性扫描与 `git diff --check`。
- [x] 汇报部署、四次调用、一次补跑及实际入库结果；明确外部抓取/响应流/上下文超限失败。

## 验证命令

```bash
rg -n '月销 > 5|月销 = 5|月销≤5|0\.005' \
  part1_data_ingestion/agent/skills/product-selection-ebay{,-us}/SKILL.md 实验手册.md

git diff --check

cd part1_data_ingestion && bash scripts/deploy.sh

cd part1_data_ingestion/agent
agentcore invoke '{"prompt": "..."}'
```

数仓验证沿用实验手册中的 Data API 查询方式，实际执行时先从 CloudFormation 输出读取 ARN，不把 ARN 写入任务文档。

# 真实部署与入库执行摘要

- UTC 基线：2026-08-24T06:06:37Z。
- 部署：`part1_data_ingestion/scripts/deploy.sh` 退出码 0；Runtime direct-code 发布成功。
- 第一批并行：
  - UK desk organizer：退出码 0，141 秒；处理 2 个，新增 `排除` 1、`PASS` 1。
  - US drawer organizer：906 秒后响应流 `IncompleteRead`，无入库；按设计补跑。
- 第二批并行：
  - UK cable organizer：退出码 0，65 秒；两个详情页空白，新增 `补采` 2。
  - US travel organizer：退出码 0，103 秒，但模型上下文超限；超限前新增 `补采` 1。
- 补跑：US drawer organizer，退出码 0，127 秒，但模型上下文超限；超限前新增 `排除` 1。
- 最终计数：`selection_verdicts` 76→82（+6），`product_candidates` 0→0（+0）。
- 失败如实保留：外部详情页空白、响应流中断、模型上下文超限；未删除数据，未虚构候选。

本目录日志已移除 Runtime ARN、Session ID、账号、控制台链接、日志组标识及端点/凭证。

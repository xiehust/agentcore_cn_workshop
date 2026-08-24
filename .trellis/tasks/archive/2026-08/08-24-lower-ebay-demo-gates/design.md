# 技术设计：降低 eBay Demo 选品门槛并补充入库样例

## 1. 变更边界

本次不新增配置项或代码分支。门槛仍由 Skill 作为业务 SOP 管理，只修改：

- `agent/skills/product-selection-ebay/SKILL.md`：UK 月销与产品点门槛及所有镜像文案；
- `agent/skills/product-selection-ebay-us/SKILL.md`：US 对应内容；
- `实验手册.md`：实验解释、公式含义与实际入库提示词；
- 如一致性扫描发现 Part 1 README 中出现旧门槛，再同步更新该文档。

亚马逊 Skill `product-selection` 保持生产阈值不变。

## 2. 一致性契约

每份 eBay Skill 的同一个门槛分散出现在摘要、核心目标、流程图、R1 表、边界说明、公式注释、字段表和伪代码中。修改必须覆盖全部镜像：

- 月销：`> 1`，边界为 `<= 1 -> PASS`；
- 产品点：`> 0.0005`，边界为 `<= 0.0005 -> 排除`；
- UK 汇率仍为 9，US 汇率仍为 7；当前价格仍分别为 `> £5` / `> $5`。

通过定向文本扫描同时检查新值存在和旧值消失，避免模型读到互相冲突的规则。

## 3. 发布与执行

Skill 随 direct-code Runtime 包发布。修改后运行 Part 1 `scripts/deploy.sh`，沿用既有幂等部署链；发布完成后以 `.bedrock_agentcore.yaml` 对应 Runtime 为调用目标。

真实入库调用采用 CLI，四个互不相同的关键词降低重复页面影响：

1. UK：desk organizer；
2. US：drawer organizer；
3. UK：cable organizer；
4. US：travel organizer。

每条提示都明确站点、关键词、前 2 个非广告、一口价、全新、按 SOP 判定并入库。两批各并行一条 UK 和 US，避免同时开启过多 Browser/Code Interpreter 会话。

## 4. 数据验证

调用前记录 `selection_verdicts` 与 `product_candidates` 总数及当前时间。调用后：

- 比较两表计数增量；
- 查询基线时间之后的最新记录，核对 `site`、`verdict`、`reason`、`product_score`；
- 保存四次 CLI 输出到任务 research 目录，仅用于实施证据，不写入用户凭证或端点；
- 文档只写可公开复用的提示词和结果摘要，不写 ARN、账号或真实端点。

## 5. 失败与降级

- 部署失败：停止调用，修复后重新发布；不以本地 Skill 冒充云端生效。
- 单次外部抓取失败：保留 Agent 按 SOP 写入的 `补采` 结果并继续下一次。
- CLI 调用超时或无入库：核对 Runtime/Browser 日志，至多补跑一条同站点不同关键词请求；不删除已有数据。
- 候选数为 0 不等于任务失败；只要四次调用完成且判定记录按 SOP 落库，即可如实验收。

## 6. 回滚

代码回滚只需恢复两份 Skill 和文档的旧阈值后重新发布 Runtime。真实演示写入的数据不自动删除，以免误删既有数据；如用户另行要求清理，再按明确时间窗/记录 ID 执行。

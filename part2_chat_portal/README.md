# Part 2：Web Chat 分析门户（预留，本期不实施）

通过 Web 聊天界面，用 Agent 分析 Part 1 入库的选品数据。

## 规划架构

```
用户浏览器
   │  ① 登录（OAuth2 / Cognito）
   ▼
Web 门户（前端 + 轻量后端）
   │  ② 携带用户身份调用
   ▼
AgentCore Identity ──── 校验用户身份，为 Agent 换取访问下游资源的凭证
   │
   ▼
AgentCore Runtime（数据分析 Agent，Strands SDK）
   ├──▶ AgentCore Gateway ──▶ 数据查询 API（Lambda）──▶ Redshift Serverless
   │        （把 REST/Lambda 转成 MCP 工具，Agent 直接调用）
   └──▶ Code Interpreter：统计分析、图表生成（如选品漏斗、产品点分布）
```

## 计划用到的 AgentCore 组件

| 组件 | 用途 |
|------|------|
| **Runtime** | 托管分析 Agent，按用户 session 隔离 |
| **Gateway** | 把 Redshift 查询 API（Lambda）转换成 MCP 工具暴露给 Agent |
| **Identity** | 门户用户认证（inbound auth）+ Agent 访问 Gateway 的授权（outbound auth） |
| **Code Interpreter** | 对查询结果做统计分析、生成图表 |

## 待确认需求（下期沟通）

- [ ] 用户体系：企业 IdP（如 Entra ID / Okta）还是 Cognito 自建？
- [ ] 分析场景：固定报表（选品漏斗、类目分布）还是开放式即席分析？
- [ ] 前端形态：独立 Web 应用还是嵌入现有系统？
- [ ] 是否需要 AgentCore Memory 记住用户的分析偏好？

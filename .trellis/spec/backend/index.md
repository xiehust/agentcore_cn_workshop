# Backend Development Guidelines

> Best practices for backend development in this project.

---

## Overview

This directory contains guidelines for backend development. Fill in each file with your project's specific conventions.

---

## Guidelines Index

| Guide | Description | Status |
|-------|-------------|--------|
| [Directory Structure](./directory-structure.md) | Module organization and file layout | To fill |
| [数据库访问约定](./database-guidelines.md) | 数仓访问路径选型（VPC Lambda / Data API）、Secrets 端点归属、超时链、参数化与 DDL | ✅ 已填 |
| [Error Handling](./error-handling.md) | Error types, handling strategies | To fill |
| [Quality Guidelines](./quality-guidelines.md) | Code standards, forbidden patterns | To fill |
| [Logging Guidelines](./logging-guidelines.md) | Structured logging, log levels | To fill |
| [数据摄入任务管理模块](./ingestion-tasks-module.md) | 任务编排跨层契约：REST/SSE 事件/任务三表/调度/Live View presign | ✅ 已填 |
| [分析会话与历史模块](./chat-sessions-module.md) | 对话持久化跨层契约：/chat 路由/chat 两表/Runtime 回灌与落库/归属模型 | ✅ 已填 |

---

## How to Fill These Guidelines

For each guideline file:

1. Document your project's **actual conventions** (not ideals)
2. Include **code examples** from your codebase
3. List **forbidden patterns** and why
4. Add **common mistakes** your team has made

The goal is to help AI assistants and new team members understand how YOUR project works.

---

**Language**: All documentation should be written in **English**.

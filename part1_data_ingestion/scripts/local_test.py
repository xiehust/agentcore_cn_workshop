"""本地测试脚本：不部署 Runtime，直接在本机跑 Agent 逻辑。

用途：验证 Skill 加载、工具编排、浏览器/沙箱调用是否正常。
注意：AgentCore Browser 与 Code Interpreter 是云端服务，本地运行也会调用
      AWS（需要凭证与相应 IAM 权限），只是 Agent 本体跑在本机。
      入库同理走云端的写库 Lambda（本机凭证需 lambda:InvokeFunction）；
      函数名默认 agentcore-workshop-writer，与 deploy.sh 一致，改名了就
      export WRITER_LAMBDA_NAME=<函数名>。

运行:
    cd part1_data_ingestion/agent
    python ../scripts/local_test.py "从亚马逊英国站搜索 desk organizer，选品判定第 1 个商品"
"""

import asyncio
import os
import sys

# 确保能 import agent 目录下的模块（main.py、tools/）
AGENT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "agent")
sys.path.insert(0, os.path.abspath(AGENT_DIR))
os.chdir(os.path.abspath(AGENT_DIR))

from main import create_agent  # noqa: E402


async def run(prompt: str):
    agent = create_agent()
    print(f"任务: {prompt}\n{'=' * 60}")
    async for event in agent.stream_async(prompt):
        # 流式打印文本增量
        if "data" in event:
            print(event["data"], end="", flush=True)
    print(f"\n{'=' * 60}\n完成")


if __name__ == "__main__":
    task = (
        sys.argv[1]
        if len(sys.argv) > 1
        else "从亚马逊英国站搜索 desk organizer，对第 1 个非广告商品做选品判定并入库"
    )
    asyncio.run(run(task))

import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";

// 浏览器不能直接跨域调 AgentCore Runtime 的数据面端点（无 CORS 头），
// 由 dev server 代理转发：前端请求 /api/invoke，这里改写成
// /runtimes/<url编码的RuntimeARN>/invocations?qualifier=DEFAULT。
// Authorization（Keycloak 用户 token）与 session 头原样透传。
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), "");
  const region = env.VITE_AWS_REGION || "us-west-2";
  const runtimeArn = env.VITE_RUNTIME_ARN || "";
  return {
    plugins: [react()],
    server: {
      host: true, // 监听所有网卡，支持从转发端口 / 机器 IP 访问
      port: 5173,
      proxy: {
        "/api/invoke": {
          target: `https://bedrock-agentcore.${region}.amazonaws.com`,
          changeOrigin: true,
          rewrite: () =>
            `/runtimes/${encodeURIComponent(runtimeArn)}/invocations?qualifier=DEFAULT`,
        },
      },
    },
  };
});

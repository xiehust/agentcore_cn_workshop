import { fileURLToPath } from "node:url";
import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";
import { viteStaticCopy } from "vite-plugin-static-copy";

// bedrock-agentcore 的 BrowserLiveView 里写的是 `import dcv from "dcv"` /
// `import { DCVViewer } from "dcv-ui"`，这两个裸 specifier 没有对应的 npm 包，
// 指的是包内 vendored 的 Amazon DCV Web SDK，必须用 alias 指过去。
// 相对路径（相对项目根）给 static copy 用，绝对路径给 alias 用。
const DCV_SDK =
  "node_modules/bedrock-agentcore/dist/src/tools/browser/live-view/nice-dcv-web-client-sdk";
const dcvPath = (sub: string) => fileURLToPath(new URL(`${DCV_SDK}/${sub}`, import.meta.url));

// 浏览器不能直接跨域调 AgentCore Runtime 的数据面端点（无 CORS 头），
// 由 dev server 代理转发：前端请求 /api/invoke，这里改写成
// /runtimes/<url编码的RuntimeARN>/invocations?qualifier=DEFAULT。
// Authorization（Keycloak 用户 token）与 session 头原样透传。
// 任务管理的 Tasks HTTP API 自带 CORS，不需要代理，前端直连 VITE_TASKS_API_URL。
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), "");
  const region = env.VITE_AWS_REGION || "us-west-2";
  const runtimeArn = env.VITE_RUNTIME_ARN || "";
  // 域名后缀按分区推导（中国区 .amazonaws.com.cn），区域来自 .env.local 单点配置
  const urlSuffix = region.startsWith("cn-") ? "amazonaws.com.cn" : "amazonaws.com";
  return {
    plugins: [
      react(),
      // DCV 运行时按 baseUrl="/nice-dcv-web-client-sdk/dcvjs-esm" 去 fetch 解码器
      // worker（dcv/*-worker.js），worker 又 importScripts 同级的 lib/*。这些不经
      // 打包，得原样摆到站点根下的对应路径。stripBase 把源目录前缀剥掉，只保留
      // dcvjs-esm/ 以下的层级（段数由 DCV_SDK 自身算出，路径改了不用手动改数字）。
      viteStaticCopy({
        targets: [
          {
            src: [`${DCV_SDK}/dcvjs-esm/dcv/**/*`, `${DCV_SDK}/dcvjs-esm/lib/**/*`],
            dest: "nice-dcv-web-client-sdk",
            rename: { stripBase: DCV_SDK.split("/").length },
          },
        ],
      }),
    ],
    resolve: {
      alias: {
        dcv: dcvPath("dcvjs-esm/dcv.js"),
        "dcv-ui": dcvPath("dcv-ui/dcv-ui.js"),
      },
      // dcv-ui 自带 react 引用，去重避免两份 React 实例导致 hooks 报错
      dedupe: ["react", "react-dom"],
    },
    server: {
      host: true, // 监听所有网卡，支持从转发端口 / 机器 IP 访问
      port: 5173,
      proxy: {
        "/api/invoke": {
          target: `https://bedrock-agentcore.${region}.${urlSuffix}`,
          changeOrigin: true,
          rewrite: () =>
            `/runtimes/${encodeURIComponent(runtimeArn)}/invocations?qualifier=DEFAULT`,
        },
      },
    },
  };
});

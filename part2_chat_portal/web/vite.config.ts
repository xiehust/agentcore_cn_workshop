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
  // 公网门户地址（03 部署脚本按栈输出写入 .env.local）：dev server 起来后直接
  // 打印，参与者不用回头翻部署日志找 URL
  const publicPortalUrl = env.VITE_PUBLIC_PORTAL_URL || "";
  // 域名后缀按分区推导（中国区 .amazonaws.com.cn），区域来自 .env.local 单点配置
  const urlSuffix = region.startsWith("cn-") ? "amazonaws.com.cn" : "amazonaws.com";
  // dev 与 preview（构建产物本地预览）共用同一份 Runtime 数据面代理
  const runtimeProxy = {
    "/api/invoke": {
      target: `https://bedrock-agentcore.${region}.${urlSuffix}`,
      changeOrigin: true,
      rewrite: () =>
        `/runtimes/${encodeURIComponent(runtimeArn)}/invocations?qualifier=DEFAULT`,
    },
  };

  // Tasks API 私有化代理（中央 Keycloak 形态，VITE_TASKS_API_URL=/api/tasks 时启用）：
  // 学员账号 execute-api 被 ICP recordal 限制时，公网 Tasks API 整体弃用，
  // 浏览器流量改走本门户同源——此 middleware 验签 JWT（与原 Tasks authorizer
  // 等价：中央 KC 的 JWKS + issuer/audience 校验）后，以实例角色 SigV4
  // lambda.invoke 调 tasks-api（构造 API GW v2 HTTP 事件，Lambda handler 零改动）。
  const tasksProxyEnabled = (env.VITE_TASKS_API_URL || "") === "/api/tasks" && env.VITE_KC_URL;
  const makeTasksProxy = () => {
    const issuer = `${env.VITE_KC_URL}/realms/${env.VITE_KC_REALM || "agentcore-workshop"}`;
    let jwksPromise: Promise<unknown> | null = null;
    const handler = async (
      req: import("node:http").IncomingMessage,
      res: import("node:http").ServerResponse,
    ) => {
      try {
        const { createRemoteJWKSet, jwtVerify } = await import("jose");
        const { LambdaClient, InvokeCommand } = await import("@aws-sdk/client-lambda");
        if (!jwksPromise) {
          jwksPromise = Promise.resolve(
            createRemoteJWKSet(new URL(`${issuer}/protocol/openid-connect/certs`)),
          );
        }
        const jwks = (await jwksPromise) as Parameters<typeof jwtVerify>[1];
        const auth = req.headers["authorization"] || "";
        const token = Array.isArray(auth) ? "" : auth.replace(/^Bearer\s+/i, "");
        if (!token) {
          res.statusCode = 401;
          res.end(JSON.stringify({ message: "missing bearer token" }));
          return;
        }
        let claims: Record<string, unknown>;
        try {
          const verified = await jwtVerify(token, jwks, {
            issuer,
            audience: "analysis-runtime",
          });
          claims = verified.payload as Record<string, unknown>;
        } catch {
          res.statusCode = 401;
          res.end(JSON.stringify({ message: "invalid token" }));
          return;
        }
        const chunks: Buffer[] = [];
        for await (const c of req) chunks.push(c as Buffer);
        const bodyRaw = Buffer.concat(chunks);
        // connect 挂载在 /api/tasks 下，req.url 已去前缀
        const [pathPart, queryPart] = (req.url || "/").split("?");
        const qs: Record<string, string> = {};
        new URLSearchParams(queryPart || "").forEach((v, k) => (qs[k] = v));
        const event = {
          version: "2.0",
          rawPath: pathPart || "/",
          rawQueryString: queryPart || "",
          headers: Object.fromEntries(
            Object.entries(req.headers).map(([k, v]) => [k, Array.isArray(v) ? v.join(",") : v]),
          ),
          queryStringParameters: Object.keys(qs).length ? qs : undefined,
          requestContext: {
            http: { method: req.method || "GET", path: pathPart || "/" },
            authorizer: { jwt: { claims } },
          },
          body: bodyRaw.length ? bodyRaw.toString("base64") : undefined,
          isBase64Encoded: bodyRaw.length > 0,
        };
        const lambda = new LambdaClient({ region });
        const out = await lambda.send(
          new InvokeCommand({
            FunctionName: "agentcore-workshop-tasks-api",
            Payload: Buffer.from(JSON.stringify(event)),
          }),
        );
        const payload = JSON.parse(Buffer.from(out.Payload ?? []).toString() || "{}");
        res.statusCode = payload.statusCode || 500;
        for (const [k, v] of Object.entries(payload.headers || {})) res.setHeader(k, String(v));
        const respBody = payload.isBase64Encoded
          ? Buffer.from(payload.body || "", "base64")
          : payload.body || "";
        res.end(respBody);
      } catch (e) {
        res.statusCode = 502;
        res.end(JSON.stringify({ message: `tasks proxy error: ${(e as Error).name}` }));
      }
    };
    return {
      name: "tasks-lambda-proxy",
      configureServer(server: import("vite").ViteDevServer) {
        server.middlewares.use("/api/tasks", handler);
      },
      configurePreviewServer(server: import("vite").PreviewServer) {
        server.middlewares.use("/api/tasks", handler);
      },
    };
  };

  return {
    plugins: [
      react(),
      ...(tasksProxyEnabled ? [makeTasksProxy()] : []),
      ...(publicPortalUrl
        ? [
            {
              name: "print-public-portal-url",
              configureServer(server: import("vite").ViteDevServer) {
                server.httpServer?.once("listening", () => {
                  // 稍等一拍，排在 Vite 自己的 Local/Network 行之后打印
                  setTimeout(() => {
                    server.config.logger.info(
                      `\n  ➜  公网门户地址: ${publicPortalUrl}\n     （浏览器直接打开即可登录；token 明文传输，仅限 workshop 演示）\n`,
                    );
                  }, 100);
                });
              },
            },
          ]
        : []),
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
      // LiveView 懒加载链上挂着 alias 进来的 dcv.js（几 MB 单文件，不走依赖
      // 预构建）。不预热的话，用户点开实况那一刻 dev server 才现场转换它，
      // 慢链路下 Suspense 会卡在「加载实况组件…」几十秒起步。启动即预热。
      warmup: { clientFiles: ["./src/tasks/LiveView.tsx"] },
      host: true, // 监听所有网卡，支持从转发端口 / 机器 IP 访问
      // 放行任意 Host 头：公网门户经 ALB HTTPS(8443) 访问时 Host 是 ALB 域名，
      // Vite 6 默认只认 localhost（Host 头校验，防 DNS rebinding）。dev server
      // 本身不含敏感态，PKCE 登录仍由 Keycloak 保护，放行是安全的
      allowedHosts: true,
      port: 5173,
      proxy: runtimeProxy,
    },
    // vite preview（构建产物）与 dev server 行为对齐：同端口、同代理、放行 Host
    preview: {
      host: true,
      allowedHosts: true,
      port: 5173,
      proxy: runtimeProxy,
    },
  };
});

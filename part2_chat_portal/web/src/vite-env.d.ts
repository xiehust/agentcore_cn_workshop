/// <reference types="vite/client" />

// deploy.sh 生成的 web/.env.local 里的变量（本文件只声明类型，不含真实值）。
interface ImportMetaEnv {
  readonly VITE_KC_URL: string;
  readonly VITE_KC_REALM: string;
  readonly VITE_KC_CLIENT_ID: string;
  readonly VITE_AWS_REGION?: string;
  readonly VITE_RUNTIME_ARN?: string;
  /** 任务管理 Tasks HTTP API 的 base URL（如 https://<api-id>.execute-api.<region>.amazonaws.com） */
  readonly VITE_TASKS_API_URL?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}

// bedrock-agentcore 的 BrowserLiveView 里 `import dcv from "dcv"`，
// vendored 的 DCV Web SDK 不带 .d.ts（vite.config.ts 用 alias 指到包内实际文件）。
declare module "dcv";
declare module "dcv-ui";

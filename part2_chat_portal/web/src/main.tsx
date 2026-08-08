import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import App from "./App";
import { initAuth } from "./auth";
import "@fontsource/ibm-plex-sans/400.css";
import "@fontsource/ibm-plex-sans/500.css";
import "@fontsource/ibm-plex-sans/600.css";
import "@fontsource/ibm-plex-mono/400.css";
import "@fontsource/ibm-plex-mono/500.css";
import "./styles.css";

// 先完成 Keycloak 登录（未登录会整页跳转到登录页），再渲染应用
initAuth().then((authenticated) => {
  if (!authenticated) return; // 正在跳转登录页
  createRoot(document.getElementById("root")!).render(
    <StrictMode>
      <App />
    </StrictMode>,
  );
});

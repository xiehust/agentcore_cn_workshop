// Keycloak OIDC 登录（Authorization Code + PKCE）。
// access token 里带 audience=analysis-runtime（realm 里配置的 mapper），
// AgentCore Runtime 的 JWT authorizer 据此放行。
import Keycloak from "keycloak-js";

export const keycloak = new Keycloak({
  url: import.meta.env.VITE_KC_URL,
  realm: import.meta.env.VITE_KC_REALM,
  clientId: import.meta.env.VITE_KC_CLIENT_ID,
});

export async function initAuth(): Promise<boolean> {
  return keycloak.init({
    onLoad: "login-required",
    // PKCE S256 依赖 crypto.subtle，浏览器只在安全上下文（HTTPS/localhost）
    // 提供；公网 HTTP 门户（NLB 非标准端口，中国区免备案形态）退回普通
    // 授权码流程（realm 侧未强制 S256）。HTTP 下 token 明文传输，仅限
    // workshop 演示，生产请走 HTTPS。
    pkceMethod: window.isSecureContext ? "S256" : false,
    checkLoginIframe: false,
  });
}

/** 取当前有效的 access token；快过期时静默刷新，刷新失败则重新登录。 */
export async function getToken(): Promise<string> {
  try {
    await keycloak.updateToken(30);
  } catch {
    await keycloak.login();
  }
  return keycloak.token!;
}

export function displayName(): string {
  const p = keycloak.tokenParsed as Record<string, string> | undefined;
  return p?.name || p?.preferred_username || "用户";
}

export function logout() {
  keycloak.logout({ redirectUri: window.location.origin });
}

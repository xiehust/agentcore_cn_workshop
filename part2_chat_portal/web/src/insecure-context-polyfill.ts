// 公网 HTTP 门户（非安全上下文，中国区免备案形态）下浏览器不提供
// crypto.randomUUID（session id 生成用到），用 getRandomValues（不受安全
// 上下文限制）垫一个 UUIDv4。本模块必须是应用入口的第一个 import。
if (!("randomUUID" in crypto)) {
  (crypto as Crypto & { randomUUID: () => string }).randomUUID = () => {
    const b = crypto.getRandomValues(new Uint8Array(16));
    b[6] = (b[6] & 0x0f) | 0x40;
    b[8] = (b[8] & 0x3f) | 0x80;
    const h = Array.from(b, (x) => x.toString(16).padStart(2, "0")).join("");
    return `${h.slice(0, 8)}-${h.slice(8, 12)}-${h.slice(12, 16)}-${h.slice(16, 20)}-${h.slice(20)}`;
  };
}

export {};

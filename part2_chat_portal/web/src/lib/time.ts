// 时间戳解析与显示辅助（任务管理与分析会话共用同一套解析规则）。

/** 时间戳后端用 to_char 输出带 Z 的 UTC ISO8601；没带时区标记的值一律按 UTC 兜底解析。 */
export function parseTs(ts: string | null | undefined): Date | null {
  if (!ts) return null;
  const normalized = /[zZ]|[+-]\d\d:?\d\d$/.test(ts) ? ts : ts.replace(" ", "T") + "Z";
  const d = new Date(normalized);
  return Number.isNaN(d.getTime()) ? null : d;
}

/** 相对时间：历史会话列表只要「刚刚 / 12 分钟前 / 3 天前」这种粒度。 */
export function formatRelative(ts: string | null | undefined): string {
  const d = parseTs(ts);
  if (!d) return "—";
  const diff = (Date.now() - d.getTime()) / 1000;
  if (diff < 60) return "刚刚";
  if (diff < 3600) return `${Math.floor(diff / 60)} 分钟前`;
  if (diff < 86400) return `${Math.floor(diff / 3600)} 小时前`;
  if (diff < 86400 * 7) return `${Math.floor(diff / 86400)} 天前`;
  return d.toLocaleDateString("zh-CN");
}

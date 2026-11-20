// Browser Live View：看云端浏览器正在抓什么。
// presigned URL 由 tasks-api 用 SigV4 签好返回（凭证不下发浏览器），最长 300 秒，
// 到期后换一个 URL 并重挂 BrowserLiveView（DCV 连接要重建，靠换 key 实现）。
// 组件本体由 RunDetail 用 React.lazy 懒加载：DCV Web SDK 有 2MB+。
import { useCallback, useEffect, useRef, useState } from "react";
import { BrowserLiveView } from "bedrock-agentcore/browser/live-view";
import { ArrowsClockwiseIcon, MonitorPlayIcon, WarningCircleIcon } from "@phosphor-icons/react";
import { ApiError, getLiveView, type LiveViewInfo } from "./api";

/** 剩余不足这么多秒就提示该刷新了（300 秒有效期 → 约第 270 秒开始提示）。 */
const WARN_SECONDS = 30;

/** DCV 认证 + 首帧超过这么多秒还没出画面就判连接失败（正常 3~5 秒内出图）。 */
const CONNECT_TIMEOUT_SECONDS = 25;

function mmss(total: number): string {
  const m = Math.floor(total / 60);
  const s = total % 60;
  return `${m}:${String(s).padStart(2, "0")}`;
}

export default function LiveView({ runId }: { runId: string }) {
  const [info, setInfo] = useState<LiveViewInfo | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [ended, setEnded] = useState(false);
  const [left, setLeft] = useState(0);
  // 自增当 BrowserLiveView 的 key：URL 一换就整块重挂，避免复用旧 DCV 连接
  const [epoch, setEpoch] = useState(0);
  // BrowserLiveView 在 DCV 认证失败时永远返回 null 且不抛错，占位层会无限
  // 停在「连接云端浏览器…」。用看门狗兜底：超时还没渲染出 canvas/video 就明说。
  const [stalled, setStalled] = useState(false);
  const stageRef = useRef<HTMLDivElement | null>(null);

  const open = useCallback(async () => {
    setLoading(true);
    setError(null);
    setStalled(false);
    try {
      const d = await getLiveView(runId);
      setInfo(d);
      setLeft(d.expires_in);
      setEpoch((e) => e + 1);
      setEnded(false);
    } catch (e) {
      if (e instanceof ApiError && (e.status === 409 || e.status === 410)) {
        setInfo(null);
        setEnded(e.status === 410);
        setError(
          e.status === 410 ? "浏览器会话已结束，实况不可用。" : "浏览器会话还没就绪，稍后再试。",
        );
      } else {
        setError((e as Error).message);
      }
    } finally {
      setLoading(false);
    }
  }, [runId]);

  useEffect(() => {
    if (!info) return;
    const id = setInterval(() => setLeft((s) => Math.max(0, s - 1)), 1000);
    return () => clearInterval(id);
  }, [info]);

  // 连接看门狗：挂载 CONNECT_TIMEOUT_SECONDS 后检查 DCV 是否真的渲染出了
  // 画面元素（canvas/video）。没有就标记 stalled，露出错误提示替代无限转圈。
  useEffect(() => {
    if (!info) return;
    setStalled(false);
    const id = setTimeout(() => {
      const stage = stageRef.current;
      const hasFrame = !!stage?.querySelector("canvas, video");
      if (!hasFrame) setStalled(true);
    }, CONNECT_TIMEOUT_SECONDS * 1000);
    return () => clearTimeout(id);
  }, [info, epoch]);

  const expired = info !== null && left === 0;
  const expiring = info !== null && left > 0 && left <= WARN_SECONDS;

  if (ended) {
    return (
      <div className="live-hint">
        <WarningCircleIcon size={16} />
        浏览器会话已结束，实况不可用。运行结束后可看下方的统计与入库明细。
      </div>
    );
  }

  if (!info) {
    return (
      <div className="live-hint">
        <button className="accent-btn" disabled={loading} onClick={() => void open()}>
          <MonitorPlayIcon size={15} weight="bold" />
          {loading ? "获取链接中…" : "打开实况"}
        </button>
        <span className="cell-muted">直播链接有效期 300 秒，过期可一键刷新重连。</span>
        {error && <span className="live-error">{error}</span>}
      </div>
    );
  }

  return (
    <>
      <div className="live-frame">
        {/* BrowserLiveView 在 DCV 认证完成前返回 null，认证期间露出下面这层提示 */}
        {!stalled && (
          <div className="live-placeholder thinking">
            连接云端浏览器<span className="dots" />
          </div>
        )}
        {stalled && !expired && (
          <div className="live-placeholder">
            <WarningCircleIcon size={16} />
            实况连接超时：直播流（wss）未能建立，可能被当前网络拦截。
            请点「刷新重连」重试，或换网络/走端口转发访问门户。
          </div>
        )}
        {!expired && !stalled && (
          <div className="live-stage" ref={stageRef}>
            <BrowserLiveView
              key={epoch}
              signedUrl={info.url}
              remoteWidth={info.width}
              remoteHeight={info.height}
            />
          </div>
        )}
        {expired && (
          <div className="live-placeholder">
            <WarningCircleIcon size={16} />
            直播链接已过期
          </div>
        )}
      </div>
      <div className="live-bar">
        <span className={expiring || expired ? "live-error mono" : "cell-muted mono"}>
          {expired ? "链接已过期" : `链接剩余 ${mmss(left)}`}
          {expiring && " · 建议刷新重连"}
        </span>
        <button className="ghost-btn tiny" disabled={loading} onClick={() => void open()}>
          <ArrowsClockwiseIcon size={13} />
          刷新重连
        </button>
      </div>
      {error && <div className="alert">{error}</div>}
    </>
  );
}

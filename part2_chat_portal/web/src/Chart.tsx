// 渲染 Agent 输出的 ```chart 代码块（约定的 JSON 图表 DSL）。
// 目前支持 type: "bar"（横向条形图，漏斗/分布/排名通用），纯 SVG 无依赖。
// 流式过程中 JSON 可能不完整，解析失败时先按普通代码块显示。

interface ChartDatum {
  label: string;
  value: number;
}
interface ChartSpec {
  type?: string;
  title?: string;
  data?: ChartDatum[];
}

// 台账配色：朱砂 / 松墨 / 赭石 / 黛青，循环使用
const PALETTE = [
  "#b23a22", "#2f6a4c", "#96771f", "#3d4a5c",
  "#8f2e1a", "#4a7d61", "#b08d2a", "#5a6b80",
];

export function tryParseChart(source: string): ChartSpec | null {
  try {
    const spec = JSON.parse(source) as ChartSpec;
    if (!Array.isArray(spec.data) || spec.data.length === 0) return null;
    if (spec.data.some((d) => typeof d.label !== "string" || typeof d.value !== "number"))
      return null;
    return spec;
  } catch {
    return null;
  }
}

export function Chart({ spec }: { spec: ChartSpec }) {
  const data = spec.data!;
  const max = Math.max(...data.map((d) => d.value), 1);
  const rowH = 34;
  const labelW = 130;
  const valueW = 76;
  const barMax = 320;
  const width = labelW + barMax + valueW;
  const height = data.length * rowH + 8;

  return (
    <div className="chart-card">
      {spec.title && <div className="chart-title">{spec.title}</div>}
      <svg
        viewBox={`0 0 ${width} ${height}`}
        width="100%"
        style={{ maxWidth: width }}
        role="img"
        aria-label={spec.title || "图表"}
      >
        {data.map((d, i) => {
          const barW = Math.max((d.value / max) * barMax, 2);
          const y = i * rowH + 6;
          return (
            <g key={i}>
              <text x={labelW - 10} y={y + 15} textAnchor="end" className="chart-label">
                {d.label.length > 9 ? d.label.slice(0, 9) + "…" : d.label}
              </text>
              <rect
                className="chart-bar"
                style={{ animationDelay: `${i * 0.07}s` }}
                x={labelW} y={y} width={barW} height={22} rx={2}
                fill={PALETTE[i % PALETTE.length]} opacity={0.92}
              />
              <text x={labelW + barW + 8} y={y + 15} className="chart-value">
                {Intl.NumberFormat("zh-CN").format(d.value)}
              </text>
            </g>
          );
        })}
      </svg>
    </div>
  );
}

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

// 暗色数据可视化色板：青柠主色 + 冷色系列，循环使用
const PALETTE = [
  "#c9f158", "#6cb2f2", "#4fd1b5", "#d9b36c",
  "#8fa1bd", "#e58fa2", "#a8d13d", "#5a8fd6",
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
                x={labelW} y={y} width={barW} height={22} rx={3}
                fill={PALETTE[i % PALETTE.length]} opacity={0.9}
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

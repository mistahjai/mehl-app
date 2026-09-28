import { useEffect, useMemo, useRef, useState } from "react";
import {
  AreaSeries,
  ColorType,
  createChart,
  type IChartApi,
  type ISeriesApi,
  type UTCTimestamp,
} from "lightweight-charts";

const RANGES: { key: string; label: string; days: number | null }[] = [
  { key: "ytd", label: "YTD", days: null },
  { key: "1y", label: "1Y", days: 365 },
  { key: "3y", label: "3Y", days: 3 * 365 },
  { key: "5y", label: "5Y", days: 5 * 365 },
  { key: "10y", label: "10Y", days: 10 * 365 },
  { key: "all", label: "All", days: null },
];

export interface ClosePoint {
  ts: string;
  close: number;
}

export function CloseChart({ points }: { points: ClosePoint[] }) {
  const containerRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const seriesRef = useRef<ISeriesApi<"Area"> | null>(null);
  const [range, setRange] = useState("all");

  const data = useMemo(() => {
    if (points.length === 0) return [];
    const lastTs = new Date(points[points.length - 1].ts);
    let cutoff: Date | null = null;
    if (range === "ytd") {
      cutoff = new Date(lastTs.getFullYear(), 0, 1);
    } else {
      const r = RANGES.find((x) => x.key === range);
      if (r?.days) cutoff = new Date(lastTs.getTime() - r.days * 86_400_000);
    }
    const filtered = cutoff ? points.filter((p) => new Date(p.ts) >= cutoff) : points;
    return filtered.map((p) => ({
      time: Math.floor(new Date(p.ts).getTime() / 1000) as UTCTimestamp,
      value: p.close,
    }));
  }, [points, range]);

  useEffect(() => {
    if (!containerRef.current) return;
    const chart = createChart(containerRef.current, {
      autoSize: true,
      layout: {
        background: { type: ColorType.Solid, color: "#0b1220" },
        textColor: "#94a3b8",
      },
      grid: {
        vertLines: { color: "#1e293b" },
        horzLines: { color: "#1e293b" },
      },
    });
    chartRef.current = chart;
    return () => {
      chart.remove();
      chartRef.current = null;
      seriesRef.current = null;
    };
  }, []);

  useEffect(() => {
    const chart = chartRef.current;
    if (!chart) return;
    if (seriesRef.current) {
      chart.removeSeries(seriesRef.current);
      seriesRef.current = null;
    }
    if (data.length === 0) return;
    const series = chart.addSeries(AreaSeries, {
      lineColor: "#2563eb",
      topColor: "rgba(37, 99, 235, 0.35)",
      bottomColor: "rgba(37, 99, 235, 0.02)",
      lineWidth: 2,
    });
    series.setData(data);
    chart.timeScale().fitContent();
    seriesRef.current = series;
  }, [data]);

  return (
    <div>
      <div className="range-row">
        {RANGES.map((r) => (
          <button
            key={r.key}
            type="button"
            className={range === r.key ? "active" : ""}
            onClick={() => setRange(r.key)}
          >
            {r.label}
          </button>
        ))}
      </div>
      <div ref={containerRef} style={{ height: 420 }} />
    </div>
  );
}

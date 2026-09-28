import { useEffect, useRef } from "react";
import {
  AreaSeries,
  ColorType,
  LineSeries,
  createChart,
  type IChartApi,
  type ISeriesApi,
  type UTCTimestamp,
} from "lightweight-charts";
import type { EquityCurvePoint } from "../api/client";

function toTime(date: string): UTCTimestamp {
  return Math.floor(new Date(date).getTime() / 1000) as UTCTimestamp;
}

export function EquityCurveChart({ points }: { points: EquityCurvePoint[] }) {
  const containerRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const valueSeriesRef = useRef<ISeriesApi<"Area"> | null>(null);
  const investedSeriesRef = useRef<ISeriesApi<"Line"> | null>(null);

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
    valueSeriesRef.current = chart.addSeries(AreaSeries, {
      lineColor: "#2563eb",
      topColor: "rgba(37, 99, 235, 0.3)",
      bottomColor: "rgba(37, 99, 235, 0.02)",
    });
    investedSeriesRef.current = chart.addSeries(LineSeries, {
      color: "#f59e0b",
      lineWidth: 1,
      lineStyle: 2,
    });
    return () => {
      chart.remove();
      chartRef.current = null;
      valueSeriesRef.current = null;
      investedSeriesRef.current = null;
    };
  }, []);

  useEffect(() => {
    if (!chartRef.current || !valueSeriesRef.current || !investedSeriesRef.current) return;
    valueSeriesRef.current.setData(
      points.map((p) => ({ time: toTime(p.date), value: p.value })),
    );
    investedSeriesRef.current.setData(
      points.map((p) => ({ time: toTime(p.date), value: p.invested })),
    );
    chartRef.current.timeScale().fitContent();
  }, [points]);

  return <div ref={containerRef} className="chart-container" />;
}

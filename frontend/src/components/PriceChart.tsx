import { useEffect, useRef, useState } from "react";
import {
  CandlestickSeries,
  ColorType,
  createChart,
  type CandlestickData,
  type IChartApi,
  type ISeriesApi,
  type UTCTimestamp,
} from "lightweight-charts";
import { fetchOhlcv, type OhlcvRow } from "../api/client";

function toCandle(row: OhlcvRow): CandlestickData<UTCTimestamp> {
  return {
    time: Math.floor(new Date(row.ts).getTime() / 1000) as UTCTimestamp,
    open: row.open,
    high: row.high,
    low: row.low,
    close: row.close,
  };
}

export function PriceChart({ symbol }: { symbol: string }) {
  const containerRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const seriesRef = useRef<ISeriesApi<"Candlestick"> | null>(null);
  const [error, setError] = useState<string | null>(null);

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
    let cancelled = false;
    setError(null);
    fetchOhlcv(symbol)
      .then((rows) => {
        if (cancelled || !chartRef.current) return;
        const chart = chartRef.current;
        if (seriesRef.current) {
          chart.removeSeries(seriesRef.current);
          seriesRef.current = null;
        }
        const series = chart.addSeries(CandlestickSeries);
        series.setData(rows.map(toCandle));
        chart.timeScale().fitContent();
        seriesRef.current = series;
      })
      .catch((e: unknown) => {
        if (!cancelled) setError(e instanceof Error ? e.message : String(e));
      });
    return () => {
      cancelled = true;
    };
  }, [symbol]);

  return (
    <div>
      {error && <p className="error-text">Failed to load {symbol}: {error}</p>}
      <div ref={containerRef} style={{ height: 420 }} />
    </div>
  );
}

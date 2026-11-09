import React, { useEffect, useRef, useState } from 'react';
import {
  createChart,
  type IChartApi,
  type ISeriesApi,
  CandlestickSeries,
  HistogramSeries,
  ColorType,
  CrosshairMode,
} from 'lightweight-charts';
import { RefreshCw } from 'lucide-react';
import { api } from '../services/api';
import { wsService } from '../services/websocket';
import type { OHLCVCandle } from '../types';

interface ChartProps {
  symbol: string;
  timeframe: string;
  onSymbolChange: (s: string) => void;
  onTimeframeChange: (tf: string) => void;
}

const SUPPORTED_SYMBOLS = ['BTC/USDT', 'ETH/USDT', 'SOL/USDT', 'BNB/USDT'];
const TIMEFRAMES = ['1m', '5m', '15m', '1h', '4h', '1d'];

export const TradingViewChart: React.FC<ChartProps> = ({
  symbol,
  timeframe,
  onSymbolChange,
  onTimeframeChange,
}) => {
  const chartContainerRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const candleSeriesRef = useRef<ISeriesApi<'Candlestick'> | null>(null);
  const volumeSeriesRef = useRef<ISeriesApi<'Histogram'> | null>(null);

  const [loading, setLoading] = useState<boolean>(true);
  const [lastPrice, setLastPrice] = useState<number | null>(null);

  // Initialize and load candles
  useEffect(() => {
    let isMounted = true;
    if (!chartContainerRef.current) return;

    // Create chart
    const container = chartContainerRef.current;
    const initialWidth = container.clientWidth > 0 ? container.clientWidth : 800;

    let chart: IChartApi;
    let candleSeries: ISeriesApi<'Candlestick'>;
    let volumeSeries: ISeriesApi<'Histogram'>;

    try {
      chart = createChart(container, {
        width: initialWidth,
        height: 420,
        layout: {
          background: { type: ColorType.Solid, color: '#111622' },
          textColor: '#94a3b8',
        },
        grid: {
          vertLines: { color: '#1e2638' },
          horzLines: { color: '#1e2638' },
        },
        crosshair: {
          mode: CrosshairMode.Normal,
        },
        rightPriceScale: {
          borderColor: '#232c40',
          scaleMargins: {
            top: 0.1,
            bottom: 0.25,
          },
        },
        timeScale: {
          borderColor: '#232c40',
          timeVisible: true,
          secondsVisible: false,
        },
      });

      candleSeries = chart.addSeries(CandlestickSeries, {
        upColor: '#00c087',
        downColor: '#f6465d',
        borderVisible: false,
        wickUpColor: '#00c087',
        wickDownColor: '#f6465d',
      });

      volumeSeries = chart.addSeries(HistogramSeries, {
        color: '#3b82f6',
        priceFormat: {
          type: 'volume',
        },
        priceScaleId: 'volume',
      });

      try {
        chart.priceScale('volume')?.applyOptions({
          scaleMargins: {
            top: 0.8,
            bottom: 0,
          },
        });
      } catch {
        // Price scale customization optional
      }

      chartRef.current = chart;
      candleSeriesRef.current = candleSeries;
      volumeSeriesRef.current = volumeSeries;
    } catch (chartInitErr) {
      console.error('Failed to initialize TradingView chart:', chartInitErr);
      setLoading(false);
      return;
    }

    // Fetch initial candles with periodic 5s background sync for live prices
    setLoading(true);
    let isFirstLoad = true;

    const fetchCandles = () => {
      api
        .getCandles(symbol, timeframe, 150)
        .then((data: OHLCVCandle[]) => {
          if (!isMounted) return;
          if (data.length > 0) {
            const sorted = [...data].sort((a, b) => a.time - b.time);
            const candleData = sorted.map((c) => ({
              time: c.time as any,
              open: c.open,
              high: c.high,
              low: c.low,
              close: c.close,
            }));
            const volumeData = sorted.map((c) => ({
              time: c.time as any,
              value: c.volume,
              color: c.close >= c.open ? 'rgba(0, 192, 135, 0.3)' : 'rgba(246, 70, 93, 0.3)',
            }));

            if (isFirstLoad) {
              candleSeries.setData(candleData);
              volumeSeries.setData(volumeData);
              chart.timeScale().fitContent();
              isFirstLoad = false;
            } else {
              // Update last 2 candles incrementally without resetting user zoom
              for (let i = Math.max(0, candleData.length - 2); i < candleData.length; i++) {
                candleSeries.update(candleData[i]);
                volumeSeries.update(volumeData[i]);
              }
            }
            const latest = sorted[sorted.length - 1];
            setLastPrice(latest.close);
          }
          setLoading(false);
        })
        .catch((err) => {
          console.error('Failed to fetch candles:', err);
          setLoading(false);
        });
    };

    fetchCandles();
    const pollInterval = setInterval(fetchCandles, 5000);

    // Handle container resize
    const handleResize = () => {
      if (chartContainerRef.current && chartRef.current) {
        chartRef.current.applyOptions({
          width: chartContainerRef.current.clientWidth,
        });
      }
    };
    window.addEventListener('resize', handleResize);

    return () => {
      isMounted = false;
      clearInterval(pollInterval);
      window.removeEventListener('resize', handleResize);
      chart.remove();
      chartRef.current = null;
    };
  }, [symbol, timeframe]);

  // Subscribe to real-time websocket updates
  useEffect(() => {
    const handleCandleUpdate = (msg: any) => {
      if (msg.data?.symbol === symbol && candleSeriesRef.current && volumeSeriesRef.current) {
        const c = msg.data;
        const timeSec = typeof c.time === 'number' ? c.time : Math.floor(new Date(c.timestamp || c.time).getTime() / 1000);
        if (!isNaN(timeSec)) {
          candleSeriesRef.current.update({
            time: timeSec as any,
            open: Number(c.open),
            high: Number(c.high),
            low: Number(c.low),
            close: Number(c.close),
          });
          volumeSeriesRef.current.update({
            time: timeSec as any,
            value: Number(c.volume),
            color: Number(c.close) >= Number(c.open) ? 'rgba(0, 192, 135, 0.3)' : 'rgba(246, 70, 93, 0.3)',
          });
        }
        if (c.close) {
          setLastPrice(Number(c.close));
        }
      }
    };

    const unsubCandle = wsService.subscribe('candle', handleCandleUpdate);
    const unsubKline = wsService.subscribe('kline', handleCandleUpdate);

    return () => {
      unsubCandle();
      unsubKline();
    };
  }, [symbol]);

  return (
    <div className="bg-darkpanel border border-darkborder rounded-xl p-4 flex flex-col shadow-lg">
      {/* Chart Toolbar */}
      <div className="flex flex-wrap items-center justify-between pb-3 mb-2 border-b border-darkborder gap-2">
        <div className="flex items-center space-x-4">
          {/* Symbol Selector */}
          <div className="flex items-center space-x-1 bg-darkcard p-1 rounded-lg border border-darkborder">
            {SUPPORTED_SYMBOLS.map((s) => (
              <button
                key={s}
                onClick={() => onSymbolChange(s)}
                className={`px-2.5 py-1 text-xs font-bold rounded transition ${
                  symbol === s
                    ? 'bg-blue-600 text-white'
                    : 'text-gray-400 hover:text-white hover:bg-darkborder'
                }`}
              >
                {s}
              </button>
            ))}
          </div>

          {/* Real-time Ticker Display */}
          <div className="flex items-center space-x-2">
            <span className="text-sm font-mono text-gray-400">{symbol}</span>
            <span className="text-lg font-mono font-bold text-white">
              {lastPrice ? `$${lastPrice.toLocaleString(undefined, { minimumFractionDigits: 2 })}` : '---'}
            </span>
          </div>
        </div>

        {/* Timeframe Selector */}
        <div className="flex items-center space-x-1 bg-darkcard p-1 rounded-lg border border-darkborder">
          {TIMEFRAMES.map((tf) => (
            <button
              key={tf}
              onClick={() => onTimeframeChange(tf)}
              className={`px-2 py-0.5 text-xs font-semibold rounded transition ${
                timeframe === tf
                  ? 'bg-blue-600/30 text-blue-400 border border-blue-500/40'
                  : 'text-gray-400 hover:text-white'
              }`}
            >
              {tf}
            </button>
          ))}
        </div>
      </div>

      {/* Chart Surface */}
      <div className="relative w-full h-[420px]" ref={chartContainerRef}>
        {loading && (
          <div className="absolute inset-0 z-10 flex items-center justify-center bg-darkpanel/80">
            <div className="flex items-center space-x-2 text-blue-400 text-sm">
              <RefreshCw className="w-4 h-4 animate-spin" />
              <span>Loading market data...</span>
            </div>
          </div>
        )}
      </div>
    </div>
  );
};

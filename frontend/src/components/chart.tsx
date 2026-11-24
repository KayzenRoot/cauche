import { useEffect, useMemo, useRef, useState } from 'react'
import {
  AreaSeries, CandlestickSeries, ColorType, createChart, createSeriesMarkers, CrosshairMode,
  HistogramSeries, LineSeries, LineStyle,
} from 'lightweight-charts'
import type { CandlestickData, IChartApi, ISeriesApi, ISeriesMarkersPluginApi, LineData, SeriesMarker, Time, UTCTimestamp } from 'lightweight-charts'
import { Expand, SlidersHorizontal } from 'lucide-react'
import type { Candle, Exchange, Marker } from '../types'
import { dateTime, number, numberValue, timestamp } from '../format'

type Overlay = 'ema' | 'bollinger' | 'levels' | 'rsi' | 'atr' | 'volume'
type GraphCandle = CandlestickData<UTCTimestamp> & {volume?: number}

const overlays: Array<{id: Overlay; label: string; color: string}> = [
  { id: 'ema', label: 'EMA 20/50', color: '#b5ff39' },
  { id: 'bollinger', label: 'Bollinger 20', color: '#79b7ff' },
  { id: 'levels', label: 'Suporte e resistência', color: '#f3bf55' },
  { id: 'rsi', label: 'RSI 14', color: '#bf9af7' },
  { id: 'atr', label: 'ATR 14', color: '#ffe5a8' },
  { id: 'volume', label: 'Volume', color: '#19d59b' },
]

const options = {
  autoSize: true,
  layout: {
    background: { type: ColorType.Solid, color: '#0c1310' },
    textColor: '#90a298',
    fontFamily: 'Space Grotesk Variable, monospace',
    fontSize: 11,
    attributionLogo: true,
  },
  grid: { vertLines: { color: '#17211b' }, horzLines: { color: '#17211b' } },
  timeScale: { borderColor: '#28392e', timeVisible: true, secondsVisible: false, rightOffset: 5 },
  rightPriceScale: { borderColor: '#28392e', minimumWidth: 64 },
  crosshair: { mode: CrosshairMode.Normal },
  localization: { locale: 'pt-BR' },
}

function chartData(rows: Candle[]) {
  const byTime = new Map<number, GraphCandle>()
  for (const candle of rows) {
    const { time } = candle
    const open = numberValue(candle.open)
    const close = numberValue(candle.close)
    const high = numberValue(candle.high)
    const low = numberValue(candle.low)
    if (!Number.isFinite(time) || open === undefined || close === undefined || high === undefined || low === undefined) continue
    if (low > Math.min(open, close) || high < Math.max(open, close)) continue
    byTime.set(time, { time: time as UTCTimestamp, open, close, high, low, volume: numberValue(candle.volume) })
  }
  return [...byTime.values()].sort((a, b) => a.time - b.time)
}

function ema(data: GraphCandle[], size: number): LineData<UTCTimestamp>[] {
  let average: number | undefined
  return data.flatMap((row, index) => {
    if (index + 1 < size) return []
    average = average === undefined
      ? data.slice(0, size).reduce((sum, item) => sum + item.close, 0) / size
      : average + (row.close - average) * (2 / (size + 1))
    return [{ time: row.time, value: average }]
  })
}

function bands(data: GraphCandle[], size: number) {
  return data.slice(size - 1).map((row, index) => {
    const highs = data.slice(index, index + size)
    const closes = highs.map(item => item.close)
    const mean = closes.reduce((sum, value) => sum + value, 0) / size
    const deviation = Math.sqrt(closes.reduce((sum, value) => sum + (value - mean) ** 2, 0) / size)
    return { time: row.time, top: mean + 2 * deviation, bottom: mean - 2 * deviation }
  })
}

function oscillators(data: GraphCandle[], size = 14) {
  let gain = 0
  let loss = 0
  let range = 0
  return data.slice(1).flatMap((row, index) => {
    const prev = data[index]
    const difference = row.close - prev.close
    const highLow = Math.max(row.high - row.low, Math.abs(row.high - prev.close), Math.abs(row.low - prev.close))
    if (index < size) {
      gain += Math.max(0, difference) / size
      loss += Math.max(0, -difference) / size
      range += highLow / size
    } else {
      gain += (Math.max(0, difference) - gain) / size
      loss += (Math.max(0, -difference) - loss) / size
      range += (highLow - range) / size
    }
    if (index + 1 < size) return []
    const rsi = loss === 0 ? (gain === 0 ? 50 : 100) : 100 - 100 / (1 + gain / loss)
    return [{ time: row.time, rsi, atr: range }]
  })
}

function snapMarkers(markers: Marker[], data: GraphCandle[], step: number): SeriesMarker<Time>[] {
  if (!data.length) return []
  return markers.flatMap((marker): SeriesMarker<Time>[] => {
    const time = Number(marker.time)
    if (!Number.isFinite(time) || time < data[0].time) return []
    const candle = data.findLast(row => row.time <= time)
    if (candle === undefined || time >= candle.time + step) return []
    const type = String(marker.type ?? marker.kind ?? '').toUpperCase()
    const isSettled = type.includes('SETTLE') || type.includes('EXIT')
    const isFill = type.includes('FILL') || type.includes('ENTRY')
    return [{
      time: candle.time,
      position: isSettled ? 'aboveBar' : 'belowBar',
      color: isSettled ? '#f3bf55' : isFill ? '#19d59b' : '#b5ff39',
      shape: isSettled ? 'square' : isFill ? 'arrowUp' : 'circle',
      text: isSettled ? 'Liquidação' : isFill ? 'Execução' : 'Sinal',
    }]
  }).sort((a, b) => Number(a.time) - Number(b.time))
}

interface Plot {
  chart: IChartApi
  candles: ISeriesApi<'Candlestick'>
  markers: ISeriesMarkersPluginApi<Time>
}

export function MarketChart({
  candles, markers, exchange, timeframe, identity,
}: {candles: Candle[]; markers?: Marker[]; exchange: Exchange; timeframe: number; identity: string}) {
  const element = useRef<HTMLDivElement>(null)
  const plot = useRef<Plot | null>(null)
  const data = useMemo(() => chartData(candles), [candles])
  const [hovered, setHovered] = useState<GraphCandle>()
  const [visible, setVisible] = useState<Overlay[]>(['ema'])
  const [showOptions, setShowOptions] = useState(false)
  const volumeAvailable = data.some(row => row.volume !== undefined)
  const digits = exchange === 'DERIV' ? 5 : 4
  const shown = hovered ?? data.at(-1)

  useEffect(() => {
    if (!element.current) return
    const chart = createChart(element.current, options)
    const candles = chart.addSeries(CandlestickSeries, {
      upColor: '#19d59b', downColor: '#ff5572',
      wickUpColor: '#19d59b', wickDownColor: '#ff5572',
      borderVisible: false, priceLineStyle: LineStyle.Dotted,
      priceFormat: { type: 'price', precision: digits, minMove: 1 / 10 ** digits },
    })
    const markers = createSeriesMarkers(candles)
    plot.current = { chart, candles, markers }
    chart.subscribeCrosshairMove(event => {
      const item = event.seriesData.get(candles) as GraphCandle | undefined
      setHovered(item)
    })
    return () => {
      plot.current = null
      chart.remove()
    }
  }, [digits, identity])

  useEffect(() => {
    const current = plot.current
    if (!current) return
    current.candles.setData(data)
    current.markers.setMarkers(snapMarkers(markers ?? [], data, timeframe))
  }, [data, digits, identity, markers, timeframe])

  useEffect(() => {
    plot.current?.chart.timeScale().fitContent()
  }, [digits, identity])

  useEffect(() => {
    const current = plot.current
    if (!current) return
    const { chart, candles } = current
    const series: Array<ISeriesApi<'Line'> | ISeriesApi<'Histogram'>> = []
    const lines: ReturnType<typeof candles.createPriceLine>[] = []
    const format = { type: 'price' as const, precision: digits, minMove: 1 / 10 ** digits }
    const addLine = (color: string, points: LineData<UTCTimestamp>[], pane = 0) => {
      const line = chart.addSeries(LineSeries, {
        color, lineWidth: 1, priceLineVisible: false, lastValueVisible: false,
        crosshairMarkerVisible: false, priceFormat: pane === 0 ? format : undefined,
      }, pane)
      line.setData(points)
      series.push(line)
    }
    if (visible.includes('ema')) {
      addLine('#b5ff39', ema(data, 20))
      addLine('#7e974d', ema(data, 50))
    }
    if (visible.includes('bollinger')) {
      const values = bands(data, 20)
      addLine('#79b7ff', values.map(({ time, top }) => ({ time, value: top })))
      addLine('#79b7ff', values.map(({ time, bottom }) => ({ time, value: bottom })))
    }
    if (visible.includes('levels') && data.length >= 20) {
      const window = data.slice(-20)
      for (const level of [
        { price: Math.min(...window.map(item => item.low)), title: 'Suporte' },
        { price: Math.max(...window.map(item => item.high)), title: 'Resistência' },
      ]) {
        lines.push(candles.createPriceLine({ ...level, lineStyle: LineStyle.Dashed, color: '#f3bf55', axisLabelVisible: true }))
      }
    }
    let pane = 1
    if (visible.includes('rsi') || visible.includes('atr')) {
      const rows = oscillators(data)
      if (visible.includes('rsi')) addLine('#bf9af7', rows.map(({ time, rsi }) => ({time, value: rsi})), pane++)
      if (visible.includes('atr')) addLine('#ffe5a8', rows.map(({ time, atr }) => ({time, value: atr})), pane++)
    }
    if (visible.includes('volume') && volumeAvailable) {
      const histogram = chart.addSeries(HistogramSeries, { color: '#19d59b', priceFormat: { type: 'volume' } }, pane)
      histogram.setData(data.flatMap(row => row.volume === undefined ? [] : [{
        time: row.time, value: row.volume, color: row.close >= row.open ? '#1f765c' : '#793947',
      }]))
      series.push(histogram)
    }
    for (const panel of chart.panes().slice(1)) panel.setHeight(80)
    return () => {
      if (plot.current !== current) return
      for (const line of lines) candles.removePriceLine(line)
      for (const line of series.reverse()) chart.removeSeries(line)
    }
  }, [data, digits, identity, visible, volumeAvailable])

  return <>
    <div className="chart-tools">
      <div className="quote-legend" aria-live="off">
        {shown && <>
          <span><b>A</b> {number(shown.open, digits)}</span>
          <span><b>M</b> {number(shown.high, digits)}</span>
          <span><b>Mín</b> {number(shown.low, digits)}</span>
          <span><b>F</b> {number(shown.close, digits)}</span>
          {shown.volume !== undefined && <span><b>Vol.</b> {number(shown.volume)}</span>}
        </>}
      </div>
      <div className="chart-actions">
        <button className="icon-button" type="button" aria-label="Indicadores do gráfico" aria-expanded={showOptions} title="Indicadores" onClick={() => setShowOptions(value => !value)}>
          <SlidersHorizontal size={15} aria-hidden="true" />
        </button>
        <button className="icon-button" type="button" aria-label="Ajustar gráfico ao período disponível" title="Ajustar período" onClick={() => plot.current?.chart.timeScale().fitContent()}>
          <Expand size={15} aria-hidden="true" />
        </button>
      </div>
    </div>
    {showOptions && <div className="chart-indicators">
      {overlays.filter(option => option.id !== 'volume' || volumeAvailable).map(({ id, label, color }) => <label key={id}>
        <input type="checkbox" checked={visible.includes(id)} onChange={event => setVisible(list => event.target.checked ? [...list, id] : list.filter(item => item !== id))} />
        <i aria-hidden="true" style={{ background: color }} />{label}
      </label>)}
    </div>}
    <div ref={element} className="candle-canvas" role="img" aria-label="Candles com abertura, máxima, mínima e fechamento em UTC. O último candle está disponível abaixo." />
    {shown && <p className="chart-caption"><span>{dateTime(shown.time)}</span><span>Gráfico financeiro · <a href="https://www.tradingview.com/" target="_blank" rel="noreferrer">TradingView</a></span></p>}
  </>
}

export function EquityGraph({ points }: {points: Array<{time: number | string; value: number | string}>}) {
  const ref = useRef<HTMLDivElement>(null)
  useEffect(() => {
    if (!ref.current) return
    const graph = createChart(ref.current, options)
    const series = graph.addSeries(AreaSeries, {
      lineColor: '#b5ff39', topColor: '#b5ff3939', bottomColor: '#b5ff3905', lineWidth: 2,
      priceLineVisible: false,
    })
    const byTime = new Map<number, number>()
    for (const point of points) {
      const time = Math.trunc(timestamp(point.time).getTime() / 1000)
      const value = numberValue(point.value)
      if (Number.isFinite(time) && value !== undefined) byTime.set(time, value)
    }
    series.setData([...byTime].sort(([first], [second]) => first - second).map(([time, value]) => ({
      time: time as UTCTimestamp, value,
    })))
    graph.timeScale().fitContent()
    return () => graph.remove()
  }, [points])
  return <div ref={ref} className="equity-canvas" role="img" aria-label="Evolução do patrimônio no período selecionado" />
}

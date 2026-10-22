import { useCallback, useEffect, useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { type HuichunChartResponse, type KlineRow } from '@/lib/api'
import { huichunChartMarkers, stockChartQueryOptions, type HuichunChartContext } from '@/lib/huichun-chart'
import { storage } from '@/lib/storage'
import {
  EChartsCandlestick,
  OVERLAY_INDICATORS,
  SUB_CHARTS,
  type ChartMarker,
  type ChartPriceLine,
  type ChartRange,
  type OHLC,
  type VolumeCompareConfig,
} from '@/components/EChartsCandlestick'

const SUB_INFO_H = 16
const SUB_GAP = 4
const DEFAULT_VOLUME_COMPARE: VolumeCompareConfig = { enabled: true, days: 1 }
const HUICHUN_MACD = [10, 20, 9] as const

function normalizeVolumeCompare(config: VolumeCompareConfig): VolumeCompareConfig {
  return {
    enabled: config.enabled !== false,
    days: Math.max(1, Math.min(20, Math.round(Number(config.days) || 1))),
  }
}

interface Props {
  symbol: string
  height?: number
  className?: string
  dateRange?: { start: string; end: string }
  markers?: ChartMarker[]
  ranges?: ChartRange[]
  priceLines?: ChartPriceLine[]
  showLimitMarkers?: boolean
  showIndicatorControls?: boolean
  showMarkerToggle?: boolean
  showMA?: boolean
  showInfoBar?: boolean
  /** 初始可见蜡烛根数; 'all' = 适配显示全部数据 */
  visibleBars?: number | 'all'
  linkedPrice?: number | null
  onDateClick?: (date: string) => void
  onPriceDoubleClick?: (price: number, currentPrice: number) => void
  /** 扩展数据列参数（逗号分隔 config_id.field_name），透传给 klineDaily 接口 */
  extColumns?: string
  /** 加入自选日 (北京时间 YYYY-MM-DD); 有值时日K主图绘制「自选」竖虚线 */
  addedDate?: string | null
  /** 回春日线使用所选形态日与规则的专用指标序列。 */
  huichunChart?: HuichunChartContext
}

function isValidRow(r: any): boolean {
  return r && r.date != null && r.open != null && r.close != null
}

export function toOHLC(rows: KlineRow[]): OHLC[] {
  return rows
    .filter(isValidRow)
    .map(r => ({
      date: typeof r.date === 'string' ? r.date.slice(0, 10) : String(r.date),
      open: Number(r.open),
      high: Number(r.high),
      low: Number(r.low),
      close: Number(r.close),
      volume: Number(r.volume ?? 0),
      ma5: r.ma5 != null ? Number(r.ma5) : null,
      ma10: r.ma10 != null ? Number(r.ma10) : null,
      ma20: r.ma20 != null ? Number(r.ma20) : null,
      ma60: r.ma60 != null ? Number(r.ma60) : null,
      macd_dif: r.macd_dif != null ? Number(r.macd_dif) : null,
      macd_dea: r.macd_dea != null ? Number(r.macd_dea) : null,
      macd_hist: r.macd_hist != null ? Number(r.macd_hist) : null,
      rsi_6: r.rsi_6 != null ? Number(r.rsi_6) : null,
      rsi_14: r.rsi_14 != null ? Number(r.rsi_14) : null,
      rsi_24: r.rsi_24 != null ? Number(r.rsi_24) : null,
      kdj_k: r.kdj_k != null ? Number(r.kdj_k) : null,
      kdj_d: r.kdj_d != null ? Number(r.kdj_d) : null,
      kdj_j: r.kdj_j != null ? Number(r.kdj_j) : null,
      boll_upper: r.boll_upper != null ? Number(r.boll_upper) : null,
      boll_lower: r.boll_lower != null ? Number(r.boll_lower) : null,
    }))
}

function buildLimitUpMarkers(rows: KlineRow[]): ChartMarker[] {
  const markers: ChartMarker[] = []
  for (const r of rows) {
    const date = typeof r.date === 'string' ? r.date.slice(0, 10) : String(r.date)
    if (r.signal_broken_limit_up) {
      markers.push({ date, kind: 'neutral', above: true, color: '#8B5CF6', label: '炸' })
    } else if (r.signal_limit_up) {
      const boards: number = r.consecutive_limit_ups ?? 1
      markers.push({ date, kind: 'buy', above: true, color: '#FACC15', label: boards <= 1 ? '板' : String(boards) })
    }
  }
  return markers
}

export function getDefaultRange(): { start: string; end: string } {
  const now = new Date()
  const end = now.toISOString().slice(0, 10)
  const s = new Date(now)
  s.setMonth(s.getMonth() - 6)
  const start = s.toISOString().slice(0, 10)
  return { start, end }
}

/** 工具栏小胶囊开关（指标与标注开关共用一套尺寸/关闭态样式，激活色由调用方给字面量）。 */
function ChartPill({
  active, label, activeClass, title, onClick,
}: {
  active: boolean
  label: string
  activeClass: string
  title?: string
  onClick: () => void
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      title={title}
      className={`px-2 py-0.5 rounded text-[10px] font-mono cursor-pointer transition-colors ${
        active ? activeClass : 'bg-elevated text-muted hover:text-secondary'
      }`}
    >
      {label}
    </button>
  )
}

export function StockDailyKChart({
  symbol,
  height = 520,
  className,
  dateRange: externalDateRange,
  markers,
  ranges,
  priceLines,
  showLimitMarkers = true,
  showIndicatorControls = true,
  showMarkerToggle = true,
  showMA = true,
  showInfoBar = true,
  visibleBars = 60,
  linkedPrice,
  onDateClick,
  onPriceDoubleClick,
  extColumns,
  addedDate,
  huichunChart,
}: Props) {
  const [activeIndicators, setActiveIndicators] = useState<string[]>(() => huichunChart ? ['vol', 'macd'] : ['vol'])
  const [showMarkers, setShowMarkers] = useState(true)
  // 加入自选日标注（与「异动」标记相互独立）
  const [showAddedMark, setShowAddedMark] = useState(true)
  const [volumeCompare, setVolumeCompare] = useState<VolumeCompareConfig>(() =>
    normalizeVolumeCompare(storage.stockVolumeCompare.get(DEFAULT_VOLUME_COMPARE)),
  )
  const dateRange = externalDateRange ?? getDefaultRange()
  const huichunMode = !!huichunChart
  const contextMismatch = !!huichunChart && huichunChart.symbol !== symbol
  const rangeAfterSignal = !!huichunChart && dateRange.start > huichunChart.as_of
  useEffect(() => {
    if (huichunMode) setActiveIndicators(previous => previous.includes('macd') ? previous : [...previous, 'macd'])
  }, [huichunMode])

  // 与 StockPanel 信息条共享同一查询；专用序列不使用普通日K缓存或实时快照。
  const kline = useQuery(stockChartQueryOptions(symbol, dateRange, extColumns, huichunChart))
  const huichunData = huichunChart && !contextMismatch && kline.data?.source === 'huichun_a0'
    ? (kline.data as HuichunChartResponse).huichun : undefined

  const sourceRows = contextMismatch || rangeAfterSignal ? undefined : kline.data?.rows
  const rows = useMemo(() => toOHLC(sourceRows ?? []), [sourceRows])
  const stockInfo = kline.data?.stock_info
  const limitMarkers = useMemo(() => buildLimitUpMarkers(sourceRows ?? []), [sourceRows])
  const allMarkers = useMemo(() => [
    ...(markers ?? []),
    ...(huichunChart && !contextMismatch ? huichunChartMarkers(huichunChart) : []),
    ...(showLimitMarkers ? limitMarkers : []),
  ], [limitMarkers, markers, showLimitMarkers, huichunChart, contextMismatch])

  const toggleIndicator = useCallback((key: string) => {
    setActiveIndicators(prev => prev.includes(key) ? prev.filter(k => k !== key) : [...prev, key])
  }, [])

  const updateVolumeCompare = useCallback((patch: Partial<VolumeCompareConfig>) => {
    setVolumeCompare(prev => {
      const next = normalizeVolumeCompare({ ...prev, ...patch })
      storage.stockVolumeCompare.set(next)
      return next
    })
  }, [])

  const activeSubDefs = activeIndicators
    .map(key => SUB_CHARTS.find(s => s.key === key))
    .filter((d): d is typeof SUB_CHARTS[number] => !!d)
  let subExtraH = 0
  activeSubDefs.forEach(def => { subExtraH += SUB_INFO_H + def.height })
  if (activeSubDefs.length > 0) subExtraH += activeSubDefs.length * SUB_GAP + 14
  const chartHeight = height + subExtraH

  if (!symbol) return null

  return (
    <div className={className} style={{ minHeight: chartHeight }}>
      {huichunChart && <div className="mb-2 rounded border border-border bg-surface px-3 py-2 text-xs leading-relaxed text-secondary">
        <p className="font-medium text-foreground">回春 A0 · MACD 10 / 20 / 9 · 规则版本 {huichunChart.rule_revision}</p>
        <p>形态日期 {huichunChart.as_of} · 趋势均线 MA{huichunChart.rules.ma_window}；按已存有效历史计算。</p>
        {huichunData?.price_basis_date && <p>价格与指标统一使用 {huichunData.price_basis_date} 的复权基准。</p>}
        <p>G 前轮金叉 · D 前轮死叉 · R 回春确认；标记为形态日期，非成交点。</p>
        {huichunData?.latest_valid_date && huichunData.latest_valid_date < huichunChart.as_of && <p role="status">有效日线仅到 {huichunData.latest_valid_date}，形态日期的数据尚不完整。</p>}
        {huichunData?.invalid_from && <p role="status">{huichunData.invalid_from} 起复权数据异常，该区间不绘制指标。</p>}
        {huichunData?.factor_status === 'invalid_undated' && <p role="status">复权异常尚无法确定生效日期，暂无可用的指标序列。</p>}
        {huichunData && huichunData.missing_observed_days != null && huichunData.missing_observed_days > 0 && <p role="status">历史含 {huichunData.missing_observed_days} 个缺失或不可用交易日；指标在有效日线段内分别计算{huichunData.segment_count != null ? `（${huichunData.segment_count} 段）` : ''}。</p>}
        {huichunData && huichunData.missing_observed_days == null && <p>历史缺口覆盖暂无法核验。</p>}
        {huichunData && ['events_present_unverified', 'no_events_unverified'].includes(huichunData.factor_status) && <p>复权事件完整性尚未核验。</p>}
      </div>}
      {showIndicatorControls && rows.length > 0 && (
        <div className="flex flex-wrap items-center gap-1.5 px-1 pb-0.5">
          {SUB_CHARTS.map(ind => (
            <ChartPill
              key={ind.key}
              active={activeIndicators.includes(ind.key)}
              label={ind.key === 'macd' && huichunChart ? 'MACD 10 / 20 / 9' : ind.label}
              activeClass="bg-accent/20 text-accent"
              onClick={() => toggleIndicator(ind.key)}
            />
          ))}
          {OVERLAY_INDICATORS.map(ind => (
            <ChartPill
              key={ind.key}
              active={activeIndicators.includes(ind.key)}
              label={ind.label}
              activeClass="bg-accent/20 text-accent"
              onClick={() => toggleIndicator(ind.key)}
            />
          ))}
          {activeIndicators.includes('vol') && (
            <div className="ml-0.5 flex h-5 items-center gap-1.5 border-l border-border/70 pl-2">
              <span className="text-[10px] text-muted">量比</span>
              <button
                type="button"
                role="switch"
                aria-checked={volumeCompare.enabled}
                aria-label="开启量能对比"
                title={volumeCompare.enabled ? '关闭量能对比' : '开启量能对比'}
                onClick={() => updateVolumeCompare({ enabled: !volumeCompare.enabled })}
                className={`relative h-3.5 w-6 shrink-0 rounded-full transition-colors ${
                  volumeCompare.enabled ? 'bg-accent' : 'bg-elevated'
                }`}
              >
                <span className={`absolute left-0 top-0.5 h-2.5 w-2.5 rounded-full bg-white transition-transform ${
                  volumeCompare.enabled ? 'translate-x-3' : 'translate-x-0.5'
                }`} />
              </button>
              <select
                aria-label="量能对比周期"
                value={volumeCompare.days}
                disabled={!volumeCompare.enabled}
                onChange={event => updateVolumeCompare({ days: Number(event.target.value) })}
                className="h-5 rounded border border-border bg-base px-1 text-[10px] text-secondary outline-none disabled:opacity-40"
              >
                {Array.from({ length: 20 }, (_, index) => index + 1).map(days => (
                  <option key={days} value={days}>前{days}日均量</option>
                ))}
              </select>
            </div>
          )}
          {/* 两个标注开关统一右对齐 */}
          <div className="ml-auto flex items-center gap-1.5">
            {showMarkerToggle && showLimitMarkers && (
              <ChartPill
                active={showMarkers}
                label="异动"
                activeClass="text-[#FACC15] bg-[#FACC15]/10"
                onClick={() => setShowMarkers(v => !v)}
              />
            )}
            {/* 激活色与图表里的 ADDED_DATE_COLOR 一致; Tailwind 只认字面量类名, 故写死色值 */}
            {showMarkerToggle && addedDate && (
              <ChartPill
                active={showAddedMark}
                label="自选"
                activeClass="text-[#3B82F6] bg-[#3B82F6]/10"
                title={showAddedMark ? '隐藏K线上的加入自选日标注' : '显示K线上的加入自选日标注'}
                onClick={() => setShowAddedMark(v => !v)}
              />
            )}
          </div>
        </div>
      )}
      {contextMismatch && <div role="alert" className="text-sm text-danger py-2">回春形态与当前股票不匹配，请重新选择候选。</div>}
      {rangeAfterSignal && <div role="status" className="text-sm text-muted py-2">所选日期范围晚于形态日期，请调整开始日期。</div>}
      {kline.isLoading && !contextMismatch && !rangeAfterSignal && <div className="text-sm text-muted py-4">加载中…</div>}
      {kline.isError && <div role="alert" className="text-sm text-danger py-2">{huichunChart ? `回春日K加载失败：${kline.error.message}` : '日K加载失败'}</div>}
      {huichunChart && !contextMismatch && !rangeAfterSignal && kline.isSuccess && rows.length === 0 && <div role="status" className="text-sm text-muted py-2">该范围暂无可用的回春日线数据。</div>}
      {!kline.isLoading && !kline.isError && (sourceRows?.length ?? 0) > 0 && rows.length === 0 && (
        <div className="text-sm text-danger py-2">数据格式异常，请刷新页面</div>
      )}
      {rows.length > 0 && (
        <EChartsCandlestick
          data={rows}
          markers={allMarkers}
          ranges={ranges}
          priceLines={priceLines}
          height={chartHeight - 22}
          showMA={showMA}
          showInfoBar={showInfoBar}
          showMarkers={showMarkers}
          stockInfo={stockInfo}
          symbol={symbol}
          linkedPrice={linkedPrice}
          onDateClick={onDateClick}
          onPriceDoubleClick={onPriceDoubleClick}
          visibleBars={visibleBars}
          activeIndicators={activeIndicators}
          volumeCompare={volumeCompare}
          addedDate={showAddedMark ? addedDate : null}
          macdParameters={huichunChart ? HUICHUN_MACD : undefined}
          trendMaPeriod={huichunChart?.rules.ma_window}
        />
      )}
    </div>
  )
}

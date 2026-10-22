import { useEffect, useRef } from 'react'
import * as echarts from 'echarts'
import type { ECharts, EChartsOption } from 'echarts'

/** 使用当前主线的原生 ECharts 生命周期，离开页签时释放图表和尺寸监听。 */
export function HistoryChart({ option, height, label }: { option: EChartsOption; height: number; label: string }) {
  const container = useRef<HTMLDivElement>(null)
  const instance = useRef<ECharts | null>(null)

  useEffect(() => {
    if (!container.current) return
    const chart = echarts.init(container.current, undefined, { renderer: 'svg' })
    instance.current = chart
    const observer = new ResizeObserver(() => chart.resize())
    observer.observe(container.current)
    return () => {
      observer.disconnect()
      chart.dispose()
      instance.current = null
    }
  }, [])

  useEffect(() => {
    instance.current?.setOption(option, { notMerge: true })
  }, [option])

  return <div ref={container} role="img" aria-label={label} className="min-w-0" style={{ height }} />
}

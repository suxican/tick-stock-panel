import { useEffect, useRef } from 'react'
import * as echarts from 'echarts'
import type { ECharts, EChartsOption } from 'echarts'

/** 挂载到真实容器后初始化；空历史、换页和切换页签时一起释放图表与尺寸监听。 */
export function EmotionHistoryChart({ option, label }: { option: EChartsOption; label: string }) {
  const container = useRef<HTMLDivElement>(null)
  const instance = useRef<ECharts | null>(null)

  useEffect(() => {
    if (!container.current) return
    const chart = echarts.init(container.current, undefined, { renderer: 'canvas' })
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

  return <div ref={container} role="img" aria-label={label} className="h-[230px] min-w-0" />
}

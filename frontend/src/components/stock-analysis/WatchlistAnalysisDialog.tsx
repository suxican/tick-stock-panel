import { useCallback, useEffect, useRef, useState } from 'react'
import { History, LineChart, Loader2, Sparkles, X } from 'lucide-react'
import { Modal } from '@/components/Modal'
import { cn } from '@/lib/cn'
import { findTodayReport, loadHistory, openHistoryReport, startAnalysis, useHistoryReports } from '@/lib/stockAnalysisStore'
import { StockAnalysisBoard } from './StockAnalysisBoard'

export function WatchlistAnalysisDialog({ symbol, name, onClose }: {
  symbol: string
  name: string
  onClose: () => void
}) {
  const [view, setView] = useState<'levels' | 'history'>('levels')
  const [historyStatus, setHistoryStatus] = useState<'loading' | 'ready' | 'error'>('loading')
  const [checking, setChecking] = useState(false)
  const [todayReportId, setTodayReportId] = useState<string | null>(null)
  const [analysisError, setAnalysisError] = useState('')
  const analysisPending = useRef(false)
  const { reports } = useHistoryReports()
  const stockReports = reports.filter(report => report.symbol === symbol)
  const mounted = useRef(false)
  const refreshHistory = useCallback(async () => {
    setHistoryStatus('loading')
    const ok = await loadHistory()
    if (mounted.current) setHistoryStatus(ok ? 'ready' : 'error')
  }, [])

  useEffect(() => {
    mounted.current = true
    void refreshHistory()
    return () => { mounted.current = false }
  }, [refreshHistory])

  const handleAnalyze = async (regenerate = false) => {
    if (analysisPending.current) return
    analysisPending.current = true
    setChecking(true)
    setAnalysisError('')
    setTodayReportId(null)
    try {
      if (!regenerate) {
        const report = await findTodayReport(symbol)
        if (!mounted.current) return
        if (report) {
          setTodayReportId(report.id)
          return
        }
      }
      const result = await startAnalysis(symbol, name)
      if (!mounted.current) return
      if (result.error) setAnalysisError(result.error)
      else onClose()
    } catch {
      if (mounted.current) setAnalysisError('无法启动分析，请重试。')
    } finally {
      analysisPending.current = false
      if (mounted.current) setChecking(false)
    }
  }

  return (
    <Modal onClose={onClose} ariaLabel={`${name || symbol} 关键分析`}
      panelClassName="w-[96vw] max-w-6xl max-h-[92dvh] flex flex-col overflow-hidden rounded-card border border-border bg-surface shadow-xl">
      <div className="flex items-center gap-3 border-b border-border px-4 py-3 shrink-0">
        <div className="min-w-0 flex-1">
          <h2 className="text-sm font-medium text-foreground truncate">{name || symbol} · 关键分析</h2>
          <div className="text-xs font-mono text-muted mt-0.5">{symbol}</div>
        </div>
        <button type="button" onClick={onClose} aria-label="关闭关键分析"
          className="p-2 rounded-btn text-muted hover:bg-elevated hover:text-foreground focus-visible:outline focus-visible:outline-2 focus-visible:outline-accent">
          <X className="h-4 w-4" />
        </button>
      </div>
      <div className="flex flex-wrap items-center gap-2 px-4 py-2 border-b border-border shrink-0" aria-label="分析视图">
        {([
          { value: 'levels', label: '关键价位', icon: LineChart },
          { value: 'history', label: `历史报告${historyStatus === 'ready' ? ` (${stockReports.length})` : ''}`, icon: History },
        ] as const).map(({ value, label, icon: Icon }) => (
          <button key={value} type="button" aria-pressed={view === value} onClick={() => setView(value)}
            className={cn('inline-flex items-center gap-1.5 rounded-btn px-3 py-2 text-xs transition-colors focus-visible:outline focus-visible:outline-2 focus-visible:outline-accent',
              view === value ? 'bg-accent/10 text-accent' : 'text-secondary hover:bg-elevated hover:text-foreground')}>
            <Icon className="h-3.5 w-3.5" />{label}
          </button>
        ))}
        <button type="button" onClick={() => void handleAnalyze()} disabled={checking} aria-busy={checking}
          className="sm:ml-auto inline-flex items-center gap-1.5 rounded-btn border border-accent/30 bg-accent/10 px-3 py-2 text-xs font-medium text-accent hover:bg-accent/20 transition-colors focus-visible:outline focus-visible:outline-2 focus-visible:outline-accent disabled:opacity-40 disabled:cursor-not-allowed">
          {checking ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Sparkles className="h-3.5 w-3.5" />}
          AI 个股分析
        </button>
      </div>
        {analysisError && <p role="alert" className="mx-4 my-3 shrink-0 text-sm text-danger">{analysisError}</p>}
        {todayReportId && (
          <div className="mx-4 my-3 shrink-0 flex flex-wrap items-center gap-3 rounded-btn bg-elevated px-3 py-3">
            <p role="status" className="text-sm text-secondary">今日已有分析报告</p>
            <div className="flex flex-wrap items-center gap-2 sm:ml-auto">
              <button type="button" onClick={() => { onClose(); openHistoryReport(todayReportId) }}
                className="rounded-btn px-3 py-2 text-xs text-accent hover:bg-accent/10 focus-visible:outline focus-visible:outline-2 focus-visible:outline-accent">查看今日报告</button>
              <button type="button" onClick={() => void handleAnalyze(true)} disabled={checking}
                className="rounded-btn border border-border px-3 py-2 text-xs text-secondary hover:bg-surface focus-visible:outline focus-visible:outline-2 focus-visible:outline-accent disabled:opacity-40">重新分析</button>
              <button type="button" onClick={() => setTodayReportId(null)}
                className="rounded-btn px-3 py-2 text-xs text-muted hover:text-foreground focus-visible:outline focus-visible:outline-2 focus-visible:outline-accent">取消</button>
            </div>
          </div>
        )}
      <div className="min-h-0 overflow-y-auto overscroll-contain p-3 sm:p-4">
        {view === 'levels' ? <StockAnalysisBoard key={symbol} symbol={symbol} height={400} /> : (
          <div className="min-h-60">
            {historyStatus === 'loading' ? (
              <div role="status" className="flex items-center justify-center gap-2 py-16 text-sm text-muted">
                <Loader2 className="h-4 w-4 animate-spin" />正在加载历史报告…
              </div>
            ) : historyStatus === 'error' ? (
              <div role="alert" className="py-16 text-center text-sm text-secondary">
                <p>历史报告加载失败</p>
                <button type="button" onClick={() => void refreshHistory()} className="mt-3 px-3 py-2 rounded-btn bg-elevated text-foreground">重试</button>
              </div>
            ) : stockReports.length === 0 ? (
              <p className="py-16 text-center text-sm text-muted">暂无该标的的历史分析报告</p>
            ) : (
              <ul className="divide-y divide-border">
                {stockReports.map(report => (
                  <li key={report.id}>
                    <button type="button" onClick={() => { onClose(); openHistoryReport(report.id) }}
                      className="w-full text-left px-3 py-4 rounded-btn hover:bg-elevated focus-visible:outline focus-visible:outline-2 focus-visible:outline-accent">
                      <div className="flex flex-wrap items-center justify-between gap-2 text-xs">
                        <time className="text-secondary" dateTime={report.created_at}>{report.created_at.replace('T', ' ').slice(0, 19)}</time>
                        <span className="text-accent">查看报告</span>
                      </div>
                      <p className="mt-1.5 text-sm text-foreground line-clamp-2 break-words">{report.summary || '个股分析报告'}</p>
                      {report.focus && <p className="mt-1 text-xs text-muted truncate">关注：{report.focus}</p>}
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </div>
        )}
      </div>
    </Modal>
  )
}

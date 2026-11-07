import { useCallback, useEffect, useId, useLayoutEffect, useRef, useState, type PointerEvent, type ReactNode } from 'react'
import { createPortal } from 'react-dom'
import { Info } from 'lucide-react'
import { cn } from '@/lib/cn'

const MAX_WIDTH = 320
const GAP = 6
const EDGE = 8

type Position = { left: number; top: number; width: number }

/**
 * 次要说明浮窗：鼠标悬停、键盘聚焦或点击（触屏）时打开；Esc、点击外部或移开鼠标关闭。
 * 浮层渲染到 body 并按视口定位，避免被表格的横向滚动容器裁剪。
 */
export function InfoTip({ title, children, className }: { title: string; children: ReactNode; className?: string }) {
  const id = useId()
  const trigger = useRef<HTMLButtonElement>(null)
  const bubble = useRef<HTMLDivElement>(null)
  const fromPointer = useRef(false)
  const leaveTimer = useRef<number | undefined>(undefined)
  const [hovered, setHovered] = useState(false)
  const [focused, setFocused] = useState(false)
  const [pinned, setPinned] = useState(false)
  const [position, setPosition] = useState<Position | null>(null)
  const open = hovered || focused || pinned

  const close = useCallback(() => {
    window.clearTimeout(leaveTimer.current)
    setHovered(false)
    setFocused(false)
    setPinned(false)
  }, [])

  const place = useCallback(() => {
    const anchor = trigger.current?.getBoundingClientRect()
    if (!anchor) return
    const width = Math.min(MAX_WIDTH, window.innerWidth - EDGE * 2)
    const height = bubble.current?.offsetHeight ?? 0
    const left = Math.max(EDGE, Math.min(anchor.left, window.innerWidth - width - EDGE))
    const below = anchor.bottom + GAP
    const above = anchor.top - GAP - height
    const top = below + height > window.innerHeight - EDGE && above >= EDGE ? above : below
    setPosition(current => current && current.left === left && current.top === top && current.width === width ? current : { left, top, width })
  }, [])

  useLayoutEffect(() => {
    if (!open) return
    place()
    window.addEventListener('resize', place)
    document.addEventListener('scroll', place, true)
    return () => {
      window.removeEventListener('resize', place)
      document.removeEventListener('scroll', place, true)
    }
  }, [open, place])

  useEffect(() => {
    if (!open) return
    const onKey = (event: KeyboardEvent) => { if (event.key === 'Escape') close() }
    const onPointer = (event: globalThis.PointerEvent) => {
      const target = event.target as Node
      if (!trigger.current?.contains(target) && !bubble.current?.contains(target)) close()
    }
    document.addEventListener('keydown', onKey)
    document.addEventListener('pointerdown', onPointer)
    return () => {
      document.removeEventListener('keydown', onKey)
      document.removeEventListener('pointerdown', onPointer)
    }
  }, [open, close])

  useEffect(() => () => window.clearTimeout(leaveTimer.current), [])

  // 只有鼠标悬停才自动打开；触屏点击由 pinned 状态控制，避免点一下又立即关闭。
  // Tab 移走焦点时收起；点击浮层内容（relatedTarget 为空）不收起，便于滚动阅读。
  const enter = (event: PointerEvent) => {
    if (event.pointerType !== 'mouse') return
    window.clearTimeout(leaveTimer.current)
    setHovered(true)
  }
  const leave = (event: PointerEvent) => {
    if (event.pointerType !== 'mouse') return
    window.clearTimeout(leaveTimer.current)
    leaveTimer.current = window.setTimeout(() => setHovered(false), 150)
  }

  return <>
    <button ref={trigger} type="button" aria-label={`查看${title}`} aria-expanded={open} aria-describedby={open ? id : undefined}
      onPointerEnter={enter} onPointerLeave={leave}
      onPointerDown={() => { fromPointer.current = true }}
      onFocus={() => { if (!fromPointer.current) setFocused(true) }}
      onBlur={event => { fromPointer.current = false; setFocused(false); if (event.relatedTarget) setPinned(false) }}
      onClick={() => { fromPointer.current = false; setPinned(value => !value) }}
      className={cn('inline-flex h-6 w-6 shrink-0 items-center justify-center rounded align-middle text-secondary transition-colors hover:text-foreground focus-visible:outline focus-visible:outline-2 focus-visible:outline-accent', open && 'text-foreground', className)}>
      <Info className="h-3.5 w-3.5" aria-hidden="true" />
    </button>
    {open && createPortal(
      <div ref={bubble} id={id} role="tooltip" onPointerEnter={enter} onPointerLeave={leave}
        style={{ left: position?.left ?? 0, top: position?.top ?? 0, width: position?.width ?? Math.min(MAX_WIDTH, window.innerWidth - EDGE * 2), visibility: position ? 'visible' : 'hidden' }}
        className="fixed z-50 max-h-[min(60vh,420px)] overflow-y-auto rounded-md border border-border bg-elevated p-3 text-left text-xs font-normal leading-relaxed text-foreground shadow-lg">
        <p className="mb-1.5 font-medium">{title}</p>
        <div className="space-y-1.5 text-secondary">{children}</div>
      </div>,
      document.body,
    )}
  </>
}

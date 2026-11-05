import { ExternalLink } from 'lucide-react'
import type { ReactNode } from 'react'

export function number(value: number | null | undefined, digits = 0, signed = false) {
  if (value == null || !Number.isFinite(value)) return '—'
  return value.toLocaleString('zh-CN', {
    minimumFractionDigits: digits, maximumFractionDigits: digits, signDisplay: signed ? 'exceptZero' : 'auto',
  })
}

export function SourceLink({ href, children, page }: { href: string | null; children: ReactNode; page?: number }) {
  if (!href || !/^https?:\/\//i.test(href)) return null
  const url = page ? href.split('#')[0] + '#page=' + page : href
  return <a href={url} target="_blank" rel="noreferrer" className="etf-link inline-flex items-center gap-1">
    {children}<ExternalLink aria-hidden size={12} />
  </a>
}

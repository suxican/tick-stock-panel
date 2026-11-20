import { describe, expect, it } from 'vitest'
import { buildPdf, buildReportFileName } from './reportExport'

describe('buildReportFileName', () => {
  it('拼接名称/代码/日期并替换非法字符', () => {
    expect(buildReportFileName('拓邦 股份', '002139.SZ', 'md', '2026-11-11T08:00:00')).toBe('拓邦_股份_002139.SZ_分析报告_20261111.md')
    expect(buildReportFileName('a/b:c', '', 'pdf', new Date(2026, 0, 5))).toBe('a_b_c_分析报告_20260105.pdf')
  })
  it('日期非法时回退为当前日期', () => {
    expect(buildReportFileName('x', 'y', 'png', 'bad')).toMatch(/^x_y_分析报告_\d{8}\.png$/)
  })
})

describe('buildPdf', () => {
  it('生成结构完整的多页 PDF', () => {
    const jpeg = new Uint8Array([0xff, 0xd8, 0xff, 0xd9])
    const bytes = buildPdf([
      { jpeg, width: 10, height: 14 },
      { jpeg, width: 10, height: 14 },
    ])
    const text = new TextDecoder('latin1').decode(bytes)
    expect(text.startsWith('%PDF-1.4')).toBe(true)
    expect(text).toContain('/Count 2')
    expect(text.trimEnd().endsWith('%%EOF')).toBe(true)
    const startxref = Number(/startxref\n(\d+)/.exec(text)![1])
    expect(text.slice(startxref, startxref + 4)).toBe('xref')
    // 第 1 个对象偏移应指向 "1 0 obj"
    const entry1 = Number(/0000000000 65535 f \n(\d{10})/.exec(text)![1])
    expect(text.slice(entry1, entry1 + 7)).toBe('1 0 obj')
  })
})

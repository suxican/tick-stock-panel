import { describe, expect, it } from 'vitest'
import type { MeozHotRank } from '@/lib/api'
import { hotStockEvidence, hotThemeByName } from './useMeozThemeAttribution'

const hot: MeozHotRank = {
  fetched_at: '2026-11-20T10:00:00+08:00',
  stocks: {
    ths_normal: { available: true, rows: [{ symbol: '002242.SZ', rank: 3, name: '九阳股份', pct_chg: 0.01 }] },
    dfcf_renqi: { available: false, rows: [] },
    ths_trend: { available: true, rows: [] },
  },
  themes: { available: true, rows: [{ rank: 1, theme_id: '801001', name: '芯片', available_at: '2026-11-20T09:55:00+08:00' }] },
}

describe('hotStockEvidence', () => {
  it('区分已上榜、未上榜与来源不可用', () => {
    expect(hotStockEvidence(hot, 'ths_normal', '002242.SZ')).toEqual({ state: 'ranked', rank: 3 })
    expect(hotStockEvidence(hot, 'ths_normal', '600000.SH')).toEqual({ state: 'unranked' })
    expect(hotStockEvidence(hot, 'dfcf_renqi', '002242.SZ')).toEqual({ state: 'unavailable' })
    expect(hotStockEvidence(null, 'ths_trend', '002242.SZ')).toEqual({ state: 'unavailable' })
  })
})

describe('hotThemeByName', () => {
  it('只做精确同名匹配', () => {
    expect(hotThemeByName(hot, '芯片')?.rank).toBe(1)
    expect(hotThemeByName(hot, '芯片概念')).toBeNull()
    expect(hotThemeByName(null, '芯片')).toBeNull()
    expect(hotThemeByName({ ...hot, themes: { available: false, rows: [] } }, '芯片')).toBeNull()
  })
})

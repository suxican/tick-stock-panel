import { expect, it } from 'vitest'
import type { KplStock } from '@/lib/api'
import { filterTiersBySymbols, mergeReasons, sealText, themeSymbolsFor } from './kaipanlaReasons'

function stock(symbol: string, reason: string): KplStock {
  return {
    symbol, name: symbol, boards: 1, first_limit_at: null, reason, theme_code: '801001', plates: [reason],
    seal_amount: 1, max_seal: 2, reseal: false, label: null, price: 1, change_pct: 0.1,
  }
}

it('盘中只有开盘啦原因时显示开盘啦', () => {
  expect(mergeReasons(undefined, [stock('002451.SZ', '电气设备')]).get('002451.SZ'))
    .toEqual({ text: '电气设备', source: 'kpl', seal: { amount: 1, max: 2 } })
})

it('盘后两者都有时以 zzshare 为准, 开盘啦放 alt', () => {
  expect(mergeReasons(new Map([['002451.SZ', '智能电网订单']]), [stock('002451.SZ', '电气设备')]).get('002451.SZ'))
    .toEqual({ text: '智能电网订单', source: 'zzshare', alt: '电气设备', seal: { amount: 1, max: 2 } })
})

it('只有 zzshare 时保持原样, 空原因忽略', () => {
  const m = mergeReasons(new Map([['600001.SH', '算力']]), [stock('000001.SZ', '')])
  expect(m.get('600001.SH')).toEqual({ text: '算力', source: 'zzshare' })
  expect(m.has('000001.SZ')).toBe(false)
})

it('按题材成分过滤梯队, 空档去掉', () => {
  const tiers = [
    { boards: 2, count: 2, stocks: [{ symbol: 'A' }, { symbol: 'B' }] },
    { boards: 1, count: 1, stocks: [{ symbol: 'C' }] },
  ]
  expect(filterTiersBySymbols(tiers, ['B'])).toEqual([{ boards: 2, count: 1, stocks: [{ symbol: 'B' }] }])
  expect(filterTiersBySymbols(tiers, undefined)).toBe(tiers)
})

it('题材成分只在开盘啦快照与梯队同日时生效', () => {
  const snap = { date: '2026-11-17', themes: [{ code: '801', name: '芯片', count: 1, symbols: ['A'] }] }
  expect(themeSymbolsFor(snap, '801', '2026-11-17')).toEqual(['A'])
  expect(themeSymbolsFor(snap, '801', '2026-11-16')).toBeUndefined()
  expect(themeSymbolsFor(snap, undefined, '2026-11-17')).toBeUndefined()
  expect(themeSymbolsFor(undefined, '801', '2026-11-17')).toBeUndefined()
})

it('开盘啦封单文案', () => {
  expect(sealText({ amount: 131_607_384, max: 231_740_112 })).toBe('开盘啦封单 1.32亿 · 最大 2.32亿')
  expect(sealText({ amount: 0, max: 50_000_000 })).toBe('开盘啦封单 0 · 最大 5000万')
})

import { expect, it } from 'vitest'
import type { KplStock, MeozLimitReason } from '@/lib/api'
import { detailLines, filterTiersBySymbols, mergeReasons, sealText, themeSymbolsFor } from './kaipanlaReasons'

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

function meoz(symbol: string, over: Partial<MeozLimitReason> = {}): [string, MeozLimitReason] {
  return [symbol, { symbol, kpl: '机器人概念', jygs: '机器人轴承+军工', jygs_detail: '详情', xgb: '公司业务说明', ...over }]
}

it('猫爪优先于开盘啦非官方接口, 主文本取开盘啦字段, 其余各家进展开区', () => {
  const m = mergeReasons(undefined, [stock('000678.SZ', '减速器')], new Map([meoz('000678.SZ')]))
  const reason = m.get('000678.SZ')!
  expect(reason).toMatchObject({ text: '机器人概念', source: 'meoz', alt: '减速器', seal: { amount: 1, max: 2 } })
  expect(detailLines(reason)).toEqual([{ label: '韭菜公社', text: '机器人轴承+军工' }, { label: '选股宝', text: '公司业务说明' }])
})

it('猫爪为主, zzshare 作补充并去掉与主因重复的开头', () => {
  const m = mergeReasons(new Map([['000678.SZ', '机器人概念；1. 据 11 月 3 日记录表…']]), undefined, new Map([meoz('000678.SZ')]))
  const reason = m.get('000678.SZ')!
  expect(reason).toMatchObject({ text: '机器人概念', source: 'meoz' })
  expect(detailLines(reason)[0]).toEqual({ label: 'ZZShare（盘后）', text: '1. 据 11 月 3 日记录表…' })
})

it('猫爪缺失时 zzshare 仍作主文本; 只有选股宝时也能显示', () => {
  const m = mergeReasons(new Map([['000001.SZ', '算力；详细']]), undefined,
    new Map([meoz('600001.SH', { kpl: null, jygs: null })]))
  expect(m.get('000001.SZ')).toEqual({ text: '算力；详细', source: 'zzshare' })
  expect(detailLines(m.get('000001.SZ')!)).toEqual([])
  expect(m.get('600001.SH')).toMatchObject({ text: '公司业务说明', source: 'meoz' })
  expect(detailLines(m.get('600001.SH')!)).toEqual([])
})

it('猫爪三家均为空时不生成归因, 回落 zzshare', () => {
  const m = mergeReasons(new Map([['000001.SZ', '旧原因']]), undefined,
    new Map([meoz('000001.SZ', { kpl: null, jygs: null, xgb: null }), meoz('000002.SZ', { kpl: null, jygs: null, xgb: null })]))
  expect(m.get('000001.SZ')).toEqual({ text: '旧原因', source: 'zzshare' })
  expect(m.has('000002.SZ')).toBe(false)
})

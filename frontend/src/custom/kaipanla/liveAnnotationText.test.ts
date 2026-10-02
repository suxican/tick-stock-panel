import { describe, expect, it } from 'vitest'
import { extractLiveHighlights } from './liveAnnotationText'

describe('extractLiveHighlights', () => {
  it.each([
    ['医药板块持续走强，南华生物涨停。', '医药板块持续走强', 'up'],
    ['芯片概念震荡走弱，权重股下跌。', '芯片概念震荡走弱', 'down'],
    ['地产链持续回流，金地集团上涨。', '地产链持续回流', 'up'],
    ['光伏板块异动拉升，弘元绿能涨停。', '光伏板块异动拉升', 'up'],
    ['银行板块震荡走强，中国银行再创新高。', '银行板块震荡走强', 'up'],
    ['AI硬件（PCB）开盘走弱，澳弘电子下跌。', 'AI硬件（PCB）开盘走弱', 'down'],
    ['数字货币概念持续走强。', '数字货币概念持续走强', 'up'],
    ['短线情绪明显修复。', '短线情绪明显修复', 'up'],
    ['午后上证指数小幅回升。', '午后上证指数小幅回升', 'up'],
    ['深成指、创业板指回落翻绿。', '深成指、创业板指回落翻绿', 'down'],
    ['锂电池竞价延续强势，相关股票高开。', '锂电池竞价延续强势', 'up'],
    ['地产链竞价弱势，相关股票低开。', '地产链竞价弱势', 'down'],
    ['医药相对强势，相关股票上涨。', '医药相对强势', 'up'],
    ['风电板块快速下挫。', '风电板块快速下挫', 'down'],
    ['大盘盘中跳水。', '大盘盘中跳水', 'down'],
    ['机器人概念分化走弱。', '机器人概念分化走弱', 'down'],
  ] as const)('keeps the source clause in %s', (comment, label, direction) => {
    expect(extractLiveHighlights(comment)).toEqual([{ label, direction }])
  })

  it.each([
    null, undefined, 0, true, {}, [], '', '   ',
    '贵州茅台快速拉升，中国银行上涨。',
    '医药近岸蛋白涨超5%再创历史新高。',
    '汽车板块江淮汽车涨超6%创趋势新高。',
    '地产链深物业A地天板涨停，三连板。',
    '风电板块延续活跃，相关个股跟涨。',
    '短线情绪分化，部分个股跌停。',
    'AI应用开盘分化，部分个股涨停。',
    '市场成交突破1万亿，预计全天成交更多。',
    '医药板块有望修复。',
    '预计医药板块持续走强。',
    '银行板块或将走弱。',
    '风电板块可能拉升。',
    '如果指数回升，医药板块持续走强。',
    '放量后才会医药板块拉升。',
    '医药板块并未走弱，反而持续走强。',
    '医药板块没有持续走强。',
    '医药板块走强又回落。',
    '医药板块先走强后走弱。',
    '指数上涨与下跌并存。',
    '关注医药板块持续走强。',
    '去年医药板块持续走强。',
    '上月芯片板块震荡走弱。',
    '前月芯片板块震荡走弱。',
    '医药板块上涨动力不足。',
    '医药板块上涨的动能不足。',
    '医药板块走强意愿不高。',
    '昨日医药板块持续走强。',
    '上周芯片板块震荡走弱。',
    '此前医药板块持续走强。',
    '前日银行板块走弱。',
    '回顾昨日：医药板块持续走强，银行板块震荡走弱。',
    '展望后市：医药板块持续走强。',
    '银行股中国银行上涨。',
    '医药板块走强并非事实。',
    '医药板块走强信号仍未出现。',
    '医药板块上涨空间有限。',
    '医药板块持续走强的概率上升。',
    '医药板块拉升空间扩大。',
  ])('does not turn uncertain or stock-only text into a market judgment: %s', comment => {
    expect(extractLiveHighlights(comment)).toEqual([])
  })

  it('keeps separate strong and weak clauses and limits each message to two highlights', () => {
    expect(extractLiveHighlights('医药板块持续走强，但芯片概念震荡走弱。地产链回流。')).toEqual([
      { label: '医药板块持续走强', direction: 'up' },
      { label: '但芯片概念震荡走弱', direction: 'down' },
    ])
  })

  it('keeps current source clauses after a separate historical sentence', () => {
    expect(extractLiveHighlights('昨日银行板块走弱。医药板块持续走强，芯片板块震荡走弱。')).toEqual([
      { label: '医药板块持续走强', direction: 'up' },
      { label: '芯片板块震荡走弱', direction: 'down' },
    ])
  })

  it('returns exact source substrings of at most 28 characters without invented text', () => {
    const comment = '午后人工智能芯片制造设备板块持续走强且相关领域资金流动和成交出现明显变化。'
    const highlights = extractLiveHighlights(comment)
    expect(highlights).toHaveLength(1)
    expect(highlights[0].direction).toBe('up')
    expect(comment).toContain(highlights[0].label)
    expect(Array.from(highlights[0].label).length).toBeLessThanOrEqual(28)
    expect(highlights[0].label).toBe('午后人工智能芯片制造设备板块持续走强')
  })

  it('preserves parentheses and source wording while trimming outer whitespace', () => {
    expect(extractLiveHighlights('  AI硬件（PCB）开盘走弱  ')).toEqual([
      { label: 'AI硬件（PCB）开盘走弱', direction: 'down' },
    ])
  })

  it('does not repeat an identical clause', () => {
    expect(extractLiveHighlights('医药板块持续走强。医药板块持续走强。')).toEqual([
      { label: '医药板块持续走强', direction: 'up' },
    ])
  })
})

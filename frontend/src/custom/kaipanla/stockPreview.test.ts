import { expect, it } from 'vitest'
import { previewStock } from './stockPreview'

it.each([
  ['600825', '600825.SH'], ['sh603396', '603396.SH'], [' 688185.SH ', '688185.SH'],
  ['000001', '000001.SZ'], ['SZ002799', '002799.SZ'], ['300746.SZ', '300746.SZ'],
  ['430047', '430047.BJ'], ['BJ832982', '832982.BJ'], ['872374.BJ', '872374.BJ'], ['920001', '920001.BJ'],
])('normalizes the explicit stock field %s', (raw, symbol) => {
  expect(previewStock(raw, ' 股票名称 ')).toEqual({ symbol, name: '股票名称' })
})

it.each([null, undefined, 600825, '', '710', 'SH000001', '399001.SZ', '801045', 'CUSTOM',
  '600825.SZ', 'SZ600825.SH', 'SH600825.SZ', '920001.SH', '600825.SH/other', '000001.SZ?x=1',
])('keeps invalid, index and conflicting codes non-interactive: %s', raw => {
  expect(previewStock(raw, '名称')).toBeNull()
})

it('can preview a valid code without inventing a missing stock name', () => {
  expect(previewStock('600825', null)).toEqual({ symbol: '600825.SH', name: undefined })
})

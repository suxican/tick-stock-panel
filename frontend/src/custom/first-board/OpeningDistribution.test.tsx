// @vitest-environment jsdom
import { renderToStaticMarkup } from 'react-dom/server'
import { expect, it } from 'vitest'
import type { FirstBoardOpeningDistribution } from '@/lib/api'
import { OpeningDistribution } from './OpeningDistribution'

const sample: FirstBoardOpeningDistribution = {
  sample_count: 12, open_count: 10, history_excluded_count: 1, up_count: 6,
  up_ratio: .6, mean_open_premium: -.0123,
  bins: ['>10%', '8～10%', '6～8%', '4～6%', '2～4%', '0～2%', '平开',
    '-2～0%', '-4～-2%', '-6～-4%', '-8～-6%', '-10～-8%', '<-10%']
    .map((label, i) => ({ label, count: [1, 0, 0, 0, 0, 5, 1, 2, 0, 0, 0, 0, 1][i] })),
}

function render(data?: FirstBoardOpeningDistribution, refreshFailed = false) {
  const host = document.createElement('div')
  host.innerHTML = renderToStaticMarkup(<OpeningDistribution feedback={{ status: 'partial', opening_distribution: data }} refreshFailed={refreshFailed} />)
  return host
}

it('shows the non-one-word cohort and its observed open returns, including flat and tail bins', () => {
  const host = render(sample)
  expect(host.querySelector('h3')?.textContent).toContain('12 只')
  expect(host.textContent).toContain('有效开盘 10 / 12 只')
  expect(host.textContent).toContain('历史待确认 1 只')
  expect(host.querySelector('figure[aria-label^="打板成功率"]')?.textContent).toContain('60.00%')
  expect(host.querySelector('figure[aria-label^="打板盈利率"]')?.textContent).toContain('-1.23%')
  const counts = Object.fromEntries([...host.querySelectorAll('dt')].map(row => [row.textContent, row.nextElementSibling?.textContent]))
  expect(counts).toMatchObject({ '>10%': '1', '平开': '1', '<-10%': '1', '0～2%': '5' })
})

it('keeps unavailable data distinct from a genuine zero result', () => {
  expect(render().querySelector('figure')).toBeNull()
  const waiting = render({ ...sample, open_count: 0, up_ratio: null, mean_open_premium: null })
  expect(waiting.textContent).toContain('等待有效开盘价')
  expect(waiting.querySelector('figure')).toBeNull()
  const flat = render({ ...sample, up_count: 0, up_ratio: 0, mean_open_premium: 0,
    bins: sample.bins.map(bin => ({ ...bin, count: bin.label === '平开' ? sample.open_count : 0 })),
  }, true)
  expect(flat.querySelectorAll('figure')).toHaveLength(2)
  expect(flat.querySelector('figure')?.textContent).toContain('0.00%')
  expect(flat.textContent).toContain('刷新失败，显示上次统计')
})

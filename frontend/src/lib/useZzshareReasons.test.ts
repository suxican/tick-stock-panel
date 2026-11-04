import { QueryClient, QueryObserver } from '@tanstack/react-query'
import { afterEach, expect, it, vi } from 'vitest'
import { api, ApiError, type ExtDataRowsResult } from './api'
import { QK } from './queryKeys'
import { zzshareReasonsOptions } from './useZzshareReasons'

const day = '2026-11-03'
const table = 'zzshare_limit_reasons'
const snapshot: ExtDataRowsResult = {
  id: table, label: 'ZZShare 涨停归因', mode: 'timeseries', date: day,
  total: 1, limit: 20000, fields: [],
  rows: [{ symbol: '301487.SZ', trade_date: day, reason: '当日原因' }],
}
const client = new QueryClient()
afterEach(() => { client.clear(); vi.restoreAllMocks() })

it('无日期不发请求；同日同表共用行缓存，首次同步后用现有前缀就能刷新', async () => {
  const read = vi.spyOn(api, 'extDataRows').mockResolvedValue(snapshot)
  const off = new QueryObserver(client, zzshareReasonsOptions(null)).subscribe(() => {})
  expect(read).not.toHaveBeenCalled()
  off()

  await Promise.all([client.fetchQuery(zzshareReasonsOptions(day)), client.fetchQuery(zzshareReasonsOptions(day))])
  expect(client.getQueryData(QK.extDataRows(table, day, 20000))).toEqual(snapshot)
  await client.fetchQuery(zzshareReasonsOptions(day))
  expect(read).toHaveBeenCalledTimes(1)
  await client.invalidateQueries({ queryKey: QK.extDataRows(table).slice(0, 2) })
  await client.fetchQuery(zzshareReasonsOptions(day))
  expect(read).toHaveBeenCalledTimes(2)
})

it('未建表与缺失日期都可正常读空；错误响应和混入其他日期的记录不能当成当日原因', async () => {
  const read = vi.spyOn(api, 'extDataRows').mockRejectedValueOnce(new ApiError('无表', 404))
  const opts = zzshareReasonsOptions(day)
  expect(await client.fetchQuery(opts)).toBeNull()
  read.mockResolvedValueOnce({ ...snapshot, total: 0, rows: [] })
  expect((await client.fetchQuery(opts))?.rows).toEqual([])
  read.mockRejectedValueOnce(new ApiError('读取失败', 500))
  await expect(client.fetchQuery(opts)).rejects.toThrow('读取失败')
  read.mockResolvedValueOnce({ ...snapshot, date: '2026-11-02' })
  await expect(client.fetchQuery(opts)).rejects.toThrow('交易日')
  read.mockResolvedValueOnce({ ...snapshot, rows: [{ ...snapshot.rows[0], trade_date: '2026-11-02' }] })
  await expect(client.fetchQuery(opts)).rejects.toThrow('交易日')
})

/**
 * 盘中快照每 5 秒刷新，按分数或涨幅排序的行会自己换位，导致误点。
 * 这里让显示顺序保持上一次的顺序，数值用最新的；新行追加在末尾，消失的行去掉，
 * 只有调用方判定需要重排时才传入 null 重新取实时顺序。
 */
export function applyFrozenOrder<T>(live: T[], previous: string[] | null, keyOf: (row: T) => string): { rows: T[]; keys: string[]; changed: boolean } {
  const liveKeys = live.map(keyOf)
  if (!previous?.length) return { rows: live, keys: liveKeys, changed: false }
  const byKey = new Map(live.map((row, index) => [liveKeys[index], row]))
  const kept = previous.filter((key, index) => byKey.has(key) && previous.indexOf(key) === index)
  const keptSet = new Set(kept)
  const rows = [...kept.map(key => byKey.get(key)!), ...live.filter((_, index) => !keptSet.has(liveKeys[index]))]
  const keys = rows.map(keyOf)
  return { rows, keys, changed: keys.some((key, index) => key !== liveKeys[index]) }
}

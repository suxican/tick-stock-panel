import { afterEach, expect, it, vi } from 'vitest'
import { newKey } from './positionTShared'

afterEach(() => vi.unstubAllGlobals())

// 回归: HTTP 非 localhost 部署下 crypto.randomUUID 不存在, 曾导致 "扩展 official.lots 暂时不可用"
it('HTTP 部署缺少 crypto.randomUUID 时仍能生成幂等键', () => {
  vi.stubGlobal('crypto', {})
  const a = newKey()
  const b = newKey()
  expect(a).toMatch(/^[a-zA-Z0-9_-]{8,}$/)
  expect(a).not.toBe(b)
})

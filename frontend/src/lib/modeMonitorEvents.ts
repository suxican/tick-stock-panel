export type ModeMonitorType = 'first_board' | 'huichun'

export function isModeMonitorType(type: string | undefined): type is ModeMonitorType {
  return type === 'first_board' || type === 'huichun'
}

export const DEFAULT_MODE_EVENTS: Record<ModeMonitorType, string[]> = {
  first_board: ['buy_candidate', 'broken', 'exit_candidate'],
  huichun: ['a0_confirmed'],
}

export const MODE_EVENT_OPTIONS: Record<ModeMonitorType, { key: string; label: string }[]> = {
  first_board: [
    { key: 'buy_candidate', label: '临近涨停买入候选' },
    { key: 'approaching', label: '临近涨停但条件未通过' },
    { key: 'sealed', label: '涨停价观察' },
    { key: 'broken', label: '开板观察' },
    { key: 'watch', label: '条件失效' },
    { key: 'invalid', label: '数据或条件失效' },
    { key: 'exit_candidate', label: '次日退出提示' },
  ],
  huichun: [
    { key: 'a0_confirmed', label: 'A0 确认' },
    { key: 'pending_cross', label: '待金叉观察' },
  ],
}

export function modeEventLabel(type: ModeMonitorType, event: string): string {
  return MODE_EVENT_OPTIONS[type].find(option => option.key === event)?.label ?? '模式事件'
}

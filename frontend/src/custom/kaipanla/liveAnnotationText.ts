const industries = [
  '数字货币', 'AI应用', 'AI硬件', '人工智能', '机器人', '房地产', '地产链', '地产',
  '玻璃基板', '磷化铟', '算电协同', '文化传媒', '大消费', '固态电池', '锂电池',
  '半导体', '创新药', '新能源', '医药', '风电', '光伏', '银行', '芯片', '化工',
  '消费', '农业', '黄酒', '造船', '军工', '煤炭', '钢铁', '有色', '证券', '电力',
  '通信', '计算机', '家电', '汽车', '中药',
].sort((left, right) => right.length - left.length).join('|')
const subject = `(?:[A-Za-z0-9\\u4e00-\\u9fff]{2,14}?(?:板块|概念|指数)`
  + `|(?:${industries})(?:[（(][^）)]{1,12}[）)])?(?:板块|概念)?`
  + '|短线情绪|三大指数|深证成指|深成指|创业板指|科创综指|科创50|沪深300|大盘|市场)'
const subjectPrefix = new RegExp(`^(?:(?:今日|当前|午后|早盘|开盘|盘中|尾盘|竞价|但|而|其中|同时|此外|随后)\\s*)*${subject}(?:[、和与及]${subject})*`)
const up = /走强|拉升|回流|修复|回升|上行|上涨|强势|翻红|反弹/
const down = /走弱|回落|下挫|跳水|下行|下跌|弱势|翻绿/
const boundDirection = /^(?:(?:全天|早盘|开盘|盘中|午后|尾盘|竞价|震荡|持续|延续|明显|逐步|继续|再度|再次|快速|加速|异动|集体|小幅|大幅|相对|总体|整体|分化|开始|出现|有所)\s*)*(走强|拉升|回流|修复|回升|上行|上涨|强势|翻红|反弹|走弱|回落|下挫|跳水|下行|下跌|弱势|翻绿)/
const uncertain = /预计|预测|预期|有望|或将|可能|或许|也许|等待|关注|如果|若|假如|倘若|一旦|只要|除非|否则|才会|将会|明日|明天|下周|后续|展望|后市/
const historical = /昨日|昨天|此前|(?:上|前)(?:个)?(?:周|月|季度)|去年|前年|前日|前天|前一(?:个)?(?:交易)?日|上个交易日|回顾/
const negated = /尚未|并未|并非|并不|未能|未见|未出现|未发生|未形成|未确认|未兑现|没有|不(?:是|会|再|能|宜|代表|意味|走强|走弱|回落|拉升|修复|回流|一定|排除)/
const directionNoun = /(?:走强|走弱|拉升|回流|修复|回升|上行|上涨|强势|翻红|反弹|回落|下挫|跳水|下行|下跌|弱势|翻绿)(?:的)?(?:空间|概率|可能性|潜力|条件|预案|动力|动能|意愿)/

/** 只摘录有明确行业/市场主体及方向的原文；没有文字证据时不补行情判断。 */
export function extractLiveHighlights(comment: unknown): { label: string; direction: 'up' | 'down' }[] {
  if (typeof comment !== 'string' || !comment.trim()) return []
  const highlights: { label: string; direction: 'up' | 'down' }[] = []
  const seen = new Set<string>()
  for (const sentence of comment.split(/[。！？!?；;\r\n]+/)) {
    // 历史、条件、预测和否定可作用于逗号后的分句，保守排除整个句子。
    if (uncertain.test(sentence) || historical.test(sentence) || negated.test(sentence) || directionNoun.test(sentence)) continue
    for (const raw of sentence.split(/[，,]+/)) {
      const clause = raw.trim()
      const prefix = subjectPrefix.exec(clause)?.[0]
      if (!prefix) continue
      const rest = clause.slice(prefix.length)
      const bound = boundDirection.exec(rest)
      if (!bound || up.test(rest) === down.test(rest)) continue
      const direction = up.test(rest) ? 'up' : 'down'
      // 长句仅截到明确方向词末尾，不生成省略号或改写原文。
      const label = Array.from(clause).length <= 28 ? clause : clause.slice(0, prefix.length + bound[0].length).trim()
      if (Array.from(label).length > 28 || seen.has(label)) continue
      seen.add(label)
      highlights.push({ label, direction })
      if (highlights.length === 2) return highlights
    }
  }
  return highlights
}

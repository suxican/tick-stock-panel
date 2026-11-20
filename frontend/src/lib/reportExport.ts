/**
 * 分析报告导出(零依赖): Markdown / PNG 图片 / PDF。
 *
 * - Markdown: 直接用报告原文生成 Blob。
 * - 图片: 克隆已渲染的报告 DOM, 内联计算样式后经 SVG foreignObject 绘制到 canvas。
 * - PDF: 把长图按 A4 比例切页, 以 JPEG 嵌入自写的最小 PDF(页面为图像, 文字不可选中)。
 */

export type ReportExportFormat = 'md' | 'png' | 'pdf'

const A4_W = 595.28
const A4_H = 841.89
/** 浏览器 canvas 单边安全上限(Chrome 为 32767, 取保守值) */
const MAX_CANVAS_SIDE = 16000

function pad2(n: number): string {
  return String(n).padStart(2, '0')
}

/** 生成导出文件名: 名称_代码_分析报告_YYYYMMDD.ext(非法字符替换为下划线) */
export function buildReportFileName(
  name: string, symbol: string, ext: ReportExportFormat, when?: string | Date | null,
): string {
  let d = when instanceof Date ? when : when ? new Date(when) : new Date()
  if (Number.isNaN(d.getTime())) d = new Date()
  const stamp = `${d.getFullYear()}${pad2(d.getMonth() + 1)}${pad2(d.getDate())}`
  const base = [name, symbol, '分析报告', stamp]
    .filter(Boolean)
    .join('_')
    .replace(/[\\/:*?"<>|\s]+/g, '_')
  return `${base}.${ext}`
}

function saveBlob(blob: Blob, fileName: string): void {
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = fileName
  document.body.appendChild(a)
  a.click()
  a.remove()
  setTimeout(() => URL.revokeObjectURL(url), 1000)
}

export function downloadMarkdown(content: string, fileName: string): void {
  saveBlob(new Blob([content], { type: 'text/markdown;charset=utf-8' }), fileName)
}

// ===== DOM → canvas =====

function inlineStyles(src: Element, dst: Element): void {
  const cs = getComputedStyle(src)
  const style = (dst as HTMLElement).style
  for (let i = 0; i < cs.length; i++) {
    const p = cs[i]
    style.setProperty(p, cs.getPropertyValue(p), cs.getPropertyPriority(p))
  }
  for (let i = 0; i < src.children.length; i++) {
    if (dst.children[i]) inlineStyles(src.children[i], dst.children[i])
  }
}

export interface RenderOptions {
  /** 顶部标题(名称/代码等), 空则不画 */
  title?: string
  subtitle?: string
  background?: string
  /** 报告内容两侧留白(px) */
  padding?: number
}

/** 把已渲染的报告节点绘制成 canvas(整段内容, 不受滚动容器裁剪) */
export async function renderNodeToCanvas(node: HTMLElement, opts: RenderOptions = {}): Promise<HTMLCanvasElement> {
  const padding = opts.padding ?? 32
  const background = opts.background || getComputedStyle(document.body).backgroundColor || '#0b0d12'
  const innerW = Math.max(320, node.offsetWidth)
  const width = innerW + padding * 2

  const clone = node.cloneNode(true) as HTMLElement
  inlineStyles(node, clone)
  clone.style.width = `${innerW}px`

  const fg = getComputedStyle(node).color
  const wrap = document.createElement('div')
  wrap.setAttribute('xmlns', 'http://www.w3.org/1999/xhtml')
  wrap.style.cssText = `box-sizing:border-box;width:${width}px;padding:${padding}px;background:${background};color:${fg};font-family:${getComputedStyle(node).fontFamily};`
  if (opts.title) {
    const h = document.createElement('div')
    h.style.cssText = 'font-size:20px;font-weight:700;margin-bottom:4px;'
    h.textContent = opts.title
    wrap.appendChild(h)
    if (opts.subtitle) {
      const s = document.createElement('div')
      s.style.cssText = 'font-size:12px;opacity:.6;margin-bottom:16px;'
      s.textContent = opts.subtitle
      wrap.appendChild(s)
    }
  }
  wrap.appendChild(clone)

  // 量高: 挂到屏幕外测量, 避免依赖原节点滚动状态
  const probe = wrap.cloneNode(true) as HTMLElement
  probe.style.position = 'fixed'
  probe.style.left = '-100000px'
  probe.style.top = '0'
  document.body.appendChild(probe)
  const height = Math.ceil(probe.getBoundingClientRect().height)
  probe.remove()

  const xml = new XMLSerializer().serializeToString(wrap)
  const svg =
    `<svg xmlns="http://www.w3.org/2000/svg" width="${width}" height="${height}">` +
    `<foreignObject width="100%" height="100%">${xml}</foreignObject></svg>`
  const img = new Image()
  img.decoding = 'async'
  await new Promise<void>((resolve, reject) => {
    img.onload = () => resolve()
    img.onerror = () => reject(new Error('报告渲染失败'))
    img.src = `data:image/svg+xml;charset=utf-8,${encodeURIComponent(svg)}`
  })

  const dpr = Math.max(1, Math.min(2, window.devicePixelRatio || 1))
  const scale = Math.min(dpr, MAX_CANVAS_SIDE / height, MAX_CANVAS_SIDE / width)
  const canvas = document.createElement('canvas')
  canvas.width = Math.floor(width * scale)
  canvas.height = Math.floor(height * scale)
  const ctx = canvas.getContext('2d')
  if (!ctx) throw new Error('当前环境不支持 canvas')
  ctx.fillStyle = background
  ctx.fillRect(0, 0, canvas.width, canvas.height)
  ctx.drawImage(img, 0, 0, canvas.width, canvas.height)
  return canvas
}

function canvasToBlob(canvas: HTMLCanvasElement, type: string, quality?: number): Promise<Blob> {
  return new Promise((resolve, reject) => {
    canvas.toBlob(b => (b ? resolve(b) : reject(new Error('图片生成失败'))), type, quality)
  })
}

export async function downloadPng(node: HTMLElement, fileName: string, opts?: RenderOptions): Promise<void> {
  const canvas = await renderNodeToCanvas(node, opts)
  saveBlob(await canvasToBlob(canvas, 'image/png'), fileName)
}

// ===== PDF =====

export interface PdfPageImage {
  jpeg: Uint8Array
  /** 像素尺寸 */
  width: number
  height: number
}

/** 最小 PDF 写入器: 每页一张铺满 A4 的 JPEG */
export function buildPdf(pages: PdfPageImage[]): Uint8Array {
  const enc = new TextEncoder()
  const chunks: Uint8Array[] = []
  const offsets: number[] = []
  let length = 0
  const push = (b: Uint8Array | string) => {
    const bytes = typeof b === 'string' ? enc.encode(b) : b
    chunks.push(bytes)
    length += bytes.length
  }
  const beginObj = (n: number) => {
    offsets[n] = length
    push(`${n} 0 obj\n`)
  }

  push('%PDF-1.4\n')
  const objCount = 2 + pages.length * 3
  beginObj(1)
  push('<< /Type /Catalog /Pages 2 0 R >>\nendobj\n')
  beginObj(2)
  const kids = pages.map((_, i) => `${3 + i * 3} 0 R`).join(' ')
  push(`<< /Type /Pages /Kids [${kids}] /Count ${pages.length} >>\nendobj\n`)

  pages.forEach((p, i) => {
    const pageId = 3 + i * 3
    const contentId = pageId + 1
    const imageId = pageId + 2
    beginObj(pageId)
    push(
      `<< /Type /Page /Parent 2 0 R /MediaBox [0 0 ${A4_W} ${A4_H}] ` +
      `/Resources << /XObject << /Im0 ${imageId} 0 R >> >> /Contents ${contentId} 0 R >>\nendobj\n`,
    )
    const content = `q ${A4_W} 0 0 ${A4_H} 0 0 cm /Im0 Do Q`
    beginObj(contentId)
    push(`<< /Length ${content.length} >>\nstream\n${content}\nendstream\nendobj\n`)
    beginObj(imageId)
    push(
      `<< /Type /XObject /Subtype /Image /Width ${p.width} /Height ${p.height} ` +
      `/ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /DCTDecode /Length ${p.jpeg.length} >>\nstream\n`,
    )
    push(p.jpeg)
    push('\nendstream\nendobj\n')
  })

  const xrefAt = length
  push(`xref\n0 ${objCount + 1}\n0000000000 65535 f \n`)
  for (let n = 1; n <= objCount; n++) push(`${String(offsets[n]).padStart(10, '0')} 00000 n \n`)
  push(`trailer\n<< /Size ${objCount + 1} /Root 1 0 R >>\nstartxref\n${xrefAt}\n%%EOF\n`)

  const out = new Uint8Array(length)
  let pos = 0
  for (const c of chunks) {
    out.set(c, pos)
    pos += c.length
  }
  return out
}

function dataUrlToBytes(url: string): Uint8Array {
  const bin = atob(url.slice(url.indexOf(',') + 1))
  const out = new Uint8Array(bin.length)
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i)
  return out
}

/** 把长图 canvas 按 A4 比例切成多页 */
function sliceCanvasToPages(canvas: HTMLCanvasElement, background: string): PdfPageImage[] {
  const pageH = Math.floor(canvas.width * (A4_H / A4_W))
  const pages: PdfPageImage[] = []
  for (let y = 0; y < canvas.height; y += pageH) {
    const slice = document.createElement('canvas')
    slice.width = canvas.width
    slice.height = pageH
    const ctx = slice.getContext('2d')
    if (!ctx) throw new Error('当前环境不支持 canvas')
    ctx.fillStyle = background
    ctx.fillRect(0, 0, slice.width, slice.height)
    ctx.drawImage(canvas, 0, y, canvas.width, Math.min(pageH, canvas.height - y), 0, 0, canvas.width, Math.min(pageH, canvas.height - y))
    pages.push({ jpeg: dataUrlToBytes(slice.toDataURL('image/jpeg', 0.92)), width: slice.width, height: slice.height })
  }
  return pages
}

export async function downloadPdf(node: HTMLElement, fileName: string, opts?: RenderOptions): Promise<void> {
  const background = opts?.background || getComputedStyle(document.body).backgroundColor || '#0b0d12'
  const canvas = await renderNodeToCanvas(node, { ...opts, background })
  const pdf = buildPdf(sliceCanvasToPages(canvas, background))
  saveBlob(new Blob([pdf as BlobPart], { type: 'application/pdf' }), fileName)
}

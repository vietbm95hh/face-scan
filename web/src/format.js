export function fmtDate(v, withTime = true) {
  if (!v) return '—'
  const d = new Date(v)
  if (Number.isNaN(d.getTime())) return String(v)
  const date = d.toLocaleDateString('vi-VN', { day: '2-digit', month: '2-digit', year: 'numeric' })
  if (!withTime) return date
  return `${date} ${d.toLocaleTimeString('vi-VN', { hour: '2-digit', minute: '2-digit' })}`
}

export function fmtNum(n) {
  if (n === null || n === undefined) return '0'
  return new Intl.NumberFormat('vi-VN').format(n)
}

export function timeAgo(v) {
  if (!v) return '—'
  const diff = (Date.now() - new Date(v).getTime()) / 1000
  if (Number.isNaN(diff)) return '—'
  if (diff < 60) return 'vừa xong'
  if (diff < 3600) return `${Math.floor(diff / 60)} phút trước`
  if (diff < 86400) return `${Math.floor(diff / 3600)} giờ trước`
  if (diff < 86400 * 30) return `${Math.floor(diff / 86400)} ngày trước`
  return fmtDate(v, false)
}

export function initials(name) {
  if (!name) return '?'
  const parts = name.trim().split(/\s+/)
  return (parts[0][0] + (parts.length > 1 ? parts[parts.length - 1][0] : '')).toUpperCase()
}

export function toIsoDate(d) {
  const p = (x) => String(x).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`
}

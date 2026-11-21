// Client gọi API FastAPI. Dev: Vite proxy /api + /media. Prod: VITE_API_BASE.
const BASE = (import.meta.env.VITE_API_BASE || '').replace(/\/$/, '')

export class ApiError extends Error {
  constructor(status, message, body) {
    super(message)
    this.status = status
    this.body = body
  }
}

function qs(params) {
  const sp = new URLSearchParams()
  for (const [k, v] of Object.entries(params || {})) {
    if (v === undefined || v === null || v === '') continue
    sp.append(k, v)
  }
  const s = sp.toString()
  return s ? `?${s}` : ''
}

async function request(method, path, { params, body } = {}) {
  const url = `${BASE}${path}${qs(params)}`
  const res = await fetch(url, {
    method,
    headers: body !== undefined ? { 'Content-Type': 'application/json' } : undefined,
    body: body !== undefined ? JSON.stringify(body) : undefined,
  })
  const text = await res.text()
  let data = null
  try {
    data = text ? JSON.parse(text) : null
  } catch {
    data = text
  }
  if (!res.ok) {
    const detail = data?.detail
    const msg =
      typeof detail === 'string'
        ? detail
        : Array.isArray(detail)
          ? detail.map((d) => d.msg || JSON.stringify(d)).join('; ')
          : `HTTP ${res.status}`
    throw new ApiError(res.status, msg, data)
  }
  return data
}

export const api = {
  health: () => request('GET', '/api/health'),

  groups: () => request('GET', '/api/groups'),
  group: (id) => request('GET', `/api/groups/${id}`),
  updateGroup: (id, body) => request('PATCH', `/api/groups/${id}`, { body }),

  posts: (params) => request('GET', '/api/posts', { params }),
  post: (id) => request('GET', `/api/posts/${id}`),
  updatePost: (id, body) => request('PATCH', `/api/posts/${id}`, { body }),
  bulkRead: (ids) => request('POST', '/api/posts/bulk-read', { body: { ids } }),
  deletePreview: (id) => request('GET', `/api/posts/${id}/delete-preview`),
  deletePost: (id) => request('DELETE', `/api/posts/${id}`),
  deletePosts: (ids) => request('DELETE', '/api/posts', { params: { confirm: true }, body: { ids } }),

  stats: (params) => request('GET', '/api/stats', { params }),
  jobs: (params) => request('GET', '/api/jobs', { params }),

  scanRequests: (params) => request('GET', '/api/scan', { params }),
  scanStatus: () => request('GET', '/api/scan/status'),
  createScan: (body) => request('POST', '/api/scan', { body }),
  cancelScan: (id) => request('DELETE', `/api/scan/${id}`),

  // Đối chiếu với Facebook (ô tìm kiếm)
  searchPreview: (body) => request('POST', '/api/search/preview', { body }),
  searchReview: (body) => request('POST', '/api/search/review', { body }),
  reviews: (params) => request('GET', '/api/search/reviews', { params }),
  review: (id, params) => request('GET', `/api/search/reviews/${id}`, { params }),
  pageCursors: (params) => request('GET', '/api/pages/cursors', { params }),
  forgetPageCursor: (key) => request('DELETE', `/api/pages/cursors/${encodeURIComponent(key)}`),
}

/** URL ảnh: /media/... đi qua proxy (dev) hoặc cùng origin (prod). */
export function mediaUrl(u) {
  if (!u) return null
  if (u.startsWith('http')) return u
  return `${BASE}${u}`
}

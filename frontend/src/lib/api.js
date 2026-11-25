// The backend API client. The token lives in sessionStorage only (gone when the tab closes) and is never logged.

const DEV = typeof location !== 'undefined' && location.port === '5173'
export const API_BASE = import.meta.env.VITE_API_BASE || (DEV ? 'http://127.0.0.1:8000' : '')

const KEY = 'aeisp.session'

export function session() {
  try {
    return JSON.parse(sessionStorage.getItem(KEY) || 'null')
  } catch {
    return null
  }
}

export function saveSession(s) {
  try {
    if (s) sessionStorage.setItem(KEY, JSON.stringify(s))
    else sessionStorage.removeItem(KEY)
  } catch {
    /* private mode: the session simply does not survive a reload */
  }
}

export class ApiError extends Error {
  constructor(status, code, message, details) {
    super(message)
    this.status = status
    this.code = code
    this.details = details
  }
}

export async function api(path, { method = 'GET', body, token, text = false } = {}) {
  const t = token ?? session()?.token
  const headers = { Accept: text ? 'text/plain' : 'application/json' }
  if (t) headers.Authorization = `Bearer ${t}`
  if (body !== undefined) headers['Content-Type'] = 'application/json'
  let res
  try {
    res = await fetch(API_BASE + path, { method, headers, body: body === undefined ? undefined : JSON.stringify(body) })
  } catch {
    throw new ApiError(0, 'offline', 'the backend does not answer (is the server running?)')
  }
  if (!res.ok) {
    let err = {}
    try {
      err = (await res.json()).error || {}
    } catch {
      /* not JSON */
    }
    throw new ApiError(res.status, err.code || String(res.status), err.message || res.statusText, err.details)
  }
  return text ? res.text() : res.json()
}

export const get = (p) => api(p)
export const post = (p, body = {}) => api(p, { method: 'POST', body })
export const patch = (p, body = {}) => api(p, { method: 'PATCH', body })
export const del = (p) => api(p, { method: 'DELETE' })
export const getText = (p) => api(p, { text: true })

export function wsUrl(token) {
  const base = API_BASE || location.origin
  return base.replace(/^http/, 'ws') + `/ws?token=${encodeURIComponent(token)}`
}

export const RANK = { viewer: 1, agent: 2, operator: 3 }
export const can = (role, need) => (RANK[role] || 0) >= RANK[need]

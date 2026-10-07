async function request(method, url, body, isForm = false) {
  const opts = { method, headers: {} }
  if (body !== undefined) {
    if (isForm) opts.body = body
    else {
      opts.headers['Content-Type'] = 'application/json'
      opts.body = JSON.stringify(body)
    }
  }
  const res = await fetch(url, opts)
  const text = await res.text()
  let data = null
  try { data = text ? JSON.parse(text) : null } catch { data = { detail: text } }
  if (!res.ok) {
    const detail = data?.detail
    const msg = typeof detail === 'string' ? detail : Array.isArray(detail) ? detail.map(d => d.msg).join('; ') : `Request failed (${res.status})`
    const err = new Error(msg)
    err.status = res.status
    throw err
  }
  return data
}

export const api = {
  health: () => request('GET', '/api/health'),
  createClaim: () => request('POST', '/api/claims', {}),
  getClaim: id => request('GET', `/api/claims/${id}`),
  updateClaim: (id, body) => request('PATCH', `/api/claims/${id}`, body),
  upload: (id, files) => {
    const fd = new FormData()
    for (const f of files) fd.append('files', f, f.name)
    return request('POST', `/api/claims/${id}/documents`, fd, true)
  },
  loadDemo: id => request('POST', `/api/claims/${id}/demo-documents`),
  replaceDocument: (docId, file) => {
    const fd = new FormData()
    fd.append('file', file, file.name)
    return request('POST', `/api/documents/${docId}/replace`, fd, true)
  },
  dismissDocument: docId => request('DELETE', `/api/documents/${docId}`),
  patchExpense: (expId, changes) => request('PATCH', `/api/expenses/${expId}`, changes),
  excludeExpense: expId => request('DELETE', `/api/expenses/${expId}`),
  resolveDuplicate: (expId, resolution) => request('POST', `/api/expenses/${expId}/duplicate-resolution`, { resolution }),
  policyOverride: (expId, action, justification) => request('POST', `/api/expenses/${expId}/policy-override`, { action, justification }),
  chat: (id, body) => request('POST', `/api/claims/${id}/chat`, body),
  summary: id => request('GET', `/api/claims/${id}/summary`),
  submit: id => request('POST', `/api/claims/${id}/submit`, { confirm: true }),
}

const SYMBOLS = { INR: '₹', USD: '$', EUR: '€', GBP: '£' }

export function money(v, currency = 'INR', decimals = false) {
  if (v === null || v === undefined || Number.isNaN(v)) return '—'
  const sym = SYMBOLS[currency] ?? `${currency} `
  const n = Number(v)
  const opts = { minimumFractionDigits: decimals || n % 1 ? 2 : 0, maximumFractionDigits: 2 }
  return `${n < 0 ? '-' : ''}${sym}${Math.abs(n).toLocaleString(currency === 'INR' ? 'en-IN' : 'en-US', opts)}`
}

export function shortDate(iso) {
  if (!iso) return '—'
  const d = new Date(iso + 'T00:00:00')
  if (Number.isNaN(d.getTime())) return iso
  return d.toLocaleDateString('en-GB', { day: 'numeric', month: 'short' })
}

export function longDate(iso) {
  if (!iso) return '—'
  const d = new Date(iso + 'T00:00:00')
  if (Number.isNaN(d.getTime())) return iso
  return d.toLocaleDateString('en-GB', { day: 'numeric', month: 'long', year: 'numeric' })
}

export const fval = (e, name) => e?.fields?.[name]?.value ?? null

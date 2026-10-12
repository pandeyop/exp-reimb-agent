import React, { useRef, useState } from 'react'
import { api } from '../api.js'
import { DOC_STATUS } from '../status.js'

const ACCEPT = '.jpg,.jpeg,.jfif,.png,.pdf,image/jpeg,image/png,application/pdf'

export default function ReceiptsPane({ snap, run, onSelect, disabled }) {
  const [drag, setDrag] = useState(false)
  const [uploading, setUploading] = useState(false)
  const input = useRef(null)
  const replaceInput = useRef(null)
  const replaceTarget = useRef(null)
  const claimId = snap.claim.id
  const docs = snap.documents

  async function upload(files) {
    if (!files?.length || disabled) return
    setUploading(true)
    try { await run(() => api.upload(claimId, [...files])) } catch { /* toast shown */ } finally { setUploading(false) }
  }

  async function loadDemo() {
    setUploading(true)
    try { await run(() => api.loadDemo(claimId)) } catch { /* toast */ } finally { setUploading(false) }
  }

  function onDrop(e) {
    e.preventDefault()
    setDrag(false)
    upload(e.dataTransfer.files)
  }

  function startReplace(docId) {
    replaceTarget.current = docId
    replaceInput.current?.click()
  }

  return (
    <div className="pane-inner">
      <div className="pane-head">
        <h2>Receipts</h2>
        <span className="muted small">{docs.length} uploaded</span>
      </div>

      {!disabled && (
        <div
          className={`dropzone ${drag ? 'drag' : ''} ${docs.length ? 'compact' : ''}`}
          onDragOver={e => { e.preventDefault(); setDrag(true) }}
          onDragLeave={() => setDrag(false)}
          onDrop={onDrop}
          onClick={() => input.current?.click()}
          role="button"
          tabIndex={0}
          onKeyDown={e => (e.key === 'Enter' || e.key === ' ') && input.current?.click()}
        >
          <div className="dz-icon" aria-hidden>⇪</div>
          <div className="dz-title">{uploading ? 'Uploading…' : 'Drop receipts here'}</div>
          <div className="dz-sub">Photos, scans or PDFs · JPG, PNG, PDF · up to 10 MB each</div>
          <input ref={input} type="file" multiple accept={ACCEPT} hidden onChange={e => { upload(e.target.files); e.target.value = '' }} />
        </div>
      )}
      {!disabled && !docs.length && (
        <button className="btn demo-btn" onClick={loadDemo} disabled={uploading}>
          Use the 12 demo receipts
        </button>
      )}

      <input ref={replaceInput} type="file" accept={ACCEPT} hidden onChange={e => {
        const f = e.target.files?.[0]
        if (f && replaceTarget.current) run(() => api.replaceDocument(replaceTarget.current, f), 'Re-uploaded — reading it again').catch(() => {})
        e.target.value = ''
      }} />

      <ul className="doc-list">
        {docs.map(d => {
          const st = DOC_STATUS[d.status] ?? DOC_STATUS.UPLOADED
          const busy = d.status === 'PROCESSING' || d.status === 'UPLOADED'
          const isImg = d.mediaType.startsWith('image/')
          return (
            <li key={d.id} className={`doc ${st.tone}`}>
              <button className="doc-main" onClick={() => d.expenseId && onSelect(d.expenseId)} disabled={!d.expenseId} title={d.filename}>
                <span className="thumb">
                  {isImg ? <img src={d.url} alt="" loading="lazy" /> : <span className="pdf">PDF{d.pages > 1 ? <small>{d.pages}p</small> : null}</span>}
                </span>
                <span className="doc-text">
                  <span className="doc-name">{d.filename}</span>
                  <span className={`doc-status ${st.tone}`}>
                    {busy && <span className="spinner" />} {st.label}
                  </span>
                  {d.error && <span className="doc-error">{d.error}</span>}
                </span>
              </button>
              {!disabled && d.status === 'FAILED' && (
                <div className="doc-actions">
                  <button className="link" onClick={() => startReplace(d.id)}>Re-upload</button>
                  <button className="link" onClick={() => run(() => api.dismissDocument(d.id), 'Removed').catch(() => {})}>Remove</button>
                </div>
              )}
            </li>
          )
        })}
      </ul>
    </div>
  )
}

import React, { useCallback, useEffect, useRef, useState } from 'react'
import { api, money } from './api.js'
import { CLAIM_STATUS } from './status.js'
import ReceiptsPane from './components/ReceiptsPane.jsx'
import ClaimPane from './components/ClaimPane.jsx'
import ChatPane from './components/ChatPane.jsx'
import ExpenseDrawer from './components/ExpenseDrawer.jsx'
import ReviewModal from './components/ReviewModal.jsx'
import SuccessScreen from './components/SuccessScreen.jsx'

const STORAGE_KEY = 'expense-agent:claimId'
const store = {
  get: () => { try { return localStorage.getItem(STORAGE_KEY) } catch { return null } },
  set: v => { try { localStorage.setItem(STORAGE_KEY, v) } catch { /* private mode */ } },
}

export default function App() {
  const [snap, setSnap] = useState(null)
  const [error, setError] = useState(null)
  const [selected, setSelected] = useState(null) // expense id
  const [reviewOpen, setReviewOpen] = useState(false)
  const [mobileTab, setMobileTab] = useState('claim')
  const [toast, setToast] = useState(null)
  const claimId = snap?.claim?.id
  const pollRef = useRef(null)

  const refresh = useCallback(async (id = claimId) => {
    if (!id) return
    try {
      const s = await api.getClaim(id)
      setSnap(s)
      setError(null)
      return s
    } catch (e) {
      setError(e.message)
    }
  }, [claimId])

  const newClaim = useCallback(async () => {
    const s = await api.createClaim()
    store.set(s.claim.id)
    setSelected(null)
    setReviewOpen(false)
    await refresh(s.claim.id)
  }, [refresh])

  // bootstrap
  useEffect(() => {
    (async () => {
      const fromUrl = new URLSearchParams(window.location.search).get('claim')
      const id = fromUrl || store.get()
      if (id) {
        try {
          const s = await api.getClaim(id)
          setSnap(s)
          store.set(id)
          return
        } catch { /* stale id: start fresh */ }
      }
      try { await newClaim() } catch (e) { setError(`Can't reach the backend: ${e.message}`) }
    })()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // poll while documents are processing
  const busy = snap && (snap.claim.status === 'PROCESSING' || snap.claim.dashboard.processing > 0 || snap.claim.status === 'SUBMITTING')
  useEffect(() => {
    clearInterval(pollRef.current)
    if (busy) pollRef.current = setInterval(() => refresh(), 700)
    return () => clearInterval(pollRef.current)
  }, [busy, refresh])

  const run = useCallback(async (fn, okMsg) => {
    try {
      const r = await fn()
      await refresh()
      if (okMsg) setToast({ text: okMsg, tone: 'ok' })
      return r
    } catch (e) {
      setToast({ text: e.message, tone: 'bad' })
      await refresh()
      throw e
    }
  }, [refresh])

  useEffect(() => {
    if (!toast) return
    const t = setTimeout(() => setToast(null), 4200)
    return () => clearTimeout(t)
  }, [toast])

  if (!snap) {
    return <div className="boot">{error ? <div className="boot-error">{error}<br /><small>Start the backend with <code>run.ps1</code> / <code>run.sh</code>.</small></div> : <div className="spinner big" />}</div>
  }

  const { claim } = snap
  const status = CLAIM_STATUS[claim.status] ?? CLAIM_STATUS.DRAFT
  const selectedExpense = snap.expenses.find(e => e.id === selected)
  const ready = claim.status === 'READY_TO_SUBMIT'
  const submitted = claim.status === 'SUBMITTED'

  return (
    <div className="app">
      <header className="topbar">
        <div className="brand">
          <span className="logo" aria-hidden>
            <svg viewBox="0 0 32 32" width="28" height="28"><rect width="32" height="32" rx="8" fill="currentColor" /><path d="M9 8h14v17l-3-2-2 2-2-2-2 2-2-2-3 2z" fill="#fff" /><path d="M12 13h8M12 17h6" stroke="currentColor" strokeWidth="2" /></svg>
          </span>
          <div>
            <div className="brand-name">Expense Agent</div>
            <div className="brand-tag">Drop your receipts. Answer only what the AI couldn't determine. Submit.</div>
          </div>
        </div>
        <div className="topbar-right">
          <span className={`ai-badge ${snap.ai.mode}`} title={snap.ai.mode === 'claude' ? `Documents are read live by ${snap.ai.model}` : 'No Anthropic credentials found: replaying recorded extractions for the demo receipts and parsing PDF text for others'}>
            <span className="dot" /> {snap.ai.mode === 'claude' ? `Claude · ${snap.ai.model}` : 'Offline demo mode'}
          </span>
          <span className="employee">{claim.employee.name} · {claim.employee.id}</span>
          <button className="btn ghost" onClick={newClaim}>+ New claim</button>
        </div>
      </header>

      <nav className="mobile-tabs" role="tablist">
        {[['receipts', 'Receipts'], ['claim', 'Claim'], ['chat', 'Assistant']].map(([k, l]) => (
          <button key={k} role="tab" aria-selected={mobileTab === k} className={mobileTab === k ? 'active' : ''} onClick={() => setMobileTab(k)}>
            {l}{k === 'chat' && snap.questions.length > 0 && <span className="count">{snap.questions.length}</span>}
          </button>
        ))}
      </nav>

      <main className="panes">
        <section className={`pane receipts ${mobileTab === 'receipts' ? 'show' : ''}`}>
          <ReceiptsPane snap={snap} run={run} onSelect={setSelected} disabled={submitted} />
        </section>
        <section className={`pane claim ${mobileTab === 'claim' ? 'show' : ''}`}>
          <ClaimPane snap={snap} run={run} onSelect={setSelected} />
        </section>
        <section className={`pane chat ${mobileTab === 'chat' ? 'show' : ''}`}>
          <ChatPane snap={snap} run={run} refresh={refresh} onSelect={setSelected} />
        </section>
      </main>

      <footer className="actionbar">
        <div className="actionbar-info">
          <span className={`pill ${status.tone}`}>{status.label.toUpperCase()}</span>
          <span className="total">{money(claim.totalAmount, claim.currency)}</span>
          <span className="muted small">
            {claim.dashboard.expenses} expense{claim.dashboard.expenses === 1 ? '' : 's'}
            {claim.blockingIssues.length > 0 && !submitted && <> · {claim.blockingIssues.length} blocking issue{claim.blockingIssues.length === 1 ? '' : 's'}</>}
          </span>
        </div>
        <div className="actionbar-buttons">
          <button className="btn" onClick={() => setReviewOpen(true)} disabled={!claim.dashboard.expenses}>Review claim</button>
          <button className="btn primary" onClick={() => setReviewOpen(true)} disabled={!ready} title={ready ? '' : 'Resolve the blocking issues first'}>
            Submit
          </button>
        </div>
      </footer>

      {selectedExpense && (
        <ExpenseDrawer key={selectedExpense.id} snap={snap} expense={selectedExpense} run={run} onClose={() => setSelected(null)} readOnly={submitted} />
      )}
      {reviewOpen && !submitted && <ReviewModal snap={snap} run={run} onClose={() => setReviewOpen(false)} />}
      {submitted && claim.submission && <SuccessScreen snap={snap} onNew={newClaim} />}
      {toast && <div className={`toast ${toast.tone}`} role="status">{toast.text}</div>}
    </div>
  )
}

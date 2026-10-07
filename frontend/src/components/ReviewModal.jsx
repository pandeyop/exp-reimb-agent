import React, { useEffect, useState } from 'react'
import { api, fval, money, shortDate, longDate } from '../api.js'

export default function ReviewModal({ snap, run, onClose }) {
  const { claim } = snap
  const [confirmed, setConfirmed] = useState(false)
  const [submitting, setSubmitting] = useState(false)
  const ready = claim.status === 'READY_TO_SUBMIT'
  const labels = Object.fromEntries(Object.entries(snap.policy.categories).map(([k, v]) => [k, v.label]))
  const active = snap.expenses.filter(e => !e.excluded)

  useEffect(() => {
    const onKey = k => k.key === 'Escape' && !submitting && onClose()
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose, submitting])

  async function submit() {
    setSubmitting(true)
    try {
      await run(() => api.submit(claim.id))
      onClose()
    } catch { /* toast shown; claim unchanged */ } finally { setSubmitting(false) }
  }

  return (
    <div className="modal-backdrop" onClick={() => !submitting && onClose()}>
      <div className="modal review" role="dialog" aria-modal="true" aria-label="Review claim" onClick={e => e.stopPropagation()}>
        <div className="modal-head">
          <h2>Review claim</h2>
          <button className="icon-btn" onClick={onClose} aria-label="Close">✕</button>
        </div>
        <div className="summary-doc">
          <div className="sum-row"><span>Claim</span><b>{claim.title}</b></div>
          <div className="sum-row"><span>Claim ID</span><b>{claim.id}</b></div>
          <div className="sum-row"><span>Employee</span><b>{claim.employee.name} ({claim.employee.id})</b></div>
          {claim.period && <div className="sum-row"><span>Period</span><b>{longDate(claim.period.from)} – {longDate(claim.period.to)}</b></div>}
          <div className="sum-rule" />
          {claim.categoryTotals.map(c => (
            <div key={c.category} className="sum-row"><span>{c.label} <span className="muted">×{c.count}</span></span><b>{money(c.amount)}</b></div>
          ))}
          <div className="sum-rule" />
          <div className="sum-row total"><span>TOTAL</span><b>{money(claim.totalAmount, claim.currency)}</b></div>
          <div className="sum-rule" />
          <div className="sum-row"><span>Receipts attached</span><b>{snap.documents.length}</b></div>
          <div className="sum-row"><span>Expenses</span><b>{active.length}</b></div>
          <div className="sum-row"><span>Policy warnings</span><b>{claim.warnings.length}</b></div>
          <div className="sum-row"><span>Blocking issues</span><b>{claim.blockingIssues.length}</b></div>
          <div className={`sum-status ${ready ? 'ok' : 'warn'}`}>STATUS: {ready ? 'READY TO SUBMIT' : claim.status.replaceAll('_', ' ')}</div>
        </div>

        {!ready && claim.blockingIssues.length > 0 && (
          <div className="banner warn">
            <b>Resolve these before submitting:</b>
            <ul>{claim.blockingIssues.map((b, i) => <li key={i}>{b.text}</li>)}</ul>
          </div>
        )}
        {claim.warnings.length > 0 && (
          <div className="banner info">
            <b>Non-blocking notes for your approver:</b>
            <ul>{claim.warnings.map((w, i) => <li key={i}>{w.text}</li>)}</ul>
          </div>
        )}

        <details className="review-lines">
          <summary>All {active.length} expenses</summary>
          <table className="exp-table compact">
            <thead><tr><th>Date</th><th>Merchant</th><th>Category</th><th>Purpose</th><th className="r">Reimburse</th></tr></thead>
            <tbody>
              {active.map(e => (
                <tr key={e.id}>
                  <td>{shortDate(fval(e, 'expenseDate'))}</td>
                  <td>{fval(e, 'merchant')}</td>
                  <td>{labels[e.category.value]}</td>
                  <td className="purpose">{fval(e, 'businessPurpose') || '—'}</td>
                  <td className="r">{money(e.reimbursableAmount)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </details>

        <label className="confirm">
          <input type="checkbox" checked={confirmed} onChange={e => setConfirmed(e.target.checked)} disabled={!ready} />
          I confirm these expenses were incurred for business and the information is accurate.
        </label>
        <div className="modal-actions">
          <button className="btn ghost" onClick={onClose} disabled={submitting}>Back</button>
          <button className="btn primary" onClick={submit} disabled={!ready || !confirmed || submitting}>
            {submitting ? 'Submitting…' : `Submit ${money(claim.totalAmount)}`}
          </button>
        </div>
      </div>
    </div>
  )
}

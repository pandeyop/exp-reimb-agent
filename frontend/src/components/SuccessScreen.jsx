import React from 'react'
import { money } from '../api.js'

export default function SuccessScreen({ snap, onNew }) {
  const { claim } = snap
  const s = claim.submission
  const m = claim.metrics
  return (
    <div className="modal-backdrop success-backdrop">
      <div className="modal success" role="dialog" aria-modal="true" aria-label="Claim submitted">
        <div className="success-check" aria-hidden>✓</div>
        <h2>Expense claim submitted</h2>
        <div className="success-title">{claim.title}</div>

        <div className="success-grid">
          <div><b>{snap.documents.length}</b><span>documents processed</span></div>
          <div><b>{s.expenseCount}</b><span>reimbursable expenses</span></div>
          <div><b>{money(s.totalAmount)}</b><span>claimed</span></div>
        </div>

        <div className="metric-block">
          <div className="metric"><b>{m.fieldsExtracted}</b> fields extracted automatically</div>
          <div className="metric"><b>{m.fieldsInferred}</b> fields intelligently inferred</div>
          <div className="metric"><b>{m.questionsAsked}</b> question{m.questionsAsked === 1 ? '' : 's'} asked{m.decisionsMade ? <> · {m.decisionsMade} decision{m.decisionsMade === 1 ? '' : 's'} confirmed</> : null}</div>
          <div className="metric"><b>{m.manualFieldsEntered}</b> field{m.manualFieldsEntered === 1 ? '' : 's'} entered by you</div>
        </div>
        <div className="avoided"><b>{m.manualEntryAvoidedPct}%</b> of manual data entry avoided</div>

        <div className="reference">
          <span>Claim reference</span>
          <b>{s.claimReference}</b>
          <span className="muted small">{s.approvalStage?.replaceAll('_', ' ').toLowerCase()} · sent to the (mock) corporate reimbursement API</span>
        </div>
        <button className="btn primary" onClick={onNew}>Start a new claim</button>
      </div>
    </div>
  )
}

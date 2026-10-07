import React, { useState } from 'react'
import { api, fval, money, shortDate } from '../api.js'
import { EXPENSE_STATUS, PIPELINE } from '../status.js'

function Tile({ icon, value, label, tone }) {
  return (
    <div className={`tile ${tone || ''}`}>
      <div className="tile-value">{icon && <span className="tile-icon">{icon}</span>}{value}</div>
      <div className="tile-label">{label}</div>
    </div>
  )
}

function Pipeline({ snap }) {
  const d = snap.claim.dashboard
  const seen = new Set(snap.events.map(e => e.stage))
  const busy = d.processing > 0
  const pct = d.receipts ? Math.round(((d.receipts - d.processing) / d.receipts) * 100) : 0
  let activeIdx = -1
  PIPELINE.forEach(([stage], i) => { if (seen.has(stage)) activeIdx = i })
  return (
    <div className="pipeline" aria-label="Agent pipeline">
      <div className="pipeline-steps">
        {PIPELINE.map(([stage, label], i) => (
          <div key={stage} className={`step ${seen.has(stage) ? 'done' : ''} ${busy && i === activeIdx + 1 ? 'active' : ''}`}>
            <span className="step-dot" />
            <span className="step-label">{label}</span>
          </div>
        ))}
      </div>
      {busy && (
        <div className="progress"><div className="progress-bar" style={{ width: `${pct}%` }} /><span>{d.receipts - d.processing} of {d.receipts} documents read</span></div>
      )}
    </div>
  )
}

function GroupTitle({ group, claimId, run, editable }) {
  const [editing, setEditing] = useState(false)
  const [value, setValue] = useState(group.title)
  if (!editing) {
    return (
      <h3 className="group-title">
        {group.title}
        {editable && <button className="link small" onClick={() => { setValue(group.title); setEditing(true) }} aria-label="Rename group">Rename</button>}
      </h3>
    )
  }
  const save = () => {
    setEditing(false)
    if (value.trim() && value.trim() !== group.title) run(() => api.updateClaim(claimId, { groupNames: { [group.id]: value.trim() } })).catch(() => {})
  }
  return (
    <input className="group-title-input" autoFocus value={value} onChange={e => setValue(e.target.value)}
      onBlur={save} onKeyDown={e => { if (e.key === 'Enter') save(); if (e.key === 'Escape') setEditing(false) }} />
  )
}

function statusNote(e) {
  if (e.status === 'POLICY_VIOLATION') {
    const r = e.policyResults.find(r => r.blocking)
    return r?.excess ? `Over by ${money(r.excess)}` : r?.title
  }
  if (e.status === 'NEEDS_INFORMATION') {
    const g = e.gaps[0]
    const name = g?.field === 'businessPurpose' ? 'Purpose' : g?.field === 'expenseDate' ? 'Date' : g?.field === 'attendees' ? 'Attendees' : g?.field
    return name ? `${name} needed` : 'Info needed'
  }
  if (e.status === 'POSSIBLE_DUPLICATE') return `${Math.round(e.duplicate.score * 100)}% match`
  if (e.status === 'WARNING') return e.policyResults.find(r => r.severity === 'warning')?.title
  if (e.policyDecision?.type === 'cap') return 'Capped at limit'
  return null
}

function ExpenseRow({ e, labels, onSelect }) {
  const st = EXPENSE_STATUS[e.status] ?? EXPENSE_STATUS.PROCESSING
  const note = statusNote(e)
  const capped = e.reimbursableAmount !== fval(e, 'total') && !e.excluded
  return (
    <tr className={`exp-row ${e.excluded ? 'excluded' : ''}`} onClick={() => onSelect(e.id)} tabIndex={0} onKeyDown={k => k.key === 'Enter' && onSelect(e.id)}>
      <td className="c-date">{shortDate(fval(e, 'expenseDate'))}</td>
      <td className="c-merchant">
        <div className="merchant">{fval(e, 'merchant') || <em className="muted">Unknown</em>}</div>
        {fval(e, 'businessPurpose') && <div className="sub" title={fval(e, 'businessPurpose')}>{fval(e, 'businessPurpose')}</div>}
      </td>
      <td className="c-cat">
        {labels[e.category.value] ?? e.category.value}
        {e.category.source !== 'user' && e.category.confidence < 0.9 && <span className="conf low" title="AI confidence">{Math.round(e.category.confidence * 100)}%</span>}
      </td>
      <td className="c-amt">
        {capped ? <><s className="muted">{money(fval(e, 'total'), fval(e, 'currency') || 'INR')}</s><br />{money(e.reimbursableAmount)}</> : money(fval(e, 'total'), fval(e, 'currency') || 'INR')}
      </td>
      <td className="c-status">
        <span className={`chip ${st.tone}`}><span aria-hidden>{st.icon}</span> {st.label}</span>
        {note && <div className="sub">{note}</div>}
      </td>
    </tr>
  )
}

export default function ClaimPane({ snap, run, onSelect }) {
  const { claim, expenses } = snap
  const d = claim.dashboard
  const labels = Object.fromEntries(Object.entries(snap.policy.categories).map(([k, v]) => [k, v.label]))
  const byId = Object.fromEntries(expenses.map(e => [e.id, e]))
  const excluded = expenses.filter(e => e.excluded)
  const maxCat = Math.max(1, ...claim.categoryTotals.map(c => c.amount))
  const [editingTitle, setEditingTitle] = useState(false)
  const [title, setTitle] = useState(claim.title)
  const editable = claim.status !== 'SUBMITTED'

  const saveTitle = () => {
    setEditingTitle(false)
    if (title.trim() && title.trim() !== claim.title) run(() => api.updateClaim(claim.id, { title: title.trim() })).catch(() => {})
  }

  if (!d.receipts) {
    return (
      <div className="pane-inner empty-claim">
        <div className="empty-art" aria-hidden>🧾</div>
        <h2>Start by dropping your receipts</h2>
        <p className="muted">The agent reads every document, categorises it, checks it against the reimbursement policy, flags duplicates and only asks you about what it genuinely can't work out.</p>
        <ol className="how">
          <li><b>Drop</b> photos, scans and PDFs, all at once</li>
          <li><b>Answer</b> the few questions the assistant asks</li>
          <li><b>Submit</b> once the claim is ready</li>
        </ol>
      </div>
    )
  }

  return (
    <div className="pane-inner">
      <div className="claim-head">
        <div>
          {editingTitle ? (
            <input className="claim-title-input" autoFocus value={title} onChange={e => setTitle(e.target.value)} onBlur={saveTitle}
              onKeyDown={e => { if (e.key === 'Enter') saveTitle(); if (e.key === 'Escape') setEditingTitle(false) }} />
          ) : (
            <h2 className="claim-title">
              {claim.title}
              {editable && <button className="link small" onClick={() => { setTitle(claim.title); setEditingTitle(true) }}>Rename</button>}
            </h2>
          )}
          <div className="muted small">
            {claim.id} · {d.receipts} receipt{d.receipts === 1 ? '' : 's'}
            {claim.period && <> · {shortDate(claim.period.from)} – {shortDate(claim.period.to)}</>}
          </div>
        </div>
        <div className="claim-total">
          <div className="muted small">Total to reimburse</div>
          <div className="big-money">{money(claim.totalAmount, claim.currency)}</div>
          {claim.totalClaimed !== claim.totalAmount && <div className="muted small">of {money(claim.totalClaimed)} spent</div>}
        </div>
      </div>

      <Pipeline snap={snap} />

      <div className="tiles">
        <Tile value={`${d.processed}/${d.receipts}`} label="Processed" />
        <Tile icon="✓" value={d.complete} label="Ready" tone="ok" />
        <Tile icon="?" value={d.needsInformation} label="Need info" tone={d.needsInformation ? 'info' : ''} />
        <Tile icon="⚠" value={d.policyViolations + d.warnings} label="Policy" tone={d.policyViolations ? 'warn' : ''} />
        <Tile icon="⊘" value={d.duplicates} label="Duplicates" tone={d.duplicates ? 'dup' : ''} />
        {d.failed > 0 && <Tile icon="✕" value={d.failed} label="Unreadable" tone="bad" />}
      </div>

      {claim.categoryTotals.length > 0 && (
        <div className="card cat-card">
          <h3>By category</h3>
          <ul className="cat-bars">
            {claim.categoryTotals.map(c => (
              <li key={c.category}>
                <span className="cat-name">{c.label}</span>
                <span className="cat-bar"><span style={{ width: `${(c.amount / maxCat) * 100}%` }} /></span>
                <span className="cat-amt">{money(c.amount)}</span>
              </li>
            ))}
          </ul>
        </div>
      )}

      {claim.groups.map(g => (
        <div key={g.id} className="card group">
          <div className="group-head">
            <div>
              <GroupTitle group={g} claimId={claim.id} run={run} editable={editable} />
              <div className="muted small">
                {g.kind === 'trip' ? `${g.period} · trip auto-detected from your ${g.city} bookings` : g.period}
                {g.purpose && <> · <span title="Business purpose">{g.purpose}</span></>}
              </div>
            </div>
            <div className="group-total">{money(g.total)}</div>
          </div>
          <div className="table-wrap">
            <table className="exp-table">
              <thead><tr><th>Date</th><th>Merchant</th><th>Category</th><th className="r">Amount</th><th>Status</th></tr></thead>
              <tbody>
                {g.expenseIds.map(id => byId[id] && <ExpenseRow key={id} e={byId[id]} labels={labels} onSelect={onSelect} />)}
              </tbody>
            </table>
          </div>
        </div>
      ))}

      {excluded.length > 0 && (
        <div className="card group excluded-group">
          <div className="group-head"><h3 className="group-title">Excluded from claim</h3></div>
          <div className="table-wrap">
            <table className="exp-table">
              <tbody>{excluded.map(e => <ExpenseRow key={e.id} e={e} labels={labels} onSelect={onSelect} />)}</tbody>
            </table>
          </div>
        </div>
      )}
    </div>
  )
}

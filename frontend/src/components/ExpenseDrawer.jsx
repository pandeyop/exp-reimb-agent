import React, { useEffect, useMemo, useRef, useState } from 'react'
import { api, fval, money, longDate } from '../api.js'
import { EXPENSE_STATUS, SOURCE_LABEL } from '../status.js'

const LABELS = {
  merchant: 'Merchant', expenseDate: 'Date', total: 'Amount', currency: 'Currency', businessPurpose: 'Business purpose',
  attendees: 'Attendees', description: 'Description', costCentre: 'Cost centre / project', origin: 'From', destination: 'To',
  checkIn: 'Check-in', checkOut: 'Check-out', nights: 'Nights', travelDate: 'Travel date', returnDate: 'Return date',
  invoiceNumber: 'Invoice no.', tax: 'Tax / GST', subtotal: 'Subtotal', gstin: 'GSTIN', paymentMethod: 'Payment',
  cardLast4: 'Card', bookingReference: 'Booking / PNR', merchantAddress: 'Address', city: 'City', covers: 'Covers',
  billingPeriod: 'Billing period', serviceCharge: 'Service charge', tip: 'Tip',
}
const TYPES = { expenseDate: 'date', checkIn: 'date', checkOut: 'date', travelDate: 'date', returnDate: 'date', total: 'number', nights: 'number' }
const CONTEXT_FIELDS = {
  TAXI: ['origin', 'destination'], LOCAL_TRANSPORT: ['origin', 'destination'],
  AIR_TRAVEL: ['origin', 'destination', 'travelDate', 'returnDate'], TRAIN_TRAVEL: ['origin', 'destination', 'travelDate'],
  HOTEL: ['checkIn', 'checkOut', 'nights'], CLIENT_ENTERTAINMENT: ['attendees'], MEALS: ['attendees'],
}
const STATE_TEXT = { KNOWN: 'Confident', INFERRED: 'Inferred', LOW_CONFIDENCE: 'Low confidence', MISSING: 'Missing', NOT_REQUIRED: '' }

function SourceTag({ field, state }) {
  if (!field || field.value === null || field.value === undefined) {
    return state === 'MISSING' ? <span className="src missing">Required</span> : null
  }
  const pct = Math.round((field.confidence || 0) * 100)
  const cls = field.source === 'user' ? 'user' : state === 'LOW_CONFIDENCE' ? 'low' : state === 'INFERRED' ? 'inferred' : 'known'
  return (
    <span className={`src ${cls}`} title={field.reason || STATE_TEXT[state] || ''}>
      {SOURCE_LABEL[field.source] || field.source}{field.source !== 'user' ? ` ${pct}%` : ''}
    </span>
  )
}

export default function ExpenseDrawer({ snap, expense: e, run, onClose, readOnly }) {
  const doc = snap.documents.find(d => d.id === e.documentId)
  const cats = snap.policy.categories
  const cat = e.category.value
  const editableFields = useMemo(() => {
    const base = ['merchant', 'expenseDate', 'total', 'currency', 'businessPurpose']
    const extra = CONTEXT_FIELDS[cat] || []
    return [...base, ...extra.filter(f => !base.includes(f)), 'description', 'costCentre']
  }, [cat])
  const initial = useMemo(() => Object.fromEntries(editableFields.map(f => [f, fval(e, f) ?? ''])), [e, editableFields])
  const [form, setForm] = useState(initial)
  const [category, setCategory] = useState(cat)
  const [saving, setSaving] = useState(false)
  const replaceInput = useRef(null)
  const [justification, setJustification] = useState(e.policyDecision?.justification || '')
  useEffect(() => { setForm(initial); setCategory(cat) }, [initial, cat])
  useEffect(() => {
    const onKey = k => k.key === 'Escape' && onClose()
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  const st = EXPENSE_STATUS[e.status] ?? EXPENSE_STATUS.PROCESSING
  const dirty = editableFields.filter(f => String(form[f] ?? '') !== String(initial[f] ?? ''))
  const catDirty = category !== cat

  async function save() {
    const changes = Object.fromEntries(dirty.map(f => [f, form[f] === '' ? null : form[f]]))
    if (catDirty) changes.category = category
    if (!Object.keys(changes).length) return
    setSaving(true)
    try { await run(() => api.patchExpense(e.id, changes), 'Saved — claim revalidated') } catch { /* toast */ } finally { setSaving(false) }
  }

  const over = e.policyResults.find(r => r.code === 'OVER_LIMIT' || r.code === 'OVER_LIMIT_CAPPED' || r.code === 'OVER_LIMIT_JUSTIFIED')
  const otherFields = Object.entries(e.fields).filter(([k, f]) => f.value !== null && !editableFields.includes(k))

  return (
    <div className="drawer-backdrop" onClick={onClose}>
      <aside className="drawer" role="dialog" aria-modal="true" aria-label="Expense details" onClick={ev => ev.stopPropagation()}>
        <div className="drawer-head">
          <div>
            <div className="muted small">{e.id} · {doc?.filename}</div>
            <h2>{fval(e, 'merchant') || 'Unknown merchant'} <span className={`chip ${st.tone}`}>{st.icon} {st.label}</span></h2>
          </div>
          <button className="icon-btn" onClick={onClose} aria-label="Close">✕</button>
        </div>

        <div className="drawer-body">
          <div className="preview">
            {doc && (doc.mediaType.startsWith('image/')
              ? <a href={doc.url} target="_blank" rel="noreferrer"><img src={doc.url} alt={`Receipt ${doc.filename}`} /></a>
              : <iframe title="Receipt PDF" src={doc.url} />)}
            {doc && (
              <div className="row-btns">
                <a className="link small" href={doc.url} target="_blank" rel="noreferrer">Open original ↗</a>
                {!readOnly && <button className="link small" onClick={() => replaceInput.current?.click()}>Replace with a clearer copy</button>}
                <input ref={replaceInput} type="file" hidden accept=".jpg,.jpeg,.jfif,.png,.pdf" onChange={ev => {
                  const file = ev.target.files?.[0]
                  if (file) run(() => api.replaceDocument(doc.id, file), 'Re-uploaded — reading it again').then(onClose).catch(() => {})
                }} />
              </div>
            )}
          </div>

          <div className="details">
            {e.excluded && <div className="banner muted-b">Excluded: {e.excludedReason}</div>}

            {e.duplicate && !e.excluded && (
              <div className="banner dup">
                <b>⊘ Possible duplicate · {Math.round(e.duplicate.score * 100)}% match</b>
                <p>{e.duplicate.explanation}</p>
                {!readOnly && (
                  <div className="row-btns">
                    <button className="btn" onClick={() => run(() => api.resolveDuplicate(e.id, 'duplicate'), 'Duplicate removed').catch(() => {})}>Confirm duplicate</button>
                    <button className="btn ghost" onClick={() => run(() => api.resolveDuplicate(e.id, 'separate'), 'Kept as separate expense').catch(() => {})}>Keep both</button>
                  </div>
                )}
              </div>
            )}

            {over && !e.excluded && (
              <div className={`banner ${over.code === 'OVER_LIMIT' ? 'warn' : 'info'}`}>
                <b>⚠ {over.title}</b>
                <div className="limit-grid">
                  <span>Claimed</span><b>{money(over.claimed)}</b>
                  <span>Policy limit</span><b>{money(over.limit)}</b>
                  <span>Excess</span><b>{money(over.excess)}</b>
                </div>
                {!readOnly && (
                  <>
                    <div className="row-btns">
                      <button className={`btn ${e.policyDecision?.type === 'cap' ? 'primary' : ''}`} onClick={() => run(() => api.policyOverride(e.id, 'cap'), 'Capped at the policy limit').catch(() => {})}>Cap at {money(over.cappedAmount ?? over.limit)}</button>
                      {e.policyDecision && <button className="btn ghost" onClick={() => run(() => api.policyOverride(e.id, 'clear')).catch(() => {})}>Undo decision</button>}
                    </div>
                    <div className="justify">
                      <input value={justification} onChange={ev => setJustification(ev.target.value)} placeholder="Or keep the full amount — justification for your approver" maxLength={500} />
                      <button className="btn" disabled={justification.trim().length < 6} onClick={() => run(() => api.policyOverride(e.id, 'justify', justification), 'Justification attached').catch(() => {})}>Justify</button>
                    </div>
                  </>
                )}
              </div>
            )}

            <section>
              <h3>Details {!readOnly && <span className="muted small">— edit anything the AI got wrong</span>}</h3>
              <div className="form-grid">
                <label className="field">
                  <span className="field-label">Category <SourceTag field={{ ...e.category, value: cat }} state={e.fieldStates?.category} /></span>
                  <select value={category} disabled={readOnly} onChange={ev => setCategory(ev.target.value)}>
                    {Object.entries(cats).map(([k, v]) => <option key={k} value={k}>{v.label}</option>)}
                  </select>
                  {e.category.reason && <span className="field-hint">{e.category.reason}</span>}
                </label>
                {editableFields.map(f => {
                  const state = e.fieldStates?.[f]
                  const gap = e.gaps.some(g => g.field === f)
                  return (
                    <label key={f} className={`field ${gap ? 'gap' : ''} ${state === 'INFERRED' ? 'inferred' : ''}`}>
                      <span className="field-label">{LABELS[f]} <SourceTag field={e.fields[f]} state={state} /></span>
                      {f === 'businessPurpose' || f === 'attendees' || f === 'description'
                        ? <textarea rows={2} value={form[f] ?? ''} disabled={readOnly} onChange={ev => setForm({ ...form, [f]: ev.target.value })} />
                        : <input type={TYPES[f] || 'text'} step={f === 'total' ? '0.01' : undefined} value={form[f] ?? ''} disabled={readOnly}
                          onChange={ev => setForm({ ...form, [f]: ev.target.value })} />}
                      {e.fields[f]?.source === 'inferred' && e.fields[f]?.reason && <span className="field-hint">{e.fields[f].reason}</span>}
                    </label>
                  )
                })}
              </div>
              {!readOnly && (
                <div className="row-btns">
                  <button className="btn primary" disabled={saving || (!dirty.length && !catDirty)} onClick={save}>{saving ? 'Saving…' : 'Save & revalidate'}</button>
                  {(dirty.length > 0 || catDirty) && <button className="btn ghost" onClick={() => { setForm(initial); setCategory(cat) }}>Reset</button>}
                  <span className="spacer" />
                  {e.excluded
                    ? <button className="btn ghost" onClick={() => run(() => api.patchExpense(e.id, { excluded: false }), 'Restored').catch(() => {})}>Restore expense</button>
                    : <button className="btn ghost danger" onClick={() => run(() => api.excludeExpense(e.id), 'Excluded from claim').catch(() => {})}>Exclude</button>}
                </div>
              )}
            </section>

            {e.explanations?.length > 0 && (
              <section>
                <h3>Why the agent decided this</h3>
                <ul className="explain">
                  {e.explanations.map((x, i) => <li key={i} className={`ex-${x.type}`}><span className="ex-type">{x.type}</span>{x.text}</li>)}
                </ul>
              </section>
            )}

            {e.policyResults.filter(r => !r.code.startsWith('OVER_LIMIT')).length > 0 && (
              <section>
                <h3>Policy checks</h3>
                <ul className="policy-list">
                  {e.policyResults.filter(r => !r.code.startsWith('OVER_LIMIT')).map((r, i) => (
                    <li key={i} className={r.severity}><b>{r.title}</b> — {r.message}</li>
                  ))}
                </ul>
              </section>
            )}

            <section>
              <h3>Everything else extracted</h3>
              <dl className="kv">
                {otherFields.map(([k, f]) => (
                  <React.Fragment key={k}>
                    <dt>{LABELS[k] || k}</dt>
                    <dd>{['subtotal', 'tax', 'serviceCharge', 'tip'].includes(k) ? money(f.value) : k === 'cardLast4' ? `•••• ${f.value}` : /Date|checkIn|checkOut/.test(k) ? longDate(f.value) : String(f.value)} <SourceTag field={f} state={e.fieldStates?.[k]} /></dd>
                  </React.Fragment>
                ))}
              </dl>
              {e.lineItems?.length > 0 && (
                <table className="line-items">
                  <thead><tr><th>Item</th><th className="r">Qty</th><th className="r">Amount</th></tr></thead>
                  <tbody>{e.lineItems.map((li, i) => <tr key={i}><td>{li.description}</td><td className="r">{li.quantity ?? ''}</td><td className="r">{li.amount != null ? money(li.amount) : ''}</td></tr>)}</tbody>
                </table>
              )}
              <p className="muted small">
                Read by: {e.extractionMode === 'claude' ? 'Claude (live)' : e.extractionMode === 'openai' ? 'OpenAI (live)' : e.extractionMode === 'replay' ? 'recorded extraction (offline demo mode)' : e.extractionMode === 'pdf-text' ? 'offline PDF text parser' : 'not readable automatically'}
                {e.extractionNotes?.length > 0 && <> · {e.extractionNotes.join(' ')}</>}
              </p>
            </section>

            {e.edits?.length > 0 && (
              <section>
                <h3>Your changes</h3>
                <ul className="edits">{e.edits.map((x, i) => <li key={i}>{LABELS[x.field] || x.field}: <s>{String(x.from ?? '—')}</s> → <b>{String(x.to ?? '—')}</b> <span className="muted small">via {x.via}</span></li>)}</ul>
              </section>
            )}
          </div>
        </div>
      </aside>
    </div>
  )
}

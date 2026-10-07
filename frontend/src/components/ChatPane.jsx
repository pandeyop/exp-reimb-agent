import React, { useEffect, useMemo, useRef, useState } from 'react'
import { api } from '../api.js'

export function Rich({ text }) {
  // Minimal, safe markdown: **bold** and line breaks. No HTML is injected.
  return text.split('\n').map((line, i) => (
    <React.Fragment key={i}>
      {i > 0 && <br />}
      {line.split(/(\*\*[^*]+\*\*)/g).map((part, j) =>
        part.startsWith('**') && part.endsWith('**') ? <strong key={j}>{part.slice(2, -2)}</strong> : <React.Fragment key={j}>{part}</React.Fragment>
      )}
    </React.Fragment>
  ))
}

const KIND_LABEL = { question: 'Question', summary: 'Summary', ready: 'Ready', submitted: 'Submitted', progress: 'Working' }

export default function ChatPane({ snap, run, refresh, onSelect }) {
  const [text, setText] = useState('')
  const [sending, setSending] = useState(false)
  const [pending, setPending] = useState(null)
  const [showTrace, setShowTrace] = useState(false)
  const listRef = useRef(null)
  const claimId = snap.claim.id
  const openIds = useMemo(() => new Set(snap.questions.map(q => q.id)), [snap.questions])
  const firstOpen = snap.questions.find(q => q.kind !== 'duplicate') || snap.questions[0]
  const submitted = snap.claim.status === 'SUBMITTED'
  const processing = snap.claim.dashboard.processing > 0

  useEffect(() => {
    const el = listRef.current
    if (el) el.scrollTop = el.scrollHeight
  }, [snap.messages.length, pending, showTrace])

  async function send(body, shown) {
    if (sending) return
    setSending(true)
    setPending(shown)
    try {
      await api.chat(claimId, body)
    } catch (e) {
      /* surfaced via toast in run */
      await run(() => Promise.reject(e)).catch(() => {})
    } finally {
      setPending(null)
      setSending(false)
      await refresh()
    }
  }

  function onSubmit(e) {
    e.preventDefault()
    const t = text.trim()
    if (!t) return
    setText('')
    send({ message: t }, t)
  }

  return (
    <div className="pane-inner chat-inner">
      <div className="pane-head">
        <h2>Assistant</h2>
        <button className={`link small ${showTrace ? 'on' : ''}`} onClick={() => setShowTrace(s => !s)}>
          {showTrace ? 'Hide' : 'Show'} agent activity
        </button>
      </div>

      <div className="messages" ref={listRef} aria-live="polite">
        {snap.messages.map(m => {
          const open = m.kind === 'question' && openIds.has(m.questionId)
          const answered = m.kind === 'question' && !open
          return (
            <div key={m.id} className={`msg ${m.role} ${m.kind} ${answered ? 'answered' : ''}`}>
              {m.role === 'assistant' && KIND_LABEL[m.kind] && (
                <div className="msg-kind">{answered ? '✓ Resolved' : KIND_LABEL[m.kind]}</div>
              )}
              <div className="bubble"><Rich text={m.text} /></div>
              {m.kind === 'question' && m.expenseIds?.length === 1 && (
                <button className="link small view-link" onClick={() => onSelect(m.expenseIds[0])}>View receipt →</button>
              )}
              {open && m.options?.length > 0 && !submitted && (
                <div className="options">
                  {m.options.map(o => (
                    <button key={o.value} className="opt" disabled={sending}
                      onClick={() => send({ questionId: m.questionId, option: o.value }, o.label)}>
                      {o.label}
                    </button>
                  ))}
                </div>
              )}
            </div>
          )
        })}
        {pending && <div className="msg user"><div className="bubble">{pending}</div></div>}
        {(sending || processing) && (
          <div className="msg assistant typing"><div className="bubble"><span className="dots"><i /><i /><i /></span>{processing && !sending && <span className="muted small"> reading documents</span>}</div></div>
        )}
        {showTrace && (
          <div className="trace">
            <div className="trace-title">Agent activity</div>
            {snap.events.slice(-40).map((ev, i) => (
              <div key={i} className="trace-row">
                <span className="trace-time">{new Date(ev.ts).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })}</span>
                <span className={`trace-stage s-${ev.stage}`}>{ev.stage}</span>
                <span className="trace-msg">{ev.message}{ev.confidence != null ? ` (${Math.round(ev.confidence * 100)}%)` : ''}</span>
              </div>
            ))}
          </div>
        )}
      </div>

      {!submitted && (
        <form className="composer" onSubmit={onSubmit}>
          <textarea
            value={text}
            onChange={e => setText(e.target.value)}
            onKeyDown={e => { if (e.key === 'Enter' && !e.shiftKey) onSubmit(e) }}
            placeholder={firstOpen?.placeholder ? `Reply… ${firstOpen.placeholder}` : 'Ask about your claim or reply to a question…'}
            rows={2}
            disabled={sending}
            aria-label="Message the assistant"
          />
          <button className="btn primary" type="submit" disabled={sending || !text.trim()}>Send</button>
        </form>
      )}
    </div>
  )
}

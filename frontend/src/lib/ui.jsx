// Small building blocks shared by every page.
import { useState } from 'react'

const TONE = {
  SUCCEEDED: 'ok', FINISHED: 'ok', SUCCESS: 'ok', PASS: 'ok', pass: 'ok', success: 'ok', fixed: 'ok', accepted: 'ok',
  built: 'ok', same: 'muted', true: 'ok',
  FAILED: 'bad', FAIL: 'bad', fail: 'bad', failed: 'bad', critical: 'bad', high: 'bad', ERROR: 'bad', worse: 'bad',
  regression: 'bad', false: 'bad', dead: 'bad',
  AWAITING_APPROVAL: 'warn', WAITING_APPROVAL: 'warn', medium: 'warn', partial: 'warn', flaky: 'warn',
  RUNNING: 'info', QUEUED: 'info', low: 'info', better: 'ok', open: 'warn', reproduced: 'bad',
  REJECTED: 'muted', CANCELLED: 'muted', EXPIRED: 'muted', STOPPED: 'muted', info: 'muted',
}

export function Badge({ value, tone }) {
  if (value === null || value === undefined || value === '') return <span className="muted">-</span>
  const t = tone || TONE[String(value)] || 'muted'
  return <span className={`badge ${t}`}>{String(value)}</span>
}

export function Card({ title, actions, children, wide }) {
  return (
    <section className={`card${wide ? ' wide' : ''}`}>
      {(title || actions) && (
        <header>
          <h3>{title}</h3>
          <div className="actions">{actions}</div>
        </header>
      )}
      {children}
    </section>
  )
}

export function ErrorBox({ error }) {
  if (!error) return null
  return (
    <div className="error" role="alert">
      <b>{error.code || 'error'}</b> {error.message}
    </div>
  )
}

export function Empty({ children = 'Nothing here yet.' }) {
  return <p className="muted empty">{children}</p>
}

export function Table({ rows, cols, onRow, empty }) {
  if (!rows) return <p className="muted">loading ...</p>
  if (!rows.length) return <Empty>{empty}</Empty>
  return (
    <div className="table-wrap">
      <table>
        <thead>
          <tr>{cols.map((c) => <th key={c.key || c.label}>{c.label}</th>)}</tr>
        </thead>
        <tbody>
          {rows.map((r, i) => (
            <tr key={r.id ?? i} onClick={onRow ? () => onRow(r) : undefined} className={onRow ? 'click' : ''}>
              {cols.map((c) => (
                <td key={c.key || c.label}>{c.render ? c.render(r) : fmt(r[c.key])}</td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

function fmt(v) {
  if (v === null || v === undefined || v === '') return <span className="muted">-</span>
  if (typeof v === 'boolean') return <Badge value={String(v)} />
  if (typeof v === 'object') return <code>{JSON.stringify(v).slice(0, 120)}</code>
  return String(v)
}


export function Json({ value }) {
  return <pre className="json">{JSON.stringify(value, null, 2)}</pre>
}

// A deliberately small Markdown renderer for the reports the backend writes: headings, lists, tables, code, bold.
export function Markdown({ text }) {
  if (!text) return <Empty />
  const lines = text.split('\n')
  const out = []
  let i = 0
  const inline = (s, k) => {
    const parts = s.split(/(`[^`]+`|\*\*[^*]+\*\*)/g)
    return parts.map((p, j) =>
      p.startsWith('`') ? <code key={`${k}-${j}`}>{p.slice(1, -1)}</code>
        : p.startsWith('**') ? <b key={`${k}-${j}`}>{p.slice(2, -2)}</b> : p)
  }
  while (i < lines.length) {
    const l = lines[i]
    if (l.startsWith('```')) {
      const lang = l.slice(3).trim()
      const body = []
      i++
      while (i < lines.length && !lines[i].startsWith('```')) body.push(lines[i++])
      i++
      out.push(lang === 'diff' ? <Diff key={i} text={body.join('\n')} /> : <pre key={i}>{body.join('\n')}</pre>)
      continue
    }
    if (l.startsWith('|')) {
      const rows = []
      while (i < lines.length && lines[i].startsWith('|')) rows.push(lines[i++])
      const cells = (r) => r.split('|').slice(1, -1).map((c) => c.trim())
      const [head, , ...body] = rows
      out.push(
        <div className="table-wrap" key={i}>
          <table>
            <thead><tr>{cells(head).map((c, j) => <th key={j}>{inline(c, j)}</th>)}</tr></thead>
            <tbody>{body.map((r, k) => <tr key={k}>{cells(r).map((c, j) => <td key={j}>{inline(c, `${k}${j}`)}</td>)}</tr>)}</tbody>
          </table>
        </div>,
      )
      continue
    }
    const h = l.match(/^(#{1,3}) (.*)/)
    if (h) out.push(h[1].length === 1 ? <h2 key={i}>{inline(h[2], i)}</h2> : <h4 key={i}>{inline(h[2], i)}</h4>)
    else if (l.startsWith('- ')) {
      const items = []
      while (i < lines.length && lines[i].startsWith('- ')) items.push(lines[i++].slice(2))
      out.push(<ul key={i}>{items.map((x, j) => <li key={j}>{inline(x, j)}</li>)}</ul>)
      continue
    } else if (l.trim()) out.push(<p key={i}>{inline(l, i)}</p>)
    i++
  }
  return <div className="markdown">{out}</div>
}

export function Diff({ text }) {
  if (!text) return <Empty>No code change.</Empty>
  return (
    <pre className="diff">
      {text.split('\n').map((l, i) => {
        const c = l.startsWith('+++') || l.startsWith('---') ? 'file' : l.startsWith('+') ? 'add'
          : l.startsWith('-') ? 'del' : l.startsWith('@@') ? 'hunk' : ''
        return <div key={i} className={c}>{l || ' '}</div>
      })}
    </pre>
  )
}

// Typed confirmation, the same rule as the CLI: the operator types the confirmation text of the proposal.
export function ConfirmDialog({ title, token, onConfirm, onClose, children }) {
  const [typed, setTyped] = useState('')
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState(null)
  return (
    <div className="modal-back" onClick={onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()} role="dialog" aria-modal="true">
        <h3>{title}</h3>
        {children}
        <label>
          Type <code>{token}</code> to approve
          <input autoFocus value={typed} onChange={(e) => setTyped(e.target.value)} aria-label="confirmation text" />
        </label>
        <ErrorBox error={err} />
        <div className="row end">
          <button onClick={onClose}>Cancel</button>
          <button className="danger" disabled={typed !== token || busy}
            onClick={async () => {
              setBusy(true)
              try {
                await onConfirm(typed)
                onClose()
              } catch (e) {
                setErr(e)
              } finally {
                setBusy(false)
              }
            }}>Approve</button>
        </div>
      </div>
    </div>
  )
}

export function Field({ label, children }) {
  return (
    <label className="field">
      <span>{label}</span>
      {children}
    </label>
  )
}

export function Stat({ label, value, tone }) {
  return (
    <div className={`stat ${tone || ''}`}>
      <div className="v">{value ?? '-'}</div>
      <div className="l">{label}</div>
    </div>
  )
}

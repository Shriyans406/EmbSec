import { useState } from 'react'
import { can, get, post } from '../lib/api'
import { go, usePoll, when } from '../lib/hooks'
import { Badge, Card, ErrorBox, Field, Json, Table } from '../lib/ui'

const FAMILIES = ['boundary', 'overflow', 'mutation', 'malformed', 'oversize', 'invalid', 'state', 'timeout']

function Finding({ id, role }) {
  const f = usePoll(() => get(`/fuzz/findings/${id}`), 5000, [id])
  const [err, setErr] = useState(null)
  const act = async (fn) => {
    setErr(null)
    try {
      await fn()
      f.reload()
    } catch (e) {
      setErr(e)
    }
  }
  const x = f.data
  if (!x) return null
  return (
    <Card title={`Finding #${x.id}: ${x.kind} on ${x.signature.split(':')[1]}`} actions={<>
      <Badge value={x.severity} /> <Badge value={x.status} />
      {can(role, 'agent') && <button onClick={() => act(() => post(`/fuzz/findings/${id}/reproduce`, { attempts: 3, minimise: true }))}>reproduce + minimise</button>}
      {can(role, 'agent') && x.severity !== 'info' && !['fixed', 'wont_fix'].includes(x.status) &&
        <button className="primary" onClick={() => act(async () => { const r = await post('/remediations', { finding_id: x.id }); go(`remediations/${r.id}`) })}>fix it</button>}
    </>}>
      <p>{x.detail}</p>
      <p className="muted">{x.cwe} - seen {x.occurrences} times on build #{x.build_id} - campaign #{x.campaign_id}</p>
      <p>input <code>{x.input_text}</code>{x.minimized && x.minimized !== x.input_text && <> - minimised <code>{x.minimized}</code></>}</p>
      <p>reply <code>{String(x.reply)}</code></p>
      <ErrorBox error={err} />
      {x.reproductions.length > 0 && <Json value={x.reproductions.map((r) => ({ build: r.build_id, status: r.status, hits: `${r.hits}/${r.attempts}`, recipe: r.recipe }))} />}
    </Card>
  )
}

export default function Fuzz({ role }) {
  const camps = usePoll(() => get('/fuzz/campaigns?limit=20'), 8000)
  const findings = usePoll(() => get('/fuzz/findings?min_severity=low&limit=100'), 8000)
  const [form, setForm] = useState({ project: '', max_inputs: 200, families: FAMILIES })
  const [open, setOpen] = useState(null)
  const [err, setErr] = useState(null)
  const start = async () => {
    setErr(null)
    try {
      await post('/fuzz/campaigns', { project: form.project || undefined, max_inputs: Number(form.max_inputs), families: form.families })
      camps.reload()
    } catch (e) {
      setErr(e)
    }
  }
  return (
    <>
      <h2>Fuzzing</h2>
      {can(role, 'agent') && (
        <Card title="New campaign">
          <div className="form">
            <div className="row">
              <Field label="project (its spec and suites become the corpus)"><input value={form.project} onChange={(e) => setForm({ ...form, project: e.target.value })} /></Field>
              <Field label="inputs (agents: up to 500)"><input type="number" min={1} max={2000} value={form.max_inputs}
                onChange={(e) => setForm({ ...form, max_inputs: e.target.value })} style={{ width: 120 }} /></Field>
            </div>
            <div className="row">{FAMILIES.map((f) => (
              <label key={f} className="row"><input type="checkbox" style={{ width: 'auto' }} checked={form.families.includes(f)}
                onChange={(e) => setForm({ ...form, families: e.target.checked ? [...form.families, f] : form.families.filter((x) => x !== f) })} />{f}</label>))}
            </div>
            <p className="muted">The board may restart (watchdog); the campaign stops after the reset budget. Bootloader,
              hang and reboot commands are never sent.</p>
            <div className="row"><button className="primary" disabled={!form.families.length} onClick={start}>Start campaign</button></div>
            <ErrorBox error={err} />
          </div>
        </Card>
      )}
      <div className="grid">
        <Card title="Campaigns">
          <Table rows={camps.data} cols={[{ key: 'id', label: '#' }, { label: 'status', render: (c) => <Badge value={c.status} /> },
            { key: 'project', label: 'project' }, { label: 'build', render: (c) => c.build_id && `#${c.build_id}` },
            { label: 'sent', render: (c) => `${c.inputs_sent}/${c.inputs_planned}` }, { key: 'anomalies', label: 'anomalies' },
            { key: 'resets', label: 'resets' }, { key: 'findings', label: 'new findings' }, { label: 'when', render: (c) => when(c.created_at) }]} />
        </Card>
        <Card title="Findings">
          <Table rows={findings.data} onRow={(f) => setOpen(f.id)} cols={[{ key: 'id', label: '#' }, { label: 'severity', render: (f) => <Badge value={f.severity} /> },
            { key: 'kind', label: 'kind' }, { label: 'input', render: (f) => <code>{(f.minimized || f.input_text).slice(0, 40)}</code> },
            { label: 'status', render: (f) => <Badge value={f.status} /> }]} />
        </Card>
      </div>
      {open && <Finding id={open} role={role} />}
    </>
  )
}

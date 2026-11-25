import { useState } from 'react'
import { API_BASE, can, get, post, session } from '../lib/api'
import { usePoll } from '../lib/hooks'
import { Badge, Card, ErrorBox, Markdown, Table } from '../lib/ui'

const KINDS = [
  { kind: 'pipeline', list: '/pipelines?limit=30', label: (x) => `#${x.id} ${x.project || ''} ${x.outcome || x.status}` },
  { kind: 'remediation', list: '/remediations?limit=30', label: (x) => `#${x.id} finding ${x.finding_id} ${x.outcome || x.status}` },
  { kind: 'optimisation', list: '/optimizations?limit=30', label: (x) => `#${x.id} ${x.project} ${x.outcome || x.status}` },
  { kind: 'agent_run', list: '/agent/runs?roots=true&limit=30', label: (x) => `#${x.id} ${x.agent}: ${x.goal.slice(0, 50)}` },
  { kind: 'fuzz_campaign', list: '/fuzz/campaigns?limit=30', label: (x) => `#${x.id} ${x.project || 'harness'} ${x.status}` },
]

async function download(path, name) {
  const res = await fetch(API_BASE + path, { headers: { Authorization: `Bearer ${session()?.token}` } })
  if (!res.ok) throw new Error(`download failed (${res.status})`)
  const url = URL.createObjectURL(await res.blob())
  const a = Object.assign(document.createElement('a'), { href: url, download: name })
  a.click()
  setTimeout(() => URL.revokeObjectURL(url), 2000)
}

export default function Reports({ role }) {
  const [k, setK] = useState(KINDS[0])
  const [id, setId] = useState('')
  const [view, setView] = useState(null)
  const [err, setErr] = useState(null)
  const list = usePoll(() => get(k.list), 0, [k.kind])
  const saved = usePoll(() => get('/records/saved'), 0)
  const base = `/records/${k.kind}/${id}`
  const run = async (fn) => {
    setErr(null)
    try {
      await fn()
    } catch (e) {
      setErr(e)
    }
  }
  return (
    <>
      <h2>Reports and records</h2>
      <p className="muted">Everything about one task in one record: agents, model calls, jobs and their logs, builds,
        deployments and who approved them, tests, findings and the audit entries - checked against what exists now.</p>
      <Card title="Task">
        <div className="row">
          <select value={k.kind} onChange={(e) => { setK(KINDS.find((x) => x.kind === e.target.value)); setId(''); setView(null) }}
            style={{ width: 'auto' }} aria-label="kind of task">{KINDS.map((x) => <option key={x.kind} value={x.kind}>{x.kind.replace('_', ' ')}</option>)}</select>
          <select value={id} onChange={(e) => { setId(e.target.value); setView(null) }} style={{ width: 'auto', minWidth: 260 }} aria-label="task">
            <option value="">choose ...</option>
            {(list.data || []).map((x) => <option key={x.id} value={x.id}>{k.label(x)}</option>)}
          </select>
          <button disabled={!id} onClick={() => run(async () => setView({ type: 'verify', data: await get(`${base}/verify`) }))}>verify</button>
          <button disabled={!id} onClick={() => run(async () => setView({ type: 'replay', data: await get(`${base}/replay`) }))}>replay timeline</button>
          <button disabled={!id} onClick={() => run(async () => setView({ type: 'md', data: await (await fetch(API_BASE + `${base}/report?format=md`, { headers: { Authorization: `Bearer ${session()?.token}` } })).text() }))}>report</button>
          <button disabled={!id} onClick={() => run(() => download(`${base}/report?format=pdf`, `${k.kind}-${id}.pdf`))}>PDF</button>
          <button disabled={!id} onClick={() => run(() => download(`${base}/report?format=json`, `${k.kind}-${id}.json`))}>JSON</button>
          {can(role, 'agent') && <button disabled={!id} onClick={() => run(async () => { await post(`${base}/save`); saved.reload() })}>save record</button>}
        </div>
        <ErrorBox error={err} />
      </Card>
      {view?.type === 'verify' && (
        <Card title={`Verification: ${view.data.ok ? 'OK' : 'FAILED'}`} actions={<Badge value={view.data.ok ? 'PASS' : 'FAIL'} />}>
          <Table rows={view.data.checks} cols={[{ key: 'check', label: 'check' }, { label: 'result', render: (c) => <Badge value={c.status} tone={{ PASS: 'ok', FAIL: 'bad', WARN: 'warn' }[c.status]} /> },
            { key: 'detail', label: 'detail' }]} />
        </Card>
      )}
      {view?.type === 'replay' && (
        <Card title={`Timeline: ${view.data.events.length} events`}>
          <Table rows={view.data.events.map((e, i) => ({ id: i, ...e }))} cols={[{ label: 'time', render: (e) => e.ts.slice(0, 19).replace('T', ' ') },
            { key: 'source', label: 'source' }, { key: 'what', label: 'what' }]} />
        </Card>
      )}
      {view?.type === 'md' && <Card><Markdown text={view.data} /></Card>}
      <Card title="Saved records">
        <Table rows={(saved.data || []).map((s) => ({ id: s.name, ...s }))} empty="No record saved yet." cols={[{ key: 'name', label: 'file' }, { key: 'bytes', label: 'bytes' },
          { label: '', render: (s) => <button onClick={() => run(async () => setView({ type: 'verify', data: await get(`/records/saved/${s.name}/verify`) }))}>verify</button> }]} />
      </Card>
    </>
  )
}

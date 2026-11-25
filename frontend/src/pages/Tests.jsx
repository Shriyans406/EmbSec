import { useState } from 'react'
import { can, get, post } from '../lib/api'
import { usePoll, when } from '../lib/hooks'
import { Badge, Card, ErrorBox, Json, Table } from '../lib/ui'

function Suite({ name, role }) {
  const suite = usePoll(() => get(`/tests/suites/${name}`), 0, [name])
  const history = usePoll(() => get(`/tests/suites/${name}/history`), 15000, [name])
  const [tab, setTab] = useState('cases')
  const [extra, setExtra] = useState(null)
  const [err, setErr] = useState(null)
  const load = async (what) => {
    setTab(what)
    setExtra(null)
    try {
      setExtra(await get(`/tests/suites/${name}/${what}`))
    } catch (e) {
      setExtra({ error: e.message })
    }
  }
  const run = async () => {
    setErr(null)
    try {
      await post(`/tests/suites/${name}/run`, { repeat: 1 })
      setTimeout(history.reload, 1500)
    } catch (e) {
      setErr(e)
    }
  }
  const s = suite.data
  if (!s) return null
  return (
    <Card title={`Suite ${s.name}`} actions={<>
      <span className="muted">revision {s.revision}{s.builtin ? ' - built in' : ''}{s.project ? ` - ${s.project}` : ''}</span>
      {can(role, 'agent') && <button className="primary" onClick={run}>Run on the board</button>}</>}>
      <ErrorBox error={err} />
      <div className="tabs">
        {['cases', 'history', 'regression', 'coverage'].map((t) => (
          <button key={t} className={tab === t ? 'on' : ''} onClick={() => (['regression', 'coverage'].includes(t) ? load(t) : setTab(t))}>{t}</button>
        ))}
      </div>
      {tab === 'cases' && <Table rows={s.cases} cols={[{ key: 'name', label: 'case' }, { label: 'command', render: (c) => <code>{c.command}</code> },
        { label: 'expect', render: (c) => <code>{JSON.stringify(c.expect)}</code> }, { label: 'on', render: (c) => <Badge value={String(c.enabled)} /> }]} />}
      {tab === 'history' && <Table rows={history.data?.builds} empty="Never run on a known build." cols={[
        { label: 'build', render: (r) => `#${r.build_id}` }, { label: 'result', render: (r) => <Badge value={r.failed ? 'FAILED' : 'PASS'} /> },
        { label: 'passed', render: (r) => `${r.passed}/${r.total}` }, { key: 'fw_version', label: 'firmware' },
        { label: 'when', render: (r) => when(r.started_at) }]} />}
      {tab === 'regression' && extra && (extra.error ? <p className="muted">{extra.error}</p> : <>
        <p><Badge value={extra.verdict} /> build #{extra.baseline?.build_id} {'->'} #{extra.current?.build_id ?? extra.current?.build_id}</p>
        <Table rows={[...(extra.regressed || []).map((x) => ({ ...x, change: 'regressed' })), ...(extra.fixed || []).map((x) => ({ ...x, change: 'fixed' }))]}
          empty="Nothing changed." cols={[{ key: 'name', label: 'case' }, { label: 'change', render: (x) => <Badge value={x.change === 'fixed' ? 'better' : 'worse'} /> },
            { label: 'before', render: (x) => <code>{x.before}</code> }, { label: 'after', render: (x) => <code>{x.after}</code> }]} />
      </>)}
      {tab === 'coverage' && extra && <Json value={extra} />}
    </Card>
  )
}

export default function Tests({ role }) {
  const suites = usePoll(() => get('/tests/suites'), 15000)
  const runs = usePoll(() => get('/tests/runs?limit=20'), 8000)
  const [name, setName] = useState(null)
  const rows = Object.entries(suites.data || {}).map(([n, s]) => ({ id: n, name: n, ...s }))
  return (
    <>
      <h2>Tests</h2>
      <div className="grid">
        <Card title="Suites">
          <Table rows={suites.data ? rows : null} onRow={(r) => setName(r.name)} cols={[{ key: 'name', label: 'suite' },
            { key: 'cases', label: 'cases' }, { key: 'project', label: 'project' }, { key: 'revision', label: 'rev' }]} />
        </Card>
        <Card title="Recent runs">
          <Table rows={runs.data} cols={[{ key: 'id', label: '#' }, { key: 'name', label: 'run' },
            { label: 'result', render: (r) => <Badge value={r.failed ? 'FAILED' : 'PASS'} /> },
            { label: 'passed', render: (r) => `${r.passed}/${r.total}` }, { label: 'build', render: (r) => r.build_id && `#${r.build_id}` },
            { label: 'when', render: (r) => when(r.started_at) }]} />
        </Card>
      </div>
      {name && <Suite name={name} role={role} />}
    </>
  )
}

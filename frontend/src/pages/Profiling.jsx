import { useState } from 'react'
import { can, get, post } from '../lib/api'
import { usePoll, when } from '../lib/hooks'
import { Badge, Card, ErrorBox, Table } from '../lib/ui'

// A small line chart of one measurement over the benchmark's runs (oldest left).
function Trend({ runs, name }) {
  const pts = runs.filter((r) => r.commands[name] != null).reverse()
  if (pts.length < 2) return null
  const vals = pts.map((r) => r.commands[name])
  const max = Math.max(...vals) * 1.1 || 1
  const W = 320, H = 70
  const xy = vals.map((v, i) => [(i / (vals.length - 1)) * (W - 10) + 5, H - 5 - (v / max) * (H - 10)])
  return (
    <svg width={W} height={H} role="img" aria-label={`${name} over runs`}>
      <polyline fill="none" stroke="var(--accent)" strokeWidth="2" points={xy.map((p) => p.join(',')).join(' ')} />
      {xy.map(([x, y], i) => <circle key={i} cx={x} cy={y} r="3" fill={pts[i].baseline ? 'var(--ok)' : 'var(--accent)'}>
        <title>{`build #${pts[i].build_id}: ${vals[i]}`}</title></circle>)}
    </svg>
  )
}

function Bench({ name, role }) {
  const history = usePoll(() => get(`/benchmarks/${name}/history`), 10000, [name])
  const [vs, setVs] = useState(null)
  const [err, setErr] = useState(null)
  const act = async (fn) => {
    setErr(null)
    try {
      await fn()
      history.reload()
    } catch (e) {
      setErr(e)
    }
  }
  const h = history.data
  if (!h) return null
  const names = Object.keys(h.runs[0]?.commands || {})
  return (
    <Card title={`Benchmark ${name}`} actions={<>
      {can(role, 'agent') && <button className="primary" onClick={() => act(() => post(`/benchmarks/${name}/run`))}>Run on the board</button>}
      <button onClick={() => act(async () => setVs(await get(`/benchmarks/${name}/vs-baseline`)))}>latest vs baseline</button></>}>
      <ErrorBox error={err} />
      <div className="row">{names.map((n) => <div key={n}><b>{n}</b><br /><Trend runs={h.runs} name={n} /></div>)}</div>
      {vs && (
        <>
          <h4>Run #{vs.after.run_id} (build #{vs.after.build_id}) against the baseline run #{vs.before.run_id}: <Badge value={vs.speed} tone={{ faster: 'ok', slower: 'bad', same: 'muted' }[vs.speed]} /></h4>
          <Table rows={Object.entries(vs.commands).map(([n, c]) => ({ id: n, n, m: c.device_us || c.host_ms, unit: c.device_us ? 'us' : 'ms' }))} cols={[
            { key: 'n', label: 'command' }, { label: 'before', render: (r) => r.m && `${r.m.before} ${r.unit}` },
            { label: 'after', render: (r) => r.m && `${r.m.after} ${r.unit}` }, { label: 'change', render: (r) => r.m && `${r.m.change_pct} %` },
            { label: 'noise', render: (r) => r.m?.noise }, { label: 'verdict', render: (r) => <Badge value={r.m?.verdict} /> }]} />
        </>
      )}
      <h4>Runs</h4>
      <Table rows={h.runs} cols={[{ key: 'run_id', label: 'run' }, { label: 'build', render: (r) => `#${r.build_id}` },
        ...names.map((n) => ({ label: n, render: (r) => r.commands[n] })),
        { key: 'throughput_cps', label: 'cmd/s' }, { label: 'repeatable', render: (r) => <Badge value={String(r.repeatable)} /> },
        { label: 'device timing', render: (r) => <Badge value={String(r.device_timing)} /> },
        { label: '', render: (r) => (r.baseline ? <Badge value="baseline" tone="ok" />
          : can(role, 'operator') && r.repeatable ? <button onClick={() => act(() => post(`/benchmarks/${name}/baseline`, { run_id: r.run_id }))}>make baseline</button> : null) },
        { label: 'when', render: (r) => when(r.at) }]} />
      <p className="muted">Numbers are device-side microseconds (harness 0.4.0 PROF) when available, otherwise host
        round trips in ms. A difference counts only when it is larger than the measured noise.</p>
    </Card>
  )
}

export default function Profiling({ role }) {
  const list = usePoll(() => get('/benchmarks'), 20000)
  const [name, setName] = useState(null)
  return (
    <>
      <h2>Profiling</h2>
      <Card title="Benchmarks">
        <Table rows={list.data} onRow={(b) => setName(b.name)} cols={[{ key: 'name', label: 'benchmark' }, { key: 'project', label: 'project' },
          { label: 'commands', render: (b) => b.definition.commands.map((c) => c.command).join(', ') },
          { label: 'runs x', render: (b) => b.definition.runs }, { label: 'baseline', render: (b) => b.baseline_run_id && `run #${b.baseline_run_id}` }]} />
      </Card>
      {name && <Bench name={name} role={role} />}
    </>
  )
}

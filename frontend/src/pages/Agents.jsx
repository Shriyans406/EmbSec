import { useCallback, useState } from 'react'
import { can, get, post } from '../lib/api'
import { go, usePoll, when } from '../lib/hooks'
import { useLive } from '../lib/live'
import { Badge, Card, ErrorBox, Field, Json, Table } from '../lib/ui'

function StartRun({ onStarted }) {
  const [goal, setGoal] = useState('')
  const [mode, setMode] = useState('team')
  const [project, setProject] = useState('')
  const [err, setErr] = useState(null)
  const start = async () => {
    setErr(null)
    try {
      const r = mode === 'team'
        ? await post('/agent/team', { goal })
        : await post('/agent/runs', { goal, agent: mode, project: project || undefined })
      onStarted(r.id)
    } catch (e) {
      setErr(e)
    }
  }
  return (
    <Card title="Start agents">
      <div className="form">
        <Field label="goal (plain words)">
          <textarea value={goal} onChange={(e) => setGoal(e.target.value)}
            placeholder="e.g. add an APP TEMP_F command to calc2 that answers in Fahrenheit" />
        </Field>
        <div className="row">
          <select value={mode} onChange={(e) => setMode(e.target.value)} aria-label="who works on it" style={{ width: 'auto' }}>
            <option value="team">the team (Supervisor + specialists)</option>
            {['lab', 'firmware', 'build', 'security', 'performance', 'verification', 'debugger']
              .map((a) => <option key={a} value={a}>{a} agent alone</option>)}
          </select>
          {mode !== 'team' && <input placeholder="project (optional)" value={project}
            onChange={(e) => setProject(e.target.value)} style={{ width: 180 }} />}
          <button className="primary" disabled={goal.trim().length < 5} onClick={start}>Start</button>
        </div>
        <p className="muted">Agents use the agent role: they can write code, build and propose deployments, never
          approve them. Every deployment waits in the approval centre.</p>
        <ErrorBox error={err} />
      </div>
    </Card>
  )
}

function RunDetail({ id, role }) {
  const run = usePoll(() => get(`/agent/runs/${id}`), 4000, [id])
  const steps = usePoll(() => get(`/agent/runs/${id}/steps`), 4000, [id])
  const tree = usePoll(() => get(`/agent/runs/${id}/tree`), 6000, [id])
  const [err, setErr] = useState(null)
  const act = async (what) => {
    setErr(null)
    try {
      await post(`/agent/runs/${id}/${what}`)
      run.reload()
    } catch (e) {
      setErr(e)
    }
  }
  const r = run.data
  if (!r) return <p className="muted">loading run #{id} ...</p>
  return (
    <>
      <Card title={`Run #${r.id} - ${r.agent}`} actions={<>
        <Badge value={r.status} /> <Badge value={r.outcome} />
        {can(role, 'agent') && r.status === 'WAITING_APPROVAL' && <button onClick={() => act('resume')}>resume</button>}
        {can(role, 'agent') && !['FINISHED', 'FAILED', 'STOPPED'].includes(r.status) &&
          <button onClick={() => act('stop')}>stop</button>}
      </>}>
        <p>{r.goal}</p>
        <p className="muted">steps {r.steps} / {r.max_steps} - tokens {r.usage?.total} in {r.usage?.calls} model calls
          - started {when(r.created_at)}</p>
        {r.status === 'WAITING_APPROVAL' && <div className="notice">Waiting for a human decision on job
          #{r.pending_job_id}. <a href="#/approvals">Approval centre</a>, then resume.</div>}
        <ErrorBox error={err} />
        {r.error && <div className="error">{r.error}</div>}
        {r.report && <><h4>Report</h4><Json value={r.report} /></>}
      </Card>
      {tree.data?.children?.length > 0 && (
        <Card title="Team">
          <Table rows={tree.data.children} cols={[{ key: 'id', label: 'run' }, { key: 'agent', label: 'agent' },
            { label: 'status', render: (c) => <Badge value={c.status} /> },
            { label: 'outcome', render: (c) => <Badge value={c.outcome} /> },
            { label: 'verdict', render: (c) => <Badge value={c.report?.verdict} /> },
            { label: 'task', render: (c) => c.task.slice(0, 90) }, { key: 'tokens', label: 'tokens' }]} />
        </Card>
      )}
      <Card title="Steps">
        <Table rows={steps.data?.steps} cols={[{ label: 'time', render: (s) => s.ts.slice(11, 19) },
          { key: 'kind', label: 'kind' }, { key: 'name', label: 'what' },
          { label: 'ok', render: (s) => (s.ok === null ? '' : <Badge value={String(s.ok)} />) },
          { label: 'detail', render: (s) => <code>{JSON.stringify(s.detail).slice(0, 160)}</code> }]} />
      </Card>
    </>
  )
}

export default function Agents({ role, params }) {
  const id = params?.[0]
  const runs = usePoll(() => get('/agent/runs?roots=true&limit=30'), 10000)
  const status = usePoll(() => get('/agent/status'), 0)
  const reloadRuns = runs.reload
  useLive(useCallback((m) => { if (m.type === 'job' && m.job.kind.startsWith('agent')) reloadRuns() }, [reloadRuns]))
  if (id) return <><p><a href="#/agents">all runs</a></p><RunDetail id={id} role={role} /></>
  return (
    <>
      <h2>Agents</h2>
      {status.data && !status.data.configured && <div className="error">No model is configured (GEMINI_API_KEY).</div>}
      {can(role, 'agent') && <StartRun onStarted={(rid) => go(`agents/${rid}`)} />}
      <Card title="Runs" actions={status.data && <span className="muted">model {status.data.model} - {status.data.usage.total} tokens so far</span>}>
        <Table rows={runs.data} onRow={(r) => go(`agents/${r.id}`)} cols={[{ key: 'id', label: '#' },
          { key: 'agent', label: 'agent' }, { label: 'status', render: (r) => <Badge value={r.status} /> },
          { label: 'outcome', render: (r) => <Badge value={r.outcome} /> },
          { label: 'goal', render: (r) => r.goal.slice(0, 90) }, { key: 'tokens', label: 'tokens' },
          { label: 'when', render: (r) => when(r.created_at) }]} />
      </Card>
    </>
  )
}

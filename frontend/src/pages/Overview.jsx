import { useCallback } from 'react'
import { get } from '../lib/api'
import { go, usePoll, when } from '../lib/hooks'
import { useLive } from '../lib/live'
import { Badge, Card, Stat, Table } from '../lib/ui'

const safe = (p) => p.catch(() => null)

export default function Overview() {
  const { data, reload } = usePoll(async () => {
    const [health, jobs, waiting, runs, builds, findings, knowledge] = await Promise.all([
      safe(get('/health')), safe(get('/jobs?limit=12')), safe(get('/jobs?status=AWAITING_APPROVAL&limit=20')),
      safe(get('/agent/runs?roots=true&limit=6')), safe(get('/builds?limit=6')),
      safe(get('/fuzz/findings?min_severity=high&limit=50')), safe(get('/knowledge/stats')),
    ])
    return { health, jobs, waiting, runs, builds, findings, knowledge }
  }, 10000)
  useLive(useCallback((m) => { if (m.type === 'job') reload() }, [reload]))
  const d = data || {}
  const running = (d.jobs || []).filter((j) => j.status === 'RUNNING').length
  const open = (d.findings || []).filter((f) => !['fixed', 'wont_fix'].includes(f.status)).length
  return (
    <>
      <h2>Overview</h2>
      <div className="stats">
        <Stat label="backend" value={d.health ? `${d.health.version} (phase ${d.health.phase})` : 'offline'}
          tone={d.health ? 'ok' : 'bad'} />
        <Stat label="waiting for approval" value={d.waiting?.length ?? '-'} tone={d.waiting?.length ? 'warn' : ''} />
        <Stat label="running jobs" value={running} />
        <Stat label="open high/critical findings" value={open} tone={open ? 'bad' : 'ok'} />
        <Stat label="knowledge entries" value={d.knowledge?.total} />
      </div>
      {d.waiting?.length > 0 && (
        <div className="notice">
          {d.waiting.length} proposal{d.waiting.length > 1 ? 's wait' : ' waits'} for a human decision.{' '}
          <a href="#/approvals">Open the approval centre</a>
        </div>
      )}
      <div className="grid">
        <Card title="Recent jobs" actions={<a href="#/jobs">all jobs</a>}>
          <Table rows={d.jobs} cols={[{ key: 'id', label: '#' }, { key: 'kind', label: 'kind' },
            { label: 'status', render: (r) => <Badge value={r.status} /> }, { key: 'summary', label: 'what' }]} />
        </Card>
        <Card title="Agent runs" actions={<a href="#/agents">agents</a>}>
          <Table rows={d.runs} onRow={(r) => go(`agents/${r.id}`)} cols={[
            { key: 'id', label: '#' }, { key: 'agent', label: 'agent' },
            { label: 'status', render: (r) => <Badge value={r.status} /> },
            { label: 'outcome', render: (r) => <Badge value={r.outcome} /> },
            { label: 'goal', render: (r) => r.goal.slice(0, 70) }]} />
        </Card>
        <Card title="Builds" actions={<a href="#/firmware">firmware</a>}>
          <Table rows={d.builds} cols={[{ key: 'id', label: '#' }, { key: 'project', label: 'project' },
            { label: 'status', render: (r) => <Badge value={r.status} /> },
            { label: 'when', render: (r) => when(r.started_at) }]} />
        </Card>
        <Card title="Serious fuzz findings" actions={<a href="#/fuzz">fuzzing</a>}>
          <Table rows={(d.findings || []).slice(0, 6)} empty="No high or critical findings." cols={[
            { key: 'id', label: '#' }, { label: 'severity', render: (r) => <Badge value={r.severity} /> },
            { key: 'kind', label: 'kind' }, { label: 'input', render: (r) => <code>{(r.minimized || r.input_text).slice(0, 40)}</code> },
            { label: 'status', render: (r) => <Badge value={r.status} /> }]} />
        </Card>
      </div>
    </>
  )
}

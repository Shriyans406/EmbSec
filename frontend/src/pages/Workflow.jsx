// The three long workflows (pipelines, remediations, optimisations) share one page layout: start form, list,
// detail with events, the Markdown report and the code diff, resume / stop.
import { useCallback, useState } from 'react'
import { can, get, getText, post } from '../lib/api'
import { go, usePoll, when } from '../lib/hooks'
import { useLive } from '../lib/live'
import { Badge, Card, Diff, ErrorBox, Markdown, Table } from '../lib/ui'

function Detail({ kind, id, role }) {
  const item = usePoll(() => get(`/${kind.api}/${id}`), 4000, [id])
  const events = usePoll(() => get(`/${kind.api}/${id}/events`), 4000, [id])
  const [tab, setTab] = useState('events')
  const [report, setReport] = useState(null)
  const [err, setErr] = useState(null)
  const x = item.data
  const act = async (what) => {
    setErr(null)
    try {
      await post(`/${kind.api}/${id}/${what}`)
      item.reload()
    } catch (e) {
      setErr(e)
    }
  }
  const showReport = async () => {
    setTab('report')
    try {
      setReport(await getText(`/${kind.api}/${id}/report`))
    } catch (e) {
      setReport(`(${e.message})`)
    }
  }
  if (!x) return <p className="muted">loading ...</p>
  const finished = ['FINISHED', 'FAILED', 'STOPPED'].includes(x.status)
  return (
    <>
      <Card title={`${kind.one} #${x.id}`} actions={<>
        <Badge value={x.status} /> <Badge value={x.outcome} /> <span className="muted">stage {x.stage}</span>
        {can(role, 'agent') && x.status === 'WAITING_APPROVAL' && <button onClick={() => act('resume')}>resume</button>}
        {can(role, 'agent') && !finished && <button onClick={() => act('stop')}>stop</button>}
      </>}>
        <p>{kind.describe(x)}</p>
        {x.status === 'WAITING_APPROVAL' && <div className="notice">Waiting for a human decision. Decide in the{' '}
          <a href="#/approvals">approval centre</a>, then press resume.</div>}
        {x.error && <div className="error">{x.error}</div>}
        <ErrorBox error={err} />
        <div className="tabs">
          <button className={tab === 'events' ? 'on' : ''} onClick={() => setTab('events')}>events</button>
          {kind.diff && <button className={tab === 'diff' ? 'on' : ''} onClick={() => setTab('diff')}>code diff</button>}
          <button className={tab === 'report' ? 'on' : ''} disabled={!finished} onClick={showReport}>report</button>
        </div>
        {tab === 'events' && <Table rows={events.data?.events} cols={[{ label: 'time', render: (e) => e.ts.slice(11, 19) },
          { key: 'stage', label: 'stage' }, { label: 'kind', render: (e) => <Badge value={e.kind} tone={{ done: 'ok', failed: 'bad', warning: 'warn', waiting: 'warn', fix: 'info' }[e.kind]} /> },
          { key: 'message', label: 'message' }]} />}
        {tab === 'diff' && <Diff text={x.diff_text} />}
        {tab === 'report' && <Markdown text={report} />}
      </Card>
    </>
  )
}

export function workflowPage(kind) {
  return function WorkflowPage({ role, params }) {
    const id = params?.[0]
    const list = usePoll(() => get(`/${kind.api}?limit=30`), 10000)
    const [form, setForm] = useState(kind.initial)
    const [err, setErr] = useState(null)
    const reloadList = list.reload
    useLive(useCallback((m) => { if (m.type === 'job' && m.job.kind === kind.job) reloadList() }, [reloadList]))
    if (id) return <><p><a href={`#/${kind.path}`}>all {kind.many}</a></p><Detail kind={kind} id={id} role={role} /></>
    const start = async () => {
      setErr(null)
      try {
        const r = await post(`/${kind.api}`, kind.body(form))
        go(`${kind.path}/${r.id}`)
      } catch (e) {
        setErr(e)
      }
    }
    return (
      <>
        <h2>{kind.title}</h2>
        <p className="muted">{kind.intro}</p>
        {can(role, 'agent') && (
          <Card title={`Start a ${kind.one.toLowerCase()}`}>
            <div className="form">
              {kind.fields(form, setForm)}
              <div className="row"><button className="primary" onClick={start}>Start</button></div>
              <ErrorBox error={err} />
            </div>
          </Card>
        )}
        <Card title={kind.many}>
          <Table rows={list.data} onRow={(r) => go(`${kind.path}/${r.id}`)} cols={[{ key: 'id', label: '#' },
            { label: 'status', render: (r) => <Badge value={r.status} /> }, { label: 'outcome', render: (r) => <Badge value={r.outcome} /> },
            { key: 'stage', label: 'stage' }, { label: 'what', render: (r) => kind.describe(r).slice(0, 100) },
            { label: 'when', render: (r) => when(r.created_at) }]} />
        </Card>
      </>
    )
  }
}

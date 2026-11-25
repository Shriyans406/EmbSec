import { useCallback, useState } from 'react'
import { can, get, post } from '../lib/api'
import { usePoll, when } from '../lib/hooks'
import { useLive } from '../lib/live'
import { Badge, Card, ErrorBox, Json, Stat, Table } from '../lib/ui'

const QUICK = ['PING', 'VERSION', 'STATUS', 'RESETINFO', 'TEMP', 'PROF', 'LED TOGGLE']

export default function Device({ role }) {
  const status = usePoll(() => get('/device/status'), 8000)
  const [busy, setBusy] = useState(null)
  useLive(useCallback((m) => { if (m.type === 'board') setBusy(m.busy) }, []))
  const st = status.data
  const info = st?.info
  const history = usePoll(() => (info?.uid ? get(`/boards/${info.uid}/history`) : Promise.resolve(null)), 30000,
    [info?.uid])
  const [cmd, setCmd] = useState('PING')
  const [log, setLog] = useState([])
  const [err, setErr] = useState(null)
  const send = async (text) => {
    setErr(null)
    try {
      const r = await post('/device/command', { command: text })
      setLog((l) => [{ at: new Date(), ...r }, ...l].slice(0, 30))
    } catch (e) {
      setErr(e)
    }
  }
  const connected = st?.connected
  return (
    <>
      <h2>Device</h2>
      {connected === false && (
        <div className="error">The board is not connected: {st.error}. Plug the Shrike Lite in (USB), then wait a
          few seconds. Builds, the knowledge base and reports keep working without it.</div>
      )}
      {(busy || st?.busy) && <div className="notice">The board is in use by: {busy || st.busy}</div>}
      <div className="stats">
        <Stat label="connection" value={connected === null ? 'busy' : connected ? 'connected' : 'not connected'}
          tone={connected ? 'ok' : connected === false ? 'bad' : 'warn'} />
        <Stat label="firmware" value={info ? `${info.firmware} ${info.version}` : '-'} />
        <Stat label="port" value={info?.port} />
        <Stat label="board id" value={info?.uid} />
      </div>
      <div className="grid">
        <Card title="Command console" actions={<button onClick={status.reload}>refresh status</button>}>
          {can(role, 'agent') ? (
            <>
              <form className="row" onSubmit={(e) => { e.preventDefault(); send(cmd) }}>
                <input value={cmd} onChange={(e) => setCmd(e.target.value)} aria-label="command" style={{ flex: 1 }} />
                <button className="primary" disabled={!connected}>send</button>
              </form>
              <div className="row" style={{ marginTop: 8 }}>
                {QUICK.map((q) => <button key={q} disabled={!connected} onClick={() => send(q)}>{q}</button>)}
              </div>
              <p className="muted">Only safe commands are accepted; the board cannot be rebooted, hung or flashed
                from here.</p>
            </>
          ) : <p className="muted">Sending commands needs the agent role.</p>}
          <ErrorBox error={err} />
          <Table rows={log} empty="No commands sent yet." cols={[
            { label: 'time', render: (r) => r.at.toLocaleTimeString() }, { label: 'command', render: (r) => <code>{r.command}</code> },
            { label: 'reply', render: (r) => <code>{r.reply}</code> }, { label: 'ms', key: 'latency_ms' }]} />
        </Card>
        <Card title="Firmware on this board over time">
          <Table rows={history.data?.firmware_periods} empty="No deployments recorded for this board." cols={[
            { label: 'from', render: (r) => when(r.from) }, { label: 'until', render: (r) => (r.until ? when(r.until) : <Badge value="now" tone="ok" />) },
            { label: 'build', render: (r) => `#${r.build_id}` }, { key: 'project', label: 'project' },
            { key: 'approved_by', label: 'approved by' }]} />
        </Card>
      </div>
      {st && <Card title="Raw status"><Json value={st} /></Card>}
    </>
  )
}

import { useCallback, useState } from 'react'
import { get } from '../lib/api'
import { usePoll, when } from '../lib/hooks'
import { useLive } from '../lib/live'
import { Badge, Card, Table } from '../lib/ui'

export default function Audit() {
  const [action, setAction] = useState('')
  const log = usePoll(() => get(`/audit?limit=200${action ? `&action=${encodeURIComponent(action)}` : ''}`), 15000, [action])
  const reload = log.reload
  useLive(useCallback((m) => { if (m.type === 'audit') reload() }, [reload]))
  return (
    <>
      <h2>Audit log</h2>
      <p className="muted">Every change through the API, every refused attempt, every tool call of an agent - who,
        what, on which target, with which outcome.</p>
      <Card title="Entries" actions={<input placeholder="filter by action, e.g. tool." value={action} onChange={(e) => setAction(e.target.value)} />}>
        <Table rows={log.data} cols={[{ key: 'id', label: '#' }, { label: 'when', render: (r) => when(r.ts) },
          { key: 'actor', label: 'who' }, { key: 'role', label: 'role' }, { key: 'action', label: 'action' },
          { label: 'target', render: (r) => <code>{(r.target || '').slice(0, 80)}</code> },
          { label: 'outcome', render: (r) => <Badge value={r.outcome} tone={/^(ok|2\d\d)$/.test(r.outcome || '') ? 'ok' : /refused|40[13]|error/.test(r.outcome || '') ? 'bad' : 'muted'} /> },
          { key: 'request_id', label: 'request' }]} />
      </Card>
    </>
  )
}

import { useEffect, useState } from 'react'
import { api, can, get, saveSession, session } from './lib/api'
import { ErrorBox } from './lib/ui'
import { PAGES } from './pages'

function useHash() {
  const [hash, setHash] = useState(() => location.hash.slice(2) || 'overview')
  useEffect(() => {
    const on = () => setHash(location.hash.slice(2) || 'overview')
    addEventListener('hashchange', on)
    return () => removeEventListener('hashchange', on)
  }, [])
  return hash
}

function Login({ onLogin }) {
  const [token, setToken] = useState('')
  const [err, setErr] = useState(null)
  const [busy, setBusy] = useState(false)
  const submit = async (e) => {
    e.preventDefault()
    setBusy(true)
    setErr(null)
    try {
      const info = await api('/system/info', { token: token.trim() })
      const s = { token: token.trim(), role: info.you.role, version: info.version }
      saveSession(s)
      onLogin(s)
    } catch (ex) {
      setErr(ex)
    } finally {
      setBusy(false)
    }
  }
  return (
    <div className="login">
      <form onSubmit={submit} className="card">
        <h1>AEISP Control Room</h1>
        <p className="muted">Agentic Embedded Intelligence &amp; Security Platform. Sign in with the API token of
          your role (viewer, agent or operator). Tokens are in the project's <code>.env</code>; run
          <code> python -m app.cli show-tokens</code> on the lab computer.</p>
        <input type="password" placeholder="API token" value={token} onChange={(e) => setToken(e.target.value)}
          aria-label="API token" autoFocus />
        <ErrorBox error={err} />
        <button className="primary" disabled={!token.trim() || busy}>Sign in</button>
      </form>
    </div>
  )
}

export default function App() {
  const [s, setS] = useState(session)
  const [health, setHealth] = useState(null)
  const path = useHash()
  const [section, ...rest] = path.split('/')
  const page = PAGES.find((p) => p.path === section) || PAGES[0]

  useEffect(() => {
    get('/health').then(setHealth).catch(() => setHealth(null))
    const t = setInterval(() => get('/health').then(setHealth).catch(() => setHealth(null)), 10000)
    return () => clearInterval(t)
  }, [])

  if (!s) return <Login onLogin={setS} />
  const Page = page.component
  return (
    <div className="shell">
      <aside>
        <div className="brand">AEISP<small>control room</small></div>
        <nav>
          {PAGES.filter((p) => can(s.role, p.need || 'viewer')).map((p) => (
            <a key={p.path} href={`#/${p.path}`} className={p.path === page.path ? 'active' : ''}>{p.label}</a>
          ))}
        </nav>
        <div className="who">
          <div><span className={`dot ${health ? 'ok' : 'bad'}`} /> backend {health ? `${health.version}` : 'offline'}</div>
          <div>role <b>{s.role}</b></div>
          <button onClick={() => { saveSession(null); setS(null) }}>Sign out</button>
        </div>
      </aside>
      <main>
        <Page role={s.role} params={rest} />
      </main>
    </div>
  )
}

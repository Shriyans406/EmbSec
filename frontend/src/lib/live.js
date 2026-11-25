// One WebSocket to the backend's live feed, shared by every page, with automatic reconnect.
import { useEffect, useState } from 'react'
import { session, wsUrl } from './api'

const listeners = new Set()
const stateListeners = new Set()
let ws = null
let state = 'closed'
let retry = 1000
let timer = null
export const board = { connected: null, busy: null }

function setState(s) {
  state = s
  stateListeners.forEach((f) => f(s))
}

function open() {
  const s = session()
  if (!s || ws) return
  setState('connecting')
  ws = new WebSocket(wsUrl(s.token))
  ws.onopen = () => {
    retry = 1000
    setState('live')
  }
  ws.onmessage = (e) => {
    let msg
    try {
      msg = JSON.parse(e.data)
    } catch {
      return
    }
    if (msg.type === 'board') Object.assign(board, { connected: msg.connected, busy: msg.busy })
    listeners.forEach((f) => f(msg))
  }
  ws.onclose = (e) => {
    ws = null
    setState(e.code === 4401 ? 'refused' : 'closed')
    if (e.code !== 4401 && session()) {
      clearTimeout(timer)
      timer = setTimeout(open, retry)
      retry = Math.min(retry * 2, 15000)
    }
  }
}

export function closeLive() {
  clearTimeout(timer)
  if (ws) ws.close()
}

// useLive(handler): call handler(msg) for every event; returns the connection state.
export function useLive(handler) {
  const [s, setS] = useState(state)
  useEffect(() => {
    open()
    if (handler) listeners.add(handler)
    stateListeners.add(setS)
    return () => {
      if (handler) listeners.delete(handler)
      stateListeners.delete(setS)
    }
  }, [handler])
  return s
}

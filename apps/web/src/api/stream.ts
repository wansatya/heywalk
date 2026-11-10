// WebSocket client for live run events (AGENTS.md section 10:
// WS /api/runs/{run_id}/stream). Auto-reconnects with backoff while the
// run is still live; terminal states close the socket for good.
//
// When the handshake never opens (a proxy or firewall between the browser
// and the server blocks the WebSocket upgrade — e.g. a TLS terminator
// without Upgrade forwarding), the client falls back to polling the REST
// telemetry endpoint so the live view keeps working, marked as 'polling'.

import { getRun, getRunTelemetry, streamUrl } from './client'
import {
  isTerminalStatus,
  type EvaluationReport,
  type RunMetrics,
  type RunStatus,
  type StreamEvent,
  type TelemetrySample,
} from './types'

export type StreamConnectionState =
  | 'disconnected'
  | 'connecting'
  | 'live'
  | 'reconnecting'
  | 'polling'
  | 'closed'

export interface StreamHandlers {
  onEvent: (event: StreamEvent) => void
  onStateChange: (state: StreamConnectionState) => void
}

const MAX_ATTEMPTS = 6
const BASE_DELAY_MS = 500
/** Server-side close code for "unknown run" (see app/api/stream.py). */
const CLOSE_UNKNOWN_RUN = 4404
/** Never-opened handshake failures before the polling fallback engages. */
const FALLBACK_AFTER_FAILURES = 2
/** REST polling cadence while the WebSocket is unavailable. */
const POLL_INTERVAL_MS = 1000
/** Consecutive poll failures before the fallback gives up. */
const MAX_POLL_FAILURES = 5

/** Human hint for the close code that killed the stream. */
function describeClose(code: number | null, connectedOnce: boolean): string {
  if (code === 1006 && !connectedOnce) {
    return ' (the connection never opened — something between the browser and the server may block the WebSocket handshake)'
  }
  if (code === 1006) return ' (the connection dropped mid-run)'
  if (code === 1011) return ' (the server closed the stream with an error — check the API logs)'
  if (code !== null) return ` (last close: ${code})`
  return ''
}

function parseEvent(data: unknown): StreamEvent | null {
  if (typeof data !== 'object' || data === null) return null
  const record = data as Record<string, unknown>
  switch (record.type) {
    case 'status':
      return { type: 'status', status: record.status as RunStatus }
    case 'log':
      return { type: 'log', line: String(record.line ?? record.message ?? '') }
    case 'telemetry':
      return { type: 'telemetry', sample: (record.sample ?? record) as TelemetrySample }
    case 'evaluation':
      return {
        type: 'evaluation',
        evaluation: record.evaluation as EvaluationReport,
        metrics: (record.metrics ?? null) as RunMetrics | null,
      }
    case 'error':
      return { type: 'error', message: String(record.message ?? 'Stream error') }
    default:
      return null
  }
}

export interface StreamOptions {
  /** Newest preloaded telemetry timestamp; the fallback emits only newer samples. */
  lastTelemetryT?: number
}

export function connectRunStream(
  runId: string,
  handlers: StreamHandlers,
  options: StreamOptions = {},
): () => void {
  let socket: WebSocket | null = null
  let attempts = 0
  let closed = false
  let reconnectTimer = 0
  let lastStatus: RunStatus | null = null
  let lastCloseCode: number | null = null
  let connectedOnce = false
  let polling = false
  let pollTimer = 0
  let pollFailures = 0
  let lastT = options.lastTelemetryT ?? -1

  const setState = (state: StreamConnectionState) => handlers.onStateChange(state)

  function open() {
    if (closed) return
    if (lastStatus && isTerminalStatus(lastStatus)) {
      setState('closed')
      return
    }
    setState(attempts === 0 ? 'connecting' : 'reconnecting')
    let ws: WebSocket
    try {
      ws = new WebSocket(streamUrl(runId))
    } catch {
      scheduleRetry()
      return
    }
    socket = ws

    ws.onopen = () => {
      attempts = 0
      connectedOnce = true
      setState('live')
    }
    ws.onmessage = (msg) => {
      let parsed: unknown
      try {
        parsed = JSON.parse(msg.data as string)
      } catch {
        handlers.onEvent({ type: 'log', line: String(msg.data) })
        return
      }
      const event = parseEvent(parsed)
      if (!event) return
      if (event.type === 'status') {
        lastStatus = event.status
        if (isTerminalStatus(event.status)) {
          handlers.onEvent(event)
          cleanup()
          setState('closed')
          return
        }
      }
      handlers.onEvent(event)
    }
    ws.onerror = () => {
      // onclose follows; retry logic lives there.
    }
    ws.onclose = (ev) => {
      socket = null
      if (closed) return
      if (lastStatus && isTerminalStatus(lastStatus)) {
        setState('closed')
        return
      }
      if (ev.code === CLOSE_UNKNOWN_RUN) {
        // The server already sent its "Unknown run" error event, so just
        // stop — retrying a run the server does not know cannot help.
        setState('closed')
        return
      }
      lastCloseCode = ev.code
      scheduleRetry()
    }
  }

  function scheduleRetry() {
    if (closed || polling) return
    attempts += 1
    if (!connectedOnce && attempts >= FALLBACK_AFTER_FAILURES) {
      startPolling()
      return
    }
    if (attempts > MAX_ATTEMPTS) {
      setState('closed')
      handlers.onEvent({
        type: 'error',
        message: `Live stream disconnected after repeated retries${describeClose(lastCloseCode, connectedOnce)}. Telemetry below may be incomplete.`,
      })
      return
    }
    setState('reconnecting')
    reconnectTimer = window.setTimeout(open, BASE_DELAY_MS * 2 ** (attempts - 1))
  }

  function startPolling() {
    if (closed || polling) return
    polling = true
    pollFailures = 0
    setState('polling')
    handlers.onEvent({
      type: 'log',
      line: 'Live stream unavailable (WebSocket handshake blocked) — polling telemetry instead.',
    })
    void pollOnce()
  }

  async function pollOnce(): Promise<void> {
    if (closed || !polling) return
    try {
      const [run, samples] = await Promise.all([getRun(runId), getRunTelemetry(runId)])
      // The run may have been deselected while the requests were in flight.
      if (closed || !polling) return
      pollFailures = 0
      const fresh = samples.filter((s) => s.t > lastT)
      if (fresh.length > 0) {
        lastT = fresh[fresh.length - 1].t
        for (const sample of fresh) handlers.onEvent({ type: 'telemetry', sample })
      }
      if (run.status !== lastStatus) {
        lastStatus = run.status
        handlers.onEvent({ type: 'status', status: run.status })
      }
      if (isTerminalStatus(run.status)) {
        polling = false
        setState('closed')
        return
      }
    } catch {
      pollFailures += 1
      if (pollFailures >= MAX_POLL_FAILURES) {
        polling = false
        setState('closed')
        handlers.onEvent({
          type: 'error',
          message:
            'Live updates unavailable (WebSocket blocked and telemetry polling failed). Showing last loaded data.',
        })
        return
      }
    }
    pollTimer = window.setTimeout(() => void pollOnce(), POLL_INTERVAL_MS)
  }

  function cleanup() {
    window.clearTimeout(reconnectTimer)
    window.clearTimeout(pollTimer)
    polling = false
    if (socket && socket.readyState === WebSocket.OPEN) socket.close()
    socket = null
  }

  open()

  return () => {
    closed = true
    cleanup()
    setState('closed')
  }
}

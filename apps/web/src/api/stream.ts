// WebSocket client for live run events (AGENTS.md section 10:
// WS /api/runs/{run_id}/stream). Auto-reconnects with backoff while the
// run is still live; terminal states close the socket for good.

import { streamUrl } from './client'
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
  | 'closed'

export interface StreamHandlers {
  onEvent: (event: StreamEvent) => void
  onStateChange: (state: StreamConnectionState) => void
}

const MAX_ATTEMPTS = 6
const BASE_DELAY_MS = 500

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

export function connectRunStream(runId: string, handlers: StreamHandlers): () => void {
  let socket: WebSocket | null = null
  let attempts = 0
  let closed = false
  let reconnectTimer = 0
  let lastStatus: RunStatus | null = null

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
    ws.onclose = () => {
      socket = null
      if (closed) return
      if (lastStatus && isTerminalStatus(lastStatus)) {
        setState('closed')
        return
      }
      scheduleRetry()
    }
  }

  function scheduleRetry() {
    if (closed) return
    if (attempts >= MAX_ATTEMPTS) {
      setState('closed')
      handlers.onEvent({
        type: 'error',
        message: 'Live stream disconnected after repeated retries. Telemetry below may be incomplete.',
      })
      return
    }
    attempts += 1
    setState('reconnecting')
    reconnectTimer = window.setTimeout(open, BASE_DELAY_MS * 2 ** (attempts - 1))
  }

  function cleanup() {
    window.clearTimeout(reconnectTimer)
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

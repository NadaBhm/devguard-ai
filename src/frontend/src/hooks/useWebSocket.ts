import { useCallback, useEffect, useRef, useState } from "react"
import { tokenStore } from "../api/tokenStore"

export interface ProgressEvent {
  type: "progress"
  phase: string
  progress: number
  message: string
}

export interface GateEvent {
  type: "gate"
  gate: string
  status: string
}

export interface ResultsReadyEvent {
  type: "results_ready"
}

export interface RagAnswerEvent {
  type: "rag_answer"
  answer: string
  sources: string[]
}

export interface WsErrorEvent {
  type: "error"
  message: string
}

export interface PongEvent {
  type: "pong"
}

export type WsEvent =
  | ProgressEvent
  | GateEvent
  | ResultsReadyEvent
  | RagAnswerEvent
  | WsErrorEvent
  | PongEvent

export type ConnectionStatus = "connecting" | "open" | "closed"

function wsUrl(jobId: string): string {
  const proto = window.location.protocol === "https:" ? "wss:" : "ws:"
  const token = tokenStore.access ?? ""
  return `${proto}//${window.location.host}/ws/jobs/${jobId}?token=${encodeURIComponent(token)}`
}

export function useWebSocket(jobId: string | undefined) {
  const [status, setStatus] = useState<ConnectionStatus>("closed")
  const [events, setEvents] = useState<WsEvent[]>([])
  const socketRef = useRef<WebSocket | null>(null)
  const timerRef = useRef<number | null>(null)
  const eventsRef = useRef<WsEvent[]>([])
  const jobIdRef = useRef(jobId)

  useEffect(() => {
    jobIdRef.current = jobId
  }, [jobId])

  const pushEvent = useCallback((event: WsEvent) => {
    eventsRef.current = [...eventsRef.current.slice(-199), event]
    setEvents(eventsRef.current)
  }, [])

  useEffect(() => {
    if (!jobId) return
    eventsRef.current = []
    setEvents([])
  }, [jobId])

  const disconnect = useCallback(() => {
    if (timerRef.current !== null) {
      window.clearTimeout(timerRef.current)
      timerRef.current = null
    }
    socketRef.current?.close()
    socketRef.current = null
    setStatus("closed")
  }, [])

  useEffect(() => {
    if (!jobId) return

    let closedByCleanup = false
    let attempt = 0

    const connect = () => {
      const id = jobIdRef.current
      if (!id) return
      setStatus("connecting")
      const socket = new WebSocket(wsUrl(id))
      socketRef.current = socket

      socket.onopen = () => {
        attempt = 0
        setStatus("open")
      }

      socket.onmessage = (msg) => {
        try {
          const data = JSON.parse(msg.data as string) as WsEvent
          pushEvent(data)
        } catch {
          /* ignore malformed frames */
        }
      }

      socket.onclose = (event) => {
        socketRef.current = null
        if (closedByCleanup || !id) return
        setStatus("closed")
        if (event.code === 4401) return
        const delay = Math.min(1000 * 2 ** attempt, 15000)
        attempt += 1
        timerRef.current = window.setTimeout(connect, delay)
      }
    }

    connect()
    return () => {
      closedByCleanup = true
      disconnect()
    }
  }, [jobId, disconnect, pushEvent])

  const send = useCallback(
    (payload: Record<string, unknown>) => {
      const socket = socketRef.current
      if (!socket || socket.readyState !== WebSocket.OPEN) return false
      socket.send(JSON.stringify(payload))
      return true
    },
    [],
  )

  const sendRagQuery = useCallback(
    (query: string) => send({ action: "rag_query", query }),
    [send],
  )

  return { status, events, send, sendRagQuery }
}
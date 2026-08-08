import { useEffect, useMemo, useRef, useState, type FormEvent } from "react"
import { useSearchParams } from "react-router-dom"
import { useQuery } from "@tanstack/react-query"
import { chatApi } from "../api/chat"
import { jobsApi } from "../api/jobs"
import { Spinner } from "../components/ui/Button"
import { Card } from "../components/ui/Card"
import { Input } from "../components/ui/Inputs"
import { IconWaves } from "../components/icons"
import type { ChatMessage } from "../types/chat"

function SendIcon() {
  return (
    <svg width="15" height="15" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M2 8 14 2.5 9.5 14 7.5 9.5z" />
      <path d="M7.5 9.5 14 2.5" />
    </svg>
  )
}

export function ChatPage() {
  const [, setParams] = useSearchParams()
  const [jobId, setJobId] = useState<string | undefined>(undefined)
  const [messages, setMessages] = useState<ChatMessage[]>([])
  const [input, setInput] = useState("")
  const [sending, setSending] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const bottomRef = useRef<HTMLDivElement>(null)

  const jobsQuery = useQuery({ queryKey: ["jobs"], queryFn: jobsApi.list })

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" })
  }, [messages])

  const runs = useMemo(() => jobsQuery.data?.jobs.slice(0, 25) ?? [], [jobsQuery.data])

  async function onSubmit(e: FormEvent) {
    e.preventDefault()
    const query = input.trim()
    if (!query || !jobId || sending) return

    const userMsg: ChatMessage = {
      id: crypto.randomUUID(),
      role: "user",
      content: query,
      sources: [],
      createdAt: new Date().toISOString(),
    }
    setMessages((m) => [...m, userMsg])
    setInput("")
    setSending(true)
    setError(null)
    try {
      const res = await chatApi.ask({ query, job_id: jobId })
      setMessages((m) => [
        ...m,
        {
          id: crypto.randomUUID(),
          role: "assistant",
          content: res.answer,
          sources: res.sources,
          createdAt: new Date().toISOString(),
        },
      ])
    } catch (err) {
      setError(err instanceof Error ? err.message : "The assistant could not answer that question.")
    } finally {
      setSending(false)
    }
  }

  return (
    <div className="mx-auto flex h-[calc(100vh-7rem)] max-w-4xl flex-col space-y-4">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-xl font-semibold tracking-tight text-foreground">RAG assistant</h1>
          <p className="mt-1 text-sm text-faint">Ask questions about an analyzed repository.</p>
        </div>
      </div>

      <Card bodyClassName="flex flex-1 flex-col min-h-0 overflow-hidden p-0">
        <div className="flex items-center justify-between gap-3 border-b border-border bg-surface-2 px-3 py-2">
          <span className="text-[12px] text-faint">Context</span>
          <select
            value={jobId ?? ""}
            onChange={(e) => {
              const v = e.target.value
              setJobId(v || undefined)
              setParams(v ? { job: v } : {})
            }}
            className="min-w-0 flex-1 rounded-md border border-border bg-surface px-2 py-1 text-[12px] text-foreground focus:outline-none"
            aria-label="Select analysis run"
          >
            <option value="">Select a run…</option>
            {runs.map((r) => (
              <option key={r.job_id} value={r.job_id}>
                {(r.repo_url ?? "unknown").replace(/^https?:\/\//, "")} · {r.job_id.slice(0, 8)}
              </option>
            ))}
          </select>
        </div>

        <div className="flex-1 space-y-3 overflow-y-auto px-4 py-4">
          {messages.length === 0 ? (
            <div className="flex h-full items-center justify-center">
              <div className="text-center">
                <div className="mx-auto mb-3 flex size-11 items-center justify-center rounded-lg border border-border bg-surface-2 text-muted">
                  <IconWaves className="size-5" />
                </div>
                <p className="text-sm font-medium text-foreground">Ask anything about the codebase</p>
                <p className="mt-1 max-w-sm text-[13px] text-faint">
                  e.g. “What secrets were found?” or “How is the cost estimate broken down by resource?”
                </p>
              </div>
            </div>
          ) : (
            messages.map((m) => (
              <div key={m.id} className={`flex ${m.role === "user" ? "justify-end" : "justify-start"}`}>
                <div
                  className={`max-w-[80%] rounded-lg px-3.5 py-2.5 text-[13px] leading-relaxed ${
                    m.role === "user"
                      ? "border border-accent/30 bg-accent/10 text-foreground"
                      : "border border-border bg-surface-2 text-foreground"
                  }`}
                >
                  {m.content}
                  {m.sources.length > 0 && (
                    <div className="mt-2 flex flex-wrap gap-1.5">
                      {m.sources.map((s, i) => (
                        <span key={i} className="font-mono rounded border border-border bg-canvas px-1.5 py-0.5 text-[10.5px] text-faint">
                          {s}
                        </span>
                      ))}
                    </div>
                  )}
                </div>
              </div>
            ))
          )}
          {sending && (
            <div className="flex items-center gap-2 text-[12px] text-faint">
              <Spinner size={14} /> thinking…
            </div>
          )}
          {error && <p className="text-[12px] text-critical">{error}</p>}
          <div ref={bottomRef} />
        </div>

        <form onSubmit={onSubmit} className="flex items-center gap-2 border-t border-border px-4 py-3">
          <Input
            value={input}
            onChange={(e) => setInput(e.target.value)}
            placeholder={jobId ? "Ask about this repository…" : "Select a run to begin"}
            disabled={!jobId || sending}
          />
          <button
            type="submit"
            aria-label="Send"
            disabled={!jobId || !input.trim() || sending}
            className="flex size-10 shrink-0 items-center justify-center rounded-md bg-surface-2 text-muted border border-border transition-colors hover:text-accent disabled:opacity-40"
          >
            <SendIcon />
          </button>
        </form>
      </Card>
    </div>
  )
}
export interface ChatRequest {
  query: string
  job_id: string
}

export interface ChatResponse {
  answer: string
  sources: string[]
}

export interface ChatMessage {
  id: string
  role: "user" | "assistant"
  content: string
  sources: string[]
  createdAt: string
}
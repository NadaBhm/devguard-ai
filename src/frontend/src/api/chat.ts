import { client } from "./client"
import type { ChatRequest, ChatResponse } from "../types/chat"

export const chatApi = {
  ask: (req: ChatRequest) => client.post<ChatResponse>("/chat/ask", req),
  ingest: (repo_path: string, job_id: string) =>
    client.post<{ status: string; chunks_ingested: number }>("/chat/ingest", { repo_path, job_id }),
}
// Calls for the client's upload page: what an email's link asks for, and a file sent for one of those documents.
// Paths go through the Next rewrite like every other call, and each sends the X-LedgerSync header, as api.ts does.
import { ApiError, request } from './api.ts'

export interface RequestedDocument {
  id: string
  document_type: string
  period: string
  reason: string
  status: 'pending' | 'received'
}

export interface RequestedDocuments {
  client_name: string
  documents: RequestedDocument[]
}

interface Reading {
  status: 'queued' | 'running' | 'succeeded' | 'failed' | 'cancelled'
  error: { code: string; message: string } | null
}

// The API takes files only with this header, which a form on another web site can't send.
const HEADERS = { 'X-LedgerSync': '1' }
const POLL_INTERVAL_MS = 1_000

const sleep = (ms: number) => new Promise(resolve => setTimeout(resolve, ms))
const linkPath = (token: string) => `/api/requests/${encodeURIComponent(token)}`

// The client's name and the documents the link asks for. An unknown link is an ApiError with code 'not_found'.
export function getRequests(token: string): Promise<RequestedDocuments> {
  return request<RequestedDocuments>(linkPath(token), { headers: HEADERS })
}

// Sends the file for one document, then waits while the API reads it. Throws an ApiError with the API's reason
// when the file is refused or can't be read.
export async function uploadRequested(token: string, requestId: string, file: File): Promise<void> {
  const body = new FormData()
  body.append('file', file)
  const path = `${linkPath(token)}/${encodeURIComponent(requestId)}/file`
  const { job_id } = await request<{ job_id: string }>(path, { method: 'POST', body, headers: HEADERS }, 120_000)
  for (;;) {
    const reading = await request<Reading>(`/api/jobs/${encodeURIComponent(job_id)}`, { headers: HEADERS })
    if (reading.status === 'succeeded') return
    if (reading.status === 'failed' || reading.status === 'cancelled') {
      throw new ApiError(reading.error?.message ?? 'The file could not be read. Try again.', reading.error?.code ?? reading.status)
    }
    await sleep(POLL_INTERVAL_MS)
  }
}

// Whether every document the link asks for has arrived.
export function allReceived(documents: RequestedDocument[]): boolean {
  return documents.length > 0 && documents.every(d => d.status === 'received')
}

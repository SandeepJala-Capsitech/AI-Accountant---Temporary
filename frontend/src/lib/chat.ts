// The accountant chat (design of 2026-11-02): its threads, the review summary a turn ends with, the questions it
// asks and the posted trial balance, and the calls that send a message (streamed back as SSE) and answer a question.
// Paths go through the Next rewrite, like every other call in api.ts. The pure functions are tested in chat.test.ts.
import { ApiError, errorFrom, request, type TrialBalanceLine, type TrialBalanceResult } from './api.ts'

// Money is a string everywhere below, like the rest of the API.
export interface ChatThread {
  id: string
  client_id: number
  year_start: string         // the client's year the chat works on: "2026-04-01"
  title: string              // "New chat" until the first message
  created_at: string
  updated_at: string
}

export interface Evidence {
  upload_id: number
  upload_name: string
  quote: string
}

// One thing the agent (or the rules, or a person's answer) did to the books, as the decisions log keeps it.
export interface Decision {
  id: string
  client_id: number
  thread_id: string | null
  year_start: string
  action: string
  rule: string               // "R2 Duplicate: kept the receipt"
  status: 'applied' | 'refused'
  row_ids: number[]
  evidence: Evidence[]
  reason: string
  before: Record<string, Record<string, unknown>>   // the changed fields of each row, keyed by its id
  after: Record<string, Record<string, unknown>>
  actor: string              // "agent:<model>", "user" or "rules"
  reverses: string | null
  created_at: string
  prev_hash: string
  hash: string
}

export type QuestionKind = 'date' | 'receipt_or_invoice' | 'vat_invoice' | 'other'

// A question for the person: open until answered, waiting when they have no answer yet, resolved once answered.
export interface Question {
  id: string
  client_id: number
  year_start: string
  row_ids: number[]
  kind: QuestionKind
  text: string               // "When was this taxi?"
  why: string                // "Over £50, so I need ..."
  source: 'rules' | 'agent'
  status: 'open' | 'waiting' | 'resolved'
  answer: string | null      // "I don't have it", "Not sure", or the answer given
  created_at: string
  resolved_at: string | null
}

export interface SuspenseItem {
  row_id: number
  description: string
  gross: string
  needs: 'receipt_or_invoice' | 'vat_invoice'
}

// The trial balance of the posted ledger, with the rows held in Suspense among it.
export interface PostedTrialBalance extends TrialBalanceResult {
  suspense: SuspenseItem[]
  unposted_changes: number   // rows changed since they were posted
}

export interface PostResult {
  batch_id: string | null    // null when the posting wrote nothing
  entries_added: number
  reversals_added: number
  total_debits: string
  total_credits: string
  problems: string[]         // why nothing was written, when the batch failed its checks
}

// What a review turn (or an answer) ends with: what it sorted, what still needs the person, and the books posted.
export interface ReviewSummary {
  sorted: Decision[]
  questions: Question[]
  trial_balance: PostedTrialBalance
  post: PostResult
}

export interface ChatMessage {
  role: 'user' | 'assistant'
  text: string
}

// A thread as GET /api/chat/threads/{id} returns it: its messages so far and the summary its last turn ended with.
export interface ThreadDetail {
  thread: ChatThread
  messages: ChatMessage[]
  summary: ReviewSummary | null
}

export type StepState = 'running' | 'done' | 'failed'

// A step of a turn. A step is sent again under its id as it changes ("Reading a.csv…" → "Read a.csv (12 rows)").
// A step about a file names it, so the file's chip can show how it went.
export interface Step {
  id: string
  text: string
  state: StepState
  file?: string
  rows?: number
  reason?: string
}

export type ChatError = { code: 'model_unavailable' | 'loop_limit' | string; message: string; steps?: number }

export type ChatEvent =
  | { type: 'status'; data: { text: string } }
  | { type: 'step'; data: Step }
  | { type: 'token'; data: { text: string } }
  | { type: 'summary'; data: ReviewSummary }
  | { type: 'error'; data: ChatError }
  | { type: 'done'; data: Record<string, never> }

export type Answer = { kind: 'date'; date: string } | { kind: 'no_document' } | { kind: 'not_sure' }

const EVENT_TYPES = new Set<string>(['status', 'step', 'token', 'summary', 'error', 'done'])

// Parses SSE text that may stop anywhere: the events completed so far, and what is left over for the next chunk.
export function parseEvents(chunk: string, carry: string): { events: ChatEvent[]; carry: string } {
  const blocks = (carry + chunk).replace(/\r\n?/g, '\n').split('\n\n')
  const rest = blocks.pop() ?? ''
  const events: ChatEvent[] = []
  for (const block of blocks) {
    let type = 'message'
    const data: string[] = []
    for (const line of block.split('\n')) {
      if (line.startsWith(':')) continue
      const colon = line.indexOf(':')
      const field = colon < 0 ? line : line.slice(0, colon)
      const value = colon < 0 ? '' : line.slice(colon + 1).replace(/^ /, '')
      if (field === 'event') type = value
      else if (field === 'data') data.push(value)
    }
    if (!EVENT_TYPES.has(type) || !data.length) continue
    events.push({ type, data: JSON.parse(data.join('\n')) } as ChatEvent)
  }
  return { events, carry: rest }
}

const dayOf = (when: Date) => new Date(when.getFullYear(), when.getMonth(), when.getDate()).getTime()

// The thread list's groups, newest first in each, by the computer's own calendar; empty groups are left out.
export function groupThreads(threads: ChatThread[], now: Date):
    { label: 'Today' | 'Yesterday' | 'Earlier'; threads: ChatThread[] }[] {
  const today = dayOf(now)
  const yesterday = dayOf(new Date(now.getFullYear(), now.getMonth(), now.getDate() - 1))
  const groups = { Today: [] as ChatThread[], Yesterday: [] as ChatThread[], Earlier: [] as ChatThread[] }
  const newest = [...threads].sort((a, b) => Date.parse(b.updated_at) - Date.parse(a.updated_at))
  for (const t of newest) {
    const day = dayOf(new Date(t.updated_at))
    groups[day >= today ? 'Today' : day === yesterday ? 'Yesterday' : 'Earlier'].push(t)
  }
  return (['Today', 'Yesterday', 'Earlier'] as const)
    .filter(label => groups[label].length)
    .map(label => ({ label, threads: groups[label] }))
}

// How a question is answered: a date (or Not sure), a document (or I don't have it), or in words.
export function questionControl(q: Question): 'date' | 'upload' | 'text' {
  if (q.kind === 'date') return 'date'
  if (q.kind === 'receipt_or_invoice' || q.kind === 'vat_invoice') return 'upload'
  return 'text'
}

// "R2" from "R2 Duplicate: kept the receipt"; '' for a rule without a number.
export const ruleBadge = (d: Decision): string => /^R\d+\b/.exec(d.rule)?.[0] ?? ''

// "£1,412.68" and "-£1,412.68", from the exact decimal string, so no pence are lost to floating point.
export function formatMoney(s: string): string {
  const negative = s.trim().startsWith('-')
  const [whole, fraction = ''] = s.trim().replace(/^[-+]/, '').split('.')
  const pence = fraction.length > 2 ? Number(`0.${fraction}`).toFixed(2).slice(2) : fraction.padEnd(2, '0')
  const pounds = (whole.replace(/^0+(?=\d)/, '') || '0').replace(/\B(?=(\d{3})+(?!\d))/g, ',')
  const zero = /^[0,]+$/.test(pounds) && pence === '00'
  return `${negative && !zero ? '-' : ''}£${pounds}.${pence}`
}

export const suspenseLabel = (needs: SuspenseItem['needs']) =>
  needs === 'vat_invoice' ? 'needs VAT invoice' : 'needs receipt or invoice'

const STATEMENT_TYPES = /\.(csv|ofx|qfx|qif|xls|xlsx)$/i

// Whether a file is likely a bank statement, so its chip offers the bank account it goes on.
export const looksLikeStatement = (name: string) => STATEMENT_TYPES.test(name) || /statement/i.test(name)

export const SUSPENSE = '9998'
const SHOWN_ACCOUNTS = 5

// The accounts the Posted part shows: the first five with a balance, and Suspense always, with how many are left.
export function shownAccounts(tb: PostedTrialBalance): { shown: TrialBalanceLine[]; more: number } {
  const used = tb.lines.filter(l => Number(l.debit) !== 0 || Number(l.credit) !== 0)
  const shown = used.slice(0, SHOWN_ACCOUNTS)
  const suspense = used.find(l => l.code === SUSPENSE)
  if (suspense && !shown.includes(suspense)) shown.push(suspense)
  return { shown, more: used.length - shown.length }
}

const WRITE = { 'X-LedgerSync': '1' }
const json = (method: string, body: unknown): RequestInit => ({
  method, headers: { 'Content-Type': 'application/json', ...WRITE }, body: JSON.stringify(body),
})

// All threads, or one client's.
export const listThreads = (clientId?: number) =>
  request<ChatThread[]>(clientId ? `/api/chat/threads?client_id=${clientId}` : '/api/chat/threads')
export const newThread = (clientId: number) => request<ChatThread>('/api/chat/threads', json('POST', { client_id: clientId }))
export const getThread = (id: string) => request<ThreadDetail>(`/api/chat/threads/${id}`)
export const stopRun = (id: string) =>
  request<unknown>(`/api/chat/threads/${id}/stop`, { method: 'POST', headers: WRITE })
// The client's open and waiting questions, from every chat on it.
export const clientQuestions = (clientId: number) => request<Question[]>(`/api/clients/${clientId}/questions`)
export const resetDemo = () => request<unknown>('/api/demo/reset', { method: 'POST', headers: WRITE }, 120_000)

export const answerQuestion = (threadId: string, questionId: string, answer: Answer) =>
  request<ReviewSummary>(`/api/chat/threads/${threadId}/questions/${questionId}/answer`, json('POST', answer), 120_000)

export function uploadForQuestion(threadId: string, questionId: string, file: File): Promise<ReviewSummary> {
  const data = new FormData()
  data.append('file', file)
  return request<ReviewSummary>(`/api/chat/threads/${threadId}/questions/${questionId}/document`,
                                { method: 'POST', body: data, headers: WRITE }, 180_000)
}

// Sends a message with its files (bankAccounts[i] is the account files[i] goes on if it is a statement; '' for the
// client's first) and calls onEvent for each event until the stream ends. A refusal, such as a second message
// while one runs (409), throws ApiError with the API's reason.
export async function streamMessage(
  threadId: string,
  text: string,
  files: File[],
  bankAccounts: string[],
  onEvent: (event: ChatEvent) => void,
  signal: AbortSignal,
): Promise<void> {
  const data = new FormData()
  data.append('text', text)
  files.forEach(f => data.append('files', f))
  bankAccounts.forEach(code => data.append('bank_accounts', code))
  const res = await fetch(`/api/chat/threads/${threadId}/messages`, { method: 'POST', body: data, headers: WRITE, signal })
    .catch((e: unknown) => {
      if (signal.aborted) throw e
      throw new ApiError('Cannot reach the Super Accountant API. Is `python server.py` running?', 'network')
    })
  if (!res.ok) throw await errorFrom(res)
  if (!res.body) return
  const reader = res.body.getReader()
  const decoder = new TextDecoder()
  let carry = ''
  for (;;) {
    const { done, value } = await reader.read()
    const parsed = parseEvents(done ? decoder.decode() : decoder.decode(value, { stream: true }), carry)
    carry = parsed.carry
    parsed.events.forEach(onEvent)
    if (done) return
  }
}

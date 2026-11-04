// The accountant chat (design of 2026-11-02): its threads, the result a turn ends with, the questions it
// asks and the posted trial balance, and the calls that send a message (streamed back as SSE) and answer a question.
// Paths go through the Next rewrite, like every other call in api.ts. The pure functions are tested in chat.test.ts.
import { ApiError, errorFrom, request, type TrialBalanceLine, type TrialBalanceResult } from './api.ts'
import { longDate } from './ledger.ts'

// What the Overview's "N things need you" link asks in a new chat, so the open items show as cards in the reply.
export const ATTENTION = 'What needs my attention?'
export const attentionHref = (clientId: number) => `/chat?client=${clientId}&ask=${encodeURIComponent(ATTENTION)}`

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
  why: string                // "£50 or more, so I need ..."
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
  needs: 'receipt_or_invoice' | 'vat_invoice' | null   // null once the row is no longer held
}

// The trial balance of the posted ledger, with the rows held in Suspense among it.
export interface PostedTrialBalance extends TrialBalanceResult {
  suspense: SuspenseItem[]
  unposted_changes: number   // rows changed since they were posted
  problems: string[]         // what is wrong with the posted books, in words
}

export interface PostResult {
  batch_id: string | null    // null when the posting wrote nothing
  entries_added: number
  reversals_added: number
  total_debits: string
  total_credits: string
  problems: string[]         // why nothing was written, when the batch failed its checks
}

// How a file the person added was read: shown as its chip, and kept with the message.
export interface FileRead {
  name: string
  state: 'done' | 'failed'
  rows: number | null
  reason: string | null
}

// What one turn did: what it sorted, the questions it raised or listed, and the books if it posted or showed them.
export interface TurnResult {
  sorted: Decision[]
  questions: Question[]
  trial_balance: PostedTrialBalance | null
  post: PostResult | null
}

// What answering a question did: that question as it now stands, and the effect on the books.
export interface AnswerResult {
  question: Question
  sorted: Decision[]
  trial_balance: PostedTrialBalance | null
  post: PostResult | null
}

export interface ChatMessage {
  role: 'user' | 'assistant'
  text: string
  files: FileRead[]
  result: TurnResult | null
}

// A thread as GET /api/chat/threads/{id} returns it: its messages so far, each with the result its turn ended with.
export interface ThreadDetail {
  thread: ChatThread
  messages: ChatMessage[]
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
  | { type: 'result'; data: TurnResult }
  | { type: 'error'; data: ChatError }
  | { type: 'done'; data: Record<string, never> }

export type Answer = { kind: 'date'; date: string } | { kind: 'no_document' } | { kind: 'not_sure' }

const EVENT_TYPES = new Set<string>(['status', 'step', 'token', 'result', 'error', 'done'])

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

const FIELD_NAMES: Record<string, string> = {
  gross: 'Amount', vat: 'VAT', net: 'Net', date: 'Date', description: 'Description', counterparty: 'Payee',
  account_code: 'Account', direction: 'Direction', excluded: 'Removed from the books', document_type: 'Document',
}
const MONEY_FIELDS = new Set(['gross', 'vat', 'net', 'vat_posted', 'amount'])

const fieldValue = (field: string, value: unknown): string => {
  if (value == null || value === '') return '—'
  if (typeof value === 'boolean') return value ? 'Yes' : 'No'
  if (MONEY_FIELDS.has(field)) return formatMoney(String(value))
  if (field === 'date' && /^\d{4}-\d{2}-\d{2}$/.test(String(value))) return longDate(String(value))
  return typeof value === 'object' ? JSON.stringify(value) : String(value)
}

const fieldName = (field: string) => FIELD_NAMES[field] ?? field.charAt(0).toUpperCase() + field.slice(1).replace(/_/g, ' ')

// A decision's before → after, one line per changed field of each row, in words.
export function changedFields(d: Decision): { row: number; field: string; before: string; after: string }[] {
  const rows = [...new Set([...Object.keys(d.before), ...Object.keys(d.after)])].sort((a, b) => Number(a) - Number(b))
  return rows.flatMap(row => {
    const before = d.before[row] ?? {}
    const after = d.after[row] ?? {}
    return [...new Set([...Object.keys(before), ...Object.keys(after)])].map(field => ({
      row: Number(row), field: fieldName(field), before: fieldValue(field, before[field]), after: fieldValue(field, after[field]),
    }))
  })
}

export type FileState = 'queued' | 'reading' | 'done' | 'failed'

const readLabel = (state: 'done' | 'failed', rows: number | null | undefined, reason: string | null | undefined) =>
  state === 'failed' ? (reason ? `Failed: ${reason}` : 'Failed') : rows == null ? 'Read' : `Read (${rows} ${rows === 1 ? 'row' : 'rows'})`

// What a file's chip says, from the latest step that names it.
export function fileStatus(name: string, steps: Step[]): { state: FileState; label: string } {
  const step = [...steps].reverse().find(s => s.file === name)
  if (!step) return { state: 'queued', label: 'Queued' }
  if (step.state === 'running') return { state: 'reading', label: 'Reading' }
  return { state: step.state, label: readLabel(step.state, step.rows, step.reason) }
}

// What a saved file's chip says, from how it was read.
export const readStatus = (f: FileRead): { state: FileState; label: string } =>
  ({ state: f.state, label: readLabel(f.state, f.rows, f.reason) })

// Whether a turn's result has anything to show.
export const hasResult = (r: TurnResult | null): r is TurnResult =>
  r != null && (r.sorted.length > 0 || r.questions.length > 0 || r.trial_balance != null || r.post != null)

// The items with every card for this question replaced by its current record.
export const withQuestion = <T extends { result: TurnResult | null }>(items: T[], q: Question): T[] =>
  items.map(item => item.result?.questions.some(c => c.id === q.id)
    ? { ...item, result: { ...item.result, questions: item.result.questions.map(c => (c.id === q.id ? q : c)) } }
    : item)

// Where a question stands, in words; null while it is open.
export function questionState(q: Question): string | null {
  if (q.status === 'open') return null
  if (q.status === 'resolved') return q.answer ? `Answered: ${q.answer}` : 'Resolved'
  if (q.answer === "I don't have it") return q.kind === 'vat_invoice' ? 'Waiting for the VAT invoice' : 'Waiting for the receipt or invoice'
  if (q.answer === 'Not sure') return 'Waiting: not sure of the date yet'
  return `Waiting: ${q.answer}`
}

// What an answer did to the books, in one line; null when it changed nothing there.
export function effectLine(a: AnswerResult): string | null {
  if (a.post?.problems.length) return `Nothing was posted: ${a.post.problems.join(' ')}`
  if (!a.trial_balance) return null
  const held = a.trial_balance.suspense.length
  const suspense = held === 0 ? 'nothing in Suspense' : `${held} ${held === 1 ? 'item' : 'items'} in Suspense`
  return `Posted · ${a.trial_balance.is_balanced ? 'balanced' : 'not balanced'} · ${suspense}`
}

// The files a retry sends again: those not read the first time, each with its bank account. A file read once is
// saved to the client already, and sending it again would book it twice.
export function retryFiles(files: File[], bankAccounts: string[], steps: Step[]): { files: File[]; bankAccounts: string[] } {
  const keep = files.map((f, i) => [f, bankAccounts[i] ?? ''] as const).filter(([f]) => fileStatus(f.name, steps).state !== 'done')
  return { files: keep.map(([f]) => f), bankAccounts: keep.map(([, code]) => code) }
}

export const suspenseLabel = (needs: SuspenseItem['needs']) =>
  needs === 'vat_invoice' ? 'needs VAT invoice' : needs === 'receipt_or_invoice' ? 'needs receipt or invoice' : 'Suspense'

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
  request<AnswerResult>(`/api/chat/threads/${threadId}/questions/${questionId}/answer`, json('POST', answer), 120_000)

export function uploadForQuestion(threadId: string, questionId: string, file: File): Promise<AnswerResult> {
  const data = new FormData()
  data.append('file', file)
  return request<AnswerResult>(`/api/chat/threads/${threadId}/questions/${questionId}/document`,
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

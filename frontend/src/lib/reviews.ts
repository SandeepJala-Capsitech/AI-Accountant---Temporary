// The background AI review (design of 2026-11-04): the runs a client's books had after a change, what they did and
// asked, and the calls that list them, undo or apply a change and answer their questions. Paths go through the Next
// rewrite like every other call. The pure functions are tested in reviews.test.ts.
import { request } from './api.ts'
import type { Answer, AnswerResult, Decision, Question } from './chat.ts'

export type ReviewStatus = 'running' | 'done' | 'failed' | 'limited' | 'off'

export interface ReviewRun {
  id: string
  client_id: number
  year_start: string
  status: ReviewStatus
  started_at: string
  finished_at: string | null
  trigger_summary: string    // "after 3 files were added"
  from_seq: number
  to_seq: number
  decision_ids: string[]
  question_ids: string[]
  looked_at: number[]
  issues_seen?: string[]    // the issues it was shown, as "row:code:digest" keys
  summary: string
  error: string
  seen: boolean
}

export interface ReviewDetail extends ReviewRun {
  decisions: Decision[]
  questions: Question[]
  applied: Record<string, string>   // a suggestion's id -> the decision that applied it
}

const WRITE = { 'X-LedgerSync': '1' }
const post = (path: string) => request<unknown>(path, { method: 'POST', headers: WRITE })

export const listReviews = (clientId: number, yearStart: string) =>
  request<ReviewDetail[]>(`/api/clients/${clientId}/reviews?year_start=${encodeURIComponent(yearStart)}`)
export const markReviewsSeen = (clientId: number) => post(`/api/clients/${clientId}/reviews/seen`)
export const undoDecision = (clientId: number, id: string) =>
  request<Decision>(`/api/clients/${clientId}/decisions/${id}/undo`, { method: 'POST', headers: WRITE })
export const applySuggestion = (clientId: number, id: string) =>
  request<Decision>(`/api/clients/${clientId}/decisions/${id}/apply`, { method: 'POST', headers: WRITE })

// How a QuestionCard answers: through the client's own routes, with no chat.
export interface Answerer {
  answer: (questionId: string, a: Answer) => Promise<AnswerResult>
  upload: (questionId: string, file: File) => Promise<AnswerResult>
}

export const clientAnswerer = (clientId: number): Answerer => ({
  answer: (qid, a) => request<AnswerResult>(`/api/clients/${clientId}/questions/${qid}/answer`, {
    method: 'POST', headers: { 'Content-Type': 'application/json', ...WRITE }, body: JSON.stringify(a),
  }, 120_000),
  upload: (qid, file) => {
    const data = new FormData()
    data.append('file', file)
    return request<AnswerResult>(`/api/clients/${clientId}/questions/${qid}/document`,
                                 { method: 'POST', body: data, headers: WRITE }, 180_000)
  },
})

const pad = (n: number) => String(n).padStart(2, '0')
const clock = (d: Date) => `${pad(d.getHours())}:${pad(d.getMinutes())}`
const DAYS = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat']
const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']
const startOfDay = (d: Date) => new Date(d.getFullYear(), d.getMonth(), d.getDate())

// "Today 10:42", "Yesterday 10:42" or "Mon 3 Nov 10:42", in the computer's own clock.
export function runWhen(iso: string, now: Date = new Date()): string {
  const d = new Date(iso)
  const days = Math.round((startOfDay(now).getTime() - startOfDay(d).getTime()) / 86_400_000)
  const day = days === 0 ? 'Today' : days === 1 ? 'Yesterday' : `${DAYS[d.getDay()]} ${d.getDate()} ${MONTHS[d.getMonth()]}`
  return `${day} ${clock(d)}`
}

// "Today 10:42, after 3 files were added: sorted 2, asked 1"; "suggested" instead of "sorted" in suggest mode.
export function runTitle(run: ReviewDetail, now: Date = new Date()): string {
  const sorted = run.decisions.filter(d => d.status === 'applied' && !d.reverses).length
  const suggested = run.decisions.filter(d => d.status === 'suggested').length
  const asked = run.questions.length
  const parts = [
    sorted ? `sorted ${sorted}` : '',
    suggested ? `suggested ${suggested}` : '',
    asked ? `asked ${asked}` : '',
  ].filter(Boolean)
  const head = [runWhen(run.started_at, now), run.trigger_summary].filter(Boolean).join(', ')
  // A review still running has not yet found nothing: it says what it has done so far, or only when it started.
  if (!parts.length && run.status === 'running') return head
  return `${head}: ${parts.length ? parts.join(', ') : 'Nothing to change.'}`
}

// What the run said it did, or why it changed nothing, in its own words; "Nothing to change." when it said nothing.
export const runSaid = (run: ReviewDetail): string =>
  run.summary.trim() || (run.status === 'running' ? 'Looking through the books now.' : 'Nothing to change.')

// The Overview's line about the last run; null when the client has had none.
export function overviewReviewLine(lastRunAt: string | null, unseen: number): string | null {
  if (!lastRunAt) return null
  return `AI review: last run ${clock(new Date(lastRunAt))}. ${unseen} ${unseen === 1 ? 'change' : 'changes'} you haven't looked at`
}

// The changes the person hasn't looked at, as the server counts them for the clients list: what unseen finished
// runs applied (an undo is not one of them) or suggested.
export const unseenChanges = (runs: ReviewDetail[]): number =>
  runs.filter(r => !r.seen && r.status === 'done').reduce((n, r) =>
    n + r.decisions.filter(d => (d.status === 'applied' && !d.reverses) || d.status === 'suggested').length, 0)

// Whether a change in this run has been undone: another decision of the run reverses it.
export const isReversed = (run: ReviewDetail, decisionId: string): boolean => run.decisions.some(d => d.reverses === decisionId)

// The runs, newest first.
export const newestFirst = (runs: ReviewDetail[]): ReviewDetail[] =>
  [...runs].sort((a, b) => (a.started_at < b.started_at ? 1 : a.started_at > b.started_at ? -1 : 0))

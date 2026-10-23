// Calls for the agent job on a client's page: starting it, watching its activity feed, and what it leaves for the
// accountant (the trial balance's workbook, the exceptions and the document requests). The types match the API's
// models in ledgersync/records. Paths go through the Next rewrite like every other call, and each sends the
// X-LedgerSync header, as api.ts does.
import { request } from './api.ts'

export type JobStage = 'ingest' | 'classify' | 'gate' | 'fix' | 'requests' | 'snapshot' | 'done'

export interface AgentJob {
  id: string
  client_id: number
  period_start: string     // a date: 2025-04-01
  period_end: string
  status: 'preparing' | 'awaiting_review' | 'failed' | 'cancelled' | 'superseded'
  stage: JobStage
  fix_rounds: number
  gate: Record<string, unknown> | null
  error: string | null     // why a failed job stopped
  cancel_requested: boolean
  new_documents: boolean
  supersedes: string | null   // the id of the job this one replaced
  created_at: string       // ISO-8601, UTC
  updated_at: string
}

// One line of the job's activity feed. seq counts from 1 within the job.
export interface JobEvent {
  job_id: string
  seq: number
  at: string
  stage: string
  message: string
}

export interface SnapshotLine {
  code: string
  name: string
  type: string
  debit: string    // exact decimal strings, as the API keeps money
  credit: string
}

// One version of the job's trial balance, with the workbook saved beside it. Version 1 is after posting, 2 after
// the quality gate.
export interface Snapshot {
  id: string
  job_id: string
  client_id: number
  version: number
  stage: 'after_posting' | 'after_gate'
  is_balanced: boolean
  lines: SnapshotLine[]
  totals: Record<'debits' | 'credits' | 'income' | 'expenses' | 'net_profit' | 'assets' | 'liabilities', string>
  problems: string[]       // what stopped the trial balance being built, when it couldn't be
  excel_blob_id: string
  created_at: string
}

// What the agent couldn't settle, and the assumption it made instead.
export interface JobException {
  id: string
  client_id: number
  job_id: string
  stage: string
  description: string
  assumption: string
  confidence: number       // 0 to 1
  row_ids: number[]
  status: 'open' | 'resolved'
  note: string             // how it was resolved
  created_at: string
  resolved_at: string | null
}

// A document asked of the client. The API never gives the upload link's token to this page.
export interface DocRequest {
  id: string
  document_type: string
  period: string
  reason: string
  status: 'pending' | 'sent' | 'receiving' | 'received' | 'replaced'
  sent_at: string | null
  received_at: string | null
}

const HEADERS = { 'X-LedgerSync': '1' }
const JSON_HEADERS = { ...HEADERS, 'Content-Type': 'application/json' }
const POLL_MS = 2_000

const jobPath = (jobId: string) => `/api/agent-jobs/${encodeURIComponent(jobId)}`
const post = (path: string) => request<AgentJob>(path, { method: 'POST', headers: HEADERS })

export function startJob(clientId: number, periodStart: string, periodEnd: string): Promise<AgentJob> {
  return request<AgentJob>(`/api/clients/${clientId}/agent-jobs`, {
    method: 'POST', headers: JSON_HEADERS, body: JSON.stringify({ period_start: periodStart, period_end: periodEnd }),
  })
}

// The client's newest job, whatever its status, or null when it has had none.
export function latestJob(clientId: number): Promise<AgentJob | null> {
  return request<AgentJob | null>(`/api/clients/${clientId}/agent-jobs/latest`, { headers: HEADERS })
}

export function getJob(jobId: string): Promise<AgentJob> {
  return request<AgentJob>(jobPath(jobId), { headers: HEADERS })
}

// The feed's lines after the one numbered `after` (0 for all), oldest first.
export function getEvents(jobId: string, after: number): Promise<JobEvent[]> {
  return request<JobEvent[]>(`${jobPath(jobId)}/events?after=${after}`, { headers: HEADERS })
}

// Asks the job to stop before its next step. It carries on being preparing until it has.
export const cancelJob = (jobId: string) => post(`${jobPath(jobId)}/cancel`)

// Runs a failed job again from the step that failed.
export const retryJob = (jobId: string) => post(`${jobPath(jobId)}/retry`)

export function getSnapshots(jobId: string): Promise<Snapshot[]> {
  return request<Snapshot[]>(`${jobPath(jobId)}/snapshots`, { headers: HEADERS })
}

export function getExceptions(jobId: string): Promise<JobException[]> {
  return request<JobException[]>(`${jobPath(jobId)}/exceptions`, { headers: HEADERS })
}

export function getRequests(jobId: string): Promise<DocRequest[]> {
  return request<DocRequest[]>(`${jobPath(jobId)}/requests`, { headers: HEADERS })
}

// Where the Excel workbook saved with one version of the trial balance is downloaded from.
export function exportUrl(jobId: string, version: number): string {
  return `${jobPath(jobId)}/snapshots/${version}/export.xlsx`
}

export const STAGES: JobStage[] = ['ingest', 'classify', 'gate', 'fix', 'requests', 'snapshot', 'done']

const STAGE_LABELS: Record<JobStage, string> = {
  ingest: 'Reading documents',
  classify: 'Coding rows',
  gate: 'Quality gate',
  fix: 'Fixing what the gate found',
  requests: 'Asking the client',
  snapshot: 'Saving the trial balance',
  done: 'Done',
}

// A stage in words. A stage this page doesn't know (a newer API's) is shown as the API names it.
export function stageLabel(stage: JobStage): string {
  return STAGE_LABELS[stage] ?? stage
}

export type StepState = 'complete' | 'working' | 'stopped' | 'waiting' | 'skipped'

// Where one step of the tracker stands for the job: complete once the job is past it, working while the job is at
// it, stopped where a job that ended early (failed, cancelled, replaced) got to, and waiting before the job
// reaches it. The fix step is skipped, not complete, once the job is past it without a fix round.
export function stepState(job: Pick<AgentJob, 'status' | 'stage' | 'fix_rounds'>, step: JobStage): StepState {
  const at = STAGES.indexOf(step)
  const now = STAGES.indexOf(job.stage)
  if (job.stage === 'done' || at < now) return step === 'fix' && job.fix_rounds === 0 ? 'skipped' : 'complete'
  if (at > now) return 'waiting'
  return job.status === 'preparing' ? 'working' : 'stopped'
}

// How long to wait before looking at the job again, or null when it has ended and there is nothing to watch for.
export function pollDelay(status: AgentJob['status']): number | null {
  return status === 'preparing' ? POLL_MS : null
}

// The newest line of the feed shown so far (0 when none): the page asks for what comes after it.
export function lastSeq(events: JobEvent[]): number {
  return events.length ? events[events.length - 1].seq : 0
}

// The feed with new lines added, each shown once: lines at or before the last shown are dropped.
export function mergeEvents(shown: JobEvent[], fresh: JobEvent[]): JobEvent[] {
  const after = lastSeq(shown)
  const added = fresh.filter(e => e.seq > after)
  return added.length ? [...shown, ...added] : shown
}

// One look at a running job: the job as it is now, then the feed lines after `after`. The job is read first because
// the agent writes a job's last lines before it marks the job ended: a job seen ended has all its lines in the feed.
export async function checkJob(jobId: string, after: number): Promise<{ job: AgentJob; events: JobEvent[] }> {
  const job = await getJob(jobId)
  return { job, events: await getEvents(jobId, after) }
}

const REQUEST_STATUS: Record<DocRequest['status'], string> = {
  pending: 'Waiting to be emailed',
  sent: 'Emailed, waiting for the client',
  receiving: 'Received',     // the client's file has been sent and is being read
  received: 'Received',
  replaced: 'Replaced by a newer request.',
}

// Where a request has got to, in words.
export function requestStatusText(status: DocRequest['status']): string {
  return REQUEST_STATUS[status] ?? status
}

// Client for the LedgerSync API. Paths are relative so every request goes through the
// Next.js rewrite in next.config.ts: no CORS and no hardcoded host.

export interface Issue {
  code: string
  message: string
  severity: 'info' | 'warning' | 'error'
}

export interface Transaction {
  date: string | null
  description: string
  direction: 'in' | 'out'
  gross: string
  vat: string | null            // VAT shown on the document; null = not shown (the API then books none)
  vat_treatment: string | null  // rate a person picked, to estimate VAT not shown; null = none picked
  vat_posted: string | null     // set by the API: the VAT the ledger books
  net: string | null            // set by the API: gross minus vat_posted
  account_code: string
  account_name?: string | null
  contra_account_code: string | null
  currency: string
  source: string
  method: string
  evidence: string | null
  issues: Issue[]
}

export interface AnalyzeResult {
  transactions: Transaction[]
  warnings: string[]
  model: string | null
}

export interface TrialBalanceLine {
  code: string
  name: string
  type: string
  debit: string
  credit: string
}

export interface TrialBalanceResult {
  lines: TrialBalanceLine[]
  total_debits: string
  total_credits: string
  is_balanced: boolean
}

export interface Health {
  model: string
  ai_reachable: boolean
  model_available: boolean
  ai_error: string | null
  max_upload_mb: number
  max_parallel_jobs?: number    // files the API reads at once; missing from older APIs
}

interface Job {
  job_id: string
  status: 'queued' | 'running' | 'succeeded' | 'failed' | 'cancelled'
  progress: string
  result: AnalyzeResult | null
  error: { code: string; message: string } | null
}

export class ApiError extends Error {
  code: string

  constructor(message: string, code: string) {
    super(message)
    this.name = 'ApiError'
    this.code = code
  }
}

const POLL_INTERVAL_MS = 1_000
const MAX_POLL_FAILURES = 10 // ~10 s: long enough for the API to restart
const API_DOWN = 'The LedgerSync API is not running or failed. Check that `python server.py` is running and see its log.'

const sleep = (ms: number) => new Promise(resolve => setTimeout(resolve, ms))

async function request<T>(path: string, init: RequestInit = {}, timeoutMs = 30_000): Promise<T> {
  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(), timeoutMs)
  const res = await fetch(path, { ...init, signal: controller.signal })
    .catch(() => {
      throw controller.signal.aborted
        ? new ApiError('The LedgerSync API did not respond in time.', 'timeout')
        : new ApiError('Cannot reach the LedgerSync API. Is `python server.py` running?', 'network')
    })
    .finally(() => clearTimeout(timer))
  if (!res.ok) throw await errorFrom(res)
  return (await res.json()) as T
}

async function errorFrom(res: Response): Promise<ApiError> {
  let detail: unknown
  try {
    detail = (await res.json()).detail
  } catch {
    // Not JSON: the Next proxy answers a plain "Internal Server Error" when the API is down.
    if (res.status >= 500) return new ApiError(API_DOWN, 'network')
    detail = undefined
  }
  const code = `http_${res.status}`
  if (typeof detail === 'string') return new ApiError(detail, code)
  if (Array.isArray(detail)) {
    return new ApiError(detail.map(d => (d as { msg?: string }).msg ?? String(d)).join('; '), code)
  }
  if (detail && typeof detail === 'object' && 'message' in detail) {
    const d = detail as { message: string; code?: string }
    return new ApiError(d.message, d.code ?? code)
  }
  return new ApiError(`The LedgerSync API returned HTTP ${res.status}.`, code)
}

export function getHealth(): Promise<Health> {
  return request<Health>('/api/health', {}, 10_000)
}

// Starts an analysis job and polls it until it finishes, reporting progress along the way.
// isCancelled is checked between polls; cancelling also tells the server to stop the job.
export async function analyze(
  formData: FormData,
  onProgress: (progress: string) => void,
  isCancelled: () => boolean,
): Promise<AnalyzeResult> {
  const { job_id } = await request<{ job_id: string }>('/api/analyze', { method: 'POST', body: formData }, 120_000)
  let failures = 0
  for (;;) {
    if (isCancelled()) {
      await request(`/api/jobs/${job_id}`, { method: 'DELETE' }).catch(() => undefined)
      throw new ApiError('Analysis cancelled.', 'cancelled')
    }
    let job: Job
    try {
      job = await request<Job>(`/api/jobs/${job_id}`)
      failures = 0
    } catch (e) {
      // The API may be restarting: keep polling briefly. Once it is back, an unknown
      // job answers 404 job_not_found with "please run it again".
      if (e instanceof ApiError && e.code === 'network' && ++failures < MAX_POLL_FAILURES) {
        onProgress('Waiting for the LedgerSync API…')
        await sleep(POLL_INTERVAL_MS)
        continue
      }
      throw e
    }
    if (job.status === 'succeeded' && job.result) return job.result
    if (job.status === 'failed') throw new ApiError(job.error?.message ?? 'Analysis failed.', job.error?.code ?? 'failed')
    if (job.status === 'cancelled') throw new ApiError('Analysis cancelled.', 'cancelled')
    onProgress(job.progress)
    await sleep(POLL_INTERVAL_MS)
  }
}

export function validateTransactions(transactions: Transaction[]): Promise<{ transactions: Transaction[] }> {
  return request<{ transactions: Transaction[] }>('/api/transactions/validate', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ transactions }),
  })
}

export function trialBalance(transactions: Transaction[]): Promise<TrialBalanceResult> {
  return request<TrialBalanceResult>('/api/trial-balance', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ transactions }),
  })
}

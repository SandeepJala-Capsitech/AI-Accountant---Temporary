// Run with: npm test. A stubbed fetch stands in for the API.
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { ApiError } from './api.ts'
import {
  cancelJob, checkJob, exportUrl, getEvents, getExceptions, getJob, getRequests, getSnapshots, latestJob, lastSeq,
  mergeEvents, pollDelay, requestStatusText, retryJob, stageLabel, startJob, stepState, STAGES, type AgentJob,
  type DocRequest, type JobEvent,
} from './agentJobs.ts'

interface Call { url: string; init: { method?: string; headers: Record<string, string>; body?: string } }

const reply = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })

// Every call goes to fetch, which answers with `body` (or, given a function, with what it returns for the call).
function mockFetch(body: unknown, status = 200): Call[] {
  const calls: Call[] = []
  globalThis.fetch = (async (url: string, init: Call['init']) => {
    calls.push({ url, init })
    return reply(typeof body === 'function' ? body(url) : body, status)
  }) as unknown as typeof fetch
  return calls
}

const event = (seq: number, message = `Line ${seq}`): JobEvent =>
  ({ job_id: 'j1', seq, at: '2026-10-21T10:00:00.000000+00:00', stage: 'classify', message })

test('startJob posts the period with the X-LedgerSync header', async () => {
  const calls = mockFetch({ id: 'j1', status: 'preparing', stage: 'ingest' })
  await startJob(3, '2025-04-01', '2026-03-31')
  assert.equal(calls[0].url, '/api/clients/3/agent-jobs')
  assert.equal(calls[0].init.headers['X-LedgerSync'], '1')
  assert.deepEqual(JSON.parse(calls[0].init.body!), { period_start: '2025-04-01', period_end: '2026-03-31' })
})

test('stageLabel names every stage', () => {
  assert.equal(stageLabel('classify'), 'Coding rows')
  assert.equal(stageLabel('done'), 'Done')
  assert.deepEqual((['ingest', 'classify', 'gate', 'fix', 'requests', 'snapshot', 'done'] as const).map(stageLabel), [
    'Reading documents', 'Coding rows', 'Quality gate', 'Fixing what the gate found', 'Asking the client',
    'Saving the trial balance', 'Done'])
})

test('the panel polls every 2 seconds only while a job is preparing', () => {
  assert.equal(pollDelay('preparing'), 2000)
  for (const s of ['awaiting_review', 'failed', 'cancelled', 'superseded'] as const) assert.equal(pollDelay(s), null)
})

test('latestJob gives null for a client with no job', async () => {
  const calls = mockFetch(null)
  assert.equal(await latestJob(3), null)
  assert.equal(calls[0].url, '/api/clients/3/agent-jobs/latest')
  assert.equal(calls[0].init.headers['X-LedgerSync'], '1')
})

test('each read names its route and sends the X-LedgerSync header', async () => {
  const calls = mockFetch([])
  await getJob('j1')
  await getEvents('j1', 12)
  await getSnapshots('j1')
  await getExceptions('j1')
  await getRequests('j1')
  assert.deepEqual(calls.map(c => c.url), [
    '/api/agent-jobs/j1', '/api/agent-jobs/j1/events?after=12', '/api/agent-jobs/j1/snapshots',
    '/api/agent-jobs/j1/exceptions', '/api/agent-jobs/j1/requests'])
  assert.deepEqual(calls.map(c => c.init.headers['X-LedgerSync']), ['1', '1', '1', '1', '1'])
})

test('cancel and retry post to the job with the X-LedgerSync header', async () => {
  const calls = mockFetch({ id: 'j1', status: 'preparing' })
  await cancelJob('j1')
  await retryJob('j1')
  assert.deepEqual(calls.map(c => [c.init.method, c.url]), [
    ['POST', '/api/agent-jobs/j1/cancel'], ['POST', '/api/agent-jobs/j1/retry']])
  assert.deepEqual(calls.map(c => c.init.headers['X-LedgerSync']), ['1', '1'])
})

test('exportUrl is the workbook of one version of the trial balance', () => {
  assert.equal(exportUrl('j1', 2), '/api/agent-jobs/j1/snapshots/2/export.xlsx')
})

test("a job that can't start says why, in the API's words", async () => {
  mockFetch({ detail: { code: 'agent_not_configured', message: 'Agent jobs need MongoDB and LEDGERSYNC_AGENT_MODEL. Set both in .env.' } }, 503)
  await assert.rejects(startJob(3, '2025-04-01', '2026-03-31'), (e: unknown) =>
    e instanceof ApiError && e.message === 'Agent jobs need MongoDB and LEDGERSYNC_AGENT_MODEL. Set both in .env.')
})

test('checkJob reads the job, then only the feed lines after the last one shown', async () => {
  const calls = mockFetch((url: string) => (url.includes('/events') ? [event(5), event(6)] : { id: 'j1', status: 'preparing' }))
  const seen = await checkJob('j1', 4)
  assert.deepEqual(calls.map(c => c.url), ['/api/agent-jobs/j1', '/api/agent-jobs/j1/events?after=4'])   // the job first: its last lines come before its end
  assert.equal(seen.job.status, 'preparing')
  assert.deepEqual(seen.events.map(e => e.seq), [5, 6])
})

test('lines already shown are not shown twice', () => {
  assert.equal(lastSeq([]), 0)
  assert.equal(lastSeq([event(1), event(2)]), 2)
  const shown = [event(1), event(2)]
  assert.deepEqual(mergeEvents(shown, [event(2), event(3)]).map(e => e.seq), [1, 2, 3])
  assert.deepEqual(mergeEvents(shown, []), shown)
  assert.deepEqual(mergeEvents([], [event(3), event(4)]).map(e => e.seq), [3, 4])
})

test('a request says in words where it has got to', () => {
  const words = (status: DocRequest['status']) => requestStatusText(status)
  assert.equal(words('pending'), 'Waiting to be emailed')
  assert.equal(words('sent'), 'Emailed, waiting for the client')
  assert.equal(words('receiving'), 'Received')      // the client's file is being read
  assert.equal(words('received'), 'Received')
  assert.equal(words('replaced'), 'Replaced by a newer request.')
})

const at = (status: AgentJob['status'], stage: AgentJob['stage'], fix_rounds = 0) => ({ status, stage, fix_rounds })
const tracker = (job: ReturnType<typeof at>) => STAGES.map(step => stepState(job, step))

test('the tracker marks the stage at work and what is before and after it', () => {
  assert.deepEqual(tracker(at('preparing', 'classify')),
    ['complete', 'working', 'waiting', 'waiting', 'waiting', 'waiting', 'waiting'])
  assert.deepEqual(tracker(at('preparing', 'ingest')),
    ['working', 'waiting', 'waiting', 'waiting', 'waiting', 'waiting', 'waiting'])
})

test('the tracker says where a job that ended without finishing stopped', () => {
  for (const status of ['failed', 'cancelled', 'superseded'] as const) {
    assert.deepEqual(tracker(at(status, 'gate')),
      ['complete', 'complete', 'stopped', 'waiting', 'waiting', 'waiting', 'waiting'])
  }
})

test('a finished job has every stage complete, and the fix stage not needed when the gate found nothing', () => {
  assert.deepEqual(tracker(at('awaiting_review', 'done')),
    ['complete', 'complete', 'complete', 'skipped', 'complete', 'complete', 'complete'])
  assert.deepEqual(tracker(at('awaiting_review', 'done', 2)),
    ['complete', 'complete', 'complete', 'complete', 'complete', 'complete', 'complete'])
  assert.equal(stepState(at('preparing', 'requests'), 'fix'), 'skipped')     // passed without a fix round
  assert.equal(stepState(at('preparing', 'requests', 1), 'fix'), 'complete')
  assert.equal(stepState(at('preparing', 'gate'), 'fix'), 'waiting')          // not reached (yet)
})

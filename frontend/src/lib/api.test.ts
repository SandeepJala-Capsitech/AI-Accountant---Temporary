// Run with: npm test. A stubbed fetch stands in for the API.
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { analyze } from './api.ts'

const reply = (body: unknown) =>
  new Response(JSON.stringify(body), { status: 200, headers: { 'Content-Type': 'application/json' } })

test('a cancel that arrives after the job saved its rows still returns them', async () => {
  // Final review #2: the page said "cancelled" for a job that had already saved, and Retry booked it twice.
  const calls: string[] = []
  let cancelled = false
  const result = { transactions: [], warnings: [], model: 'fake-model', upload_id: 7 }
  const job = (status: string) => ({ job_id: 'j1', status, progress: 'Saving', result: status === 'succeeded' ? result : null, error: null })
  globalThis.fetch = (async (url: string, init?: RequestInit) => {
    calls.push(`${init?.method ?? 'GET'} ${url}`)
    if (init?.method === 'POST') return reply({ job_id: 'j1', status: 'queued' })
    if (init?.method === 'DELETE') return reply(job('running'))
    if (calls.filter(c => c === 'GET /api/jobs/j1').length === 1) {
      cancelled = true            // the person clicks Cancel while the job is saving
      return reply(job('running'))
    }
    return reply(job('succeeded'))
  }) as typeof fetch
  assert.deepEqual(await analyze(new FormData(), () => {}, () => cancelled), result)
  assert.equal(calls.filter(c => c.startsWith('DELETE')).length, 1)
})

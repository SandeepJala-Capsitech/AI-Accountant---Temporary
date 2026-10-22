// Run with: npm test. A stubbed fetch stands in for the API.
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { ApiError } from './api.ts'
import { allReceived, getRequests, uploadRequested, type RequestedDocument } from './requests.ts'

const reply = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })

const job = (status: string, error: { code: string; message: string } | null = null) =>
  ({ job_id: 'j1', status, progress: '', result: null, error })

test('uploadRequested sends the file with the X-LedgerSync header', async () => {
  const calls: { url: string; init?: RequestInit }[] = []
  globalThis.fetch = (async (url: string, init?: RequestInit) => {
    calls.push({ url, init })
    return init?.method === 'POST' ? reply({ job_id: 'j1', status: 'queued' }, 202) : reply(job('succeeded'))
  }) as typeof fetch
  await uploadRequested('tok-1', 'r1', new File(['%PDF-1.7'], 'acme.pdf', { type: 'application/pdf' }))
  assert.equal(calls[0].url, '/api/requests/tok-1/r1/file')
  assert.equal(new Headers(calls[0].init?.headers).get('X-LedgerSync'), '1')
  assert.equal(((calls[0].init?.body as FormData).get('file') as File).name, 'acme.pdf')
  assert.deepEqual(calls.slice(1).map(c => c.url), ['/api/jobs/j1'])   // waits for the file to be read
})

test('uploadRequested says why a file could not be read', async () => {
  globalThis.fetch = (async (_url: string, init?: RequestInit) => init?.method === 'POST'
    ? reply({ job_id: 'j1', status: 'queued' }, 202)
    : reply(job('failed', { code: 'unreadable_file', message: "No transactions were found in 'acme.pdf'." }))) as typeof fetch
  await assert.rejects(uploadRequested('tok-1', 'r1', new File(['x'], 'acme.pdf')),
                       (e: unknown) => e instanceof ApiError && e.message === "No transactions were found in 'acme.pdf'.")
})

test('getRequests passes on the unknown-link message', async () => {
  let sent: Headers | undefined
  globalThis.fetch = (async (_url: string, init?: RequestInit) => {
    sent = new Headers(init?.headers)
    return reply({ detail: { code: 'not_found', message: "This link isn't valid. Ask your accountant for a new one." } }, 404)
  }) as typeof fetch
  await assert.rejects(getRequests('nope'), (e: unknown) =>
    e instanceof ApiError && e.code === 'not_found' && e.message === "This link isn't valid. Ask your accountant for a new one.")
  assert.equal(sent?.get('X-LedgerSync'), '1')
})

test('everything has arrived only once every document asked for has', () => {
  const doc = (status: RequestedDocument['status']): RequestedDocument =>
    ({ id: 'r', document_type: 'Invoice', period: 'March 2026', reason: 'Why', status })
  assert.equal(allReceived([doc('received'), doc('received')]), true)
  assert.equal(allReceived([doc('received'), doc('pending')]), false)
  assert.equal(allReceived([]), false)
})

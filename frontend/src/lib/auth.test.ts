// Run with: npm test. A stubbed fetch stands in for the API.
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { request } from './api.ts'
import { loginPath, safeNext, signIn } from './auth.ts'

const answer = (status: number, body: unknown) =>
  new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })

test('safeNext keeps same-site paths only', () => {
  assert.equal(safeNext('/clients/3?x=1'), '/clients/3?x=1')
  for (const bad of [null, '', 'https://evil.example', '//evil.example', '/\\evil.example']) {
    assert.equal(safeNext(bad), '/')
  }
})

test('loginPath carries the page to come back to', () => {
  assert.equal(loginPath('/clients/3?y=2'), '/login?next=%2Fclients%2F3%3Fy%3D2')
})

test('a wrong password is a wrong_credentials error, not a sign-out', async () => {
  globalThis.fetch = (async () => answer(401, {
    detail: { code: 'wrong_credentials', message: "That username and password don't match." },
  })) as typeof fetch
  await assert.rejects(signIn('admin', 'nope'), (e: { code: string; message: string }) => {
    assert.equal(e.code, 'wrong_credentials')
    assert.equal(e.message, "That username and password don't match.")
    return true
  })
})

test('signIn sends the pages header and the pair as JSON', async () => {
  let sent: RequestInit | undefined
  globalThis.fetch = (async (_url: string, init?: RequestInit) => {
    sent = init
    return new Response(null, { status: 204 })
  }) as typeof fetch
  await signIn('admin', 's3cret')
  assert.equal(sent?.method, 'POST')
  assert.equal((sent?.headers as Record<string, string>)['X-LedgerSync'], '1')
  assert.deepEqual(JSON.parse(String(sent?.body)), { username: 'admin', password: 's3cret' })
})

test('an API call without a session rejects as not_signed_in', async () => {
  globalThis.fetch = (async () => answer(401, {
    detail: { code: 'not_signed_in', message: 'Sign in to use Super Accountant.' },
  })) as typeof fetch
  await assert.rejects(request('/api/accounts'), (e: { code: string }) => e.code === 'not_signed_in')
})

// Run with: npm test. A stubbed fetch stands in for the API.
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { ApiError } from './api.ts'
import {
  answerQuestion, formatMoney, groupThreads, looksLikeStatement, parseEvents, questionControl, ruleBadge,
  shownAccounts, streamMessage, suspenseLabel, uploadForQuestion,
  type ChatEvent, type ChatThread, type Decision, type PostedTrialBalance, type Question,
} from './chat.ts'

const thread = (id: string, updated_at: string): ChatThread => ({
  id, client_id: 1, year_start: '2026-04-01', title: id, created_at: updated_at, updated_at,
})

const question = (kind: Question['kind']): Question => ({
  id: 'q1', client_id: 1, year_start: '2026-04-01', row_ids: [4], kind, text: 'When was this taxi?',
  why: '£18.00, no date on it and no matching bank line.', source: 'rules', status: 'open', answer: null,
  created_at: '2026-11-02T10:00:00+00:00', resolved_at: null,
})

const decision = (rule: string): Decision => ({
  id: 'd1', client_id: 1, thread_id: 't1', year_start: '2026-04-01', action: 'keep_one', rule, status: 'applied',
  row_ids: [1, 2], evidence: [], reason: '', before: {}, after: {}, actor: 'agent:m', reverses: null,
  created_at: '2026-11-02T10:00:00+00:00', prev_hash: '', hash: 'h',
})

const summaryJson = '{"sorted":[],"questions":[],"trial_balance":{"lines":[]},"post":{"batch_id":null}}'

test('an event split across two chunks arrives once, whole', () => {
  const first = parseEvents('event: step\ndata: {"id":"f1","text":"Read', '')
  assert.deepEqual(first.events, [])
  const second = parseEvents(' a.csv","state":"done"}\n\nevent: token\ndata: {"text":"Hi"}\n\n', first.carry)
  assert.deepEqual(second.events, [
    { type: 'step', data: { id: 'f1', text: 'Read a.csv', state: 'done' } },
    { type: 'token', data: { text: 'Hi' } },
  ])
  assert.equal(second.carry, '')
})

test('events separated by CRLF, with comments and several data lines, parse', () => {
  const { events, carry } = parseEvents(
    ': keep-alive\r\n\r\nevent: summary\r\ndata: ' + summaryJson + '\r\n\r\nevent: done\r\ndata: {}\r\n\r\nevent: tok', '')
  assert.deepEqual(events.map(e => e.type), ['summary', 'done'])
  assert.equal(carry, 'event: tok')
  const joined = parseEvents('event: token\ndata: {"text":\ndata: "two lines"}\n\n', '')
  assert.deepEqual(joined.events, [{ type: 'token', data: { text: 'two lines' } }])
})

test('an event the client does not know is skipped', () => {
  assert.deepEqual(parseEvents('event: ping\ndata: {}\n\n', '').events, [])
})

test('threads group by the calendar day, across midnight', () => {
  const now = new Date(2026, 10, 2, 0, 5)   // 00:05 on 2 Nov, local time
  const groups = groupThreads([
    thread('earlier', new Date(2026, 9, 31, 23, 59).toISOString()),
    thread('yesterday-late', new Date(2026, 10, 1, 23, 59).toISOString()),
    thread('today', new Date(2026, 10, 2, 0, 1).toISOString()),
    thread('yesterday-early', new Date(2026, 10, 1, 0, 0).toISOString()),
  ], now)
  assert.deepEqual(groups.map(g => [g.label, g.threads.map(t => t.id)]), [
    ['Today', ['today']],
    ['Yesterday', ['yesterday-late', 'yesterday-early']],
    ['Earlier', ['earlier']],
  ])
})

test('empty groups are left out', () => {
  const now = new Date(2026, 10, 2, 12, 0)
  assert.deepEqual(groupThreads([thread('a', new Date(2026, 10, 2, 9).toISOString())], now).map(g => g.label), ['Today'])
  assert.deepEqual(groupThreads([], now), [])
})

test('each kind of question gets its control', () => {
  assert.equal(questionControl(question('date')), 'date')
  assert.equal(questionControl(question('receipt_or_invoice')), 'upload')
  assert.equal(questionControl(question('vat_invoice')), 'upload')
  assert.equal(questionControl(question('other')), 'text')
})

test('the rule badge is the rule number', () => {
  assert.equal(ruleBadge(decision('R2 Duplicate: kept the receipt')), 'R2')
  assert.equal(ruleBadge(decision('R1 Date from the bank')), 'R1')
  assert.equal(ruleBadge(decision('Documentation required')), '')
})

test('money shows pounds with thousands and two decimals', () => {
  assert.equal(formatMoney('1412.68'), '£1,412.68')
  assert.equal(formatMoney('1234567.5'), '£1,234,567.50')
  assert.equal(formatMoney('-1412.68'), '-£1,412.68')
  assert.equal(formatMoney('0'), '£0.00')
  assert.equal(formatMoney('240.00'), '£240.00')
  assert.equal(formatMoney('-0.00'), '£0.00')
})

test('a Suspense item says what it needs', () => {
  assert.equal(suspenseLabel('receipt_or_invoice'), 'needs receipt or invoice')
  assert.equal(suspenseLabel('vat_invoice'), 'needs VAT invoice')
})

test('statements are told apart by their name', () => {
  assert.equal(looksLikeStatement('barclays-oct.csv'), true)
  assert.equal(looksLikeStatement('Statement October.pdf'), true)
  assert.equal(looksLikeStatement('export.OFX'), true)
  assert.equal(looksLikeStatement('costco-receipt.jpg'), false)
  assert.equal(looksLikeStatement('invoice-240.pdf'), false)
})

test('the posted table shows five accounts and Suspense, skipping empty ones', () => {
  const line = (code: string, debit = '10.00', credit = '0.00') => ({ code, name: code, type: 'expense', debit, credit })
  const tb = {
    lines: [line('1200', '0.00', '1412.68'), line('4000', '0.00', '0.00'), line('7400'), line('7500'), line('7600'),
            line('7700'), line('7800'), line('9998', '240.00')],
  } as unknown as PostedTrialBalance
  const { shown, more } = shownAccounts(tb)
  assert.deepEqual(shown.map(l => l.code), ['1200', '7400', '7500', '7600', '7700', '9998'])
  assert.equal(more, 1)
})

const sse = (text: string, chunks = 3) => {
  const size = Math.ceil(text.length / chunks)
  const parts = Array.from({ length: chunks }, (_, i) => text.slice(i * size, (i + 1) * size))
  return new Response(new ReadableStream({
    start(c) { for (const p of parts) c.enqueue(new TextEncoder().encode(p)); c.close() },
  }), { status: 200, headers: { 'Content-Type': 'text/event-stream' } })
}

test('a message is sent with its files and bank accounts, and its events streamed back', async () => {
  let url = ''
  let sent: RequestInit | undefined
  globalThis.fetch = (async (u: string, init?: RequestInit) => {
    url = u
    sent = init
    return sse('event: status\ndata: {"text":"Working on it…"}\n\nevent: token\ndata: {"text":"Done."}\n\n' +
               'event: done\ndata: {}\n\n', 4)
  }) as typeof fetch
  const events: ChatEvent[] = []
  const files = [new File(['a'], 'bank.csv'), new File(['b'], 'receipt.jpg')]
  await streamMessage('t1', 'Review these', files, ['1201', ''], e => events.push(e), new AbortController().signal)
  assert.equal(url, '/api/chat/threads/t1/messages')
  assert.equal(new Headers(sent?.headers).get('X-LedgerSync'), '1')
  const body = sent?.body as FormData
  assert.equal(body.get('text'), 'Review these')
  assert.deepEqual((body.getAll('files') as File[]).map(f => f.name), ['bank.csv', 'receipt.jpg'])
  assert.deepEqual(body.getAll('bank_accounts'), ['1201', ''])
  assert.deepEqual(events.map(e => e.type), ['status', 'token', 'done'])
})

test('a second message while the first runs is refused with the reason', async () => {
  globalThis.fetch = (async () => new Response(JSON.stringify({ detail: 'Still working on the last message.' }),
    { status: 409, headers: { 'Content-Type': 'application/json' } })) as typeof fetch
  await assert.rejects(streamMessage('t1', 'again', [], [], () => {}, new AbortController().signal),
    (e: unknown) => e instanceof ApiError && e.code === 'http_409' && e.message === 'Still working on the last message.')
})

test('an answer is posted as JSON with the header and returns the new summary', async () => {
  let sent: { url: string; init?: RequestInit } | undefined
  globalThis.fetch = (async (url: string, init?: RequestInit) => {
    sent = { url, init }
    return new Response(summaryJson, { status: 200, headers: { 'Content-Type': 'application/json' } })
  }) as typeof fetch
  const summary = await answerQuestion('t1', 'q1', { kind: 'date', date: '2026-10-03' })
  assert.equal(sent?.url, '/api/chat/threads/t1/questions/q1/answer')
  assert.equal(sent?.init?.method, 'POST')
  assert.equal(new Headers(sent?.init?.headers).get('X-LedgerSync'), '1')
  assert.deepEqual(JSON.parse(String(sent?.init?.body)), { kind: 'date', date: '2026-10-03' })
  assert.deepEqual(summary.sorted, [])
})

test('a document for a question is uploaded as its file', async () => {
  let sent: { url: string; init?: RequestInit } | undefined
  globalThis.fetch = (async (url: string, init?: RequestInit) => {
    sent = { url, init }
    return new Response(summaryJson, { status: 200, headers: { 'Content-Type': 'application/json' } })
  }) as typeof fetch
  await uploadForQuestion('t1', 'q2', new File(['%PDF'], 'invoice.pdf'))
  assert.equal(sent?.url, '/api/chat/threads/t1/questions/q2/document')
  assert.equal(new Headers(sent?.init?.headers).get('X-LedgerSync'), '1')
  assert.equal(((sent?.init?.body as FormData).get('file') as File).name, 'invoice.pdf')
})

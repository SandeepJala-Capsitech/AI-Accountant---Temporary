// Run with: npm test.
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { canShow, changeLines, documentOf, fileHref, isPdf, sameFixGroups } from './suggestions.ts'
import type { Decision } from './chat.ts'
import type { Upload } from './clients'

type Fields = Record<string, Record<string, unknown>>
const suggestion = (id: string, action: string, before: Fields, after: Fields): Decision => ({
  id, client_id: 1, thread_id: null, year_start: '2026-04-01', action, rule: 'R6 Account changed', status: 'suggested',
  trigger: 'background', row_ids: Object.keys(after).map(Number), evidence: [], reason: 'why', before, after, actor: 'rules',
  reverses: null, created_at: '2026-11-04T10:42:00', prev_hash: '', hash: 'h',
} as unknown as Decision)
const account = (id: string, row: number, to = '7400') =>
  suggestion(id, 'change_account', { [row]: { account_code: '7500' } }, { [row]: { account_code: to } })
const upload = (id: number, kind: string, name = 'receipts.pdf'): Upload => ({ id, name, kind, sha256: 'abc' } as unknown as Upload)

test('one changed field is one line, and nothing reads as none', () => {
  const d = suggestion('D1', 'set_date', { 4: { date: null } }, { 4: { date: '2026-09-02' } })
  assert.deepEqual(changeLines(d), { lines: ['Date: none → 2 Sep 2026'], more: 0 })
})

test('a change to two rows names each row', () => {
  const d = suggestion('D1', 'change_account', { 4: { account_code: '7500' }, 9: { account_code: '7500' } },
                       { 4: { account_code: '7400' }, 9: { account_code: '7400' } })
  assert.deepEqual(changeLines(d).lines, ['Row 4 · Account: 7500 → 7400', 'Row 9 · Account: 7500 → 7400'])
})

test('more than four lines are cut, and say how many more', () => {
  const fields = { a: 1, b: 2, c: 3, d: 4, e: 5, f: 6 }
  const found = changeLines(suggestion('D1', 'edit', { 4: {} }, { 4: fields }))
  assert.equal(found.lines.length, 4)
  assert.equal(found.more, 2)
  assert.equal(changeLines(suggestion('D1', 'edit', { 4: {} }, { 4: fields }), 6).more, 0)
})

test('a suggestion that changes nothing has no lines', () => {
  assert.deepEqual(changeLines(suggestion('D1', 'keep_one', {}, {})), { lines: [], more: 0 })
})

test('two suggestions that move rows to the same account are one group', () => {
  const [d1, d2] = [account('D1', 4), account('D2', 9)]
  assert.deepEqual(sameFixGroups([d1, d2]), [
    { key: 'change_account|Account=7400', label: 'Account → 7400 on 2 rows', rows: 2, suggestions: [d1, d2] },
  ])
})

test('one suggestion of a kind makes no group', () => {
  assert.deepEqual(sameFixGroups([account('D1', 4), suggestion('D2', 'set_date', { 5: { date: null } }, { 5: { date: '2026-09-02' } })]), [])
})

test('the same kind with different new values is two groups, each of two, in the order they first appear', () => {
  const found = sameFixGroups([account('D1', 4, '7600'), account('D2', 5), account('D3', 6, '7600'), account('D4', 7), account('D5', 8, '7700')])
  assert.deepEqual(found.map(g => [g.key, g.suggestions.map(s => s.id)]),
                   [['change_account|Account=7600', ['D1', 'D3']], ['change_account|Account=7400', ['D2', 'D4']]])
})

test('the same new value from a different kind of change is not the same fix', () => {
  const other = suggestion('D2', 'use_primary', { 9: { account_code: '7500' } }, { 9: { account_code: '7400' } })
  assert.deepEqual(sameFixGroups([account('D1', 4), other]), [])
})

test('a suggestion that changes nothing joins no group', () => {
  assert.deepEqual(sameFixGroups([suggestion('D1', 'keep_one', {}, {}), suggestion('D2', 'keep_one', {}, {})]), [])
})

test('canShow goes by the name, whatever its case', () => {
  for (const name of ['SCAN.PDF', 'photo.JPG', 'a.jpeg', 'a.webp', 'a.gif', 'a.png']) assert.equal(canShow(name), true, name)
  for (const name of ['claim.xlsx', 'x.svg', 'page.html', 'noext', 'pdf']) assert.equal(canShow(name), false, name)
  assert.deepEqual([isPdf('SCAN.PDF'), isPdf('photo.jpg')], [true, false])
})

test('a row typed in by hand has no document; a row from a file has its upload', () => {
  const uploads = [upload(1, 'manual', 'Manual entry'), upload(2, 'pdf')]
  assert.equal(documentOf({ upload_id: 1 }, uploads), null)
  assert.equal(documentOf({ upload_id: 2 }, uploads)?.id, 2)
  assert.equal(documentOf({ upload_id: 3 }, uploads), null)
})

test('fileHref asks for showing only when told', () => {
  assert.equal(fileHref(1, 3), '/api/clients/1/uploads/3/file')
  assert.equal(fileHref(1, 3, true), '/api/clients/1/uploads/3/file?show=1')
})

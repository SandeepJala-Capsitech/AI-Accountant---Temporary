// Run with: npm test.
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { groupProblems, problemCount } from './problems.ts'
import type { Decision } from './chat.ts'
import type { SavedRow } from './clients'

const issue = (code: string, severity: 'info' | 'warning' | 'error' = 'warning', related: number[] = []) =>
  ({ code, message: `${code} said`, severity, related })
const row = (id: number, issues: ReturnType<typeof issue>[], more: Partial<SavedRow> = {}): SavedRow => ({
  id, upload_id: 1, edited: false, original: null, date: '2026-10-12', description: 'Vodafone bill', direction: 'out',
  gross: '86.40', issues, ...more,
} as unknown as SavedRow)
const suggestion = (id: string, action: string, row_ids: number[]): Decision => ({
  id, client_id: 1, thread_id: null, year_start: '2026-04-01', action, rule: 'R6 Account changed', status: 'suggested',
  trigger: 'chat', row_ids, evidence: [], reason: 'why', before: {}, after: {}, actor: 'rules', reverses: null,
  created_at: '2026-11-04T10:42:00', prev_hash: '', hash: 'h',
} as unknown as Decision)

test('a warning becomes a problem in its family', () => {
  const [group] = groupProblems([row(7, [issue('unusual_account', 'warning', [1, 2])])], [])
  assert.equal(group.family, 'coding')
  assert.equal(group.title, 'Account')
  assert.deepEqual(group.problems, [{ rowId: 7, what: 'Vodafone bill, 12 Oct, £86.40', code: 'unusual_account',
                                      message: 'unusual_account said', related: [1, 2], suggestion: null }])
})

test('info notes are not problems', () => {
  assert.deepEqual(groupProblems([row(7, [issue('asset_check', 'info')])], []), [])
})

test('errors are problems, and an unknown code is Other', () => {
  const [group] = groupProblems([row(7, [issue('vat_arithmetic', 'error')])], [])
  assert.equal(group.title, 'Other')
  assert.equal(group.problems[0].code, 'vat_arithmetic')
})

test('rows out of the books are left out', () => {
  const rows = [row(1, [issue('unusual_account')], { excluded: true }),
                row(2, [issue('unusual_account')], { superseded_by: 'd1' })]
  assert.deepEqual(groupProblems(rows, []), [])
})

test('a row with two warnings is listed once in each family', () => {
  const groups = groupProblems([row(7, [issue('possible_duplicate'), issue('unusual_account')])], [])
  assert.deepEqual(groups.map(g => [g.family, g.problems.map(p => p.rowId)]), [['coding', [7]], ['duplicate', [7]]])
})

test('a suggestion is paired with its row and family', () => {
  const rows = [row(7, [issue('possible_duplicate'), issue('unusual_account')]), row(8, [issue('unusual_account')])]
  const change = suggestion('s1', 'change_account', [7])
  const keep = suggestion('s2', 'keep_one', [3, 7])
  const [coding, duplicate] = groupProblems(rows, [change, keep])
  assert.deepEqual(coding.problems.map(p => p.suggestion?.id ?? null), ['s1', null])
  assert.deepEqual(duplicate.problems.map(p => p.suggestion?.id ?? null), ['s2'])
})

test('a suggestion with no matching problem is dropped', () => {
  const [coding] = groupProblems([row(7, [issue('unusual_account')])], [suggestion('s1', 'keep_one', [7]),
                                                                        suggestion('s2', 'change_account', [99])])
  assert.equal(coding.problems[0].suggestion, null)
})

test('groups come in the fixed order and empty ones are left out', () => {
  const rows = [row(1, [issue('needs_document')]), row(2, [issue('date_missing')]), row(3, [issue('small_asset')])]
  assert.deepEqual(groupProblems(rows, []).map(g => g.title), ['Account', 'Date', 'Documents'])
})

test('what names the row without its id, and says when it has no date', () => {
  const [group] = groupProblems([row(7, [issue('date_missing')], { date: null, gross: '1800.00', description: 'Dell laptop' })], [])
  assert.equal(group.problems[0].what, 'Dell laptop, no date, £1,800.00')
})

test('problemCount adds the groups', () => {
  const rows = [row(1, [issue('needs_document')]), row(2, [issue('date_missing'), issue('small_asset')])]
  assert.equal(problemCount(groupProblems(rows, [])), 3)
  assert.equal(problemCount([]), 0)
})

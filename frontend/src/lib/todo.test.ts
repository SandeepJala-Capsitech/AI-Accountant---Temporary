// Run with: npm test.
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { needCount, openQuestions, tagOf, todoCards, waitingSuggestions } from './todo.ts'
import type { Decision, Question } from './chat.ts'
import type { SavedRow } from './clients'
import type { ReviewDetail } from './reviews.ts'

type Severity = 'info' | 'warning' | 'error'
const issue = (code: string, severity: Severity = 'warning', more: object = {}) =>
  ({ code, message: `${code} said`, severity, related: [], ...more })
const row = (id: number, issues: ReturnType<typeof issue>[] = [], more: Partial<SavedRow> = {}): SavedRow => ({
  id, upload_id: 1, edited: false, original: null, date: '2026-10-12', description: 'Vodafone bill', direction: 'out',
  gross: '86.40', issues, ...more,
} as unknown as SavedRow)
const suggestion = (id: string, row_ids: number[], rule = 'R6 Account changed'): Decision => ({
  id, client_id: 1, thread_id: null, year_start: '2026-04-01', action: 'change_account', rule, status: 'suggested',
  trigger: 'background', row_ids, evidence: [], reason: 'why', before: {}, after: {}, actor: 'rules', reverses: null,
  created_at: '2026-11-04T10:42:00', prev_hash: '', hash: 'h',
} as unknown as Decision)
const question = (id: string, row_ids: number[], status: Question['status'] = 'open', text = 'When was this taxi?'): Question => ({
  id, client_id: 1, year_start: '2026-04-01', row_ids, kind: 'other', text, why: 'why', source: 'agent', status,
  answer: status === 'waiting' ? "I don't have it" : null, created_at: '2026-11-04T10:42:00', resolved_at: null,
})
const file = (r: SavedRow) => `file${r.upload_id}.xlsx, expense claim`
const keys = (cards: { key: string }[]) => cards.map(c => c.key)

test('a row with two warnings is one card, tagged by the first', () => {
  const cards = todoCards([row(1, [issue('unusual_account'), issue('date_missing')])], [], [], file)
  assert.equal(cards.length, 1)
  assert.deepEqual([cards[0].key, cards[0].kind, cards[0].tag, cards[0].issues.length], ['row:1', 'row', 'Account', 2])
  assert.equal(cards[0].title, 'Vodafone bill, 12 Oct, £86.40')
  assert.equal(cards[0].row?.id, 1)
})

test('an error names the card before a warning does, and an unknown code is Other', () => {
  const [card] = todoCards([row(1, [issue('unusual_account'), issue('vat_arithmetic', 'error')])], [], [], file)
  assert.equal(card.tag, 'Other')
  assert.deepEqual(card.issues.map(i => i.code), ['vat_arithmetic', 'unusual_account'])
})

test('a note alone, or a row left out, makes no card', () => {
  const rows = [row(1, [issue('asset_check', 'info')]), row(2, [issue('unusual_account')], { excluded: true }),
                row(3, [issue('unusual_account')], { superseded_by: 'D1' })]
  assert.deepEqual(todoCards(rows, [], [], file), [])
})

test('the sundry, refreshments and meal questions are Account cards', () => {
  for (const code of ['sundry_check', 'claim_refreshments', 'meal_check']) {
    const [card] = todoCards([row(1, [issue(code)])], [], [], file)
    assert.equal(card.tag, 'Account')
  }
})

test('a note is left off a card that has a warning', () => {
  const [card] = todoCards([row(1, [issue('asset_check', 'info'), issue('unusual_account')])], [], [], file)
  assert.deepEqual(card.issues.map(i => i.code), ['unusual_account'])
})

test('a document problem is a card named by its file', () => {
  const rows = [row(1, [issue('total_mismatch', 'warning', { about: 'document', difference: '457.20' })], { document_ref: 'claim' }),
                row(2, [], { document_ref: 'claim' })]
  const cards = todoCards(rows, [], [], file)
  assert.equal(cards.length, 1)
  assert.deepEqual([cards[0].key, cards[0].kind, cards[0].tag, cards[0].title], ['doc:1', 'document', 'Document total', 'file1.xlsx, expense claim'])
  assert.equal(cards[0].issues.length, 1)
})

test('a row with its own warning and its document’s problem is two cards', () => {
  const rows = [row(1, [issue('total_mismatch', 'warning', { about: 'document' }), issue('unusual_account')], { document_ref: 'claim' })]
  assert.deepEqual(keys(todoCards(rows, [], [], file)).sort(), ['doc:1', 'row:1'])
})

test('a suggestion and a question land on their row’s card', () => {
  const cards = todoCards([row(1, [issue('unusual_account')])], [suggestion('D1', [1])], [question('Q1', [9, 1])], file)
  assert.equal(cards.length, 1)
  assert.deepEqual([cards[0].suggestions.map(s => s.id), cards[0].questions.map(q => q.id)], [['D1'], ['Q1']])
})

test('a suggestion about the row that carries a document’s problem lands on the document’s card', () => {
  const rows = [row(1, [issue('total_mismatch', 'warning', { about: 'document' })], { document_ref: 'claim' }),
                row(2, [], { document_ref: 'claim' })]
  const cards = todoCards(rows, [suggestion('D1', [1])], [], file)
  assert.deepEqual(keys(cards), ['doc:1'])
  assert.deepEqual(cards[0].suggestions.map(s => s.id), ['D1'])
})

test('a question about another line of that document gets its own card, named by the line', () => {
  const rows = [row(1, [issue('total_mismatch', 'warning', { about: 'document' })], { document_ref: 'claim' }),
                row(2, [], { document_ref: 'claim', description: 'Uber' })]
  const cards = todoCards(rows, [], [question('Q1', [2])], file)
  assert.deepEqual(keys(cards), ['q:Q1', 'doc:1'])
  assert.equal(cards[0].title, 'Uber, 12 Oct, £86.40')
})

test('a question about a row with no problem gets its own card, named by the row', () => {
  const [card] = todoCards([row(5)], [], [question('Q1', [5])], file)
  assert.deepEqual([card.key, card.kind, card.tag, card.title, card.row?.id], ['q:Q1', 'other', 'Question', 'Vodafone bill, 12 Oct, £86.40', 5])
})

test('a question about a row left out of the books is still listed, named by the row', () => {
  const [card] = todoCards([row(5, [], { excluded: true })], [], [question('Q1', [5])], file)
  assert.deepEqual([card.key, card.row?.id], ['q:Q1', 5])
})

test('a question and a suggestion about the same row with no problem share a card', () => {
  const cards = todoCards([row(5)], [suggestion('D1', [5])], [question('Q1', [5])], file)
  assert.equal(cards.length, 1)
  assert.deepEqual([cards[0].tag, cards[0].suggestions.length, cards[0].questions.length], ['Question', 1, 1])
})

test('a question that names no row is titled by its text', () => {
  const [card] = todoCards([], [], [question('Q1', [], 'open', 'Is there a payroll report?')], file)
  assert.deepEqual([card.key, card.row, card.title, card.tag], ['q:Q1', null, 'Is there a payroll report?', 'Question'])
})

test('a suggestion whose row is gone is titled by its rule', () => {
  const [card] = todoCards([], [suggestion('D1', [99], 'R2 Duplicate: kept the receipt')], [], file)
  assert.deepEqual([card.key, card.row, card.title, card.tag], ['s:D1', null, 'Duplicate: kept the receipt', 'Suggestion'])
})

test('errors come first, then questions, then suggestions, then the rest by date, waiting last', () => {
  const rows = [
    row(1, [issue('unusual_account')], { date: '2026-10-20' }),
    row(2, [issue('unusual_account')], { date: '2026-10-05' }),
    row(3, [issue('unusual_account')], { date: '2026-10-25' }),
    row(4, [issue('unusual_account')], { date: '2026-10-28' }),
    row(5, [issue('vat_arithmetic', 'error')], { date: '2026-10-30' }),
    row(6, [issue('date_missing')], { date: null }),
    row(7),
  ]
  const cards = todoCards(rows, [suggestion('D1', [3])], [question('Q1', [4]), question('Q2', [7], 'waiting')], file)
  assert.deepEqual(keys(cards), ['row:5', 'row:4', 'row:3', 'row:2', 'row:1', 'row:6', 'q:Q2'])
})

test('a card with only a waiting question is waiting and not counted', () => {
  const cards = todoCards([row(1, [issue('unusual_account')]), row(7)], [], [question('Q2', [7], 'waiting')], file)
  assert.deepEqual(cards.map(c => c.waiting), [false, true])
  assert.equal(needCount(cards), 1)
})

test('a waiting question on a row with a problem leaves the card needing the person', () => {
  const [card] = todoCards([row(1, [issue('unusual_account')])], [], [question('Q2', [1], 'waiting')], file)
  assert.equal(card.waiting, false)
})

test('the same question in two runs is listed once; a resolved one is not', () => {
  const run = (questions: Question[]) => ({ questions } as unknown as ReviewDetail)
  const found = openQuestions([run([question('Q1', [1]), question('Q3', [2], 'resolved')]), run([question('Q1', [1]), question('Q2', [], 'waiting')])])
  assert.deepEqual(found.map(q => q.id), ['Q1', 'Q2'])
})

test('waitingSuggestions lists each suggestion once', () => {
  const cards = todoCards([row(1, [issue('unusual_account')]), row(5)], [suggestion('D1', [1]), suggestion('D2', [5])], [], file)
  assert.deepEqual(waitingSuggestions(cards).map(s => s.id).sort(), ['D1', 'D2'])
})

test('a charge left off a claim is about documents, and an unclear set about a purchase recorded twice', () => {
  assert.equal(tagOf('not_claimed'), 'Documents')
  assert.equal(tagOf('claim_receipts_unclear'), 'Recorded twice')
})

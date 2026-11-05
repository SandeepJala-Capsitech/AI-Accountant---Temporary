// Run with: npm test.
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { isReversed, newestFirst, overviewReviewLine, runTitle, unseenChanges, type ReviewDetail } from './reviews.ts'
import type { Decision, Question } from './chat.ts'

const decision = (id: string, status: Decision['status']): Decision => ({
  id, client_id: 1, thread_id: null, year_start: '2026-04-01', action: 'keep_one', rule: 'R2', status, trigger: 'background',
  row_ids: [1], evidence: [], reason: '', before: {}, after: {}, actor: 'agent:m', reverses: null,
  created_at: '2026-11-04T10:42:00', prev_hash: '', hash: 'h',
})
const question = (id: string): Question => ({
  id, client_id: 1, year_start: '2026-04-01', row_ids: [4], kind: 'date', text: 'When?', why: 'why', source: 'agent',
  status: 'open', answer: null, created_at: '2026-11-04T10:42:00', resolved_at: null,
})
const run = (over: Partial<ReviewDetail> = {}): ReviewDetail => ({
  id: 'r1', client_id: 1, year_start: '2026-04-01', status: 'done', started_at: '2026-11-04T10:42:00', finished_at: null,
  trigger_summary: 'after 3 files were added', from_seq: 1, to_seq: 3, decision_ids: [], question_ids: [], looked_at: [],
  summary: '', error: '', seen: false, decisions: [], questions: [], applied: {}, ...over,
})
const now = new Date(2026, 10, 4, 15, 0)   // Wed 4 Nov 2026

test('runTitle: today, with sorted and asked parts', () => {
  const r = run({ decisions: [decision('a', 'applied'), decision('b', 'applied')], questions: [question('q')] })
  assert.equal(runTitle(r, now), 'Today 10:42, after 3 files were added: sorted 2, asked 1')
})

test('runTitle: omits zero parts', () => {
  assert.equal(runTitle(run({ decisions: [decision('a', 'applied')] }), now), 'Today 10:42, after 3 files were added: sorted 1')
  assert.equal(runTitle(run({ questions: [question('q')] }), now), 'Today 10:42, after 3 files were added: asked 1')
})

test('runTitle: nothing to change', () => {
  assert.equal(runTitle(run(), now), 'Today 10:42, after 3 files were added: Nothing to change.')
})

test('runTitle: suggest mode says suggested', () => {
  const r = run({ decisions: [decision('a', 'suggested'), decision('b', 'suggested')] })
  assert.equal(runTitle(r, now), 'Today 10:42, after 3 files were added: suggested 2')
})

test('runTitle: yesterday and older days', () => {
  assert.equal(runTitle(run({ started_at: '2026-11-03T09:05:00' }), now), 'Yesterday 09:05, after 3 files were added: Nothing to change.')
  assert.equal(runTitle(run({ started_at: '2026-11-03T09:05:00', decisions: [decision('a', 'applied')] }), new Date(2026, 10, 5, 8, 0)),
    'Tue 3 Nov 09:05, after 3 files were added: sorted 1')
})

test('overviewReviewLine', () => {
  assert.equal(overviewReviewLine(null, 3), null)
  assert.equal(overviewReviewLine('2026-11-04T10:42:00', 0), "AI review: last run 10:42. 0 changes you haven't looked at")
  assert.equal(overviewReviewLine('2026-11-04T10:42:00', 1), "AI review: last run 10:42. 1 change you haven't looked at")
  assert.equal(overviewReviewLine('2026-11-04T10:42:00', 2), "AI review: last run 10:42. 2 changes you haven't looked at")
})

test('unseenChanges counts applied and suggested changes of unseen done runs, not undos, seen or other runs', () => {
  const undo = { ...decision('u', 'applied'), reverses: 'a' }
  const runs = [
    run({ decisions: [decision('a', 'applied'), undo, decision('s', 'suggested')] }),
    run({ id: 'r2', seen: true, decisions: [decision('b', 'applied')] }),
    run({ id: 'r3', status: 'failed', decisions: [decision('c', 'applied'), decision('d', 'applied')] }),
  ]
  assert.equal(unseenChanges(runs), 2)
  assert.equal(isReversed(runs[0], 'a'), true)
  assert.equal(isReversed(runs[0], 'x'), false)
})

test('newestFirst orders by start', () => {
  const order = newestFirst([run({ id: 'old', started_at: '2026-11-01T10:00:00' }), run({ id: 'new', started_at: '2026-11-04T10:00:00' })])
  assert.deepEqual(order.map(r => r.id), ['new', 'old'])
})

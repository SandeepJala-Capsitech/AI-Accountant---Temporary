// Run with: npm test (Node's own test runner; Node strips the TypeScript types itself).
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { possibleDuplicates, type PaymentRow } from './duplicates.ts'

const boiler: PaymentRow = { sourceId: 1, gross: '280.00', direction: 'out', date: '2025-05-08', kind: 'document',
                             ref: 'boiler', linked: [] }

test('a bill and the item of an agent statement that paid it are not duplicates', () => {
  // The agent paid the boiler bill out of the rent: the bill and that item were flagged as a possible duplicate.
  const item: PaymentRow = { ...boiler, sourceId: 2, date: '2025-05-09', ref: 'item', linked: ['boiler'] }
  assert.deepEqual(possibleDuplicates([{ ...boiler, linked: ['item'] }, item]), [null, null])
})

test('the same amount from two uploads a day apart is still flagged', () => {
  assert.deepEqual(possibleDuplicates([boiler, { ...boiler, sourceId: 2, date: '2025-05-09', ref: 'other' }]), [null, 0])
})

// Run with: npm test (Node's own test runner; Node strips the TypeScript types itself).
import { test } from 'node:test'
import assert from 'node:assert/strict'
import type { Transaction } from './api.ts'
import { rowStatus, totalsOf } from './ledger.ts'

// A transaction as the API returns it (an unpaid rent bill), with what a test needs changed.
const tx = (change: Partial<Transaction> = {}): Transaction => ({
  date: '2026-10-01', description: 'October rent', direction: 'out', gross: '12000.00', vat: '2000.00',
  vat_treatment: null, vat_posted: '2000.00', net: '10000.00', account_code: '7100', contra_account_code: '2100',
  currency: 'GBP', source: 'text', method: 'llm', evidence: null, issues: [], document_type: 'invoice',
  counterparty: 'Business Cube', document_ref: 'bill', owed: '12000.00', paid_by: [], ...change,
})

const paidBy = [{ ref: 'line', amount: '12000.00', date: '2026-10-03', description: 'BUSINESS CUBE MGMT' }]

test('an unpaid bill is owed to the supplier, and a paid one says when it was paid', () => {
  assert.deepEqual(rowStatus(tx()).map(s => s.text), ['Unpaid, owed to Business Cube'])
  assert.deepEqual(rowStatus(tx({ owed: '0.00', paid_by: paidBy })).map(s => s.text), ['Paid by bank line on 3 Oct'])
})

test('who owes whom follows the direction of the money, so credit notes read the right way', () => {
  // Final review #5: the text followed the Debtors account instead, so both kinds of credit note read backwards.
  const sale = { account_code: '4000', contra_account_code: '1100', counterparty: 'Harbour & Lane' }
  assert.equal(rowStatus(tx({ ...sale, direction: 'in' }))[0].text, 'Unpaid, owed by Harbour & Lane')
  assert.equal(rowStatus(tx({ ...sale, direction: 'out' }))[0].text, 'Unpaid, owed to Harbour & Lane')
  assert.equal(rowStatus(tx({ direction: 'in', counterparty: 'Clearway' }))[0].text, 'Unpaid, owed by Clearway')
})

test('a choice between documents can also be declined', () => {
  // Final review #2: with only Link buttons, the person had to post the payment wrongly to unblock the trial balance.
  const option = { ref: 'bill', amount: '72.00', date: '2026-09-20', description: 'BT' }
  const [status] = rowStatus(tx({
    document_type: 'statement', owed: null, candidates: [[option], [{ ...option, ref: 'bill-2' }]],
    issues: [{ code: 'choose_payment', severity: 'error', message: 'Could pay: … Choose one.' }],
  }))
  assert.equal(status.text, 'Could pay:')
  assert.deepEqual(status.actions.map(a => a.link), [['bill'], ['bill-2'], []])
  assert.equal(status.actions[2].label, 'None of these')
})

test('document totals say what is still owed, not only what was billed', () => {
  // Final review #12: "Owed by you £12,000.00" showed for a bill that was already paid.
  const bill = tx({ owed: '0.00', paid_by: paidBy })
  const line = tx({ document_type: 'statement', document_ref: 'line', vat: null, vat_posted: '0.00', net: '12000.00',
                    owed: null, contra_account_code: '1200', paid_against: '2100' })
  assert.deepEqual(totalsOf([bill, line]).map(t => [t.label, t.gross, t.stillOwed]), [
    ['Paid out (bank)', '12000.00', undefined],
    ['Documents, money out', '12000.00', '0.00'],
  ])
})

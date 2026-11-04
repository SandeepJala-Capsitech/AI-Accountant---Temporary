// Run with: npm test (Node's own test runner; Node strips the TypeScript types itself).
import { test } from 'node:test'
import assert from 'node:assert/strict'
import type { BankCheck, Client, InvoiceSummary, Ledger, OpeningBalances, SavedRow, Year } from './clients.ts'
import type { TrialBalanceResult } from './api.ts'
import {
  bankCheckNotice, bankCheckText, closeQuestion, defaultBank, fullDate, linesCount, linesFor, openingAccounts, openingErrors, splitInvoices, trialRows,
} from './sections.ts'

const check = (change: Partial<BankCheck> = {}): BankCheck => ({
  code: '1200', name: 'Current', kind: 'current', books: '100.00', statement: '100.00', statement_date: '2025-09-30',
  difference: '0.00', ...change,
})

const inv = (kind: 'sale' | 'purchase'): InvoiceSummary => ({
  ref: kind, kind, counterparty: 'Acme', number: '1', date: '2025-05-01', total: '10.00', paid: '0.00', owed: '10.00',
  paid_by: [], brought_forward: false,
})

test('the bank check says whether the books agree with the statement', () => {
  assert.equal(bankCheckText(check({ difference: '0.00', statement_date: '2025-09-30' })), 'Agrees with the statement of 30 Sep 2025.')
  assert.equal(bankCheckText(check({ difference: '-25.00', statement_date: '2025-09-30' })), '£25.00 less in the books than the statement of 30 Sep 2025.')
  assert.equal(bankCheckText(check({ difference: '1200.50', statement_date: '2025-09-30' })), '£1,200.50 more in the books than the statement of 30 Sep 2025.')
  assert.equal(bankCheckText(check({ statement: null, difference: null, statement_date: null })), 'No statement this year.')
})

test('invoices split into sales and purchases', () => {
  const { sales, purchases } = splitInvoices([inv('sale'), inv('purchase'), inv('sale')])
  assert.equal(sales.length, 2); assert.equal(purchases.length, 1)
})

test('dates are written 30 Sep 2025, and a missing one says so', () => {
  assert.equal(fullDate('2025-09-30'), '30 Sep 2025')
  assert.equal(fullDate('2026-03-01'), '1 Mar 2026')
  assert.equal(fullDate(null), 'No date')
})

test('the notice names the first problem that stops the bank check', () => {
  assert.equal(bankCheckNotice('2 problem(s) must be fixed before the trial balance: #3: The date is missing. #5: The total is wrong.'),
               'The bank check needs the trial balance: the date is missing.')
  assert.equal(bankCheckNotice('Fix the earlier years first: 1 problem(s) must be fixed before the trial balance: #2: Unknown account.'),
               'The bank check needs the trial balance: unknown account.')
  assert.equal(bankCheckNotice('Something else went wrong.'), 'The bank check needs the trial balance: something else went wrong.')
})

const client = (kinds: ('current' | 'savings')[]): Client => ({
  id: 1, name: 'Acme', business_type: 'limited_company', contact_name: '', contact_email: '', contact_phone: '',
  vat_registered: false, first_year_end: '2026-03-31', folder: '', archived: false, archived_at: null, created_at: '', updated_at: '',
  bank_accounts: kinds.map((kind, i) => ({ code: kind === 'current' ? `120${i}` : `121${i}`, name: `Account ${i}`, kind })),
})

let nextId = 1
const row = (change: Partial<SavedRow>): SavedRow => ({
  id: nextId++, upload_id: 1, edited: false, original: null,
  date: '2025-05-01', description: 'x', direction: 'out', gross: '10.00', vat: null, vat_treatment: null, vat_posted: null, net: null,
  account_code: '7100', contra_account_code: null, currency: 'GBP', source: 'pdf', method: 'llm', evidence: null, issues: [],
  document_type: 'statement', bank_account: '1200', ...change,
})
const ledgerOf = (c: Client, rows: SavedRow[]) => ({ client: c, transactions: rows }) as unknown as Ledger

test('the default bank is the first current account, else the first account', () => {
  assert.equal(defaultBank(client(['savings', 'current'])), '1201')
  assert.equal(defaultBank(client(['savings'])), '1210')
})

test('an account shows its statement lines and the transfers into it from another account', () => {
  const c = client(['current', 'savings'])   // codes 1200 and 1211
  const own = row({ bank_account: '1200', description: 'own' })
  const unset = row({ bank_account: null, description: 'default account' })
  const other = row({ bank_account: '1211', description: 'savings line' })
  const transferIn = row({ bank_account: '1211', account_code: '1200', description: 'transfer to current' })
  const receipt = row({ bank_account: null, document_type: 'receipt', description: 'a receipt' })
  const ledger = ledgerOf(c, [own, unset, other, transferIn, receipt])
  assert.deepEqual(linesFor(ledger, '1200').map(r => r.description), ['own', 'default account', 'transfer to current'])
  assert.deepEqual(linesFor(ledger, '1211').map(r => r.description), ['savings line', 'transfer to current'])
})

test("an account's count is of the lines in the books, and says how many it lists left out", () => {
  // The Bank page counted a bank line a decision left out, so its lines did not add up to the books.
  const kept = [row({}), row({ id: 2 })]
  assert.equal(linesCount(kept, 'Current'), '2 transactions on Current')
  assert.equal(linesCount([...kept, row({ id: 3, excluded: true }), row({ id: 4, superseded_by: 'd1' })], 'Current'),
               '2 transactions on Current, 2 left out')
  assert.equal(linesCount([row({ excluded: true })], 'Current'), '0 transactions on Current, 1 left out')
})

const year = (end: string): Year => ({ start: '2025-04-01', end, label: `Year to ${end}`, closed: false })

test('opening balances must balance', () => {
  assert.deepEqual(openingErrors({ lines: [{ code: '1200', debit: '10.00', credit: '0.00' }], unpaid: [] }),
    ['Opening debits £10.00 and credits £0.00 must be the same.'])
  assert.deepEqual(openingErrors({ lines: [], unpaid: [] }), [])
})

test('the unpaid invoices must make up Debtors and Creditors', () => {
  const item = (direction: 'in' | 'out', amount: string) => ({ id: direction + amount, direction, counterparty: 'Acme', amount, number: null, date: null })
  const balanced = (o: Partial<OpeningBalances>): OpeningBalances => ({
    lines: [
      { code: '1100', debit: '300.00', credit: '0.00' }, { code: '2100', debit: '0.00', credit: '120.50' },
      { code: '3100', debit: '0.00', credit: '179.50' },
    ],
    unpaid: [item('in', '300.00'), item('out', '120.50')], ...o,
  })
  assert.deepEqual(openingErrors(balanced({})), [])
  assert.deepEqual(openingErrors(balanced({ unpaid: [item('in', '250.00'), item('out', '120.50')] })),
    ['The unpaid invoices from customers add up to £250.00; Debtors is £300.00.'])
  assert.deepEqual(openingErrors(balanced({ unpaid: [item('in', '300.00')] })),
    ['The unpaid bills from suppliers add up to £0.00; Creditors is £120.50.'])
  assert.deepEqual(openingErrors(balanced({ unpaid: [item('in', '1300.00'), item('out', '120.50')] })),
    ['The unpaid invoices from customers add up to £1,300.00; Debtors is £300.00.'])
})

test('closing a year asks first', () => {
  assert.match(closeQuestion(year('2026-03-31')), /^Close the year to 31 Mar 2026\?/)
  assert.equal(closeQuestion(year('2026-03-31')),
    'Close the year to 31 Mar 2026? Its figures are locked and carried into the next year. You can reopen it later.')
})

test('the trial balance shows each account opening, moving and closing', () => {
  const line = (code: string, debit: string, credit: string) => ({ code, name: `Account ${code}`, type: 'asset', debit, credit })
  const result = {
    opening: [line('1200', '100.00', '0.00'), line('3100', '0.00', '100.00')],
    lines: [line('1200', '70.00', '0.00'), line('3100', '0.00', '100.00'), line('7100', '30.00', '0.00')],
  } as unknown as TrialBalanceResult
  assert.deepEqual(trialRows(result).map(r => [r.code, r.opening, r.movement, r.closing]), [
    ['1200', 10000, -3000, 7000],
    ['3100', -10000, 0, -10000],
    ['7100', 0, 3000, 3000],
  ])
})

test('opening balances are offered balance sheet accounts and the client\'s own bank accounts', () => {
  const account = (code: string, type: string) => ({ code, name: code, type, vat: 'outside_scope' })
  const chart = [account('7100', 'expense'), account('4000', 'income'), account('3200', 'equity'), account('1100', 'asset'),
                 account('1201', 'asset'), account('1200', 'asset'), account('2100', 'liability')]
  assert.deepEqual(openingAccounts(chart, client(['current'])).map(a => a.code), ['1100', '1200', '2100', '3200'])
})

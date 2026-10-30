// What the Bank and Invoices sections say and show, kept apart from the screens so node --test can run them.
import type { TrialBalanceResult } from './api'
import type { AccountChoice, BankCheck, Client, InvoiceSummary, Ledger, OpeningBalances, SavedRow, Year } from './clients'
import { longDate, money } from './ledger.ts'

// 30 Sep 2025; "No date" when there is none.
export const fullDate = (iso: string | null | undefined): string => (iso ? longDate(iso) : 'No date')

// How the books compare with the latest statement of the year. The difference is the books less the statement.
export function bankCheckText(c: BankCheck): string {
  if (c.statement === null || c.difference === null || c.statement_date === null) return 'No statement this year.'
  const on = `the statement of ${fullDate(c.statement_date)}`
  const difference = Number(c.difference)
  if (difference === 0) return `Agrees with ${on}.`
  return `${money(c.difference.replace(/^-/, ''))} ${difference < 0 ? 'less' : 'more'} in the books than ${on}.`
}

// What the page says when the bank check cannot be worked out because rows need fixing. The API's message lists the
// problems as "#3: …"; the notice names the first, without the row number.
export function bankCheckNotice(message: string): string {
  const first = /#\d+:\s*(.+?)(?=\s+#\d+:|$)/.exec(message)?.[1] ?? message
  return `The bank check needs the trial balance: ${first.charAt(0).toLowerCase()}${first.slice(1)}`
}

// The account a row with no bank account of its own (a receipt, say) is paid from: the first current account.
export const defaultBank = (client: Client): string | null =>
  (client.bank_accounts.find(a => a.kind === 'current') ?? client.bank_accounts[0])?.code ?? null

// The year's rows on an account's statements, and the lines on another account's statement that are coded to it
// (transfers in), by date.
export function linesFor(ledger: Ledger, code: string): SavedRow[] {
  const fallback = defaultBank(ledger.client)
  return ledger.transactions
    .filter(row => row.document_type === 'statement' && ((row.bank_account ?? fallback) === code || row.account_code === code))
    .sort((a, b) => (a.date ?? '9999').localeCompare(b.date ?? '9999'))
}

export function splitInvoices(list: InvoiceSummary[]): { sales: InvoiceSummary[]; purchases: InvoiceSummary[] } {
  return { sales: list.filter(i => i.kind === 'sale'), purchases: list.filter(i => i.kind === 'purchase') }
}

// Money in whole pence, so sums are exact. A blank or unreadable amount counts as nothing.
const pence = (amount: string): number => Math.round(Number(amount) * 100) || 0
const pounds = (p: number): string => `${p < 0 ? '-' : ''}${money((Math.abs(p) / 100).toFixed(2))}`

const DEBTORS = '1100'
const CREDITORS = '2100'

// What is wrong with a first year's opening balances, in the words the server uses (books.check_opening): debits
// and credits are the same, and the unpaid invoices add up to Debtors and Creditors. Empty when all is well.
export function openingErrors(o: OpeningBalances): string[] {
  const errors: string[] = []
  const debits = o.lines.reduce((sum, l) => sum + pence(l.debit), 0)
  const credits = o.lines.reduce((sum, l) => sum + pence(l.credit), 0)
  if (debits !== credits) errors.push(`Opening debits ${pounds(debits)} and credits ${pounds(credits)} must be the same.`)
  const balance = (code: string) => o.lines.filter(l => l.code === code).reduce((sum, l) => sum + pence(l.debit) - pence(l.credit), 0)
  const owed = (direction: 'in' | 'out') => o.unpaid.filter(i => i.direction === direction).reduce((sum, i) => sum + pence(i.amount), 0)
  if (owed('in') !== balance(DEBTORS)) {
    errors.push(`The unpaid invoices from customers add up to ${pounds(owed('in'))}; Debtors is ${pounds(balance(DEBTORS))}.`)
  }
  if (owed('out') !== -balance(CREDITORS)) {
    errors.push(`The unpaid bills from suppliers add up to ${pounds(owed('out'))}; Creditors is ${pounds(-balance(CREDITORS))}.`)
  }
  return errors
}

export const closeQuestion = (year: Year): string =>
  `Close the year to ${longDate(year.end)}? Its figures are locked and carried into the next year. You can reopen it later.`

export interface TrialRow {
  code: string
  name: string
  type: string
  opening: number            // each in pence, a debit positive and a credit negative
  movement: number
  closing: number
}

// The year's accounts with what each opened with, moved by and closed with, by account code.
export function trialRows(result: TrialBalanceResult): TrialRow[] {
  const net = (l: { debit: string; credit: string }) => pence(l.debit) - pence(l.credit)
  const rows = new Map<string, TrialRow>()
  const rowOf = (l: { code: string; name: string; type: string }) => {
    if (!rows.has(l.code)) rows.set(l.code, { code: l.code, name: l.name, type: l.type, opening: 0, movement: 0, closing: 0 })
    return rows.get(l.code)!
  }
  for (const l of result.opening ?? []) rowOf(l).opening = net(l)
  for (const l of result.lines) rowOf(l).closing = net(l)
  return [...rows.values()].sort((a, b) => a.code.localeCompare(b.code))
    .map(r => ({ ...r, movement: r.closing - r.opening }))
}

// £1,200.00 Dr for a debit balance, £300.00 Cr for a credit one; a dash for nothing.
export const balanceText = (p: number): string => (p === 0 ? '—' : `${pounds(Math.abs(p))} ${p > 0 ? 'Dr' : 'Cr'}`)

// The accounts opening balances can go on: the chart's assets, liabilities and equity (income and cost accounts are
// refused), less the bank accounts and cards (12xx) the client does not have.
export const openingAccounts = (chart: AccountChoice[], client: Client): AccountChoice[] =>
  chart.filter(a => ['asset', 'liability', 'equity'].includes(a.type)
    && (!a.code.startsWith('12') || client.bank_accounts.some(b => b.code === a.code)))
    .sort((a, b) => a.code.localeCompare(b.code))

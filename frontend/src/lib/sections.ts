// What the Bank and Invoices sections say and show, kept apart from the screens so node --test can run them.
import type { BankCheck, Client, InvoiceSummary, Ledger, SavedRow } from './clients'
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

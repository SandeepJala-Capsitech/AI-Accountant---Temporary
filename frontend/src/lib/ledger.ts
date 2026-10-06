// What the table shows for each row and in its totals: how a row is booked, the status line under its
// description with the choices it offers a person, and the totals. Pure functions, tested in ledger.test.ts.
import type { Settlement, Transaction } from './api'

const fmt = (n: number) =>
  n === 0
    ? ''
    : `£${Math.abs(n).toLocaleString('en-GB', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`

// Money as the table shows it: £1,234.50, or a dash when unknown.
export const money = (value: string | null | undefined) => (value == null ? '—' : fmt(Number(value)) || '£0.00')

export const NOT_TRANSACTIONS: Record<string, string> = {
  quote: 'a quote', pro_forma: 'a pro forma invoice', purchase_order: 'a purchase order',
  remittance_advice: 'a remittance advice', supplier_statement: "a supplier's statement of account",
  other: 'a document that is not a transaction',
}

export type RowKind = 'document' | 'bank' | 'other' | 'not booked'

// How a row is booked: a document holding what is owed, a bank line, anything else paid when it
// happened, or not booked at all (a quote or the like, until a person ticks Include).
export const kindOf = (tx: Transaction): RowKind => {
  const type = tx.document_type ?? 'receipt'
  if (type in NOT_TRANSACTIONS) return tx.include ? 'document' : 'not booked'
  if (type === 'invoice' || type === 'expense_claim') return 'document'
  return type === 'statement' ? 'bank' : 'other'
}

const shortDate = (iso: string | null) =>
  iso ? new Date(`${iso}T00:00:00`).toLocaleDateString('en-GB', { day: 'numeric', month: 'short' }) : 'no date'

const optionLabel = (option: Settlement[]) =>
  option.map(s => `${s.description} (${money(s.amount)}, ${shortDate(s.date)})`).join(' + ')

// A button under a bank line: clicking it sets the line's link (null: match automatically).
export interface RowAction { label: string; link: string[] | null }

// One status line under a row's description. `include` is set on a document that is not a transaction:
// the Include tick, and whether it is ticked.
export interface RowStatus { text: string; actions: RowAction[]; include?: boolean }

// What is still owed on a document, or how it was paid. Money in means they owe the business (a sale, or a
// supplier's credit note); money out means the business owes them (a bill, a claim, a customer's credit note).
function owedStatus(tx: Transaction): RowStatus | null {
  if (tx.owed == null) return null
  const owed = Number(tx.owed)
  const who = tx.counterparty ?? 'them'
  const text = !tx.paid_by?.length
    ? (tx.direction === 'in' ? `Unpaid, owed by ${who}` : `Unpaid, owed to ${who}`)
    : owed > 0 ? `Part paid: ${money(tx.owed)} still owed`
    : owed < 0 ? `Overpaid by ${money(String(-owed))}`
    : `Paid by bank line on ${shortDate(tx.paid_by[tx.paid_by.length - 1].date)}`
  return { text, actions: [] }
}

// The status lines under a row's description, with the person's choices: Include, Link, Unlink. A choice
// between documents can also be declined: "None of these" makes it an ordinary bank line.
export function rowStatus(tx: Transaction): RowStatus[] {
  const type = tx.document_type ?? 'receipt'
  if (type in NOT_TRANSACTIONS) {
    const tick: RowStatus = {
      text: tx.include ? 'Included as an invoice'
        : `Looks like ${NOT_TRANSACTIONS[type]}, not booked. Tick to include it as an invoice`,
      actions: [], include: !!tx.include,
    }
    const owed = tx.include ? owedStatus(tx) : null
    return owed ? [tick, owed] : [tick]
  }
  const kind = kindOf(tx)
  if (kind === 'document') {
    const owed = owedStatus(tx)
    return owed ? [owed] : []
  }
  if (kind !== 'bank') return []
  if (tx.pays?.length) return [{ text: `Pays: ${optionLabel(tx.pays)}`, actions: [{ label: 'Unlink', link: [] }] }]
  if (tx.candidates?.length) {
    const choosing = tx.issues.some(i => i.code === 'choose_payment')
    return [{
      text: choosing ? 'Could pay:' : 'May pay:',
      actions: [
        ...tx.candidates.map(option => ({ label: `Link ${optionLabel(option)}`, link: option.map(s => s.ref) })),
        { label: 'None of these', link: [] },
      ],
    }]
  }
  if (tx.link && !tx.link.length) {
    return [{ text: 'Not matched to a document', actions: [{ label: 'Match automatically', link: null }] }]
  }
  return []
}

export interface TotalRow {
  key: string
  label: string
  count: number
  gross: string | null
  vat: string | null
  net: string | null
  stillOwed?: string     // documents only: what is still owed on them
}

// Totals by how rows are booked, in whole pennies so they don't drift: money that moved through the
// bank, and documents (bills, claims, sales invoices, credit notes) with what is still owed on them. An
// unpaid bill is not money out, so the two are kept apart; rows not booked are left out. A row with an
// error (impossible VAT) has no VAT or net, so its group's VAT and net totals are unknown.
export function totalsOf(txs: Transaction[]): TotalRow[] {
  const sum = (values: (string | null)[]) => values.some(v => v == null) ? null
    : (values.reduce((pennies, v) => pennies + Math.round(Number(v) * 100), 0) / 100).toFixed(2)
  const moved = (tx: Transaction) => ['bank', 'other'].includes(kindOf(tx))
  const groups = [
    { key: 'bank-out', label: 'Paid out (bank)', keep: (tx: Transaction) => moved(tx) && tx.direction === 'out' },
    { key: 'bank-in', label: 'Received (bank)', keep: (tx: Transaction) => moved(tx) && tx.direction === 'in' },
    { key: 'documents-out', label: 'Documents, money out', keep: (tx: Transaction) => kindOf(tx) === 'document' && tx.direction === 'out' },
    { key: 'documents-in', label: 'Documents, money in', keep: (tx: Transaction) => kindOf(tx) === 'document' && tx.direction === 'in' },
  ]
  return groups.flatMap(({ key, label, keep }) => {
    const rows = txs.filter(keep)
    if (!rows.length) return []
    const total: TotalRow = { key, label, count: rows.length, gross: sum(rows.map(tx => tx.gross)),
                              vat: sum(rows.map(tx => tx.vat_posted)), net: sum(rows.map(tx => tx.net)) }
    if (key.startsWith('documents')) {
      // What is still owed is carried by every row of a document, so it is counted once per document.
      const owed = new Map(rows.map((tx, i) => [tx.document_ref ?? `row-${i}`, Math.max(0, Number(tx.owed ?? 0))]))
      total.stillOwed = ([...owed.values()].reduce((pennies, v) => pennies + Math.round(v * 100), 0) / 100).toFixed(2)
    }
    return [total]
  })
}

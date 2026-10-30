// Calls for clients and their saved ledgers (design of 2026-10-06). Paths go through the Next rewrite,
// like every other call in api.ts.
import { request, type Settlement, type Transaction, type TrialBalanceResult } from './api'

export type BusinessType = 'limited_company' | 'sole_trader' | 'partnership' | 'llp'

export type BankAccountKind = 'current' | 'savings' | 'credit_card' | 'petty_cash'

export interface BankAccount {
  code: string               // its account in the chart; '' until the server gives it the next free one
  name: string
  kind: BankAccountKind
}

export interface ClientFields {
  name: string
  business_type: BusinessType
  contact_name: string       // the responsible person: the client's own contact
  contact_email: string
  contact_phone: string
  vat_registered: boolean
  first_year_end: string     // the end of the first year the books are kept for: "2026-03-31"
  bank_accounts: BankAccount[]
  folder: string             // where the client's files are on the computer the site runs on; '' for none
}

export interface ClosedYear {
  start: string
  end: string
  closed_at: string
}

export interface Client extends ClientFields {
  id: number
  closed_years?: ClosedYear[]
  archived: boolean
  archived_at: string | null
  created_at: string
  updated_at: string
}

export interface ClientSummary extends Client {
  rows: number
  to_review: number          // rows with an error or a warning
}

export interface StatementCheck {
  status: 'ok' | 'gap' | 'none'
  difference: string | null
}

export interface Upload {
  id: number
  name: string               // the file's name, "Pasted text" or "Manual entry"
  kind: string
  sha256: string             // the file's fingerprint, to skip a file uploaded before
  model: string
  warnings: string[]
  created_at: string
  rows: number
  statement: StatementCheck | null
  bank_account?: string | null   // the account or card its statement lines are on; null: the default bank
  year_start?: string | null     // the year it was added to, which its undated rows wait in
}

export type EditableField =
  'date' | 'description' | 'counterparty' | 'direction' | 'gross' | 'vat' | 'account_code' | 'document_type'

export interface SavedRow extends Transaction {
  id: number
  upload_id: number
  edited: boolean
  original: Partial<Record<EditableField, string | null>> | null   // what was read, once a person edits
}

export interface Year {
  start: string              // "2025-04-01"
  end: string
  label: string              // "Year to 31 Mar 2026"
  closed: boolean
  changed_since_closed?: boolean   // a closed year whose rows now work out to other balances than it closed with
}

// One year of a client: its rows, and the uploads that hold them or were added to it.
export interface Ledger {
  client: Client
  year: Year
  years: Year[]
  uploads: Upload[]
  transactions: SavedRow[]
}

// A person's change to a row: Link or Include, an edit, or revert.
export type RowChange = Partial<Record<EditableField, string | null>> & {
  link?: string[] | null
  include?: boolean
  apart_from?: number[]
  revert?: boolean
}

// Money is a string everywhere below, like the rest of the API.
export interface OpeningLine {
  code: string
  debit: string
  credit: string
}

export interface OpenItem {
  id: string
  direction: 'in' | 'out'    // in: a customer owes the client; out: the client owes a supplier
  counterparty: string
  amount: string
  number: string | null
  date: string | null
}

export interface OpeningBalances {
  lines: OpeningLine[]
  unpaid: OpenItem[]
}

export interface InvoiceSummary {
  ref: string
  kind: 'sale' | 'purchase'
  counterparty: string | null
  number: string | null
  date: string | null
  total: string
  paid: string
  owed: string               // still owed after every payment matched to it, in any year
  paid_by: Settlement[]
  brought_forward: boolean   // dated before the year and still owed when it started
}

// A bank account's balance in the books against its latest statement of the year, both as the statement prints it.
export interface BankCheck {
  code: string
  name: string
  kind: BankAccountKind
  books: string
  statement: string | null
  statement_date: string | null
  difference: string | null  // books less statement
}

export interface YearSummary {
  year: Year
  rows: number
  to_review: number
  unpaid_sales: string
  unpaid_purchases: string
  bank: BankCheck[]
  trial_balance: 'balanced' | 'blocked'
  problems: string[]         // what stops the trial balance
}

export interface AccountChoice {
  code: string
  name: string
  type: string
  vat: string
}

const send = (method: string, body: unknown): RequestInit => ({
  method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
})

export const listClients = (archived = false) => request<ClientSummary[]>(`/api/clients?archived=${archived}`)
export const addClient = (fields: ClientFields) => request<Client>('/api/clients', send('POST', fields))
export const changeClient = (id: number, change: Partial<ClientFields> & { archived?: boolean }) =>
  request<Client>(`/api/clients/${id}`, send('PATCH', change))
export const getClient = (id: number) => request<Client>(`/api/clients/${id}`)

// The calls for one of a client's years take its start ("2025-04-01"). An unknown year is a 404.
const inYear = (id: number, start: string) => `/api/clients/${id}/years/${start}`
export const getYears = (id: number) => request<Year[]>(`/api/clients/${id}/years`)
export const getYearLedger = (id: number, start: string) => request<Ledger>(`${inYear(id, start)}/ledger`)
export const getYearTrialBalance = (id: number, start: string) =>
  request<TrialBalanceResult>(`${inYear(id, start)}/trial-balance`)
export const getInvoices = (id: number, start: string) => request<InvoiceSummary[]>(`${inYear(id, start)}/invoices`)
export const getBankChecks = (id: number, start: string) => request<BankCheck[]>(`${inYear(id, start)}/bank`)
export const getSummary = (id: number, start: string) => request<YearSummary>(`${inYear(id, start)}/summary`)
export const closeYear = (id: number, start: string) => request<Client>(`${inYear(id, start)}/close`, { method: 'POST' })
export const reopenYear = (id: number, start: string) => request<Client>(`${inYear(id, start)}/reopen`, { method: 'POST' })
export const yearExportUrl = (id: number, start: string) => `${inYear(id, start)}/export.xlsx`
export const getOpening = (id: number) => request<OpeningBalances>(`/api/clients/${id}/opening-balances`)
export const putOpening = (id: number, opening: OpeningBalances) =>
  request<OpeningBalances>(`/api/clients/${id}/opening-balances`, send('PUT', opening))

export interface FolderImportResult {
  jobs: { name: string; job_id: string }[]
  skipped: string[]          // already known, or being read now
  unreadable: string[]       // of a type that can't be read
  refused: { name: string; message: string }[]   // failed their check, with why
}

// Reads the files in the client's folder that it has not had before into a year; a statement goes on bankAccount.
export const importFolder = (id: number, start: string, bankAccount: string | null) =>
  request<FolderImportResult>(`/api/clients/${id}/import-folder`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-LedgerSync': '1' },
    body: JSON.stringify({ year_start: start, bank_account: bankAccount || null }),
  }, 120_000)
export const setUploadBank = (id: number, uploadId: number, code: string) =>
  request<Ledger>(`/api/clients/${id}/uploads/${uploadId}`, send('PATCH', { bank_account: code }))
export const changeRow = (id: number, rowId: number, change: RowChange) =>
  request<Ledger>(`/api/clients/${id}/rows/${rowId}`, send('PATCH', change))
// Rows a person types in, added to the year start names: a row without a date waits in that year.
export const addManualRows = (id: number, transactions: Transaction[], yearStart: string) =>
  request<Ledger>(`/api/clients/${id}/uploads`,
                  send('POST', { name: 'Manual entry', kind: 'manual', transactions, year_start: yearStart }))
export const removeRow = (id: number, rowId: number) =>
  request<Ledger>(`/api/clients/${id}/rows/${rowId}`, { method: 'DELETE' })
export const removeUpload = (id: number, uploadId: number) =>
  request<Ledger>(`/api/clients/${id}/uploads/${uploadId}`, { method: 'DELETE' })
// The accounts a row of this client can be coded to: its own bank accounts and cards among them.
export const listAccounts = (clientId: number) => request<AccountChoice[]>(`/api/accounts?client_id=${clientId}`)

// The open problems of a client's year, for the AI review page: every warning and error on a row in the books, by
// kind, each with the suggestion waiting for it, if any. Kept apart from the network so node --test can run it.
import type { Decision } from './chat.ts'
import { formatMoney } from './chat.ts'
import { dayMonth } from './clientRules.ts'
import type { SavedRow } from './clients'

export type ProblemFamily = 'coding' | 'duplicate' | 'date' | 'combined_vat' | 'missing_vat' | 'conflict' | 'documents' | 'other'

export interface Problem {
  rowId: number
  what: string                  // the row as a person knows it: "Vodafone bill, 12 Oct, £86.40"
  code: string
  message: string
  related: number[]             // the other rows the problem is about
  suggestion: Decision | null   // a change waiting for the person to apply
}
export interface ProblemGroup { family: ProblemFamily; title: string; problems: Problem[] }

// The groups, in the order they are listed.
const TITLES: [ProblemFamily, string][] = [
  ['coding', 'Account'], ['duplicate', 'Recorded twice'], ['date', 'Date'], ['combined_vat', 'VAT across items'],
  ['missing_vat', 'VAT not booked'], ['conflict', 'Records disagree'], ['documents', 'Documents'], ['other', 'Other'],
]

// The family of each issue code: FAMILIES in ledgersync/agent/tools.py. Any other code is 'other'.
const FAMILIES: Record<string, ProblemFamily> = {
  date_missing: 'date', date_conflict: 'date', date_out_of_period: 'date', before_first_year: 'date', file_date: 'date',
  booked_twice: 'duplicate', duplicate_document: 'duplicate', total_mismatch: 'duplicate', possible_duplicate: 'duplicate',
  mixed_items: 'combined_vat', vat_rate_mismatch: 'combined_vat',
  amount_conflict: 'conflict', claim_vat: 'conflict',
  vat_not_booked: 'missing_vat', not_vat_invoice: 'missing_vat',
  not_booked: 'documents', open_document: 'documents', no_payroll: 'documents', needs_document: 'documents',
  account_unclear: 'coding', unusual_account: 'coding', asset_check: 'coding', should_be_asset: 'coding', small_asset: 'coding',
}
export const familyOf = (code: string): ProblemFamily => FAMILIES[code] ?? 'other'

// The family of problem each kind of suggestion settles.
const SETTLES: Record<string, ProblemFamily> = {
  change_account: 'coding', keep_one: 'duplicate', use_primary: 'duplicate', set_date: 'date', split_vat: 'combined_vat',
  set_vat_from_document: 'missing_vat',
}

const whatOf = (row: SavedRow): string =>
  [row.description, row.date ? dayMonth(row.date) : 'no date', formatMoney(row.gross)].join(', ')

// The year's problems by family, in the fixed order, empty families left out. A problem is a warning or an error on
// a row that is in the books (notes are not problems; a row a decision left out or replaced has none). A row with
// two warnings is listed under each. Each suggestion goes with the first problem of the family it settles on the
// first of its rows that has one; a suggestion with no such problem is dropped.
export function groupProblems(rows: SavedRow[], suggestions: Decision[]): ProblemGroup[] {
  const problems: (Problem & { family: ProblemFamily })[] = []
  for (const row of rows) {
    if (row.excluded || row.superseded_by) continue
    for (const issue of row.issues) {
      if (issue.severity === 'info') continue
      problems.push({ rowId: row.id, what: whatOf(row), code: issue.code, message: issue.message,
                      related: issue.related ?? [], suggestion: null, family: familyOf(issue.code) })
    }
  }
  for (const s of suggestions) {
    const family = SETTLES[s.action]
    const found = s.row_ids.map(id => problems.find(p => p.rowId === id && p.family === family && !p.suggestion))
      .find(p => p !== undefined)
    if (found) found.suggestion = s
  }
  return TITLES.map(([family, title]) => ({
    family, title, problems: problems.filter(p => p.family === family).map(({ family: _, ...p }) => p),
  })).filter(g => g.problems.length > 0)
}

export const problemCount = (groups: ProblemGroup[]): number => groups.reduce((n, g) => n + g.problems.length, 0)

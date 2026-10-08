'use client'

import { useMemo, useState } from 'react'
import type { Issue } from '@/lib/api'
import type { Ledger, RowChange, SavedRow } from '@/lib/clients'
import { DOCUMENT_TYPES, transactionCount } from '@/lib/clientRules'
import { DUPLICATE_WINDOW_DAYS, possibleDuplicates } from '@/lib/duplicates'
import { issueTitle, kindOf, money, paymentRowOf, reviewSummary, rowStatus, takesLines, totalsOf } from '@/lib/ledger'

const SEVERITY_ORDER = { error: 0, warning: 1, info: 2 } as const
const MARKS = { error: '✖', warning: '⚠', info: 'ℹ' } as const

// The worst issue on a row, as the Check column shows it.
function checkOf(issues: Issue[]) {
  if (!issues.length) return { mark: '✓', cls: 'check-ok', title: 'No issues' }
  const worst = issues.some(i => i.severity === 'error') ? 'error' : issues.some(i => i.severity === 'warning') ? 'warning' : 'info'
  const cls = { error: 'check-error', warning: 'check-warn', info: 'check-info' }[worst]
  return { mark: `${MARKS[worst]} ${issues.length}`, cls, title: issues.map(i => i.message).join('\n') }
}

const FIELD_NAMES: Record<string, string> = {
  date: 'date', description: 'description', counterparty: 'counterparty', direction: 'in/out', gross: 'amount',
  vat: 'VAT', account_code: 'account', document_type: 'type',
}

// The hover on an edited row: what the model read, where it differs from now.
function originalText(row: SavedRow): string {
  const now = row as unknown as Record<string, string | null | undefined>
  const changed = Object.entries(row.original ?? {})
    .filter(([field, value]) => (value ?? '') !== (now[field] ?? ''))
    .map(([field, value]) => `${FIELD_NAMES[field] ?? field} ${field === 'gross' || field === 'vat' ? money(value) : value || 'none'}`)
  return changed.length ? `Read by the model: ${changed.join(', ')}` : 'Edited'
}

// What is wrong with a row, in words, under its description: errors first, then warnings, then notes.
function IssueList({ issues }: { issues: Issue[] }) {
  if (!issues.length) return null
  const sorted = [...issues].sort((a, b) => SEVERITY_ORDER[a.severity] - SEVERITY_ORDER[b.severity])
  return (
    <ul className="issues">
      {sorted.map((issue, k) => (
        <li key={k} className={`issue issue-${issue.severity}`}>
          <span className="issue-mark" aria-hidden="true">{MARKS[issue.severity]}</span>
          <span><strong>{issueTitle(issue.code)}.</strong> {issue.message}</span>
        </li>
      ))}
    </ul>
  )
}

// The client's saved rows: what needs review and why, statuses with Link, Unlink and Include, Edit, Add line,
// Remove and Revert, and totals.
export default function TransactionsTable({ ledger, readOnly, onChange, onEdit, onAdd, onRemove }: {
  ledger: Ledger
  readOnly: boolean
  onChange: (rowId: number, change: RowChange) => void
  onEdit?: (row: SavedRow) => void
  onAdd?: (lineOf: SavedRow | null) => void     // a new transaction, or a line of the row's document
  onRemove?: (row: SavedRow) => void
}) {
  const rows = ledger.transactions
  const [only, setOnly] = useState<string | null>(null)   // an issue code, 'review' for every flagged row, or all
  const uploads = useMemo(() => new Map(ledger.uploads.map(u => [u.id, u.name])), [ledger.uploads])
  const issuesOf = useMemo(() => {
    const duplicateOf = possibleDuplicates(rows.map(paymentRowOf))
    return rows.map((row, i): Issue[] => {
      const of = duplicateOf[i]
      return of === null ? row.issues : [...row.issues, {
        code: 'possible_duplicate', severity: 'warning',
        message: `It may repeat "${rows[of].description}" from ${uploads.get(rows[of].upload_id) ?? 'another upload'}: `
                 + `the same amount and direction, dated within ${DUPLICATE_WINDOW_DAYS} days. Remove one if it is.`,
      }]
    })
  }, [rows, uploads])
  const review = useMemo(() => reviewSummary(issuesOf), [issuesOf])
  const flagged = issuesOf.filter(issues => issues.some(i => i.severity !== 'info')).length
  const totals = useMemo(() => totalsOf(rows), [rows])
  const shown = rows.map((row, i) => ({ row, issues: issuesOf[i] })).filter(({ issues }) =>
    only === null || (only === 'review' ? issues.some(i => i.severity !== 'info') : issues.some(i => i.code === only)))
  const filter = only !== null && !shown.length ? null : only   // a filter whose rows are all fixed shows everything

  return (
    <section className="card section table-card">
      <div className="section-head">
        <h2>Transactions</h2>
        <span className="muted small">{transactionCount(rows.length)}</span>
        {!readOnly && onAdd && (
          <button type="button" className="btn btn-small" onClick={() => onAdd(null)}>Add transaction</button>
        )}
      </div>
      {flagged > 0 && (
        <div className="review-bar" role="group" aria-label="Rows to review">
          <span className="review-count">{flagged} to review</span>
          <button type="button" className={`chip${filter === null ? ' chip-on' : ''}`} onClick={() => setOnly(null)}>All</button>
          <button type="button" className={`chip${filter === 'review' ? ' chip-on' : ''}`} onClick={() => setOnly('review')}>
            Only these
          </button>
          {review.map(item => (
            <button key={item.code} type="button" onClick={() => setOnly(item.code)}
                    className={`chip chip-${item.severity}${filter === item.code ? ' chip-on' : ''}`}>
              {item.title} <span className="chip-count">{item.rows}</span>
            </button>
          ))}
        </div>
      )}
      {!rows.length ? <p className="muted">Add documents to see their transactions here.</p> : (
        <table className="table">
          <thead>
            <tr>
              <th>Date</th>
              <th>Description</th>
              <th>In / out</th>
              <th className="num">Amount</th>
              <th className="num">VAT</th>
              <th className="num">Net</th>
              <th>Account</th>
              <th>Check</th>
              <th aria-label="Actions" />
            </tr>
          </thead>
          <tbody>
            {(filter === null ? rows.map((row, i) => ({ row, issues: issuesOf[i] })) : shown).map(({ row, issues }) => {
              const check = checkOf(issues)
              const origin = `${uploads.get(row.upload_id) ?? ''} · ${DOCUMENT_TYPES[row.document_type ?? 'receipt'] ?? row.document_type}`
              const classes = [kindOf(row) === 'not booked' ? 'row-not-booked' : '',
                               check.cls === 'check-error' ? 'row-flag-error' : check.cls === 'check-warn' ? 'row-flag-warn' : '']
              return (
                <tr key={row.id} className={classes.filter(Boolean).join(' ') || undefined}>
                  <td className="mono">{row.date ?? '—'}</td>
                  <td>
                    {row.description}
                    {row.edited && <span className="badge badge-muted edited" title={originalText(row)}>Edited</span>}
                    <div className="row-origin" title={origin}>{origin}</div>
                    {rowStatus(row).map((status, k) => (status.include !== undefined ? (
                      <label key={k} className="row-status">
                        <input type="checkbox" checked={status.include} disabled={readOnly}
                               onChange={e => onChange(row.id, { include: e.target.checked })} />
                        {status.text}
                      </label>
                    ) : (
                      <div key={k} className="row-status">
                        {status.text}
                        {!readOnly && status.actions.map((action, j) => (
                          <button key={j} type="button" className="link-btn" onClick={() => onChange(row.id, action.change)}>
                            {action.label}
                          </button>
                        ))}
                      </div>
                    )))}
                    <IssueList issues={issues} />
                  </td>
                  <td>
                    <span className={row.direction === 'in' ? 'badge badge-accent' : 'badge badge-muted'}>
                      {row.direction === 'in' ? 'Money in' : 'Money out'}
                    </span>
                  </td>
                  <td className="num">{money(row.gross)}</td>
                  <td className="num">{money(row.vat_posted)}</td>
                  <td className="num">{money(row.net)}</td>
                  <td>
                    {row.paid_against
                      ? `${row.paid_against} ${row.paid_against_name ?? ''}`
                      : `${row.account_code} ${row.account_name ?? ''}`}
                  </td>
                  <td className={`check ${check.cls}`} title={check.title}>{check.mark}</td>
                  <td className="row-actions">
                    {!readOnly && onEdit && <button type="button" className="link-btn" onClick={() => onEdit(row)}>Edit</button>}
                    {!readOnly && onAdd && takesLines(row) && (
                      <button type="button" className="link-btn" onClick={() => onAdd(row)}>Add line</button>
                    )}
                    {!readOnly && row.edited && (
                      <button type="button" className="link-btn" onClick={() => onChange(row.id, { revert: true })}>Revert</button>
                    )}
                    {!readOnly && onRemove && (
                      <button type="button" className="link-btn danger" onClick={() => onRemove(row)}>Remove</button>
                    )}
                  </td>
                </tr>
              )
            })}
          </tbody>
          <tfoot>
            {totals.map(t => (
              <tr key={t.key}>
                <td colSpan={3}>
                  {t.label} ({transactionCount(t.count)})
                  {t.stillOwed != null && ` · ${money(t.stillOwed)} still owed`}
                </td>
                <td className="num">{money(t.gross)}</td>
                <td className="num" title={t.vat == null ? 'Fix the rows marked ✖ first' : undefined}>{money(t.vat)}</td>
                <td className="num" title={t.net == null ? 'Fix the rows marked ✖ first' : undefined}>{money(t.net)}</td>
                <td colSpan={3} />
              </tr>
            ))}
          </tfoot>
        </table>
      )}
    </section>
  )
}

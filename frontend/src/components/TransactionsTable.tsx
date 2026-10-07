'use client'

import { useMemo } from 'react'
import type { Issue } from '@/lib/api'
import type { Ledger, RowChange, SavedRow } from '@/lib/clients'
import { DOCUMENT_TYPES, transactionCount } from '@/lib/clientRules'
import { DUPLICATE_WINDOW_DAYS, possibleDuplicates } from '@/lib/duplicates'
import { kindOf, money, rowStatus, totalsOf } from '@/lib/ledger'

// The worst issue on a row, as the Check column shows it.
function checkOf(issues: Issue[]) {
  if (!issues.length) return { mark: '✓', cls: 'check-ok', title: 'No issues' }
  const cls = issues.some(i => i.severity === 'error') ? 'check-error'
    : issues.some(i => i.severity === 'warning') ? 'check-warn' : 'check-info'
  return { mark: `${cls === 'check-error' ? '✖' : '⚠'} ${issues.length}`, cls, title: issues.map(i => i.message).join('\n') }
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

// The client's saved rows: statuses with Link, Unlink and Include, duplicate warnings, Revert, and totals.
export default function TransactionsTable({ ledger, readOnly, onChange, onEdit }: {
  ledger: Ledger
  readOnly: boolean
  onChange: (rowId: number, change: RowChange) => void
  onEdit?: (row: SavedRow) => void
}) {
  const rows = ledger.transactions
  const uploads = useMemo(() => new Map(ledger.uploads.map(u => [u.id, u.name])), [ledger.uploads])
  const duplicateOf = useMemo(() => possibleDuplicates(rows.map(row => ({
    sourceId: row.upload_id, gross: row.gross, direction: row.direction, date: row.date, kind: kindOf(row),
  }))), [rows])
  const totals = useMemo(() => totalsOf(rows), [rows])
  const duplicates = duplicateOf.filter(of => of !== null).length

  return (
    <section className="card section table-card">
      <div className="section-head">
        <h2>Transactions</h2>
        <span className="muted small">
          {transactionCount(rows.length)}
          {duplicates ? ` · ${duplicates} possible duplicate${duplicates === 1 ? '' : 's'}` : ''}
        </span>
      </div>
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
            {rows.map((row, i) => {
              const of = duplicateOf[i]
              const issues: Issue[] = of === null ? row.issues : [...row.issues, {
                code: 'possible_duplicate', severity: 'warning',
                message: `Possible duplicate of "${rows[of].description}" (${uploads.get(rows[of].upload_id) ?? 'another upload'}): `
                         + `same amount and direction, dated within ${DUPLICATE_WINDOW_DAYS} days.`,
              }]
              const check = checkOf(issues)
              const origin = `${uploads.get(row.upload_id) ?? ''} · ${DOCUMENT_TYPES[row.document_type ?? 'receipt'] ?? row.document_type}`
              return (
                <tr key={row.id} className={kindOf(row) === 'not booked' ? 'row-not-booked' : undefined}>
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
                          <button key={j} type="button" className="link-btn" onClick={() => onChange(row.id, { link: action.link })}>
                            {action.label}
                          </button>
                        ))}
                      </div>
                    )))}
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
                    {!readOnly && row.edited && (
                      <button type="button" className="link-btn" onClick={() => onChange(row.id, { revert: true })}>Revert</button>
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

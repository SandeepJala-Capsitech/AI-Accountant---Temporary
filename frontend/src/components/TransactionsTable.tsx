'use client'

import { useEffect, useMemo, useRef, useState } from 'react'
import { clientDecisions, postedTrialBalance, type Issue } from '@/lib/api'
import { changedFields, type Decision } from '@/lib/chat'
import { useClientYear } from '@/components/ClientYearContext'
import type { Ledger, RowChange, SavedRow } from '@/lib/clients'
import { DOCUMENT_TYPES, transactionCount } from '@/lib/clientRules'
import { DUPLICATE_WINDOW_DAYS, possibleDuplicates } from '@/lib/duplicates'
import { aboutAnotherRow, accountName, apartChange, apartStatus, canBeApart, checkHover, issueHeading, kindOf, leftOut, money, needsReview,
         paymentRowOf, reviewSummary, rowStatus, showLabel, takesLines, totalsOf } from '@/lib/ledger'

const SEVERITY_ORDER = { error: 0, warning: 1, info: 2 } as const
const MARKS = { error: '✖', warning: '⚠', info: 'ℹ' } as const

// The worst issue on a row, as the Check column shows it. A row left out of the books needs no review, whatever its
// issues say: they stay listed under its description, unmarked.
function checkOf(issues: Issue[], left: boolean) {
  if (left) return { mark: '—', cls: 'check-info', title: 'Left out of the books: nothing to review' }
  if (!issues.length) return { mark: '✓', cls: 'check-ok', title: 'No issues' }
  const worst = issues.some(i => i.severity === 'error') ? 'error' : issues.some(i => i.severity === 'warning') ? 'warning' : 'info'
  const cls = { error: 'check-error', warning: 'check-warn', info: 'check-info' }[worst]
  return { mark: `${MARKS[worst]} ${issues.length}`, cls, title: checkHover(issues) }
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

// What is wrong with a row, in words, under its description: errors first, then warnings, then notes. A warning
// about this row and others offers Show both, and one where two records may be one offers Not the same.
function IssueList({ issues, onShowBoth, onNotSame }: {
  issues: Issue[]
  onShowBoth?: (related: number[]) => void   // each issue shows the rows it is about
  onNotSame?: (related: number[]) => void
}) {
  if (!issues.length) return null
  const sorted = [...issues].sort((a, b) => SEVERITY_ORDER[a.severity] - SEVERITY_ORDER[b.severity])
  return (
    <ul className="issues">
      {sorted.map((issue, k) => (
        <li key={k} className={`issue issue-${issue.severity}`}>
          <span className="issue-mark" aria-hidden="true">{MARKS[issue.severity]}</span>
          <span>
            <strong>{issueHeading(issue.code)}</strong> {issue.message}
            {onShowBoth && aboutAnotherRow(issue.code) && !!issue.related?.length && (
              <button type="button" className="link-btn" onClick={() => onShowBoth(issue.related!)}>
                {showLabel(issue.code)}
              </button>
            )}
            {onNotSame && canBeApart(issue.code) && !!issue.related?.length && (
              <button type="button" className="link-btn" onClick={() => onNotSame(issue.related!)}>Not the same</button>
            )}
          </span>
        </li>
      ))}
    </ul>
  )
}

// The decisions that touched a row, as the Agent badge's popover shows them: the rule, what it was based on, and
// what changed.
function AgentBadge({ decisions }: { decisions: Decision[] }) {
  return (
    <details className="small agent-decisions">
      <summary className="badge badge-accent">Agent</summary>
      {decisions.map(d => (
        <div key={d.id}>
          <strong>{d.rule}</strong>{d.status === 'refused' && ' (refused)'}
          <div className="muted">{d.trigger === 'background' ? 'From a background review' : 'From a chat'}</div>
          {d.reason && <div>{d.reason}</div>}
          {d.evidence.map((e, k) => <div key={k} className="muted">{e.upload_name}: “{e.quote}”</div>)}
          {changedFields(d).map((c, k) => (
            <div key={k} className="muted">Row {c.row} {c.field}: {c.before} → {c.after}</div>
          ))}
        </div>
      ))}
    </details>
  )
}

// The client's saved rows: what needs review and why, statuses with Link, Unlink and Include, Edit, Add line,
// Remove and Revert, and totals. When readOnly (a closed year or an archived client) no control changes a row; Show
// both and the filters still work.
export default function TransactionsTable({ ledger, readOnly, onChange, onEdit, onAdd, onRemove }: {
  ledger: Ledger
  readOnly: boolean
  onChange: (rowId: number, change: RowChange) => void
  onEdit?: (row: SavedRow) => void
  onAdd?: (lineOf: SavedRow | null) => void     // a new transaction, or a line of the row's document
  onRemove?: (row: SavedRow) => void
}) {
  const { client, year } = useClientYear()
  const rows = ledger.transactions
  const [decisions, setDecisions] = useState<Map<string, Decision>>(new Map())
  const [held, setHeld] = useState<Set<number>>(new Set())   // rows waiting in Suspense for a document
  // The decisions and the posted ledger's held rows are read again whenever the rows are loaded again; a failure
  // leaves the table as it was.
  useEffect(() => {
    let current = true
    clientDecisions(client.id, year.start)
      .then(found => { if (current) setDecisions(new Map(found.map(d => [d.id, d]))) }).catch(() => {})
    postedTrialBalance(client.id, year.start)
      .then(tb => { if (current) setHeld(new Set(tb.suspense.filter(s => s.needs).map(s => s.row_id))) }).catch(() => {})
    return () => { current = false }
  }, [client.id, year.start, ledger])
  // Why a row was left out: the reason of the decision that did it (the latest of those that touched the row).
  const whyLeftOut = (row: SavedRow): string | null => {
    const ids = row.superseded_by ? [row.superseded_by] : [...(row.decision_ids ?? [])].reverse()
    for (const id of ids) { const d = decisions.get(id); if (d?.reason) return d.reason }
    return null
  }
  const [only, setOnly] = useState<string | null>(null)   // an issue code, 'review' for every flagged row, or all
  const [focus, setFocus] = useState<number[] | null>(null)   // rows shown side by side (Show both), or none
  const card = useRef<HTMLElement>(null)
  const uploads = useMemo(() => new Map(ledger.uploads.map(u => [u.id, u.name])), [ledger.uploads])
  const byId = useMemo(() => new Map(rows.map(r => [r.id, r])), [rows])
  const described = useMemo(() => new Map(rows.map(r => [r.id, r.description])), [rows])
  const issuesOf = useMemo(() => {
    const duplicateOf = possibleDuplicates(rows.map(paymentRowOf))
    return rows.map((row, i): Issue[] => {
      const of = duplicateOf[i]
      return of === null ? row.issues : [...row.issues, {
        code: 'possible_duplicate', severity: 'warning', related: [rows[of].id],
        message: `Same amount as "${rows[of].description}" (${uploads.get(rows[of].upload_id) ?? 'another upload'}), `
                 + `within ${DUPLICATE_WINDOW_DAYS} days.`,
      }]
    })
  }, [rows, uploads])
  // Rows left out of the books are listed, crossed out, but are never counted or filtered as rows to review.
  const review = useMemo(() => reviewSummary(issuesOf.filter((_, i) => !leftOut(rows[i]))), [rows, issuesOf])
  const flagged = rows.filter((row, i) => needsReview(row, issuesOf[i])).length
  const totals = useMemo(() => totalsOf(rows), [rows])
  const all = rows.map((row, i) => ({ row, issues: issuesOf[i] }))
  const shown = all.filter(({ row, issues }) =>
    only === null || (only === 'review' ? needsReview(row, issues) : !leftOut(row) && issues.some(i => i.code === only)))
  const filter = only !== null && !shown.length ? null : only   // a filter whose rows are all fixed shows everything
  const together = focus ? all.filter(({ row }) => focus.includes(row.id)) : []
  const listed = together.length ? together : filter === null ? all : shown
  const showBoth = (row: SavedRow, related: number[]) => {
    setFocus([row.id, ...related])
    card.current?.scrollIntoView({ block: 'start', behavior: 'smooth' })
  }

  return (
    <section ref={card} className="card section table-card">
      <div className="section-head">
        <h2>Transactions</h2>
        <span className="muted small">{transactionCount(rows.length)}</span>
        {!readOnly && onAdd && (
          <button type="button" className="btn btn-small" onClick={() => onAdd(null)}>Add transaction</button>
        )}
      </div>
      {together.length > 0 ? (
        <div className="review-bar" role="group" aria-label="Rows shown together">
          <span className="review-count">{transactionCount(together.length)} side by side</span>
          <button type="button" className="chip chip-on" onClick={() => setFocus(null)}>Show all</button>
        </div>
      ) : flagged > 0 && (
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
            {listed.map(({ row, issues }) => {
              const left = leftOut(row)
              const check = checkOf(issues, left)
              const origin = `${uploads.get(row.upload_id) ?? ''} · ${DOCUMENT_TYPES[row.document_type ?? 'receipt'] ?? row.document_type}`
              const classes = [left || kindOf(row) === 'not booked' ? 'row-not-booked' : '',
                               check.cls === 'check-error' ? 'row-flag-error' : check.cls === 'check-warn' ? 'row-flag-warn' : '']
              const touched = (row.decision_ids ?? []).flatMap(id => decisions.get(id) ?? [])
              return (
                <tr key={row.id} className={classes.filter(Boolean).join(' ') || undefined}>
                  <td className="mono">{row.date ?? '—'}</td>
                  <td>
                    <span style={left ? { textDecoration: 'line-through' } : undefined}>{row.description}</span>
                    {!!row.decision_ids?.length && (touched.length ? <AgentBadge decisions={touched} />
                      : <span className="badge badge-accent">Agent</span>)}
                    {left && <div className="row-status">{row.superseded_by ? 'Replaced' : 'Left out'}
                      {whyLeftOut(row) && `: ${whyLeftOut(row)}`}</div>}
                    {held.has(row.id) && <div className="row-status"><span className="badge badge-warn">Documentation required</span></div>}
                    {row.edited && <span className="badge badge-muted edited" title={originalText(row)}>Edited</span>}
                    <div className="row-origin" title={origin}>{origin}</div>
                    {[...rowStatus(row, id => byId.get(id)), ...[apartStatus(row, id => described.get(id))].filter(s => s !== null)]
                      .map((status, k) => (status.include !== undefined ? (
                      <label key={k} className="row-status">
                        <input type="checkbox" checked={status.include} disabled={readOnly}
                               onChange={e => onChange(row.id, { include: e.target.checked })} />
                        {status.text}
                      </label>
                    ) : (
                      <div key={k} className="row-status">
                        {status.text}
                        {!readOnly && status.actions.map((action, j) => (
                          <button key={j} type="button" className="link-btn"
                                  onClick={() => onChange(action.row ?? row.id, action.change)}>
                            {action.label}
                          </button>
                        ))}
                      </div>
                    )))}
                    <IssueList issues={issues} onShowBoth={related => showBoth(row, related)}
                               onNotSame={readOnly ? undefined : related => onChange(row.id, apartChange(row, related))} />
                  </td>
                  <td>
                    <span className={row.direction === 'in' ? 'badge badge-accent' : 'badge badge-muted'}>
                      {row.direction === 'in' ? 'Money in' : 'Money out'}
                    </span>
                  </td>
                  <td className="num">{money(row.gross)}</td>
                  <td className="num">{money(row.vat_posted)}</td>
                  <td className="num">{money(row.net)}</td>
                  <td>{accountName(row)}</td>
                  <td className={`check ${check.cls}`} title={check.title}>{check.mark}</td>
                  <td className="row-actions">
                    {!readOnly && onEdit && <button type="button" className="link-btn" onClick={() => onEdit(row)}>Edit</button>}
                    {!readOnly && onAdd && takesLines(row) && (
                      <button type="button" className="link-btn" onClick={() => onAdd(row)}>Add line</button>
                    )}
                    {!readOnly && row.edited && (
                      <button type="button" className="link-btn"
                              onClick={() => onChange(row.id, { revert: true })}>Revert</button>
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

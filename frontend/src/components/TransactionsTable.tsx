'use client'

import Link from 'next/link'
import { useEffect, useMemo, useRef, useState } from 'react'
import { clientDecisions, postedTrialBalance, type Issue } from '@/lib/api'
import { agentDecisions, changedFields, type Decision } from '@/lib/chat'
import { useClientYear } from '@/components/ClientYearContext'
import { listAccounts, type AccountChoice, type Ledger, type RowChange, type SavedRow } from '@/lib/clients'
import { accountList, DOCUMENT_TYPES, transactionCount } from '@/lib/clientRules'
import { sectionHref } from '@/lib/years'
import { acceptLabel, askAbout, claimLineOf, filtersSet, issueFixes, matchesFilters, netBooked, NO_FILTERS, vatBooked, type IssueFix, type RowFilters } from '@/lib/ledger'
import { aboutAnotherRow, acceptChange, accountName, apartChange, apartStatus, canBeAccepted, canBeApart, checkHover, documentProblems, issueHeading, kindOf, leftOut, leftOutLabel,
         longDate, money, needsReview, reviewSummary, rowIssues, rowStatus, showLabel, takesLines, totalsOf } from '@/lib/ledger'

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

// A row's VAT treatment, when a person chose one, under its VAT.
const VAT_SHORT: Record<string, string> = { standard: '20%', reduced: '5%', zero: 'Zero-rated', exempt: 'Exempt',
                                            outside_scope: 'Outside scope' }

const FIELD_NAMES: Record<string, string> = {
  date: 'date', description: 'description', counterparty: 'counterparty', direction: 'in/out', gross: 'amount',
  vat: 'VAT', account_code: 'account', document_type: 'type', currency: 'currency',
  vat_treatment: 'VAT treatment',
}

// The hover on an edited row: what the model read, where it differs from now.
function originalText(row: SavedRow): string {
  const now = row as unknown as Record<string, string | null | undefined>
  const changed = Object.entries(row.original ?? {})
    .filter(([field, value]) => (value ?? '') !== (now[field] ?? ''))
    .map(([field, value]) => `${FIELD_NAMES[field] ?? field} ${field === 'gross' || field === 'vat' ? money(value) : value || 'none'}`)
  return changed.length ? `Read by the model: ${changed.join(', ')}` : 'Edited'
}

// What is wrong with a row, in words, under its description: errors first, then warnings, then notes. Each warning
// or error carries its own way out: the fix where there is one (a change made at once, the accounts to choose from,
// Edit, or Documents for a missing file), Show both and Not the same for two records that may be one, It's fine for
// a matter of judgement, and Ask the AI, which opens a chat on the issue.
function IssueList({ issues, onShowBoth, onNotSame, onAccept, fixesOf, onFix, onEdit, onAddLine, accounts, documentsHref, askHref }: {
  issues: Issue[]
  onShowBoth?: (related: number[]) => void   // each issue shows the rows it is about
  onNotSame?: (related: number[]) => void
  onAccept?: (code: string) => void          // a warning that is a matter of judgement: the person says it is fine
  fixesOf?: (issue: Issue) => IssueFix[]     // none when the row can't be changed
  onFix?: (change: RowChange) => void
  onEdit?: () => void
  onAddLine?: () => void                     // a document's problem: add the line its total is short of
  accounts?: AccountChoice[]                 // the accounts a row of the client can be coded to
  documentsHref?: string
  askHref?: (issue: Issue) => string         // the chat, asked about the issue
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
            <span className="issue-actions">
            {onShowBoth && aboutAnotherRow(issue.code) && !!issue.related?.length && (
              <button type="button" className="link-btn" onClick={() => onShowBoth(issue.related!)}>
                {showLabel(issue.code)}
              </button>
            )}
            {onNotSame && canBeApart(issue.code) && !!issue.related?.length && (
              <button type="button" className="link-btn" onClick={() => onNotSame(issue.related!)}>Not the same</button>
            )}
            {onAddLine && issue.severity !== 'info' && (
              <button type="button" className="link-btn" onClick={onAddLine}>Add line</button>
            )}
            {(fixesOf?.(issue) ?? []).map((fix, j) => (
              fix.change && onFix ? (
                <button key={j} type="button" className="link-btn" onClick={() => onFix(fix.change!)}>{fix.label}</button>
              ) : fix.choose === 'account' && onFix ? (
                <select key={j} className="input issue-choice" value="" aria-label={fix.label}
                        onChange={e => { if (e.target.value) onFix({ account_code: e.target.value }) }}>
                  <option value="">{accounts?.length ? `${fix.label}…` : 'Loading the accounts…'}</option>
                  {(accounts ?? []).map(a => <option key={a.code} value={a.code}>{a.name}</option>)}
                </select>
              ) : fix.edit && onEdit ? (
                <button key={j} type="button" className="link-btn" onClick={onEdit}>{fix.label}</button>
              ) : fix.documents && documentsHref ? (
                <Link key={j} className="link-btn" href={documentsHref}>{fix.label}</Link>
              ) : null
            ))}
            {onAccept && issue.severity === 'warning' && canBeAccepted(issue.code) && (
              <button type="button" className="link-btn" onClick={() => onAccept(issue.code)}>{acceptLabel(issue.code)}</button>
            )}
            {askHref && issue.severity !== 'info' && (
              <Link className="link-btn" href={askHref(issue)}>Ask the AI</Link>
            )}
            </span>
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
export default function TransactionsTable({ ledger, readOnly, onChange, onEdit, onAdd, onRemove, show, saving }: {
  ledger: Ledger
  show?: number[]                               // rows to open on, side by side (a link from the problems list)
  saving?: Set<number>                          // rows whose change is being saved: dimmed until it is
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
  // The accounts a row can be coded to, for an issue that is fixed by choosing one; a bank account or card by the
  // name the client gives it. Read once, and only when a row asks for them.
  const [accounts, setAccounts] = useState<AccountChoice[]>([])
  const wantsAccounts = !readOnly && rows.some(r => r.issues.some(i => issueFixes(r, i).some(f => f.choose === 'account')))
  useEffect(() => {
    if (!wantsAccounts || accounts.length) return
    let current = true
    listAccounts(client.id).then(found => {
      if (!current) return
      const named = found.map(a => ({ ...a, name: client.bank_accounts.find(b => b.code === a.code)?.name || a.name }))
      setAccounts(accountList(named, { code: '', name: '' }, ''))
    }).catch(() => {})
    return () => { current = false }
  }, [wantsAccounts, accounts.length, client.id, client.bank_accounts])
  // The held row being booked without its document, and the reason typed for it; null when none is.
  const [without, setWithout] = useState<{ row: number; reason: string } | null>(null)
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
  const [focus, setFocus] = useState<number[] | null>(show?.length ? show : null)   // rows shown side by side (Show both), or none
  const [byDate, setByDate] = useState(true)
  const card = useRef<HTMLElement>(null)
  const uploads = useMemo(() => new Map(ledger.uploads.map(u => [u.id, u.name])), [ledger.uploads])
  const byId = useMemo(() => new Map(rows.map(r => [r.id, r])), [rows])
  const described = useMemo(() => new Map(rows.map(r => [r.id, r.description])), [rows])
  // Each row's own issues, a possible duplicate among them (worked out with the books, so the AI sees it too). A
  // problem with a whole document is not its first line's: it is listed once, above the table.
  const issuesOf = useMemo(() => rows.map(rowIssues), [rows])
  const docs = useMemo(() => documentProblems(rows), [rows])
  // Rows left out of the books are listed, crossed out, but are never counted or filtered as rows to review.
  const review = useMemo(() => reviewSummary([...issuesOf.filter((_, i) => !leftOut(rows[i])), ...docs.map(d => [d.issue])]),
                         [rows, issuesOf, docs])
  const flagged = rows.filter((row, i) => needsReview(row, issuesOf[i])).length + docs.length
  const all = rows.map((row, i) => ({ row, issues: issuesOf[i] }))
  // The filters narrow the table by several fields at once; the review chips then pick among the rows left.
  const [filters, setFilters] = useState<RowFilters>(NO_FILTERS)
  const setFilter = (field: keyof RowFilters, value: string) => setFilters(f => ({ ...f, [field]: value }))
  const narrowed = filtersSet(filters) > 0
  const passing = narrowed ? all.filter(({ row }) => matchesFilters(row, filters, uploads.get(row.upload_id))) : all
  const accountChoices = useMemo(() => [...new Map(rows.filter(r => r.account_code).map(r =>
    [r.account_code, r.account_name || r.account_code])).entries()].sort((a, b) => a[1].localeCompare(b[1], 'en-GB')), [rows])
  const typeChoices = useMemo(() => [...new Set(rows.map(r => r.document_type ?? 'receipt'))], [rows])
  // A chip also shows every line of a document with that problem ("Only these": with any problem).
  const ofDocs = new Set(docs.filter(d => only === 'review' || d.issue.code === only).flatMap(d => d.lines))
  const shown = passing.filter(({ row, issues }) => only === null || ofDocs.has(row.id)
    || (only === 'review' ? needsReview(row, issues) : !leftOut(row) && issues.some(i => i.code === only)))
  const filter = only !== null && !shown.length ? null : only   // a filter whose rows are all fixed shows everything
  const together = focus ? all.filter(({ row }) => focus.includes(row.id)) : []
  const picked = together.length ? together : filter === null ? passing : shown
  // The totals are of the whole year, or, while the table is narrowed, of the rows it shows.
  const totals = totalsOf(narrowed && !together.length ? picked.map(p => p.row) : rows)
  // By date, oldest first, undated rows first so they are seen; or as the files were read, each document's rows
  // together. Rows of one day keep the order they were read in.
  const listed = byDate
    ? picked.map((item, n) => ({ item, n })).sort((a, b) =>
        (a.item.row.date ?? '').localeCompare(b.item.row.date ?? '') || a.n - b.n).map(({ item }) => item)
    : picked
  const showTogether = (ids: number[]) => {
    setFocus(ids)
    card.current?.scrollIntoView({ block: 'start', behavior: 'smooth' })
  }
  const showBoth = (row: SavedRow, related: number[]) => showTogether([row.id, ...related])

  return (
    <section ref={card} className="card section table-card">
      <div className="section-head">
        <h2>Transactions</h2>
        <span className="muted small">{transactionCount(rows.length)}</span>
        <button type="button" className="btn btn-small" onClick={() => setByDate(v => !v)}
                title={byDate ? 'Listed by date, oldest first' : 'Listed as the files were read'}>
          {byDate ? 'Order: by date' : 'Order: as read'}
        </button>
        {!readOnly && onAdd && (
          <button type="button" className="btn btn-small" onClick={() => onAdd(null)}>Add transaction</button>
        )}
      </div>
      {rows.length > 0 && !together.length && (
        <div className="filter-bar" role="search" aria-label="Filter transactions">
          <input className="input filter-text" type="search" value={filters.text} aria-label="Search"
                 placeholder="Search description, supplier, file or account" onChange={e => setFilter('text', e.target.value)} />
          <select className="input" value={filters.account} aria-label="Account" onChange={e => setFilter('account', e.target.value)}>
            <option value="">All accounts</option>
            {accountChoices.map(([code, name]) => <option key={code} value={code}>{name}</option>)}
          </select>
          <select className="input" value={filters.type} aria-label="Document type" onChange={e => setFilter('type', e.target.value)}>
            <option value="">All types</option>
            {typeChoices.map(type => <option key={type} value={type}>{DOCUMENT_TYPES[type] ?? type}</option>)}
          </select>
          <select className="input" value={filters.direction} aria-label="Money in or out" onChange={e => setFilter('direction', e.target.value)}>
            <option value="">In and out</option>
            <option value="out">Money out</option>
            <option value="in">Money in</option>
          </select>
          <select className="input" value={filters.status} aria-label="State" onChange={e => setFilter('status', e.target.value)}>
            <option value="">Any state</option>
            <option value="review">To review</option>
            <option value="edited">Edited</option>
            <option value="vat">With VAT</option>
            <option value="no_vat">No VAT</option>
            <option value="not_booked">Not booked</option>
          </select>
          <label className="filter-range">From
            <input className="input" type="date" value={filters.from} onChange={e => setFilter('from', e.target.value)} />
          </label>
          <label className="filter-range">to
            <input className="input" type="date" value={filters.to} onChange={e => setFilter('to', e.target.value)} />
          </label>
          <label className="filter-range">£ from
            <input className="input filter-amount" inputMode="decimal" value={filters.min} placeholder="0.00"
                   onChange={e => setFilter('min', e.target.value)} />
          </label>
          <label className="filter-range">to
            <input className="input filter-amount" inputMode="decimal" value={filters.max} placeholder="any"
                   onChange={e => setFilter('max', e.target.value)} />
          </label>
          {narrowed && (
            <>
              <span className="muted small">{passing.length} of {transactionCount(rows.length)} shown</span>
              <button type="button" className="btn btn-small" onClick={() => setFilters(NO_FILTERS)}>Clear filters</button>
            </>
          )}
        </div>
      )}
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
      {/* A problem with a whole document, said once against the document: its lines don't reach its total and no
          one line explains why. Not narrowed by the filters, so it is never hidden with its first line. */}
      {docs.length > 0 && (
        <div className="document-problems" role="group" aria-label="Documents to check">
          {docs.map(({ row, issue, lines }) => (
            <div key={`${row.id}:${issue.code}`} className="document-problem">
              <div className="document-name">
                {uploads.get(row.upload_id) ?? 'Document'} · {DOCUMENT_TYPES[row.document_type ?? 'receipt'] ?? row.document_type}
              </div>
              <IssueList issues={[issue]} onShowBoth={() => showTogether(lines)}
                         onAddLine={!readOnly && onAdd && takesLines(row) ? () => onAdd(row) : undefined}
                         fixesOf={readOnly ? undefined : found => issueFixes(row, found)}
                         onFix={change => onChange(row.id, change)}
                         askHref={readOnly ? undefined
                           : found => `/chat?client=${client.id}&ask=${encodeURIComponent(askAbout(row, found))}`} />
            </div>
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
              const touched = row.decision_ids?.length ? agentDecisions(row.decision_ids, decisions) : []
              return (
                <tr key={row.id} className={classes.filter(Boolean).join(' ') || undefined}
                    aria-busy={saving?.has(row.id) || undefined} style={saving?.has(row.id) ? { opacity: 0.5 } : undefined}>
                  <td className="mono">{row.date ? longDate(row.date) : '—'}</td>
                  <td>
                    <span style={left ? { textDecoration: 'line-through' } : undefined}>{row.description}</span>
                    {touched === null ? <span className="badge badge-accent">Agent</span>
                      : touched.length > 0 && <AgentBadge decisions={touched} />}
                    {left && <div className="row-status">{leftOutLabel(row)}
                      {!row.removed && whyLeftOut(row) && `: ${whyLeftOut(row)}`}
                      {row.removed && !readOnly && (
                        <button type="button" className="link-btn" title="Book it again"
                                onClick={() => onChange(row.id, { removed: false })}>Put back</button>
                      )}</div>}
                    {held.has(row.id) && !row.issues.some(i => i.code === 'booked_without_document') && (
                      <div className="row-status">
                        <span className="badge badge-warn">Documentation required</span>
                        {!readOnly && without?.row !== row.id && (
                          <button type="button" className="link-btn" title="Book it to its account with no VAT reclaimed"
                                  onClick={() => setWithout({ row: row.id, reason: '' })}>Book without a receipt</button>
                        )}
                      </div>
                    )}
                    {without?.row === row.id && (
                      <form className="row-status without-form" onSubmit={e => {
                        e.preventDefault()
                        if (!without.reason.trim()) return
                        onChange(row.id, { without_document: without.reason.trim() })
                        setWithout(null)
                      }}>
                        <input className="input" value={without.reason} maxLength={300} autoFocus
                               aria-label="Why there is no receipt or invoice" placeholder="Why there is none, e.g. receipt lost"
                               onChange={e => setWithout({ row: row.id, reason: e.target.value })} />
                        <button type="submit" className="btn btn-primary btn-small" disabled={!without.reason.trim()}>
                          Book with no VAT
                        </button>
                        <button type="button" className="btn btn-small" onClick={() => setWithout(null)}>Cancel</button>
                      </form>
                    )}
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
                    <IssueList issues={issues.filter(i => i.code !== 'booked_without_document')} onShowBoth={related => showBoth(row, related)}
                               onNotSame={readOnly ? undefined : related => onChange(row.id, apartChange(row, related))}
                               onAccept={readOnly ? undefined : code => onChange(row.id, acceptChange(row, code))}
                               fixesOf={readOnly || left ? undefined : issue => issueFixes(row, issue, claimLineOf(row, rows))}
                               onFix={change => onChange(row.id, change)} onEdit={onEdit ? () => onEdit(row) : undefined}
                               accounts={accounts} documentsHref={sectionHref(client.id, year.start, 'documents')}
                               askHref={readOnly || left ? undefined
                                 : issue => `/chat?client=${client.id}&ask=${encodeURIComponent(askAbout(row, issue))}`} />
                  </td>
                  <td>
                    <span className={row.direction === 'in' ? 'badge badge-accent' : 'badge badge-muted'}>
                      {row.document_type === 'journal' ? 'Journal' : row.direction === 'in' ? 'Money in' : 'Money out'}
                    </span>
                  </td>
                  <td className="num">{money(row.gross)}</td>
                  <td className="num">
                    {money(vatBooked(row))}
                    {row.vat_treatment && <div className="muted small">{VAT_SHORT[row.vat_treatment] ?? row.vat_treatment}</div>}
                    {row.vat_not_reclaimed && (
                      <div className="muted small vat-not-reclaimed" title={row.vat_not_reclaimed_why ?? undefined}>
                        {money(row.vat_not_reclaimed)} not reclaimed
                        {!readOnly && !left && (
                          <button type="button" className="link-btn" title="Reclaim this VAT on your say: you hold the VAT invoice"
                                  onClick={() => onChange(row.id, { vat_confirmed: true })}>Reclaim it</button>
                        )}
                      </div>
                    )}
                  </td>
                  <td className="num">{money(netBooked(row))}</td>
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
                    {!readOnly && onRemove && !row.removed && (
                      <button type="button" className="link-btn danger" onClick={() => onRemove(row)}>Remove</button>
                    )}
                  </td>
                </tr>
              )
            })}
          </tbody>
          <tfoot>
            {narrowed && !together.length && (
              <tr><td colSpan={9} className="muted small">
                {listed.length ? `Totals of the ${transactionCount(listed.length)} shown` : 'No transactions match these filters.'}
              </td></tr>
            )}
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

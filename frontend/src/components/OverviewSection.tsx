'use client'

import Link from 'next/link'
import { useEffect, useState } from 'react'
import ClientDialog from '@/components/ClientDialog'
import { useClientYear } from '@/components/ClientYearContext'
import { attentionHref, clientQuestions } from '@/lib/chat'
import { postedTrialBalance } from '@/lib/api'
import { changeClient, getSummary, type ClientFields, type YearSummary } from '@/lib/clients'
import { BUSINESS_TYPES, transactionCount } from '@/lib/clientRules'
import { money, postedLedgerNote } from '@/lib/ledger'
import { bankCheckText, fullDate } from '@/lib/sections'
import { newestFirst, listReviews, overviewReviewLine, unseenChanges } from '@/lib/reviews'
import { sectionHref } from '@/lib/years'

const message = (e: unknown) => (e instanceof Error ? e.message : String(e))

// A client's details, and how the year stands: whether it is open, what needs review or is unpaid, how the bank
// agrees with the statements, and whether the trial balance balances.
export default function OverviewSection() {
  const { client, year, ledger, reload } = useClientYear()
  const [summary, setSummary] = useState<YearSummary | null>(null)
  const [error, setError] = useState('')
  const [editing, setEditing] = useState(false)
  const [needYou, setNeedYou] = useState(0)             // open and waiting questions, from every chat on the client
  const [reviewLine, setReviewLine] = useState<string | null>(null)   // the AI review's last run; null when it has had none
  const [unposted, setUnposted] = useState<number | null>(null)   // changes not in the posted ledger; null until known

  // What the accountant chat has to say about the client; a failure just leaves these out.
  useEffect(() => {
    let current = true
    clientQuestions(client.id).then(qs => { if (current) setNeedYou(qs.filter(q => q.status !== 'resolved').length) }).catch(() => {})
    listReviews(client.id, year.start).then(runs => {
      if (!current) return
      const done = newestFirst(runs).filter(r => r.status === 'done')
      setReviewLine(overviewReviewLine(done[0]?.started_at ?? null, unseenChanges(runs)))
    }).catch(() => { if (current) setReviewLine(null) })
    postedTrialBalance(client.id, year.start)
      .then(tb => { if (current) setUnposted(tb.unposted_changes ?? 0) }).catch(() => { if (current) setUnposted(null) })
    return () => { current = false }
  }, [client.id, year.start, ledger])

  // The summary is worked out from the year's rows, so it is fetched again whenever they are loaded again.
  useEffect(() => {
    let current = true
    getSummary(client.id, year.start)
      .then(found => { if (current) { setSummary(found); setError('') } })
      .catch(e => { if (current) { setSummary(null); setError(message(e)) } })
    return () => { current = false }
  }, [client.id, year.start, ledger])

  const saveDetails = async (fields: ClientFields) => {
    await changeClient(client.id, fields)
    setEditing(false)
    await reload()
  }
  const link = (slug: 'transactions' | 'invoices' | 'bank' | 'trial-balance', text: string) => (
    <Link href={sectionHref(client.id, year.start, slug)} className="link-btn">{text}</Link>
  )
  const details: [string, string][] = [
    ['Business type', BUSINESS_TYPES[client.business_type]],
    ['Responsible person', client.contact_name || '—'],
    ['Email', client.contact_email || '—'],
    ['Phone', client.contact_phone || '—'],
    ['VAT', client.vat_registered ? 'Registered' : 'Not registered'],
    ['First year end', fullDate(client.first_year_end)],
    ['Bank accounts', client.bank_accounts.map(a => a.name).join(', ') || '—'],
    ['Folder', client.folder || '—'],
  ]

  const ledgerNote = postedLedgerNote(unposted)

  return (
    <>
      <section className="card section">
        <div className="section-head">
          <h2>Client details</h2>
          {!client.archived && <button type="button" className="btn btn-small" onClick={() => setEditing(true)}>Edit details</button>}
        </div>
        <dl className="details">
          {details.map(([label, value]) => (
            <div key={label}>
              <dt>{label}</dt>
              <dd>{value}</dd>
            </div>
          ))}
        </dl>
      </section>

      <section className="card section">
        <div className="section-head">
          <h2>{year.label}</h2>
          <span className={`badge ${year.closed ? 'badge-muted' : 'badge-accent'}`}>{year.closed ? 'Closed' : 'Open'}</span>
        </div>
        {year.closed && year.changed_since_closed && (
          <div className="notice notice-warn">
            <span>This closed year’s figures no longer match what was saved when it was closed.</span>
          </div>
        )}
        {error && <div className="notice notice-error">{error}</div>}
        {!summary && !error && <p className="muted">Loading the summary…</p>}
        {needYou > 0 && (
          <p><Link href={attentionHref(client.id)} className="link-btn">{needYou} {needYou === 1 ? 'thing needs' : 'things need'} you</Link></p>
        )}
        {reviewLine && <p><Link href={sectionHref(client.id, year.start, 'ai-review')} className="link-btn">{reviewLine}</Link></p>}
        {summary?.missing_paperwork?.length ? (
          <div className="notice notice-warn">
            <strong>Missing paperwork</strong>
            <ul className="small">{summary.missing_paperwork.map(m => <li key={m}>{m}</li>)}</ul>
          </div>
        ) : null}
        {summary?.pointers?.map(p => <p key={p} className="muted small">{p}</p>)}
        {ledgerNote && <p className="muted small">{ledgerNote}</p>}
        {summary && (
          <dl className="details">
            <div>
              <dt>Rows to review</dt>
              <dd>
                {summary.to_review
                  ? <>{summary.to_review} of {transactionCount(summary.rows)} {link('transactions', 'Review')}</>
                  : <>None of {transactionCount(summary.rows)}</>}
              </dd>
            </div>
            <div>
              <dt>Unpaid sales</dt>
              <dd>{money(summary.unpaid_sales)} {link('invoices', 'Invoices')}</dd>
            </div>
            <div>
              <dt>Unpaid purchases</dt>
              <dd>{money(summary.unpaid_purchases)} {link('invoices', 'Invoices')}</dd>
            </div>
            {summary.bank.map(check => (
              <div key={check.code}>
                <dt>{check.name}</dt>
                <dd>{bankCheckText(check)} {link('bank', 'Bank')}</dd>
              </div>
            ))}
            <div>
              <dt>Trial balance</dt>
              <dd>
                {summary.trial_balance === 'balanced'
                  ? <span className="badge badge-accent">Balances</span>
                  : <span className="badge badge-warn">Blocked</span>}{' '}
                {link('trial-balance', 'Trial balance')}
                {summary.problems.map(p => <div key={p} className="text-warn small">{p}</div>)}
              </dd>
            </div>
          </dl>
        )}
      </section>
      {editing && <ClientDialog client={client} onCancel={() => setEditing(false)} onSave={saveDetails} />}
    </>
  )
}

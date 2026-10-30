'use client'

import { useEffect, useState } from 'react'
import { useClientYear } from '@/components/ClientYearContext'
import { getInvoices, type InvoiceSummary } from '@/lib/clients'
import { money } from '@/lib/ledger'
import { fullDate, splitInvoices } from '@/lib/sections'

const message = (e: unknown) => (e instanceof Error ? e.message : String(e))

function InvoiceTable({ title, who, invoices }: { title: string; who: string; invoices: InvoiceSummary[] }) {
  return (
    <section className="card section table-card">
      <div className="section-head">
        <h2>{title}</h2>
        <span className="muted small">{invoices.length} invoice{invoices.length === 1 ? '' : 's'}</span>
      </div>
      {!invoices.length ? <p className="muted">No {title.toLowerCase()} invoices in this year.</p> : (
        <table className="table">
          <thead>
            <tr>
              <th>{who}</th>
              <th>Number</th>
              <th>Date</th>
              <th className="num">Total</th>
              <th className="num">Paid</th>
              <th className="num">Owed</th>
              <th>Paid on</th>
            </tr>
          </thead>
          <tbody>
            {invoices.map(invoice => (
              <tr key={invoice.ref}>
                <td>{invoice.counterparty ?? 'Unknown'}</td>
                <td className="mono">{invoice.number ?? '—'}</td>
                <td>
                  {fullDate(invoice.date)}
                  {invoice.brought_forward && <div><span className="badge badge-warn">Brought forward</span></div>}
                </td>
                <td className="num">{money(invoice.total)}</td>
                <td className="num">{money(invoice.paid)}</td>
                <td className={`num${Number(invoice.owed) > 0 ? ' owed' : ''}`}>{money(invoice.owed)}</td>
                <td>
                  {invoice.paid_by.length
                    ? invoice.paid_by.map((p, k) => <div key={k}>{fullDate(p.date)} <span className="muted">· {money(p.amount)}</span></div>)
                    : <span className="muted">—</span>}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  )
}

// A client's sales and purchase invoices of the year, with what has been paid and what is still owed.
export default function InvoicesSection() {
  const { client, year, ledger } = useClientYear()
  const [invoices, setInvoices] = useState<InvoiceSummary[] | null>(null)
  const [error, setError] = useState('')

  // What is owed depends on the year's rows, so the list is fetched again whenever they are loaded again.
  useEffect(() => {
    let current = true
    getInvoices(client.id, year.start)
      .then(found => { if (current) { setInvoices(found); setError('') } })
      .catch(e => { if (current) setError(message(e)) })
    return () => { current = false }
  }, [client.id, year.start, ledger])

  if (error) return <div className="notice notice-error">{error}</div>
  if (!invoices) return <p className="muted">Loading invoices…</p>
  const { sales, purchases } = splitInvoices(invoices)
  return (
    <>
      <InvoiceTable title="Sales" who="Customer" invoices={sales} />
      <InvoiceTable title="Purchases" who="Supplier" invoices={purchases} />
    </>
  )
}

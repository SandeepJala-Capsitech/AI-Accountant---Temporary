'use client'

import Link from 'next/link'
import { useEffect, useState } from 'react'
import { useClientYear } from '@/components/ClientYearContext'
import { getBankChecks, type BankCheck } from '@/lib/clients'
import { transactionCount } from '@/lib/clientRules'
import { accountName, money } from '@/lib/ledger'
import { bankCheckNotice, bankCheckText, defaultBank, fullDate, linesFor } from '@/lib/sections'
import { sectionHref } from '@/lib/years'

const message = (e: unknown) => (e instanceof Error ? e.message : String(e))

// A tab for each of the client's bank accounts and cards: its check against the statements of the year, then its
// lines. The lines are read-only here: they are edited in Transactions.
export default function BankSection() {
  const { client, year, ledger } = useClientYear()
  const [picked, setPicked] = useState('')
  const [checks, setChecks] = useState<BankCheck[]>([])
  const [notice, setNotice] = useState('')

  // The check is worked out from the year's rows, so it is fetched again whenever they are loaded again.
  useEffect(() => {
    let current = true
    getBankChecks(client.id, year.start)
      .then(found => { if (current) { setChecks(found); setNotice('') } })
      .catch(e => { if (current) { setChecks([]); setNotice(bankCheckNotice(message(e))) } })
    return () => { current = false }
  }, [client.id, year.start, ledger])

  const accounts = client.bank_accounts
  const account = accounts.find(a => a.code === picked) ?? accounts[0]
  if (!account) {
    return (
      <section className="card section">
        <h2>Bank</h2>
        <p className="muted">This client has no bank accounts yet. Add one on the client's details.</p>
      </section>
    )
  }
  const check = checks.find(c => c.code === account.code)
  const lines = linesFor(ledger, account.code)
  const fallback = defaultBank(client)
  const nameOf = (code: string | null | undefined) => accounts.find(a => a.code === (code ?? fallback))?.name ?? 'another account'

  return (
    <>
      {notice && <div className="notice notice-error">{notice}</div>}
      <section className="card section table-card">
        <div className="section-head">
          <h2>Bank</h2>
          <span className="muted small">{transactionCount(lines.length)} on {account.name}</span>
          <Link href={sectionHref(client.id, year.start, 'transactions')} className="link-btn">Edit in Transactions</Link>
        </div>
        <div className="tabs" role="tablist">
          {accounts.map(a => (
            <button key={a.code} type="button" role="tab" className="tab" aria-selected={a.code === account.code}
                    onClick={() => setPicked(a.code)}>
              {a.name}
            </button>
          ))}
        </div>
        {check && (
          <div className={`notice ${Number(check.difference ?? 0) === 0 && check.statement !== null ? 'notice-info' : 'notice-warn'}`}>
            <span>
              {bankCheckText(check)}
              {check.statement !== null && (
                <span className="muted"> In the books {money(check.books)}; on the statement {money(check.statement)}.</span>
              )}
            </span>
          </div>
        )}
        {!lines.length ? <p className="muted">No lines on this account in this year.</p> : (
          <table className="table">
            <thead>
              <tr>
                <th>Date</th>
                <th>Description</th>
                <th className="num">Money in</th>
                <th className="num">Money out</th>
                <th>Account</th>
              </tr>
            </thead>
            <tbody>
              {lines.map(row => {
                const fromHere = (row.bank_account ?? fallback) === account.code
                // A line on another account's statement that is coded here is the other end of a transfer.
                const incoming = (row.direction === 'in') === fromHere
                return (
                  <tr key={row.id}>
                    <td className="mono">{fullDate(row.date)}</td>
                    <td>
                      {row.description}
                      {!fromHere && <div className="row-origin">Transfer from {nameOf(row.bank_account)}</div>}
                    </td>
                    <td className="num">{incoming ? money(row.gross) : ''}</td>
                    <td className="num">{incoming ? '' : money(row.gross)}</td>
                    <td>{fromHere ? accountName(row) : account.name}</td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        )}
      </section>
    </>
  )
}

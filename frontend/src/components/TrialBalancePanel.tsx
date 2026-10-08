'use client'

import { useEffect, useState } from 'react'
import type { TrialBalanceResult } from '@/lib/api'
import { clientTrialBalance } from '@/lib/clients'
import { money } from '@/lib/ledger'

// The client's trial balance from its saved rows. A change to the rows clears one already shown.
export default function TrialBalancePanel({ clientId, version, hasRows }: {
  clientId: number
  version: number
  hasRows: boolean
}) {
  const [result, setResult] = useState<TrialBalanceResult | null>(null)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  useEffect(() => { setResult(null); setError('') }, [version])

  const generate = async () => {
    setBusy(true)
    setError('')
    try {
      setResult(await clientTrialBalance(clientId))
    } catch (e) {
      setResult(null)
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  const apart = result ? Math.abs(Number(result.total_debits) - Number(result.total_credits)) : 0
  return (
    <section className="card section">
      <div className="section-head">
        <h2>Trial balance</h2>
        <button type="button" className="btn" onClick={generate} disabled={busy || !hasRows}>
          {busy ? <><span className="spinner" aria-hidden="true" /> Generating…</> : 'Generate trial balance'}
        </button>
      </div>
      {!result && !error && (
        <p className="muted small">Built from every saved row that is booked. Rows marked ✖ need fixing first.</p>
      )}
      {error && <div className="notice notice-error">{error}</div>}
      {result && (
        <>
          <table className="table">
            <thead><tr><th>Account</th><th className="num">Debit</th><th className="num">Credit</th></tr></thead>
            <tbody>
              {result.lines.map(line => (
                <tr key={line.code}>
                  <td><span className="mono">{line.code}</span> {line.name}</td>
                  <td className="num">{Number(line.debit) > 0 ? money(line.debit) : '—'}</td>
                  <td className="num">{Number(line.credit) > 0 ? money(line.credit) : '—'}</td>
                </tr>
              ))}
            </tbody>
            <tfoot>
              <tr>
                <td>Totals</td>
                <td className="num">{money(result.total_debits)}</td>
                <td className="num">{money(result.total_credits)}</td>
              </tr>
            </tfoot>
          </table>
          <span className={`badge tb-pill ${result.is_balanced ? 'badge-accent' : 'badge-warn'}`}>
            {result.is_balanced ? 'Debits equal credits' : `Out of balance by £${apart.toFixed(2)}`}
          </span>
        </>
      )}
    </section>
  )
}

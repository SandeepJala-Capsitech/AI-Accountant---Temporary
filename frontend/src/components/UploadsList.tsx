'use client'

import type { BankAccount, Upload } from '@/lib/clients'
import { dayMonth, statementText, transactionCount } from '@/lib/clientRules'

// What was added to the year, newest first, with each bank statement's account (changeable when the client has
// several) and balance check, and Remove.
export default function UploadsList({ uploads, bankAccounts, readOnly, onRemove, onBank }: {
  uploads: Upload[]
  bankAccounts: BankAccount[]
  readOnly: boolean
  onRemove: (upload: Upload) => void
  onBank: (upload: Upload, code: string) => void
}) {
  return (
    <section className="card section">
      <div className="section-head">
        <h2>Uploads</h2>
        <span className="muted small">{uploads.length}</span>
      </div>
      {!uploads.length ? <p className="muted small">Nothing added yet.</p> : (
        <ul className="upload-list">
          {uploads.map(upload => {
            const check = statementText(upload.statement)
            const status = upload.statement?.status
            const isStatement = !!upload.statement || upload.bank_account != null
            const account = upload.bank_account ?? bankAccounts[0]?.code ?? ''
            return (
              <li key={upload.id}>
                <div className="upload-name" title={upload.name}>{upload.name}</div>
                <div className="muted small">{transactionCount(upload.rows)} · {dayMonth(upload.created_at)}</div>
                {isStatement && bankAccounts.length > 1 && (readOnly ? (
                  <div className="muted small">On {bankAccounts.find(a => a.code === account)?.name ?? account}</div>
                ) : (
                  <label className="small upload-bank">
                    <span className="muted">On</span>
                    <select className="input" value={account} aria-label={`Bank account for ${upload.name}`}
                            onChange={e => onBank(upload, e.target.value)}>
                      {bankAccounts.map(a => <option key={a.code} value={a.code}>{a.name}</option>)}
                    </select>
                  </label>
                ))}
                {check && (
                  <div className={`small ${status === 'gap' ? 'text-warn' : status === 'ok' ? 'text-ok' : 'muted'}`}>{check}</div>
                )}
                {upload.warnings.length > 0 && (
                  <details className="small">
                    <summary>{upload.warnings.length} warning{upload.warnings.length === 1 ? '' : 's'}</summary>
                    {upload.warnings.map((warning, i) => <div key={i} className="muted">{warning}</div>)}
                  </details>
                )}
                {!readOnly && (
                  <button type="button" className="link-btn danger" onClick={() => onRemove(upload)}>Remove</button>
                )}
              </li>
            )
          })}
        </ul>
      )}
    </section>
  )
}

'use client'

import type { Upload } from '@/lib/clients'
import { dayMonth, statementText, transactionCount } from '@/lib/clientRules'

// What was added to the client, newest first, with each bank statement's balance check and Remove.
export default function UploadsList({ uploads, readOnly, onRemove }: {
  uploads: Upload[]
  readOnly: boolean
  onRemove: (upload: Upload) => void
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
            return (
              <li key={upload.id}>
                <div className="upload-name" title={upload.name}>{upload.name}</div>
                <div className="muted small">{transactionCount(upload.rows)} · {dayMonth(upload.created_at)}</div>
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

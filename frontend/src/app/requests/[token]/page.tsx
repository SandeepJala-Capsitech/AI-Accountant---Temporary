'use client'

import { useParams } from 'next/navigation'
import { useCallback, useEffect, useState } from 'react'
import { ApiError } from '@/lib/api'
import { allReceived, getRequests, uploadRequested, type RequestedDocument, type RequestedDocuments } from '@/lib/requests'

const UNKNOWN_LINK = "This link isn't valid. Ask your accountant for a new one."
const ALL_RECEIVED = 'Thank you. Everything we asked for has arrived.'
const ACCEPT = '.csv,.tsv,.txt,.xlsx,.xls,.pdf,.png,.jpg,.jpeg,.webp,.bmp,.tif,.tiff'

const message = (e: unknown) => (e instanceof Error ? e.message : String(e))

// The page a client opens from the email: each document the accountant asked for, with the reason, and a file
// picker for each one still to send. Nothing else from the job is shown. The link doesn't expire.
export default function RequestedDocumentsPage() {
  const { token } = useParams<{ token: string }>()
  const [page, setPage] = useState<RequestedDocuments | null>(null)
  const [unknown, setUnknown] = useState(false)
  const [error, setError] = useState('')
  const [sending, setSending] = useState<string | null>(null)   // the document whose file is being sent

  const load = useCallback(async () => {
    try {
      setPage(await getRequests(token))
    } catch (e) {
      if (e instanceof ApiError && e.code === 'not_found') setUnknown(true)
      else setError(message(e))
    }
  }, [token])

  useEffect(() => { load() }, [load])

  const send = async (doc: RequestedDocument, file: File | undefined) => {
    if (!file) return
    setSending(doc.id)
    setError('')
    try {
      await uploadRequested(token, doc.id, file)
    } catch (e) {
      setError(`${doc.document_type}: ${message(e)}`)
    } finally {
      setSending(null)
      await load()
    }
  }

  if (unknown) {
    return (
      <div className="empty card">
        <h1>Link not valid</h1>
        <p className="muted">{UNKNOWN_LINK}</p>
      </div>
    )
  }
  if (!page) return error ? <div className="notice notice-error" role="alert">{error}</div> : <p className="muted">Loading…</p>

  const done = allReceived(page.documents)
  return (
    <>
      <div className="page-head">
        <div>
          <h1>Documents for {page.client_name}</h1>
          {!done && (
            <p className="muted">Your accountant needs these to finish the accounts. Send each one below. You can come back to this page at any time.</p>
          )}
        </div>
      </div>
      {error && <div className="notice notice-error" role="alert">{error}</div>}
      <section className="card section">
        {done && <p className="text-ok" role="status">{ALL_RECEIVED}</p>}
        <ul className="upload-list">
          {page.documents.map(doc => (
            <li key={doc.id}>
              <div className="upload-name">{doc.document_type} <span className="muted">({doc.period})</span></div>
              <div className="muted small">{doc.reason}</div>
              {doc.status === 'received' ? (
                <div className="small text-ok">✓ Received</div>
              ) : sending === doc.id ? (
                <div className="small" role="status">Uploading…</div>
              ) : (
                <label className="small">
                  <span className="text-warn">Waiting for this document. </span>
                  <input type="file" accept={ACCEPT} disabled={sending !== null}
                         aria-label={`Choose the file for ${doc.document_type}`}
                         onChange={e => { const file = e.target.files?.[0]; e.target.value = ''; send(doc, file) }} />
                </label>
              )}
            </li>
          ))}
        </ul>
      </section>
    </>
  )
}

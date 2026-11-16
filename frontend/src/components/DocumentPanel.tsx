'use client'

import { useEffect } from 'react'
import { useClientYear } from '@/components/ClientYearContext'
import type { SavedRow, Upload } from '@/lib/clients'
import { longDate, money } from '@/lib/ledger'
import { canShow, fileHref, isPdf } from '@/lib/suggestions'

// The document a card is about, beside the list: what the AI quoted from it, then the file itself when a browser
// can show it (a PDF or an image), or else the lines that were read from it, with the card's own row marked.
export default function DocumentPanel({ upload, rows, rowId, quotes, onClose }: {
  upload: Upload
  rows: SavedRow[]        // the rows read from this upload
  rowId: number           // the card's row
  quotes: string[]        // what the card's suggestions quote from this file
  onClose: () => void
}) {
  const { client } = useClientYear()
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose() }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])
  const kept = !!upload.sha256
  const shown = kept && canShow(upload.name)
  return (
    <aside className="document-panel" aria-label={`Document: ${upload.name}`}>
      <div className="document-panel-head">
        <strong>{upload.name}</strong>
        {kept && <a className="link-btn" href={fileHref(client.id, upload.id)}>Download</a>}
        <button type="button" className="btn btn-small" onClick={onClose}>Close</button>
      </div>
      {quotes.map((quote, i) => <blockquote key={i} className="evidence"><p>“{quote}”</p></blockquote>)}
      {shown && isPdf(upload.name) && <iframe title={upload.name} src={fileHref(client.id, upload.id, true)} />}
      {shown && !isPdf(upload.name) && (
        // eslint-disable-next-line @next/next/no-img-element -- the client's own file, served by the API as it was sent
        <img alt={upload.name} src={fileHref(client.id, upload.id, true)} />
      )}
      {!shown && (
        <>
          <p className="muted small">
            {kept ? 'This kind of file can’t be shown here. These are the lines read from it.'
              : 'The original file wasn’t kept. These are the lines read from it.'}
          </p>
          <table className="table document-lines">
            <thead><tr><th>Date</th><th>Description</th><th className="num">Amount</th></tr></thead>
            <tbody>
              {rows.map(row => (
                <tr key={row.id} aria-current={row.id === rowId ? 'true' : undefined}>
                  <td>{row.date ? longDate(row.date) : 'No date'}</td>
                  <td>{row.description}</td>
                  <td className="num">{money(row.gross)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}
    </aside>
  )
}

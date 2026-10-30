'use client'

import { useState } from 'react'
import AddDocuments from '@/components/AddDocuments'
import { useClientYear } from '@/components/ClientYearContext'
import { useConfirm } from '@/components/ConfirmDialog'
import UploadsList from '@/components/UploadsList'
import { removeUpload, setUploadBank, type Ledger, type Upload } from '@/lib/clients'
import { CLOSED_YEAR_NOTICE, transactionCount } from '@/lib/clientRules'

const message = (e: unknown) => (e instanceof Error ? e.message : String(e))

// A client's year: adding documents (upload, paste, by hand, or from the client's folder) and the files it holds.
// A closed year or an archived client shows the files only.
export default function DocumentsSection() {
  const { client, year, ledger, reload } = useClientYear()
  const { ask, dialog: confirmDialog } = useConfirm()
  const [error, setError] = useState('')
  const readOnly = year.closed || client.archived

  // A change the API answers with the year's ledger: the year is loaded again to show it.
  const act = async (change: () => Promise<Ledger>) => {
    setError('')
    try { await change(); await reload() } catch (e) { setError(message(e)) }
  }
  const remove = async (upload: Upload) => {
    const question = `Remove ${upload.name} and its ${transactionCount(upload.rows)}? This can't be undone: analyse the file again to get them back.`
    if (await ask(question, 'Remove')) await act(() => removeUpload(client.id, upload.id))
  }

  return (
    <>
      <h2>Documents</h2>
      {year.closed && <div className="notice notice-warn">{CLOSED_YEAR_NOTICE}</div>}
      {error && <div className="notice notice-error">{error}</div>}
      {!readOnly && (
        <AddDocuments clientId={client.id} yearStart={year.start} bankAccounts={client.bank_accounts}
                      folder={client.folder} uploads={ledger.uploads} onSaved={reload} />
      )}
      <UploadsList uploads={ledger.uploads} bankAccounts={client.bank_accounts} readOnly={readOnly}
                   onRemove={remove} onBank={(upload, code) => act(() => setUploadBank(client.id, upload.id, code))} />
      {confirmDialog}
    </>
  )
}

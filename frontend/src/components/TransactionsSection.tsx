'use client'

import { useState } from 'react'
import { useClientYear } from '@/components/ClientYearContext'
import { useConfirm } from '@/components/ConfirmDialog'
import EditRowDialog from '@/components/EditRowDialog'
import TransactionsTable from '@/components/TransactionsTable'
import type { Transaction } from '@/lib/api'
import { addManualRows, changeRow, removeRow, type Ledger, type RowChange, type SavedRow } from '@/lib/clients'
import { CLOSED_YEAR_NOTICE, lineTemplate, newRowFrom, type EditDraft } from '@/lib/clientRules'
import { money } from '@/lib/ledger'

const message = (e: unknown) => (e instanceof Error ? e.message : String(e))

// A client's year: its rows, editable. A closed year or an archived client shows them as they are.
export default function TransactionsSection() {
  const { client, year, ledger, reload } = useClientYear()
  const { ask, dialog: confirmDialog } = useConfirm()
  const [error, setError] = useState('')
  const [editing, setEditing] = useState<SavedRow | null>(null)
  const [adding, setAdding] = useState<{ template: Transaction; title: string } | null>(null)   // a row being added
  const readOnly = year.closed || client.archived

  // A change the API answers with the year's ledger: the year is loaded again to show it.
  const act = async (change: () => Promise<Ledger>) => {
    setError('')
    try { await change(); await reload() } catch (e) { setError(message(e)) }
  }
  const remove = async (row: SavedRow) => {
    const question = `Remove "${row.description}" (${money(row.gross)})? This can't be undone. Its document's total is checked again without it.`
    if (await ask(question, 'Remove')) await act(() => removeRow(client.id, row.id))
  }
  const startAdding = (lineOf: SavedRow | null) => setAdding({
    template: lineTemplate(lineOf),
    title: lineOf ? `Add a line to ${lineOf.counterparty ?? 'this document'}` : 'Add a transaction',
  })
  const saveNew = async (draft: EditDraft) => {
    if (!adding) return
    await addManualRows(client.id, [newRowFrom(draft, adding.template)], year.start)
    setAdding(null)
    await reload()
  }
  const saveRow = async (change: RowChange) => {
    if (!editing) return
    await changeRow(client.id, editing.id, change)
    setEditing(null)
    await reload()
  }

  return (
    <>
      {year.closed && <div className="notice notice-warn">{CLOSED_YEAR_NOTICE}</div>}
      {error && <div className="notice notice-error">{error}</div>}
      <TransactionsTable ledger={ledger} readOnly={readOnly} onChange={(id, change) => act(() => changeRow(client.id, id, change))}
                         onEdit={setEditing} onAdd={startAdding} onRemove={remove} />
      {editing && (
        <EditRowDialog row={editing} client={client} onCancel={() => setEditing(null)} onSave={saveRow} />
      )}
      {adding && (
        <EditRowDialog row={adding.template} client={client} title={adding.title}
                       onCancel={() => setAdding(null)} onAdd={saveNew} />
      )}
      {confirmDialog}
    </>
  )
}

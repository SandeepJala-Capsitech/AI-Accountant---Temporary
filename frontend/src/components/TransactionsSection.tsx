'use client'

import { useSearchParams } from 'next/navigation'
import { useMemo, useState } from 'react'
import { useClientYear } from '@/components/ClientYearContext'
import { useConfirm } from '@/components/ConfirmDialog'
import EditRowDialog from '@/components/EditRowDialog'
import TransactionsTable from '@/components/TransactionsTable'
import type { Transaction } from '@/lib/api'
import { addManualRows, changeRow, removeRow, type RowChange, type SavedRow } from '@/lib/clients'
import { CLOSED_YEAR_NOTICE, lineTemplate, newRowFrom, type EditDraft } from '@/lib/clientRules'
import { money, removedForGood } from '@/lib/ledger'

const message = (e: unknown) => (e instanceof Error ? e.message : String(e))

// A client's year: its rows, editable. A closed year or an archived client shows them as they are.
export default function TransactionsSection() {
  const { client, year, ledger, show: showLedger } = useClientYear()
  const { ask, dialog: confirmDialog } = useConfirm()
  const [error, setError] = useState('')
  const [editing, setEditing] = useState<SavedRow | null>(null)
  const [adding, setAdding] = useState<{ template: Transaction; title: string } | null>(null)   // a row being added
  // Rows being removed leave the table at once, and rows being changed are dimmed, while the API works.
  const [removing, setRemoving] = useState<Set<number>>(new Set())
  const [saving, setSaving] = useState<Set<number>>(new Set())
  const without = (set: Set<number>, id: number) => { const next = new Set(set); next.delete(id); return next }
  const listed = useMemo(() => (removing.size
    ? { ...ledger, transactions: ledger.transactions.filter(r => !removing.has(r.id)) } : ledger), [ledger, removing])
  const readOnly = year.closed || client.archived
  // ?row=7 (from the AI review's problems list): open on that row, beside the rows its problems name.
  const wanted = Number(useSearchParams().get('row'))
  const asked = ledger.transactions.find(r => r.id === wanted)
  const show = asked ? [asked.id, ...asked.issues.flatMap(i => i.related ?? [])] : undefined

  // A change the API answers with the year's ledger, which is shown as it comes back.
  const change = async (rowId: number, made: RowChange) => {
    setError('')
    setSaving(s => new Set(s).add(rowId))
    try { showLedger(await changeRow(client.id, rowId, made)) } catch (e) { setError(message(e)) }
    finally { setSaving(s => without(s, rowId)) }
  }
  // The row leaves the table as soon as the person confirms; it comes back, with why, if the API refuses.
  const remove = async (row: SavedRow) => {
    // A row typed in by hand is deleted; one read from a file is set aside, and its document still accounts for it.
    const question = removedForGood(row, ledger.uploads)
      ? `Remove "${row.description}" (${money(row.gross)})? This can't be undone. Its document's total is checked again without it.`
      : `Remove "${row.description}" (${money(row.gross)})? It is taken out of the books and stays listed, crossed out, so you can put it back.`
    if (!(await ask(question, 'Remove'))) return
    setError('')
    setRemoving(s => new Set(s).add(row.id))
    try { showLedger(await removeRow(client.id, row.id)) } catch (e) { setError(message(e)) }
    finally { setRemoving(s => without(s, row.id)) }
  }
  const startAdding = (lineOf: SavedRow | null) => setAdding({
    template: lineTemplate(lineOf),
    title: lineOf ? `Add a line to ${lineOf.counterparty ?? 'this document'}` : 'Add a transaction',
  })
  const saveNew = async (draft: EditDraft) => {
    if (!adding) return
    const next = await addManualRows(client.id, [newRowFrom(draft, adding.template)], year.start)
    setAdding(null)
    showLedger(next)
  }
  const saveRow = async (change: RowChange) => {   // the dialog shows its own Saving… and any error
    if (!editing) return
    const next = await changeRow(client.id, editing.id, change)
    setEditing(null)
    showLedger(next)
  }

  return (
    <>
      {year.closed && <div className="notice notice-warn">{CLOSED_YEAR_NOTICE}</div>}
      {error && <div className="notice notice-error">{error}</div>}
      <TransactionsTable key={wanted || 'all'} ledger={listed} readOnly={readOnly} show={show} saving={saving}
                         onChange={change} onEdit={setEditing} onAdd={startAdding} onRemove={remove} />
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

'use client'

import { useEffect, useState, type FormEvent } from 'react'
import { listAccounts, type AccountChoice, type BusinessType, type RowChange, type SavedRow } from '@/lib/clients'
import { changesOf, DOCUMENT_TYPES, draftOf, rowEditErrors, type EditDraft, type EditErrors } from '@/lib/clientRules'

// Corrects what the model read on one row. Counterparty and document type change for the whole document.
export default function EditRowDialog({ row, businessType, onCancel, onSave }: {
  row: SavedRow
  businessType: BusinessType
  onCancel: () => void
  onSave: (change: RowChange) => Promise<void>
}) {
  const [draft, setDraft] = useState<EditDraft>(() => draftOf(row))
  const [accounts, setAccounts] = useState<AccountChoice[]>([])
  const [showErrors, setShowErrors] = useState(false)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')
  const errors: EditErrors = rowEditErrors(draft)

  useEffect(() => {
    listAccounts(businessType).then(setAccounts).catch(e => setError(e instanceof Error ? e.message : String(e)))
  }, [businessType])

  const set = (field: keyof EditDraft, value: string) => setDraft(d => ({ ...d, [field]: value }))
  const fieldError = (field: keyof EditErrors) =>
    (showErrors && errors[field] ? <span className="field-error">{errors[field]}</span> : null)

  const submit = async (e: FormEvent) => {
    e.preventDefault()
    setShowErrors(true)
    if (Object.keys(errors).length) return
    const change = changesOf(row, draft)
    if (!Object.keys(change).length) {
      onCancel()
      return
    }
    setSaving(true)
    setError('')
    try {
      await onSave(change)
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
      setSaving(false)
    }
  }

  // The saved account stays listed even when this kind of business can't choose it, so the form shows the truth.
  const options = accounts.some(a => a.code === draft.account_code)
    ? accounts : [{ code: row.account_code, name: row.account_name ?? '', type: '', vat: '' }, ...accounts]

  return (
    <div className="overlay" role="presentation" onMouseDown={e => { if (e.target === e.currentTarget) onCancel() }}
         onKeyDown={e => { if (e.key === 'Escape') onCancel() }}>
      <form className="dialog" role="dialog" aria-modal="true" aria-labelledby="edit-row-title" onSubmit={submit} noValidate>
        <h2 id="edit-row-title">Edit row</h2>
        <p className="dialog-note">Counterparty and document type change for every row of this document.</p>
        <div className="field-row">
          <label className="field">
            <span>Date</span>
            <input className="input" type="date" value={draft.date} onChange={e => set('date', e.target.value)} />
            {fieldError('date')}
          </label>
          <label className="field">
            <span>Document type</span>
            <select className="input" value={draft.document_type} onChange={e => set('document_type', e.target.value)}>
              {Object.entries(DOCUMENT_TYPES).map(([value, label]) => <option key={value} value={value}>{label}</option>)}
            </select>
          </label>
        </div>
        <label className="field">
          <span>Description</span>
          <input className="input" value={draft.description} onChange={e => set('description', e.target.value)} />
          {fieldError('description')}
        </label>
        <label className="field">
          <span>Counterparty</span>
          <input className="input" value={draft.counterparty} onChange={e => set('counterparty', e.target.value)}
                 placeholder="Who was paid, or who paid" />
        </label>
        <div className="field-row">
          <label className="field">
            <span>Money</span>
            <select className="input" value={draft.direction} onChange={e => set('direction', e.target.value)}>
              <option value="out">Paid out</option>
              <option value="in">Received</option>
            </select>
          </label>
          <label className="field">
            <span>Amount (£)</span>
            <input className="input" inputMode="decimal" value={draft.gross} onChange={e => set('gross', e.target.value)} />
            {fieldError('gross')}
          </label>
        </div>
        <div className="field-row">
          <label className="field">
            <span>VAT shown (£)</span>
            <input className="input" inputMode="decimal" value={draft.vat} onChange={e => set('vat', e.target.value)}
                   placeholder="None shown" />
            {fieldError('vat')}
          </label>
          <label className="field">
            <span>Account</span>
            <select className="input" value={draft.account_code} onChange={e => set('account_code', e.target.value)}>
              {options.map(a => <option key={a.code} value={a.code}>{a.code} {a.name}</option>)}
            </select>
          </label>
        </div>
        {error && <div className="notice notice-error">{error}</div>}
        <div className="dialog-actions">
          <button type="button" className="btn btn-ghost" onClick={onCancel}>Cancel</button>
          <button type="submit" className="btn btn-primary" disabled={saving}>{saving ? 'Saving…' : 'Save'}</button>
        </div>
      </form>
    </div>
  )
}

'use client'

import { useState, type FormEvent } from 'react'
import type { AiReviewMode, BankAccountKind, BusinessType, Client, ClientFields } from '@/lib/clients'
import { bankAccountsLeft, BANK_KINDS, BUSINESS_TYPES, clientFormErrors } from '@/lib/clientRules'

const NEW_CLIENT: ClientFields = {
  name: '', business_type: 'limited_company', contact_name: '', contact_email: '', contact_phone: '', vat_registered: true,
  first_year_end: '', bank_accounts: [{ code: '', name: 'Bank current account', kind: 'current' }], folder: '', ai_review: 'automatic',
}

// Adds a client, or edits one. The responsible person is the client's own contact. A bank account that has a code
// keeps it when saved (the server gives a new one the next free code of its kind).
export default function ClientDialog({ client, taken, onCancel, onSave }: {
  client?: Client
  taken?: string[]           // the other clients' names
  onCancel: () => void
  onSave: (fields: ClientFields) => Promise<void>
}) {
  const [fields, setFields] = useState<ClientFields>(() => (client ? {
    name: client.name, business_type: client.business_type, contact_name: client.contact_name,
    contact_email: client.contact_email, contact_phone: client.contact_phone, vat_registered: client.vat_registered,
    vat_from: client.vat_from ?? '',
    first_year_end: client.first_year_end, bank_accounts: client.bank_accounts.map(a => ({ ...a })), folder: client.folder,
    ai_review: client.ai_review ?? 'automatic', company_number: client.company_number ?? '', utr: client.utr ?? '',
    vat_number: client.vat_number ?? '', paye_reference: client.paye_reference ?? '',
  } : NEW_CLIENT))
  const [showErrors, setShowErrors] = useState(false)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')
  const errors = clientFormErrors(fields, taken)
  const set = <K extends keyof ClientFields>(key: K, value: ClientFields[K]) => setFields(f => ({ ...f, [key]: value }))
  const changeAccount = (index: number, change: { name?: string; kind?: BankAccountKind }) =>
    set('bank_accounts', fields.bank_accounts.map((a, i) => (i === index ? { ...a, ...change } : a)))
  const kindToAdd = (Object.keys(BANK_KINDS) as BankAccountKind[]).find(kind => bankAccountsLeft(fields.bank_accounts, kind) > 0)

  const submit = async (e: FormEvent) => {
    e.preventDefault()
    setShowErrors(true)
    if (Object.keys(errors).length) return
    setSaving(true)
    setError('')
    try {
      await onSave(fields)
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
      setSaving(false)
    }
  }

  return (
    <div className="overlay" role="presentation" onMouseDown={e => { if (e.target === e.currentTarget) onCancel() }}
         onKeyDown={e => { if (e.key === 'Escape') onCancel() }}>
      <form className="dialog" role="dialog" aria-modal="true" aria-labelledby="client-dialog-title" onSubmit={submit} noValidate>
        <h2 id="client-dialog-title">{client ? 'Edit client' : 'Add client'}</h2>
        <label className="field">
          <span>Company name</span>
          <input className="input" autoFocus value={fields.name} onChange={e => set('name', e.target.value)}
                 placeholder="e.g. Acme Corp Ltd" />
          {showErrors && errors.name && <span className="field-error">{errors.name}</span>}
        </label>
        <label className="field">
          <span>Business type</span>
          <select className="input" value={fields.business_type}
                  onChange={e => set('business_type', e.target.value as BusinessType)}>
            {Object.entries(BUSINESS_TYPES).map(([value, label]) => <option key={value} value={value}>{label}</option>)}
          </select>
        </label>
        <label className="field">
          <span>Responsible person</span>
          <input className="input" value={fields.contact_name} onChange={e => set('contact_name', e.target.value)}
                 placeholder="e.g. Full Name" />
          {showErrors && errors.contact_name && <span className="field-error">{errors.contact_name}</span>}
        </label>
        <div className="field-row">
          <label className="field">
            <span>Email</span>
            <input className="input" type="email" value={fields.contact_email}
                   onChange={e => set('contact_email', e.target.value)} placeholder="e.g. name@company.co.uk" />
            {showErrors && errors.contact_email && <span className="field-error">{errors.contact_email}</span>}
          </label>
          <label className="field">
            <span>Phone</span>
            <input className="input" type="tel" value={fields.contact_phone}
                   onChange={e => set('contact_phone', e.target.value)} placeholder="e.g. +44 7700 900123" />
            {showErrors && errors.contact_phone && <span className="field-error">{errors.contact_phone}</span>}
          </label>
        </div>
        <label className="field">
          <span>First year end</span>
          <input className="input" type="date" value={fields.first_year_end}
                 onChange={e => set('first_year_end', e.target.value)} />
          <span className="hint">The end of the first year the books are kept for, e.g. 31 March 2026. Later years follow it.</span>
          {showErrors && errors.first_year_end && <span className="field-error">{errors.first_year_end}</span>}
        </label>
        <div className="field-row">
          <label className="field">
            <span>Company number</span>
            <input className="input" value={fields.company_number ?? ''} onChange={e => set('company_number', e.target.value)}
                   placeholder="e.g. 09876543" />
          </label>
          <label className="field">
            <span>UTR</span>
            <input className="input" value={fields.utr ?? ''} onChange={e => set('utr', e.target.value)}
                   placeholder="e.g. 12345 67890" />
          </label>
        </div>
        <div className="field-row">
          <label className="field">
            <span>VAT number</span>
            <input className="input" value={fields.vat_number ?? ''} onChange={e => set('vat_number', e.target.value)}
                   placeholder="e.g. GB 123 4567 89" />
          </label>
          <label className="field">
            <span>PAYE reference</span>
            <input className="input" value={fields.paye_reference ?? ''} onChange={e => set('paye_reference', e.target.value)}
                   placeholder="e.g. 120/PA00123456" />
          </label>
        </div>
        <fieldset className="field bank-accounts">
          <legend>Bank accounts</legend>
          {fields.bank_accounts.map((account, i) => (
            <div className="bank-row" key={i}>
              <input className="input" value={account.name} aria-label={`Bank account ${i + 1} name`}
                     onChange={e => changeAccount(i, { name: e.target.value })} placeholder="e.g. Barclays current" />
              <select className="input" value={account.kind} aria-label={`Bank account ${i + 1} kind`}
                      disabled={!!account.code} title={account.code ? 'The kind of an account can’t change once it has a code.' : undefined}
                      onChange={e => changeAccount(i, { kind: e.target.value as BankAccountKind })}>
                {(Object.entries(BANK_KINDS) as [BankAccountKind, string][]).map(([kind, label]) => (
                  <option key={kind} value={kind} disabled={kind !== account.kind && bankAccountsLeft(fields.bank_accounts, kind) < 1}>
                    {label}
                  </option>
                ))}
              </select>
              <button type="button" className="link-btn danger" onClick={() => set('bank_accounts', fields.bank_accounts.filter((_, j) => j !== i))}>
                Remove
              </button>
            </div>
          ))}
          <div>
            <button type="button" className="btn btn-small" disabled={!kindToAdd}
                    onClick={() => kindToAdd && set('bank_accounts', [...fields.bank_accounts, { code: '', name: '', kind: kindToAdd }])}>
              Add bank account
            </button>
          </div>
          {showErrors && errors.bank_accounts && <span className="field-error">{errors.bank_accounts}</span>}
        </fieldset>
        <label className="field">
          <span>Folder</span>
          <input className="input" value={fields.folder} onChange={e => set('folder', e.target.value)}
                 placeholder="e.g. /Users/you/Clients/Acme" />
          <span className="hint">Where this client’s files are on this computer. Optional: it lets you import new files in one go.</span>
        </label>
        <label className="field">
          <span>AI review</span>
          <select className="input" value={fields.ai_review ?? 'automatic'} onChange={e => set('ai_review', e.target.value as AiReviewMode)}>
            <option value="automatic">Automatic</option>
            <option value="suggest">Suggest only</option>
            <option value="off">Off</option>
          </select>
          <span className="hint">After the books change and go quiet, the AI looks them over. Automatic makes the changes (you can undo them); Suggest only leaves them for you to apply.</span>
        </label>
        <label className="switch">
          <input type="checkbox" checked={fields.vat_registered} onChange={e => set('vat_registered', e.target.checked)} />
          <span>VAT registered</span>
        </label>
        {fields.vat_registered && (
          <label className="field">
            <span>VAT registered from</span>
            <input className="input" type="date" value={fields.vat_from ?? ''} onChange={e => set('vat_from', e.target.value)} />
            <span className="hint">Leave empty if the client was registered before the first year. Rows dated before this day book no VAT.</span>
          </label>
        )}
        {error && <div className="notice notice-error">{error}</div>}
        <div className="dialog-actions">
          <button type="button" className="btn btn-ghost" onClick={onCancel}>Cancel</button>
          <button type="submit" className="btn btn-primary" disabled={saving}>
            {saving ? 'Saving…' : client ? 'Save changes' : 'Add client'}
          </button>
        </div>
      </form>
    </div>
  )
}

'use client'

import { useCallback, useEffect, useRef, useState } from 'react'
import { analysisForm, analyze, ApiError, getHealth, waitForJob, type Transaction } from '@/lib/api'
import { addManualRows, importFolder, type BankAccount, type Upload } from '@/lib/clients'
import { importText, requeue, sameFileAs, sameFileText, transactionCount, type FileStatus, type PickedFile } from '@/lib/clientRules'
import { fingerprint } from '@/lib/duplicates'

const MAX_BATCH_FILES = 20

const MARK: Record<FileStatus, string> = { waiting: '○', reading: '', done: '✓', failed: '✖', skipped: '–', cancelled: '■' }

const message = (e: unknown) => (e instanceof Error ? e.message : String(e))

// Upload, Paste and Add by hand for one year of a client, and Import from folder. Each analysis runs as a job that
// the API saves to the year when it finishes, so onSaved reloads the year; a row added by hand is saved here, once
// the model has suggested its account. A statement goes on the account picked in Add to (the client's first by
// default): the API refuses a statement without one when the client has several.
export default function AddDocuments({ clientId, yearStart, bankAccounts, folder, uploads, onSaved }: {
  clientId: number
  yearStart: string
  bankAccounts: BankAccount[]
  folder: string             // the client's folder on this computer; '' when none is set
  uploads: Upload[]
  onSaved: () => void
}) {
  const [tab, setTab] = useState<'upload' | 'paste' | 'manual'>('upload')
  const [pasteText, setPasteText] = useState('')
  const [manual, setManual] = useState({ description: '', amount: '', direction: 'out' as 'in' | 'out' })
  const [dragOver, setDragOver] = useState(false)
  const [busy, setBusy] = useState(false)
  const [progress, setProgress] = useState('')
  const [error, setError] = useState('')
  const [batch, setBatch] = useState<PickedFile[]>([])
  const [picked, setPicked] = useState('')   // the account chosen in Add to
  const [imported, setImported] = useState('')   // what the last import from the folder found and did
  const [limits, setLimits] = useState({ parallel: 1, maxMb: 20 })
  const bank = bankAccounts.some(a => a.code === picked) ? picked : bankAccounts[0]?.code ?? ''
  const where = { clientId, yearStart, bankAccount: bank }
  const cancelRef = useRef(false)
  const nextId = useRef(1)
  const fileInput = useRef<HTMLInputElement>(null)

  useEffect(() => {
    getHealth()
      .then(h => setLimits({ parallel: Math.max(1, h.max_parallel_jobs ?? 1), maxMb: h.max_upload_mb }))
      .catch(() => { /* the header says the API is down; the defaults stand */ })
  }, [])

  const update = useCallback((id: number, patch: Partial<PickedFile>) =>
    setBatch(prev => prev.map(item => (item.id === id ? { ...item, ...patch } : item))), [])

  const analyseText = async () => {
    if (!pasteText.trim()) return
    cancelRef.current = false
    setBusy(true)
    setError('')
    setProgress('Uploading…')
    try {
      const result = await analyze(analysisForm({ text: pasteText.trim() }, where), setProgress, () => cancelRef.current)
      if (!result.transactions.length) throw new ApiError('No transactions were found in this text.', 'empty')
      setPasteText('')
      onSaved()
    } catch (e) {
      setError(message(e))
    } finally {
      setBusy(false)
      setProgress('')
    }
  }

  // Reads the waiting files, up to `parallel` at a time; each is saved to the client as it finishes.
  const runBatch = async (items: PickedFile[]) => {
    const todo = items.filter(item => item.status === 'waiting')
    if (!todo.length) return
    cancelRef.current = false
    setBusy(true)
    setError('')
    let next = 0
    let read = 0
    const worker = async () => {
      while (next < todo.length) {
        const item = todo[next++]
        if (cancelRef.current) {
          update(item.id, { status: 'cancelled', detail: 'Not read: cancelled' })
          continue
        }
        update(item.id, { status: 'reading', detail: 'Uploading…' })
        try {
          const result = await analyze(analysisForm({ file: item.file }, where), p => update(item.id, { detail: p }),
                                       () => cancelRef.current)
          const count = result.transactions.length
          if (!count) throw new ApiError('No transactions were found in this file.', 'empty')
          update(item.id, { status: 'done', detail: `${transactionCount(count)} saved` })
          onSaved()
        } catch (e) {
          const cancelled = e instanceof ApiError && e.code === 'cancelled'
          update(item.id, { status: cancelled ? 'cancelled' : 'failed', detail: message(e) })
        }
        read++
        setProgress(`${read} of ${todo.length} files read, up to ${limits.parallel} at a time`)
      }
    }
    setProgress(`Reading ${todo.length} file${todo.length === 1 ? '' : 's'}, up to ${limits.parallel} at a time…`)
    await Promise.all(Array.from({ length: Math.min(limits.parallel, todo.length) }, worker))
    setBusy(false)
    setProgress('')
  }

  const pickFiles = async (files: FileList | null) => {
    const chosen = Array.from(files ?? [])
    if (fileInput.current) fileInput.current.value = ''   // lets the same files be chosen again
    if (!chosen.length) return
    const hashes = await Promise.all(chosen.slice(0, MAX_BATCH_FILES).map(fingerprint))
    const items: PickedFile[] = []
    hashes.forEach((hash, n) => {
      const file = chosen[n]
      // A file already saved for this client, or picked twice, is not read again: its rows would be booked twice.
      const saved = sameFileAs(hash, uploads)
      const twice = hash ? items.find(item => item.hash === hash) : undefined
      // Size is checked here because the Next proxy cuts oversized bodies instead of refusing them.
      const tooBig = file.size > limits.maxMb * 1024 * 1024
      const detail = saved ? sameFileText(saved)
        : twice ? `Skipped: same file as ${twice.file.name} in this upload`
        : tooBig ? `Larger than the ${limits.maxMb} MB limit: split it and upload the parts` : 'Waiting'
      items.push({ id: nextId.current++, file, hash, status: saved || twice || tooBig ? 'skipped' : 'waiting', detail })
    })
    setBatch(items)
    setError(chosen.length > MAX_BATCH_FILES
      ? `Only the first ${MAX_BATCH_FILES} files were added; upload the other ${chosen.length - MAX_BATCH_FILES} next.`
      : '')
    runBatch(items)
  }

  // Files saved after all, just as a cancel arrived, are skipped rather than read and booked again.
  const retry = () => {
    const again = requeue(batch, uploads)
    setBatch(again)
    runBatch(again)
  }

  // A row typed by hand: the model only suggests its account; the ledger splits VAT like any other row's.
  const addByHand = async () => {
    const amount = Math.abs(Number(manual.amount))
    const description = manual.description.trim()
    if (!description || !Number.isFinite(amount) || amount <= 0) return
    cancelRef.current = false
    setBusy(true)
    setError('')
    setProgress('Asking the AI model for an account…')
    try {
      const verb = manual.direction === 'in' ? 'received' : 'paid'
      const result = await analyze(analysisForm({ text: `${description} - ${verb} GBP ${amount}` }), setProgress, () => cancelRef.current)
      const suggested = result.transactions[0]
      const row: Transaction = {
        date: null, description, direction: manual.direction, gross: amount.toFixed(2), vat: null,
        vat_treatment: null, vat_posted: null, net: null, account_code: suggested?.account_code ?? '9998',
        contra_account_code: null, currency: 'GBP', source: 'manual', method: 'user', evidence: null,
        document_type: 'receipt',
        issues: suggested
          ? suggested.issues.filter(i => i.code === 'account_not_recognised')
          : [{ code: 'account_not_recognised', severity: 'warning', message: 'No account suggested; posted to Suspense.' }],
      }
      await addManualRows(clientId, [row], yearStart)
      onSaved()
      setManual(m => ({ ...m, description: '', amount: '' }))
    } catch (e) {
      setError(message(e))
    } finally {
      setBusy(false)
      setProgress('')
    }
  }

  // Reads the files in the client's folder it has not had before, as jobs like an upload's, then reloads the year.
  const importFromFolder = async () => {
    cancelRef.current = false
    setBusy(true)
    setError('')
    setImported('')
    setProgress('Looking in the folder…')
    try {
      const result = await importFolder(clientId, yearStart, bank)
      const failed: string[] = []
      let read = 0
      setProgress(`Reading ${result.jobs.length} new file${result.jobs.length === 1 ? '' : 's'}…`)
      await Promise.all(result.jobs.map(async job => {
        try {
          await waitForJob(job.job_id, () => {}, () => cancelRef.current)
        } catch (e) {
          failed.push(`${job.name}: ${message(e)}`)
        }
        setProgress(`${++read} of ${result.jobs.length} files read…`)
      }))
      setImported(importText(result))
      if (failed.length) setError(failed.join(' '))
    } catch (e) {
      setError(message(e))
    } finally {
      setBusy(false)
      setProgress('')
      onSaved()
    }
  }

  const canRetry = !busy && batch.some(item => item.status === 'failed' || item.status === 'cancelled')
  const closed = busy   // nothing new can be started while files are being read
  return (
    <section className="card section">
      <div className="section-head">
        <h2>Add documents</h2>
        <span className="muted small">Invoices, receipts, bank statements and expense claims</span>
      </div>
      <div className="tabs" role="tablist">
        {(['upload', 'paste', 'manual'] as const).map(t => (
          <button key={t} type="button" role="tab" className="tab" aria-selected={tab === t} onClick={() => setTab(t)}>
            {t === 'upload' ? 'Upload files' : t === 'paste' ? 'Paste text' : 'Add by hand'}
          </button>
        ))}
      </div>
      <div className="add-to">
        {bankAccounts.length > 1 && (
          <label className="year-picker">
            <span>Add to</span>
            <select className="input" value={bank} aria-label="Bank account for statements" onChange={e => setPicked(e.target.value)}>
              {bankAccounts.map(a => <option key={a.code} value={a.code}>{a.name}</option>)}
            </select>
          </label>
        )}
        <button type="button" className="btn" onClick={importFromFolder} disabled={closed || !folder}>Import from folder</button>
        {!folder && <span className="muted small">Set the client&apos;s folder in Edit details to import from it.</span>}
      </div>
      {imported && <div className="notice notice-info">{imported}</div>}
      {tab === 'upload' && (
        <>
          <input ref={fileInput} type="file" multiple hidden onChange={e => pickFiles(e.target.files)}
                 accept=".csv,.tsv,.txt,.xlsx,.xls,.pdf,.png,.jpg,.jpeg,.webp,.bmp,.tiff" />
          <div className={`dropzone${dragOver ? ' drag-over' : ''}`} role="button" tabIndex={0}
               onClick={() => { if (!closed) fileInput.current?.click() }}
               onKeyDown={e => { if ((e.key === 'Enter' || e.key === ' ') && !closed) fileInput.current?.click() }}
               onDragOver={e => { e.preventDefault(); setDragOver(true) }}
               onDragLeave={() => setDragOver(false)}
               onDrop={e => { e.preventDefault(); setDragOver(false); if (!closed) pickFiles(e.dataTransfer.files) }}>
            <strong>Drop files here or click to choose</strong>
            Up to {MAX_BATCH_FILES} at a time: PDFs, photos, CSV and Excel files, up to {limits.maxMb} MB each
          </div>
        </>
      )}
      {tab === 'paste' && (
        <>
          <textarea className="input" value={pasteText} onChange={e => setPasteText(e.target.value)} aria-label="Text to analyse"
                    placeholder={'BT Business broadband, 05/09/2026, £72.00 including £12.00 VAT\nBank: 25/09/2026 BT GROUP PLC DD -72.00'} />
          <div className="form-actions">
            <button type="button" className="btn btn-primary" onClick={analyseText} disabled={closed || !pasteText.trim()}>
              Analyse
            </button>
          </div>
        </>
      )}
      {tab === 'manual' && (
        <>
          <div className="manual-grid">
            <label className="field">
              <span>Description</span>
              <input className="input" value={manual.description} placeholder="Office chair"
                     onChange={e => setManual(m => ({ ...m, description: e.target.value }))} />
            </label>
            <label className="field">
              <span>Amount (£)</span>
              <input className="input" type="number" step="0.01" min="0" value={manual.amount} placeholder="120.00"
                     onChange={e => setManual(m => ({ ...m, amount: e.target.value }))} />
            </label>
            <label className="field">
              <span>Money</span>
              <select className="input" value={manual.direction}
                      onChange={e => setManual(m => ({ ...m, direction: e.target.value as 'in' | 'out' }))}>
                <option value="out">Paid out</option>
                <option value="in">Received</option>
              </select>
            </label>
          </div>
          <div className="form-actions">
            <button type="button" className="btn btn-primary" onClick={addByHand}
                    disabled={closed || !manual.description.trim() || !manual.amount}>
              Add row
            </button>
          </div>
        </>
      )}
      {busy && (
        <div className="progress">
          <span className="spinner" aria-hidden="true" />
          <span>{progress || 'Working…'}</span>
          <button type="button" className="btn btn-ghost" onClick={() => { cancelRef.current = true; setProgress('Cancelling…') }}>
            Cancel
          </button>
        </div>
      )}
      {error && <div className="notice notice-error">{error}</div>}
      {batch.length > 0 && (
        <ul className="batch">
          {batch.map(item => (
            <li key={item.id} className={`batch-${item.status}`}>
              <span aria-hidden="true">{item.status === 'reading' ? <span className="spinner" /> : MARK[item.status]}</span>
              <span className="batch-name" title={item.file.name}>{item.file.name}</span>
              <span className="batch-detail" title={item.detail}>{item.detail}</span>
            </li>
          ))}
        </ul>
      )}
      {canRetry && (
        <div className="form-actions"><button type="button" className="btn" onClick={retry}>Retry failed files</button></div>
      )}
    </section>
  )
}

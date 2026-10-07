'use client'

import { useCallback, useEffect, useRef, useState } from 'react'
import { analyze, ApiError, getHealth, type Transaction } from '@/lib/api'
import { addManualRows, type Ledger, type Upload } from '@/lib/clients'
import { requeue, sameFileAs, sameFileText, type FileStatus, type PickedFile } from '@/lib/clientRules'
import { fingerprint } from '@/lib/duplicates'

const MAX_BATCH_FILES = 20

const MARK: Record<FileStatus, string> = { waiting: '○', reading: '', done: '✓', failed: '✖', skipped: '–', cancelled: '■' }

const message = (e: unknown) => (e instanceof Error ? e.message : String(e))

const form = (fields: Record<string, string | Blob>) => {
  const data = new FormData()
  for (const [key, value] of Object.entries(fields)) data.append(key, value)
  return data
}

// Upload, Paste and Add by hand for one client. Each analysis runs as a job that the API saves to the client
// when it finishes, so onSaved reloads the ledger; a row added by hand is saved here, once the model has
// suggested its account.
export default function AddDocuments({ clientId, uploads, onSaved, onLedger }: {
  clientId: number
  uploads: Upload[]
  onSaved: () => void
  onLedger: (ledger: Ledger) => void
}) {
  const [tab, setTab] = useState<'upload' | 'paste' | 'manual'>('upload')
  const [pasteText, setPasteText] = useState('')
  const [manual, setManual] = useState({ description: '', amount: '', direction: 'out' as 'in' | 'out' })
  const [dragOver, setDragOver] = useState(false)
  const [busy, setBusy] = useState(false)
  const [progress, setProgress] = useState('')
  const [error, setError] = useState('')
  const [batch, setBatch] = useState<PickedFile[]>([])
  const [limits, setLimits] = useState({ parallel: 1, maxMb: 20 })
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
      const result = await analyze(form({ text: pasteText.trim(), client_id: String(clientId) }), setProgress,
                                   () => cancelRef.current)
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
          const result = await analyze(form({ file: item.file, client_id: String(clientId) }),
                                       p => update(item.id, { detail: p }), () => cancelRef.current)
          const count = result.transactions.length
          if (!count) throw new ApiError('No transactions were found in this file.', 'empty')
          update(item.id, { status: 'done', detail: `${count} row${count === 1 ? '' : 's'} saved` })
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
    const picked = Array.from(files ?? [])
    if (fileInput.current) fileInput.current.value = ''   // lets the same files be chosen again
    if (!picked.length) return
    const hashes = await Promise.all(picked.slice(0, MAX_BATCH_FILES).map(fingerprint))
    const items: PickedFile[] = []
    hashes.forEach((hash, n) => {
      const file = picked[n]
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
    setError(picked.length > MAX_BATCH_FILES
      ? `Only the first ${MAX_BATCH_FILES} files were added; upload the other ${picked.length - MAX_BATCH_FILES} next.`
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
      const result = await analyze(form({ text: `${description} - ${verb} GBP ${amount}` }), setProgress,
                                   () => cancelRef.current)
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
      onLedger(await addManualRows(clientId, [row]))
      setManual(m => ({ ...m, description: '', amount: '' }))
    } catch (e) {
      setError(message(e))
    } finally {
      setBusy(false)
      setProgress('')
    }
  }

  const canRetry = !busy && batch.some(item => item.status === 'failed' || item.status === 'cancelled')
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
      {tab === 'upload' && (
        <>
          <input ref={fileInput} type="file" multiple hidden onChange={e => pickFiles(e.target.files)}
                 accept=".csv,.tsv,.txt,.xlsx,.xls,.pdf,.png,.jpg,.jpeg,.webp,.bmp,.tiff" />
          <div className={`dropzone${dragOver ? ' drag-over' : ''}`} role="button" tabIndex={0}
               onClick={() => { if (!busy) fileInput.current?.click() }}
               onKeyDown={e => { if ((e.key === 'Enter' || e.key === ' ') && !busy) fileInput.current?.click() }}
               onDragOver={e => { e.preventDefault(); setDragOver(true) }}
               onDragLeave={() => setDragOver(false)}
               onDrop={e => { e.preventDefault(); setDragOver(false); if (!busy) pickFiles(e.dataTransfer.files) }}>
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
            <button type="button" className="btn btn-primary" onClick={analyseText} disabled={busy || !pasteText.trim()}>
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
                    disabled={busy || !manual.description.trim() || !manual.amount}>
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

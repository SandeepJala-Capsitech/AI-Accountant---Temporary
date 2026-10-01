'use client'

import { useState, useRef, useCallback, useEffect } from 'react'
import {
  analyze as runAnalysisJob,
  ApiError,
  getHealth,
  trialBalance,
  validateTransactions,
  type Transaction,
  type AnalyzeResult,
  type Health,
  type TrialBalanceResult,
} from '@/lib/api'

// ─── Utilities ────────────────────────────────────────────────────────────────

const fmt = (n: number) =>
  n === 0
    ? ''
    : `£${Math.abs(n).toLocaleString('en-GB', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`

const money = (value: string | null) => (value == null ? '—' : fmt(Number(value)) || '£0.00')

const issueSummary = (tx: Transaction) => {
  if (!tx.issues.length) return { mark: '✓', cls: 'issue-ok', title: 'No issues' }
  const worst = tx.issues.some(i => i.severity === 'error') ? 'issue-error'
    : tx.issues.some(i => i.severity === 'warning') ? 'issue-warn' : 'issue-info'
  return { mark: `${worst === 'issue-error' ? '✖' : '⚠'} ${tx.issues.length}`, cls: worst,
           title: tx.issues.map(i => i.message).join('\n') }
}

// ─── Several files in one upload ──────────────────────────────────────────────

const MAX_BATCH_FILES = 20

type FileStatus = 'waiting' | 'reading' | 'done' | 'failed' | 'skipped' | 'cancelled'

interface BatchItem {
  id: number
  file: File
  status: FileStatus
  detail: string
}

const STATUS_MARK: Record<FileStatus, string> = {
  waiting: '○', reading: '', done: '✓', failed: '✖', skipped: '–', cancelled: '■',
}

// A row of the table and where it came from: a file name, "Pasted text" or "Manual entry".
interface LedgerRow {
  tx: Transaction
  origin: string
}

// ─── Main Page Component ──────────────────────────────────────────────────────

export default function Home() {
  // Step 1 state
  const [activeTab, setActiveTab] = useState<'upload' | 'paste' | 'manual'>('paste')
  const [pasteText, setPasteText] = useState('')
  const [manualDesc, setManualDesc] = useState('')
  const [manualAmount, setManualAmount] = useState('')
  const [manualType, setManualType] = useState<'expense' | 'revenue'>('expense')
  const [dragOver, setDragOver] = useState(false)
  const fileInputRef = useRef<HTMLInputElement>(null)

  // API / model status
  const [health, setHealth] = useState<Health | null>(null)
  const [healthError, setHealthError] = useState('')

  // Step 2 state
  const [analyzing, setAnalyzing] = useState(false)
  const [progress, setProgress] = useState('')
  const [analyzeError, setAnalyzeError] = useState('')
  const [warnings, setWarnings] = useState<string[]>([])
  const [modelName, setModelName] = useState<string | null>(null)
  const [rows, setRows] = useState<LedgerRow[]>([])      // every input adds to these
  const [batch, setBatch] = useState<BatchItem[]>([])    // the files of the latest upload
  const nextFileId = useRef(1)
  const cancelRef = useRef(false)

  // Step 3 state
  const [generating, setGenerating] = useState(false)
  const [tbResult, setTbResult] = useState<TrialBalanceResult | null>(null)
  const [tbError, setTbError] = useState('')

  // ── AI online / offline indicator ──────────────────────────────────────────

  useEffect(() => {
    let alive = true
    const check = () =>
      getHealth()
        .then(h => {
          if (alive) { setHealth(h); setHealthError('') }
        })
        .catch((e: unknown) => {
          if (alive) { setHealth(null); setHealthError(e instanceof Error ? e.message : String(e)) }
        })
    check()
    const timer = setInterval(check, 30_000)
    return () => { alive = false; clearInterval(timer) }
  }, [])

  // ── Step 1 → Step 2: run an analysis job ───────────────────────────────────

  // New rows join the table; a trial balance made before them no longer covers every row.
  const addRows = useCallback((txs: Transaction[], origin: string, warningsFound: string[]) => {
    setRows(prev => [...prev, ...txs.map(tx => ({ tx, origin }))])
    setWarnings(prev => [...prev, ...warningsFound.map(w => `${origin}: ${w}`)])
    setTbResult(null)
    setTbError('')
  }, [])

  const runAnalysis = useCallback(async (
    formData: FormData,
    origin: string,
    toTransactions: (result: AnalyzeResult) => Transaction[] | Promise<Transaction[]> = result => result.transactions,
  ) => {
    cancelRef.current = false
    setAnalyzing(true)
    setProgress('Uploading…')
    setAnalyzeError('')
    try {
      const result = await runAnalysisJob(formData, setProgress, () => cancelRef.current)
      setModelName(result.model)
      const txs = await toTransactions(result)
      if (!txs.length) throw new ApiError('No transactions were found in this input.', 'empty')
      addRows(txs, origin, result.warnings)
    } catch (e: unknown) {
      setAnalyzeError(e instanceof Error ? e.message : String(e))
    } finally {
      setAnalyzing(false)
      setProgress('')
    }
  }, [addRows])

  const updateFile = useCallback((id: number, patch: Partial<BatchItem>) => {
    setBatch(prev => prev.map(item => (item.id === id ? { ...item, ...patch } : item)))
  }, [])

  // Reads the waiting files one after another, through the same job API as a single upload.
  // A file that fails is marked and the next one is read; Cancel stops the batch.
  const runBatch = useCallback(async (items: BatchItem[]) => {
    const todo = items.filter(item => item.status === 'waiting')
    if (!todo.length) return
    cancelRef.current = false
    setAnalyzing(true)
    const cancelRest = (from: number) =>
      todo.slice(from).forEach(rest => updateFile(rest.id, { status: 'cancelled', detail: 'Not read: cancelled' }))
    for (let n = 0; n < todo.length; n++) {
      const item = todo[n]
      if (cancelRef.current) {
        cancelRest(n)
        break
      }
      setProgress(`File ${n + 1} of ${todo.length}: ${item.file.name}`)
      updateFile(item.id, { status: 'reading', detail: 'Uploading…' })
      try {
        const formData = new FormData()
        formData.append('file', item.file)
        const result = await runAnalysisJob(formData, p => updateFile(item.id, { detail: p }),
                                            () => cancelRef.current)
        const count = result.transactions.length
        if (!count) throw new ApiError('No transactions were found in this file.', 'empty')
        setModelName(result.model)
        addRows(result.transactions, item.file.name, result.warnings)
        updateFile(item.id, { status: 'done', detail: `${count} row${count === 1 ? '' : 's'}` })
      } catch (e: unknown) {
        const cancelled = e instanceof ApiError && e.code === 'cancelled'
        updateFile(item.id, { status: cancelled ? 'cancelled' : 'failed',
                              detail: e instanceof Error ? e.message : String(e) })
        if (cancelled) {
          cancelRest(n + 1)
          break
        }
      }
    }
    setAnalyzing(false)
    setProgress('')
  }, [addRows, updateFile])

  const cancelAnalysis = () => {
    cancelRef.current = true
    setProgress('Cancelling…')
  }

  const handlePaste = () => {
    if (!pasteText.trim()) return
    const fd = new FormData()
    fd.append('text', pasteText.trim())
    runAnalysis(fd, 'Pasted text')
  }

  const handleManual = () => {
    const amt = parseFloat(manualAmount)
    if (!manualDesc.trim() || isNaN(amt)) return
    const description = manualDesc.trim()
    const amount = Math.abs(amt)
    const type = manualType
    const verb = type === 'revenue' ? 'received' : 'paid'
    const fd = new FormData()
    fd.append('text', `${description} - ${verb} GBP ${amount}`)
    // The model only suggests the account; the backend splits the VAT like any other row.
    runAnalysis(fd, 'Manual entry', async result => {
      const suggested = result.transactions[0]
      const row: Transaction = {
        date: null, description, direction: type === 'revenue' ? 'in' : 'out', gross: amount.toFixed(2),
        vat: null, vat_treatment: null, vat_posted: null, net: null,
        account_code: suggested?.account_code ?? '9998',
        contra_account_code: null, currency: 'GBP', source: 'manual', method: 'user', evidence: null,
        issues: suggested
          ? suggested.issues.filter(i => i.code === 'account_not_recognised')
          : [{ code: 'account_not_recognised', severity: 'warning', message: 'No account suggested; posted to Suspense.' }],
      }
      return (await validateTransactions([row])).transactions
    })
  }

  const handleFiles = (files: FileList | null) => {
    const picked = Array.from(files ?? [])
    if (fileInputRef.current) fileInputRef.current.value = '' // lets the same files be chosen again
    if (!picked.length) return
    const limitMb = health?.max_upload_mb ?? 20
    const items: BatchItem[] = picked.slice(0, MAX_BATCH_FILES).map(file => {
      // Checked here because the Next proxy cuts oversized bodies instead of rejecting them.
      const tooBig = file.size > limitMb * 1024 * 1024
      return {
        id: nextFileId.current++, file, status: tooBig ? 'skipped' : 'waiting',
        detail: tooBig ? `Larger than the ${limitMb} MB limit: split it and upload the parts` : 'Waiting',
      }
    })
    setBatch(items)
    setAnalyzeError(picked.length > MAX_BATCH_FILES
      ? `Only the first ${MAX_BATCH_FILES} files were added; upload the other ${picked.length - MAX_BATCH_FILES} next.`
      : '')
    runBatch(items)
  }

  const retryFailed = () => {
    const again = batch.map(item => (item.status === 'failed' || item.status === 'cancelled'
      ? { ...item, status: 'waiting' as const, detail: 'Waiting' }
      : item))
    setBatch(again)
    setAnalyzeError('')
    runBatch(again)
  }

  const clearTable = () => {
    setRows([])
    setBatch([])
    setWarnings([])
    setAnalyzeError('')
    setModelName(null)
    setTbResult(null)
    setTbError('')
  }

  // ── Step 2 → Step 3: call /api/trial-balance ───────────────────────────────

  const generateTrialBalance = useCallback(async () => {
    if (!rows.length) return
    setGenerating(true)
    setTbError('')
    setTbResult(null)
    try {
      setTbResult(await trialBalance(rows.map(row => row.tx)))
    } catch (e: unknown) {
      setTbError(e instanceof Error ? e.message : String(e))
    } finally {
      setGenerating(false)
    }
  }, [rows])

  // ── Pipeline step state ────────────────────────────────────────────────────
  const step1Done = rows.length > 0
  const step2Active = analyzing || step1Done
  const showStep2 = analyzing || rows.length > 0 || batch.length > 0 || !!analyzeError
  const canRetry = !analyzing && batch.some(item => item.status === 'failed' || item.status === 'cancelled')
  const step3Active = tbResult !== null || generating

  // ── Render ─────────────────────────────────────────────────────────────────

  return (
    <div className="page-wrapper">

      {/* Header */}
      <header className="header">
        <div className="header-icon">£</div>
        <div>
          <div className="header-title">UK LedgerSync</div>
          <div className="header-sub">
            {health
              ? health.model_available
                ? <span className="health-ok">● AI online · {health.model}</span>
                : <span className="health-off">● AI offline · {health.ai_error}</span>
              : <span className="health-off">● {healthError || 'Checking API…'}</span>}
          </div>
        </div>
      </header>

      {/* Pipeline stepper */}
      <div className="pipeline">
        <div className={`pipeline-step ${step1Done ? 'done' : 'active'}`}>
          <div className="step-num">{step1Done ? '✓' : '1'}</div>
          Input
        </div>
        <div className="pipeline-arrow" />
        <div className={`pipeline-step ${step3Active ? 'done' : step2Active ? 'active' : ''}`}>
          <div className="step-num">{step3Active ? '✓' : '2'}</div>
          Qwen AI
        </div>
        <div className="pipeline-arrow" />
        <div className={`pipeline-step ${step3Active ? 'active' : ''}`}>
          <div className="step-num">3</div>
          Trial Balance
        </div>
      </div>

      {/* ── STEP 1 — Input ─────────────────────────────────────────────────── */}
      <div className="section">
        <div className="section-header">
          <span className="step-badge badge-1">Step 1</span>
          <span className="section-title">Provide Financial Input</span>
          <span className="section-sub">Upload · Paste · Manual</span>
        </div>
        <div className="section-body">

          {/* Tabs */}
          <div className="tabs">
            {(['paste', 'upload', 'manual'] as const).map(t => (
              <button
                key={t}
                className={`tab-btn ${activeTab === t ? 'active' : ''}`}
                onClick={() => setActiveTab(t)}
              >
                {t === 'paste' ? '📋 Quick Paste' : t === 'upload' ? '📁 File Upload' : '✏️ Manual Entry'}
              </button>
            ))}
          </div>

          {/* Quick Paste */}
          <div className={`tab-panel ${activeTab === 'paste' ? 'active' : ''}`}>
            <textarea
              id="pasteInput"
              className="textarea"
              placeholder={`Paste transaction text or CSV rows…\n\nExamples:\n  Office chair purchased for £500\n  Consulting services sold for £2,000\n  BT Business Broadband monthly bill 72.00`}
              value={pasteText}
              onChange={e => setPasteText(e.target.value)}
            />
            <div className="row-end">
              <button
                id="analyzePasteBtn"
                className="btn btn-primary"
                onClick={handlePaste}
                disabled={analyzing || !pasteText.trim()}
              >
                {analyzing ? <><span className="spinner" /> Analysing…</> : '→ Analyse with Qwen'}
              </button>
            </div>
          </div>

          {/* File Upload */}
          <div className={`tab-panel ${activeTab === 'upload' ? 'active' : ''}`}>
            <input
              type="file"
              multiple
              ref={fileInputRef}
              style={{ display: 'none' }}
              accept=".csv,.tsv,.txt,.xlsx,.xls,.pdf,.png,.jpg,.jpeg,.webp,.bmp,.tiff"
              onChange={e => handleFiles(e.target.files)}
            />
            <div
              id="dropzone"
              className={`dropzone ${dragOver ? 'drag-over' : ''}`}
              onClick={() => !analyzing && fileInputRef.current?.click()}
              onDragOver={e => { e.preventDefault(); setDragOver(true) }}
              onDragLeave={() => setDragOver(false)}
              onDrop={e => { e.preventDefault(); setDragOver(false); if (!analyzing) handleFiles(e.dataTransfer.files) }}
            >
              <div className="dropzone-icon">📥</div>
              <h3>Drop files here or click to browse</h3>
              <p>Up to {MAX_BATCH_FILES} at a time: bank statements, VAT receipts, invoices, spreadsheets</p>
              <div className="file-tags">
                {['.CSV', '.XLSX', '.PDF', '.PNG / .JPG'].map(t => (
                  <span key={t} className="file-tag">{t}</span>
                ))}
              </div>
            </div>
          </div>

          {/* Manual Entry */}
          <div className={`tab-panel ${activeTab === 'manual' ? 'active' : ''}`}>
            <div className="form-grid">
              <div style={{ gridColumn: '1 / -1' }}>
                <label htmlFor="mDesc">Description / Payee</label>
                <input
                  id="mDesc"
                  className="input"
                  placeholder="e.g. Office chair, Consulting to ACME Corp"
                  value={manualDesc}
                  onChange={e => setManualDesc(e.target.value)}
                />
              </div>
              <div>
                <label htmlFor="mAmount">Amount (£)</label>
                <input
                  id="mAmount"
                  className="input"
                  type="number"
                  step="0.01"
                  placeholder="500.00"
                  value={manualAmount}
                  onChange={e => setManualAmount(e.target.value)}
                />
              </div>
              <div>
                <label htmlFor="mType">Transaction Type</label>
                <select
                  id="mType"
                  className="input"
                  value={manualType}
                  onChange={e => setManualType(e.target.value as 'expense' | 'revenue')}
                >
                  <option value="expense">Expense (Payment Out)</option>
                  <option value="revenue">Revenue (Payment In)</option>
                </select>
              </div>
            </div>
            <div className="row-end">
              <button
                id="analyzeManualBtn"
                className="btn btn-primary"
                onClick={handleManual}
                disabled={analyzing || !manualDesc.trim() || !manualAmount}
              >
                {analyzing ? <><span className="spinner" /> Analysing…</> : '→ Analyse with Qwen'}
              </button>
            </div>
          </div>
        </div>
      </div>

      {/* Connector */}
      {showStep2 && (
        <div className="connector">↓</div>
      )}

      {/* ── STEP 2 — Structured Data ────────────────────────────────────────── */}
      {showStep2 && (
        <div className="section">
          <div className="section-header">
            <span className="step-badge badge-2">Step 2</span>
            <span className="section-title">Qwen AI — Structured Output</span>
            {rows.length > 0 && (
              <span className="section-sub">
                {rows.length} transaction{rows.length !== 1 ? 's' : ''} extracted
                {modelName ? ` · ${modelName}` : ''}
              </span>
            )}
          </div>
          <div className="section-body">

            {analyzing && (
              <div className="status-msg status-processing">
                <span className="spinner" />
                <span>{progress || 'Working…'}</span>
                <button className="btn btn-ghost" onClick={cancelAnalysis} disabled={progress === 'Cancelling…'}>
                  Cancel
                </button>
              </div>
            )}

            {analyzeError && (
              <div className="status-msg status-error">
                ⚠ {analyzeError}
              </div>
            )}

            {batch.length > 0 && (
              <div className="batch-list">
                {batch.map(item => (
                  <div key={item.id} className={`batch-item batch-${item.status}`}>
                    <span className="batch-mark">
                      {item.status === 'reading' ? <span className="spinner" /> : STATUS_MARK[item.status]}
                    </span>
                    <span className="batch-name" title={item.file.name}>{item.file.name}</span>
                    <span className="batch-detail" title={item.detail}>{item.detail}</span>
                  </div>
                ))}
                {canRetry && (
                  <div className="row-end">
                    <button className="btn btn-ghost" onClick={retryFailed}>↻ Retry failed files</button>
                  </div>
                )}
              </div>
            )}

            {warnings.length > 0 && (
              <div className="status-msg status-warning">
                <div>
                  {warnings.map((w, i) => <div key={i}>⚠ {w}</div>)}
                </div>
              </div>
            )}

            {rows.length > 0 && (
              <>
                <div className="data-table-wrap">
                  <table className="data-table">
                    <thead>
                      <tr>
                        <th>Date</th>
                        <th>Description</th>
                        <th>In / Out</th>
                        <th>Amount</th>
                        <th>VAT</th>
                        <th>Account</th>
                        <th>Check</th>
                      </tr>
                    </thead>
                    <tbody>
                      {rows.map(({ tx, origin }, i) => {
                        const check = issueSummary(tx)
                        return (
                          <tr key={i}>
                            <td>{tx.date ?? '—'}</td>
                            <td>
                              {tx.description}
                              <div className="row-origin" title={origin}>{origin}</div>
                            </td>
                            <td>
                              <span className={`badge-type ${tx.direction === 'out' ? 'badge-expense' : 'badge-revenue'}`}>
                                {tx.direction === 'out' ? 'money out' : 'money in'}
                              </span>
                            </td>
                            <td className="amount-cell">{money(tx.gross)}</td>
                            <td className="amount-cell">{money(tx.vat_posted)}</td>
                            <td>{tx.account_code} {tx.account_name ?? ''}</td>
                            <td className={check.cls} title={check.title}>{check.mark}</td>
                          </tr>
                        )
                      })}
                    </tbody>
                  </table>
                </div>

                <div className="table-actions">
                  <button
                    id="clearTableBtn"
                    className="btn btn-ghost"
                    onClick={clearTable}
                    disabled={analyzing}
                  >
                    Clear table
                  </button>
                  <button
                    id="generateTbBtn"
                    className="btn btn-green"
                    onClick={generateTrialBalance}
                    disabled={generating}
                  >
                    {generating
                      ? <><span className="spinner" /> Generating…</>
                      : '⟹ Generate Trial Balance'}
                  </button>
                </div>
              </>
            )}
          </div>
        </div>
      )}

      {/* Connector */}
      {(generating || tbResult || tbError) && (
        <div className="connector">↓</div>
      )}

      {/* ── STEP 3 — Trial Balance ──────────────────────────────────────────── */}
      {(generating || tbResult || tbError) && (
        <div className="section">
          <div className="section-header">
            <span className="step-badge badge-3">Step 3</span>
            <span className="section-title">Trial Balance</span>
            {tbResult && (
              <span className="section-sub">
                {tbResult.lines.length} account{tbResult.lines.length !== 1 ? 's' : ''}
              </span>
            )}
          </div>
          <div className="section-body">

            {generating && (
              <div className="status-msg status-processing">
                <span className="spinner" />
                Computing trial balance…
              </div>
            )}

            {tbError && (
              <div className="status-msg status-error">⚠ {tbError}</div>
            )}

            {tbResult && (
              <>
                <div className="data-table-wrap">
                  <table className="tb-table">
                    <thead>
                      <tr>
                        <th style={{ textAlign: 'left' }}>Account</th>
                        <th>Debit (£)</th>
                        <th>Credit (£)</th>
                      </tr>
                    </thead>
                    <tbody>
                      {tbResult.lines.map(line => (
                        <tr key={line.code}>
                          <td className="tb-account">{line.code} {line.name}</td>
                          <td className={Number(line.debit) > 0 ? 'debit-val' : 'empty-cell'}>
                            {Number(line.debit) > 0 ? money(line.debit) : '—'}
                          </td>
                          <td className={Number(line.credit) > 0 ? 'credit-val' : 'empty-cell'}>
                            {Number(line.credit) > 0 ? money(line.credit) : '—'}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                    <tfoot>
                      <tr className="tb-total-row">
                        <td>Totals</td>
                        <td className="debit-val">{money(tbResult.total_debits)}</td>
                        <td className="credit-val">{money(tbResult.total_credits)}</td>
                      </tr>
                    </tfoot>
                  </table>
                </div>

                <div>
                  <span className={`balance-pill ${tbResult.is_balanced ? 'pill-balanced' : 'pill-unbalanced'}`}>
                    {tbResult.is_balanced
                      ? '✓ Trial Balance Balances'
                      : `⚠ Out of Balance by £${Math.abs(Number(tbResult.total_debits) - Number(tbResult.total_credits)).toFixed(2)}`}
                  </span>
                </div>
              </>
            )}
          </div>
        </div>
      )}
    </div>
  )
}

'use client'

import { useState, useRef, useCallback } from 'react'

// ─── Types ────────────────────────────────────────────────────────────────────

interface AccountingTransaction {
  description: string
  date: string | null
  amount: number
  currency: string
  type: 'expense' | 'revenue'
  account: string
}

interface AnalyzeResult {
  success: boolean
  count: number
  data: AccountingTransaction[]
  raw_model_output?: string
  validation_error?: string
}

interface TrialBalanceLine {
  account: string
  debit: number
  credit: number
}

interface TrialBalanceResult {
  lines: TrialBalanceLine[]
  total_debits: number
  total_credits: number
  is_balanced: boolean
}

// ─── Utilities ────────────────────────────────────────────────────────────────

const fmt = (n: number) =>
  n === 0
    ? ''
    : `£${Math.abs(n).toLocaleString('en-GB', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`

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

  // Step 2 state
  const [analyzing, setAnalyzing] = useState(false)
  const [analyzeError, setAnalyzeError] = useState('')
  const [transactions, setTransactions] = useState<AccountingTransaction[]>([])

  // Step 3 state
  const [generating, setGenerating] = useState(false)
  const [tbResult, setTbResult] = useState<TrialBalanceResult | null>(null)
  const [tbError, setTbError] = useState('')

  // ── Step 1 → Step 2: call /api/analyze ─────────────────────────────────────

  const analyze = useCallback(async (formData: FormData) => {
    setAnalyzing(true)
    setAnalyzeError('')
    setTransactions([])
    setTbResult(null)
    setTbError('')
    try {
      const res = await fetch('http://localhost:8085/api/analyze', { method: 'POST', body: formData })
      if (!res.ok) {
        const detail = await res.text()
        throw new Error(`API ${res.status}: ${detail}`)
      }
      const result: AnalyzeResult = await res.json()
      if (!result.success || !result.data.length) {
        throw new Error(result.validation_error || 'No transactions extracted.')
      }
      setTransactions(result.data)
    } catch (e: unknown) {
      setAnalyzeError(e instanceof Error ? e.message : String(e))
    } finally {
      setAnalyzing(false)
    }
  }, [])

  const handlePaste = () => {
    if (!pasteText.trim()) return
    const fd = new FormData()
    fd.append('text', pasteText.trim())
    analyze(fd)
  }

  const handleManual = () => {
    const amt = parseFloat(manualAmount)
    if (!manualDesc.trim() || isNaN(amt)) return
    const verb = manualType === 'revenue' ? 'received' : 'paid'
    const fd = new FormData()
    fd.append('text', `${manualDesc.trim()} - ${verb} GBP ${Math.abs(amt)}`)
    analyze(fd)
  }

  const handleFiles = (files: FileList | null) => {
    if (!files || !files.length) return
    const fd = new FormData()
    fd.append('file', files[0])
    analyze(fd)
  }

  // ── Step 2 → Step 3: call /api/trial-balance ───────────────────────────────

  const generateTrialBalance = useCallback(async () => {
    if (!transactions.length) return
    setGenerating(true)
    setTbError('')
    setTbResult(null)
    try {
      const res = await fetch('http://localhost:8085/api/trial-balance', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(transactions),
      })
      if (!res.ok) throw new Error(`API ${res.status}: ${await res.text()}`)
      const result: TrialBalanceResult = await res.json()
      setTbResult(result)
    } catch (e: unknown) {
      setTbError(e instanceof Error ? e.message : String(e))
    } finally {
      setGenerating(false)
    }
  }, [transactions])

  // ── Pipeline step state ────────────────────────────────────────────────────
  const step1Done = transactions.length > 0
  const step2Active = analyzing || step1Done
  const step3Active = tbResult !== null || generating

  // ── Render ─────────────────────────────────────────────────────────────────

  return (
    <div className="page-wrapper">

      {/* Header */}
      <header className="header">
        <div className="header-icon">£</div>
        <div>
          <div className="header-title">UK LedgerSync</div>
          <div className="header-sub">Local Qwen AI · Trial Balance Prototype</div>
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
              ref={fileInputRef}
              style={{ display: 'none' }}
              accept=".csv,.xlsx,.xls,.txt,.pdf,.png,.jpg,.jpeg"
              onChange={e => handleFiles(e.target.files)}
            />
            <div
              id="dropzone"
              className={`dropzone ${dragOver ? 'drag-over' : ''}`}
              onClick={() => fileInputRef.current?.click()}
              onDragOver={e => { e.preventDefault(); setDragOver(true) }}
              onDragLeave={() => setDragOver(false)}
              onDrop={e => { e.preventDefault(); setDragOver(false); handleFiles(e.dataTransfer.files) }}
            >
              <div className="dropzone-icon">📥</div>
              <h3>Drop a file here or click to browse</h3>
              <p>Bank statements, VAT receipts, invoices, spreadsheets</p>
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
      {(analyzing || transactions.length > 0 || analyzeError) && (
        <div className="connector">↓</div>
      )}

      {/* ── STEP 2 — Structured Data ────────────────────────────────────────── */}
      {(analyzing || transactions.length > 0 || analyzeError) && (
        <div className="section">
          <div className="section-header">
            <span className="step-badge badge-2">Step 2</span>
            <span className="section-title">Qwen AI — Structured Output</span>
            {transactions.length > 0 && (
              <span className="section-sub">{transactions.length} transaction{transactions.length !== 1 ? 's' : ''} extracted</span>
            )}
          </div>
          <div className="section-body">

            {analyzing && (
              <div className="status-msg status-processing">
                <span className="spinner" />
                Local Qwen model is analysing your input…
              </div>
            )}

            {analyzeError && (
              <div className="status-msg status-error">
                ⚠ {analyzeError}
              </div>
            )}

            {transactions.length > 0 && (
              <>
                <div className="data-table-wrap">
                  <table className="data-table">
                    <thead>
                      <tr>
                        <th>Description</th>
                        <th>Amount</th>
                        <th>Type</th>
                        <th>Account / Category</th>
                        <th>Currency</th>
                      </tr>
                    </thead>
                    <tbody>
                      {transactions.map((tx, i) => (
                        <tr key={i}>
                          <td>{tx.description}</td>
                          <td className="amount-cell">
                            £{tx.amount.toLocaleString('en-GB', { minimumFractionDigits: 2 })}
                          </td>
                          <td>
                            <span className={`badge-type ${tx.type === 'expense' ? 'badge-expense' : 'badge-revenue'}`}>
                              {tx.type}
                            </span>
                          </td>
                          <td>{tx.account}</td>
                          <td style={{ color: 'var(--text-muted)', fontSize: '0.8rem' }}>{tx.currency || 'GBP'}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>

                <div className="row-end" style={{ marginTop: '1.25rem' }}>
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
                      {tbResult.lines.map((line, i) => (
                        <tr key={i}>
                          <td className="tb-account">{line.account}</td>
                          <td className={line.debit > 0 ? 'debit-val' : 'empty-cell'}>
                            {line.debit > 0 ? fmt(line.debit) : '—'}
                          </td>
                          <td className={line.credit > 0 ? 'credit-val' : 'empty-cell'}>
                            {line.credit > 0 ? fmt(line.credit) : '—'}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                    <tfoot>
                      <tr className="tb-total-row">
                        <td>Totals</td>
                        <td className="debit-val">{fmt(tbResult.total_debits) || '£0.00'}</td>
                        <td className="credit-val">{fmt(tbResult.total_credits) || '£0.00'}</td>
                      </tr>
                    </tfoot>
                  </table>
                </div>

                <div>
                  <span className={`balance-pill ${tbResult.is_balanced ? 'pill-balanced' : 'pill-unbalanced'}`}>
                    {tbResult.is_balanced
                      ? '✓ Trial Balance Balances'
                      : `⚠ Out of Balance by £${Math.abs(tbResult.total_debits - tbResult.total_credits).toFixed(2)}`}
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

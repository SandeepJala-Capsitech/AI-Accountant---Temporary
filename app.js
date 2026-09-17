// Application State
let transactions = [];

// DOM Elements
const dropzone = document.getElementById('dropzone');
const fileInput = document.getElementById('fileInput');
const pasteInput = document.getElementById('pasteInput');
const processPasteBtn = document.getElementById('processPasteBtn');
const manualEntryForm = document.getElementById('manualEntryForm');
const loadSampleBtn = document.getElementById('loadSampleBtn');
const clearDataBtn = document.getElementById('clearDataBtn');
const exportCsvBtn = document.getElementById('exportCsvBtn');
const tableSearch = document.getElementById('tableSearch');
const transactionTbody = document.getElementById('transactionTbody');

// UK Metric Elements
const metricCount = document.getElementById('metricCount');
const metricNetTotal = document.getElementById('metricNetTotal');
const metricVatTotal = document.getElementById('metricVatTotal');
const metricInflow = document.getElementById('metricInflow');
const metricOutflow = document.getElementById('metricOutflow');
const metricNet = document.getElementById('metricNet');
const tableCountBadge = document.getElementById('tableCountBadge');

// Tab Switching
document.querySelectorAll('.tab-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
    document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));
    
    btn.classList.add('active');
    const targetTab = btn.getAttribute('data-tab');
    document.getElementById(targetTab).classList.add('active');
  });
});

// Drag & Drop & Click Setup
dropzone.addEventListener('click', (e) => {
  if (e.target !== fileInput) {
    fileInput.click();
  }
});

['dragenter', 'dragover', 'dragleave', 'drop'].forEach(eventName => {
  dropzone.addEventListener(eventName, preventDefaults, false);
});

function preventDefaults(e) {
  e.preventDefault();
  e.stopPropagation();
}

['dragenter', 'dragover'].forEach(eventName => {
  dropzone.addEventListener(eventName, () => dropzone.classList.add('dragover'), false);
});

['dragleave', 'drop'].forEach(eventName => {
  dropzone.addEventListener(eventName, () => dropzone.classList.remove('dragover'), false);
});

dropzone.addEventListener('drop', (e) => {
  const dt = e.dataTransfer;
  const files = dt.files;
  handleFiles(files);
});

fileInput.addEventListener('change', (e) => {
  handleFiles(e.target.files);
});

// File Handling Core (Step 1 -> Step 2 Local Qwen AI Processing)
async function handleFiles(files) {
  for (const file of files) {
    await processStep1InputWithQwen(file, null, file.name);
  }
}

// Universal Step 1 -> Step 2 Local Qwen AI Pipeline
async function processStep1InputWithQwen(file, textContent, sourceName) {
  // Show processing feedback banner
  const countBadge = document.getElementById('tableCountBadge');
  if (countBadge) countBadge.textContent = '🤖 Step 2 Qwen AI Analyzing...';

  try {
    const formData = new FormData();
    if (file) {
      formData.append('file', file);
    }
    if (textContent) {
      formData.append('text', textContent);
    }

    const response = await fetch('/api/analyze', {
      method: 'POST',
      body: formData
    });

    if (!response.ok) {
      throw new Error(`API Error ${response.status}: ${await response.text()}`);
    }

    const result = await response.json();

    if (result.success && result.data && result.data.length > 0) {
      // Step 2 validated JSON payload -> Convert to Ledger items (Step 3)
      const converted = result.data.map(item => {
        const isExpense = item.type === 'expense';
        const gross = isExpense ? -Math.abs(item.amount) : Math.abs(item.amount);
        const net = +(gross / 1.2).toFixed(2);
        const vat = +(gross - net).toFixed(2);

        return {
          id: generateId(),
          date: item.date ? formatUkDate(item.date) : getUkDateStr(new Date()),
          rawDateIso: item.date || new Date().toISOString().split('T')[0],
          description: item.description,
          category: item.account,
          grossAmount: gross,
          vatRate: 20,
          vatAmount: vat,
          netAmount: net,
          type: item.type === 'expense' ? 'debit' : 'credit',
          source: `Local Qwen AI (${sourceName})`
        };
      });

      addTransactions(converted);


    } else {
      alert('Step 2 Qwen AI returned empty or unvalidated JSON for input.');
    }

  } catch (err) {
    console.error('Step 2 Qwen error, falling back to local format mapper:', err);
    // Fallback if local backend server is restarting
    if (file) {
      const ext = file.name.split('.').pop().toLowerCase();
      if (ext === 'csv') parseCSVFileFallback(file);
      else if (ext === 'xlsx' || ext === 'xls') parseExcelFileFallback(file);
    }
  } finally {
    renderMetrics();
  }
}

// 1. CSV Parser (UK Localization)
function parseCSVFile(file) {
  if (window.Papa) {
    Papa.parse(file, {
      header: true,
      skipEmptyLines: true,
      complete: (results) => {
        const parsedRows = mapRawRowsToTransactions(results.data, file.name);
        addTransactions(parsedRows);
      }
    });
  } else {
    const reader = new FileReader();
    reader.onload = (e) => {
      const text = e.target.result;
      const lines = text.split('\n').filter(l => l.trim());
      if (lines.length <= 1) return;
      
      const headers = lines[0].split(',').map(h => h.trim().replace(/^["']|["']$/g, ''));
      const rawRows = lines.slice(1).map(line => {
        const values = line.split(',').map(v => v.trim().replace(/^["']|["']$/g, ''));
        const obj = {};
        headers.forEach((h, idx) => { obj[h] = values[idx] || ''; });
        return obj;
      });
      const parsedRows = mapRawRowsToTransactions(rawRows, file.name);
      addTransactions(parsedRows);
    };
    reader.readAsText(file);
  }
}

// 2. Excel Parser
function parseExcelFile(file) {
  const reader = new FileReader();
  reader.onload = (e) => {
    try {
      const data = new Uint8Array(e.target.result);
      const workbook = XLSX.read(data, { type: 'array' });
      const firstSheetName = workbook.SheetNames[0];
      const worksheet = workbook.Sheets[firstSheetName];
      const jsonRows = XLSX.utils.sheet_to_json(worksheet, { defval: '' });
      
      const parsedRows = mapRawRowsToTransactions(jsonRows, file.name);
      addTransactions(parsedRows);
    } catch (err) {
      alert('Error parsing Excel spreadsheet: ' + err.message);
    }
  };
  reader.readAsArrayBuffer(file);
}

// 3. Image Receipt Parsing Simulation (UK Receipt)
function parseImageReceipt(file) {
  const sampleUkReceipt = [
    {
      id: generateId(),
      date: getUkDateStr(new Date()),
      rawDateIso: new Date().toISOString().split('T')[0],
      description: `UK VAT Receipt (${file.name}) - Office Supplies`,
      category: '7500 Printing & Stationery',
      grossAmount: -48.00,
      vatRate: 20,
      vatAmount: -8.00,
      netAmount: -40.00,
      type: 'debit',
      source: `UK Receipt (${file.name})`
    }
  ];
  addTransactions(sampleUkReceipt);
}

// 4. PDF Document Handling Simulation (UK Bank Statement)
function parsePDFDocument(file) {
  const sampleUkPdf = [
    {
      id: generateId(),
      date: getUkDateStr(new Date()),
      rawDateIso: new Date().toISOString().split('T')[0],
      description: `Barclays Statement (${file.name}) - HMRC VAT Payment`,
      category: '2200 HMRC VAT Settlement',
      grossAmount: -1450.00,
      vatRate: -1,
      vatAmount: 0.00,
      netAmount: -1450.00,
      type: 'debit',
      source: `UK Statement (${file.name})`
    }
  ];
  addTransactions(sampleUkPdf);
}

// Quick TSV / Paste Processing -> Step 2 Local Qwen AI Pipeline
processPasteBtn.addEventListener('click', async () => {
  const text = pasteInput.value.trim();
  if (!text) {
    alert('Please paste some text or transaction records first.');
    return;
  }

  processPasteBtn.disabled = true;
  processPasteBtn.textContent = '⏳ Qwen AI Analyzing...';

  try {
    await processStep1InputWithQwen(null, text, 'Pasted Input');
    pasteInput.value = '';
  } finally {
    processPasteBtn.disabled = false;
    processPasteBtn.textContent = 'Process & Load Rows';
  }
});

// Manual Form Submission (UK VAT Form)
// Qwen is used ONLY for account/category classification.
// All structured data (date, amount, VAT) comes directly from the form.
manualEntryForm.addEventListener('submit', async (e) => {
  e.preventDefault();
  const dateVal = document.getElementById('mDate').value;
  const desc = document.getElementById('mDesc').value;
  const grossVal = parseFloat(document.getElementById('mAmount').value);
  const vatRate = parseFloat(document.getElementById('mVatRate').value);
  const userCategory = document.getElementById('mCategory').value || '';

  if (!dateVal || !desc || isNaN(grossVal)) {
    alert('Please fill out all required fields.');
    return;
  }

  const submitBtn = manualEntryForm.querySelector('button[type="submit"]');
  const originalText = submitBtn.textContent;
  submitBtn.disabled = true;
  submitBtn.textContent = '⏳ Qwen AI Analyzing...';

  try {
    // Build natural-language sentence so Qwen can classify the account category
    const verb = grossVal >= 0 ? 'received' : 'paid';
    const nlPrompt = `${desc} - ${verb} GBP ${Math.abs(grossVal)}`;

    let resolvedCategory = userCategory;

    try {
      const formData = new FormData();
      formData.append('text', nlPrompt);
      const response = await fetch('/api/analyze', { method: 'POST', body: formData });
      if (response.ok) {
        const result = await response.json();
        if (result.success && result.data && result.data.length > 0) {
          // Use Qwen's account only if user didn't provide one
          resolvedCategory = userCategory || result.data[0].account;
        }
      }
    } catch (err) {
      console.warn('Qwen category lookup failed, using form category:', err);
    }

    // Compute VAT from form values
    let vatAmt = 0;
    let netAmt = grossVal;
    if (vatRate > 0 && grossVal !== 0) {
      const factor = 1 + (vatRate / 100);
      netAmt = +(grossVal / factor).toFixed(2);
      vatAmt = +(grossVal - netAmt).toFixed(2);
    }

    const entry = {
      id: generateId(),
      date: formatUkDate(dateVal),
      rawDateIso: dateVal,
      description: desc,
      category: resolvedCategory || 'General Expenses',
      grossAmount: grossVal,
      vatRate: vatRate,
      vatAmount: vatAmt,
      netAmount: netAmt,
      type: grossVal >= 0 ? 'credit' : 'debit',
      source: 'Manual Entry (Qwen Categorised)'
    };

    addTransactions([entry]);
    manualEntryForm.reset();
    document.getElementById('mDate').valueAsDate = new Date();

  } finally {
    submitBtn.disabled = false;
    submitBtn.textContent = originalText;
  }
});

// Load Pre-populated Sample UK Ledger (£ GBP)
loadSampleBtn.addEventListener('click', () => {
  const ukSamples = [
    {
      id: generateId(),
      date: '01/09/2026',
      rawDateIso: '2026-09-01',
      description: 'Acme UK Ltd Sales Invoice Paid',
      category: '4000 UK Sales / Revenue',
      grossAmount: 3600.00,
      vatRate: 20,
      vatAmount: 600.00,
      netAmount: 3000.00,
      type: 'credit',
      source: 'Barclays Bank CSV'
    },
    {
      id: generateId(),
      date: '02/09/2026',
      rawDateIso: '2026-09-02',
      description: 'BT Business Broadband & Phone',
      category: '7550 Telephone & Broadband',
      grossAmount: -72.00,
      vatRate: 20,
      vatAmount: -12.00,
      netAmount: -60.00,
      type: 'debit',
      source: 'Barclays Bank CSV'
    },
    {
      id: generateId(),
      date: '03/09/2026',
      rawDateIso: '2026-09-03',
      description: 'HMRC PAYE & NIC Direct Debit',
      category: '2210 HMRC PAYE/NIC',
      grossAmount: -840.00,
      vatRate: -1,
      vatAmount: 0.00,
      netAmount: -840.00,
      type: 'debit',
      source: 'Barclays Bank CSV'
    },
    {
      id: generateId(),
      date: '04/09/2026',
      rawDateIso: '2026-09-04',
      description: 'Sainsbury\'s Supermarkets (Office Tea & Coffee)',
      category: '7400 Staff Refreshments',
      grossAmount: -24.50,
      vatRate: 0,
      vatAmount: 0.00,
      netAmount: -24.50,
      type: 'debit',
      source: 'UK VAT Receipt'
    },
    {
      id: generateId(),
      date: '05/09/2026',
      rawDateIso: '2026-09-05',
      description: 'Companies House Annual Confirmation Statement',
      category: '7600 Legal & Filing Fees',
      grossAmount: -34.00,
      vatRate: -1,
      vatAmount: 0.00,
      netAmount: -34.00,
      type: 'debit',
      source: 'Gov.uk Filing'
    },
    {
      id: generateId(),
      date: '06/09/2026',
      rawDateIso: '2026-09-06',
      description: 'London Merchant Client Consulting Retainer',
      category: '4000 UK Sales / Revenue',
      grossAmount: 2400.00,
      vatRate: 20,
      vatAmount: 400.00,
      netAmount: 2000.00,
      type: 'credit',
      source: 'Excel Ledger'
    }
  ];
  addTransactions(ukSamples);
});

// Reset Data
clearDataBtn.addEventListener('click', () => {
  if (confirm('Clear all UK transaction data?')) {
    transactions = [];
    render();
  }
});

// Export UK CSV (GBP £)
exportCsvBtn.addEventListener('click', () => {
  if (transactions.length === 0) {
    alert('No data to export.');
    return;
  }
  let csvContent = 'Date (UK),Description,Category,Type,Net (£),VAT (£),Gross (£),Source\n';
  transactions.forEach(t => {
    csvContent += `"${t.date}","${t.description.replace(/"/g, '""')}","${t.category}","${t.type}",${t.netAmount.toFixed(2)},${t.vatAmount.toFixed(2)},${t.grossAmount.toFixed(2)},"${t.source}"\n`;
  });

  const blob = new Blob([csvContent], { type: 'text/csv;charset=utf-8;' });
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.setAttribute('href', url);
  link.setAttribute('download', `uk_accounting_transactions_gbp_${new Date().toISOString().split('T')[0]}.csv`);
  document.body.appendChild(link);
  link.click();
  document.body.removeChild(link);
});

// Search Filter
tableSearch.addEventListener('input', () => {
  renderTable();
});

// Helper Mappers & Utilities
function mapRawRowsToTransactions(rawRows, fileName) {
  return rawRows.map(row => {
    const keys = Object.keys(row);
    const findVal = (patterns) => {
      const matchKey = keys.find(k => patterns.some(p => k.toLowerCase().includes(p)));
      return matchKey ? row[matchKey] : '';
    };

    const rawDate = findVal(['date', 'dt', 'time']) || new Date().toISOString().split('T')[0];
    const desc = findVal(['description', 'desc', 'payee', 'memo', 'name', 'details']) || 'UK Transaction Item';
    const cat = findVal(['category', 'cat', 'account', 'class', 'code']) || 'General';
    
    const debitVal = parseFloat(findVal(['debit', 'expense', 'outflow']) || 0);
    const creditVal = parseFloat(findVal(['credit', 'income', 'inflow']) || 0);
    const rawAmt = findVal(['gross', 'amount', 'amt', 'value', 'price']);

    let gross = 0;
    if (creditVal > 0) {
      gross = creditVal;
    } else if (debitVal > 0) {
      gross = -debitVal;
    } else if (rawAmt) {
      const parsed = parseFloat(String(rawAmt).replace(/[£\$,]/g, ''));
      gross = isNaN(parsed) ? 0 : parsed;
    }

    let vatRateVal = 20;
    let netAmt = +(gross / 1.2).toFixed(2);
    let vatAmt = +(gross - netAmt).toFixed(2);

    return {
      id: generateId(),
      date: formatUkDate(rawDate),
      rawDateIso: parseToIsoDate(rawDate),
      description: desc,
      category: cat,
      grossAmount: gross,
      vatRate: vatRateVal,
      vatAmount: vatAmt,
      netAmount: netAmt,
      type: gross >= 0 ? 'credit' : 'debit',
      source: fileName
    };
  }).filter(t => t.description || t.grossAmount !== 0);
}

function addTransactions(newItems) {
  transactions = [...newItems, ...transactions];
  render();
}

function deleteRow(id) {
  transactions = transactions.filter(t => t.id !== id);
  render();
}

function render() {
  renderMetrics();
  renderTable();
  clearDataBtn.style.display = transactions.length > 0 ? 'inline-flex' : 'none';
}

function renderMetrics() {
  const count = transactions.length;
  let grossInflow = 0;
  let grossOutflow = 0;
  let totalNet = 0;
  let totalVat = 0;

  transactions.forEach(t => {
    totalNet += t.netAmount;
    totalVat += t.vatAmount;
    if (t.grossAmount >= 0) {
      grossInflow += t.grossAmount;
    } else {
      grossOutflow += Math.abs(t.grossAmount);
    }
  });

  const netPos = grossInflow - grossOutflow;

  metricCount.textContent = count;
  metricNetTotal.textContent = formatGbp(totalNet);
  metricVatTotal.textContent = formatGbp(totalVat);
  metricInflow.textContent = formatGbp(grossInflow);
  metricOutflow.textContent = formatGbp(grossOutflow);
  
  metricNet.textContent = formatGbp(netPos);
  metricNet.className = `metric-value ${netPos >= 0 ? 'inflow' : 'outflow'}`;

  tableCountBadge.textContent = `${count} ${count === 1 ? 'item' : 'items'}`;
}

function renderTable() {
  const query = tableSearch.value.trim().toLowerCase();
  const filtered = transactions.filter(t => {
    if (!query) return true;
    return (
      t.date.toLowerCase().includes(query) ||
      t.description.toLowerCase().includes(query) ||
      t.category.toLowerCase().includes(query) ||
      t.source.toLowerCase().includes(query) ||
      t.grossAmount.toString().includes(query)
    );
  });

  if (filtered.length === 0) {
    transactionTbody.innerHTML = `
      <tr>
        <td colspan="9" class="empty-state">
          ${transactions.length === 0 ? 'No UK transaction data loaded yet. Upload files, quick paste, or load sample UK data.' : 'No matching UK transactions found.'}
        </td>
      </tr>
    `;
    return;
  }

  transactionTbody.innerHTML = filtered.map(t => `
    <tr>
      <td style="font-weight: 600;">${t.date}</td>
      <td>${escapeHtml(t.description)}</td>
      <td><span style="color: var(--text-secondary); font-size: 0.85rem;">${escapeHtml(t.category)}</span></td>
      <td>
        <span class="type-badge ${t.type === 'credit' ? 'type-credit' : 'type-debit'}">${t.type}</span>
      </td>
      <td style="text-align: right; font-family: monospace;">${formatGbp(t.netAmount)}</td>
      <td style="text-align: right; font-family: monospace;">
        ${t.vatAmount !== 0 ? `<span class="vat-tag">${t.vatRate}%</span> ${formatGbp(t.vatAmount)}` : '<span style="color: var(--text-muted);">0.00</span>'}
      </td>
      <td style="text-align: right; font-weight: 700; font-family: monospace; font-size: 0.95rem; color: ${t.grossAmount >= 0 ? 'var(--success)' : 'var(--text-primary)'};">
        ${t.grossAmount >= 0 ? '+' : ''}${formatGbp(t.grossAmount)}
      </td>
      <td><span class="source-badge">${escapeHtml(t.source)}</span></td>
      <td style="text-align: center;">
        <button onclick="deleteRow('${t.id}')" style="background: none; border: none; color: var(--danger); cursor: pointer; font-size: 1.1rem;" title="Delete item">&times;</button>
      </td>
    </tr>
  `).join('');
}

function formatGbp(val) {
  const sign = val < 0 ? '-' : '';
  const absVal = Math.abs(val);
  return `${sign}£${absVal.toLocaleString('en-GB', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

function formatUkDate(str) {
  if (!str) return getUkDateStr(new Date());
  // If already DD/MM/YYYY
  if (/^\d{2}\/\d{2}\/\d{4}$/.test(str)) return str;

  const d = new Date(str);
  if (isNaN(d.getTime())) return String(str);
  return getUkDateStr(d);
}

function parseToIsoDate(str) {
  if (/^\d{4}-\d{2}-\d{2}$/.test(str)) return str;
  if (/^\d{2}\/\d{2}\/\d{4}$/.test(str)) {
    const p = str.split('/');
    return `${p[2]}-${p[1]}-${p[0]}`;
  }
  return new Date().toISOString().split('T')[0];
}

function getUkDateStr(d) {
  const day = String(d.getDate()).padStart(2, '0');
  const month = String(d.getMonth() + 1).padStart(2, '0');
  const year = d.getFullYear();
  return `${day}/${month}/${year}`;
}

function generateId() {
  return 'uk_tx_' + Math.random().toString(36).substr(2, 9);
}

function escapeHtml(str) {
  return String(str).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}



// Default today's date in manual form
document.getElementById('mDate').valueAsDate = new Date();

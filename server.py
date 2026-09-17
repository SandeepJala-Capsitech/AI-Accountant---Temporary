import os
import shutil
import tempfile
from typing import Optional, List, Dict
from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from qwen_service import QwenAccountingExtractor, TransactionExtractionResult, AccountingTransaction

app = FastAPI(
    title="UK LedgerSync API",
    description="Steps 1-3: Local Qwen AI → Structured Accounting Data → Trial Balance"
)

# Enable CORS (allows Next.js on port 3000 to call this API)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

extractor_service = QwenAccountingExtractor(model_path_or_name="./models/qwen")


# ─── Step 3 — Trial Balance Models ────────────────────────────────────────────

class TrialBalanceLine(BaseModel):
    account: str
    debit: float   # positive value when this account has a debit balance
    credit: float  # positive value when this account has a credit balance

class TrialBalanceResult(BaseModel):
    lines: List[TrialBalanceLine]
    total_debits: float
    total_credits: float
    is_balanced: bool


# ─── Health ────────────────────────────────────────────────────────────────────

@app.get("/api/health")
def health_check():
    return {
        "status": "online",
        "model_path": extractor_service.model_path,
        "is_model_loaded": extractor_service.is_loaded
    }


# ─── Step 2 — Analyze (Qwen) ──────────────────────────────────────────────────

import io
def _extract_pdf_text(contents: bytes) -> Optional[str]:
    try:
        from pdfminer.high_level import extract_text
        text = extract_text(io.BytesIO(contents))
        if text and text.strip():
            return text.strip()
            
        # If pdfminer finds no text, it's likely an image-only (scanned) PDF.
        # Render the PDF pages to images using fitz, then OCR them.
        import fitz
        doc = fitz.open(stream=contents, filetype="pdf")
        ocr_texts = []
        for page in doc:
            pix = page.get_pixmap(dpi=200)
            img_bytes = pix.tobytes("png")
            page_text = _extract_image_text(img_bytes)
            if page_text:
                ocr_texts.append(page_text)
                
        full_text = "\n".join(ocr_texts)
        return full_text.strip() if full_text.strip() else None
        
    except Exception as e:
        print(f"PDF extraction error: {e}")
        return None

ocr_engine = None
def get_ocr_engine():
    global ocr_engine
    if ocr_engine is None:
        try:
            import easyocr
            # EasyOCR will automatically use CUDA if available, else CPU.
            ocr_engine = easyocr.Reader(['en'], verbose=False)
        except Exception as e:
            print(f"EasyOCR load error: {e}")
            return None
    return ocr_engine

def _extract_image_text(contents: bytes) -> Optional[str]:
    try:
        engine = get_ocr_engine()
        if not engine:
            return None
        
        # EasyOCR can read bytes directly
        result = engine.readtext(contents)
        print(f"DEBUG - EasyOCR Raw Result: {result}")
        text_lines = []
        for line in result:
            # Format is [([box_coords], 'text', confidence), ...]
            text_lines.append(line[1])
            
        text = "\n".join(text_lines)
        print(f"DEBUG - EasyOCR Joined Text: {text}")
        return text.strip() if text and text.strip() else None
    except Exception as e:
        print(f"Image extraction error: {e}")
        return None


@app.post("/api/analyze", response_model=TransactionExtractionResult)
async def analyze_transaction(
    text: Optional[str] = Form(None),
    file: Optional[UploadFile] = File(None)
):
    """
    Step 2 Smart Router Endpoint:
    1. CSV / Excel / Text  -> parsed to plain text -> Qwen fallback / model
    2. PDF                 -> pdfminer text extraction -> Qwen fallback / model
    3. Image (PNG/JPG/etc) -> pytesseract OCR -> Qwen fallback / model
                          OR -> Qwen-VL vision model if loaded
    """
    temp_file_path = None
    extracted_text_from_file = None

    try:
        if file:
            ext = os.path.splitext(file.filename)[1].lower()
            contents = await file.read()

            # ── Tabular text files ──────────────────────────────────────────
            if ext in ['.csv', '.txt', '.tsv']:
                extracted_text_from_file = contents.decode('utf-8', errors='ignore')

            # ── Excel ───────────────────────────────────────────────────────
            elif ext in ['.xlsx', '.xls']:
                try:
                    import pandas as pd
                    import io as _io
                    df = pd.read_excel(_io.BytesIO(contents))
                    extracted_text_from_file = df.to_string(index=False)
                except Exception:
                    extracted_text_from_file = contents.decode('utf-8', errors='ignore')

            # ── PDF — extract text first ─────────────────────────────────
            elif ext == '.pdf':
                extracted_text_from_file = _extract_pdf_text(contents)
                if not extracted_text_from_file:
                    # Save for Qwen-VL if model is loaded
                    with tempfile.NamedTemporaryFile(delete=False, suffix=ext) as tmp:
                        tmp.write(contents)
                        temp_file_path = tmp.name

            # ── Images — OCR first, then Qwen-VL ────────────────────────
            elif ext in ['.png', '.jpg', '.jpeg', '.webp', '.bmp', '.tiff']:
                extracted_text_from_file = _extract_image_text(contents)
                if not extracted_text_from_file:
                    # Save for Qwen-VL if model is loaded
                    with tempfile.NamedTemporaryFile(delete=False, suffix=ext) as tmp:
                        tmp.write(contents)
                        temp_file_path = tmp.name

            else:
                raise HTTPException(status_code=415, detail=f"Unsupported file type: {ext}")

        combined_text = text or extracted_text_from_file

        # If we have no text AND no image for the model, return a clear error
        if not combined_text and not temp_file_path:
            raise HTTPException(
                status_code=422,
                detail=(
                    "Could not extract text from the uploaded file. "
                    "For image receipts, please ensure Tesseract OCR is installed, "
                    "or paste the receipt text manually in Quick Paste."
                )
            )

        result = extractor_service.extract_accounting_data(
            text_input=combined_text,
            image_path=temp_file_path
        )

        if not result.success:
            raise HTTPException(status_code=422, detail=f"Failed to produce valid JSON: {result.validation_error}")

        return result

    finally:
        if temp_file_path and os.path.exists(temp_file_path):
            try:
                os.remove(temp_file_path)
            except Exception:
                pass


# ─── Step 3 — Trial Balance ───────────────────────────────────────────────────

@app.post("/api/trial-balance", response_model=TrialBalanceResult)
def generate_trial_balance(transactions: List[AccountingTransaction]):
    """
    Step 3 — Trial Balance Generator.

    Debit/Credit treatment (basic double-entry):
      - expense transactions → Debit side
      - revenue transactions → Credit side

    Groups by account name, sums amounts, checks if trial balance balances.
    """
    account_map: Dict[str, Dict[str, float]] = {}

    for tx in transactions:
        acc = tx.account or "Uncategorised"
        if acc not in account_map:
            account_map[acc] = {"debit": 0.0, "credit": 0.0}

        if tx.type.lower() == "expense":
            account_map[acc]["debit"] += tx.amount
        else:
            account_map[acc]["credit"] += tx.amount

    lines = [
        TrialBalanceLine(account=acc, debit=round(v["debit"], 2), credit=round(v["credit"], 2))
        for acc, v in sorted(account_map.items())
    ]

    total_debits = round(sum(l.debit for l in lines), 2)
    total_credits = round(sum(l.credit for l in lines), 2)

    return TrialBalanceResult(
        lines=lines,
        total_debits=total_debits,
        total_credits=total_credits,
        is_balanced=abs(total_debits - total_credits) < 0.01
    )


# ─── Static Frontend (legacy HTML) ────────────────────────────────────────────
current_dir = os.path.dirname(os.path.abspath(__file__))
app.mount("/", StaticFiles(directory=current_dir, html=True), name="static")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="0.0.0.0", port=8085, reload=True)

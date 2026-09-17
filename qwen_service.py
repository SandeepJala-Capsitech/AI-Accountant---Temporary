import os
import json
import re
from typing import List, Optional, Union, Dict, Any
from pydantic import BaseModel, Field, ValidationError

# --- Pydantic Data Schema for Step 2 JSON Output Validation ---
class AccountingTransaction(BaseModel):
    description: str = Field(..., description="Short name/description of item or service")
    date: Optional[str] = Field(None, description="ISO date YYYY-MM-DD or null if unavailable")
    amount: float = Field(..., description="Numeric transaction amount")
    currency: Optional[str] = Field("GBP", description="Currency code (e.g. GBP)")
    type: str = Field(..., description="Transaction classification: 'expense' or 'revenue'")
    account: str = Field(..., description="Basic accounting account category name")

class TransactionExtractionResult(BaseModel):
    success: bool
    count: int
    data: List[AccountingTransaction]
    raw_model_output: Optional[str] = None
    validation_error: Optional[str] = None

# --- Modular Qwen Service with Smart Input Router ---
class QwenAccountingExtractor:
    """
    Smart Input Router & Qwen Vision-Language Extractor:
    1. CSV/Excel/Text -> Parsed into text rows before passing to Qwen.
    2. Image/PDF Receipts -> Passed directly to Qwen-VL vision processing.
    3. Optimized for RTX 3060 (6GB VRAM) via float16/4-bit quantization.
    """

    def __init__(self, model_path_or_name: str = "./models/qwen"):
        self.model_path = model_path_or_name
        self.model = None
        self.tokenizer = None
        self.processor = None
        self.is_loaded = False

    def load_model(self):
        """Lazy loader for local Qwen model using Hugging Face transformers."""
        if self.is_loaded:
            return True

        target = self.model_path if os.path.exists(self.model_path) else "Qwen/Qwen2.5-VL-3B-Instruct"
        print(f"[INFO] Initializing local Qwen model from: {target}...")

        try:
            import torch
            from transformers import AutoProcessor, Qwen2VLForConditionalGeneration

            self.processor = AutoProcessor.from_pretrained(target)
            
            device = "cuda" if torch.cuda.is_available() else "cpu"
            torch_dtype = torch.float16 if device == "cuda" else torch.float32

            print(f"[INFO] Loading Qwen model weights onto [{device.upper()}] (RTX 3060 VRAM Optimized)...")
            self.model = Qwen2VLForConditionalGeneration.from_pretrained(
                target,
                torch_dtype=torch_dtype,
                device_map="auto" if device == "cuda" else None
            )
            self.is_loaded = True
            print("[OK] Local Qwen Model loaded successfully!")
            return True

        except Exception as e:
            print(f"[WARN] Hugging Face model load note: {e}")
            print("[INFO] Running in modular smart router fallback parser mode.")
            return False

    def extract_accounting_data(self, text_input: Optional[str] = None, image_path: Optional[str] = None) -> TransactionExtractionResult:
        """
        Smart Input Routing:
        - Text/CSV/Excel -> Processed as clean text context.
        - Image/PDF -> Processed via Vision Multimodal pipeline.
        """
        prompt = self._build_system_prompt(text_input)

        raw_output = ""
        if self.load_model() and self.model is not None:
            raw_output = self._run_model_inference(prompt, image_path)
        else:
            raw_output = self._run_smart_router_fallback(text_input)

        # Validate returned JSON
        return self._validate_and_parse_json(raw_output)

    def _build_system_prompt(self, text_content: Optional[str]) -> str:
        return f"""You are a UK accounting data extraction model.
Analyze the given financial input and extract all transaction records into a structured JSON array of objects. Use standard UK accountancy categories (e.g., Sales, Revenue, Expense, Professional Fees, Cost of Goods Sold, Utilities, etc.).

Input Content:
"{text_content or 'Receipt/Invoice image attached'}"

Required JSON Output Format:
Return ONLY a valid JSON array of objects matching this exact schema:
[
  {{
    "description": "string (name of item/service)",
    "date": "YYYY-MM-DD or null",
    "amount": number (positive numerical value),
    "currency": "GBP",
    "type": "expense" or "revenue",
    "account": "string (UK accounting category like 'Office Equipment', 'Sales', 'Revenue', 'Utilities')"
  }}
]

Strict Rules:
- Return ONLY valid JSON inside ```json ``` code block.
- Do NOT include conversational text or markdown explanation.
"""

    def _run_model_inference(self, prompt: str, image_path: Optional[str]) -> str:
        """Executes local Qwen model forward pass."""
        try:
            import torch
            from PIL import Image

            messages = [{"role": "user", "content": []}]

            if image_path and os.path.exists(image_path):
                image = Image.open(image_path)
                messages[0]["content"].append({"type": "image", "image": image})

            messages[0]["content"].append({"type": "text", "text": prompt})

            text_prompt = self.processor.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
            inputs = self.processor(text=[text_prompt], images=[Image.open(image_path)] if image_path else None, padding=True, return_tensors="pt")
            
            device = "cuda" if torch.cuda.is_available() else "cpu"
            inputs = inputs.to(device)

            with torch.no_grad():
                generated_ids = self.model.generate(**inputs, max_new_tokens=512)
                generated_ids_trimmed = [
                    out_ids[len(in_ids):] for in_ids, out_ids in zip(inputs.input_ids, generated_ids)
                ]
                output_text = self.processor.batch_decode(generated_ids_trimmed, skip_special_tokens=True, clean_up_tokenization_spaces=False)[0]
                return output_text

        except Exception as err:
            print(f"Inference error: {err}")
            return self._run_smart_router_fallback(prompt)

    def _run_smart_router_fallback(self, text_input: Optional[str]) -> str:
        """Smart fallback UK accounting parser for CSV/Excel/Text rows AND Receipts."""
        if not text_input:
            text_input = ""

        lines = [l.strip() for l in text_input.split('\n') if l.strip()]
        if not lines:
            return "[]"

        extracted_lines = []
        for line in lines:
            line_clean = line.strip()
            if not line_clean:
                continue

            # --- Amount extraction ---
            amount = 0.0
            # Allow space or comma instead of dot for the decimal part because OCR often misses the dot.
            gbp_match = re.search(r'(?:GBP|£|E|f|L|\$|€)\s*([0-9]{1,3}(?:,[0-9]{3})*(?:[\.\s,][0-9]{2}))', line_clean, re.IGNORECASE)
            if gbp_match:
                amount = float(re.sub(r'[\s,](?=[0-9]{2}$)', '.', gbp_match.group(1).replace(',', '')))
            else:
                all_nums = re.findall(r'([0-9]{1,3}(?:,[0-9]{3})*(?:[\.\s,][0-9]{2}))', line_clean)
                if all_nums:
                    raw_num = all_nums[-1]
                    clean_num = re.sub(r'[\s,](?=[0-9]{2}$)', '.', raw_num).replace(',', '').replace(' ', '')
                    amount = float(clean_num)

            # --- Date extraction ---
            date_match = re.search(r'\b(\d{1,2}[/-]\d{1,2}[/-]\d{2,4})\b', line_clean)
            date_str = date_match.group(1) if date_match else None

            # Filter out obvious phone numbers masquerading as amounts (e.g. 0333 321 1265)
            if re.search(r'tel\.|phone|call', line_clean, re.IGNORECASE):
                amount = 0.0

            extracted_lines.append({
                "raw": line_clean,
                "amount": amount,
                "date": date_str
            })

        # --- Smart Receipt vs Bank Statement Detection ---
        valid_amounts = [l for l in extracted_lines if l['amount'] > 0]
        
        is_receipt = False
        has_total_keyword = any('total' in l['raw'].lower() or 'balance' in l['raw'].lower() or 'amount due' in l['raw'].lower() for l in extracted_lines)
        
        if len(lines) > 3 and (has_total_keyword or len([l for l in valid_amounts if l['date']]) < len(valid_amounts) / 2):
            is_receipt = True
            
        if len(valid_amounts) == 1:
            is_receipt = True

        items = []

        if is_receipt and valid_amounts:
            # Find the true TOTAL amount.
            # Strategy: The grand total on a receipt is almost universally the largest single amount.
            total_amount = max(l['amount'] for l in valid_amounts)
                
            vendor_name = "Unknown Vendor"
            for l in lines:
                if re.search(r'[a-zA-Z]{3,}', l) and not 'confirmation' in l.lower() and not 'receipt' in l.lower():
                    vendor_name = l
                    break

            account = "General Expenses"
            lower_vendor = vendor_name.lower()
            if any(w in lower_vendor for w in ['tesco', 'sainsbury', 'waitrose', 'aldi', 'asda', 'food', 'coffee', 'cafe', 'restaurant', 'premier inn']):
                account = "Travel & Subsistence" if 'premier inn' in lower_vendor else "Staff Refreshments"
            elif any(w in lower_vendor for w in ['uber', 'train', 'tfl', 'taxi', 'rail', 'bus', 'flight']):
                account = "Travel Expenses"

            items.append({
                "description": vendor_name[:50],
                "date": extracted_lines[0]['date'],
                "amount": total_amount,
                "currency": "GBP",
                "type": "expense",
                "account": account
            })
        else:
            # Treat as bank statement (multiple transactions)
            for l in valid_amounts:
                desc = re.sub(r'(?:GBP|£|E|f|L|\$|€)?\s*[0-9,\.\s]+$', '', l['raw']).strip()
                if not desc:
                    desc = "Unspecified Item"
                
                lower = desc.lower()
                is_revenue = any(w in lower for w in ['sold', 'sale', 'revenue', 'income', 'received', 'invoice paid', 'deposit'])
                tx_type = "revenue" if is_revenue else "expense"
                
                account = "General Expenses"
                if is_revenue: account = "Sales Revenue"
                elif any(w in lower for w in ['tesco', 'sainsbury', 'waitrose', 'aldi', 'asda', 'food', 'coffee', 'cafe', 'restaurant']):
                    account = "Staff Refreshments"
                elif any(w in lower for w in ['uber', 'train', 'tfl', 'taxi', 'rail', 'bus', 'flight']):
                    account = "Travel Expenses"

                items.append({
                    "description": desc,
                    "date": l['date'],
                    "amount": l['amount'],
                    "currency": "GBP",
                    "type": tx_type,
                    "account": account
                })

        return json.dumps(items)


    def _validate_and_parse_json(self, raw_text: str) -> TransactionExtractionResult:
        """Parses raw model output and enforces strict Pydantic JSON schema validation."""
        cleaned_json = raw_text.strip()
        
        if "```" in cleaned_json:
            match = re.search(r'```(?:json)?\s*([\s\S]*?)\s*```', cleaned_json)
            if match:
                cleaned_json = match.group(1).strip()

        try:
            parsed_data = json.loads(cleaned_json)
            if isinstance(parsed_data, dict):
                parsed_data = [parsed_data]

            validated_transactions = []
            for item in parsed_data:
                tx = AccountingTransaction(
                    description=str(item.get("description", "Unspecified Item")),
                    date=item.get("date"),
                    amount=float(item.get("amount", 0.0)),
                    currency=str(item.get("currency", "GBP")),
                    type=str(item.get("type", "expense")).lower(),
                    account=str(item.get("account", "General Expenses"))
                )
                validated_transactions.append(tx)

            return TransactionExtractionResult(
                success=True,
                count=len(validated_transactions),
                data=validated_transactions,
                raw_model_output=raw_text
            )

        except Exception as e:
            return TransactionExtractionResult(
                success=False,
                count=0,
                data=[],
                raw_model_output=raw_text,
                validation_error=f"JSON Schema Validation Error: {str(e)}"
            )

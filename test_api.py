"""
Test script for the UK LedgerSync API - exercises all 3 input modes.
"""
import urllib.request
import json
import uuid

BASE_URL = "http://localhost:8085"


def post_text(text):
    boundary = uuid.uuid4().hex
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="text"\r\n\r\n'
        f"{text}\r\n"
        f"--{boundary}--\r\n"
    ).encode("utf-8")
    req = urllib.request.Request(
        f"{BASE_URL}/api/analyze",
        data=body,
        method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())


def run_tests():
    tests = [
        # Quick Paste / File input - full unstructured sentences
        ("Quick Paste: consulting revenue",     "Consulting services sold to ACME Corp for 2400"),
        ("Quick Paste: broadband bill",         "BT Business Broadband monthly bill 72.00"),
        ("Quick Paste: HMRC VAT payment",       "HMRC VAT settlement payment 1450.00"),
        ("Quick Paste: sales invoice",          "Invoice paid - London Merchant client retainer 2000"),
        # Manual Entry - natural language (description + verb + amount) as the fixed handler now sends
        ("Manual Entry: office chair expense",  "Office chair - paid GBP 500"),
        ("Manual Entry: consulting revenue",    "Consulting services for ACME Corp - received GBP 2400"),
        ("Manual Entry: broadband expense",     "BT Business Broadband - paid GBP 72"),
    ]

    print("=" * 65)
    print("  UK LedgerSync - Qwen AI Pipeline Test")
    print("=" * 65)

    all_pass = True
    for label, text in tests:
        print(f"\n[TEST] {label}")
        print(f"  Input: {text!r}")
        try:
            result = post_text(text)
            if result.get("success") and result.get("data"):
                for item in result["data"]:
                    print(f"  -> description : {item['description']}")
                    print(f"     amount      : {item['amount']}  ({item.get('currency','GBP')})")
                    print(f"     type        : {item['type']}")
                    print(f"     account     : {item['account']}")
                print("  [PASS]")
            else:
                print(f"  [FAIL] success=False or empty data. validation_error={result.get('validation_error')}")
                all_pass = False
        except Exception as e:
            print(f"  [ERROR] {e}")
            all_pass = False

    print("\n" + "=" * 65)
    print("  ALL TESTS PASSED" if all_pass else "  SOME TESTS FAILED - see above")
    print("=" * 65)


if __name__ == "__main__":
    run_tests()

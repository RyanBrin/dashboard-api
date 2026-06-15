"""Unit tests for income classification logic. Run standalone — no FastAPI needed."""
import sys

_PFC_MAP = {
    "FOOD_AND_DRINK_RESTAURANTS":  "Food / Drinks",
    "SHOPPING_ELECTRONICS":        "Tech / Best Buy",
    "INCOME_PAYROLL":              "Income",
    "INCOME_OTHER_INCOME":         "Income",
    "LOAN_PAYMENTS_CREDIT_CARD_PAYMENT": "Credit Card Payments",
}
_LEGACY_CAT_MAP = {
    "payroll":      "Income",
    "deposit":      "Income",
    "coffee shop":  "Food / Drinks",
    "shops":        "Shopping / Lifestyle",
    "subscription": "Apps / Subscriptions",
    "payment":      "Credit Card Payments",
    "transfer":     "Transfers / Unclear",
}
_KEYWORD_RULES = [
    (["best buy", "newegg"], "Tech / Best Buy"),
    (["starbucks", "dunkin"], "Food / Drinks"),
    (["spotify", "netflix"],  "Apps / Subscriptions"),
]
_INCOME_KEYWORDS = {
    "payroll", "direct deposit", "paycheck", "salary", "wages",
    "employer deposit", "ach credit", "dd", "dir dep",
}
_PURCHASE_SIGNALS = {
    "pos purchase", "pos debit", "check card", "debit card",
    "card purchase", "card transaction", "purchase", "debit purchase",
}
_KNOWN_PAYCHECK_EMPLOYERS = {
    "bismarck park",
    "best buy",
}


def _is_income_transaction(txn: dict) -> tuple:
    if txn.get("pending"):
        return False, ""
    pfc       = (txn.get("personal_finance_category") or "").upper()
    cat       = (txn.get("category") or "").lower()
    name      = (txn.get("name") or "").lower()
    amount    = float(txn.get("amount", 0))
    acct_type = (txn.get("account_type") or "").lower()
    is_credit = amount < 0

    if any(k in pfc for k in ("INCOME_PAYROLL", "INCOME_WAGES")):
        return True, "paycheck"
    if any(k in pfc for k in ("INCOME_DIVIDENDS", "INCOME_OTHER_INCOME", "INCOME_")):
        return True, "deposit"
    if "payroll" in cat:
        return True, "paycheck"
    if cat in ("deposit",) and is_credit:
        return True, "deposit"
    for kw in _INCOME_KEYWORDS:
        if kw in name:
            return True, "paycheck"
    for _employer in _KNOWN_PAYCHECK_EMPLOYERS:
        if _employer in name:
            if any(sig in name for sig in _PURCHASE_SIGNALS):
                break
            if is_credit and acct_type in ("depository", "checking", "savings", "cash", ""):
                return True, "paycheck"
            if any(k in pfc for k in ("INCOME", "PAYROLL")):
                return True, "paycheck"
    if is_credit and acct_type in ("depository", "checking", "savings", "cash", ""):
        if any(k in name for k in ("deposit", "credit", "refund", "return")):
            if any(k in name for k in ("refund", "return", "adjustment", "reversal")):
                return True, "refund"
            return True, "deposit"
    return False, ""


def _assign_category(txn: dict) -> str:
    pfc  = (txn.get("personal_finance_category") or "").upper().replace(" ", "_")
    cat  = (txn.get("category") or "").lower()
    name = ((txn.get("merchant_name") or txn.get("name") or "")).lower()
    # High-priority: Best Buy purchase always wins over legacy "shops" category
    if "best buy" in name:
        return "Tech / Best Buy"
    for key, bc in _PFC_MAP.items():
        if key in pfc:
            return bc
    for key, bc in _LEGACY_CAT_MAP.items():
        if key in cat:
            return bc
    for keywords, bc in _KEYWORD_RULES:
        if any(kw in name for kw in keywords):
            return bc
    return "Other"


def _classify_transaction(txn: dict) -> dict:
    is_income, income_type = _is_income_transaction(txn)
    if is_income:
        txn.update(is_income=True, income_type=income_type,
                   excluded_from_budget=True, exclusion_reason="income", budget_category="")
        return txn
    if txn.get("pending"):
        txn.update(is_income=False, income_type="", excluded_from_budget=True,
                   exclusion_reason="pending", budget_category="")
        return txn
    pfc  = (txn.get("personal_finance_category") or "").upper()
    cat  = (txn.get("category") or "").lower()
    name = (txn.get("name") or "").lower()
    if any(k in pfc for k in ("TRANSFER_IN", "TRANSFER_OUT", "ACCOUNT_TRANSFER")):
        txn.update(is_income=False, income_type="", excluded_from_budget=True,
                   exclusion_reason="transfer", budget_category=""); return txn
    if "transfer" in cat:
        txn.update(is_income=False, income_type="", excluded_from_budget=True,
                   exclusion_reason="transfer", budget_category=""); return txn
    if any(k in name for k in ("zelle", "venmo", "cash app", "cashapp")):
        txn.update(is_income=False, income_type="", excluded_from_budget=True,
                   exclusion_reason="transfer", budget_category=""); return txn
    if "CREDIT_CARD_PAYMENT" in pfc or "LOAN_PAYMENTS_CREDIT_CARD" in pfc:
        txn.update(is_income=False, income_type="", excluded_from_budget=True,
                   exclusion_reason="credit_card_payment", budget_category=""); return txn
    if "payment" in cat and "credit" in name:
        txn.update(is_income=False, income_type="", excluded_from_budget=True,
                   exclusion_reason="credit_card_payment", budget_category=""); return txn
    if float(txn.get("amount", 0)) < 0:
        txn.update(is_income=False, income_type="", excluded_from_budget=True,
                   exclusion_reason="credit_to_account", budget_category=""); return txn
    txn.update(is_income=False, income_type="", excluded_from_budget=False,
               exclusion_reason="", budget_category=_assign_category(txn))
    return txn


PASS = 0
FAIL = 0


def check(label, txn, expect_income, expect_cat=None, expect_income_type=None):
    global PASS, FAIL
    t = dict(txn)
    _classify_transaction(t)
    ok = (t["is_income"] == expect_income)
    if expect_cat is not None:
        ok = ok and (t["budget_category"] == expect_cat)
    if expect_income_type is not None:
        ok = ok and (t["income_type"] == expect_income_type)
    if ok:
        print(f"  [PASS] {label}")
        PASS += 1
    else:
        print(f"  [FAIL] {label}")
        print(f"         is_income={t['is_income']} (expected {expect_income})")
        print(f"         income_type={t['income_type']!r} (expected {expect_income_type!r})")
        print(f"         budget_category={t['budget_category']!r} (expected {expect_cat!r})")
        print(f"         exclusion_reason={t['exclusion_reason']!r}")
        FAIL += 1


print("=== Bismarck Park (Pebble Creek) payroll ===")
check("Bismarck Park District deposit -> paycheck",
      {"name": "Bismarck Park District", "amount": -850.0,
       "account_type": "depository", "personal_finance_category": "", "category": "", "pending": False},
      True, expect_income_type="paycheck")

check("Bismarck Park with purchase signal -> not income",
      {"name": "Bismarck Park Pos Purchase", "amount": 25.00,
       "account_type": "depository", "personal_finance_category": "", "category": "", "pending": False},
      False)

check("Bismarck Park pending -> not income",
      {"name": "Bismarck Park District", "amount": -850.0,
       "account_type": "depository", "personal_finance_category": "", "category": "", "pending": True},
      False)

print()
print("=== Best Buy payroll vs purchase ===")
check("BB direct deposit (neg, depository) -> paycheck",
      {"name": "Best Buy Direct Deposit", "amount": -1234.56,
       "account_type": "depository", "personal_finance_category": "", "category": "", "pending": False},
      True, expect_income_type="paycheck")

check("BB POS Purchase (pos, depository) -> Tech/Best Buy spending",
      {"name": "Best Buy POS Purchase", "amount": 49.99,
       "account_type": "depository", "personal_finance_category": "", "category": "", "pending": False},
      False, "Tech / Best Buy")

check("BB Check Card (purchase signal) -> Tech/Best Buy spending",
      {"name": "Best Buy Check Card 1234", "amount": 89.99,
       "account_type": "depository", "personal_finance_category": "", "category": "shops", "pending": False},
      False, "Tech / Best Buy")

check("BB PFC=INCOME_PAYROLL -> paycheck",
      {"name": "Best Buy Payroll", "amount": -2000.0,
       "account_type": "depository", "personal_finance_category": "INCOME_PAYROLL", "category": "", "pending": False},
      True, expect_income_type="paycheck")

check("BB on credit account SHOPPING_ELECTRONICS -> Tech/Best Buy",
      {"name": "Best Buy", "amount": 299.99,
       "account_type": "credit", "personal_finance_category": "SHOPPING_ELECTRONICS", "category": "shops", "pending": False},
      False, "Tech / Best Buy")

check("BB positive on credit (no purchase signal) -> Tech/Best Buy via keyword",
      {"name": "Best Buy", "amount": 15.00,
       "account_type": "credit", "personal_finance_category": "", "category": "shops", "pending": False},
      False, "Tech / Best Buy")

print()
print("=== General income ===")
check("Payroll direct deposit keyword -> paycheck",
      {"name": "Payroll Direct Deposit", "amount": -1800.0,
       "account_type": "depository", "personal_finance_category": "", "category": "", "pending": False},
      True, expect_income_type="paycheck")

check("ACH Credit employer deposit -> paycheck",
      {"name": "ACH Credit Employer Deposit", "amount": -950.0,
       "account_type": "depository", "personal_finance_category": "", "category": "", "pending": False},
      True, expect_income_type="paycheck")

check("PFC INCOME_PAYROLL -> paycheck",
      {"name": "Employer Co Payroll", "amount": -2100.0,
       "account_type": "depository", "personal_finance_category": "INCOME_PAYROLL", "category": "", "pending": False},
      True, expect_income_type="paycheck")

check("Wages keyword -> paycheck",
      {"name": "Wages from Company", "amount": -500.0,
       "account_type": "depository", "personal_finance_category": "", "category": "", "pending": False},
      True)

print()
print("=== Exclusions (not counted as spending OR income) ===")
check("Pending transaction -> excluded",
      {"name": "Best Buy Direct Deposit", "amount": -1000.0,
       "account_type": "depository", "personal_finance_category": "", "category": "", "pending": True},
      False)

check("Zelle transfer -> excluded transfer",
      {"name": "Zelle Payment From Ryan", "amount": -200.0,
       "account_type": "depository", "personal_finance_category": "", "category": "transfer", "pending": False},
      False)

check("CC payment -> excluded",
      {"name": "Chase Credit Card Payment", "amount": 500.0,
       "account_type": "depository", "personal_finance_category": "LOAN_PAYMENTS_CREDIT_CARD_PAYMENT",
       "category": "payment", "pending": False},
      False)

print()
print("=== Spending correctly classified ===")
check("Starbucks -> Food / Drinks",
      {"name": "Starbucks", "amount": 6.50, "account_type": "depository",
       "personal_finance_category": "", "category": "coffee shop", "pending": False},
      False, "Food / Drinks")

check("Spotify -> Apps / Subscriptions",
      {"name": "Spotify USA", "amount": 9.99, "account_type": "depository",
       "personal_finance_category": "", "category": "subscription", "pending": False},
      False, "Apps / Subscriptions")

check("Generic POS on depository -> Other (no match)",
      {"name": "Some Unknown Merchant", "amount": 23.00, "account_type": "depository",
       "personal_finance_category": "", "category": "", "pending": False},
      False, "Other")

print()
print(f"Results: {PASS} passed, {FAIL} failed")
if FAIL:
    sys.exit(1)

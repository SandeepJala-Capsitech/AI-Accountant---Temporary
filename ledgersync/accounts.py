"""UK chart of accounts (Sage 50-style nominal codes). Every transaction posts to one of these,
and the AI picks from this list instead of inventing account names."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional

from .money import VatTreatment as V


class AccountType(str, Enum):
    ASSET = "asset"
    LIABILITY = "liability"
    EQUITY = "equity"
    INCOME = "income"
    EXPENSE = "expense"


@dataclass(frozen=True)
class Account:
    code: str
    name: str
    type: AccountType
    vat: V              # usual rate, to check VAT a document shows; none is booked when none is shown
    definition: str     # what belongs here, in plain words: the model chooses by it
    reclaim_vat: bool = True   # False: VAT on a purchase here can't be reclaimed, so it stays in the cost


_A, _L, _E, _I, _X = (AccountType.ASSET, AccountType.LIABILITY, AccountType.EQUITY,
                      AccountType.INCOME, AccountType.EXPENSE)

CHART: tuple[Account, ...] = (
    Account("0030", "Office Equipment", _A, V.STANDARD, "Computers, printers and other equipment kept for over a year."),
    Account("0031", "Office Equipment Depreciation", _A, V.OUTSIDE_SCOPE,
            "Office equipment's value used up over the years (accumulated depreciation)."),
    Account("0040", "Furniture and Fixtures", _A, V.STANDARD, "Desks, chairs, shelving and fittings for the premises."),
    Account("0041", "Furniture and Fixtures Depreciation", _A, V.OUTSIDE_SCOPE,
            "Furniture and fixtures' value used up over the years (accumulated depreciation)."),
    Account("0050", "Cars", _A, V.STANDARD,
            "Cars bought for the business; VAT on a car can't be reclaimed unless it is used only for business.",
            reclaim_vat=False),
    Account("0051", "Cars Depreciation", _A, V.OUTSIDE_SCOPE,
            "Cars' value used up over the years (accumulated depreciation)."),
    Account("0055", "Vans", _A, V.STANDARD, "Vans and other commercial vehicles bought for the business."),
    Account("0056", "Vans Depreciation", _A, V.OUTSIDE_SCOPE,
            "Vans' value used up over the years (accumulated depreciation)."),
    Account("1100", "Debtors", _A, V.OUTSIDE_SCOPE, "Money customers owe for invoices the business has sent and not yet been paid."),
    Account("1103", "Prepayments", _A, V.OUTSIDE_SCOPE, "Costs paid in advance that belong to a later period."),
    Account("1200", "Bank Current Account", _A, V.OUTSIDE_SCOPE, "The business's main bank account."),
    Account("1201", "Bank Current Account 2", _A, V.OUTSIDE_SCOPE, "The business's second current account."),
    Account("1202", "Bank Current Account 3", _A, V.OUTSIDE_SCOPE, "The business's third current account."),
    Account("1210", "Bank Deposit Account", _A, V.OUTSIDE_SCOPE, "A savings account; moving money into it is a transfer, not spending."),
    Account("1211", "Bank Deposit Account 2", _A, V.OUTSIDE_SCOPE,
            "A second savings account; moving money into it is a transfer, not spending."),
    Account("1230", "Petty Cash", _A, V.OUTSIDE_SCOPE, "Cash withdrawn to keep for small purchases."),
    Account("1240", "Company Credit Card", _L, V.OUTSIDE_SCOPE,
            "The business's credit card; paying the card off from the bank is a transfer, not spending."),
    Account("1241", "Company Credit Card 2", _L, V.OUTSIDE_SCOPE,
            "The business's second credit card; paying it off from the bank is a transfer."),
    Account("2100", "Creditors", _L, V.OUTSIDE_SCOPE, "Money the business owes suppliers for bills it has received and not yet paid."),
    Account("2109", "Accruals", _L, V.OUTSIDE_SCOPE, "Costs for the period that have not been billed or paid yet."),
    Account("2110", "Expenses Owed to Staff", _L, V.OUTSIDE_SCOPE, "Expense claims approved for staff and not yet reimbursed."),
    Account("2200", "Sales VAT", _L, V.OUTSIDE_SCOPE, "VAT charged to customers."),
    Account("2201", "Purchase VAT", _L, V.OUTSIDE_SCOPE, "VAT paid to suppliers."),
    Account("2202", "VAT Liability", _L, V.OUTSIDE_SCOPE, "VAT paid to, or refunded by, HMRC for VAT returns."),
    Account("2210", "PAYE and National Insurance", _L, V.OUTSIDE_SCOPE, "Employees' income tax and National Insurance paid to HMRC."),
    Account("2220", "Net Wages", _L, V.OUTSIDE_SCOPE,
            "Net pay owed to staff, from a payroll report; cleared by the bank payments to them."),
    Account("2230", "Pension Fund", _L, V.OUTSIDE_SCOPE, "Pension contributions owed to the pension provider."),
    Account("2250", "Director's Loan Account", _L, V.OUTSIDE_SCOPE,
            "Money a director has lent the company, or taken from it for personal use. Limited companies only."),
    Account("2320", "Corporation Tax", _L, V.OUTSIDE_SCOPE,
            "Corporation tax owed to, or paid to, HMRC. Limited companies only."),
    Account("2300", "Loans", _L, V.OUTSIDE_SCOPE, "Money borrowed on a business loan, or repaid (not the interest)."),
    Account("3000", "Ordinary Shares", _E, V.OUTSIDE_SCOPE,
            "Money shareholders paid in for shares in the company. Limited companies only."),
    Account("3010", "Capital Introduced", _E, V.OUTSIDE_SCOPE, "Money the owners put into the business."),
    Account("3100", "Owner's Capital Account", _E, V.OUTSIDE_SCOPE,
            "The owners' stake in the business, where each year's profit is closed."),
    Account("3200", "Retained Earnings", _E, V.OUTSIDE_SCOPE,
            "Profits kept in the company from earlier years, where each year's profit is closed."),
    Account("3250", "Dividends", _E, V.OUTSIDE_SCOPE,
            "Dividends paid out to the company's shareholders. Limited companies only."),
    Account("3260", "Drawings", _E, V.OUTSIDE_SCOPE, "Money the owner takes out for personal use."),
    Account("4000", "Sales", _I, V.STANDARD, "Money from customers for the business's goods or services."),
    Account("4900", "Other Income", _I, V.STANDARD, "Income that is not from sales, such as bank interest received."),
    Account("4904", "Rent Income", _I, V.EXEMPT,
            "Rent from tenants of property the business lets, paid directly or through a letting agent."),
    Account("5000", "Purchases", _X, V.STANDARD, "Goods bought to resell or to make products that are sold; not things the business uses."),
    Account("6201", "Advertising and Marketing", _X, V.STANDARD,
            "Advertising, marketing, promotions, online ads and sponsorship."),
    Account("7000", "Gross Wages", _X, V.OUTSIDE_SCOPE, "Salaries and wages paid to employees."),
    Account("7001", "Directors' Salaries", _X, V.OUTSIDE_SCOPE, "Salaries paid to the company's directors. Limited companies only."),
    Account("7006", "Employer's National Insurance", _X, V.OUTSIDE_SCOPE,
            "National Insurance the employer pays on its staff's wages."),
    Account("7007", "Employer's Pension Contributions", _X, V.OUTSIDE_SCOPE,
            "Pension contributions the employer pays for its staff."),
    Account("7100", "Rent", _X, V.STANDARD, "Rent for offices, desks or other premises."),
    Account("7103", "Business Rates", _X, V.OUTSIDE_SCOPE, "Business rates paid to the local council."),
    Account("7200", "Electricity", _X, V.STANDARD, "Electricity for the premises."),
    Account("7201", "Gas", _X, V.STANDARD, "Gas for the premises."),
    Account("7300", "Fuel and Oil", _X, V.STANDARD, "Fuel for business vehicles."),
    Account("7302", "Vehicle Licences", _X, V.OUTSIDE_SCOPE, "Vehicle tax for business vehicles."),
    Account("7400", "Travel", _X, V.ZERO, "Train, bus, air and taxi fares, and parking, when travelling for work."),
    Account("7402", "Hotels", _X, V.STANDARD, "Places to stay when travelling for work."),
    Account("7403", "Entertainment", _X, V.STANDARD,
            "Meals and hospitality for clients or other guests; VAT on it can't be reclaimed.", reclaim_vat=False),
    Account("7406", "Subsistence", _X, V.STANDARD, "Meals, snacks and drinks bought while working, from cafés, restaurants or takeaways."),
    Account("7500", "Printing", _X, V.STANDARD, "Printing and copying done by others."),
    Account("7501", "Postage and Carriage", _X, V.EXEMPT, "Stamps, parcels, couriers and delivery charges."),
    Account("7502", "Telephone and Internet", _X, V.STANDARD, "Phone, mobile and broadband services."),
    Account("7504", "Office Stationery", _X, V.STANDARD, "Paper, pens, ink and other small office supplies."),
    Account("7600", "Legal Fees", _X, V.STANDARD, "Solicitors, legal advice and company filing fees."),
    Account("7601", "Accountancy Fees", _X, V.STANDARD, "Accountants, bookkeeping and audit."),
    Account("7602", "Consultancy Fees", _X, V.STANDARD, "Consultants and contractors working for the business."),
    Account("7603", "Professional Fees", _X, V.STANDARD, "Other professional services, such as surveyors or recruiters."),
    Account("7700", "Equipment Hire", _X, V.STANDARD, "Renting or leasing equipment."),
    Account("7800", "Repairs and Renewals", _X, V.STANDARD, "Repairing and maintaining premises or equipment, including tools, hardware and building supplies."),
    Account("7801", "Cleaning", _X, V.STANDARD, "Cleaning services and supplies for the premises."),
    Account("7900", "Bank Interest Paid", _X, V.EXEMPT, "Interest charged on overdrafts or loans."),
    Account("7901", "Bank Charges", _X, V.EXEMPT, "Bank account fees and card processing fees."),
    Account("8000", "Depreciation", _X, V.OUTSIDE_SCOPE, "The cost of equipment and vehicles used up in the year."),
    Account("8200", "Donations", _X, V.OUTSIDE_SCOPE, "Gifts to charities."),
    Account("8250", "Fines and Penalties", _X, V.OUTSIDE_SCOPE,
            "Parking, speeding and other fines and penalties; not tax-deductible."),
    Account("8201", "Subscriptions and Software", _X, V.STANDARD, "Software, online services, memberships and publications paid for regularly."),
    Account("8203", "Training", _X, V.STANDARD, "Courses and professional development."),
    Account("8204", "Insurance", _X, V.EXEMPT, "Business insurance premiums."),
    Account("8205", "Refreshments", _X, V.ZERO, "Tea, coffee, milk, snacks and groceries for the workplace, such as from a supermarket."),
    Account("8800", "Corporation Tax Charge", _X, V.OUTSIDE_SCOPE,
            "The year's corporation tax as a cost. Limited companies only."),
    Account("9998", "Suspense", _L, V.OUTSIDE_SCOPE, "Anything that cannot be placed yet; a person will check it."),
)

BY_CODE: dict[str, Account] = {a.code: a for a in CHART}
BANK, SALES_VAT, PURCHASE_VAT, SUSPENSE = "1200", "2200", "2201", "9998"
DEBTORS, CREDITORS, STAFF_EXPENSES = "1100", "2100", "2110"
DIRECTORS_LOAN, CAPITAL, DRAWINGS = "2250", "3010", "3260"
SHARES, DIVIDENDS, RETAINED, OWNERS_CAPITAL = "3000", "3250", "3200", "3100"
NET_WAGES, PAYE, PENSION = "2220", "2210", "2230"
WAGES = frozenset({"7000", "7001"})   # Gross Wages, Directors' Salaries: booked from the payroll report
VAT_LIABILITY = "2202"
HOTELS = "7402"
# The bank accounts and cards a client may have, in the order they are added. Only a client's own are offered.
BANK_SLOTS: dict[str, tuple[str, ...]] = {"current": ("1200", "1201", "1202"), "savings": ("1210", "1211"),
                                          "credit_card": ("1240", "1241"), "petty_cash": ("1230",)}
ALL_BANK_CODES = frozenset(code for codes in BANK_SLOTS.values() for code in codes)
# The VAT accounts, which the trial balance lists last, after every other account.
VAT_ACCOUNTS = frozenset({SALES_VAT, PURCHASE_VAT, VAT_LIABILITY})
# Picked by the ledger, never chosen for a row: the VAT control accounts (the ledger splits VAT itself), the
# accounts for what is owed (from the document type), and the accounts only year-end work posts to (depreciation,
# accruals, prepayments, the tax charge and where profit closes). The bank accounts, 1200 too, are the other side of
# each posting; a row is coded to one only as a transfer from another of the client's own (see choosable).
_LEDGER_ONLY = frozenset({SALES_VAT, PURCHASE_VAT, DEBTORS, CREDITORS, STAFF_EXPENSES,
                          "0031", "0041", "0051", "0056", "1103", "2109", OWNERS_CAPITAL, RETAINED, "8000", "8800"})
# Accounts that exist for a limited company only, and those that exist for everyone else.
_COMPANY_ONLY = frozenset({DIRECTORS_LOAN, "2320", SHARES, DIVIDENDS, "7001", "8800"})
_NOT_COMPANY = frozenset({CAPITAL, DRAWINGS})


def model_accounts() -> tuple[Account, ...]:
    """Every account a row can be coded to, whatever the kind of business and its bank accounts."""
    return tuple(a for a in CHART if a.code not in _LEDGER_ONLY)


def choosable(business_type: Optional[str] = None, bank_codes: tuple[str, ...] = ()) -> tuple[Account, ...]:
    """The accounts offered for one client. A limited company's owners go through the director's loan account,
    shares and dividends; anyone else's through Drawings and Capital Introduced, as when the kind of business
    isn't known (an analysis without a client). Of the bank accounts and cards only the client's own are
    offered (petty cash always), 1200 too, for a transfer from another of them; without a client, the first deposit
    account too."""
    hidden = _NOT_COMPANY if business_type == "limited_company" else _COMPANY_ONLY
    offered_banks = set(bank_codes) if bank_codes else {"1210"}
    hidden = hidden | (ALL_BANK_CODES - offered_banks - {"1230"})
    return tuple(a for a in model_accounts() if a.code not in hidden)


def reserve_for(business_type: str) -> str:
    """Where a year's profit closes: retained earnings for a company or LLP, the owners' capital otherwise."""
    return RETAINED if business_type in ("limited_company", "llp") else OWNERS_CAPITAL

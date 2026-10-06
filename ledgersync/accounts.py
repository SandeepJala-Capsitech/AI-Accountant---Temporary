"""UK chart of accounts (Sage 50-style nominal codes). Every transaction posts to one of these,
and the AI picks from this list instead of inventing account names."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

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


_A, _L, _E, _I, _X = (AccountType.ASSET, AccountType.LIABILITY, AccountType.EQUITY,
                      AccountType.INCOME, AccountType.EXPENSE)

CHART: tuple[Account, ...] = (
    Account("0030", "Office Equipment", _A, V.STANDARD, "Computers, printers and other equipment kept for over a year."),
    Account("0040", "Furniture and Fixtures", _A, V.STANDARD, "Desks, chairs, shelving and fittings for the premises."),
    Account("0050", "Motor Vehicles", _A, V.STANDARD, "Cars and vans bought for the business."),
    Account("1100", "Debtors", _A, V.OUTSIDE_SCOPE, "Money customers owe for invoices the business has sent and not yet been paid."),
    Account("1200", "Bank Current Account", _A, V.OUTSIDE_SCOPE, "The business's main bank account."),
    Account("1210", "Bank Deposit Account", _A, V.OUTSIDE_SCOPE, "A savings account; moving money into it is a transfer, not spending."),
    Account("1230", "Petty Cash", _A, V.OUTSIDE_SCOPE, "Cash withdrawn to keep for small purchases."),
    Account("2100", "Creditors", _L, V.OUTSIDE_SCOPE, "Money the business owes suppliers for bills it has received and not yet paid."),
    Account("2200", "Sales VAT", _L, V.OUTSIDE_SCOPE, "VAT charged to customers."),
    Account("2201", "Purchase VAT", _L, V.OUTSIDE_SCOPE, "VAT paid to suppliers."),
    Account("2202", "VAT Liability", _L, V.OUTSIDE_SCOPE, "VAT paid to, or refunded by, HMRC for VAT returns."),
    Account("2210", "PAYE and National Insurance", _L, V.OUTSIDE_SCOPE, "Employees' income tax and National Insurance paid to HMRC."),
    Account("2300", "Loans", _L, V.OUTSIDE_SCOPE, "Money borrowed on a business loan, or repaid (not the interest)."),
    Account("3000", "Capital Introduced", _E, V.OUTSIDE_SCOPE, "Money the owners put into the business."),
    Account("3260", "Drawings", _E, V.OUTSIDE_SCOPE, "Money the owner takes out for personal use."),
    Account("4000", "Sales", _I, V.STANDARD, "Money from customers for the business's goods or services."),
    Account("4900", "Other Income", _I, V.STANDARD, "Income that is not from sales, such as bank interest received."),
    Account("5000", "Purchases", _X, V.STANDARD, "Goods bought to resell or to make products that are sold; not things the business uses."),
    Account("7000", "Gross Wages", _X, V.OUTSIDE_SCOPE, "Salaries and wages paid to employees."),
    Account("7100", "Rent", _X, V.STANDARD, "Rent for offices, desks or other premises."),
    Account("7103", "Business Rates", _X, V.OUTSIDE_SCOPE, "Business rates paid to the local council."),
    Account("7200", "Electricity", _X, V.STANDARD, "Electricity for the premises."),
    Account("7201", "Gas", _X, V.STANDARD, "Gas for the premises."),
    Account("7300", "Fuel and Oil", _X, V.STANDARD, "Fuel for business vehicles."),
    Account("7302", "Vehicle Licences", _X, V.OUTSIDE_SCOPE, "Vehicle tax for business vehicles."),
    Account("7400", "Travel", _X, V.ZERO, "Train, bus, air and taxi fares, and parking, when travelling for work."),
    Account("7402", "Hotels", _X, V.STANDARD, "Places to stay when travelling for work."),
    Account("7403", "Entertainment", _X, V.STANDARD, "Meals and hospitality for clients or other guests."),
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
    Account("8200", "Donations", _X, V.OUTSIDE_SCOPE, "Gifts to charities."),
    Account("8201", "Subscriptions and Software", _X, V.STANDARD, "Software, online services, memberships and publications paid for regularly."),
    Account("8203", "Training", _X, V.STANDARD, "Courses and professional development."),
    Account("8204", "Insurance", _X, V.EXEMPT, "Business insurance premiums."),
    Account("8205", "Refreshments", _X, V.ZERO, "Tea, coffee, milk, snacks and groceries for the workplace, such as from a supermarket."),
    Account("9998", "Suspense", _L, V.OUTSIDE_SCOPE, "Anything that cannot be placed yet; a person will check it."),
)

BY_CODE: dict[str, Account] = {a.code: a for a in CHART}
BANK, SALES_VAT, PURCHASE_VAT, SUSPENSE = "1200", "2200", "2201", "9998"
DEBTORS, CREDITORS = "1100", "2100"


def choosable() -> tuple[Account, ...]:
    """Accounts a model may pick for a transaction: every account except the bank (the other
    side of each posting), the VAT control accounts (the ledger splits VAT itself), and Debtors
    and Creditors (every document is booked as paid through the bank, so nothing is left owing)."""
    return tuple(a for a in CHART if a.code not in (BANK, SALES_VAT, PURCHASE_VAT, DEBTORS, CREDITORS))

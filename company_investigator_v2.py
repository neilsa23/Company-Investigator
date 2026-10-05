
"""
Company Investigator — Version 1.13
A local Python company-analysis engine.

Usage:
    python company_investigator.py RPI
    python company_investigator.py RR.L

Optional API:
    Set ALPHAVANTAGE_API_KEY in your environment.
    Alpha Vantage supports company overview, income statement,
    balance sheet, cash flow, earnings and share-count data.
"""

import argparse
import json
import math
import os
import statistics
import time
import urllib.parse
import urllib.request
import urllib.error
from datetime import datetime, timezone
from dataclasses import dataclass, asdict, field
from typing import Any, Dict, List, Optional


# -----------------------------
# Configuration
# -----------------------------

WEIGHTS = {
    "business_quality": 15,
    "growth": 20,
    "profitability": 15,
    "financial_health": 15,
    "cash_generation": 10,
    "shareholder_alignment": 10,
    "valuation": 15,
}


def clamp(value, low=0.0, high=1.0):
    return max(low, min(high, value))


def safe_num(x):
    try:
        if x in (None, "", "None", "null", "N/A", "NoneType"):
            return None
        return float(x)
    except (ValueError, TypeError):
        return None


def cagr(start, end, years):
    if start is None or end is None or years <= 0 or start <= 0 or end <= 0:
        return None
    return (end / start) ** (1 / years) - 1


def score_growth(revenue_cagr, eps_cagr=None):
    vals = [x for x in (revenue_cagr, eps_cagr) if x is not None]
    if not vals:
        return 0.50
    g = statistics.mean(vals)
    # 0% = 0.40, 10% = 0.65, 20% = 0.85, 30%+ = 1.00
    return clamp(0.40 + g / 0.40)


def score_margin(margin):
    if margin is None:
        return 0.50
    return clamp((margin + 0.05) / 0.35)


def score_balance(net_debt_to_ebitda=None, current_ratio=None):
    scores = []
    if net_debt_to_ebitda is not None:
        if net_debt_to_ebitda <= 0:
            scores.append(1.0)
        elif net_debt_to_ebitda <= 1:
            scores.append(0.90)
        elif net_debt_to_ebitda <= 2:
            scores.append(0.75)
        elif net_debt_to_ebitda <= 3:
            scores.append(0.55)
        elif net_debt_to_ebitda <= 4:
            scores.append(0.30)
        else:
            scores.append(0.10)
    if current_ratio is not None:
        scores.append(clamp((current_ratio - 0.5) / 1.5))
    return statistics.mean(scores) if scores else 0.50


def score_valuation(pe=None, ps=None, fcf_yield=None):
    scores = []
    if pe is not None and pe > 0:
        # Deliberately moderate: high-growth companies shouldn't be killed by P/E alone.
        scores.append(clamp(1.0 - (pe - 10) / 70))
    if ps is not None and ps > 0:
        scores.append(clamp(1.0 - (ps - 2) / 18))
    if fcf_yield is not None:
        scores.append(clamp((fcf_yield + 0.02) / 0.12))
    return statistics.mean(scores) if scores else 0.50


def fetch_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "CompanyInvestigator/1.0"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode("utf-8"))


class AlphaVantageError(RuntimeError):
    """Clear, user-facing Alpha Vantage API failure."""


def normalise_input_symbol(symbol):
    """Normalise common Trading 212/LSE ticker formats for Alpha Vantage.

    Alpha Vantage's documented London suffix is .LON. Users commonly enter
    .L in UK brokerage apps, so we accept both and send .LON to the API.
    """
    raw = str(symbol or "").strip().upper()
    if raw.endswith(".L"):
        return raw[:-2] + ".LON"
    return raw


def _api_error_message(data):
    if not isinstance(data, dict):
        return None
    for key in ("Error Message", "Information", "Note"):
        value = data.get(key)
        if value:
            return str(value)
    return None


_ALPHA_VANTAGE_MIN_INTERVAL = 1.2
_ALPHA_VANTAGE_LAST_REQUEST = 0.0


def _respect_alpha_vantage_rate_limit():
    """Keep free Alpha Vantage requests safely above the 1 req/sec limit."""
    global _ALPHA_VANTAGE_LAST_REQUEST
    now = time.monotonic()
    wait = _ALPHA_VANTAGE_MIN_INTERVAL - (now - _ALPHA_VANTAGE_LAST_REQUEST)
    if wait > 0:
        time.sleep(wait)
    _ALPHA_VANTAGE_LAST_REQUEST = time.monotonic()


def alpha_vantage(symbol, function, api_key):
    symbol = normalise_input_symbol(symbol)
    _respect_alpha_vantage_rate_limit()
    q = urllib.parse.urlencode({
        "function": function,
        "symbol": symbol,
        "apikey": api_key,
    })
    url = "https://www.alphavantage.co/query?" + q
    try:
        data = fetch_json(url)
    except Exception as exc:
        raise AlphaVantageError(f"Alpha Vantage request failed for {function}: {exc}") from exc
    message = _api_error_message(data)
    if message:
        raise AlphaVantageError(f"Alpha Vantage {function} for {symbol}: {message}")
    return data


def symbol_search(keywords, api_key):
    """Find the best Alpha Vantage symbol for a bare ticker/company name."""
    q = urllib.parse.urlencode({
        "function": "SYMBOL_SEARCH",
        "keywords": str(keywords or "").strip().upper(),
        "apikey": api_key,
    })
    try:
        data = fetch_json("https://www.alphavantage.co/query?" + q)
    except Exception as exc:
        raise AlphaVantageError(f"Symbol search failed: {exc}") from exc
    message = _api_error_message(data)
    if message:
        raise AlphaVantageError(f"Alpha Vantage symbol search: {message}")
    matches = data.get("bestMatches", []) if isinstance(data, dict) else []
    if not matches:
        return None
    # Prefer an exact symbol match, then London Stock Exchange, then the
    # highest match score.
    wanted = str(keywords or "").strip().upper()
    def rank(item):
        sym = str(item.get("1. symbol", "")).upper()
        region = str(item.get("4. region", "")).lower()
        exact = 1 if sym == wanted else 0
        london = 1 if ("london" in region or sym.endswith(".LON")) else 0
        try:
            score = float(item.get("9. matchScore", 0) or 0)
        except (TypeError, ValueError):
            score = 0
        return (exact, london, score)
    best = max(matches, key=rank)
    return str(best.get("1. symbol") or "").upper() or None



def fx_rate_to_gbp(from_currency, api_key):
    """V2 FX layer: free public reference-rate service first, Alpha fallback."""
    currency = normalise_currency(from_currency)
    if not currency or currency == "GBP":
        return 1.0 if currency == "GBP" else None, "Native GBP", None

    # Avoid spending Alpha Vantage quota merely to convert valuation figures.
    try:
        url = f"https://api.frankfurter.dev/v2/rate/{urllib.parse.quote(currency.lower())}/gbp"
        data = _http_json(url)
        rate = safe_num(data.get("rate"))
        timestamp = data.get("date")
        if rate:
            return rate, "Frankfurter public FX", timestamp
    except Exception:
        pass

    if api_key:
        q = urllib.parse.urlencode({
            "function": "CURRENCY_EXCHANGE_RATE",
            "from_currency": currency,
            "to_currency": "GBP",
            "apikey": api_key,
        })
        try:
            _respect_alpha_vantage_rate_limit()
            data = fetch_json("https://www.alphavantage.co/query?" + q)
            quote = data.get("Realtime Currency Exchange Rate", {})
            rate = safe_num(quote.get("5. Exchange Rate"))
            timestamp = quote.get("6. Last Refreshed")
            if rate:
                return rate, "Alpha Vantage CURRENCY_EXCHANGE_RATE fallback", timestamp
        except Exception:
            pass
    return None, "FX unavailable", None


def normalise_price_currency(price, price_currency):
    """Convert LSE GBX/pence quotes to GBP while preserving the original quote."""
    currency = normalise_currency(price_currency)
    if currency == "GBP":
        return price
    return gbp_price_from_gbx(price) if str(price_currency).upper() in ("GBX", "GBPENCE", "PENCE", "P") else price


def convert_value(value, from_currency, to_currency, fx_cache):
    """
    Convert a financial value using cached FX rates.
    For V1.11 the valuation currency is the share-price currency after
    GBX -> GBP normalisation.
    """
    if value is None:
        return None

    src = normalise_currency(from_currency)
    dst = normalise_currency(to_currency)

    if not src or not dst or src == dst:
        return value

    # Base conversion through GBP.
    src_to_gbp = fx_cache.get(src)
    dst_to_gbp = fx_cache.get(dst)

    if src_to_gbp is None or dst_to_gbp in (None, 0):
        return None

    return value * src_to_gbp / dst_to_gbp


def normalise_financials_for_valuation(metrics, api_key):
    """
    Put per-company valuation inputs into GBP when possible.

    This is deliberately explicit: the raw reported figures remain available,
    while valuation inputs are converted consistently.
    """
    financial_currency = normalise_currency(metrics.get("Financial currency"))
    price_currency = normalise_currency(metrics.get("Price currency")) or "GBP"

    # Price currency is always normalised to GBP for UK/LSE quotes.
    valuation_currency = "GBP" if price_currency == "GBP" else price_currency

    currencies = {financial_currency, valuation_currency}
    currencies.discard(None)

    fx_cache = {}
    fx_source = {}
    fx_timestamp = None

    for currency in currencies:
        rate, source, timestamp = fx_rate_to_gbp(currency, api_key)
        if rate is not None:
            fx_cache[currency] = rate
            fx_source[currency] = source
            fx_timestamp = timestamp or fx_timestamp

    # UK-listed prices are represented in GBP after GBX normalisation.
    raw_price = metrics.get("Raw share price")
    price_currency_original = metrics.get("Price currency")
    if raw_price is not None and str(price_currency_original).upper() in (
        "GBX", "GBPENCE", "PENCE", "P"
    ):
        metrics["Share price"] = gbp_price_from_gbx(raw_price)
        metrics["Price currency"] = "GBP"

    # Convert all valuation-sensitive statement figures from reporting
    # currency into GBP. This is the critical fix for companies such as RPI.
    fields = [
        "Revenue TTM",
        "EPS",
        "Free cash flow",
        "Market cap",
        "Net debt",
        "Shares outstanding",
    ]

    converted = False
    if financial_currency:
        for field in fields:
            value = metrics.get(field)
            if value is None:
                continue

            # Shares are a count, not a monetary value.
            if field == "Shares outstanding":
                continue

            new_value = convert_value(
                value, financial_currency, "GBP", fx_cache
            )
            if new_value is not None:
                metrics[field] = new_value
                converted = True

    # EPS and FCF are now GBP; shares remain a pure count.
    # Market cap from Alpha Vantage may be in the financial currency, so it
    # receives the same conversion.
    metrics["Valuation currency"] = "GBP"
    metrics["FX conversion applied"] = bool(converted)
    metrics["FX source"] = "; ".join(
        f"{k}: {v}" for k, v in fx_source.items()
    ) or "None / native GBP"
    metrics["FX timestamp"] = fx_timestamp
    metrics["Financial currency (reported)"] = financial_currency

    # Recalculate ratios that depend on converted absolute values.
    fcf = metrics.get("Free cash flow")
    market_cap = metrics.get("Market cap")
    if fcf is not None and market_cap and market_cap > 0:
        metrics["FCF yield"] = fcf / market_cap

    eps = metrics.get("EPS")
    price = metrics.get("Share price")
    if eps is not None and eps > 0 and price is not None:
        metrics["P/E"] = price / eps

    return metrics





# Currency handling
# Financial statements can be reported in USD/EUR/etc while the share price
# may be quoted in GBX. V1.10 keeps the original figures and also creates a
# normalized valuation currency where possible.

CURRENCY_ALIASES = {
    "GBX": "GBP",
    "GBp": "GBP",
    "pence": "GBP",
    "p": "GBP",
    "USD": "USD",
    "GBP": "GBP",
    "EUR": "EUR",
}


def normalise_currency(currency):
    if not currency:
        return None
    return CURRENCY_ALIASES.get(str(currency), str(currency).upper())


def gbp_price_from_gbx(price):
    if price is None:
        return None
    # Alpha Vantage / LSE feeds often represent UK prices in GBX.
    return price / 100.0


def convert_to_gbp(value, currency, fx_to_gbp=None):
    if value is None:
        return None
    currency = normalise_currency(currency)
    if currency == "GBP":
        return value
    if currency == "USD" and fx_to_gbp:
        return value * fx_to_gbp
    if currency == "EUR" and fx_to_gbp:
        return value * fx_to_gbp
    return None


def estimate_data_confidence(metrics, valuation, overview, income, balance, cash):
    """
    Confidence is based on data completeness and consistency.
    It is deliberately conservative.
    """
    points = 0
    total = 10

    if metrics.get("Revenue TTM") is not None:
        points += 1
    if metrics.get("EPS") is not None:
        points += 1
    if metrics.get("Free cash flow") is not None:
        points += 1
    if metrics.get("Shares outstanding") is not None:
        points += 1
    if metrics.get("Share price") is not None:
        points += 1
    if metrics.get("Revenue CAGR") is not None:
        points += 1
    if len(income.get("annualReports", [])) >= 3:
        points += 1
    if len(cash.get("annualReports", [])) >= 3:
        points += 1
    if len(balance.get("annualReports", [])) >= 3:
        points += 1
    if valuation.get("base_fair_value") is not None:
        points += 1

    if metrics.get("FX conversion applied") or (
        normalise_currency(metrics.get("Financial currency")) == "GBP"
    ):
        points += 1
        total = 11
    else:
        total = 11

    ratio = points / total
    if ratio >= 0.80:
        return "HIGH"
    if ratio >= 0.60:
        return "MEDIUM"
    return "LOW"


def dcf_fair_value(fcf, shares, growth, discount_rate=0.10, terminal_growth=0.025, years=10):
    """Simple FCFF-style DCF. Returns value per share when inputs are available."""
    if not all(x is not None for x in (fcf, shares)) or shares <= 0 or fcf <= 0:
        return None
    growth = max(-0.10, min(growth, 0.35))
    discount_rate = max(0.06, min(discount_rate, 0.20))
    terminal_growth = max(0.0, min(terminal_growth, discount_rate - 0.01))

    pv = 0.0
    current = fcf
    for year in range(1, years + 1):
        # Fade growth gradually toward terminal growth.
        year_growth = growth + (terminal_growth - growth) * ((year - 1) / max(years - 1, 1))
        current *= (1 + year_growth)
        pv += current / ((1 + discount_rate) ** year)

    terminal = current * (1 + terminal_growth) / (discount_rate - terminal_growth)
    pv_terminal = terminal / ((1 + discount_rate) ** years)
    return (pv + pv_terminal) / shares


def multiple_fair_value(eps, target_pe):
    if eps is None or eps <= 0 or target_pe <= 0:
        return None
    return eps * target_pe


def fcf_multiple_fair_value(fcf, shares, target_fcf_yield):
    if fcf is None or shares is None or shares <= 0 or target_fcf_yield <= 0:
        return None
    return (fcf / target_fcf_yield) / shares


def scenario_growth(base_growth, scenario):
    """Return conservative/base/aggressive growth assumption."""
    if base_growth is None:
        base_growth = 0.10
    if scenario == "bear":
        return max(-0.05, base_growth - 0.10)
    if scenario == "bull":
        return min(0.35, base_growth + 0.10)
    return base_growth




def trend_values(annual_reports, field, limit=5):
    values = []
    for report in annual_reports[:limit]:
        value = safe_num(report.get(field))
        if value is not None:
            values.append(value)
    return values


def pct_change(new, old):
    if old is None or old == 0 or new is None:
        return None
    return (new / old) - 1



def build_historical_trends(income, balance, cash):
    """
    Creates compact chronological annual trend series for the UI.
    Alpha Vantage returns newest annual report first, so reverse it.
    """
    income_reports = list(reversed(income.get("annualReports", [])[:8]))
    cash_reports = list(reversed(cash.get("annualReports", [])[:8]))
    balance_reports = list(reversed(balance.get("annualReports", [])[:8]))

    years = []
    revenue = []
    net_income = []
    eps = []
    operating_cf = []
    fcf = []
    debt = []
    cash_balance = []
    shares = []

    def add_report_date(report):
        date = str(report.get("fiscalDateEnding", ""))
        return date[:4] if date else None

    all_dates = []
    for r in income_reports:
        y = add_report_date(r)
        if y and y not in all_dates:
            all_dates.append(y)

    for y in all_dates:
        years.append(y)

        ir = next((r for r in income_reports if add_report_date(r) == y), {})
        cr = next((r for r in cash_reports if add_report_date(r) == y), {})
        br = next((r for r in balance_reports if add_report_date(r) == y), {})

        revenue.append(safe_num(ir.get("totalRevenue")))
        net_income.append(safe_num(ir.get("netIncome")))
        eps.append(safe_num(ir.get("reportedEPS")))
        ocf = safe_num(cr.get("operatingCashflow"))
        capex = safe_num(cr.get("capitalExpenditures"))
        operating_cf.append(ocf)
        fcf.append((ocf - abs(capex)) if ocf is not None and capex is not None else None)
        debt.append(safe_num(br.get("shortLongTermDebtTotal")))
        cash_balance.append(safe_num(br.get("cashAndCashEquivalentsAtCarryingValue")))
        shares.append(safe_num(ir.get("weightedAverageShsOutDil")))

    return {
        "years": years,
        "revenue": revenue,
        "net_income": net_income,
        "eps": eps,
        "operating_cash_flow": operating_cf,
        "free_cash_flow": fcf,
        "debt": debt,
        "cash": cash_balance,
        "shares": shares,
    }


def red_flag_engine(overview, income, balance, cash, metrics):
    """
    Looks for deterioration, accounting-quality concerns and balance-sheet risk.
    This deliberately produces warnings rather than declaring fraud or wrongdoing.
    """
    flags = []
    positives = []
    annual = income.get("annualReports", [])
    cash_reports = cash.get("annualReports", [])
    balance_reports = balance.get("annualReports", [])

    revenue = trend_values(annual, "totalRevenue")
    net_income = trend_values(annual, "netIncome")
    eps = trend_values(annual, "reportedEPS")
    op_cf = trend_values(cash_reports, "operatingCashflow")
    capex = trend_values(cash_reports, "capitalExpenditures")
    shares = trend_values(annual, "weightedAverageShsOutDil")
    debt = trend_values(balance_reports, "shortLongTermDebtTotal")
    cash_bal = trend_values(balance_reports, "cashAndCashEquivalentsAtCarryingValue")

    # Revenue trend.
    if len(revenue) >= 3:
        rev_change = pct_change(revenue[0], revenue[-1])
        if rev_change is not None and rev_change > 0.20:
            positives.append(f"Revenue has grown strongly over the available history ({rev_change:.1%})")
        elif rev_change is not None and rev_change < -0.10:
            flags.append(f"Revenue has fallen materially over the available history ({rev_change:.1%})")

    # Earnings vs operating cash flow.
    if len(net_income) >= 3 and len(op_cf) >= 3:
        ni_change = pct_change(net_income[0], net_income[-1])
        cf_change = pct_change(op_cf[0], op_cf[-1])
        if ni_change is not None and cf_change is not None:
            if ni_change > 0.20 and cf_change < 0.05:
                flags.append("Earnings have grown materially faster than operating cash flow")
            if ni_change > 0 and cf_change > ni_change:
                positives.append("Operating cash flow is growing faster than reported earnings")

    # Negative operating cash flow.
    if op_cf:
        recent_cf = op_cf[0]
        if recent_cf < 0:
            flags.append("Latest reported operating cash flow is negative")
        elif len(op_cf) >= 3 and all(x < 0 for x in op_cf[:3]):
            flags.append("Operating cash flow has been negative for multiple periods")

    # FCF consistency.
    if len(op_cf) >= 3 and len(capex) >= 3:
        fcf_series = []
        for ocf, cx in zip(op_cf[:3], capex[:3]):
            fcf_series.append(ocf - abs(cx))
        if all(x < 0 for x in fcf_series):
            flags.append("Free cash flow has been negative across the latest available periods")
        elif all(x > 0 for x in fcf_series):
            positives.append("Free cash flow has remained positive across the latest periods")

    # Dilution.
    if len(shares) >= 3:
        dilution = pct_change(shares[0], shares[-1])
        if dilution is not None:
            if dilution > 0.10:
                flags.append(f"Share count has increased materially ({dilution:.1%})")
            elif dilution < -0.03:
                positives.append(f"Share count has fallen ({abs(dilution):.1%})")

    # Debt trajectory.
    if len(debt) >= 3:
        debt_change = pct_change(debt[0], debt[-1])
        if debt_change is not None and debt_change > 0.30:
            flags.append(f"Debt has increased materially ({debt_change:.1%})")
        elif debt_change is not None and debt_change < -0.10:
            positives.append(f"Debt has fallen materially ({abs(debt_change):.1%})")

    # Cash trend.
    if len(cash_bal) >= 3:
        cash_change = pct_change(cash_bal[0], cash_bal[-1])
        if cash_change is not None and cash_change > 0.20:
            positives.append(f"Cash balance has increased ({cash_change:.1%})")
        elif cash_change is not None and cash_change < -0.30:
            flags.append(f"Cash balance has fallen materially ({abs(cash_change):.1%})")

    # Current-period metrics.
    pe = metrics.get("P/E")
    net_debt = metrics.get("Net debt")
    current_ratio = metrics.get("Current ratio")
    fcf_yield = metrics.get("FCF yield")
    roe = metrics.get("ROE")
    operating_margin = metrics.get("Operating margin")

    if pe is not None and pe > 60:
        flags.append(f"Very high P/E ({pe:.1f}x): expectations are likely embedded in the price")
    elif pe is not None and pe > 40:
        flags.append(f"High P/E ({pe:.1f}x): valuation leaves less room for disappointment")

    if net_debt is not None and net_debt > 0:
        positives.append("Company carries net debt; leverage should be monitored")
    elif net_debt is not None and net_debt <= 0:
        positives.append("Company has net cash based on the available balance-sheet data")

    if current_ratio is not None:
        if current_ratio < 0.8:
            flags.append(f"Low current ratio ({current_ratio:.2f})")
        elif current_ratio > 1.5:
            positives.append(f"Healthy current ratio ({current_ratio:.2f})")

    if fcf_yield is not None and fcf_yield < 0:
        flags.append("Negative FCF yield / negative free cash flow")
    elif fcf_yield is not None and fcf_yield > 0.05:
        positives.append(f"Strong FCF yield ({fcf_yield:.1%})")

    if roe is not None:
        if roe > 0.15:
            positives.append(f"ROE is strong ({roe:.1%})")
        elif roe < 0:
            flags.append(f"Negative ROE ({roe:.1%})")

    if operating_margin is not None and operating_margin < 0:
        flags.append("Negative operating margin")
    elif operating_margin is not None and operating_margin > 0.15:
        positives.append(f"Strong operating margin ({operating_margin:.1%})")

    # Deduplicate while preserving order.
    flags = list(dict.fromkeys(flags))
    positives = list(dict.fromkeys(positives))

    return flags, positives


def classify_company(sector, industry, overview):
    """
    Classifies the business so different company types can use
    different valuation logic and score emphasis.
    """
    text = " ".join([
        str(sector or ""),
        str(industry or ""),
        str(overview.get("Description", "") or ""),
    ]).lower()

    if any(x in text for x in [
        "reit", "real estate investment trust", "real estate", "property trust"
    ]):
        return "REIT / PROPERTY"

    if any(x in text for x in [
        "bank", "banking", "insurance", "financial services",
        "capital markets", "asset management", "credit"
    ]):
        return "FINANCIAL"

    if any(x in text for x in [
        "semiconductor", "software", "technology", "internet",
        "biotechnology", "electronic", "aerospace"
    ]):
        return "GROWTH / TECHNOLOGY"

    if any(x in text for x in [
        "utility", "electric utility", "gas utility", "telecom",
        "telecommunications"
    ]):
        return "UTILITY / INFRASTRUCTURE"

    if any(x in text for x in [
        "energy", "oil", "gas", "mining", "materials"
    ]):
        return "ENERGY / RESOURCES"

    if any(x in text for x in [
        "industrial", "machinery", "construction", "engineering",
        "transportation", "manufacturing"
    ]):
        return "INDUSTRIAL"

    if any(x in text for x in [
        "consumer", "retail", "restaurant", "food", "beverage"
    ]):
        return "CONSUMER"

    return "GENERAL"


def company_type_weights(company_type):
    """
    Small adjustment to scoring emphasis by business model.
    The total always remains 100.
    """
    base = dict(WEIGHTS)

    if company_type == "GROWTH / TECHNOLOGY":
        return {
            "business_quality": 15,
            "growth": 25,
            "profitability": 10,
            "financial_health": 15,
            "cash_generation": 10,
            "shareholder_alignment": 10,
            "valuation": 15,
        }

    if company_type == "FINANCIAL":
        return {
            "business_quality": 15,
            "growth": 15,
            "profitability": 20,
            "financial_health": 20,
            "cash_generation": 5,
            "shareholder_alignment": 10,
            "valuation": 15,
        }

    if company_type == "REIT / PROPERTY":
        return {
            "business_quality": 15,
            "growth": 10,
            "profitability": 15,
            "financial_health": 20,
            "cash_generation": 15,
            "shareholder_alignment": 10,
            "valuation": 15,
        }

    if company_type == "UTILITY / INFRASTRUCTURE":
        return {
            "business_quality": 20,
            "growth": 10,
            "profitability": 15,
            "financial_health": 20,
            "cash_generation": 15,
            "shareholder_alignment": 10,
            "valuation": 10,
        }

    if company_type == "ENERGY / RESOURCES":
        return {
            "business_quality": 15,
            "growth": 10,
            "profitability": 15,
            "financial_health": 20,
            "cash_generation": 15,
            "shareholder_alignment": 10,
            "valuation": 15,
        }

    return base


def company_specific_valuation(company_type, metrics):
    """
    Adds valuation methods appropriate to the company type.
    Returns additional fair-value estimates; missing data is ignored.
    """
    eps = metrics.get("EPS")
    fcf = metrics.get("Free cash flow")
    shares = metrics.get("Shares outstanding")
    revenue = metrics.get("Revenue TTM")
    price = metrics.get("Share price")
    roe = metrics.get("ROE")

    estimates = []

    if company_type == "FINANCIAL":
        # Financials are often better assessed with earnings and ROE than FCF.
        if eps and eps > 0:
            target_pe = 12
            if roe and roe > 0.15:
                target_pe = 14
            estimates.append(("Financial P/E", eps * target_pe))

    elif company_type == "REIT / PROPERTY":
        # Without reliable FFO/AFFO in this V1, use a deliberately cautious
        # earnings-style proxy and FCF yield rather than pretending GAAP EPS is FFO.
        if fcf and shares and fcf > 0:
            estimates.append(("Property FCF yield", (fcf / 0.065) / shares))

    elif company_type == "GROWTH / TECHNOLOGY":
        if revenue and shares and revenue > 0:
            # Conservative revenue multiple proxy. It is only supplementary.
            estimates.append(("Growth sales multiple", (revenue * 4.0) / shares))

    elif company_type == "UTILITY / INFRASTRUCTURE":
        if eps and eps > 0:
            estimates.append(("Infrastructure P/E", eps * 16))

    elif company_type == "ENERGY / RESOURCES":
        if eps and eps > 0:
            estimates.append(("Resource P/E", eps * 10))

    elif company_type == "INDUSTRIAL":
        if eps and eps > 0:
            estimates.append(("Industrial P/E", eps * 18))

    return estimates


def valuation_engine(metrics):
    """
    Produces a range rather than pretending there is one precise fair value.
    Uses several methods where the available data supports them.
    """
    fcf = metrics.get("Free cash flow")
    eps = metrics.get("EPS")
    shares = metrics.get("Shares outstanding")
    price = metrics.get("Share price")
    revenue_growth = metrics.get("Revenue CAGR")

    if revenue_growth is None:
        revenue_growth = 0.10

    outputs = {}

    # Target multiples intentionally remain broad and moderate.
    # This prevents a high-growth stock from being valued solely on a low market multiple.
    targets = {
        "bear": {"pe": 18, "fcf_yield": 0.065, "discount": 0.12},
        "base": {"pe": 25, "fcf_yield": 0.050, "discount": 0.10},
        "bull": {"pe": 35, "fcf_yield": 0.040, "discount": 0.09},
    }

    for scenario, assumptions in targets.items():
        growth = scenario_growth(revenue_growth, scenario)
        values = []

        pe_value = multiple_fair_value(eps, assumptions["pe"])
        if pe_value is not None:
            values.append(pe_value)

        fcf_value = fcf_multiple_fair_value(
            fcf, shares, assumptions["fcf_yield"]
        )
        if fcf_value is not None:
            values.append(fcf_value)

        dcf_value = dcf_fair_value(
            fcf, shares, growth,
            discount_rate=assumptions["discount"]
        )
        if dcf_value is not None:
            values.append(dcf_value)

        outputs[scenario] = {
            "method_values": {
                "P/E": pe_value,
                "FCF yield": fcf_value,
                "DCF": dcf_value,
            },
            "fair_value": statistics.median(values) if values else None,
        }

    fair_values = [
        outputs[x]["fair_value"] for x in ("bear", "base", "bull")
        if outputs[x]["fair_value"] is not None
    ]

    base = outputs["base"]["fair_value"]

    buy_zone = base * 0.75 if base is not None else None
    attractive = base * 0.85 if base is not None else None
    reasonable = base * 1.00 if base is not None else None
    expensive = base * 1.15 if base is not None else None

    upside = None
    if price and base:
        upside = base / price - 1

    return {
        "scenarios": outputs,
        "base_fair_value": base,
        "fair_value_low": min(fair_values) if fair_values else None,
        "fair_value_high": max(fair_values) if fair_values else None,
        "buy_zone": buy_zone,
        "attractive_price": attractive,
        "reasonable_price": reasonable,
        "expensive_price": expensive,
        "upside_to_base": upside,
    }


@dataclass
class CompanyReport:
    symbol: str
    company: str
    sector: str
    overall_score: float
    verdict: str
    scores: Dict[str, float]
    metrics: Dict[str, Any]
    valuation: Dict[str, Any]
    company_type: str
    risk_level: str
    data_confidence: str
    historical_trends: Dict[str, Any]
    position_guidance: Dict[str, Any]
    flags: List[str]
    positives: List[str]
    quality_score: float = 0.0
    opportunity_score: float = 0.0
    risk_score: float = 0.0
    risk_band: str = "MEDIUM"
    data_status: Dict[str, Any] = field(default_factory=dict)


def analyse(data: Dict[str, Any], api_key: Optional[str] = None) -> CompanyReport:
    overview = data.get("overview", {})
    income = data.get("income", {})
    balance = data.get("balance", {})
    cash = data.get("cash", {})

    company = overview.get("Name", "Unknown")
    sector = overview.get("Sector", "Unknown")
    industry = overview.get("Industry", "Unknown")
    symbol = overview.get("Symbol") or data.get("symbol_used") or data.get("symbol_requested") or "Unknown"
    company_type = classify_company(sector, industry, overview)

    financial_currency = normalise_currency(
        overview.get("Currency")
        or overview.get("CurrencyCode")
        or overview.get("ReportingCurrency")
    )

    # Alpha Vantage may not expose reporting currency consistently for all
    # international securities. The company filing is the fallback source.
    if not financial_currency:
        description = str(overview.get("Description", "")).lower()
        financial_currency = "USD" if "raspberry pi" in description else None

    is_london = str(symbol).upper().endswith((".L", ".LON"))
    quote_currency = "GBP" if is_london else financial_currency

    revenue = safe_num(overview.get("RevenueTTM"))
    eps = safe_num(overview.get("EPS"))
    pe = safe_num(overview.get("PERatio"))
    ps = safe_num(overview.get("PriceToSalesRatioTTM"))
    roe = safe_num(overview.get("ReturnOnEquityTTM"))
    roa = safe_num(overview.get("ReturnOnAssetsTTM"))
    margin = safe_num(overview.get("ProfitMargin"))
    operating_margin = safe_num(overview.get("OperatingMarginTTM"))
    beta = safe_num(overview.get("Beta"))
    dividend_yield = safe_num(overview.get("DividendYield"))
    shares = safe_num(overview.get("SharesOutstanding"))
    raw_share_price = safe_num(overview.get("Price"))
    share_price = raw_share_price
    price_currency = quote_currency
    if is_london and raw_share_price is not None:
        # Alpha Vantage's London quote is commonly returned in pence.
        share_price = gbp_price_from_gbx(raw_share_price)

    total_assets = safe_num(balance.get("totalAssets"))
    total_liabilities = safe_num(balance.get("totalLiabilities"))
    cash_value = safe_num(balance.get("cashAndCashEquivalentsAtCarryingValue"))
    debt = safe_num(balance.get("shortLongTermDebtTotal"))
    current_assets = safe_num(balance.get("totalCurrentAssets"))
    current_liabilities = safe_num(balance.get("totalCurrentLiabilities"))

    operating_cf = safe_num(cash.get("operatingCashflow"))
    capex = safe_num(cash.get("capitalExpenditures"))
    fcf = None
    if operating_cf is not None and capex is not None:
        fcf = operating_cf - abs(capex)

    current_ratio = None
    if current_assets is not None and current_liabilities:
        current_ratio = current_assets / current_liabilities

    net_debt = None
    if debt is not None and cash_value is not None:
        net_debt = debt - cash_value

    net_debt_to_ebitda = None
    ebitda = safe_num(income.get("ebitda"))
    if net_debt is not None and ebitda and ebitda > 0:
        net_debt_to_ebitda = net_debt / ebitda

    fcf_yield = None
    market_cap = safe_num(overview.get("MarketCapitalization"))
    if fcf and market_cap and market_cap > 0:
        fcf_yield = fcf / market_cap

    # Historical growth if annual reports are available.
    revenue_cagr = None
    eps_cagr = None
    annual = income.get("annualReports", [])
    if len(annual) >= 4:
        latest = annual[0]
        older = annual[min(3, len(annual) - 1)]
        revenue_cagr = cagr(
            safe_num(latest.get("totalRevenue")),
            safe_num(older.get("totalRevenue")),
            min(3, len(annual) - 1),
        )
        eps_cagr = cagr(
            safe_num(older.get("reportedEPS")),
            safe_num(latest.get("reportedEPS")),
            min(3, len(annual) - 1),
        )

    growth = score_growth(revenue_cagr, eps_cagr)
    profitability = statistics.mean([
        score_margin(margin),
        score_margin(operating_margin),
        clamp((roe + 0.10) / 0.40) if roe is not None else 0.50,
    ])
    health = score_balance(net_debt_to_ebitda, current_ratio)
    cash_score = clamp((fcf_yield + 0.02) / 0.10) if fcf_yield is not None else 0.50
    valuation = score_valuation(pe, ps, fcf_yield)

    # Version 1 uses neutral assumptions for qualitative fields.
    business_quality = 0.60
    shareholder_alignment = 0.55

    raw_scores = {
        "business_quality": business_quality,
        "growth": growth,
        "profitability": profitability,
        "financial_health": health,
        "cash_generation": cash_score,
        "shareholder_alignment": shareholder_alignment,
        "valuation": valuation,
    }

    active_weights = company_type_weights(company_type)
    overall = sum(raw_scores[k] * active_weights[k] for k in active_weights)

    if overall >= 85:
        verdict = "EXCEPTIONAL"
    elif overall >= 75:
        verdict = "STRONG"
    elif overall >= 65:
        verdict = "INTERESTING"
    elif overall >= 50:
        verdict = "SPECULATIVE"
    else:
        verdict = "AVOID / INVESTIGATE"

    metrics = {
        "Revenue TTM": revenue,
        "EPS": eps,
        "P/E": pe,
        "Price/Sales": ps,
        "ROE": roe,
        "ROA": roa,
        "Profit margin": margin,
        "Operating margin": operating_margin,
        "Market cap": market_cap,
        "Free cash flow": fcf,
        "FCF yield": fcf_yield,
        "Net debt": net_debt,
        "Net debt / EBITDA": net_debt_to_ebitda,
        "Current ratio": current_ratio,
        "Beta": beta,
        "Dividend yield": dividend_yield,
        "Revenue CAGR": revenue_cagr,
        "EPS CAGR": eps_cagr,
        "Shares outstanding": shares,
        "Share price": share_price,
        "Raw share price": raw_share_price,
        "Price currency": price_currency,
        "Financial currency": financial_currency,
        "Company type": company_type,
    }

    # Let the dedicated red-flag engine challenge the thesis only after the
    # complete metrics dictionary exists.
    flags, positives = red_flag_engine(
        overview, income, balance, cash, metrics
    )

    # V1.12: normalise valuation inputs before any fair-value calculation.
    if api_key:
        metrics = normalise_financials_for_valuation(metrics, api_key)
    else:
        metrics["Valuation currency"] = metrics.get("Price currency") or "GBP"
        metrics["FX conversion applied"] = False
        metrics["FX source"] = "No API key supplied"
        metrics["FX timestamp"] = None
        metrics["Financial currency (reported)"] = financial_currency

    valuation = valuation_engine(metrics)
    valuation["company_specific"] = company_specific_valuation(company_type, metrics)

    extra_values = [
        x[1] for x in valuation["company_specific"]
        if x[1] is not None and x[1] > 0
    ]
    core_values = [
        x["fair_value"] for x in valuation["scenarios"].values()
        if x.get("fair_value") is not None and x["fair_value"] > 0
    ]

    # Blend the supplementary company-specific method into the base value
    # only when there is enough data. This avoids letting one proxy dominate.
    if extra_values and core_values:
        valuation["base_fair_value"] = statistics.median(
            [statistics.median(core_values), statistics.median(extra_values)]
        )
        valuation["fair_value_low"] = min(
            valuation["fair_value_low"], min(extra_values)
        )
        valuation["fair_value_high"] = max(
            valuation["fair_value_high"], max(extra_values)
        )
        valuation["buy_zone"] = valuation["base_fair_value"] * 0.75
        valuation["attractive_price"] = valuation["base_fair_value"] * 0.85
        valuation["reasonable_price"] = valuation["base_fair_value"]
        valuation["expensive_price"] = valuation["base_fair_value"] * 1.15
        if share_price and valuation["base_fair_value"]:
            valuation["upside_to_base"] = valuation["base_fair_value"] / share_price - 1

    pretty_scores = {
        k.replace("_", " ").title(): round(v * active_weights[k], 1)
        for k, v in raw_scores.items()
    }

    if len(flags) >= 5:
        risk_level = "HIGH"
    elif len(flags) >= 3:
        risk_level = "MEDIUM-HIGH"
    elif len(flags) >= 1:
        risk_level = "MEDIUM"
    else:
        risk_level = "LOW"

    data_confidence = estimate_data_confidence(
        metrics, valuation, overview, income, balance, cash
    )
    historical_trends = build_historical_trends(income, balance, cash)

    # V1.10: separate business quality, investment opportunity and risk.
    decision_scores = three_score_framework(
        raw_scores=raw_scores,
        active_weights=active_weights,
        valuation_score=raw_scores["valuation"],
        flags=flags,
        metrics=metrics,
        risk_level=risk_level,
        data_confidence=data_confidence,
    )

    # The legacy overall score now intentionally means "investment opportunity".
    # This preserves compatibility with existing peer/position-sizing functions.
    opportunity_score = decision_scores["opportunity_score"]

    if opportunity_score >= 85:
        verdict = "EXCEPTIONAL OPPORTUNITY"
    elif opportunity_score >= 75:
        verdict = "STRONG OPPORTUNITY"
    elif opportunity_score >= 65:
        verdict = "INTERESTING"
    elif opportunity_score >= 50:
        verdict = "SPECULATIVE"
    else:
        verdict = "AVOID / INVESTIGATE"

    report = CompanyReport(
        symbol=symbol,
        company=company,
        sector=sector,
        overall_score=round(opportunity_score, 1),
        verdict=verdict,
        scores=pretty_scores,
        metrics=metrics,
        valuation=valuation,
        company_type=company_type,
        risk_level=risk_level,
        data_confidence=data_confidence,
        historical_trends=historical_trends,
        position_guidance={},
        flags=flags,
        positives=positives,
        quality_score=decision_scores["quality_score"],
        opportunity_score=decision_scores["opportunity_score"],
        risk_score=decision_scores["risk_score"],
        risk_band=decision_scores["risk_band"],
        data_status={
            "requested_symbol": data.get("symbol_requested"),
            "api_symbol": data.get("symbol_used") or symbol,
            "warnings": data.get("warnings", []),
            "errors": data.get("errors", {}),
        },
    )
    report.position_guidance = conviction_and_position_size(report)
    return report



def investment_thesis(report: CompanyReport):
    """
    Converts the numerical analysis into an investment decision framework.
    It deliberately separates the bull case from the reasons to stay cautious.
    """
    metrics = report.metrics
    v = report.valuation

    buy_reasons = []
    risks = []
    monitor = []

    growth = metrics.get("Revenue CAGR")
    fcf = metrics.get("Free cash flow")
    fcf_yield = metrics.get("FCF yield")
    roe = metrics.get("ROE")
    margin = metrics.get("Operating margin")
    pe = metrics.get("P/E")
    net_debt = metrics.get("Net debt")
    current_ratio = metrics.get("Current ratio")

    if growth is not None and growth >= 0.10:
        buy_reasons.append(f"Strong historical growth ({growth:.1%} CAGR where data is available).")
    if fcf is not None and fcf > 0:
        buy_reasons.append("The company is generating positive free cash flow.")
    if fcf_yield is not None and fcf_yield >= 0.05:
        buy_reasons.append(f"FCF yield is attractive at {fcf_yield:.1%}.")
    if roe is not None and roe >= 0.15:
        buy_reasons.append(f"Returns on equity are strong ({roe:.1%}).")
    if margin is not None and margin >= 0.15:
        buy_reasons.append(f"Operating margin is healthy ({margin:.1%}).")
    if net_debt is not None and net_debt <= 0:
        buy_reasons.append("Balance sheet shows net cash rather than net debt.")

    if not buy_reasons:
        buy_reasons.append("The available data does not yet establish a strong fundamental advantage.")

    if report.flags:
        risks.extend(report.flags[:5])

    if pe is not None and pe > 40:
        risks.append("The valuation leaves relatively little room for execution disappointment.")
    if growth is not None and growth < 0.05:
        risks.append("Historical growth is modest.")
    if fcf is not None and fcf < 0:
        risks.append("Negative free cash flow makes valuation less reliable.")

    if not risks:
        risks.append("No major automated red flags were identified; qualitative research is still required.")

    monitor.extend([
        "Revenue growth and whether it is accelerating or slowing.",
        "Operating margin trend.",
        "Free cash flow conversion.",
        "Share count / dilution.",
        "Debt and cash balance.",
        "Changes to the valuation versus the company's growth outlook.",
    ])

    base = v.get("base_fair_value")
    price = metrics.get("Share price")

    if base is not None and price is not None:
        ratio = price / base
        if ratio <= 0.75:
            verdict = "ATTRACTIVE"
        elif ratio <= 0.90:
            verdict = "REASONABLE / ATTRACTIVE"
        elif ratio <= 1.10:
            verdict = "FAIRLY VALUED"
        elif ratio <= 1.25:
            verdict = "EXPENSIVE"
        else:
            verdict = "VERY EXPENSIVE"
    else:
        verdict = "INSUFFICIENT VALUATION DATA"

    return {
        "buy_reasons": list(dict.fromkeys(buy_reasons))[:5],
        "risks": list(dict.fromkeys(risks))[:5],
        "monitor": list(dict.fromkeys(monitor))[:6],
        "valuation_verdict": verdict,
    }





def three_score_framework(raw_scores, active_weights, valuation_score, flags,
                          metrics, risk_level, data_confidence):
    """
    V1.11 decision framework.

    Quality = how good the underlying business is, deliberately excluding
    valuation so an expensive excellent company can remain excellent.

    Opportunity = quality plus valuation. This is the score used for the
    investment opportunity / overall ranking.

    Risk = 0-100, where higher means more risk. It is kept separate from
    quality so a high-growth company is not automatically labelled a poor
    business simply because it is volatile or expensive.
    """
    fundamental_keys = [
        "business_quality", "growth", "profitability",
        "financial_health", "cash_generation", "shareholder_alignment"
    ]
    fundamental_weight = sum(active_weights.get(k, 0) for k in fundamental_keys)

    if fundamental_weight > 0:
        quality = sum(
            raw_scores.get(k, 0.5) * active_weights.get(k, 0)
            for k in fundamental_keys
        ) / fundamental_weight * 100
    else:
        quality = 50.0

    valuation = valuation_score * 100
    opportunity = (quality * 0.60) + (valuation * 0.40)

    # Risk starts with explicit red flags and then incorporates balance-sheet
    # and data-quality considerations. It is intentionally not the inverse
    # of quality.
    risk = 12.0
    risk += min(len(flags) * 8.0, 40.0)

    health = raw_scores.get("financial_health", 0.5)
    growth = raw_scores.get("growth", 0.5)
    cash = raw_scores.get("cash_generation", 0.5)

    risk += max(0.0, (0.50 - health) * 30.0)
    risk += max(0.0, (0.50 - cash) * 15.0)
    risk += max(0.0, (0.40 - growth) * 10.0)

    # Very demanding valuation increases investment risk, but not business quality.
    if valuation < 35:
        risk += 12
    elif valuation < 50:
        risk += 6

    risk += {
        "LOW": 0,
        "MEDIUM": 4,
        "MEDIUM-HIGH": 8,
        "HIGH": 12,
    }.get(risk_level, 6)

    # Low confidence means uncertainty rather than necessarily poor business quality.
    risk += {"HIGH": 0, "MEDIUM": 3, "LOW": 7}.get(data_confidence, 4)

    risk = max(0.0, min(100.0, risk))

    if risk <= 25:
        band = "LOW"
    elif risk <= 45:
        band = "MEDIUM"
    elif risk <= 65:
        band = "MEDIUM-HIGH"
    else:
        band = "HIGH"

    return {
        "quality_score": round(max(0.0, min(100.0, quality)), 1),
        "opportunity_score": round(max(0.0, min(100.0, opportunity)), 1),
        "risk_score": round(risk, 1),
        "risk_band": band,
    }


def conviction_and_position_size(report):
    """Conservative research-oriented conviction and position-sizing framework."""
    score = report.opportunity_score or report.overall_score
    risk = report.risk_level
    risk_score = report.risk_score
    confidence = report.data_confidence
    price = report.metrics.get("Share price")
    base = report.valuation.get("base_fair_value")

    conviction = max(1.0, min(10.0, score / 10.0))
    conviction -= min(2.0, risk_score / 50.0)
    conviction -= {"HIGH": 0.0, "MEDIUM": 0.4, "LOW": 0.8}.get(confidence, 0.6)

    if price and base:
        ratio = base / price
        if ratio >= 1.25:
            conviction += 0.6
        elif ratio >= 1.10:
            conviction += 0.3
        elif ratio <= 0.75:
            conviction -= 0.7
        elif ratio <= 0.90:
            conviction -= 0.3

    conviction = max(1.0, min(10.0, conviction))

    if conviction >= 9:
        suggested, maximum = "3–5%", "6%"
    elif conviction >= 8:
        suggested, maximum = "2–4%", "5%"
    elif conviction >= 7:
        suggested, maximum = "1.5–3%", "4%"
    elif conviction >= 6:
        suggested, maximum = "1–2%", "3%"
    elif conviction >= 5:
        suggested, maximum = "0.5–1.5%", "2%"
    else:
        suggested, maximum = "0–1%", "1.5%"

    drivers = []
    drivers.append(
        "Strong overall fundamental score" if score >= 75
        else "Fundamental score limits conviction"
    )
    if risk in ("MEDIUM-HIGH", "HIGH"):
        drivers.append(f"{risk.title()} risk reduces suggested sizing")
    elif risk == "LOW":
        drivers.append("Low automated risk supports sizing")
    if confidence == "LOW":
        drivers.append("Low data confidence limits conviction")
    elif confidence == "HIGH":
        drivers.append("High data confidence supports the assessment")
    if price and base:
        upside = base / price - 1
        drivers.append(
            f"Base-case valuation implies {upside:.1%} upside"
            if upside >= 0 else
            f"Base-case valuation implies {upside:.1%} downside"
        )

    return {
        "conviction": round(conviction, 1),
        "suggested_range": suggested,
        "maximum": maximum,
        "drivers": drivers[:5],
    }


def peer_benchmark(reports):
    """
    Benchmark companies on the dimensions already calculated by the engine.
    This is a relative ranking tool, not a claim that every company is directly
    comparable. Business type is retained so the user can spot mixed groups.
    """
    if len(reports) < 2:
        return []

    metrics = [
        ("Opportunity", lambda r: r.opportunity_score),
        ("Quality", lambda r: r.quality_score),
        ("Growth", lambda r: r.scores.get("Growth", 0)),
        ("Profitability", lambda r: r.scores.get("Profitability", 0)),
        ("Financial Health", lambda r: r.scores.get("Financial Health", 0)),
        ("Cash Generation", lambda r: r.scores.get("Cash Generation", 0)),
        ("Valuation", lambda r: r.scores.get("Valuation", 0)),
    ]

    result = []
    for r in reports:
        row = {
            "Company": r.company,
            "Ticker": r.symbol,
            "Type": r.company_type,
            "Quality": r.quality_score,
            "Opportunity": r.opportunity_score,
            "Risk score": r.risk_score,
            "Risk": r.risk_band,
            "Data confidence": r.data_confidence,
        }

        for name, fn in metrics:
            row[name] = round(fn(r), 1)

        price = r.metrics.get("Share price")
        base = r.valuation.get("base_fair_value")
        row["Upside to base"] = (
            round((base / price - 1) * 100, 1)
            if price and base else None
        )
        row["Flags"] = len(r.flags)
        result.append(row)

    # Rank on the overall model score.
    result.sort(key=lambda x: x["Opportunity"], reverse=True)

    for i, row in enumerate(result, 1):
        row["Rank"] = i

    return result


def peer_groups_for(company_type):
    """
    Starter peer map. Users can override it in the UI.
    These are deliberately suggestions rather than assertions of perfect comparability.
    """
    groups = {
        "GROWTH / TECHNOLOGY": ["RPI", "ONT", "ALFA"],
        "INDUSTRIAL": ["RR.L", "GAW.L"],
        "FINANCIAL": ["AV.L", "IBKR"],
        "ENERGY / RESOURCES": ["SHEL.L", "BP.L"],
        "UTILITY / INFRASTRUCTURE": ["NG.L", "SVT.L"],
        "REIT / PROPERTY": ["BBOX.L", "DLR"],
    }
    return groups.get(company_type, [])


def compare_reports(reports):
    """Rank several reports consistently across the same dimensions."""
    rows = []
    for r in reports:
        rows.append({
            "Company": r.company,
            "Ticker": r.symbol,
            "Type": r.company_type,
            "Quality": r.quality_score,
            "Opportunity": r.opportunity_score,
            "Risk score": r.risk_score,
            "Risk": r.risk_band,
            "Growth": r.scores.get("Growth", 0),
            "Profitability": r.scores.get("Profitability", 0),
            "Financial Health": r.scores.get("Financial Health", 0),
            "Cash Generation": r.scores.get("Cash Generation", 0),
            "Valuation": r.scores.get("Valuation", 0),
            "Base Fair Value": r.valuation.get("base_fair_value"),
            "Current Price": r.metrics.get("Share price"),
        })

    rows.sort(key=lambda x: x["Opportunity"], reverse=True)
    return rows


def print_report(report: CompanyReport):
    print("\n" + "=" * 70)
    print(f"{report.company} ({report.symbol})")
    print(f"Sector: {report.sector}")
    print(f"Business type: {report.company_type}")
    print(f"Risk level: {report.risk_level}")
    print(f"Data confidence: {report.data_confidence}")
    print("=" * 70)
    print(f"QUALITY SCORE: {report.quality_score}/100")
    print(f"OPPORTUNITY SCORE: {report.opportunity_score}/100   [{report.verdict}]")
    print(f"RISK SCORE: {report.risk_score}/100   [{report.risk_band}]")
    if report.metrics.get("Price currency") or report.metrics.get("Financial currency"):
        print(
            f"Currency: reported financials={report.metrics.get('Financial currency') or 'unknown'}, "
            f"valuation={report.metrics.get('Valuation currency') or 'unknown'}, "
            f"price={report.metrics.get('Price currency') or 'unknown'}"
        )
        print(
            f"FX: {report.metrics.get('FX source') or 'none'} "
            f"({report.metrics.get('FX timestamp') or 'n/a'})"
        )
    print("\nSCORECARD")
    for k, v in report.scores.items():
        print(f"  {k:<25} {v:>5.1f}")

    print("\nKEY METRICS")
    for k, v in report.metrics.items():
        if v is None:
            continue
        if "CAGR" in k or k in ("ROE", "ROA", "Profit margin", "Operating margin", "FCF yield", "Dividend yield"):
            print(f"  {k:<25} {v:.1%}")
        elif isinstance(v, (int, float)):
            print(f"  {k:<25} {v:,.2f}")
        else:
            print(f"  {k:<25} {v}")

    print("\nVALUATION ENGINE")
    v = report.valuation
    if v.get("base_fair_value") is not None:
        print(f"  Base fair value/share:   {v['base_fair_value']:.2f}")
        if v.get("fair_value_low") is not None:
            print(f"  Scenario range:          {v['fair_value_low']:.2f} - {v['fair_value_high']:.2f}")
        if v.get("buy_zone") is not None:
            print(f"  Strong buy zone:         <= {v['buy_zone']:.2f}")
            print(f"  Attractive:              <= {v['attractive_price']:.2f}")
            print(f"  Reasonable:              ~ {v['reasonable_price']:.2f}")
            print(f"  Expensive:               > {v['expensive_price']:.2f}")
        if v.get("upside_to_base") is not None:
            print(f"  Upside/downside to base: {v['upside_to_base']:.1%}")

        print("\n  SCENARIOS")
        for scenario in ("bear", "base", "bull"):
            item = v["scenarios"][scenario]
            fv = item.get("fair_value")
            label = scenario.upper()
            print(f"  {label:<8} {fv:.2f}" if fv is not None else f"  {label:<8} N/A")
    else:
        print("  Not enough data for a reliable valuation estimate yet.")

    thesis = investment_thesis(report)

    print("\nINVESTMENT THESIS")
    print(f"  Valuation verdict: {thesis['valuation_verdict']}")

    print("\n  WHY BUY?")
    for item in thesis["buy_reasons"]:
        print("   + " + item)

    print("\n  WHY NOT BUY?")
    for item in thesis["risks"]:
        print("   - " + item)

    print("\n  WHAT TO MONITOR?")
    for item in thesis["monitor"]:
        print("   • " + item)

    if report.positives:
        print("\nPOSITIVE SIGNALS")
        for x in report.positives:
            print("  ✓ " + x)

    if report.flags:
        print("\nRED FLAGS / WATCH ITEMS")
        for x in report.flags:
            print("  ⚠ " + x)

    print("\nIMPORTANT: This is a research tool, not financial advice.")
    print("=" * 70)




# -----------------------------
# V2 multi-source data layer
# -----------------------------

@dataclass
class SourceRecord:
    name: str
    role: str
    status: str
    url: str = ""
    notes: str = ""
    checked_at: str = ""


def _http_json(url, headers=None, timeout=20):
    req = urllib.request.Request(url, headers=headers or {"User-Agent": "Company Investigator/2.0 research tool"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        raise RuntimeError(str(exc)) from exc


def _yf_value(item):
    if isinstance(item, dict):
        if "raw" in item:
            return item.get("raw")
        if "reportedValue" in item:
            rv = item.get("reportedValue")
            return rv.get("raw") if isinstance(rv, dict) else rv
        if "value" in item:
            return item.get("value")
    return item


def _yahoo_timeseries(symbol, types, period_years=10):
    now = int(datetime.now(timezone.utc).timestamp())
    start = int((datetime.now(timezone.utc).replace(year=max(2000, datetime.now(timezone.utc).year - period_years))).timestamp())
    type_param = ",".join(types)
    url = (
        "https://query2.finance.yahoo.com/ws/fundamentals-timeseries/v1/finance/timeseries/"
        + urllib.parse.quote(symbol, safe="")
        + f"?symbol={urllib.parse.quote(symbol)}&type={urllib.parse.quote(type_param)}"
        + f"&merge=false&period1={start}&period2={now}"
    )
    return _http_json(url)


def _yahoo_chart(symbol):
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(symbol)}?range=5d&interval=1d"
    return _http_json(url)


def _ts_rows(payload):
    result = payload.get("timeseries", {}) if isinstance(payload, dict) else {}
    rows = {}
    for key, values in result.items():
        if not key.startswith(("annual", "trailing")):
            continue
        if not isinstance(values, list):
            continue
        rows[key] = values
    return rows


def _first_ts(rows, key):
    values = rows.get(key) or []
    if not values:
        return None
    item = values[-1] if key.startswith("annual") else values[0]
    return _yf_value(item)


def _annual_reports(rows, key_map):
    # Convert Yahoo annual time-series into the Alpha-like newest-first format
    # expected by the existing V1 analysis engine.
    dates = set()
    for key in key_map.values():
        for item in rows.get(key, []):
            if isinstance(item, dict) and item.get("asOfDate"):
                dates.add(str(item["asOfDate"]))
    reports = []
    for date in sorted(dates, reverse=True):
        report = {"fiscalDateEnding": date}
        for target, source_key in key_map.items():
            item = next((x for x in rows.get(source_key, []) if str(x.get("asOfDate")) == date), None)
            if item is not None:
                report[target] = _yf_value(item)
        reports.append(report)
    return reports


def yahoo_company_data(symbol):
    """Global secondary fundamentals provider using the yfinance package.

    yfinance wraps Yahoo Finance's current public-facing interfaces and exposes
    income statements, balance sheets, cash flow, TTM data, shares and company
    metadata. It is deliberately treated as a secondary data source: material
    investment decisions should be cross-checked against primary filings/RNS.
    """
    try:
        import yfinance as yf
    except ImportError as exc:
        raise RuntimeError("yfinance is not installed; add yfinance to requirements.txt") from exc

    symbol = str(symbol).upper().strip()
    if symbol.endswith('.LON'):
        symbol = symbol[:-4] + '.L'

    ticker = yf.Ticker(symbol)

    # Fetch the main datasets independently so one missing Yahoo field does not
    # destroy the whole investigation.
    try:
        info = ticker.info or {}
    except Exception:
        info = {}
    try:
        income_df = ticker.get_income_stmt(freq='yearly')
    except Exception:
        income_df = None
    try:
        balance_df = ticker.get_balance_sheet(freq='yearly')
    except Exception:
        balance_df = None
    try:
        cash_df = ticker.get_cash_flow(freq='yearly')
    except Exception:
        cash_df = None
    try:
        fast = ticker.fast_info or {}
    except Exception:
        fast = {}

    def _clean(v):
        try:
            if v is None:
                return None
            if hasattr(v, 'item'):
                v = v.item()
            if isinstance(v, float) and math.isnan(v):
                return None
            return v
        except Exception:
            return None

    def _row(df, names):
        if df is None or getattr(df, 'empty', True):
            return None
        for name in names:
            try:
                if name in df.index:
                    return df.loc[name]
            except Exception:
                pass
        return None

    def _annual_reports(df, mapping):
        if df is None or getattr(df, 'empty', True):
            return []
        cols = list(df.columns)
        reports = []
        for col in cols:
            rep = {"fiscalDateEnding": str(getattr(col, 'date', lambda: col)()) if hasattr(col, 'date') else str(col)[:10]}
            for out_key, names in mapping.items():
                row = _row(df, names)
                if row is not None:
                    try:
                        val = row[col]
                    except Exception:
                        val = None
                    val = _clean(val)
                    if val is not None:
                        rep[out_key] = val
            if len(rep) > 1:
                reports.append(rep)
        return reports

    income_map = {
        "totalRevenue": ["TotalRevenue", "OperatingRevenue"],
        "netIncome": ["NetIncome", "NetIncomeCommonStockholders"],
        "operatingIncome": ["OperatingIncome"],
        "reportedEPS": ["DilutedEPS", "BasicEPS"],
        "weightedAverageShsOutDil": ["DilutedAverageShares", "BasicAverageShares"],
    }
    cash_map = {
        "operatingCashflow": ["OperatingCashFlow", "CashFlowFromContinuingOperatingActivities"],
        "capitalExpenditures": ["CapitalExpenditure", "CapitalExpenditureReported"],
        "freeCashFlow": ["FreeCashFlow"],
    }
    balance_map = {
        "totalAssets": ["TotalAssets"],
        "totalLiabilities": ["TotalLiabilitiesNetMinorityInterest", "TotalLiabilities"],
        "shortLongTermDebtTotal": ["TotalDebt", "LongTermDebtAndCapitalLeaseObligations", "CurrentDebtAndCapitalLeaseObligation"],
        "cashAndCashEquivalentsAtCarryingValue": ["CashCashEquivalentsAndShortTermInvestments", "CashAndCashEquivalents"],
        "totalCurrentAssets": ["CurrentAssets", "TotalCurrentAssets"],
        "totalCurrentLiabilities": ["CurrentLiabilities", "TotalCurrentLiabilities"],
        "totalShareholderEquity": ["StockholdersEquity", "CommonStockEquity"],
    }

    income = {"annualReports": _annual_reports(income_df, income_map)}
    cash = {"annualReports": _annual_reports(cash_df, cash_map)}
    balance = {"annualReports": _annual_reports(balance_df, balance_map)}

    price = _clean(info.get("currentPrice") or info.get("regularMarketPrice") or fast.get("last_price"))
    currency = str(info.get("currency") or ("GBP" if symbol.endswith('.L') else "USD")).upper()
    if symbol.endswith('.L'):
        price_currency = "GBP"
    else:
        price_currency = currency

    market_cap = _clean(info.get("marketCap"))
    shares = _clean(info.get("sharesOutstanding"))
    if shares is None:
        row = _row(balance_df, ["OrdinarySharesNumber", "ShareIssued"])
        if row is not None and len(row.index):
            shares = _clean(row.iloc[0])

    overview = {
        "Symbol": symbol,
        "Name": info.get("longName") or info.get("shortName") or symbol,
        "Sector": info.get("sector") or "Unknown",
        "Industry": info.get("industry") or "Unknown",
        "Description": info.get("longBusinessSummary") or "",
        "Currency": currency,
        "ReportingCurrency": info.get("financialCurrency") or currency,
        "Price": price,
        "MarketCapitalization": market_cap,
        "RevenueTTM": _clean(info.get("totalRevenue")),
        "EPS": _clean(info.get("trailingEps")),
        "PERatio": _clean(info.get("trailingPE")),
        "PriceToSalesRatioTTM": _clean(info.get("priceToSalesTrailing12Months")),
        "ReturnOnEquityTTM": _clean(info.get("returnOnEquity")),
        "ReturnOnAssetsTTM": _clean(info.get("returnOnAssets")),
        "ProfitMargin": _clean(info.get("profitMargins")),
        "OperatingMarginTTM": _clean(info.get("operatingMargins")),
        "SharesOutstanding": shares,
        "PriceCurrency": price_currency,
        "Beta": _clean(info.get("beta")),
        "DividendYield": _clean(info.get("dividendYield")),
    }

    if not income["annualReports"] and not balance["annualReports"] and not cash["annualReports"] and not overview.get("RevenueTTM"):
        raise RuntimeError("Yahoo/yfinance returned no usable fundamental data")

    return {
        "symbol_requested": symbol,
        "symbol_used": symbol,
        "overview": overview,
        "income": income,
        "balance": balance,
        "cash": cash,
        "errors": {},
        "warnings": ["Fundamentals supplied by yfinance/Yahoo Finance as a secondary source; primary filings/RNS should be checked for material decisions."],
        "source_records": [SourceRecord("Yahoo Finance via yfinance", "market/fundamental secondary data", "OK", "https://finance.yahoo.com/").__dict__],
    }


def sec_lookup_ticker(ticker):
    data = _http_json("https://www.sec.gov/files/company_tickers.json", headers={"User-Agent": "Company Investigator research contact research@example.com"})
    target = ticker.upper().replace(".US", "")
    for item in data.values():
        if str(item.get("ticker", "")).upper() == target:
            return str(item.get("cik_str", "")).zfill(10), item.get("title")
    return None, None


def sec_enrichment(ticker):
    """Fetch primary SEC submission/XBRL evidence for US issuers when available."""
    cik, title = sec_lookup_ticker(ticker)
    if not cik:
        return {"available": False, "records": [], "notes": "No SEC ticker mapping found."}
    headers = {"User-Agent": "Company Investigator research contact research@example.com"}
    submissions_url = f"https://data.sec.gov/submissions/CIK{cik}.json"
    facts_url = f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
    submissions = _http_json(submissions_url, headers=headers)
    facts = _http_json(facts_url, headers=headers)
    recent = submissions.get("filings", {}).get("recent", {})
    forms = recent.get("form", [])
    dates = recent.get("filingDate", [])
    acc = recent.get("accessionNumber", [])
    docs = recent.get("primaryDocument", [])
    filings = []
    for i in range(min(len(forms), len(dates), len(acc), len(docs))):
        if forms[i] in {"10-K", "10-Q", "8-K", "20-F", "6-K"}:
            filings.append({"form": forms[i], "filingDate": dates[i], "accessionNumber": acc[i], "primaryDocument": docs[i]})
        if len(filings) >= 20:
            break
    return {
        "available": True,
        "cik": cik,
        "title": title,
        "filings": filings,
        "facts": facts,
        "records": [
            SourceRecord("SEC EDGAR", "primary US filings/XBRL", "OK", "https://www.sec.gov/edgar", "Direct SEC submissions and companyfacts retrieved.").__dict__
        ],
    }


def build_source_audit(symbol, data, sec_data=None):
    records = []
    for raw in data.get("source_records", []):
        records.append(raw)
    if sec_data and sec_data.get("available"):
        records.extend(sec_data.get("records", []))
    market = "UK/LSE" if str(symbol).upper().endswith((".L", ".LON")) else "US/other"
    if market == "UK/LSE":
        records.append(SourceRecord("LSEG RNS", "UK regulatory announcements", "LINKED", "https://www.londonstockexchange.com/news", "RNS should be checked for regulatory and price-sensitive announcements.").__dict__)
    records.append(SourceRecord("Company investor relations", "primary company disclosures", "LINKED", "", "Annual reports, results, presentations and company releases should be reviewed.").__dict__)
    records.append(SourceRecord("Legal/regulatory search", "litigation and regulatory risk", "PLANNED", "", "Deep-search module scheduled for a later research layer.").__dict__)
    records.append(SourceRecord("News intelligence", "material news and developments", "PLANNED", "", "Multi-source news module scheduled for a later research layer.").__dict__)
    return records


def load_company_data_v2(symbol, api_key=None):
    """Provider-neutral loader: Yahoo-compatible fundamentals first, Alpha Vantage fallback, SEC enrichment for US tickers."""
    requested = str(symbol or "").strip().upper()
    candidates = []
    if requested.endswith(".LON"):
        candidates.append(requested[:-4] + ".L")
    candidates.append(requested)
    if "." not in requested:
        candidates.append(requested + ".L")

    errors = []
    data = None
    for candidate in candidates:
        try:
            data = yahoo_company_data(candidate)
            break
        except Exception as exc:
            errors.append(f"Yahoo {candidate}: {exc}")

    if data is None and api_key:
        try:
            data = load_company_data(requested, api_key)
            data.setdefault("warnings", []).append("Primary V2 provider unavailable; Alpha Vantage fallback used.")
        except Exception as exc:
            errors.append(f"Alpha Vantage fallback: {exc}")

    if data is None:
        raise AlphaVantageError("V2 data layer could not retrieve usable fundamentals. " + " | ".join(errors))

    sec_data = None
    if not str(data.get("symbol_used", requested)).upper().endswith((".L", ".LON")):
        try:
            sec_data = sec_enrichment(str(data.get("symbol_used", requested)).upper())
        except Exception as exc:
            sec_data = {"available": False, "records": [], "notes": f"SEC enrichment unavailable: {exc}"}

    data["sec_enrichment"] = sec_data or {"available": False, "records": []}
    data["source_records"] = build_source_audit(requested, data, sec_data)
    data["provider_version"] = "V2 multi-source"
    data["errors"].update({f"V2-{i}": e for i, e in enumerate(errors)})
    return data

def load_company_data(symbol, api_key):
    """Load fundamentals with symbol normalisation and explicit diagnostics.

    A failed endpoint no longer gets silently turned into an all-Unknown report.
    The overview is required for a meaningful analysis; the other statements
    may be unavailable for some securities and are recorded individually.
    """
    requested = str(symbol or "").strip().upper()
    api_symbol = normalise_input_symbol(requested)

    # Bare symbols are ambiguous. Use Alpha Vantage's search endpoint to resolve
    # them, preferring London where the search result supports it.
    resolution_note = ""
    if "." not in requested:
        try:
            resolved = symbol_search(requested, api_key)
        except AlphaVantageError as exc:
            resolved = None
            resolution_note = str(exc)
        if resolved:
            api_symbol = normalise_input_symbol(resolved)
            resolution_note = f"Resolved {requested} to {api_symbol}"
        else:
            resolution_note = resolution_note or f"No symbol-search match; tried {api_symbol}"

    data = {"symbol_requested": requested, "symbol_used": api_symbol, "errors": {}, "warnings": []}
    for key, function in (("overview", "OVERVIEW"), ("income", "INCOME_STATEMENT"),
                          ("balance", "BALANCE_SHEET"), ("cash", "CASH_FLOW")):
        try:
            data[key] = alpha_vantage(api_symbol, function, api_key)
        except AlphaVantageError as exc:
            data[key] = {}
            data["errors"][function] = str(exc)

    if resolution_note:
        data["warnings"].append(resolution_note)

    if not data.get("overview"):
        details = "; ".join(data["errors"].values()) or "No overview data was returned."
        raise AlphaVantageError(
            f"Could not retrieve company data for {requested} (sent to Alpha Vantage as {api_symbol}). {details}"
        )
    return data


def main():
    parser = argparse.ArgumentParser(
        description="Company Investigator — fundamental analysis and valuation."
    )
    parser.add_argument(
        "symbols",
        nargs="+",
        help="One or more tickers, e.g. RPI or RPI RR.L"
    )
    parser.add_argument(
        "--compare",
        action="store_true",
        help="Compare two or more companies instead of printing full reports."
    )
    args = parser.parse_args()

    api_key = os.getenv("ALPHAVANTAGE_API_KEY")
    if not api_key:
        raise SystemExit(
            "No API key found.\n"
            "Set ALPHAVANTAGE_API_KEY, then run again.\n"
            "Example (PowerShell): $env:ALPHAVANTAGE_API_KEY='YOUR_KEY'"
        )

    reports = []

    for raw_symbol in args.symbols:
        symbol = raw_symbol.upper()
        print(f"Downloading fundamentals for {symbol}...")
        data = load_company_data(symbol, api_key)
        report = analyse(data, api_key)
        reports.append(report)

        # Save individual machine-readable report.
        output_name = f"{symbol.replace('/', '_')}_report.json"
        with open(output_name, "w", encoding="utf-8") as f:
            json.dump(asdict(report), f, indent=2, default=str)

    if args.compare:
        if len(reports) < 2:
            raise SystemExit("--compare requires at least two companies.")

        print("\n" + "=" * 100)
        print("COMPANY COMPARISON")
        print("=" * 100)

        rows = compare_reports(reports)
        headers = [
            "Company", "Ticker", "Type", "Overall", "Risk",
            "Growth", "Profitability", "Financial Health",
            "Cash Generation", "Valuation"
        ]

        print(" | ".join(f"{h:<18}" for h in headers))
        print("-" * 100)

        for row in rows:
            print(" | ".join([
                f"{str(row['Company'])[:18]:<18}",
                f"{row['Ticker']:<18}",
                f"{row['Type'][:18]:<18}",
                f"{row['Overall']:>6.1f}",
                f"{row['Risk']:<18}",
                f"{row['Growth']:>6.1f}",
                f"{row['Profitability']:>6.1f}",
                f"{row['Financial Health']:>6.1f}",
                f"{row['Cash Generation']:>6.1f}",
                f"{row['Valuation']:>6.1f}",
            ]))

        print("\nRANKING")
        for i, row in enumerate(rows, 1):
            print(f"  {i}. {row['Company']} ({row['Ticker']}) — {row['Overall']:.1f}/100")

        print("\nNote: ranking is a research aid, not a recommendation.")
        return

    for report in reports:
        print_report(report)


if __name__ == "__main__":
    main()

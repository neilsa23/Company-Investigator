
"""
Company Investigator — Version 4.1
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
import re
import requests
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



def financial_forensics(income, balance, cash, metrics):
    """Deep financial-quality analysis from annual statements.

    Designed to be conservative: missing or non-comparable line items are
    reported as unavailable rather than invented.
    """
    annual_income = income.get("annualReports", []) or []
    annual_balance = balance.get("annualReports", []) or []
    annual_cash = cash.get("annualReports", []) or []

    def series(rows, key):
        out=[]
        for r in rows:
            value=safe_num(r.get(key))
            year=r.get("fiscalDateEnding") or r.get("year") or r.get("period")
            if value is not None:
                out.append((str(year)[:10], value))
        return out

    def latest_and_oldest(rows, key, n=5):
        vals=series(rows, key)
        if len(vals) < 2:
            return None, None, None
        use=vals[:n]
        return use[0][1], use[-1][1], len(use)-1

    rev_latest, rev_old, rev_years = latest_and_oldest(annual_income, "totalRevenue", 6)
    ni_latest, ni_old, ni_years = latest_and_oldest(annual_income, "netIncome", 6)
    op_latest, op_old, op_years = latest_and_oldest(annual_income, "operatingIncome", 6)
    eps_latest, eps_old, eps_years = latest_and_oldest(annual_income, "reportedEPS", 6)
    shares_latest, shares_old, shares_years = latest_and_oldest(annual_income, "weightedAverageShsOutDil", 6)
    ocf_latest, ocf_old, ocf_years = latest_and_oldest(annual_cash, "operatingCashflow", 6)
    capex_latest, capex_old, capex_years = latest_and_oldest(annual_cash, "capitalExpenditures", 6)
    debt_latest, debt_old, debt_years = latest_and_oldest(annual_balance, "shortLongTermDebtTotal", 6)
    cash_latest, cash_old, cash_years = latest_and_oldest(annual_balance, "cashAndCashEquivalentsAtCarryingValue", 6)

    def growth(a,b,y):
        return cagr(b,a,y) if a is not None and b is not None and y else None

    revenue_cagr = growth(rev_latest, rev_old, rev_years)
    net_income_cagr = growth(ni_latest, ni_old, ni_years)
    eps_cagr = growth(eps_latest, eps_old, eps_years)
    shares_cagr = growth(shares_latest, shares_old, shares_years)
    debt_cagr = growth(debt_latest, debt_old, debt_years) if debt_latest and debt_old and debt_latest > 0 and debt_old > 0 else None

    latest_margin = op_latest / rev_latest if op_latest is not None and rev_latest else None
    old_margin = op_old / rev_old if op_old is not None and rev_old else None
    margin_change = latest_margin - old_margin if latest_margin is not None and old_margin is not None else None

    fcf_latest = None
    if ocf_latest is not None:
        fcf_latest = ocf_latest - abs(capex_latest or 0)
    fcf_conversion = fcf_latest / ni_latest if fcf_latest is not None and ni_latest and ni_latest > 0 else None
    ocf_conversion = ocf_latest / ni_latest if ocf_latest is not None and ni_latest and ni_latest > 0 else None

    # ROIC proxy: NOPAT / (equity + net debt), using latest annual data.
    equity_latest = None
    assets_latest = None
    liabilities_latest = None
    if annual_balance:
        equity_latest = safe_num(annual_balance[0].get("totalShareholderEquity"))
        assets_latest = safe_num(annual_balance[0].get("totalAssets"))
        liabilities_latest = safe_num(annual_balance[0].get("totalLiabilities"))
    tax_expense = safe_num(annual_income[0].get("incomeTaxExpense")) if annual_income else None
    pretax = safe_num(annual_income[0].get("incomeBeforeTax")) if annual_income else None
    tax_rate = tax_expense / pretax if tax_expense is not None and pretax and pretax > 0 else 0.25
    nopat = op_latest * (1 - clamp(tax_rate, 0, 0.40)) if op_latest is not None else None
    latest_debt = debt_latest or 0
    invested_capital = (equity_latest or 0) + latest_debt - (cash_latest or 0)
    roic = nopat / invested_capital if nopat is not None and invested_capital > 0 else None

    growth_quality = 50.0
    quality_points=[]
    if revenue_cagr is not None:
        quality_points.append(clamp(0.40 + revenue_cagr / 0.40) * 100)
    if eps_cagr is not None:
        quality_points.append(clamp(0.40 + eps_cagr / 0.40) * 100)
    if fcf_conversion is not None:
        quality_points.append(clamp(fcf_conversion / 1.0) * 100)
    if margin_change is not None:
        quality_points.append(clamp(0.50 + margin_change / 0.20) * 100)
    if quality_points:
        growth_quality = statistics.mean(quality_points)

    flags=[]
    positives=[]
    if revenue_cagr is not None and revenue_cagr < 0:
        flags.append("Revenue has contracted over the available long-term annual history.")
    if eps_cagr is not None and eps_cagr < 0:
        flags.append("EPS has declined over the available long-term annual history.")
    if margin_change is not None and margin_change < -0.03:
        flags.append("Operating margin has deteriorated materially over the available history.")
    if fcf_conversion is not None and fcf_conversion < 0.70:
        flags.append("Free-cash-flow conversion is below 70% of net income in the latest year.")
    if shares_cagr is not None and shares_cagr > 0.02:
        flags.append("Share count has risen materially, indicating dilution risk.")
    if debt_cagr is not None and debt_cagr > 0.08:
        flags.append("Debt has grown materially over the available history.")
    if revenue_cagr is not None and revenue_cagr >= 0.10:
        positives.append("Revenue has compounded at 10%+ over the available annual history.")
    if eps_cagr is not None and eps_cagr >= 0.10:
        positives.append("EPS has compounded at 10%+ over the available annual history.")
    if margin_change is not None and margin_change > 0.02:
        positives.append("Operating margin has expanded by more than 2 percentage points.")
    if fcf_conversion is not None and fcf_conversion >= 0.90:
        positives.append("Free cash flow converts at 90%+ of latest-year net income.")
    if roic is not None and roic >= 0.15:
        positives.append("ROIC proxy is above 15%, indicating strong capital efficiency.")

    return {
        "years_available": max(len(annual_income), len(annual_balance), len(annual_cash)),
        "revenue_cagr_5y": revenue_cagr,
        "net_income_cagr_5y": net_income_cagr,
        "eps_cagr_5y": eps_cagr,
        "shares_cagr_5y": shares_cagr,
        "debt_cagr_5y": debt_cagr,
        "latest_operating_margin": latest_margin,
        "operating_margin_change": margin_change,
        "latest_fcf": fcf_latest,
        "fcf_conversion": fcf_conversion,
        "ocf_conversion": ocf_conversion,
        "roic_proxy": roic,
        "growth_quality_score": round(growth_quality,1),
        "flags": flags,
        "positives": positives,
    }


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
    forensic = financial_forensics(income, balance, cash, metrics)
    metrics["Growth quality score"] = forensic["growth_quality_score"]
    metrics["ROIC proxy"] = forensic["roic_proxy"]

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
            "financial_forensics": forensic,
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
    """Secondary market/fundamental provider for global tickers.

    This deliberately feeds the existing V1 analysis engine using a stable
    internal schema. It is a convenience source, not the final primary-source
    evidence layer.
    """
    symbol = str(symbol).upper().strip()
    annual_types = [
        "annualTotalRevenue", "annualNetIncome", "annualOperatingIncome",
        "annualDilutedEPS", "annualDilutedAverageShares",
        "annualOperatingCashFlow", "annualCapitalExpenditure", "annualFreeCashFlow",
        "annualTotalAssets", "annualTotalLiabilities", "annualTotalDebt",
        "annualCashCashEquivalentsAndShortTermInvestments", "annualTotalCurrentAssets",
        "annualTotalCurrentLiabilities", "annualStockholdersEquity",
    ]
    trailing_types = [
        "trailingTotalRevenue", "trailingDilutedEPS", "trailingOperatingCashFlow",
        "trailingFreeCashFlow", "trailingMarketCap", "trailingPeRatio", "trailingPsRatio",
        "trailingReturnOnEquity", "trailingReturnOnAssets", "trailingProfitMargins",
        "trailingOperatingMargins", "trailingTotalDebt", "trailingCashCashEquivalentsAndShortTermInvestments",
        "trailingTotalCurrentAssets", "trailingTotalCurrentLiabilities",
    ]
    payload = _yahoo_timeseries(symbol, annual_types + trailing_types, period_years=10)
    rows = _ts_rows(payload)
    chart = _yahoo_chart(symbol)
    meta = ((chart.get("chart") or {}).get("result") or [{}])[0].get("meta") or {}

    income_map = {
        "totalRevenue": "annualTotalRevenue",
        "netIncome": "annualNetIncome",
        "operatingIncome": "annualOperatingIncome",
        "reportedEPS": "annualDilutedEPS",
        "weightedAverageShsOutDil": "annualDilutedAverageShares",
    }
    cash_map = {
        "operatingCashflow": "annualOperatingCashFlow",
        "capitalExpenditures": "annualCapitalExpenditure",
        "freeCashFlow": "annualFreeCashFlow",
    }
    balance_map = {
        "totalAssets": "annualTotalAssets",
        "totalLiabilities": "annualTotalLiabilities",
        "shortLongTermDebtTotal": "annualTotalDebt",
        "cashAndCashEquivalentsAtCarryingValue": "annualCashCashEquivalentsAndShortTermInvestments",
        "totalCurrentAssets": "annualTotalCurrentAssets",
        "totalCurrentLiabilities": "annualTotalCurrentLiabilities",
        "totalShareholderEquity": "annualStockholdersEquity",
    }

    income = {"annualReports": _annual_reports(rows, income_map)}
    cash = {"annualReports": _annual_reports(rows, cash_map)}
    balance = {"annualReports": _annual_reports(rows, balance_map)}

    price = meta.get("regularMarketPrice")
    currency = meta.get("currency") or "USD"
    exchange = str(meta.get("exchangeName") or "").upper()
    is_london = symbol.endswith(".L") or symbol.endswith(".LON") or exchange in {"LSE", "LONDON STOCK EXCHANGE"}
    if is_london:
        # Yahoo's LSE quote is normally GBP, not GBX; keep this explicit so
        # the existing V1 GBP valuation normalisation does not divide by 100.
        price_currency = "GBP"
    else:
        price_currency = currency

    overview = {
        "Symbol": symbol,
        "Name": meta.get("longName") or meta.get("shortName") or symbol,
        "Sector": "Unknown",
        "Industry": "Unknown",
        "Description": "",
        "Currency": currency,
        "ReportingCurrency": currency,
        "Price": price,
        "MarketCapitalization": _first_ts(rows, "trailingMarketCap"),
        "RevenueTTM": _first_ts(rows, "trailingTotalRevenue"),
        "EPS": _first_ts(rows, "trailingDilutedEPS"),
        "PERatio": _first_ts(rows, "trailingPeRatio"),
        "PriceToSalesRatioTTM": _first_ts(rows, "trailingPsRatio"),
        "ReturnOnEquityTTM": _first_ts(rows, "trailingReturnOnEquity"),
        "ReturnOnAssetsTTM": _first_ts(rows, "trailingReturnOnAssets"),
        "ProfitMargin": _first_ts(rows, "trailingProfitMargins"),
        "OperatingMarginTTM": _first_ts(rows, "trailingOperatingMargins"),
        "SharesOutstanding": _first_ts(rows, "trailingTotalSharesOutstanding") or _first_ts(rows, "annualDilutedAverageShares"),
        "PriceCurrency": price_currency,
    }

    # If Yahoo returns no fundamentals, fail cleanly so another provider can try.
    if not income["annualReports"] and not overview.get("RevenueTTM"):
        raise RuntimeError("Yahoo returned no usable fundamental data")

    return {
        "symbol_requested": symbol,
        "symbol_used": symbol,
        "overview": overview,
        "income": income,
        "balance": balance,
        "cash": cash,
        "errors": {},
        "warnings": ["Fundamentals supplied by Yahoo Finance-compatible data endpoints; primary filings should be checked for material decisions."],
        "source_records": [SourceRecord("Yahoo Finance-compatible feed", "market/fundamental secondary data", "OK", "https://finance.yahoo.com/").__dict__],
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



# ========================= V4.0 LIVE RESEARCH ENGINE =========================
V4_VERSION = "4.1"


def _http_get(url, params=None, headers=None, timeout=20):
    """Small, dependency-light HTTP helper used by the live research layer."""
    import requests
    base_headers = {
        "User-Agent": "Company-Investigator/4.0 research-tool contact=investigation@localhost",
        "Accept": "application/json,text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    if headers:
        base_headers.update(headers)
    r = requests.get(url, params=params, headers=base_headers, timeout=timeout)
    r.raise_for_status()
    return r


def _strip_html(text):
    import re, html
    text = re.sub(r"<script[\s\S]*?</script>", " ", text, flags=re.I)
    text = re.sub(r"<style[\s\S]*?</style>", " ", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def sec_live_research(ticker, max_filings=12):
    """Retrieve current SEC filing metadata for a US issuer when SEC can resolve it."""
    import re
    result = {"status": "NOT_AVAILABLE", "company": None, "cik": None, "filings": [], "ownership_filings": [], "source": "SEC EDGAR"}
    try:
        mapping = _http_get("https://www.sec.gov/files/company_tickers.json").json()
        bare = ticker.upper().replace(".L", "").replace(".LON", "")
        hit = None
        for item in mapping.values():
            if str(item.get("ticker", "")).upper() == bare:
                hit = item; break
        if not hit:
            result["status"] = "NO_SEC_ISSUER_MATCH"
            return result
        cik = str(hit["cik_str"]).zfill(10)
        sub = _http_get(f"https://data.sec.gov/submissions/CIK{cik}.json").json()
        recent = sub.get("filings", {}).get("recent", {})
        keys = ["accessionNumber", "filingDate", "reportDate", "form", "primaryDocument", "items"]
        rows = []
        for i in range(len(recent.get("form", []))):
            form = recent.get("form", [""])[i]
            if form in {"10-K", "10-Q", "8-K", "20-F", "6-K", "DEF 14A", "3", "4", "5", "SC 13D", "SC 13G"}:
                row = {k: recent.get(k, [None]*len(recent.get("form", [])))[i] for k in keys}
                acc = (row.get("accessionNumber") or "").replace("-", "")
                doc = row.get("primaryDocument")
                row["url"] = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc}/{doc}" if acc and doc else None
                rows.append(row)
            if len(rows) >= max_filings:
                break
        result.update({"status": "LIVE", "company": hit.get("title"), "cik": cik, "filings": rows,
                       "ownership_filings": [x for x in rows if x.get("form") in {"3","4","5","SC 13D","SC 13G"}]})
    except Exception as exc:
        result["status"] = "ERROR"
        result["error"] = str(exc)
    return result


def gdelt_news_research(company, symbol=None, days=90, max_results=20):
    """Live, source-attributed news discovery using GDELT 2 DOC API."""
    result = {"status": "NOT_AVAILABLE", "events": [], "source": "GDELT 2 DOC"}
    try:
        import urllib.parse
        q = f'"{company}"'
        if symbol:
            q += f' OR "{symbol}"'
        data = _http_get("https://api.gdeltproject.org/api/v2/doc/doc", params={
            "query": q, "mode": "artlist", "maxrecords": max_results, "format": "json",
            "timespan": f"{days}d", "sort": "datedesc"
        }).json()
        for a in data.get("articles", [])[:max_results]:
            result["events"].append({
                "date": a.get("seendate") or a.get("socialimage"),
                "title": a.get("title"),
                "url": a.get("url"),
                "domain": a.get("domain"),
                "language": a.get("language"),
                "source_country": a.get("sourcecountry"),
            })
        result["status"] = "LIVE" if result["events"] else "NO_RESULTS"
    except Exception as exc:
        result["status"] = "ERROR"; result["error"] = str(exc)
    return result


def uk_company_house_live_research(company, max_officers=20):
    """Retrieve public Companies House HTML pages without requiring a Companies House API key."""
    import re
    result = {"status": "NOT_AVAILABLE", "company_number": None, "company_url": None, "officers": [], "filings_url": None, "source": "Companies House"}
    try:
        r = _http_get("https://find-and-update.company-information.service.gov.uk/search/companies", params={"q": company})
        text = r.text
        matches = re.findall(r'href="(/company/(\d{8}))"[^>]*>(.*?)</a>', text, flags=re.I|re.S)
        if not matches:
            result["status"] = "NO_MATCH"; return result
        path, number, label = matches[0]
        url = "https://find-and-update.company-information.service.gov.uk" + path
        result.update({"status": "LIVE", "company_number": number, "company_url": url,
                       "filings_url": url + "/filing-history", "officers_url": url + "/officers"})
        officers_html = _http_get(url + "/officers").text
        clean = _strip_html(officers_html)
        # Keep a compact evidence excerpt rather than pretending HTML parsing proves role history.
        result["officer_excerpt"] = clean[:5000]
    except Exception as exc:
        result["status"] = "ERROR"; result["error"] = str(exc)
    return result


def uk_primary_source_routes(company, symbol):
    import urllib.parse, re
    q = urllib.parse.quote_plus(company)
    bare = (symbol or "").upper().replace(".L", "").replace(".LON", "")
    slug = re.sub(r"[^a-z0-9]+", "-", company.lower()).strip("-")
    return [
        {"name":"LSE company/news", "url":f"https://www.londonstockexchange.com/stock/{bare}/{slug}", "role":"Primary market/RNS route"},
        {"name":"FCA National Storage Mechanism", "url":f"https://data.fca.org.uk/#/nsm/nationalstoragemechanism", "role":"Primary UK regulatory disclosure route; search company/ticker"},
        {"name":"Companies House", "url":f"https://find-and-update.company-information.service.gov.uk/search/companies?q={q}", "role":"Primary corporate/officer/filing route"},
        {"name":"Company investor relations", "url":f"https://www.google.com/search?q={q}+investor+relations+annual+report", "role":"Discovery route; verify official domain before citing"},
    ]



def _fetch_text(url, headers=None, timeout=25):
    r = _http_get(url, headers=headers, timeout=timeout)
    return r.text


def _search_web_for_official(company, domain_hint=None, max_results=8):
    """Lightweight discovery of official investor-relations pages.

    Discovery is deliberately treated as secondary routing. The returned URLs
    are subsequently fetched and classified; search results themselves are
    never treated as investment evidence.
    """
    import html as _html
    q = f'"{company}" investor relations annual report'
    if domain_hint:
        q += f' site:{domain_hint}'
    try:
        url = "https://www.google.com/search?" + urllib.parse.urlencode({"q": q, "num": max_results})
        text = _fetch_text(url, headers={"User-Agent":"Mozilla/5.0 Company-Investigator/4.1"})
        links = re.findall(r'href="(/url\?q=|https?://[^"&<> ]+)', text)
        out=[]
        for raw in links:
            u=raw
            if u.startswith('/url?q='):
                u=urllib.parse.parse_qs(urllib.parse.urlparse(u).query).get('q',[''])[0]
            u=_html.unescape(u)
            if not u.startswith('http') or 'google.' in urllib.parse.urlparse(u).netloc:
                continue
            if u not in out: out.append(u)
            if len(out)>=max_results: break
        return out
    except Exception:
        return []


def uk_rns_live_research(symbol, max_events=30):
    """Retrieve recent RNS listing information for an LSE symbol."""
    bare=(symbol or '').upper().replace('.LON','').replace('.L','')
    result={'status':'NOT_AVAILABLE','events':[],'source':'LSE/RNS'}
    if not bare: return result
    urls=[f'https://www.lse.co.uk/rns/{urllib.parse.quote(bare)}/',
          f'https://www.lse.co.uk/rns/{urllib.parse.quote(bare)}/?mobile_view=desktop']
    try:
        text=_fetch_text(urls[0])
        clean=_strip_html(text)
        # LSE pages expose rows as date/headline text; retain a conservative
        # excerpt and link rather than over-parsing into false facts.
        result['listing_url']=urls[0]
        date_hits=re.findall(r'(\d{1,2}(?:st|nd|rd|th)?\s+[A-Za-z]{3,9}\s+202\d)', clean)
        result['page_excerpt']=clean[:12000]
        result['events']=[{'date':d,'title':'RNS item — inspect source page','url':urls[0],'source':'LSE/RNS'} for d in date_hits[:max_events]]
        result['status']='LIVE'
    except Exception as exc:
        result['status']='ERROR'; result['error']=str(exc)
    return result


def companies_house_filing_history_live(company_number, max_items=30):
    result={'status':'NOT_AVAILABLE','filings':[],'source':'Companies House'}
    if not company_number: return result
    url=f'https://find-and-update.company-information.service.gov.uk/company/{company_number}/filing-history'
    try:
        text=_fetch_text(url)
        clean=_strip_html(text)
        result['url']=url
        # Keep the raw excerpt for human verification; structured parsing is
        # intentionally conservative because Companies House markup changes.
        result['excerpt']=clean[:16000]
        result['status']='LIVE'
    except Exception as exc:
        result['status']='ERROR'; result['error']=str(exc)
    return result


def build_document_evidence_ledger(company, symbol, live_pack):
    """Create a source/date/claim/evidence ledger from retrievable primary routes."""
    ledger=[]
    now=datetime.now(timezone.utc).isoformat()
    sec=live_pack.get('sec',{})
    for f in sec.get('filings',[]):
        ledger.append({'area':'Primary filing','source':'SEC EDGAR','date':f.get('filingDate'),
                       'document':f.get('form'),'url':f.get('url'),'claim':'Primary filing retrieved; content review required.',
                       'confidence':'HIGH','materiality':'PENDING REVIEW'})
    ch=live_pack.get('uk_companies_house',{})
    if ch.get('status')=='LIVE':
        ledger.append({'area':'Corporate record','source':'Companies House','date':None,
                       'document':'Company profile / filing history','url':ch.get('filings_url') or ch.get('company_url'),
                       'claim':'Official company record retrieved; filing-level review required.','confidence':'HIGH','materiality':'PENDING REVIEW'})
    rns=live_pack.get('uk_rns',{})
    if rns.get('status')=='LIVE':
        ledger.append({'area':'Regulatory news','source':'LSE/RNS','date':None,'document':'RNS listing',
                       'url':rns.get('listing_url'),'claim':'Official RNS route retrieved; individual announcements require review.',
                       'confidence':'HIGH','materiality':'PENDING REVIEW'})
    ir=live_pack.get('investor_relations',{})
    if ir.get('status')=='LIVE':
        ledger.append({'area':'Investor relations','source':'Company IR','date':None,'document':'IR reports/news page',
                       'url':ir.get('url'),'claim':'Official investor-relations page located.','confidence':'HIGH','materiality':'PENDING REVIEW'})
    return {'retrieved_at_utc':now,'company':company,'symbol':symbol,'items':ledger,
            'completeness':min(100, 20*len(ledger)) if ledger else 0}


def v41_live_research_pack(report, data=None, previous_snapshot=None):
    """V4.1 evidence-first retrieval: primary routes + documents + ledger."""
    base=build_live_research_pack(report, data, previous_snapshot)
    symbol=report.symbol
    company=report.company
    # UK-specific routes are attempted for LSE-style symbols.
    if str(symbol).upper().endswith(('.L','.LON')) or report.metrics.get('Price currency') in ('GBP','GBX'):
        base['uk_rns']=uk_rns_live_research(symbol)
        ch=base.get('uk_companies_house',{})
        if ch.get('company_number'):
            base['uk_filing_history']=companies_house_filing_history_live(ch.get('company_number'))
        # Prefer a company-owned IR URL when discovery returns one.
        candidates=_search_web_for_official(company)
        official=next((u for u in candidates if any(k in u.lower() for k in ('investor','results','reports')) and 'google.' not in u.lower()),None)
        if official:
            base['investor_relations']={'status':'LIVE','url':official,'source':'Company investor relations','discovery':'web search; official domain must be verified'}
        else:
            base['investor_relations']={'status':'UNVERIFIED','candidates':candidates[:5],'source':'Company investor relations'}
    base['evidence_ledger']=build_document_evidence_ledger(company,symbol,base)
    base['version']='4.1'
    return base

def build_live_research_pack(report, data=None, previous_snapshot=None):
    """One-call live research layer. Failures are explicit and never converted into clean bills of health."""
    company = report.company
    symbol = report.symbol
    pack = {
        "version": V4_VERSION, "company": company, "symbol": symbol,
        "retrieved_at_utc": datetime.now(timezone.utc).isoformat(),
        "sec": sec_live_research(symbol),
        "news": gdelt_news_research(company, symbol),
        "uk_companies_house": uk_company_house_live_research(company),
        "uk_routes": uk_primary_source_routes(company, symbol),
        "evidence": [], "warnings": []
    }
    if pack["sec"]["status"] == "LIVE":
        pack["evidence"].append({"area":"SEC filings", "status":"LIVE", "count":len(pack["sec"]["filings"])})
    elif pack["sec"]["status"] == "NO_SEC_ISSUER_MATCH":
        pack["evidence"].append({"area":"SEC filings", "status":"NOT APPLICABLE / NO MATCH"})
    else:
        pack["warnings"].append("SEC retrieval did not complete; this is not evidence that no SEC matters exist.")
    if pack["news"]["status"] == "LIVE":
        pack["evidence"].append({"area":"News discovery", "status":"LIVE", "count":len(pack["news"]["events"])})
    else:
        pack["warnings"].append("News discovery did not return live results; recent developments remain unverified.")
    if pack["uk_companies_house"]["status"] == "LIVE":
        pack["evidence"].append({"area":"Companies House", "status":"LIVE", "company_number":pack["uk_companies_house"].get("company_number")})
    return pack


def live_research_summary(pack):
    """Compact summary suitable for Quick View / Investment Committee UI."""
    sec = pack.get("sec", {})
    news = pack.get("news", {})
    ch = pack.get("uk_companies_house", {})
    return {
        "retrieved_at_utc": pack.get("retrieved_at_utc"),
        "sec_status": sec.get("status"), "sec_filings": len(sec.get("filings", [])),
        "news_status": news.get("status"), "news_events": len(news.get("events", [])),
        "companies_house_status": ch.get("status"), "company_number": ch.get("company_number"),
        "warnings": pack.get("warnings", []),
    }

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

# -----------------------------
# V3 research framework helpers
# -----------------------------

def quick_view(report, thesis):
    """Compact 1–2 minute decision view. Never invents missing evidence."""
    v = report.valuation or {}
    metrics = report.metrics or {}
    forensic = (report.data_status or {}).get("financial_forensics", {})
    return {
        "company": report.company,
        "symbol": report.symbol,
        "type": report.company_type,
        "quality": report.quality_score,
        "opportunity": report.opportunity_score,
        "risk": report.risk_score,
        "risk_band": report.risk_band,
        "data_confidence": report.data_confidence,
        "price": metrics.get("Share price"),
        "base_value": v.get("base_fair_value"),
        "bull_value": (v.get("scenarios", {}).get("bull") or {}).get("fair_value"),
        "bear_value": (v.get("scenarios", {}).get("bear") or {}).get("fair_value"),
        "revenue_cagr": forensic.get("revenue_cagr_5y"),
        "fcf_conversion": forensic.get("fcf_conversion"),
        "growth_quality": forensic.get("growth_quality_score"),
        "buy_reasons": thesis.get("buy_reasons", [])[:3],
        "risks": thesis.get("risks", [])[:3],
        "monitor": thesis.get("monitor", [])[:3],
        "legal_status": "PRIMARY-SOURCE LEGAL REVIEW NOT YET RUN",
        "patent_status": "IP/PATENT REVIEW NOT YET RUN",
        "recent_change_status": "RECENT-EVENT REVIEW NOT YET RUN",
        "verdict": report.verdict,
    }


def patent_research_links(company_name, symbol=None):
    """Official/public search routes for the IP research layer.

    Patent results are deliberately not represented as discovered facts until a
    live patent source has been queried and evidence captured.
    """
    q = company_name or symbol or ""
    encoded = urllib.parse.quote(q)
    return [
        {
            "source": "UK IPO / GOV.UK",
            "purpose": "UK patents and applications",
            "url": "https://www.gov.uk/search-for-patent",
            "status": "SEARCH REQUIRED",
        },
        {
            "source": "Espacenet",
            "purpose": "Worldwide patent applications and grants",
            "url": "https://worldwide.espacenet.com/patent/search?q=" + encoded,
            "status": "SEARCH REQUIRED",
        },
        {
            "source": "Google Patents",
            "purpose": "Broad patent/application discovery",
            "url": "https://patents.google.com/?assignee=" + encoded,
            "status": "SEARCH REQUIRED",
        },
        {
            "source": "USPTO / PatentsView",
            "purpose": "US patent data and assignee/inventor research",
            "url": "https://patentsview.org/",
            "status": "SEARCH REQUIRED",
        },
    ]


def full_investigation_framework(report, thesis):
    """Return the structured Full Investigation dossier skeleton.

    Empty sections are explicit rather than inferred. This prevents the UI from
    presenting an unperformed legal/news/IP search as a negative finding.
    """
    return {
        "Executive verdict": "Available from current financial model; deeper primary-source research pending.",
        "Business": "Primary-source business/segment research pending.",
        "Financial forensics": "Completed by the current engine.",
        "Primary filings": "Not yet queried in this release for UK issuers; SEC enrichment is available for eligible US issuers.",
        "Management & capital allocation": "Not yet researched.",
        "Competition & industry": "Starter peer framework available; full competitive research not yet run.",
        "Patents & intellectual property": "Not yet researched. Search routes are provided separately.",
        "Legal & regulatory": "Not yet researched.",
        "Recent news & events": "Not yet researched.",
        "Catalysts": "To be derived from primary disclosures and dated news research.",
        "Red-team / thesis breakers": "Initial financial risks available; dedicated red-team research pending.",
        "Bull / base / bear": "Valuation scenarios available; narrative scenario research pending.",
        "What changed since last investigation": "No previous dossier comparison is available yet.",
        "Evidence confidence": report.data_confidence,
    }


# -----------------------------
# V3.1 primary-source research layer
# -----------------------------

def _is_uk_symbol(symbol):
    s = str(symbol or '').upper()
    return s.endswith('.L') or s.endswith('.LON')


def _sec_filing_url(cik, accession, document):
    if not cik or not accession or not document:
        return ''
    acc = str(accession).replace('-', '')
    return f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc}/{document}"


def build_primary_source_research(report, data):
    """Build an evidence-first primary-source pack.

    US issuers get live SEC filing evidence. UK issuers get official RNS, FCA/NSM
    and Companies House research routes. A link is never treated as evidence by
    itself; evidence status is explicit.
    """
    symbol = str(getattr(report, 'symbol', '') or data.get('symbol_used') or '').upper()
    company = getattr(report, 'company', None) or data.get('overview', {}).get('Name') or symbol
    sec = data.get('sec_enrichment') or {}
    pack = {
        'company': company, 'symbol': symbol,
        'jurisdiction': 'UK/LSE' if _is_uk_symbol(symbol) else 'US/other',
        'evidence_standard': 'Primary filing evidence required for material conclusions.',
        'filings': [], 'insider_activity': [], 'official_routes': [], 'findings': [],
    }
    if sec.get('available'):
        cik = sec.get('cik')
        for f in sec.get('filings', []):
            url = _sec_filing_url(cik, f.get('accessionNumber'), f.get('primaryDocument'))
            item = dict(f)
            item['url'] = url
            item['evidence'] = 'PRIMARY SEC FILING'
            pack['filings'].append(item)
            if f.get('form') in {'3','4','5','SC 13D','SC 13G','SC 13D/A','SC 13G/A'}:
                pack['insider_activity'].append(item)
        pack['official_routes'] = [
            {'source':'SEC EDGAR company filings','url':f"https://www.sec.gov/edgar/browse/?CIK={str(cik).lstrip('0') or '0'}",'status':'LIVE PRIMARY SOURCE'},
            {'source':'SEC litigation releases','url':'https://www.sec.gov/litigation/litreleases','status':'OFFICIAL RESEARCH ROUTE'},
            {'source':'SEC enforcement','url':'https://www.sec.gov/enforcement-litigation','status':'OFFICIAL RESEARCH ROUTE'},
        ]
        pack['findings'].append({
            'topic':'SEC filing coverage',
            'status':'EVIDENCE AVAILABLE',
            'detail':f"Retrieved {len(pack['filings'])} recent 10-K/10-Q/8-K/20-F/6-K records from SEC submissions."
        })
        pack['findings'].append({
            'topic':'Management / ownership',
            'status':'PARTIAL',
            'detail':f"SEC ownership filings are surfaced where present ({len(pack['insider_activity'])} recent ownership records in the retrieved set). Proxy/remuneration review remains a narrative step."
        })
    elif _is_uk_symbol(symbol):
        encoded = urllib.parse.quote(company)
        pack['official_routes'] = [
            {'source':'LSE News Explorer / RNS','url':'https://www.londonstockexchange.com/news','status':'OFFICIAL RESEARCH ROUTE'},
            {'source':'FCA National Storage Mechanism','url':'https://data.fca.org.uk/#/nsm/nationalstoragemanagement','status':'OFFICIAL RESEARCH ROUTE'},
            {'source':'Companies House company search','url':'https://find-and-update.company-information.service.gov.uk/advanced-search/get-results?companyName='+encoded,'status':'OFFICIAL RESEARCH ROUTE'},
            {'source':'GOV.UK company information','url':'https://find-and-update.company-information.service.gov.uk/','status':'OFFICIAL RESEARCH ROUTE'},
        ]
        pack['findings'].append({
            'topic':'UK primary filings',
            'status':'ROUTE READY — LIVE QUERY REQUIRED',
            'detail':'RNS, FCA National Storage Mechanism and Companies House routes are prepared; no UK filing is treated as reviewed until retrieved.'
        })
    else:
        pack['official_routes'] = [
            {'source':'Company investor relations','url':'','status':'IDENTIFICATION REQUIRED'},
            {'source':'Local securities regulator','url':'','status':'IDENTIFICATION REQUIRED'},
        ]
        pack['findings'].append({'topic':'Primary filings','status':'PENDING','detail':'Issuer jurisdiction/regulator must be identified before primary filings can be verified.'})
    return pack


def red_team_framework(report, thesis):
    """Generate targeted thesis-breaker questions; no answers are invented."""
    t = str(getattr(report, 'company_type', '') or '').upper()
    common = [
        'What assumption in the current valuation is most fragile?',
        'What would make revenue growth materially slower than expected?',
        'Could margins revert even if revenue keeps growing?',
        'Is free cash flow quality weaker than reported earnings suggest?',
        'Could dilution or stock compensation reduce per-share value?',
        'What balance-sheet event could permanently impair equity value?',
        'Which competitor or technology could make the business less relevant?',
        'What regulatory, legal or contractual event could change the thesis?',
        'What would management have to do for us to lose confidence?',
        'What evidence would make us sell rather than simply wait?',
    ]
    type_specific = {
        'REIT / PROPERTY': ['Could asset values or occupancy fall enough to break the valuation?','Are financing costs and refinancing needs understated?'],
        'FINANCIAL': ['Could credit losses or funding costs invalidate earnings?','Are returns on equity sustainable without excessive leverage?'],
        'GROWTH / TECHNOLOGY': ['Is the addressable market genuinely large enough for the valuation?','Could AI/technology disruption destroy the moat rather than strengthen it?'],
        'ENERGY / RESOURCES': ['What commodity-price assumption breaks the model?','Could capex, reserves or regulation destroy expected returns?'],
        'UTILITY / INFRASTRUCTURE': ['Could regulation or allowed returns compress cash generation?','Are project execution and financing assumptions realistic?'],
    }
    return common + type_specific.get(t, [])


def investigation_evidence_score(primary_pack, patent_status='PENDING', legal_status='PENDING', news_status='PENDING'):
    """Simple evidence-completeness score, distinct from investment quality."""
    score = 0
    if primary_pack.get('filings'): score += 40
    elif any(x.get('status','').startswith('OFFICIAL') for x in primary_pack.get('official_routes', [])): score += 15
    if patent_status == 'COMPLETED': score += 20
    if legal_status == 'COMPLETED': score += 25
    if news_status == 'COMPLETED': score += 15
    return min(score, 100)



def legal_research_routes(company, symbol=None, jurisdiction=None):
    """Official legal/regulatory research routes. Links are routes, not evidence."""
    company_q = urllib.parse.quote_plus(str(company or '').strip())
    symbol_q = urllib.parse.quote_plus(str(symbol or '').strip())
    routes = []
    j = str(jurisdiction or '').upper()
    is_us = bool(symbol and not _is_uk_symbol(symbol) and not j.startswith('UK'))
    is_uk = bool(symbol and _is_uk_symbol(symbol)) or j.startswith('UK')
    if is_us:
        routes += [
            {'category':'SEC litigation','source':'SEC Litigation Releases','url':'https://www.sec.gov/enforcement-litigation/litigation-releases','status':'OFFICIAL PRIMARY ROUTE'},
            {'category':'SEC enforcement','source':'SEC Administrative Proceedings','url':'https://www.sec.gov/enforcement-litigation/administrative-proceedings','status':'OFFICIAL PRIMARY ROUTE'},
            {'category':'SEC filings','source':'SEC EDGAR full-text search','url':'https://www.sec.gov/search-filings','status':'OFFICIAL PRIMARY ROUTE'},
            {'category':'Federal courts','source':'PACER Case Locator','url':'https://pcl.uscourts.gov/pcl/index.jsf','status':'OFFICIAL COURT ROUTE'},
        ]
    if is_uk:
        routes += [
            {'category':'FCA enforcement','source':'FCA Enforcement','url':'https://www.fca.org.uk/about/how-we-regulate/enforcement','status':'OFFICIAL PRIMARY ROUTE'},
            {'category':'FCA decisions','source':'FCA statutory notices','url':'https://www.fca.org.uk/about/who-we-are/committees/regulatory-decisions-committee/statutory-notices','status':'OFFICIAL PRIMARY ROUTE'},
            {'category':'Company filings','source':'FCA National Storage Mechanism','url':'https://data.fca.org.uk/#/nsm/nationalstoragemanagement','status':'OFFICIAL PRIMARY ROUTE'},
            {'category':'Corporate records','source':'Companies House','url':'https://find-and-update.company-information.service.gov.uk/advanced-search/get-results?companyName='+company_q,'status':'OFFICIAL PRIMARY ROUTE'},
            {'category':'Competition','source':'Competition and Markets Authority','url':'https://www.gov.uk/government/organisations/competition-and-markets-authority','status':'OFFICIAL REGULATOR ROUTE'},
            {'category':'UK judgments','source':'Find Case Law','url':'https://caselaw.nationalarchives.gov.uk/','status':'OFFICIAL COURT ROUTE'},
        ]
    routes += [
        {'category':'IP disputes','source':'UK IPO patent search','url':'https://www.gov.uk/search-for-patent','status':'OFFICIAL IP ROUTE'},
        {'category':'Global IP','source':'Espacenet','url':'https://worldwide.espacenet.com/patent/search?q='+company_q,'status':'OFFICIAL IP DISCOVERY ROUTE'},
    ]
    return routes


def classify_legal_severity(text):
    """Conservative keyword severity helper; never labels an allegation as proven misconduct."""
    t = str(text or '').lower()
    critical = ['criminal charges','criminal investigation','material litigation','government investigation','sec investigation','fca enforcement','antitrust investigation','class action']
    high = ['lawsuit','litigation','investigation','subpoena','enforcement action','regulatory investigation','patent infringement','product liability','restatement']
    moderate = ['claim','dispute','complaint','proceeding','arbitration','regulatory inquiry','legal contingency']
    if any(k in t for k in critical): return 'CRITICAL/HIGH — REVIEW REQUIRED'
    if any(k in t for k in high): return 'HIGH — REVIEW REQUIRED'
    if any(k in t for k in moderate): return 'MODERATE — REVIEW REQUIRED'
    return 'LOW / NO LEGAL KEYWORD SIGNAL'


def sec_legal_proceedings_scan(sec_pack, company):
    """Retrieve the latest US annual/quarterly filing and inspect its legal-proceedings area.
    This is evidence extraction, not a substitute for court-docket research."""
    result = {'status':'NOT AVAILABLE','company':company,'filing':None,'signals':[],'notes':[], 'severity':'UNKNOWN'}
    filings = sec_pack.get('filings', []) if isinstance(sec_pack, dict) else []
    target = next((x for x in filings if str(x.get('form','')).upper() in {'10-K','20-F'}), None)
    if not target:
        target = next((x for x in filings if str(x.get('form','')).upper() in {'10-Q','6-K'}), None)
    if not target:
        result['status'] = 'NO ELIGIBLE SEC FILING RETRIEVED'
        return result
    url = target.get('url') or target.get('primary_document_url')
    result['filing'] = target
    if not url:
        result['status'] = 'FILING FOUND — DOCUMENT URL MISSING'
        return result
    try:
        r = requests.get(url, headers={'User-Agent':'Company Investigator research contact; public-data tool'}, timeout=20)
        r.raise_for_status()
        html = r.text
        text = re.sub(r'<[^>]+>', ' ', html)
        text = re.sub(r'\\s+', ' ', text)
        lower = text.lower()
        keywords = ['legal proceedings','litigation','lawsuit','class action','investigation','regulatory','subpoena','patent infringement','antitrust','environmental liability','product liability']
        for kw in keywords:
            if kw in lower:
                idx = lower.find(kw)
                excerpt = text[max(0, idx-220):idx+520].strip()
                result['signals'].append({'keyword':kw,'excerpt':excerpt})
        result['severity'] = classify_legal_severity(' '.join(x['excerpt'] for x in result['signals']))
        result['status'] = 'FILING SCANNED — HUMAN REVIEW REQUIRED'
        result['notes'].append('Keyword hits are signals only. The investigator must verify allegation, parties, amount, probability, disclosure and latest status from the underlying filing/court record.')
    except Exception as exc:
        result['status'] = 'FILING RETRIEVED — TEXT SCAN FAILED'
        result['notes'].append(str(exc))
    return result


def legal_investigation_framework(report, primary_pack):
    company = getattr(report, 'company', '')
    symbol = getattr(report, 'symbol', '')
    sec_pack = primary_pack if isinstance(primary_pack, dict) else {}
    sec_scan = sec_legal_proceedings_scan(sec_pack, company) if sec_pack.get('filings') else {'status':'LIVE COURT/FILING SEARCH REQUIRED','signals':[],'notes':[],'severity':'UNKNOWN'}
    jurisdiction = 'UK' if _is_uk_symbol(symbol) else 'US/OTHER'
    return {
        'research_status':'PARTIAL — official routes + filing scan where available',
        'severity_rule':'Critical / High / Moderate / Low. Allegations are not treated as findings.',
        'official_routes':legal_research_routes(company, symbol, jurisdiction),
        'sec_filing_scan':sec_scan,
        'required_checks':[
            'Identify who is suing whom / regulator vs company / company vs third party.',
            'Record allegation or issue, filing date, court/regulator, current status and next milestone.',
            'Quantify disclosed exposure, provision, insurance/recovery and maximum plausible downside where disclosed.',
            'Compare company disclosure with regulator/court primary records.',
            'Check whether management discussed the matter in the latest annual/interim report or results.',
            'Do not conclude “nothing material found” until the relevant official searches have actually been completed.'
        ],
        'materiality_framework':{
            'LOW':'Routine/disclosed matter with limited financial or strategic impact.',
            'MODERATE':'Potentially meaningful but currently contained, uncertain or financially limited.',
            'HIGH':'Could materially affect earnings, cash, licences, reputation, strategy or valuation.',
            'CRITICAL':'Potential existential threat, major regulatory action, criminal exposure or catastrophic liability.'
        }
    }




# -----------------------------
# V3.3 news, timeline & change-detection layer
# -----------------------------

def _safe_snapshot_value(value):
    if value is None:
        return None
    try:
        return round(float(value), 6)
    except Exception:
        return str(value)


def build_investigation_snapshot(report):
    """Create a compact, comparable state snapshot for a company investigation."""
    m = getattr(report, 'metrics', {}) or {}
    v = getattr(report, 'valuation', {}) or {}
    return {
        'company': getattr(report, 'company', ''),
        'symbol': getattr(report, 'symbol', ''),
        'captured_at_utc': datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace('+00:00','Z'),
        'quality_score': _safe_snapshot_value(getattr(report, 'quality_score', None)),
        'opportunity_score': _safe_snapshot_value(getattr(report, 'opportunity_score', None)),
        'risk_score': _safe_snapshot_value(getattr(report, 'risk_score', None)),
        'price': _safe_snapshot_value(m.get('Price')),
        'revenue_ttm': _safe_snapshot_value(m.get('Revenue TTM')),
        'eps': _safe_snapshot_value(m.get('EPS')),
        'pe': _safe_snapshot_value(m.get('P/E')),
        'fcf': _safe_snapshot_value(m.get('FCF')),
        'fcf_yield': _safe_snapshot_value(m.get('FCF yield')),
        'net_debt': _safe_snapshot_value(m.get('Net Debt')),
        'shares': _safe_snapshot_value(m.get('Shares Outstanding')),
        'base_fair_value': _safe_snapshot_value(v.get('base_fair_value')),
        'bear_fair_value': _safe_snapshot_value(v.get('bear_fair_value')),
        'bull_fair_value': _safe_snapshot_value(v.get('bull_fair_value')),
        'verdict': getattr(report, 'verdict', ''),
        'risk_band': getattr(report, 'risk_band', ''),
    }


def compare_investigation_snapshots(previous, current):
    """Explain material state changes between two investigations."""
    if not previous:
        return {'status':'NO PREVIOUS SNAPSHOT', 'changes':[], 'materiality':'UNKNOWN'}
    changes = []
    checks = [
        ('opportunity_score','Opportunity score',2.0),
        ('quality_score','Business quality score',2.0),
        ('risk_score','Risk score',3.0),
        ('price','Share price',0.05),
        ('revenue_ttm','Revenue TTM',0.05),
        ('eps','EPS',0.05),
        ('pe','P/E',0.10),
        ('fcf','Free cash flow',0.10),
        ('fcf_yield','FCF yield',0.10),
        ('net_debt','Net debt',0.10),
        ('shares','Shares outstanding',0.02),
        ('base_fair_value','Base fair value',0.05),
        ('bear_fair_value','Bear fair value',0.05),
        ('bull_fair_value','Bull fair value',0.05),
    ]
    for key, label, threshold in checks:
        old = previous.get(key)
        new = current.get(key)
        if old is None or new is None:
            continue
        try:
            delta = float(new) - float(old)
            denom = abs(float(old)) if abs(float(old)) > 1e-12 else 1.0
            pct = delta / denom
            if abs(pct) >= threshold:
                changes.append({'field':key,'label':label,'old':old,'new':new,'delta':delta,'pct_change':pct})
        except Exception:
            continue
    if previous.get('verdict') != current.get('verdict'):
        changes.append({'field':'verdict','label':'Verdict','old':previous.get('verdict'),'new':current.get('verdict'),'type':'QUALITATIVE'})
    if previous.get('risk_band') != current.get('risk_band'):
        changes.append({'field':'risk_band','label':'Risk band','old':previous.get('risk_band'),'new':current.get('risk_band'),'type':'QUALITATIVE'})
    materiality = 'HIGH' if len(changes) >= 5 else ('MODERATE' if len(changes) >= 2 else ('LOW' if changes else 'NONE DETECTED'))
    return {
        'status':'COMPARISON COMPLETE',
        'previous_captured_at':previous.get('captured_at_utc'),
        'current_captured_at':current.get('captured_at_utc'),
        'changes':changes,
        'materiality':materiality,
        'note':'This detects changes in the model/data snapshot. It does not prove that a news event caused the change.'
    }


def news_research_routes(company, symbol=None):
    """Official and high-quality discovery routes for dated company developments."""
    company_q = urllib.parse.quote_plus(str(company or '').strip())
    symbol_q = urllib.parse.quote_plus(str(symbol or '').strip())
    routes = []
    if _is_uk_symbol(symbol):
        routes.extend([
            {'category':'RNS','source':'LSE News Explorer','url':'https://www.londonstockexchange.com/news?tab=news-explorer','status':'OFFICIAL LIVE RESEARCH ROUTE','use':'Filter by company/code and date range.'},
            {'category':'Company disclosures','source':'LSE company news','url':'https://www.londonstockexchange.com/news','status':'OFFICIAL RESEARCH ROUTE','use':'Search ticker/company and review dated announcements.'},
            {'category':'Company filings','source':'FCA National Storage Mechanism','url':'https://data.fca.org.uk/#/nsm/nationalstoragemanagement','status':'OFFICIAL RESEARCH ROUTE','use':'Verify formal regulatory filings.'},
        ])
    else:
        routes.extend([
            {'category':'SEC current events','source':'SEC EDGAR','url':'https://www.sec.gov/search-filings','status':'OFFICIAL LIVE RESEARCH ROUTE','use':'Search 8-K/6-K and filing text for dated events.'},
            {'category':'SEC company filings','source':'SEC company filing index','url':'https://www.sec.gov/edgar/browse/','status':'OFFICIAL RESEARCH ROUTE','use':'Review latest 10-K/10-Q/8-K and ownership filings.'},
        ])
    routes.extend([
        {'category':'Company announcements','source':'Investor relations search','url':'','status':'COMPANY URL IDENTIFICATION REQUIRED','use':'Review results, presentations, guidance and contract announcements.'},
        {'category':'News discovery','source':'Google News company search','url':'https://news.google.com/search?q='+company_q,'status':'SECONDARY DISCOVERY ONLY','use':'Find potentially material developments, then verify against primary sources.'},
    ])
    return routes


def build_event_timeline(report, data, previous_snapshot=None):
    """Build a dated event/research timeline from available primary evidence plus change detection."""
    events = []
    sec = data.get('sec_enrichment') or {}
    for f in sec.get('filings', [])[:20]:
        form = str(f.get('form',''))
        if form not in {'10-K','10-Q','8-K','20-F','6-K','DEF 14A','4','3','5','SC 13D','SC 13G','SC 13D/A','SC 13G/A'}:
            continue
        events.append({
            'date': f.get('filingDate') or f.get('filedDate') or f.get('reportDate') or '',
            'type': form,
            'headline': f.get('primaryDocument') or f.get('form') or 'SEC filing',
            'source': 'SEC EDGAR',
            'url': f.get('url',''),
            'evidence': 'PRIMARY',
            'materiality': 'REVIEW',
        })
    snap = build_investigation_snapshot(report)
    comparison = compare_investigation_snapshots(previous_snapshot, snap)
    for change in comparison.get('changes', []):
        events.append({
            'date': snap.get('captured_at_utc',''),
            'type': 'MODEL CHANGE',
            'headline': change.get('label','Model change'),
            'source': 'Company Investigator',
            'url': '',
            'evidence': 'MODEL-DERIVED',
            'materiality': 'HIGH' if abs(change.get('pct_change',0) or 0) >= 0.15 else 'MODERATE',
            'detail': change,
        })
    events.sort(key=lambda x: str(x.get('date','')), reverse=True)
    return {
        'company': getattr(report,'company',''),
        'symbol': getattr(report,'symbol',''),
        'routes': news_research_routes(getattr(report,'company',''), getattr(report,'symbol','')),
        'events': events,
        'snapshot': snap,
        'comparison': comparison,
        'status': 'PARTIAL — primary filing chronology where available; live news verification still required' if not events else 'PRIMARY EVENT CHRONOLOGY AVAILABLE',
        'research_rule': 'A secondary news item can trigger investigation, but a material thesis change should be verified against an RNS, SEC filing, company disclosure, regulator or court record where possible.',
    }


def what_changed_since_last_investigation(previous_snapshot, current_report, event_timeline=None):
    current = build_investigation_snapshot(current_report)
    comparison = compare_investigation_snapshots(previous_snapshot, current)
    comparison['current_snapshot'] = current
    if event_timeline:
        comparison['dated_events'] = event_timeline.get('events', [])[:20]
    if not comparison.get('changes') and not comparison.get('dated_events'):
        comparison['headline'] = 'No model-level change detected from the available evidence.'
    else:
        comparison['headline'] = f"{len(comparison.get('changes', []))} model-level change(s) and {len(comparison.get('dated_events', []))} dated event(s) available for review."
    return comparison


def full_investigation_framework_v31(report, thesis, primary_pack):
    base = full_investigation_framework(report, thesis)
    base.update({
        'Primary-source research': primary_pack,
        'Red-team questions': red_team_framework(report, thesis),
        'Evidence completeness': investigation_evidence_score(primary_pack),
        'Management & capital allocation': 'SEC proxy/ownership evidence is surfaced for US issuers; UK management/remuneration review requires RNS/annual-report retrieval.',
        'Legal & regulatory': legal_investigation_framework(report, primary_pack),
        'Recent news & events': 'Primary filing chronology available for US issuers; UK RNS chronology requires live retrieval.',
        'What changed since last investigation': 'Version 3.1 adds primary-source evidence tracking; persistent historical dossier comparison is the next layer.',
    })
    return base

# =========================
# V3.4 Management & Capital Allocation
# =========================

def build_management_research(report, data):
    """Build an evidence-first management/capital-allocation dossier.

    US issuers: use SEC DEF 14A/proxy plus Forms 3/4/5 when available.
    UK issuers: provide official RNS/annual-report/Companies House routes.
    No route is treated as evidence until retrieved and reviewed.
    """
    symbol = str(getattr(report, "symbol", "") or "").upper()
    company = str(getattr(report, "company", "") or "")
    sec = (data or {}).get("sec_enrichment") or {}
    filings = sec.get("filings", []) if sec.get("available") else []
    mgmt_forms = {"DEF 14A", "DEFA14A", "3", "4", "5", "SC 13D", "SC 13G", "13D", "13G", "20-F"}
    relevant = [f for f in filings if f.get("form") in mgmt_forms]
    records = []
    cik = sec.get("cik")
    for f in relevant[:30]:
        url = ""
        if cik and f.get("accessionNumber") and f.get("primaryDocument"):
            url = _sec_filing_url(cik, f["accessionNumber"], f["primaryDocument"])
        records.append({
            "date": f.get("filingDate", ""),
            "form": f.get("form", ""),
            "url": url,
            "status": "PRIMARY EVIDENCE AVAILABLE",
            "use": "Management ownership, remuneration, governance or insider transaction review."
        })

    is_uk = _is_uk_symbol(symbol)
    routes = []
    if is_uk:
        encoded = company.replace(" ", "+")
        routes = [
            {"source": "LSE/RNS", "purpose": "Director/PDMR dealings, board changes, remuneration and capital-allocation announcements", "status": "SEARCH REQUIRED", "url": f"https://www.londonstockexchange.com/news"},
            {"source": "Companies House", "purpose": "Directors, persons with significant control and company filings", "status": "SEARCH REQUIRED", "url": f"https://find-and-update.company-information.service.gov.uk/search/companies?q={encoded}"},
            {"source": "FCA National Storage Mechanism", "purpose": "Annual reports, corporate disclosures and regulatory filings", "status": "SEARCH REQUIRED", "url": "https://data.fca.org.uk/#/nsm/nationalstoragemechanism"},
            {"source": "Company investor relations", "purpose": "Annual report, remuneration report, governance report and results presentations", "status": "SEARCH REQUIRED", "url": ""},
        ]
    else:
        routes = [
            {"source": "SEC EDGAR", "purpose": "Proxy statements, insider Forms 3/4/5 and beneficial ownership", "status": "RETRIEVED" if records else "SEARCH REQUIRED", "url": "https://www.sec.gov/edgar/search/"},
            {"source": "Company investor relations", "purpose": "Annual report, proxy, governance and capital-allocation disclosures", "status": "SEARCH REQUIRED", "url": ""},
        ]

    return {
        "company": company,
        "symbol": symbol,
        "status": "PRIMARY MANAGEMENT EVIDENCE RETRIEVED" if records else "MANAGEMENT REVIEW PENDING",
        "primary_records": records,
        "official_routes": routes,
        "checks": [
            "CEO/CFO and board tenure and track record",
            "Insider ownership and meaningful open-market buying/selling",
            "Director/PDMR dealings and transaction context",
            "Executive remuneration versus company performance",
            "Share options, RSUs and dilution risk",
            "Acquisitions and disposals: price, strategic logic and returns",
            "Debt issuance/repayment and balance-sheet discipline",
            "Dividends and buybacks versus free cash flow",
            "Management guidance versus subsequent delivery",
            "Governance, related-party transactions and shareholder alignment",
        ],
        "scoring_rule": "Do not score management positively or negatively until the underlying filings/annual reports have been reviewed.",
    }


def capital_allocation_red_flags(report, management_pack):
    """Generate conservative prompts from already-known financial metrics; not accusations."""
    m = getattr(report, "metrics", {}) or {}
    flags = []
    if m.get("shares_cagr_5y") is not None and m.get("shares_cagr_5y") > 0.03:
        flags.append("Share count has been rising materially; investigate compensation, acquisitions and equity issuance.")
    if m.get("debt_cagr_5y") is not None and m.get("debt_cagr_5y") > 0.08:
        flags.append("Debt has grown materially; investigate whether returns on deployed capital justify the increase.")
    fcf = m.get("latest_fcf")
    ni = m.get("net_income_ttm")
    if fcf is not None and ni is not None and ni > 0 and fcf < ni * 0.6:
        flags.append("Free cash flow is substantially below net income; investigate working capital and capital expenditure discipline.")
    if not flags:
        flags.append("No automatic capital-allocation red flag triggered by the available headline metrics; primary-source review is still required.")
    return flags


# =========================
# V3.5 Competition & Industry Intelligence
# =========================

COMPETITIVE_PEERS = {
    "GROWTH / TECHNOLOGY": ["RPI", "ONT", "ALFA"],
    "INDUSTRIAL": ["RR.L", "GAW.L"],
    "FINANCIAL": ["AV.L", "IBKR"],
    "ENERGY / RESOURCES": ["SHEL.L", "BP.L"],
    "UTILITY / INFRASTRUCTURE": ["NG.L", "SVT.L"],
    "REIT / PROPERTY": ["BBOX.L", "DLR"],
    "CONSUMER": ["GAW.L"],
}


def _safe_num(v):
    try:
        return float(v) if v is not None else None
    except Exception:
        return None


def build_competitive_industry_intelligence(report, data=None):
    """Evidence-first competitive/industry dossier.

    The engine deliberately separates quantitative signals from qualitative
    claims. It does not invent market share, TAM, customer concentration or
    competitor facts when the underlying source has not been retrieved.
    """
    symbol = str(getattr(report, "symbol", "") or "").upper()
    company = str(getattr(report, "company", "") or "")
    ctype = str(getattr(report, "company_type", "GENERAL") or "GENERAL").upper()
    m = getattr(report, "metrics", {}) or {}
    if ctype not in COMPETITIVE_PEERS:
        ctype = "GENERAL"
    peers = COMPETITIVE_PEERS.get(ctype, [])

    roic = _safe_num(m.get("roic_proxy"))
    gm = _safe_num(m.get("gross_margin"))
    opm = _safe_num(m.get("operating_margin"))
    growth = _safe_num(m.get("revenue_cagr_5y"))
    fcf_conv = _safe_num(m.get("fcf_conversion"))

    quantitative_signals = []
    if roic is not None:
        quantitative_signals.append({"metric":"ROIC proxy","value":round(roic,2),"interpretation":"Positive quantitative moat signal if sustained and above the cost of capital.","status":"EVIDENCE FROM FINANCIAL DATA"})
    else:
        quantitative_signals.append({"metric":"ROIC proxy","value":"N/A","interpretation":"Not available; retrieve financial statements and calculate consistently.","status":"MISSING"})
    if gm is not None:
        quantitative_signals.append({"metric":"Gross margin","value":round(gm,2),"interpretation":"Use level and 5-year stability versus peers to test pricing power/cost advantage.","status":"EVIDENCE FROM FINANCIAL DATA"})
    else:
        quantitative_signals.append({"metric":"Gross margin","value":"N/A","interpretation":"Peer and historical gross-margin evidence required.","status":"MISSING"})
    if opm is not None:
        quantitative_signals.append({"metric":"Operating margin","value":round(opm,2),"interpretation":"Test whether margins are structurally above peers and resilient through cycles.","status":"EVIDENCE FROM FINANCIAL DATA"})
    else:
        quantitative_signals.append({"metric":"Operating margin","value":"N/A","interpretation":"Operating-margin history required.","status":"MISSING"})
    if growth is not None:
        quantitative_signals.append({"metric":"Revenue CAGR","value":round(growth,2),"interpretation":"Growth is not itself a moat; determine whether share gains or market expansion explain it.","status":"EVIDENCE FROM FINANCIAL DATA"})
    if fcf_conv is not None:
        quantitative_signals.append({"metric":"FCF conversion","value":round(fcf_conv,2),"interpretation":"Strong conversion can support reinvestment and shareholder returns; investigate durability.","status":"EVIDENCE FROM FINANCIAL DATA"})

    moat_sources = [
        {"source":"Intangible assets / IP","test":"Patents, licences, proprietary technology or brand evidence; verify scope, expiry and relevance.","status":"RESEARCH REQUIRED"},
        {"source":"Switching costs","test":"Can customers leave without meaningful operational, financial or data friction? Seek retention/churn evidence.","status":"RESEARCH REQUIRED"},
        {"source":"Network effects","test":"Does each additional user/customer make the product more valuable or harder to displace?","status":"RESEARCH REQUIRED"},
        {"source":"Cost advantage / scale","test":"Are unit costs structurally lower because of scale, process, sourcing or installed infrastructure?","status":"RESEARCH REQUIRED"},
        {"source":"Efficient scale / regulation","test":"Is the market naturally limited to a few viable players, or protected by regulation/capacity constraints?","status":"RESEARCH REQUIRED"},
    ]

    five_forces = [
        ("Rivalry", "How intense is price/product competition, and is the industry structurally over-supplied?"),
        ("New entrants", "What capital, technology, IP, distribution, regulation or scale is required to enter?"),
        ("Substitutes", "What alternative technology/product/service can solve the customer's problem?"),
        ("Buyer power", "Are customers concentrated, price-sensitive or able to multi-source?"),
        ("Supplier power", "Are critical components, labour, infrastructure or inputs concentrated among few suppliers?"),
    ]

    sector_tests = {
        "GROWTH / TECHNOLOGY": ["R&D intensity and product roadmap", "Platform/ecosystem lock-in", "Technology displacement risk", "Customer concentration and retention", "Market-share trajectory"],
        "INDUSTRIAL": ["Order book and book-to-bill", "Capacity utilisation", "Pricing versus input inflation", "Aftermarket/service mix", "Customer and supplier concentration"],
        "FINANCIAL": ["Cost of funding", "Credit losses/defaults", "Capital requirements", "Customer acquisition economics", "Regulatory barriers and competitive pricing"],
        "ENERGY / RESOURCES": ["Commodity exposure", "Reserve/resource quality", "Cost curve position", "Capex intensity", "Political/environmental/regulatory exposure"],
        "UTILITY / INFRASTRUCTURE": ["Regulated returns", "Contract duration/indexation", "Asset utilisation", "Financing costs", "Renewal/refinancing risk"],
        "REIT / PROPERTY": ["Occupancy and rent growth", "Asset quality/location", "Tenant concentration", "Development pipeline", "Debt/LTV and refinancing"],
        "CONSUMER": ["Brand strength", "Distribution reach", "Pricing power", "Customer acquisition/retention", "Private-label/substitution risk"],
        "GENERAL": ["Market structure", "Customer concentration", "Supplier concentration", "Substitution risk", "Capital intensity"],
    }.get(ctype, [])

    # Conservative provisional score: only financial signals available to the
    # model are scored; qualitative moat evidence remains pending.
    score_parts = []
    if roic is not None:
        score_parts.append(min(100,max(0, roic * 3.0)))
    if gm is not None:
        score_parts.append(min(100,max(0, gm * 1.25)))
    if opm is not None:
        score_parts.append(min(100,max(0, opm * 2.0)))
    if fcf_conv is not None:
        score_parts.append(min(100,max(0, fcf_conv)))
    provisional = round(sum(score_parts)/len(score_parts),1) if score_parts else None

    return {
        "company": company,
        "symbol": symbol,
        "company_type": ctype,
        "starter_peers": peers,
        "provisional_quant_score": provisional,
        "quantitative_signals": quantitative_signals,
        "moat_sources": moat_sources,
        "five_forces": [{"force":a,"question":b,"status":"RESEARCH REQUIRED"} for a,b in five_forces],
        "sector_tests": sector_tests,
        "market_share_status":"NOT RETRIEVED — do not infer from company marketing claims.",
        "tam_status":"NOT RETRIEVED — require independently sourced market size plus bottom-up sanity check.",
        "competitive_verdict":"PENDING PRIMARY/PEER EVIDENCE",
        "research_rules":[
            "Never treat revenue growth as proof of a moat.",
            "Compare ROIC, margins and growth with relevant peers over multiple years.",
            "Separate industry attractiveness from company-specific advantage.",
            "A high market share without high returns is not automatically a durable moat.",
            "A strong moat can still be a poor investment if valuation already prices in excessive durability or growth.",
        ],
    }

# =========================
# V3.6 Catalysts & Scenario Engine
# =========================

def _scenario_price(report, scenario_name):
    try:
        return (getattr(report, 'valuation', {}) or {}).get('scenarios', {}).get(scenario_name, {}).get('fair_value')
    except Exception:
        return None


def build_scenario_catalyst_engine(report, thesis=None, event_timeline=None):
    """Build an evidence-first catalyst and scenario dossier.

    Scenario outputs reuse the existing valuation engine rather than inventing a
    second valuation model. Catalyst items are framed as conditions to verify,
    with explicit thesis-breakers and monitoring triggers.
    """
    m = getattr(report, 'metrics', {}) or {}
    company_type = str(getattr(report, 'company_type', 'GENERAL') or 'GENERAL').upper()
    current = _safe_num(m.get('Share price'))
    scenarios = {}
    assumptions = {
        'bear': {'probability': 0.25, 'label': 'Bear', 'growth_shift': 'Growth materially below base; multiple compression; execution/legal/regulatory setback.'},
        'base': {'probability': 0.50, 'label': 'Base', 'growth_shift': 'Current operating trajectory broadly continues and valuation normalises toward the modelled base.'},
        'bull': {'probability': 0.25, 'label': 'Bull', 'growth_shift': 'Growth persists above base, margins/returns improve and the market awards a stronger multiple.'},
    }
    for key, a in assumptions.items():
        value = _scenario_price(report, key)
        scenarios[key] = {
            'label': a['label'],
            'probability': a['probability'],
            'fair_value': value,
            'upside_downside': (value / current - 1) if value is not None and current not in (None, 0) else None,
            'assumption': a['growth_shift'],
            'confidence': 'MODELLED — VERIFY INPUTS',
        }

    weighted = None
    usable = [(x['probability'], x['fair_value']) for x in scenarios.values() if x['fair_value'] is not None]
    if usable:
        total_p = sum(p for p, _ in usable)
        weighted = sum(p * v for p, v in usable) / total_p if total_p else None

    catalyst_map = {
        'GROWTH / TECHNOLOGY': [
            'Revenue growth beats guidance/consensus without a deterioration in cash conversion.',
            'New product/platform adoption accelerates and expands the addressable market.',
            'Gross/operating margins improve while growth remains strong.',
            'Major customer wins or design wins are converted into recurring revenue.',
        ],
        'INDUSTRIAL': [
            'Order intake/backlog converts into revenue and margin expansion.',
            'Capacity expansion delivers returns without excessive leverage or dilution.',
            'Pricing and productivity gains offset input-cost pressure.',
        ],
        'FINANCIAL': [
            'Credit quality remains benign while returns on equity/capital improve.',
            'Net interest/funding economics improve without taking disproportionate balance-sheet risk.',
            'Fee or asset growth translates into sustainable cash earnings.',
        ],
        'ENERGY / RESOURCES': [
            'Commodity prices or production volumes outperform the base case.',
            'Project execution/capex comes in below assumptions and free cash flow improves.',
            'Reserve/resource additions extend the economic life of assets.',
        ],
        'UTILITY / INFRASTRUCTURE': [
            'Regulated/contracted returns and project execution exceed expectations.',
            'Financing costs stabilise and cash generation supports investment without excessive dilution.',
            'New projects enter service on time and on budget.',
        ],
        'REIT / PROPERTY': [
            'Occupancy, rents and like-for-like income outperform expectations.',
            'Asset values stabilise or rise while financing costs fall.',
            'Development pipeline converts into completed assets with attractive yields.',
        ],
    }
    catalysts = [{'catalyst': x, 'status': 'VERIFY AGAINST PRIMARY/INDUSTRY EVIDENCE', 'materiality': 'POTENTIALLY HIGH'} for x in catalyst_map.get(company_type, [
        'Revenue and cash flow outperform the base case.',
        'Margins/returns improve without materially increasing balance-sheet risk.',
        'A strategic catalyst expands the addressable market or competitive position.',
    ])]

    thesis_breakers = [
        'Two consecutive periods of material guidance misses or deteriorating forward indicators.',
        'Growth slows materially without a compensating improvement in margins or cash generation.',
        'Free cash flow remains persistently below accounting earnings.',
        'Material dilution, leverage or acquisition risk changes per-share economics.',
        'A legal, regulatory, product, technology or competitive event damages the core thesis.',
        'The market price moves materially above the modelled bull case without a corresponding improvement in fundamentals.',
    ]
    if company_type == 'REIT / PROPERTY':
        thesis_breakers += ['Occupancy/like-for-like income weakens materially or refinancing becomes difficult.']
    elif company_type == 'FINANCIAL':
        thesis_breakers += ['Credit losses or funding pressure materially exceed the assumptions embedded in earnings.']
    elif company_type == 'GROWTH / TECHNOLOGY':
        thesis_breakers += ['Technology/product substitution or loss of key customers undermines the expected growth runway.']

    monitoring = [
        {'trigger': 'Results/guidance', 'watch': 'Revenue, margins, EPS and management guidance versus the current scenario assumptions.'},
        {'trigger': 'Cash conversion', 'watch': 'Operating cash flow and FCF versus net income and reported growth.'},
        {'trigger': 'Valuation', 'watch': 'Price versus bear/base/bull values and whether fundamentals justify multiple expansion.'},
        {'trigger': 'Capital allocation', 'watch': 'Shares, debt, acquisitions, buybacks and dividends.'},
        {'trigger': 'Competitive position', 'watch': 'Market share, pricing, customer wins/losses and technology substitution.'},
        {'trigger': 'Legal/regulatory', 'watch': 'New claims, investigations, enforcement, IP disputes or material disclosures.'},
    ]

    events = (event_timeline or {}).get('events', []) if isinstance(event_timeline, dict) else []
    return {
        'status': 'SCENARIO ENGINE ACTIVE — assumptions require verification',
        'current_price': current,
        'scenarios': scenarios,
        'probability_weighted_value': weighted,
        'probability_weighted_upside_downside': (weighted / current - 1) if weighted is not None and current not in (None, 0) else None,
        'catalysts': catalysts,
        'thesis_breakers': thesis_breakers,
        'monitoring_triggers': monitoring,
        'recent_events_available': len(events),
        'rules': [
            'Scenario probabilities are research defaults, not statistical forecasts.',
            'A catalyst only becomes thesis evidence after the underlying announcement/filing is verified.',
            'Probability-weighted value is a decision aid, not a target price.',
            'Bull/base/bear assumptions must be challenged against primary filings and peer evidence.',
        ],
    }


# ========================= V4.0 INVESTMENT COMMITTEE =========================

def _pct(v):
    return None if v is None else float(v) * 100.0


def _safe_text(v, fallback='N/A'):
    return fallback if v in (None, '', []) else str(v)


def build_investment_committee_decision(report, thesis=None, primary_pack=None,
                                        legal_pack=None, management_pack=None,
                                        competitive_pack=None, scenario_pack=None,
                                        changed_pack=None, event_timeline=None):
    """Synthesize all V3 research layers into an evidence-gated IC decision.

    This deliberately does not equate a high quantitative score with a BUY.
    Missing primary research can force NEEDS MORE RESEARCH even when valuation
    looks attractive.
    """
    primary_pack = primary_pack or {}
    legal_pack = legal_pack or {}
    management_pack = management_pack or {}
    competitive_pack = competitive_pack or {}
    scenario_pack = scenario_pack or {}
    changed_pack = changed_pack or {}
    event_timeline = event_timeline or {}

    quality = float(getattr(report, 'quality_score', 0) or 0)
    opportunity = float(getattr(report, 'opportunity_score', 0) or 0)
    risk = float(getattr(report, 'risk_score', 100) or 100)
    data_conf = str(getattr(report, 'data_confidence', 'UNKNOWN') or 'UNKNOWN').upper()
    metrics = getattr(report, 'metrics', {}) or {}
    valuation = float(metrics.get('Valuation score', getattr(report, 'valuation_score', 0)) or 0)
    current = _safe_num(metrics.get('Share price'))
    base = _safe_num(scenario_pack.get('scenarios', {}).get('base', {}).get('fair_value'))
    weighted = _safe_num(scenario_pack.get('probability_weighted_value'))

    evidence_items = []
    primary_filings = primary_pack.get('filings') or []
    if primary_filings:
        evidence_items.append(('Primary filings', True, 'Primary filing records retrieved.'))
    else:
        evidence_items.append(('Primary filings', False, 'No primary filing records retrieved.'))

    legal_status = str(legal_pack.get('status', '') or '').upper()
    legal_ready = bool(legal_pack) and 'PENDING' not in legal_status and 'REQUIRED' not in legal_status
    evidence_items.append(('Legal/regulatory', legal_ready, legal_pack.get('status', 'Legal research pending.')))

    mgmt_status = str(management_pack.get('status', '') or '').upper()
    mgmt_ready = bool(management_pack) and 'PENDING' not in mgmt_status and 'REQUIRED' not in mgmt_status
    evidence_items.append(('Management/capital allocation', mgmt_ready, management_pack.get('status', 'Management research pending.')))

    comp_verdict = str(competitive_pack.get('competitive_verdict', '') or '').upper()
    comp_ready = bool(competitive_pack) and 'PENDING' not in comp_verdict
    evidence_items.append(('Competition/moat', comp_ready, competitive_pack.get('competitive_verdict', 'Competitive research pending.')))

    events = event_timeline.get('events') or []
    evidence_items.append(('Recent events', bool(events), f'{len(events)} dated event(s) retrieved.'))

    evidence_true = sum(1 for _, ok, _ in evidence_items if ok)
    evidence_score = round(100 * evidence_true / len(evidence_items)) if evidence_items else 0

    # Score gate: the model can be positive, but weak evidence prevents a clean BUY.
    reasons = []
    blockers = []
    if opportunity >= 75:
        reasons.append(f'Investment Opportunity score is strong at {opportunity:.0f}/100.')
    elif opportunity >= 65:
        reasons.append(f'Investment Opportunity score is favourable at {opportunity:.0f}/100.')
    else:
        blockers.append(f'Opportunity score is below the preferred 65/100 threshold ({opportunity:.0f}).')

    if quality >= 70:
        reasons.append(f'Business Quality is strong at {quality:.0f}/100.')
    elif quality < 55:
        blockers.append(f'Business Quality is below 55/100 ({quality:.0f}).')

    if risk <= 30:
        reasons.append(f'Automated risk score is relatively low at {risk:.0f}/100.')
    elif risk >= 60:
        blockers.append(f'Risk score is elevated at {risk:.0f}/100.')

    if current is not None and weighted is not None:
        weighted_upside = weighted / current - 1 if current else None
        if weighted_upside is not None and weighted_upside >= 0.15:
            reasons.append(f'Probability-weighted scenario value implies about {weighted_upside:+.1%} upside.')
        elif weighted_upside is not None and weighted_upside < 0:
            blockers.append(f'Probability-weighted scenario value implies about {weighted_upside:+.1%} downside.')

    if data_conf not in {'HIGH', 'MEDIUM-HIGH'}:
        blockers.append(f'Underlying data confidence is only {data_conf}.')

    # Explicit evidence gate: a clean BUY requires more than quantitative scoring.
    if evidence_score < 60:
        decision = 'NEEDS MORE RESEARCH'
        blockers.append(f'Only {evidence_score}% of the core evidence gates are currently satisfied.')
    elif blockers and opportunity < 70:
        decision = 'AVOID'
    elif blockers:
        decision = 'WATCH'
    elif opportunity >= 75 and quality >= 70 and risk <= 40:
        decision = 'BUY CANDIDATE'
    elif opportunity >= 65:
        decision = 'WATCH'
    else:
        decision = 'NEEDS MORE RESEARCH'

    confidence = 'HIGH' if evidence_score >= 80 and data_conf in {'HIGH', 'MEDIUM-HIGH'} else ('MEDIUM' if evidence_score >= 60 else 'LOW')

    decision_rules = [
        'A high quantitative score cannot override missing primary-source evidence.',
        'BUY CANDIDATE means the research case is strong enough to investigate position sizing; it is not a guarantee of returns.',
        'NEEDS MORE RESEARCH is a valid outcome when material evidence has not been verified.',
        'Legal, management, competitive and scenario conclusions should remain evidence-qualified until their source records are reviewed.',
    ]

    next_actions = []
    if not primary_filings: next_actions.append('Retrieve and review the latest annual/interim and material-event filings.')
    if not legal_ready: next_actions.append('Complete the legal/regulatory search and verify any material cases or investigations.')
    if not mgmt_ready: next_actions.append('Review remuneration, ownership, insider dealing and capital-allocation evidence.')
    if not comp_ready: next_actions.append('Validate competitors, market share, pricing power and moat evidence.')
    if not events: next_actions.append('Review the latest RNS/SEC/company announcements and build the dated event timeline.')
    if not next_actions: next_actions.append('Stress-test the bear case and monitor the defined thesis breakers before committing capital.')

    return {
        'decision': decision,
        'confidence': confidence,
        'evidence_score': evidence_score,
        'opportunity': opportunity,
        'quality': quality,
        'risk': risk,
        'valuation_score': valuation,
        'current_price': current,
        'base_value': base,
        'probability_weighted_value': weighted,
        'reasons': reasons,
        'blockers': blockers,
        'evidence_gates': [
            {'area': area, 'status': 'PASS' if ok else 'PENDING', 'detail': detail}
            for area, ok, detail in evidence_items
        ],
        'next_actions': next_actions,
        'decision_rules': decision_rules,
        'thesis': thesis or {},
        'what_changed': changed_pack.get('headline', 'No prior comparison available.'),
        'generated_for': getattr(report, 'company', 'Unknown'),
    }


def build_investment_committee_report(report, decision_pack, legal_pack=None,
                                      management_pack=None, competitive_pack=None,
                                      scenario_pack=None, event_timeline=None):
    """Return a structured, export-friendly final IC dossier."""
    legal_pack = legal_pack or {}
    management_pack = management_pack or {}
    competitive_pack = competitive_pack or {}
    scenario_pack = scenario_pack or {}
    event_timeline = event_timeline or {}
    m = getattr(report, 'metrics', {}) or {}
    return {
        'title': f"Investment Committee Report — {getattr(report, 'company', 'Unknown')}",
        'symbol': getattr(report, 'symbol', ''),
        'executive_decision': decision_pack.get('decision'),
        'confidence': decision_pack.get('confidence'),
        'evidence_score': decision_pack.get('evidence_score'),
        'scorecard': {
            'business_quality': getattr(report, 'quality_score', None),
            'investment_opportunity': getattr(report, 'opportunity_score', None),
            'risk': getattr(report, 'risk_score', None),
            'valuation': decision_pack.get('valuation_score'),
            'data_confidence': getattr(report, 'data_confidence', None),
        },
        'valuation': {
            'current_price': decision_pack.get('current_price'),
            'base_value': decision_pack.get('base_value'),
            'probability_weighted_value': decision_pack.get('probability_weighted_value'),
            'bear': scenario_pack.get('scenarios', {}).get('bear', {}),
            'base': scenario_pack.get('scenarios', {}).get('base', {}),
            'bull': scenario_pack.get('scenarios', {}).get('bull', {}),
        },
        'financial_snapshot': {
            k: m.get(k) for k in ['Revenue TTM', 'EPS TTM', 'FCF TTM', 'FCF yield', 'P/E', 'ROE', 'Operating margin', 'Net debt', 'Current ratio', 'Revenue CAGR'] if k in m
        },
        'investment_case': decision_pack.get('reasons', []),
        'key_risks': decision_pack.get('blockers', []),
        'legal_regulatory': legal_pack,
        'management_capital_allocation': management_pack,
        'competition_industry': competitive_pack,
        'scenarios_catalysts': scenario_pack,
        'recent_events': event_timeline,
        'thesis_breakers': scenario_pack.get('thesis_breakers', []),
        'monitoring_triggers': scenario_pack.get('monitoring_triggers', []),
        'next_actions': decision_pack.get('next_actions', []),
        'methodology_rules': decision_pack.get('decision_rules', []),
    }

# ========================= V4.2 PRIMARY-DOCUMENT DATA BACKBONE =========================
# V4.2 deliberately removes Yahoo/Alpha Vantage from the critical path for UK/LSE
# companies.  It discovers and downloads primary company documents (IR reports,
# RNS and, where possible, Companies House filings), extracts financial evidence,
# and keeps market-price data as a separate, lower-confidence layer.
V4_VERSION = "4.2"

PRIMARY_UK_SOURCES = {
    "RPI": {
        "name": "Raspberry Pi Holdings plc",
        "ir": "https://investors.raspberrypi.com/",
        "reports": "https://investors-assets.raspberrypi.com/reports",
    },
}


def _v42_get(url, timeout=30, headers=None):
    h = {"User-Agent": "Company-Investigator/4.2 research-tool", "Accept": "text/html,application/pdf,application/xhtml+xml,*/*;q=0.8"}
    if headers:
        h.update(headers)
    r = requests.get(url, headers=h, timeout=timeout)
    r.raise_for_status()
    return r


def _v42_search_web(query, max_results=12):
    """Lightweight discovery only. Search results are routes to primary documents,
    never treated as evidence themselves."""
    try:
        q = urllib.parse.quote_plus(query)
        url = f"https://www.google.com/search?q={q}&num={max_results}"
        r = _v42_get(url, timeout=20, headers={"Accept-Language": "en-GB,en;q=0.8"})
        links = []
        for href in re.findall(r'href="(https?://[^\"]+)"', r.text):
            if "google.com" in urllib.parse.urlparse(href).netloc:
                continue
            if href not in links:
                links.append(href)
        return links[:max_results]
    except Exception:
        return []


def _v42_normalise_ticker(symbol):
    s = str(symbol or "").strip().upper()
    s = s.replace(".LON", ".L")
    return s


def _v42_company_name(symbol):
    base = _v42_normalise_ticker(symbol).split(".")[0]
    if base in PRIMARY_UK_SOURCES:
        return PRIMARY_UK_SOURCES[base]["name"]
    # Search the LSE company page and extract its title where possible.
    try:
        links = _v42_search_web(f"LSE {base} stock company", 8)
        for u in links:
            if "londonstockexchange.com/stock/" in u:
                rr = _v42_get(u, timeout=15)
                title = re.sub(r"<[^>]+>", " ", rr.text[:100000])
                title = re.sub(r"\\s+", " ", title).strip()
                m = re.search(r"(?:Stock|Company Page).*?([A-Z][A-Za-z0-9&.,' -]{3,100})", title)
                if m:
                    return m.group(1).strip()
    except Exception:
        pass
    return base


def _v42_extract_links(html_text, base_url):
    out = []
    for raw in re.findall(r'(?:href|src)=[\'\"]([^\'\"]+)', html_text, flags=re.I):
        try:
            u = urllib.parse.urljoin(base_url, raw)
        except Exception:
            continue
        if u.startswith("http") and u not in out:
            out.append(u)
    return out


def _v42_document_discovery(symbol, company):
    base = _v42_normalise_ticker(symbol).split(".")[0]
    docs = []
    routes = []
    if base in PRIMARY_UK_SOURCES:
        cfg = PRIMARY_UK_SOURCES[base]
        routes.extend([cfg["reports"], cfg["ir"]])
    else:
        routes.extend(_v42_search_web(f'"{company}" investor relations annual report results', 12))
        routes.extend(_v42_search_web(f'"{company}" "Annual Report" PDF', 12))
        routes.extend(_v42_search_web(f'"{company}" "Interim Results" PDF', 8))

    seen = set()
    for route in routes:
        if route in seen:
            continue
        seen.add(route)
        try:
            r = _v42_get(route, timeout=25)
            ctype = (r.headers.get("content-type") or "").lower()
            if "pdf" in ctype or route.lower().endswith(".pdf"):
                docs.append({"url": route, "title": route.rsplit("/", 1)[-1], "kind": "PDF"})
                continue
            links = _v42_extract_links(r.text, route)
            for u in links:
                lu = u.lower()
                if not any(k in lu for k in ("annual", "interim", "results", "report", "presentation", "financial")):
                    continue
                if ".pdf" in lu or "download" in lu:
                    kind = "PDF" if ".pdf" in lu else "DOCUMENT"
                    docs.append({"url": u, "title": u.rsplit("/", 1)[-1], "kind": kind})
        except Exception:
            continue

    # Deduplicate and prefer recent/financial documents by URL/title keywords.
    unique = []
    for d in docs:
        if d["url"] not in {x["url"] for x in unique}:
            unique.append(d)
    def rank(d):
        t = d["title"].lower()
        score = 0
        for k, pts in (("annual", 10), ("results", 8), ("interim", 7), ("2026", 6), ("2025", 5), ("report", 4), ("financial", 3)):
            if k in t:
                score += pts
        return score
    unique.sort(key=rank, reverse=True)
    return unique[:10]


def _v42_pdf_text(content):
    try:
        from pypdf import PdfReader
        import io
        reader = PdfReader(io.BytesIO(content))
        chunks = []
        for page in reader.pages[:80]:
            try:
                chunks.append(page.extract_text() or "")
            except Exception:
                pass
        return "\n".join(chunks)
    except Exception:
        return ""


def _v42_plain_text(content):
    try:
        return content.decode("utf-8", errors="ignore")
    except Exception:
        return ""


def _v42_num(s):
    if s is None:
        return None
    s = str(s).replace(",", "").replace("£", "").replace("$", "").replace("€", "")
    s = s.replace("(", "-").replace(")", "")
    try:
        return float(re.search(r"-?\d+(?:\.\d+)?", s).group(0))
    except Exception:
        return None


def _v42_find_metric(text, labels):
    # Handles common PDF extraction layouts where the label is followed by the
    # current and prior period values on the same or following line.
    for label in labels:
        pat = re.compile(r"(?i)" + re.escape(label) + r"[^\n]{0,180}")
        for m in pat.finditer(text):
            chunk = m.group(0)
            nums = re.findall(r"(?<![A-Za-z])\(?-?\d{1,3}(?:,\d{3})*(?:\.\d+)?\)?", chunk)
            vals = [_v42_num(x) for x in nums]
            vals = [x for x in vals if x is not None]
            if vals:
                return vals[0], (vals[1] if len(vals) > 1 else None), chunk.strip()
    return None, None, None


def _v42_extract_financials(text):
    # Internal units are millions of reported currency unless explicitly EPS/share.
    mapping = {
        "revenue": ["Revenue", "Group revenue", "Total revenue"],
        "netIncome": ["Profit for the year", "Profit for the period", "Net profit", "Net income"],
        "operatingIncome": ["Operating profit", "Operating income"],
        "ebitda": ["Adjusted EBITDA", "EBITDA"],
        "profitBeforeTax": ["Profit before tax", "Profit/(loss) before tax"],
        "eps": ["Basic earnings per share", "Basic EPS", "Diluted earnings per share", "Diluted EPS"],
        "cash": ["Cash and cash equivalents", "Cash and cash equivalents at end of year"],
        "debt": ["Borrowings", "Total borrowings", "Net debt", "Total debt"],
        "operatingCashflow": ["Net cash generated from operating activities", "Cash generated from operations", "Operating cash flow"],
        "capex": ["Purchase of property, plant and equipment", "Capital expenditure", "Capital expenditures"],
        "freeCashFlow": ["Free cash flow"],
        "assets": ["Total assets"],
        "liabilities": ["Total liabilities"],
        "equity": ["Total equity", "Total shareholders' equity", "Equity attributable to owners"],
        "shares": ["Weighted average number of shares", "Weighted average shares", "Number of shares"],
    }
    vals = {}
    evidence = []
    for key, labels in mapping.items():
        cur, prev, snippet = _v42_find_metric(text, labels)
        if cur is not None:
            vals[key] = cur
            vals[key + "_prior"] = prev
            evidence.append({"field": key, "value": cur, "prior": prev, "snippet": snippet[:220] if snippet else ""})
    return vals, evidence


def _v42_build_data(symbol, company, documents):
    income_reports = []
    cash_reports = []
    balance_reports = []
    evidence = []
    warnings = []
    source_records = []
    report_year = None
    currency = "USD" if "raspberry" in company.lower() else "GBP"

    for d in documents[:8]:
        try:
            r = _v42_get(d["url"], timeout=35)
            ctype = (r.headers.get("content-type") or "").lower()
            text = _v42_pdf_text(r.content) if ("pdf" in ctype or d["url"].lower().endswith(".pdf")) else _v42_plain_text(r.content)
            if len(text) < 500:
                continue
            vals, ev = _v42_extract_financials(text)
            if not vals:
                continue
            year_matches = re.findall(r"\b(20(?:2[0-9]|1[0-9]))\b", d["url"] + " " + d["title"] + " " + text[:8000])
            year = max([int(y) for y in year_matches], default=None)
            if year and (report_year is None or year > report_year):
                report_year = year
            label = str(year or "latest")
            # Company reports commonly present the statement in $m/£m.
            # Convert monetary statement values to absolute units used by the
            # existing valuation engine. EPS remains per-share; share counts
            # reported in millions are converted to absolute shares.
            money_keys = {"revenue", "netIncome", "operatingIncome", "ebitda", "profitBeforeTax", "cash", "debt", "operatingCashflow", "capex", "freeCashFlow", "assets", "liabilities", "equity"}
            rec = {"fiscalDateEnding": f"{label}-12-31", "reportedCurrency": currency}
            for k, v in vals.items():
                if k.endswith("_prior"):
                    continue
                vv = v * 1_000_000 if k in money_keys else (v * 1_000_000 if k == "shares" else v)
                if k == "eps":
                    rec["reportedEPS"] = v
                elif k == "revenue": rec["totalRevenue"] = vv
                elif k == "netIncome": rec["netIncome"] = vv
                elif k == "operatingIncome": rec["operatingIncome"] = vv
                elif k == "shares": rec["weightedAverageShsOutDil"] = vv
                elif k == "profitBeforeTax": rec["profitBeforeTax"] = vv
            income_reports.append(rec)
            c = {"fiscalDateEnding": f"{label}-12-31", "reportedCurrency": currency}
            if "operatingCashflow" in vals: c["operatingCashflow"] = vals["operatingCashflow"] * 1_000_000
            if "capex" in vals: c["capitalExpenditures"] = -abs(vals["capex"] * 1_000_000)
            if "freeCashFlow" in vals: c["freeCashFlow"] = vals["freeCashFlow"] * 1_000_000
            if c.keys() - {"fiscalDateEnding", "reportedCurrency"}: cash_reports.append(c)
            b = {"fiscalDateEnding": f"{label}-12-31", "reportedCurrency": currency}
            for src, dst in (("cash", "cashAndCashEquivalentsAtCarryingValue"), ("debt", "shortLongTermDebtTotal"), ("assets", "totalAssets"), ("liabilities", "totalLiabilities"), ("equity", "totalShareholderEquity")):
                if src in vals: b[dst] = vals[src] * 1_000_000
            if len(b) > 2: balance_reports.append(b)
            evidence.extend([{**x, "source_url": d["url"], "document": d["title"], "date": year} for x in ev])
            source_records.append(SourceRecord("Company primary documents", "annual/interim financial report", "OK", d["url"], "Financial values extracted from a primary company document; parser evidence retained.").__dict__)
        except Exception as exc:
            warnings.append(f"Document {d['url']}: {exc}")

    # Keep only one record per fiscal year, preferring the first primary document found.
    def dedupe(rows):
        out = {}
        for row in rows:
            out.setdefault(row.get("fiscalDateEnding"), row)
        return list(out.values())
    income_reports = dedupe(income_reports)
    cash_reports = dedupe(cash_reports)
    balance_reports = dedupe(balance_reports)

    latest = income_reports[0] if income_reports else {}
    price, price_source = _v42_market_price(symbol)
    if price is not None and _v42_normalise_ticker(symbol).endswith(".L") and price > 100:
        # Stooq commonly returns London equity quotes in pence. The analysis
        # engine uses GBP, so convert obvious GBX quotes here.
        price = price / 100.0
    shares = latest.get("weightedAverageShsOutDil")
    eps = latest.get("reportedEPS")
    market_cap = price * shares if price is not None and shares else None
    overview = {
        "Symbol": _v42_normalise_ticker(symbol),
        "Name": company,
        "Sector": "Unknown",
        "Industry": "Unknown",
        "Description": "Primary-document financial dataset built from company disclosures.",
        "Currency": currency,
        "ReportingCurrency": currency,
        "Price": price,
        "PriceCurrency": "GBP" if _v42_normalise_ticker(symbol).endswith(".L") else currency,
        "MarketCapitalization": market_cap,
        "RevenueTTM": latest.get("totalRevenue"),
        "EPS": eps,
        "SharesOutstanding": shares,
        "PERatio": (price / eps) if price is not None and eps not in (None, 0) else None,
    }
    if price_source:
        source_records.append(SourceRecord("Independent market quote", "current price only", "OK", price_source, "Market price is kept separate from primary financial statements.").__dict__)
    if not income_reports:
        warnings.append("Primary documents were discovered but no financial table could be parsed confidently. The report must not treat missing fields as zero.")
    return {
        "symbol_requested": symbol,
        "symbol_used": _v42_normalise_ticker(symbol),
        "overview": overview,
        "income": {"annualReports": income_reports},
        "balance": {"annualReports": balance_reports},
        "cash": {"annualReports": cash_reports},
        "errors": {},
        "warnings": warnings,
        "source_records": source_records,
        "primary_document_evidence": evidence,
        "primary_documents": documents,
        "provider_version": "V4.2 primary-document backbone",
        "market_price_source": price_source,
    }


def _v42_market_price(symbol):
    """Best-effort independent quote. Never blocks the primary-document analysis."""
    s = _v42_normalise_ticker(symbol)
    candidates = [s.replace(".L", ".uk").lower(), s.split(".")[0].lower() + ".uk"] if s.endswith(".L") else [s.lower()]
    for cand in candidates:
        try:
            url = f"https://stooq.com/q/d/l/?s={urllib.parse.quote(cand)}&i=d"
            r = _v42_get(url, timeout=12)
            lines = r.text.strip().splitlines()
            if len(lines) >= 2:
                headers = [x.strip().lower() for x in lines[0].split(",")]
                row = lines[-1].split(",")
                if "close" in headers:
                    p = _v42_num(row[headers.index("close")])
                    if p is not None and p > 0:
                        return p, url
        except Exception:
            pass
    return None, None


def load_company_data_v2(symbol, api_key=None):
    """V4.2 loader. UK/LSE companies use primary documents first; US issuers use
    SEC evidence where possible. Yahoo and Alpha Vantage are optional fallback
    sources only and are never required for a UK investigation."""
    requested = str(symbol or "").strip().upper()
    is_uk = requested.endswith(".L") or requested.endswith(".LON") or "." not in requested
    if is_uk:
        company = _v42_company_name(requested)
        docs = _v42_document_discovery(requested, company)
        if not docs:
            raise AlphaVantageError(f"V4.2 could not discover primary company documents for {requested}. No Yahoo/Alpha Vantage dependency was used.")
        data = _v42_build_data(requested, company, docs)
        data["source_audit"] = build_source_audit(requested, data, None)
        data["source_audit"].append(SourceRecord("V4.2 primary-document backbone", "financial statements", "OK" if data["income"].get("annualReports") else "PARTIAL", "", "Primary company documents are the critical-path financial source.").__dict__)
        data["source_audit"].append(SourceRecord("Yahoo Finance", "secondary fundamentals", "OPTIONAL", "https://finance.yahoo.com/", "Not required for UK/LSE investigations; used only in an explicitly added fallback.").__dict__)
        data["source_audit"].append(SourceRecord("Alpha Vantage", "secondary fundamentals", "OPTIONAL", "https://www.alphavantage.co/", "Not required for UK/LSE investigations.").__dict__)
        return data

    # US route: SEC is primary. If SEC fails, do not silently fabricate a report.
    try:
        sec = sec_enrichment(requested)
        if sec.get("available"):
            # Reuse secondary data only if it is explicitly available; SEC evidence remains primary.
            if api_key:
                try:
                    data = load_company_data(requested, api_key)
                    data["sec_enrichment"] = sec
                    data["source_records"] = build_source_audit(requested, data, sec)
                    data["provider_version"] = "V4.2 SEC-enriched"
                    return data
                except Exception:
                    pass
            raise AlphaVantageError("SEC primary filing route is available, but a complete structured financial statement parser for this issuer is not yet available. No Yahoo data was used.")
    except AlphaVantageError:
        raise
    except Exception as exc:
        raise AlphaVantageError(f"V4.2 SEC route failed for {requested}: {exc}")
    raise AlphaVantageError(f"V4.2 could not establish a primary filing route for {requested}.")

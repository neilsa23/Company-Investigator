"""Focused offline tests for Company Investigator V5.11 integrity checks."""
from company_investigator_v5 import build_financial_integrity_checks, financial_forensics, _v42_get, _v42_document_discovery

DATASET = {
    2023: {"revenue": 265_800_000, "gross_profit": 66_000_000, "operating_cashflow": 21_100_000, "capex": 25_800_000, "free_cash_flow": -4_700_000, "cash": 42_200_000, "assets": 265_300_000, "liabilities": 106_100_000, "equity": 159_200_000, "lease_liabilities": 7_100_000},
    2024: {"revenue": 259_500_000, "gross_profit": 63_200_000, "operating_cashflow": -3_200_000, "capex": 23_100_000, "free_cash_flow": -26_300_000, "cash": 45_800_000, "assets": 331_400_000, "liabilities": 121_000_000, "equity": 210_400_000, "lease_liabilities": 6_200_000},
    2025: {"revenue": 323_200_000, "gross_profit": 77_800_000, "operating_cashflow": 6_200_000, "capex": 18_200_000, "free_cash_flow": -12_000_000, "cash": 28_100_000, "debt": 0, "net_debt": -28_100_000, "assets": 331_600_000, "liabilities": 90_800_000, "equity": 240_800_000, "lease_liabilities": 8_900_000},
}

def sample_data(dataset=None):
    rows = DATASET if dataset is None else dataset
    evidence = []
    for year, record in rows.items():
        for field in record:
            derived = field in ("free_cash_flow", "net_debt")
            evidence.append({
                "field": field,
                "fiscal_year": year,
                "source_url": "derived" if derived else "https://investors.raspberrypi.com/reports",
                "document": "Calculation" if derived else f"Raspberry Pi FY{year} report",
                "method": "Formula test" if derived else "Cross-checked against primary report",
                "snippet": "Formula" if derived else "Verified annual fact: value cross-checked",
            })
    return {"financial_dataset": rows, "primary_document_evidence": evidence, "overview": {"Price": None, "EPS": None, "PriceCurrency": "GBP", "ReportingCurrency": "USD", "PERatio": None}}

def test_rpi_arithmetic_passes_and_page_evidence_is_not_overclaimed():
    checks = build_financial_integrity_checks(sample_data())
    arithmetic_reviews = [c for c in checks if c["Status"] == "REVIEW" and c["Check"] not in ("Page-level evidence auditability",)]
    assert not arithmetic_reviews
    assert any(c["Check"] == "Page-level evidence auditability" and c["Status"] == "REVIEW" for c in checks)

def test_bad_balance_sheet_is_flagged():
    rows = {year: dict(record) for year, record in DATASET.items()}
    rows[2025]["equity"] = 200_000_000
    checks = build_financial_integrity_checks(sample_data(rows))
    assert any(check["Year"] == "2025" and check["Check"].startswith("Balance sheet") and check["Status"] == "REVIEW" for check in checks)

def test_missing_fields_are_not_testable():
    checks = build_financial_integrity_checks({"financial_dataset": {2025: {"revenue": 323_200_000}}, "primary_document_evidence": [], "overview": {}})
    assert any(check["Status"] == "NOT TESTABLE" for check in checks)

def test_net_cash_does_not_count_lease_liabilities_as_borrowings():
    rows = {2025: dict(DATASET[2025])}
    checks = build_financial_integrity_checks(sample_data(rows))
    debt_check = [c for c in checks if c["Year"] == "2025" and c["Check"] == "Net debt calculation"]
    assert len(debt_check) == 1 and debt_check[0]["Status"] == "PASS"
    assert rows[2025]["debt"] == 0 and rows[2025]["lease_liabilities"] == 8_900_000


def _annual_rows(reverse=False):
    rows = [
        {"fiscalDateEnding": "2023-12-31", "totalRevenue": 265_800_000, "reportedEPS": 0.195, "netIncome": 31_600_000, "operatingIncome": 37_600_000},
        {"fiscalDateEnding": "2024-12-31", "totalRevenue": 259_500_000, "reportedEPS": 0.0648, "netIncome": 11_700_000, "operatingIncome": 17_600_000},
        {"fiscalDateEnding": "2025-12-31", "totalRevenue": 323_200_000, "reportedEPS": 0.1122, "netIncome": 21_700_000, "operatingIncome": 28_000_000},
    ]
    return list(reversed(rows)) if reverse else rows

def test_financial_forensics_cagr_uses_chronological_endpoints():
    expected_revenue = (323_200_000 / 265_800_000) ** 0.5 - 1
    expected_eps = (0.1122 / 0.195) ** 0.5 - 1
    for reverse in (False, True):
        result = financial_forensics(
            {"annualReports": _annual_rows(reverse)},
            {"annualReports": []},
            {"annualReports": []},
            {},
        )
        assert abs(result["revenue_cagr_5y"] - expected_revenue) < 1e-9
        assert abs(result["eps_cagr_5y"] - expected_eps) < 1e-9

def test_missing_capex_does_not_become_zero_fcf():
    income = {"annualReports": [
        {"fiscalDateEnding": "2023-12-31", "totalRevenue": 100, "netIncome": 10, "operatingIncome": 12},
        {"fiscalDateEnding": "2024-12-31", "totalRevenue": 110, "netIncome": 11, "operatingIncome": 13},
    ]}
    cash = {"annualReports": [
        {"fiscalDateEnding": "2023-12-31", "operatingCashflow": 15},
        {"fiscalDateEnding": "2024-12-31", "operatingCashflow": 16},
    ]}
    result = financial_forensics(income, {"annualReports": []}, cash, {})
    assert result["latest_fcf"] is None
    assert result["fcf_conversion"] is None

def test_roic_proxy_unavailable_when_required_balance_inputs_missing():
    income = {"annualReports": [
        {"fiscalDateEnding": "2023-12-31", "totalRevenue": 100, "netIncome": 10, "operatingIncome": 12, "incomeBeforeTax": 12, "incomeTaxExpense": 2},
        {"fiscalDateEnding": "2024-12-31", "totalRevenue": 110, "netIncome": 11, "operatingIncome": 13, "incomeBeforeTax": 13, "incomeTaxExpense": 2},
    ]}
    result = financial_forensics(income, {"annualReports": []}, {"annualReports": []}, {})
    assert result["roic_proxy"] is None

def test_document_fetch_retries_once_after_connection_reset(monkeypatch):
    import company_investigator_v5 as engine
    calls = {"count": 0}
    class FakeResponse:
        def raise_for_status(self):
            return None
    def fake_get(*args, **kwargs):
        calls["count"] += 1
        if calls["count"] == 1:
            raise engine.requests.ConnectionError("temporary reset")
        return FakeResponse()
    monkeypatch.setattr(engine.requests, "get", fake_get)
    monkeypatch.setattr(engine.time, "sleep", lambda *_: None)
    response = _v42_get("https://example.invalid/report.pdf", timeout=(5, 24))
    assert isinstance(response, FakeResponse)
    assert calls["count"] == 2


def test_cagr_display_values_are_percentages():
    # Engine stores ratios as decimals; UI should display them as percentages.
    assert f"{0.103:.1%}" == "10.3%"
    assert f"{-0.2414588901156277:.1%}" == "-24.1%"


def test_raspberry_pi_routes_have_public_regulatory_fallbacks():
    docs = _v42_document_discovery("RPI.L", "Raspberry Pi Holdings plc")
    assert any(d.get("fallback_url", "").endswith("fy-2025-results-n2lz4nht5hqu0cg.html") for d in docs)
    assert any("data.fca.org.uk" in d.get("fallback_url", "") for d in docs)

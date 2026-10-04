
import os
import streamlit as st
import pandas as pd
from company_investigator_v1 import (
    AlphaVantageError,
    load_company_data,
    analyse,
    investment_thesis,
    compare_reports,
    peer_benchmark,
    peer_groups_for,
)

st.set_page_config(
    page_title="Company Investigator",
    page_icon="📊",
    layout="wide",
)

st.title("📊 Company Investigator")
st.caption("A structured pre-investment research and valuation tool • Engine v1.12")

api_key = os.getenv("ALPHAVANTAGE_API_KEY")
if not api_key:
    st.warning("Set ALPHAVANTAGE_API_KEY before using the analyser.")
    st.code("ALPHAVANTAGE_API_KEY=YOUR_KEY")
    st.stop()


def traffic(score):
    if score >= 0.80:
        return "🟢"
    if score >= 0.60:
        return "🟡"
    if score >= 0.40:
        return "🟠"
    return "🔴"


def score_colour(score):
    if score >= 75:
        return "🟢"
    if score >= 65:
        return "🟡"
    if score >= 50:
        return "🟠"
    return "🔴"


def money(value):
    return "N/A" if value is None else f"{value:,.2f}"


tab1, tab2 = st.tabs(["🔎 Analyse", "⚖️ Compare"])

with tab1:
    ticker = st.text_input(
        "Company ticker",
        placeholder="RPI.L, RR.L, RPI.LON, IBM...",
        label_visibility="visible",
    )

    if st.button("🔍 Analyse company", type="primary", disabled=not ticker.strip()):
        with st.spinner(f"Researching {ticker.upper()}..."):
            try:
                data = load_company_data(ticker.upper().strip(), api_key)
                report = analyse(data, api_key)
                thesis = investment_thesis(report)
                st.session_state["report"] = report
                st.session_state["thesis"] = thesis
                st.session_state.pop("analysis_error", None)
            except AlphaVantageError as exc:
                st.session_state["analysis_error"] = str(exc)
                st.session_state.pop("report", None)
                st.session_state.pop("thesis", None)

    if st.session_state.get("analysis_error"):
        st.error("Company data could not be retrieved")
        st.warning(st.session_state["analysis_error"])
        st.info(
            "Try the LSE format RPI.L (the app will automatically send RPI.LON to Alpha Vantage), "
            "or check whether your Alpha Vantage key has reached its daily request limit."
        )

    report = st.session_state.get("report")
    thesis = st.session_state.get("thesis")

    if report:
        status = report.data_status or {}
        if status.get("warnings"):
            for warning in status["warnings"]:
                st.info(warning)
        endpoint_errors = status.get("errors") or {}
        if endpoint_errors:
            with st.expander("⚠️ Data-source diagnostics", expanded=False):
                st.caption(
                    "Some Alpha Vantage endpoints did not return data. The analysis continues using the data that was available."
                )
                for endpoint, message in endpoint_errors.items():
                    st.write(f"**{endpoint}** — {message}")
        # HERO
        st.divider()
        st.subheader(f"{report.company}  •  {report.symbol}")
        st.caption(
            f"{report.sector}  |  {report.company_type}  |  "
            f"Financials: {report.metrics.get('Financial currency') or 'unknown'}  |  "
            f"Price: {report.metrics.get('Price currency') or 'unknown'}"
        )

        h1, h2, h3, h4, h5 = st.columns(5)
        h1.metric("Business quality", f"{report.quality_score:.0f}/100")
        h2.metric("Opportunity", f"{report.opportunity_score:.0f}/100", report.verdict)
        h3.metric("Risk", f"{report.risk_score:.0f}/100", report.risk_band)
        h4.metric("Data confidence", report.data_confidence)
        h5.metric("Base value", money(report.valuation.get("base_fair_value")))

        st.caption(
            "Quality = how good the business is. Opportunity = quality + valuation. "
            "Risk = 0–100, where higher means more risk."
        )

        st.markdown("## 🧭 The three scores")
        score_df = pd.DataFrame([
            {
                "Measure": "Business quality",
                "Score": report.quality_score,
                "Meaning": "Underlying business strength, excluding valuation",
            },
            {
                "Measure": "Investment opportunity",
                "Score": report.opportunity_score,
                "Meaning": "Business quality combined with valuation",
            },
            {
                "Measure": "Risk",
                "Score": report.risk_score,
                "Meaning": "0 = lower risk; 100 = higher risk",
            },
        ])
        st.dataframe(score_df, use_container_width=True, hide_index=True)

        # Conviction & sizing
        st.markdown("## 🎯 Conviction & position sizing")
        pg = report.position_guidance
        pc1, pc2, pc3 = st.columns(3)
        pc1.metric("Conviction", f"{pg.get('conviction', 'N/A')}/10")
        pc2.metric("Suggested range", pg.get("suggested_range", "N/A"))
        pc3.metric("Maximum", pg.get("maximum", "N/A"))
        st.caption(
            "Research sizing framework only — not a personalised recommendation. "
            "Actual sizing should reflect your portfolio, diversification, risk tolerance "
            "and time horizon."
        )
        for driver in pg.get("drivers", []):
            st.write("• " + driver)

        # Investment case
        st.markdown("## 🧠 Investment case")
        a, b = st.columns(2)

        with a:
            st.markdown("### Why it could work")
            for item in thesis["buy_reasons"][:4]:
                st.success(item)

        with b:
            st.markdown("### What could go wrong")
            for item in thesis["risks"][:4]:
                st.error(item)

        # Valuation
        st.markdown("## 💰 Valuation")
        v = report.valuation
        valuation_cols = st.columns(5)

        vals = [
            ("Strong buy", v.get("buy_zone")),
            ("Attractive", v.get("attractive_price")),
            ("Base value", v.get("base_fair_value")),
            ("Expensive", v.get("expensive_price")),
            ("Current", report.metrics.get("Share price")),
        ]

        for col, (label, value) in zip(valuation_cols, vals):
            col.metric(label, money(value))

        if v.get("upside_to_base") is not None:
            upside = v["upside_to_base"]
            if upside >= 0:
                st.success(f"Base-case upside: **{upside:.1%}**")
            else:
                st.error(f"Base-case downside: **{upside:.1%}**")

        scenario_data = pd.DataFrame([
            {
                "Scenario": s.title(),
                "Fair value": v["scenarios"][s].get("fair_value"),
            }
            for s in ("bear", "base", "bull")
        ]).set_index("Scenario")

        st.bar_chart(scenario_data)

        # Scorecard
        st.markdown("## 📊 Scorecard")
        for name, value in report.scores.items():
            max_score = {
                "Business Quality": 20,
                "Growth": 25,
                "Profitability": 20,
                "Financial Health": 20,
                "Cash Generation": 15,
                "Shareholder Alignment": 10,
                "Valuation": 15,
            }.get(name, 20)

            ratio = min(max(value / max_score, 0), 1)
            st.write(f"{traffic(ratio)} **{name}** — {value:.1f}")
            st.progress(ratio)

        # Historical trends
        st.markdown("## 📈 Financial trends")
        trends = report.historical_trends
        trend_frame = pd.DataFrame({
            "Year": trends.get("years", []),
            "Revenue": trends.get("revenue", []),
            "Net income": trends.get("net_income", []),
            "Operating cash flow": trends.get("operating_cash_flow", []),
            "Free cash flow": trends.get("free_cash_flow", []),
            "Debt": trends.get("debt", []),
            "Cash": trends.get("cash", []),
            "Shares": trends.get("shares", []),
        })

        if not trend_frame.empty:
            trend_frame = trend_frame.set_index("Year")

            c1, c2 = st.columns(2)
            with c1:
                st.markdown("**Revenue & net income**")
                st.line_chart(trend_frame[["Revenue", "Net income"]])
            with c2:
                st.markdown("**Operating cash flow & free cash flow**")
                st.line_chart(trend_frame[["Operating cash flow", "Free cash flow"]])

            c3, c4 = st.columns(2)
            with c3:
                st.markdown("**Debt & cash**")
                st.line_chart(trend_frame[["Debt", "Cash"]])
            with c4:
                st.markdown("**Dilution / share count**")
                st.line_chart(trend_frame[["Shares"]])
        else:
            st.info("Not enough historical data to display trend charts.")

        # Key metrics
        st.markdown("## 📋 Key financial metrics")
        metrics = {
            k: v for k, v in report.metrics.items()
            if v is not None and k not in ("Company type",)
        }

        metric_rows = []
        for k, value in metrics.items():
            if isinstance(value, (int, float)):
                metric_rows.append({"Metric": k, "Value": value})
            else:
                metric_rows.append({"Metric": k, "Value": value})

        st.dataframe(
            pd.DataFrame(metric_rows),
            use_container_width=True,
            hide_index=True,
        )

        # Risk and monitoring
        st.markdown("## 🚦 Risk & monitoring")

        r1, r2 = st.columns(2)
        with r1:
            st.markdown("### Red flags")
            if report.flags:
                for item in report.flags:
                    st.warning(item)
            else:
                st.success("No major automated red flags identified.")

        with r2:
            st.markdown("### What to monitor")
            for item in thesis["monitor"]:
                st.write("• " + item)

        # Positive signals
        with st.expander("🟢 Positive signals"):
            for item in report.positives:
                st.write("✓ " + item)

        with st.expander("🔬 Valuation methodology"):
            st.write(
                "The valuation combines available DCF, earnings-multiple and "
                "free-cash-flow approaches, with additional methods selected "
                "according to the company's business type. The result is a "
                "research range, not a precise intrinsic-value claim."
            )

        st.markdown("## 👥 Suggested peers")
        suggested = peer_groups_for(report.company_type)
        suggested = [x for x in suggested if x.upper() != report.symbol.upper()]

        if suggested:
            st.caption(
                "Starter peers based on business type. Treat these as research "
                "suggestions, not perfect like-for-like matches."
            )
            st.write(", ".join(suggested))
        else:
            st.caption("No starter peer group is configured for this business type yet.")

        st.info(
            "This is a research framework, not financial advice. "
            "Always verify the latest company filings, price and assumptions."
        )

with tab2:
    st.subheader("⚖️ Compare companies")
    symbols = st.text_input(
        "Enter 2–6 tickers separated by commas",
        placeholder="RPI, ONT, ALFA, GAW",
    )

    if st.button("⚖️ Compare", type="primary", disabled=not symbols.strip()):
        tickers = [x.strip().upper() for x in symbols.split(",") if x.strip()]

        if len(tickers) < 2:
            st.error("Enter at least two companies.")
        elif len(tickers) > 6:
            st.error("Maximum six companies.")
        else:
            reports = []
            failed = []
            with st.spinner("Analysing companies..."):
                for ticker_value in tickers:
                    try:
                        data = load_company_data(ticker_value, api_key)
                        reports.append(analyse(data, api_key))
                    except AlphaVantageError as exc:
                        failed.append((ticker_value, str(exc)))

            if failed:
                for ticker_value, message in failed:
                    st.warning(f"{ticker_value}: {message}")

            if len(reports) < 2:
                st.error("Fewer than two companies returned usable data, so a comparison cannot be produced.")
                st.stop()

            rows = compare_reports(reports)

            st.markdown("### 🏆 Ranking")
            for i, row in enumerate(rows, 1):
                emoji = score_colour(row["Opportunity"])
                st.write(
                    f"### {i}. {emoji} {row['Company']} ({row['Ticker']}) — "
                    f"{row['Opportunity']:.1f}/100"
                )
                st.caption(
                    f"{row['Type']} • {row['Risk']} risk • "
                    f"Valuation score {row['Valuation']:.1f}"
                )

            display_rows = []
            for row in rows:
                display_rows.append({
                    "Company": row["Company"],
                    "Ticker": row["Ticker"],
                    "Quality": round(row["Quality"], 1),
                    "Opportunity": round(row["Opportunity"], 1),
                    "Risk": round(row["Risk score"], 1),
                    "Risk band": row["Risk"],
                    "Growth": round(row["Growth"], 1),
                    "Profitability": round(row["Profitability"], 1),
                    "Financial health": round(row["Financial Health"], 1),
                    "Cash generation": round(row["Cash Generation"], 1),
                    "Valuation": round(row["Valuation"], 1),
                })

            st.dataframe(
                pd.DataFrame(display_rows),
                use_container_width=True,
                hide_index=True,
            )

            st.markdown("### 📐 Relative benchmark")
            benchmark_rows = peer_benchmark(reports)
            if benchmark_rows:
                benchmark_df = pd.DataFrame(benchmark_rows)
                st.dataframe(
                    benchmark_df,
                    use_container_width=True,
                    hide_index=True,
                )

                top = benchmark_rows[0]
                st.info(
                    f"**Top model score:** {top['Company']} ({top['Ticker']}) "
                    f"at **{top['Overall']}/100**. "
                    "Check business type and data confidence before treating "
                    "the ranking as a like-for-like comparison."
                )

st.divider()
st.caption("Company Investigator • Version 1.11")


import os
import streamlit as st
import pandas as pd
from company_investigator_v4_1 import (
    AlphaVantageError,
    load_company_data_v2,
    analyse,
    investment_thesis,
    compare_reports,
    peer_benchmark,
    peer_groups_for,
    quick_view,
    full_investigation_framework,
    patent_research_links,
    build_primary_source_research,
    red_team_framework,
    full_investigation_framework_v31,
    legal_investigation_framework,
    build_event_timeline,
    build_investigation_snapshot,
    what_changed_since_last_investigation,
    build_management_research,
    capital_allocation_red_flags,
    build_scenario_catalyst_engine,
    build_investment_committee_decision,
    build_investment_committee_report,
    build_live_research_pack,
    live_research_summary,
    v41_live_research_pack,
)

st.set_page_config(
    page_title="Company Investigator",
    page_icon="📊",
    layout="wide",
)

st.title("📊 Company Investigator")
st.caption("A structured pre-investment research and valuation tool • Engine v4.1 • Evidence-first live research + Investment Committee dossier")

def _get_alpha_vantage_key():
    # Streamlit Cloud secrets are exposed through st.secrets, not necessarily
    # as OS environment variables. Support both deployment styles.
    key = os.getenv("ALPHAVANTAGE_API_KEY")
    if key:
        return key.strip()
    try:
        secret = st.secrets.get("ALPHAVANTAGE_API_KEY")
        if secret:
            return str(secret).strip()
    except Exception:
        pass
    return None


api_key = _get_alpha_vantage_key()
if not api_key:
    st.info("No Alpha Vantage key is configured. V4.1 will use the primary finance data layer first; Alpha Vantage is available as a fallback if a Streamlit secret named ALPHAVANTAGE_API_KEY is configured.")


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
                data = load_company_data_v2(ticker.upper().strip(), api_key)
                report = analyse(data, api_key)
                report.data_status = report.data_status or {}
                report.data_status["source_records"] = data.get("source_records", [])
                report.data_status["sec_enrichment"] = data.get("sec_enrichment", {})
                thesis = investment_thesis(report)
                st.session_state["report"] = report
                st.session_state["thesis"] = thesis
                primary_pack = build_primary_source_research(report, data)
                primary_pack["evidence_completeness"] = 40 if primary_pack.get("filings") else (15 if primary_pack.get("official_routes") else 0)
                st.session_state["primary_pack"] = primary_pack
                st.session_state["quick_view"] = quick_view(report, thesis)
                previous_snapshot = st.session_state.get("investigation_snapshot")
                event_timeline = build_event_timeline(report, data, previous_snapshot)
                st.session_state["previous_snapshot"] = previous_snapshot
                st.session_state["investigation_snapshot"] = event_timeline["snapshot"]
                st.session_state["event_timeline"] = event_timeline
                st.session_state["what_changed"] = what_changed_since_last_investigation(previous_snapshot, report, event_timeline)
                st.session_state["legal_framework"] = legal_investigation_framework(report, primary_pack)
                st.session_state["management_pack"] = build_management_research(report, data)
                st.session_state["capital_flags"] = capital_allocation_red_flags(report, st.session_state["management_pack"])
                st.session_state["competitive_pack"] = build_competitive_industry_intelligence(report, data)
                st.session_state["scenario_pack"] = build_scenario_catalyst_engine(report, thesis, event_timeline)
                st.session_state["ic_decision"] = build_investment_committee_decision(
                    report, thesis, primary_pack, st.session_state["legal_framework"],
                    st.session_state["management_pack"], st.session_state["competitive_pack"],
                    st.session_state["scenario_pack"], st.session_state["what_changed"], event_timeline
                )
                st.session_state["ic_report"] = build_investment_committee_report(
                    report, st.session_state["ic_decision"], st.session_state["legal_framework"],
                    st.session_state["management_pack"], st.session_state["competitive_pack"],
                    st.session_state["scenario_pack"], event_timeline
                )
                st.session_state["full_framework"] = full_investigation_framework_v31(report, thesis, primary_pack)
                st.session_state["patent_links"] = patent_research_links(report.company, report.symbol)
                # V4: retrieve live primary/news/corporate evidence after the financial analysis.
                live_pack = v41_live_research_pack(report, data, previous_snapshot)
                st.session_state["live_research_pack"] = live_pack
                st.session_state["live_research_summary"] = live_research_summary(live_pack)
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
        view_mode = st.radio("Report depth", ["⚡ Quick View", "🔬 Full Investigation"], horizontal=True, key="report_mode")
        if view_mode == "⚡ Quick View":
            qv = st.session_state.get("quick_view", quick_view(report, thesis))
            st.divider()
            st.subheader(f"{qv["company"]} • {qv["symbol"]}")
            q1,q2,q3,q4 = st.columns(4)
            q1.metric("Opportunity", f"{qv["opportunity"]:.0f}/100")
            q2.metric("Quality", f"{qv["quality"]:.0f}/100")
            q3.metric("Risk", f"{qv["risk"]:.0f}/100", qv["risk_band"])
            q4.metric("Data confidence", qv["data_confidence"])
            st.markdown("### 💰 Valuation snapshot")
            vc=st.columns(4)
            vc[0].metric("Current", money(qv["price"]))
            vc[1].metric("Bear", money(qv["bear_value"]))
            vc[2].metric("Base", money(qv["base_value"]))
            vc[3].metric("Bull", money(qv["bull_value"]))
            st.markdown("### 🟢 Why it could work")
            for x in qv["buy_reasons"] or ["No strong automated positive identified."]: st.success(x)
            st.markdown("### 🔴 What could go wrong")
            for x in qv["risks"] or ["No major automated risk identified."]: st.error(x)
            st.markdown("### 🔎 Key checks before investing")
            for x in [qv["legal_status"], qv["patent_status"], qv["recent_change_status"]]: st.warning(x)
            st.markdown("### 🎯 Current verdict")
            st.info(f"**{qv["verdict"]}** — use Full Investigation for primary-source, legal/IP, management, competitive and dated-event research.")
            st.caption("Quick View is designed to take roughly 1–2 minutes to read. Missing research is explicitly labelled rather than assumed negative.")
            st.stop()

        status = report.data_status or {}
        if status.get("warnings"):
            for warning in status["warnings"]:
                st.info(warning)
        endpoint_errors = status.get("errors") or {}
        source_records = getattr(report, "data_status", {}).get("source_records") or []
        if source_records:
            with st.expander("📚 Sources & evidence audit", expanded=False):
                st.caption("V4 records live retrieval status, source role, date and direct evidence routes; secondary sources are clearly labelled.")
                for source in source_records:
                    st.write(f"**{source.get('name','Source')}** — {source.get('status','UNKNOWN')} · {source.get('role','')}" )
                    if source.get('notes'):
                        st.caption(source.get('notes'))
                    if source.get('url'):
                        st.markdown(source.get('url'))
        if endpoint_errors:
            with st.expander("⚠️ Data-source diagnostics", expanded=False):
                st.caption(
                    "Some Alpha Vantage endpoints did not return data. The analysis continues using the data that was available."
                )
                for endpoint, message in endpoint_errors.items():
                    st.write(f"**{endpoint}** — {message}")
        # V4 LIVE RESEARCH
        live_pack = st.session_state.get("live_research_pack")
        if live_pack:
            ls = st.session_state.get("live_research_summary", live_research_summary(live_pack))
            st.markdown("## 🌐 V4 Live Research")
            lc1, lc2, lc3, lc4 = st.columns(4)
            lc1.metric("SEC", str(ls.get("sec_status")))
            lc2.metric("SEC filings", str(ls.get("sec_filings", 0)))
            lc3.metric("News events", str(ls.get("news_events", 0)))
            lc4.metric("Companies House", str(ls.get("companies_house_status")))
            st.caption(f"Retrieved: {ls.get('retrieved_at_utc', 'unknown')} • Live retrieval is evidence collection, not proof that no issue exists.")
            if ls.get("warnings"):
                for w in ls["warnings"]:
                    st.warning(w)
            with st.expander("📄 Live primary-source evidence", expanded=False):
                sec = live_pack.get("sec", {})
                if sec.get("filings"):
                    st.write(f"**SEC issuer:** {sec.get('company')} • CIK {sec.get('cik')}")
                    st.dataframe(pd.DataFrame(sec["filings"])[[c for c in ["filingDate","form","reportDate","primaryDocument","url"] if c in pd.DataFrame(sec["filings"]).columns]], use_container_width=True, hide_index=True)
                else:
                    st.info("No live SEC issuer/filings were retrieved for this symbol. This is not a clean legal conclusion.")
                ch = live_pack.get("uk_companies_house", {})
                if ch.get("status") == "LIVE":
                    st.write(f"**Companies House match:** {ch.get('company_number')}")
                    if ch.get("company_url"):
                        st.markdown(ch["company_url"])
                    if ch.get("officer_excerpt"):
                        st.caption(ch["officer_excerpt"][:1800])
                news = live_pack.get("news", {})
                if news.get("events"):
                    st.write("**Recent news discovery — secondary evidence:**")
                    for item in news["events"][:12]:
                        title = item.get("title") or "Untitled"
                        url = item.get("url")
                        if url:
                            st.markdown(f"- [{title}]({url}) — {item.get('domain','unknown source')}")
                        else:
                            st.write(f"- {title}")
            with st.expander("🇬🇧 UK primary-source routes", expanded=False):
                for route in live_pack.get("uk_routes", []):
                    st.markdown(f"- **{route['name']}** — {route['role']} — {route['url']}")

        # V4.1 EVIDENCE LEDGER
        if live_pack and live_pack.get("evidence_ledger"):
            ledger = live_pack["evidence_ledger"]
            st.markdown("## 🧾 V4.1 Evidence Ledger")
            ec1, ec2 = st.columns(2)
            ec1.metric("Evidence completeness", f"{ledger.get("completeness",0):.0f}/100")
            ec2.metric("Primary evidence items", len(ledger.get("items",[])))
            st.caption("A retrieved document is evidence that the source exists; the ledger does not claim the document supports a thesis until its content is reviewed.")
            if ledger.get("items"):
                st.dataframe(pd.DataFrame(ledger["items"])[[c for c in ["area","source","date","document","claim","confidence","materiality"] if c in pd.DataFrame(ledger["items"]).columns]], use_container_width=True, hide_index=True)

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

        # Deep financial forensics
        forensic = status.get("financial_forensics", {})
        st.markdown("## 🔬 Deep financial forensics")
        fc1, fc2, fc3, fc4 = st.columns(4)
        fc1.metric("Revenue CAGR", f"{forensic.get('revenue_cagr_5y'):.1%}" if forensic.get('revenue_cagr_5y') is not None else "N/A")
        fc2.metric("EPS CAGR", f"{forensic.get('eps_cagr_5y'):.1%}" if forensic.get('eps_cagr_5y') is not None else "N/A")
        fc3.metric("FCF conversion", f"{forensic.get('fcf_conversion'):.1%}" if forensic.get('fcf_conversion') is not None else "N/A")
        fc4.metric("ROIC proxy", f"{forensic.get('roic_proxy'):.1%}" if forensic.get('roic_proxy') is not None else "N/A")

        fdf = pd.DataFrame([
            {"Forensic measure":"Growth quality score", "Value": forensic.get("growth_quality_score"), "Interpretation":"Growth quality after checking revenue, EPS, cash conversion and margins"},
            {"Forensic measure":"Operating margin change", "Value": forensic.get("operating_margin_change"), "Interpretation":"Change versus earliest comparable annual period"},
            {"Forensic measure":"Net income CAGR", "Value": forensic.get("net_income_cagr_5y"), "Interpretation":"Long-term earnings compounding"},
            {"Forensic measure":"Share count CAGR", "Value": forensic.get("shares_cagr_5y"), "Interpretation":"Positive can indicate dilution"},
            {"Forensic measure":"Debt CAGR", "Value": forensic.get("debt_cagr_5y"), "Interpretation":"Debt trajectory where comparable positive debt data exists"},
        ])
        st.dataframe(fdf, use_container_width=True, hide_index=True)
        fleft, fright = st.columns(2)
        with fleft:
            st.markdown("### What the numbers support")
            for item in forensic.get("positives", []) or ["No strong positive forensic signal identified."]:
                st.success(item)
        with fright:
            st.markdown("### Forensic warning signs")
            for item in forensic.get("flags", []) or ["No major automated forensic warning identified."]:
                st.warning(item)

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

        # V3 research dossier framework
        st.markdown("## 🏛️ Investment Committee decision")
        ic = st.session_state.get("ic_decision", {})
        decision = ic.get("decision", "NEEDS MORE RESEARCH")
        decision_icon = {"BUY CANDIDATE":"🟢", "WATCH":"🟡", "AVOID":"🔴", "NEEDS MORE RESEARCH":"🟠"}.get(decision, "⚪")
        ic1, ic2, ic3, ic4 = st.columns(4)
        ic1.metric("Decision", f"{decision_icon} {decision}")
        ic2.metric("Evidence completeness", f"{ic.get('evidence_score', 0):.0f}/100")
        ic3.metric("Decision confidence", ic.get("confidence", "LOW"))
        ic4.metric("Opportunity", f"{ic.get('opportunity', 0):.0f}/100")
        st.info("**Decision gate:** a strong quantitative score cannot override missing primary-source evidence. The system can therefore deliberately return NEEDS MORE RESEARCH.")

        with st.expander("📌 Why the committee reached this decision", expanded=True):
            for item in ic.get("reasons", []): st.success(item)
            for item in ic.get("blockers", []): st.error(item)

        with st.expander("🧾 Evidence gates", expanded=True):
            for gate in ic.get("evidence_gates", []):
                icon = "✅" if gate.get("status") == "PASS" else "⏳"
                st.write(f"{icon} **{gate.get('area','')}** — {gate.get('status','')} · {gate.get('detail','')}")

        with st.expander("➡️ What needs to happen before investing", expanded=False):
            for item in ic.get("next_actions", []): st.write("• " + item)

        st.markdown("### 📋 Final investment case")
        ic_report = st.session_state.get("ic_report", {})
        st.write("**Decision:**", ic_report.get("executive_decision", "N/A"))
        st.write("**Evidence score:**", ic_report.get("evidence_score", "N/A"))
        st.write("**What changed:**", ic_report.get("recent_events", {}).get("status", "No event summary available."))

        st.markdown("## 🧾 Full Investigation roadmap")
        framework = st.session_state.get("full_framework", full_investigation_framework(report, thesis))
        for section, status_text in framework.items():
            with st.expander(section, expanded=section in {"Executive verdict", "Financial forensics"}):
                st.write(status_text)

        # V3.3 news + change detection
        st.markdown("## 📰 Recent developments & what changed")
        changed = st.session_state.get("what_changed", {})
        if changed.get("status") == "NO PREVIOUS SNAPSHOT":
            st.info("This is the first investigation in this session. The tool has created a baseline snapshot so the next investigation can identify model-level changes.")
        else:
            st.metric("Change materiality", changed.get("materiality", "UNKNOWN"))
            st.write(changed.get("headline", "No change summary available."))
            for change in changed.get("changes", []):
                pct = change.get("pct_change")
                pct_text = f" ({pct:+.1%})" if isinstance(pct, (int, float)) else ""
                st.write(f"• **{change.get('label','Change')}**: {change.get('old')} → {change.get('new')}{pct_text}")

        timeline = st.session_state.get("event_timeline", {})
        with st.expander("📅 Dated event timeline", expanded=False):
            st.caption(timeline.get("status", "Timeline not available."))
            for event in timeline.get("events", [])[:20]:
                headline = event.get("headline", "Event")
                date = event.get("date", "")
                source = event.get("source", "")
                st.write(f"**{date} — {event.get('type','EVENT')}** · {headline} · {source}")
                if event.get("url"):
                    st.markdown(event["url"])
            if not timeline.get("events"):
                st.warning("No dated primary events were retrieved. Use the research routes below and verify the latest announcements before relying on the thesis.")

        with st.expander("🔎 News research routes", expanded=False):
            for route in timeline.get("routes", []):
                st.write(f"**{route.get('source','Source')}** — {route.get('status','')} — {route.get('use','')}")
                if route.get("url"):
                    st.markdown(route["url"])
            st.caption(timeline.get("research_rule", "Verify material news against primary sources."))

        st.markdown("## 👥 Management & capital allocation")
        mgmt = st.session_state.get("management_pack", {})
        st.caption(mgmt.get("status", "Management research not available."))
        st.write("**What we will test:**")
        for check in mgmt.get("checks", []):
            st.write("• " + check)
        st.write("**Automatic financial prompts:**")
        for flag in st.session_state.get("capital_flags", []):
            st.write("• " + flag)
        with st.expander("📄 Primary management evidence", expanded=False):
            for item in mgmt.get("primary_records", []):
                st.write(f"**{item.get('date','')} — {item.get('form','')}** · {item.get('status','')}")
                if item.get("url"):
                    st.markdown(item["url"])
        with st.expander("🔎 Official management research routes", expanded=False):
            for route in mgmt.get("official_routes", []):
                st.write(f"**{route.get('source','')}** — {route.get('purpose','')} · {route.get('status','')}")
                if route.get("url"):
                    st.markdown(route["url"])
        st.info("Management conclusions are only promoted into conviction after the relevant proxy/annual-report, ownership, dealing and capital-allocation evidence has been reviewed.")

        st.markdown("## 🏭 Competition & industry intelligence")
        comp = st.session_state.get("competitive_pack", {})
        st.caption(comp.get("competitive_verdict", "Competitive analysis pending."))
        peers = comp.get("starter_peers", [])
        if peers:
            st.write("**Starter peer set:** " + ", ".join(peers) + " — these are research starting points, not automatically valid peers.")
        if comp.get("provisional_quant_score") is not None:
            st.metric("Provisional quantitative moat signal", f"{comp['provisional_quant_score']}/100")
            st.caption("This is deliberately NOT a final moat rating. It uses only available financial signals; qualitative competitive evidence remains pending.")
        with st.expander("📊 Quantitative competitive signals", expanded=True):
            for item in comp.get("quantitative_signals", []):
                st.write(f"**{item.get('metric','')}** — {item.get('value','N/A')} · {item.get('interpretation','')} · {item.get('status','')}")
        with st.expander("🛡️ Economic moat tests", expanded=False):
            for item in comp.get("moat_sources", []):
                st.write(f"**{item.get('source','')}** — {item.get('test','')} · {item.get('status','')}")
        with st.expander("⚔️ Industry Five Forces", expanded=False):
            for item in comp.get("five_forces", []):
                st.write(f"**{item.get('force','')}** — {item.get('question','')} · {item.get('status','')}")
        with st.expander("🎯 Sector-specific tests", expanded=False):
            for item in comp.get("sector_tests", []):
                st.write("• " + item)
        st.write(f"**Market-share evidence:** {comp.get('market_share_status','NOT RETRIEVED')}")
        st.write(f"**TAM evidence:** {comp.get('tam_status','NOT RETRIEVED')}")
        st.info("Competitive conclusions should only be promoted into conviction after peer financials, market-share evidence, customer/supplier evidence and primary company disclosures have been reviewed.")

        st.markdown("## 🎯 Catalysts & scenario engine")
        scen = st.session_state.get("scenario_pack", build_scenario_catalyst_engine(report, thesis, st.session_state.get("event_timeline")))
        st.caption(scen.get("status", "Scenario engine pending."))
        sv = scen.get("probability_weighted_value")
        su = scen.get("probability_weighted_upside_downside")
        sc1, sc2, sc3 = st.columns(3)
        sc1.metric("Probability-weighted value", money(sv))
        sc2.metric("Weighted upside/downside", "N/A" if su is None else f"{su:+.1%}")
        sc3.metric("Dated events available", str(scen.get("recent_events_available", 0)))
        with st.expander("📈 Bear / base / bull assumptions", expanded=True):
            for key in ("bear", "base", "bull"):
                x=scen.get("scenarios",{}).get(key,{})
                up=x.get("upside_downside")
                up_text="N/A" if up is None else f"{up:+.1%}"
                st.write(f"**{x.get('label',key.title())}** · probability {x.get('probability',0):.0%} · fair value {money(x.get('fair_value'))} · {up_text}")
                st.caption(x.get("assumption", ""))
        with st.expander("🚀 Potential catalysts", expanded=False):
            for x in scen.get("catalysts",[]): st.write(f"• **{x.get('materiality','')}** — {x.get('catalyst','')} · {x.get('status','')}")
        with st.expander("💥 Thesis breakers", expanded=False):
            for x in scen.get("thesis_breakers",[]): st.write("• " + x)
        with st.expander("👀 Ongoing monitoring triggers", expanded=False):
            for x in scen.get("monitoring_triggers",[]): st.write(f"**{x.get('trigger','')}** — {x.get('watch','')}")
        st.info("Scenario probabilities are defaults for structured thinking, not forecasts. Material catalysts and thesis breakers must be verified against primary evidence.")

        st.markdown("## 🧬 Patents & intellectual property")
        st.caption("V3 separates patent discovery from patent conclusions. A search link is not evidence that a patent exists or is material.")
        patent_links = st.session_state.get("patent_links", patent_research_links(report.company, report.symbol))
        for item in patent_links:
            st.markdown(f"**{item['source']}** — {item['purpose']} · {item['status']}")
            st.markdown(item['url'])
        st.info("Patent findings will only be promoted into the investment thesis after the underlying application/grant, filing date, assignee, status and relevance have been verified.")

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
                        data = load_company_data_v2(ticker_value, api_key)
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
st.caption("Company Investigator • Version 4.1 • Evidence-first live research + Investment Committee dossier")

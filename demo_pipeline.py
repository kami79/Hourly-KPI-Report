"""
demo_pipeline.py
----------------
Offline demonstration script for the Hourly KPI Platform.

Simulates a live hourly telecom network snapshot across 6 MSC nodes,
computes deltas against rolling baselines, applies multi-tiered SLA thresholds,
constructs a 48-hour incident matrix, and generates:
  1. Responsive Executive HTML Email (saved to output/demo_alert_email.html)
  2. PowerBI-ready Sanitized CSV (saved to output/demo_KPI_MSC_HOURLY.csv)
  3. Executive PDF Report (saved to output/demo_Hourly_KPI_Report.pdf)

Zero database connection or external credentials required.
Run:
    python demo_pipeline.py
"""

from __future__ import annotations

import os
import sys
import time
from datetime import datetime, timedelta
import numpy as np
import pandas as pd

# Core & Quality modules
from quality.deltas import apply as apply_deltas
from quality.thresholds import apply as apply_thresholds
from alerts.alert_builder import (
    build_alert_report,
    build_email_bodies,
    compute_changes,
    summarize_network,
)
from reports.pdf_report import generate_pdf_report
from core.term_ui import (
    init_terminal,
    print_banner,
    print_stage_header,
    print_substep,
    print_health_card,
    print_alerts_table,
    print_summary_card,
    c,
    FG_BRIGHT_CYAN,
    FG_BRIGHT_WHITE,
    FG_GRAY,
    BOLD,
)


def generate_synthetic_snapshot(timestamp: datetime, degradation_profile: str = "normal") -> pd.DataFrame:
    """
    Generates realistic hourly MSC counter snapshots for 6 core nodes.
    Profiles:
      - 'normal': Healthy network baseline
      - 'incident': Injects realistic degradation (e.g., CSSR drop on South node, Paging spike on East node)
    """
    nodes = [
        "MSC-NORTH-01",
        "MSC-NORTH-02",
        "MSC-SOUTH-01",
        "MSC-EAST-01",
        "MSC-WEST-01",
        "MSC-CENTRAL-GW",
    ]

    day_str = timestamp.strftime("%Y-%m-%d")
    hour_val = timestamp.hour

    records = []
    for node in nodes:
        # Base healthy values
        auth_sr = np.random.uniform(99.2, 99.9)
        mo_cassr = np.random.uniform(99.4, 99.9)
        cassr_gsm = np.random.uniform(99.1, 99.7)
        ciph_gsm = np.random.uniform(99.7, 100.0)
        ciph_wcdma = np.random.uniform(99.8, 100.0)
        imei_sr = np.random.uniform(95.0, 98.5)
        non_reg_lusr = np.random.uniform(93.0, 97.0)
        reg_lusr = np.random.uniform(94.0, 98.0)
        reg_gsm_lusr = np.random.uniform(93.5, 97.5)
        periodic_lusr = np.random.uniform(95.0, 98.8)
        overall_lusr = np.random.uniform(94.5, 98.2)
        sys_paging_sr = np.random.uniform(92.0, 98.5)
        cssr = np.random.uniform(98.8, 99.6)
        call_sr = np.random.uniform(98.9, 99.7)
        ho_sr = np.random.uniform(98.5, 99.5)
        erlang = np.random.uniform(4200.0, 8500.0)
        vlr_subs = int(np.random.uniform(350000, 620000))

        # Inject simulated issues if incident profile active
        if degradation_profile == "incident":
            if node == "MSC-SOUTH-01":
                # Severe hardware degradation causing critical CSSR drop
                cssr = 92.40
                call_sr = 93.10
                cassr_gsm = 96.20
            elif node == "MSC-EAST-01":
                # Warning-level paging degradation
                sys_paging_sr = 78.50
            elif node == "MSC-WEST-01":
                # Minor dip in authentication
                auth_sr = 97.80

        records.append({
            "node": node,
            "day": day_str,
            "hour": hour_val,
            "datehour": timestamp,
            "authentication_sr": round(auth_sr, 2),
            "mo_channel_assignment_sr": round(mo_cassr, 2),
            "channel_assignment_sr_gsm": round(cassr_gsm, 2),
            "ciphering_sr_gsm": round(ciph_gsm, 2),
            "ciphering_sr_wcdma": round(ciph_wcdma, 2),
            "imei_fetch_ms_sr": round(imei_sr, 2),
            "non_registered_subscribers_lusr": round(non_reg_lusr, 2),
            "registered_subscribers_lusr": round(reg_lusr, 2),
            "registered_gsm_subscribers_lusr": round(reg_gsm_lusr, 2),
            "periodic_update_lusr": round(periodic_lusr, 2),
            "overall_lusr": round(overall_lusr, 2),
            "system_perceived_paging_success": round(sys_paging_sr, 2),
            "cssr": round(cssr, 2),
            "call_sr": round(call_sr, 2),
            "inter_msc_handover_success_gsm": round(ho_sr, 2),
            "erlang_traffic": round(erlang, 2),
            "vlr_subscribers": vlr_subs,
        })

    return pd.DataFrame(records)


def run_demo():
    t_start = time.time()
    init_terminal()

    now = datetime.now().replace(minute=0, second=0, microsecond=0)

    print_banner(
        title="Telecom Hourly KPI Surveillance & Alerting Platform",
        subtitle="Offline Demonstration & Simulation Runner (v2.1)",
        meta_items=[
            ("Snapshot Timestamp", now.strftime("%Y-%m-%d %H:00:00")),
            ("Simulated Nodes", "6 Core MSCs"),
            ("Profile", "Active Incident Injection"),
        ],
        width=80,
    )

    output_dir = os.path.join(os.path.dirname(__file__), "output")
    history_dir = os.path.join(output_dir, "history")
    reports_dir = os.path.join(output_dir, "reports")
    os.makedirs(output_dir, exist_ok=True)
    os.makedirs(history_dir, exist_ok=True)
    os.makedirs(reports_dir, exist_ok=True)

    # ── Stage 1: Data Synthesis ──────────────────────────────────────────────
    print_stage_header(
        1, 6,
        "Snapshot Simulation & History Alignment",
        "Generating synthetic counter data for current window and 5 rolling baseline hours",
    )
    t0 = time.time()
    historical_snapshots = []
    for h in range(5, 0, -1):
        hist_time = now - timedelta(hours=h)
        prof = "incident" if h == 1 else "normal"
        df_hist = generate_synthetic_snapshot(hist_time, degradation_profile=prof)
        df_hist_processed = apply_deltas(df_hist, prev_df=None, rolling_dfs=None)
        df_hist_processed = apply_thresholds(df_hist_processed)
        historical_snapshots.append(df_hist_processed)

    curr_raw = generate_synthetic_snapshot(now, degradation_profile="incident")
    prev_raw = historical_snapshots[-1]
    print_substep("ok", f"Generated current snapshot ({len(curr_raw)} nodes)", f"{time.time()-t0:.2f}s")
    print_substep("ok", f"Aligned 5 rolling historical snapshots for baseline comparisons")

    # ── Stage 2: Delta Engine ─────────────────────────────────────────────────
    print_stage_header(
        2, 6,
        "Vectorized Delta Computation",
        "Evaluating hourly step changes (1h) and 24h diurnal deviations against rolling baselines",
    )
    t0 = time.time()
    curr_with_deltas = apply_deltas(curr_raw, prev_df=prev_raw, rolling_dfs=historical_snapshots)
    delta_cols = [c for c in curr_with_deltas.columns if c.endswith("_change") or c.endswith("_change_pct")]
    print_substep("ok", f"Vectorized deltas applied across {len(delta_cols)} KPI channels", f"{time.time()-t0:.2f}s")

    # ── Stage 3: Multi-Tier SLA Thresholds ────────────────────────────────────
    print_stage_header(
        3, 6,
        "Multi-Tier SLA Threshold Classification",
        "Categorizing performance against critical and warning thresholds (Voice, Mobility, SMS)",
    )
    t0 = time.time()
    curr_evaluated = apply_thresholds(curr_with_deltas)

    # Export clean CSV
    powerbi_csv_path = os.path.join(output_dir, "demo_KPI_MSC_HOURLY.csv")
    curr_evaluated.to_csv(powerbi_csv_path, index=False)
    print_substep("ok", f"SLA rules evaluated across all telecom network dimensions", f"{time.time()-t0:.2f}s")
    print_substep("ok", f"Sanitized PowerBI dataset written: {powerbi_csv_path}")

    # ── Stage 4: Network Health Analysis & Matrix ─────────────────────────────
    print_stage_header(
        4, 6,
        "Executive Health Analysis & Alert Matrix",
        "Synthesizing network health score, degraded nodes, and multi-hour incident timeline",
    )
    t0 = time.time()
    alert_df, summary = build_alert_report(curr_evaluated)
    prev_alert_df, _ = build_alert_report(prev_raw)
    changes = compute_changes(alert_df, prev_alert_df, summary)
    metrics = summarize_network(alert_df, full_df=curr_evaluated)
    print_substep("ok", f"Health analysis completed in {time.time()-t0:.2f}s")

    # Render Network Health Card
    print_health_card(metrics, summary, width=78)

    # Render Alerts Table
    if alert_df is not None and not alert_df.empty:
        print_alerts_table(alert_df, max_rows=6, width=78)

    # ── Stage 5: Responsive Executive Email ───────────────────────────────────
    print_stage_header(
        5, 6,
        "Executive HTML Email Composition",
        "Assembling mobile-responsive executive email digest with visual KPI badges and incident summary",
    )
    t0 = time.time()
    body_text, body_html = build_email_bodies(
        alert_df=alert_df,
        summary=summary,
        prev_alert_df=prev_alert_df,
        full_df=curr_evaluated,
        changes=changes,
        metrics=metrics,
    )

    demo_email_path = os.path.join(output_dir, "demo_alert_email.html")
    with open(demo_email_path, "w", encoding="utf-8") as f:
        f.write(body_html)
    print_substep("ok", f"HTML email rendered with modern executive styling", f"{time.time()-t0:.2f}s")
    print_substep("ok", f"Saved preview: {demo_email_path}")

    # ── Stage 6: Executive PDF Generation ─────────────────────────────────────
    print_stage_header(
        6, 6,
        "Executive PDF Management Report",
        "Compiling multi-page PDF with landscape health matrices, hotspot rankings, and charts",
    )
    t0 = time.time()
    demo_pdf_path = os.path.join(reports_dir, f"demo_Hourly_KPI_Report_{now.strftime('%Y%m%d_%H%M')}.pdf")
    try:
        generate_pdf_report(
            df=curr_evaluated,
            output_pdf_path=demo_pdf_path,
            alert_df=alert_df,
            summary=summary,
            changes=changes,
        )
        print_substep("ok", f"Executive PDF report compiled successfully", f"{time.time()-t0:.2f}s")
        print_substep("ok", f"Saved PDF: {demo_pdf_path}")
    except Exception as exc:
        print_substep("warn", f"PDF generation warning: {exc}")
        demo_pdf_path = None

    # ── Final Execution Summary Card ──────────────────────────────────────────
    total_elapsed = time.time() - t_start
    artifacts = [
        ("PowerBI CSV", powerbi_csv_path),
        ("HTML Digest", demo_email_path),
    ]
    if demo_pdf_path:
        artifacts.append(("PDF Report", demo_pdf_path))

    print_summary_card(
        duration_sec=total_elapsed,
        rows_count=len(curr_evaluated),
        nodes_count=metrics.get("nodes_monitored", 6),
        incidents_count=int(summary.get("critical", 0) or 0) + int(summary.get("warning", 0) or 0),
        artifacts=artifacts,
        status="SUCCESS",
        width=80,
    )

    html_url = f"file:///{demo_email_path.replace(os.sep, '/')}"
    sys.stdout.write(f"   {c('✦ Tip:', FG_BRIGHT_CYAN, BOLD)} {c('Open the HTML Email in your browser to view the executive dashboard:', FG_GRAY)}\n")
    sys.stdout.write(f"     {c(html_url, FG_BRIGHT_CYAN, BOLD)}\n\n")
    sys.stdout.flush()


if __name__ == "__main__":
    run_demo()

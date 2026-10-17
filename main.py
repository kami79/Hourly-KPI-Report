
"""
main.py — Hourly KPI Engine (v2.1)
----------------------------------
Loads a raw SQL snapshot, applies delta/threshold logic, exports to CSV,
saves to history, builds alerts, builds a 48-hour incident history across
ALL nodes, and sends the email report.

Fixed in v2.1:
  - [BUG-CRITICAL] Fixed ValueError crash in _format_incidents_html() on NaT/missing timestamps.
  - [BUG-CRITICAL] Replaced sys.exit(1) in _resolve_node_column() with ValueError so bad historical
                   snapshots can be skipped gracefully without terminating the live run.
  - [BUG-CRITICAL] Safe handling of null/empty email attachment lists in config.json.
  - [DATA] Prevented export_csv_for_powerbi() from turning NaN/None into literal "nan"/"None" strings.
  - [DATA] Fixed make_unique_columns() to prevent column collisions with pre-existing _1/_2 suffixes.
  - [DATA] Fixed datehour rebuild to preserve existing datehour values when day/hour parsing produces NaT.
  - [LOGIC] Fixed inspect.signature check to properly recognize functions that use **kwargs.
  - [ROBUSTNESS] Improved error recovery across historical snapshot loops.
"""

from __future__ import annotations

import csv
import html
import inspect
import json
import os
import re
import sys
import traceback
import warnings
from datetime import datetime

import pandas as pd

# Suppress noisy but harmless Pandas/SQLAlchemy compatibility warnings.
warnings.filterwarnings("ignore", category=pd.errors.PerformanceWarning)
warnings.filterwarnings("ignore", message="pandas only supports SQLAlchemy connectable")

# load_env() mirrors the Daily engine's security fix: DB_PASSWORD /
# EMAIL_PASSWORD are read from .env, never from config.json. Falls back to
# a no-op if this project doesn't have core/env_loader.py yet.
try:
    from core.env_loader import load_env
except ImportError:
    def load_env(*_args, **_kwargs):
        return None

from core.history import (
    load_last_n_snapshots,
    load_previous,
    save_snapshot,
    set_history_dir,
)
from core.loader import load_query_to_df
from core.logger import log, log_section, log_banner, notify_desktop
from core.mailer import send_email
from core.term_ui import (
    init_terminal,
    print_banner,
    print_stage_header,
    print_substep,
    print_health_card,
    print_alerts_table,
    print_summary_card,
    print_error_card,
    c,
    FG_BRIGHT_CYAN,
    FG_BRIGHT_WHITE,
    FG_GRAY,
    BOLD,
)

from quality.deltas import apply as apply_deltas
from quality.thresholds import apply as apply_thresholds

from reports.pdf_report import generate_pdf_report
from alerts.alert_builder import (
    build_alert_report,
    build_email_bodies,
    compute_changes,
    summarize_network,
)


# ─────────────────────────────────────────────────────────────────────────────
# Column helpers
# ─────────────────────────────────────────────────────────────────────────────

def sanitize_column_name(col: str) -> str:
    col = str(col).strip()
    col = col.replace("\n", " ").replace("\r", " ").replace("\t", " ")
    col = re.sub(r"\s+", "_", col)
    return col


def make_unique_columns(columns):
    """
    Sanitizes column names and guarantees unique names, even if original columns
    contain pre-existing numerical suffixes like '_1'.
    """
    seen = set()
    result = []
    for col in columns:
        base_col = sanitize_column_name(col)
        candidate = base_col
        count = 1
        while candidate in seen:
            candidate = f"{base_col}_{count}"
            count += 1
        seen.add(candidate)
        result.append(candidate)
    return result


def export_csv_for_powerbi(df: pd.DataFrame, output_csv: str) -> pd.DataFrame:
    """
    Write df to CSV with sanitised, unique column names and clean string values.
    Returns the renamed copy; the original df is not modified.
    Preserves NaN / None values as true empty CSV fields.
    """
    export = df.copy()
    export.columns = make_unique_columns(export.columns)

    # Ensure all metric columns are numeric and rounded to 2 decimal places
    for col in export.columns:
        if col not in ("node", "datehour", "day", "hour", "ELEM") and not str(col).endswith("_status"):
            try:
                s_num = pd.to_numeric(export[col], errors="coerce")
                if s_num.notna().any():
                    export[col] = s_num.round(2)
            except Exception:
                pass

    for col in export.columns:
        if pd.api.types.is_string_dtype(export[col]) or str(export[col].dtype) == "object":
            # Sanitize string newlines without converting NaN to literal "nan"
            export[col] = export[col].apply(
                lambda x: x.replace("\n", " ").replace("\r", " ") if isinstance(x, str) else x
            )

    export.to_csv(output_csv, index=False, encoding="utf-8", quoting=csv.QUOTE_ALL, float_format="%.2f")
    return export


# ─────────────────────────────────────────────────────────────────────────────
# Node-column resolution — fixes the "only ARMSS5" bug
# ─────────────────────────────────────────────────────────────────────────────

def _resolve_node_column(df: pd.DataFrame, verbose: bool = True) -> pd.DataFrame:
    """
    Ensure df has a single, correct 'node' column identifying EVERY network
    element in the dataset.

    Fix: 'ELEM' is ALWAYS treated as the canonical per-row node id when
    present. If a different 'node' column also exists and disagrees with
    'ELEM', it is dropped (with a warning) in favour of 'ELEM'.

    Raises ValueError if neither 'node' nor 'ELEM' is found.
    """
    df = df.copy()
    has_node = "node" in df.columns
    has_elem = "ELEM" in df.columns

    if has_elem:
        if has_node:
            try:
                same = df["node"].astype(str).equals(df["ELEM"].astype(str))
            except Exception:
                same = False
            if not same:
                sample_node = df["node"].iloc[0] if len(df) else "?"
                if verbose:
                    log(
                        "WARNING: Both 'node' and 'ELEM' columns are present and "
                        f"differ (e.g. existing 'node' = {sample_node!r} for every "
                        "row). 'ELEM' contains the real per-row element id and will "
                        "be used as 'node'. Dropping the original 'node' column.",
                        level="WARNING",
                    )
            df = df.drop(columns=["node"])
        df = df.rename(columns={"ELEM": "node"})
    elif not has_node:
        raise ValueError("Neither 'node' nor 'ELEM' column found in DataFrame.")

    n_unique = df["node"].nunique()
    sample = sorted(df["node"].dropna().astype(str).unique())[:10]
    suffix = "..." if n_unique > len(sample) else ""
    if verbose:
        log(f"Node identifier resolved -> 'node'. Unique nodes: {n_unique} (sample: {sample}{suffix})")

    if n_unique <= 1 and verbose:
        log(
            "WARNING: Only 1 unique node found after resolution. If you "
            "expected multiple nodes, check the SQL source — this is the "
            "exact symptom of the ARMSS5-only bug.",
            level="WARNING",
        )

    return df


# ─────────────────────────────────────────────────────────────────────────────
# Pipeline-stage logging
# ─────────────────────────────────────────────────────────────────────────────

def _log_stage(label: str, df: pd.DataFrame) -> None:
    """
    Reports BOTH row count and unique node count, so a one-node regression
    is visible at the exact stage it's introduced.
    """
    if df is None or df.empty:
        log(f"[{label}] rows=0 unique_nodes=0")
        return

    if "node" in df.columns:
        nodes = df["node"].nunique()
    else:
        nodes = "n/a (no 'node' column yet)"
    log(f"[{label}] rows={len(df)} unique_nodes={nodes}")


# ─────────────────────────────────────────────────────────────────────────────
# Config validation
# ─────────────────────────────────────────────────────────────────────────────

def _load_and_validate_config(path: str) -> dict:
    """
    Load config.json and verify that required top-level keys are present.
    """
    with open(path, "r", encoding="utf-8-sig") as f:
        cfg = json.load(f)

    required_keys = ["dsn", "username", "db_queries_path", "report_path", "history_path", "email"]
    for key in required_keys:
        if key not in cfg:
            raise ValueError(
                f"config.json is missing required key '{key}'. "
                f"Please check your configuration file."
            )

    return cfg


# ─────────────────────────────────────────────────────────────────────────────
# Email subject
# ─────────────────────────────────────────────────────────────────────────────

def build_subject(summary: dict, metrics: dict) -> str:
    summary = summary or {}
    metrics = metrics or {}
    hs = metrics.get('network_health_score', 100)
    hl = str(metrics.get('network_health_label', 'Healthy')).title()
    crit = int(summary.get('critical', 0) or 0)
    warn = int(summary.get('warning', 0) or 0)
    time_str = datetime.now().strftime("%d-%b-%Y %H:%M")

    if crit > 0 and warn > 0:
        issues_str = f"{crit} Critical, {warn} Warning"
    elif crit > 0:
        issues_str = f"{crit} Critical"
    elif warn > 0:
        issues_str = f"{warn} Warning"
    else:
        issues_str = "All Normal"

    return f"Hourly KPI Report - {time_str} | Health: {hs}% ({hl}) - {issues_str}"



# ─────────────────────────────────────────────────────────────────────────────
# Incident history — last N hours, ALL nodes
# ─────────────────────────────────────────────────────────────────────────────

def _ensure_status_columns(df: pd.DataFrame, label: str = "") -> pd.DataFrame:
    """Re-apply thresholds to a historical snapshot if it lacks *_status columns."""
    if any(str(c).endswith("_status") for c in df.columns):
        return df
    try:
        return apply_thresholds(df.copy())
    except Exception as exc:
        log(f"WARNING: could not apply thresholds to {label} snapshot — {exc}")
        return df


def build_recent_incidents(
    current_alert_df: pd.DataFrame,
    current_run_ts: pd.Timestamp,
    history_snapshots: list,
    window_hours: int,
) -> pd.DataFrame:
    """
    Combine this run's alerts with alerts extracted from recent history
    snapshots, covering the last `window_hours` hours across ALL nodes.
    """
    frames = []

    if current_alert_df is not None and not current_alert_df.empty:
        cur = current_alert_df.copy()
        cur["snapshot_time"] = current_run_ts
        frames.append(cur)

    cutoff = current_run_ts - pd.Timedelta(hours=window_hours)

    for snap in (history_snapshots or []):
        if snap is None or snap.empty or "datehour" not in snap.columns:
            continue

        try:
            snap = snap.copy()
            snap["_dt"] = pd.to_datetime(snap["datehour"], errors="coerce")
            snap = snap[snap["_dt"] >= cutoff]
            if snap.empty:
                continue

            snap = _resolve_node_column(snap, verbose=False)
            snap = _ensure_status_columns(snap, label="history")

            a_df, _ = build_alert_report(snap)
            if a_df is None or a_df.empty:
                continue

            valid_dts = snap["_dt"].dropna()
            snap_ts = valid_dts.iloc[0] if not valid_dts.empty else current_run_ts

            a_df = a_df.copy()
            a_df["snapshot_time"] = snap_ts
            frames.append(a_df)
        except Exception as exc:
            log(f"WARNING: skipping bad history snapshot during incident build — {exc}")
            continue

    if not frames:
        return pd.DataFrame()

    return pd.concat(frames, ignore_index=True, sort=False)


def summarize_incidents(incidents_df: pd.DataFrame) -> pd.DataFrame:
    """
    Roll the combined incidents DataFrame up to one row per
    (node, domain, kpi, status), with occurrence counts and a first/last
    seen window — covering every impacted node, sorted CRITICAL first.
    """
    if incidents_df is None or incidents_df.empty:
        return pd.DataFrame()

    required = {"node", "domain", "kpi", "status", "snapshot_time"}
    missing = required - set(incidents_df.columns)
    if missing:
        log(f"WARNING: incident summary skipped — missing columns: {sorted(missing)}")
        return pd.DataFrame()

    sev_order = {"CRITICAL": 0, "WARNING": 1}

    summary = (
        incidents_df.groupby(["node", "domain", "kpi", "status"])
        .agg(
            occurrences=("snapshot_time", "count"),
            first_seen=("snapshot_time", "min"),
            last_seen=("snapshot_time", "max"),
        )
        .reset_index()
    )
    summary["_sev"] = summary["status"].map(sev_order).fillna(2)
    summary = (
        summary.sort_values(["_sev", "occurrences", "node"], ascending=[True, False, True])
        .drop(columns="_sev")
        .reset_index(drop=True)
    )
    return summary


# ── Plain-text / HTML rendering (fallback if alert_builder isn't updated) ────

def _format_incidents_text(summary_df: pd.DataFrame, window_hours: int) -> str:
    lines = ["", "=" * 78, f"INCIDENT HISTORY — LAST {window_hours} HOURS (ALL NODES)", "=" * 78]

    if summary_df is None or summary_df.empty:
        lines.append("No CRITICAL/WARNING incidents recorded in this window.")
        return "\n".join(lines)

    n_nodes = summary_df["node"].nunique()
    n_crit  = int((summary_df["status"] == "CRITICAL").sum())
    n_warn  = int((summary_df["status"] == "WARNING").sum())

    lines.append(f"Nodes affected: {n_nodes}  |  CRITICAL rows: {n_crit}  |  WARNING rows: {n_warn}")
    lines.append("")
    lines.append(f"{'Node':<18}{'Domain':<16}{'KPI':<38}{'Status':<10}{'Count':>6}  Last seen")
    lines.append("-" * 100)

    for _, r in summary_df.iterrows():
        last_seen = pd.Timestamp(r["last_seen"]).strftime("%Y-%m-%d %H:%M") if pd.notna(r["last_seen"]) else "N/A"
        node_str = str(r['node'])[:17]
        domain_str = str(r['domain'])[:15]
        kpi_str = str(r['kpi'])[:37]
        lines.append(
            f"{node_str:<18}{domain_str:<16}{kpi_str:<38}"
            f"{str(r['status']):<10}{int(r['occurrences']):>6}  {last_seen}"
        )

    return "\n".join(lines)


def _format_incidents_html(summary_df: pd.DataFrame, window_hours: int) -> str:
    """
    Formats the incident history as an HTML block.
    FIXED: Safe pd.notna() check on last_seen prevents ValueError crashes on NaT.
    """
    if summary_df is None or summary_df.empty:
        return (
            "<div style='margin:18px 28px;padding:14px 16px;background:#f0fff4;"
            "border-radius:6px;border-left:4px solid #1a7a4a;font-family:Segoe UI,Arial,sans-serif'>"
            f"<p style='margin:0;font-weight:700;color:#1a7a4a'>Incident History — Last {window_hours} Hours (All Nodes)</p>"
            "<p style='margin:6px 0 0;color:#555;font-size:13px'>No CRITICAL/WARNING incidents recorded in this window.</p>"
            "</div>"
        )

    n_nodes = summary_df["node"].nunique()
    n_crit  = int((summary_df["status"] == "CRITICAL").sum())
    n_warn  = int((summary_df["status"] == "WARNING").sum())
    total   = len(summary_df)
    shown   = min(100, total)

    rows_html = ""
    for _, r in summary_df.head(shown).iterrows():
        st    = str(r["status"])
        color = "#b00020" if st == "CRITICAL" else "#e67e22"
        bg    = "#fff0f0" if st == "CRITICAL" else "#fff8ee"
        last_seen = pd.Timestamp(r["last_seen"]).strftime("%Y-%m-%d %H:%M") if pd.notna(r.get("last_seen")) else "N/A"
        occ = int(r["occurrences"]) if pd.notna(r.get("occurrences")) and str(r.get("occurrences")).strip() != "" else 0
        rows_html += (
            f"<tr style='background:{bg}'>"
            f"<td style='padding:5px 8px;font-weight:700'>{r['node']}</td>"
            f"<td style='padding:5px 8px'>{r['domain']}</td>"
            f"<td style='padding:5px 8px'>{r['kpi']}</td>"
            f"<td style='padding:5px 8px;text-align:center'>"
            f"<span style='background:{color};color:#fff;padding:1px 7px;border-radius:3px;"
            f"font-size:11px;font-weight:700'>{st}</span></td>"
            f"<td style='padding:5px 8px;text-align:center;font-weight:700'>{occ}</td>"
            f"<td style='padding:5px 8px;text-align:center;font-size:11px;color:#666'>{last_seen}</td>"
            f"</tr>"
        )

    note = ""
    if total > shown:
        note = (
            f"<p style='margin:8px 0 0;font-size:11px;color:#888'>"
            f"Showing {shown} of {total} node/KPI combinations. Full data in attached CSV.</p>"
        )

    return f"""
    <div style="margin:18px 28px 0;font-family:Segoe UI,Arial,sans-serif">
      <p style="margin:0 0 8px;font-size:15px;font-weight:700;color:#1a3a5c">
        Incident History — Last {window_hours} Hours (All Nodes)
      </p>
      <p style="margin:0 0 8px;font-size:12px;color:#666">
        {n_nodes} node(s) affected &nbsp;|&nbsp; {n_crit} CRITICAL occurrence(s) &nbsp;|&nbsp; {n_warn} WARNING occurrence(s)
      </p>
      <table width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;font-size:12px;border:1px solid #eee;border-radius:6px;overflow:hidden">
        <tr style="background:#f0f3f6">
          <th style="padding:6px 8px;text-align:left;color:#1a3a5c">Node</th>
          <th style="padding:6px 8px;text-align:left;color:#1a3a5c">Domain</th>
          <th style="padding:6px 8px;text-align:left;color:#1a3a5c">KPI</th>
          <th style="padding:6px 8px;text-align:center;color:#1a3a5c">Status</th>
          <th style="padding:6px 8px;text-align:center;color:#1a3a5c">Occurrences</th>
          <th style="padding:6px 8px;text-align:center;color:#1a3a5c">Last Seen</th>
        </tr>
        {rows_html}
      </table>
      {note}
    </div>
    """


def _inject_html_section(body_html: str, section_html: str) -> str:
    """
    Fallback insertion of section_html just before </body>, or append if no </body> tag.
    """
    if not body_html:
        return section_html
    idx = body_html.lower().rfind("</body>")
    if idx == -1:
        return body_html + section_html
    return body_html[:idx] + section_html + body_html[idx:]


def _send_emergency_failure_email(cfg: dict | None, exc: Exception, tb: str) -> None:
    if not cfg:
        return
    alert_cfg = cfg.get("alerts", {})
    if not alert_cfg.get("send_on_error", True):
        return
    try:
        time_str = datetime.now().strftime('%d-%b-%Y %H:%M')
        subject = f"Hourly KPI Alert - System Error - {time_str}"
        body_text = (
            f"Hourly KPI Surveillance Platform encountered a critical failure.\n\n"
            f"Error: {exc}\n\n"
            f"Stack Trace:\n{tb}\n\n"
            f"Please inspect the logs at logs/kpi_errors.log on the server."
        )
        body_html = f"""
        <div style="font-family:Segoe UI,Arial,sans-serif;padding:18px;border:2px solid #b00020;border-radius:8px">
          <h2 style="color:#b00020;margin-top:0">Hourly KPI Surveillance Failed</h2>
          <p><strong>Timestamp:</strong> {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</p>
          <p><strong>Error:</strong> <span style="color:#b00020;font-weight:700">{html.escape(str(exc))}</span></p>
          <h3>Stack Trace:</h3>
          <pre style="background:#f4f4f4;padding:12px;border-radius:4px;overflow-x:auto;font-size:12px">{html.escape(tb)}</pre>
          <p style="font-size:12px;color:#666">Check server log at <code>logs/kpi_errors.log</code> for full details.</p>
        </div>
        """
        send_email(subject=subject, body_text=body_text, body_html=body_html)
        log("Emergency failure email dispatched to alert recipients.")
    except Exception as email_exc:
        log(f"Failed to dispatch emergency failure email: {email_exc}", level="WARNING")


# ─────────────────────────────────────────────────────────────────────────────
# Main entry point
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    init_terminal()
    run_ts = datetime.now()

    # Load .env (DB_PASSWORD / EMAIL_PASSWORD) before anything else needs it.
    load_env()

    # ── Load and validate configuration ──────────────────────────────────────
    _script_dir = os.path.dirname(os.path.abspath(__file__))
    _candidates = [
        os.path.join(_script_dir, "config.json"),
        os.path.join(_script_dir, "config", "config.json"),
    ]
    config_path = next((p for p in _candidates if os.path.exists(p)), _candidates[-1])

    try:
        cfg = _load_and_validate_config(config_path)
    except (FileNotFoundError, ValueError, json.JSONDecodeError) as exc:
        print_error_card("Configuration Error", f"Failed to load or validate config file: {exc}")
        log(f"FATAL: config load failed — {exc}")
        sys.exit(1)

    db_queries_path = os.path.abspath(os.path.join(_script_dir, cfg["db_queries_path"])) if not os.path.isabs(cfg["db_queries_path"]) else cfg["db_queries_path"]
    report_path     = os.path.abspath(os.path.join(_script_dir, cfg["report_path"])) if not os.path.isabs(cfg["report_path"]) else cfg["report_path"]
    history_dir     = os.path.abspath(os.path.join(_script_dir, cfg["history_path"])) if not os.path.isabs(cfg["history_path"]) else cfg["history_path"]

    for d in (report_path, history_dir):
        os.makedirs(d, exist_ok=True)

    set_history_dir(history_dir)

    archive_db_path = cfg.get("archive_db_path")
    if archive_db_path:
        db_abs = os.path.abspath(os.path.join(_script_dir, archive_db_path)) if not os.path.isabs(archive_db_path) else archive_db_path
        os.environ["ARCHIVE_DB_PATH"] = db_abs
        os.makedirs(os.path.dirname(db_abs), exist_ok=True)

    output_csv = os.path.join(report_path, "KPI_MSC_HOURLY.csv")

    alert_cfg     = cfg.get("alerts", {})
    window_hours  = int(alert_cfg.get("incident_window_hours", 48))
    lookback_snaps = int(alert_cfg.get("incident_lookback_snapshots", max(window_hours, 48)))

    # Banner display
    print_banner(
        title="Telecom Hourly KPI Surveillance & Alerting Platform",
        subtitle="Autonomous Core Network Health & SLA Surveillance Engine (v2.1)",
        meta_items=[
            ("Run Timestamp", run_ts.strftime("%Y-%m-%d %H:%M:%S")),
            ("Environment", "Live Production"),
            ("Lookback Window", f"{window_hours}h Incident Horizon"),
        ],
        width=82,
    )
    log_section(f"HOURLY KPI SURVEILLANCE ENGINE STARTED — {run_ts.strftime('%Y-%m-%d %H:%M:%S')}")

    # SQL file resolution
    sql_filename = cfg.get("sql_filename")
    if sql_filename:
        sql_path = os.path.join(db_queries_path, sql_filename)
    else:
        try:
            sql_files = [f for f in os.listdir(db_queries_path) if f.lower().endswith(".sql")]
        except FileNotFoundError:
            sql_files = []
        if len(sql_files) == 1:
            sql_path = os.path.join(db_queries_path, sql_files[0])
            log(f"Auto-detected SQL file: {sql_files[0]}")
        elif len(sql_files) > 1:
            log(f"Multiple SQL files found in {db_queries_path}: {sql_files}")
            log("Add \"sql_filename\": \"yourfile.sql\" to config.json to pick one. Using first: " + sql_files[0])
            sql_path = os.path.join(db_queries_path, sql_files[0])
        else:
            print_error_card("SQL Query Missing", f"No .sql files found in {db_queries_path}")
            log(f"FATAL: No .sql files found in db_queries_path: {db_queries_path}")
            log("Either fix db_queries_path in config.json or add \"sql_filename\": \"yourfile.sql\"")
            sys.exit(1)

    # ── Stage 1: Load raw KPI data ───────────────────────────────────────────
    print_stage_header(
        1, 6,
        "Data Ingestion & SQL Query Execution",
        f"Extracting live counter snapshots via SQL from '{os.path.basename(sql_path)}'",
    )
    log(f"Loading hourly SQL: {sql_path}")
    t_load = datetime.now()
    try:
        df = load_query_to_df(sql_path)
    except Exception as exc:
        print_error_card("Database Query Failed", f"SQL query execution failed: {exc}", traceback.format_exc())
        log(f"FATAL: SQL query failed — {exc}\n{traceback.format_exc()}")
        sys.exit(1)

    if df is None or df.empty:
        print_error_card("Empty Dataset", "Database returned zero rows for the hourly snapshot.")
        log("FATAL: No data returned from SQL query. Exiting.")
        sys.exit(1)

    # Enforce exactly 2 decimal places on all numeric KPI columns early
    for c_name in df.columns:
        if c_name not in ("node", "datehour", "day", "hour", "ELEM") and not str(c_name).endswith("_status"):
            try:
                s_num = pd.to_numeric(df[c_name], errors="coerce")
                if s_num.notna().any():
                    df[c_name] = s_num.round(2)
            except Exception:
                pass

    _log_stage("after load", df)

    # ── Rebuild datehour from day + hour ──────────────────────────────────────
    if "day" in df.columns and "hour" in df.columns:
        try:
            hour_raw = pd.to_numeric(df["hour"], errors="coerce")
            n_bad_hour = int(hour_raw.isna().sum())
            if n_bad_hour:
                log(f"WARNING: {n_bad_hour} row(s) had non-numeric 'hour' values; treated as 0.")
            hour_num = hour_raw.fillna(0).astype(int)

            day_raw = pd.to_datetime(df["day"], dayfirst=False, errors="coerce")
            n_bad_day = int(day_raw.isna().sum())
            if n_bad_day:
                log(f"WARNING: {n_bad_day} row(s) had unparseable 'day' values; datehour will be NaT for these rows.")

            rebuilt = (day_raw + pd.to_timedelta(hour_num, unit="h")).dt.strftime("%Y-%m-%d %H:%M:%S")
            if "datehour" in df.columns:
                df["datehour"] = rebuilt.fillna(df["datehour"])
            else:
                df["datehour"] = rebuilt

            sample = df["datehour"].dropna().iloc[0] if df["datehour"].notna().any() else "N/A"
            log(f"datehour rebuilt from day+hour. Sample: {sample}")
        except Exception as exc:
            log(f"WARNING: Could not rebuild datehour — {exc}\n{traceback.format_exc()}")
    else:
        log("WARNING: 'day' or 'hour' column missing — datehour not rebuilt.")

    # ── Resolve node identifier column ────────────────────────────────────────
    try:
        df = _resolve_node_column(df)
    except ValueError as exc:
        print_error_card("Node Resolution Failed", str(exc))
        log(f"FATAL: {exc}")
        sys.exit(1)

    load_sec = (datetime.now() - t_load).total_seconds()
    print_substep("ok", f"Extracted {len(df):,} counter rows in {load_sec:.1f}s")
    print_substep("ok", f"Node identifiers resolved: {df['node'].nunique()} unique MSC nodes")
    _log_stage("after node resolution", df)

    # ── Stage 2: Load history ─────────────────────────────────────────────────
    print_stage_header(
        2, 6,
        "Historical Snapshot Alignment",
        f"Retrieving previous snapshot and {lookback_snaps} historical snapshots for {window_hours}h horizon",
    )
    prev_df = load_previous()

    try:
        incident_snapshots = load_last_n_snapshots(lookback_snaps)
        log(f"Loaded {len(incident_snapshots or [])} snapshot(s) for {window_hours}h incident history "
            f"(requested last {lookback_snaps} snapshots).")
        print_substep("ok", f"Loaded {len(incident_snapshots or [])} historical snapshots from archive")
    except Exception as exc:
        log(f"WARNING: could not load incident-history snapshots — {exc}\n{traceback.format_exc()}")
        incident_snapshots = []
        print_substep("warn", f"Could not load historical snapshots ({exc})")

    rolling_dfs = incident_snapshots[-7:] if incident_snapshots else load_last_n_snapshots(7)

    # ── Stage 3: Apply delta calculations ─────────────────────────────────────
    print_stage_header(
        3, 6,
        "Vectorized Delta Computation",
        "Evaluating step deviations (1h) and 7-day rolling diurnal baseline deltas",
    )
    t_delta = datetime.now()
    try:
        sig = inspect.signature(apply_deltas)
        if "rolling_dfs" in sig.parameters:
            df = apply_deltas(df, prev_df, rolling_dfs=rolling_dfs)
        else:
            df = apply_deltas(df, prev_df)
    except Exception as exc:
        print_error_card("Delta Calculation Failed", f"apply_deltas failed: {exc}", traceback.format_exc())
        log(f"FATAL: apply_deltas failed — {exc}\n{traceback.format_exc()}")
        sys.exit(1)

    delta_sec = (datetime.now() - t_delta).total_seconds()
    delta_cols = [col for col in df.columns if col.endswith("_change") or col.endswith("_change_pct")]
    print_substep("ok", f"Vectorized deltas applied across {len(delta_cols)} KPI channels ({delta_sec:.1f}s)")
    _log_stage("after apply_deltas", df)

    # ── Stage 4: Apply threshold flags & Persist Datasets ─────────────────────
    print_stage_header(
        4, 6,
        "Multi-Tier SLA Threshold Evaluation",
        "Classifying counter values against critical & warning thresholds and exporting datasets",
    )
    t_thresh = datetime.now()
    try:
        df = apply_thresholds(df)
    except Exception as exc:
        print_error_card("Threshold Application Failed", f"apply_thresholds failed: {exc}", traceback.format_exc())
        log(f"FATAL: apply_thresholds failed — {exc}\n{traceback.format_exc()}")
        sys.exit(1)

    _log_stage("after apply_thresholds", df)

    # Persist snapshot BEFORE renaming columns for Power BI
    history_path = None
    try:
        history_path = save_snapshot(df)
        if history_path:
            log(f"Archived snapshot into database: {history_path}")
            print_substep("ok", f"Snapshot archived to database: {os.path.basename(history_path)}")
            print_substep("ok", "Visualization dataset preserved: KPI_MSC_HOURLY_prev.csv")
        else:
            log("Snapshot was empty; skipped saving to history.")
            print_substep("warn", "Snapshot was empty; skipped archive")
    except Exception as exc:
        log(f"WARNING: snapshot save failed — {exc}\n{traceback.format_exc()}")
        print_substep("warn", f"History save warning: {exc}")

    # Export for Power BI
    try:
        export_csv_for_powerbi(df, output_csv)
        log(f"Saved Power BI CSV: {output_csv}")
        print_substep("ok", f"Sanitized Power BI dataset exported: {output_csv}")
    except Exception as exc:
        log(f"WARNING: CSV export failed — {exc}\n{traceback.format_exc()}")
        print_substep("warn", f"PowerBI CSV export warning: {exc}")

    # ── Stage 5: Build Alert DataFrame, Metrics & Incident Matrix ─────────────
    print_stage_header(
        5, 6,
        "Network Health Synthesis & Incident Matrix",
        f"Synthesizing network health score and compiling {window_hours}h incident timeline",
    )
    try:
        alert_df, summary = build_alert_report(df)
    except Exception as exc:
        print_error_card("Alert Report Failed", f"build_alert_report failed: {exc}", traceback.format_exc())
        log(f"FATAL: build_alert_report failed — {exc}\n{traceback.format_exc()}")
        sys.exit(1)

    if alert_df is not None and not alert_df.empty and "node" in alert_df.columns:
        log(f"[alert_df] rows={len(alert_df)} unique_nodes={alert_df['node'].nunique()}")
    else:
        log("[alert_df] no active alerts")

    # Build previous-snapshot alert DataFrame for change detection
    prev_alert_df = None
    if prev_df is not None and not prev_df.empty:
        try:
            prev_df = _resolve_node_column(prev_df.copy()) if "ELEM" in prev_df.columns else prev_df

            if any(str(c).endswith("_status") for c in prev_df.columns):
                prev_df_with_status = prev_df.copy()
            else:
                prev_df_with_status = apply_thresholds(prev_df.copy())

            prev_alert_df, _ = build_alert_report(prev_df_with_status)
        except Exception as exc:
            log(f"WARNING: previous alert snapshot failed — {exc}\n{traceback.format_exc()}")

    # Compute changes and network metrics
    try:
        changes = compute_changes(alert_df, prev_alert_df, summary)
        metrics = summarize_network(alert_df, full_df=df)
    except Exception as exc:
        print_error_card("Metrics Computation Failed", f"summarize_network failed: {exc}", traceback.format_exc())
        log(f"FATAL: metrics computation failed — {exc}\n{traceback.format_exc()}")
        sys.exit(1)

    # Build incident history (last N hours, ALL nodes)
    incidents_df = build_recent_incidents(
        current_alert_df=alert_df,
        current_run_ts=pd.Timestamp(run_ts),
        history_snapshots=incident_snapshots,
        window_hours=window_hours,
    )
    incident_summary = summarize_incidents(incidents_df)

    if not incident_summary.empty:
        log(
            f"[incident_summary] window={window_hours}h "
            f"unique_nodes={incident_summary['node'].nunique()} "
            f"rows={len(incident_summary)} "
            f"critical={(incident_summary['status'] == 'CRITICAL').sum()} "
            f"warning={(incident_summary['status'] == 'WARNING').sum()}"
        )
    else:
        log(f"[incident_summary] window={window_hours}h — no incidents recorded")

    incidents_csv_path = None
    if not incident_summary.empty:
        try:
            incidents_csv_path = os.path.join(report_path, f"incident_history_last_{window_hours}h.csv")
            incident_summary.to_csv(incidents_csv_path, index=False, encoding="utf-8", quoting=csv.QUOTE_ALL, float_format="%.2f")
            log(f"Saved incident history CSV: {incidents_csv_path}")
            print_substep("ok", f"Saved incident timeline CSV: {incidents_csv_path}")
        except Exception as exc:
            log(f"WARNING: incident history CSV export failed — {exc}\n{traceback.format_exc()}")
            incidents_csv_path = None

    # Render Network Health Card & Alerts Matrix in console
    print_health_card(metrics, summary, width=80)
    if alert_df is not None and not alert_df.empty:
        print_alerts_table(alert_df, max_rows=8, width=80)

    # ── Decide whether to send an email ──────────────────────────────────────
    send_on_critical = alert_cfg.get("send_on_critical", True)
    send_on_warning  = alert_cfg.get("send_on_warning",  False)
    send_always      = alert_cfg.get("send_always",      False)

    send_on_incident_history     = alert_cfg.get("send_on_incident_history", True)
    incident_history_min_critical = int(alert_cfg.get("incident_history_min_critical", 1))

    has_critical = (summary or {}).get("critical", 0) > 0
    has_warning  = (summary or {}).get("warning",  0) > 0

    incident_critical_count = 0
    if not incident_summary.empty:
        incident_critical_count = int((incident_summary["status"] == "CRITICAL").sum())
    has_incident_history = (
        send_on_incident_history
        and incident_critical_count >= incident_history_min_critical
    )

    should_send = (
        send_always
        or (has_critical and send_on_critical)
        or (has_warning  and send_on_warning)
        or has_incident_history
    )

    num_incidents = len(incident_summary) if incident_summary is not None else 0
    elapsed_sec = (datetime.now() - run_ts).total_seconds()

    if not should_send:
        log("No alerts to send based on current config thresholds/settings.")
        log_section("HOURLY KPI SURVEILLANCE ENGINE COMPLETED")
        artifacts = [("PowerBI CSV", output_csv)]
        if history_path:
            artifacts.append(("History Archive", history_path))
        print_summary_card(
            duration_sec=elapsed_sec,
            rows_count=len(df),
            nodes_count=df["node"].nunique(),
            incidents_count=num_incidents,
            artifacts=artifacts,
            status="OPTIMAL (CLEAN)",
            width=82,
        )
        return

    if not has_critical and not has_warning and has_incident_history:
        log(
            f"This hour's snapshot is clean, but sending report anyway: "
            f"incident history shows {incident_critical_count} CRITICAL "
            f"occurrence(s) across {incident_summary['node'].nunique()} node(s) "
            f"in the last {window_hours}h."
        )

    # ── Stage 6: Build email & generate Executive PDF report ──────────────────
    print_stage_header(
        6, 6,
        "Reporting & Deliverables Dispatch",
        "Compiling Executive PDF Report and transmitting responsive HTML email",
    )
    try:
        email_kwargs = dict(
            alert_df=alert_df,
            summary=summary,
            prev_alert_df=prev_alert_df,
            full_df=df,
            changes=changes,
            metrics=metrics,
        )

        be_sig = inspect.signature(build_email_bodies)
        accepts_var_kwargs = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in be_sig.parameters.values())

        native_incidents = False
        for kw_name in ("recent_incidents", "incident_summary", "incidents_df"):
            if kw_name in be_sig.parameters or accepts_var_kwargs:
                email_kwargs[kw_name] = incident_summary
                email_kwargs.setdefault("incident_window_hours", window_hours)
                if not accepts_var_kwargs and "incident_window_hours" not in be_sig.parameters:
                    email_kwargs.pop("incident_window_hours", None)
                native_incidents = True
                break

        body_text, body_html = build_email_bodies(**email_kwargs)

        if not native_incidents:
            body_text += "\n" + _format_incidents_text(incident_summary, window_hours)
            body_html = _inject_html_section(body_html, _format_incidents_html(incident_summary, window_hours))

    except Exception as exc:
        print_error_card("Email Composition Failed", f"build_email_bodies failed: {exc}", traceback.format_exc())
        log(f"FATAL: build_email_bodies failed — {exc}\n{traceback.format_exc()}")
        sys.exit(1)

    # Generate Management PDF Report
    pdf_report_path = None
    try:
        reports_dir = os.path.join(report_path, "reports")
        os.makedirs(reports_dir, exist_ok=True)
        pdf_filename = f"Hourly_KPI_Report_{run_ts.strftime('%Y%m%d_%H%M')}.pdf"
        pdf_report_path = os.path.join(reports_dir, pdf_filename)
        generate_pdf_report(
            df=df,
            output_pdf_path=pdf_report_path,
            alert_df=alert_df,
            summary=summary,
            changes=changes,
        )
        log(f"Saved Executive PDF Report: {pdf_report_path}")
        print_substep("ok", f"Executive PDF Report compiled: {pdf_report_path}")
    except Exception as exc:
        log(f"WARNING: PDF report generation failed — {exc}\n{traceback.format_exc()}")
        print_substep("warn", f"PDF report generation warning: {exc}")
        pdf_report_path = None

    # Send email
    attachments = list(cfg.get("email", {}).get("attachments") or [])
    if pdf_report_path and os.path.exists(pdf_report_path):
        attachments.append(pdf_report_path)
    if incidents_csv_path:
        attachments.append(incidents_csv_path)

    try:
        send_email(
            subject=build_subject(summary, metrics),
            body_text=body_text,
            body_html=body_html,
            attachments=attachments,
        )
        print_substep("ok", f"Alert email delivered with {len(attachments)} attachment(s)")
    except Exception as exc:
        tb = traceback.format_exc()
        print_error_card("Email Transmission Failed", f"send_email failed: {exc}", tb)
        log(f"FATAL: email send failed — {exc}\n{tb}", level="ERROR")
        notify_desktop("Hourly KPI Platform - Error", f"Email send failed: {exc}", is_error=True)
        _send_emergency_failure_email(cfg, exc, tb)
        sys.exit(1)

    elapsed_sec = (datetime.now() - run_ts).total_seconds()
    log_section("HOURLY KPI SURVEILLANCE ENGINE COMPLETED")
    log(
        f"Execution Summary: Duration={elapsed_sec:.1f}s | "
        f"Rows={len(df):,} | Nodes={df['node'].nunique()} | "
        f"48h Incidents={num_incidents} | Status=SUCCESS"
    )
    notify_desktop(
        "Hourly KPI Surveillance",
        f"Surveillance completed in {elapsed_sec:.1f}s. Report delivered.",
    )

    # Final Execution Summary Card
    artifacts = [
        ("PowerBI CSV", output_csv),
    ]
    if pdf_report_path:
        artifacts.append(("PDF Report", pdf_report_path))
    if incidents_csv_path:
        artifacts.append(("Incident CSV", incidents_csv_path))
    if history_path:
        artifacts.append(("History Archive", history_path))

    print_summary_card(
        duration_sec=elapsed_sec,
        rows_count=len(df),
        nodes_count=df["node"].nunique(),
        incidents_count=num_incidents,
        artifacts=artifacts,
        status="SUCCESS",
        width=82,
    )


if __name__ == "__main__":
    main()

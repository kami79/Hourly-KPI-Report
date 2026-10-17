# Contributing to Hourly KPI Platform

Thank you for your interest in contributing to the **Hourly KPI Platform**! This project provides enterprise-grade hourly surveillance, SLA tracking, incident correlation, and reporting for core telecommunications networks (MSC, VLR, SGSN, and RAN elements).

We welcome contributions ranging from new telecom KPI formulas and anomaly algorithms to performance enhancements and documentation improvements.

---

## Code of Conduct

Please help maintain a professional, respectful, and collaborative environment. Treat all team members and contributors with dignity and respect.

---

## Development Workflow

1. **Fork or Branch**:
   - Create a feature branch from `main`:
     ```bash
     git checkout -b feature/my-new-kpi
     ```

2. **Set Up Local Environment**:
   - Python 3.10 or higher is required.
   - Create and activate a virtual environment:
     ```bash
     python -m venv .venv
     # Windows:
     .venv\Scripts\activate
     # Linux/macOS:
     source .venv/bin/activate
     ```
   - Install dependencies:
     ```bash
     pip install -r requirements.txt
     ```

3. **Code Style & Standards**:
   - Write clean, type-annotated, PEP 8-compliant Python code.
   - Use vectorized Pandas and NumPy operations. Avoid iterative `iterrows()` or `itertuples()` over large snapshot DataFrames.
   - Keep SQL queries compatible with standard enterprise RDBMS (Sybase IQ, Oracle, MSSQL, PostgreSQL).
   - Ensure all database queries avoid CTEs where specific ODBC drivers suffer from truncation bugs.
   - Handle missing timestamps and empty values safely with `pd.to_numeric()`, `pd.to_datetime()`, and `errors="coerce"`.

4. **Testing Your Changes**:
   - Run the included offline demo pipeline to verify calculations, alert matrix generation, and reporting:
     ```bash
     python demo_pipeline.py
     ```
   - Verify that:
     - All 11 KPI suites compute cleanly without raising uncaught warnings or errors.
     - `output/demo_alert_email.html` renders valid HTML in your browser.
     - `output/demo_Hourly_KPI_Report.pdf` generates successfully.
     - `output/KPI_MSC_HOURLY.csv` contains sanitized, unique column headers without literal `"nan"` strings.

---

## Submitting Pull Requests

1. Commit your changes with clear, descriptive commit messages:
   ```bash
   git commit -m "feat(kpi): add VoLTE call drop rate metric to surveillance engine"
   ```
2. Push your branch to GitHub:
   ```bash
   git push origin feature/my-new-kpi
   ```
3. Open a Pull Request against `main`.
4. Include a concise summary of the change, affected modules, and test results.

---

## Security & Sensitive Information

- **NEVER** commit production database credentials, passwords, or live subscriber data.
- Place all environment variables in `.env` (which is gitignored). Use `.env.example` as a template.
- Sanitize any sample data before committing.

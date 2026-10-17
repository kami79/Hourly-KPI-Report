# GitHub Repository Upload Guide

This guide walks you through publishing this project to your GitHub account as a portfolio showcase.

---

## Preparation Complete

The repository has been fully configured and sanitized:
- Clean code architecture with zero private credentials or live subscriber data.
- Professional portfolio `README.md` with badges, architecture diagrams, and 3GPP metrics.
- In-depth `PORTFOLIO.md` case study and `ARCHITECTURE.md` specification.
- Standalone offline `demo_pipeline.py` script so visitors can test the pipeline immediately.
- `.gitignore`, `.env.example`, `LICENSE` (MIT), and `CONTRIBUTING.md`.
- A dedicated, pristine export folder: **`ready_to_upload_for_github/`**.

---

## Method 1: Push via Git CLI (Recommended)

### Step 1: Create a New Repository on GitHub
1. Open [GitHub](https://github.com/new).
2. Set **Repository name**: e.g., `Hourly-Telecom-KPI-Platform` or `Telecom-Core-KPI-Surveillance`.
3. Set **Description**: e.g., `Enterprise Telecom Core Network (MSC/VLR) Hourly KPI Surveillance, Automated Incident Detection, and Executive Reporting Engine.`
4. Choose **Public** (to showcase in your portfolio).
5. **DO NOT** check "Add a README file", ".gitignore", or "license" (we already created them).
6. Click **Create repository**.

### Step 2: Push from your local terminal
Open PowerShell in this project folder:
```powershell
# Verify git status
git status

# Stage the new portfolio documents and demo runner
git add .

# Commit changes
git commit -m "docs: add portfolio showcase, case study, architecture spec, and offline demo runner"

# Rename branch to main (if not already main)
git branch -M main

# Link to your new GitHub repository (replace with your actual GitHub URL)
git remote add origin https://github.com/YOUR_USERNAME/Hourly-Telecom-KPI-Platform.git

# Push code to GitHub
git push -u origin main
```

---

## Method 2: Using GitHub Desktop

1. Open **GitHub Desktop**.
2. Click **File** > **Add Local Repository...** (or press `Ctrl+O`).
3. Select this folder: `C:\Users\kamiran.gabriel\Desktop\KPIs\Hourly_KPI_Platform` (or the clean `ready_to_upload_for_github` folder).
4. Click **Publish repository** in GitHub Desktop.
5. Uncheck "Keep this code private" to make it visible on your portfolio.
6. Click **Publish Repository**.

---

## Method 3: Drag & Drop via GitHub Web UI

If you prefer uploading via browser without using the Git CLI:
1. Create a new repository on GitHub with a README.
2. Go to the clean folder: `Hourly_KPI_Platform/ready_to_upload_for_github/`.
3. Drag and drop all files and folders directly into GitHub's web interface.
4. Commit changes.

---

## Recommended GitHub Repository Settings

To make your repository stand out to recruiters and engineering leaders:
- **About Section Topics/Tags**:  
  `python` `data-engineering` `telecommunications` `3gpp` `pandas` `surveillance` `kpi-monitoring` `incident-response` `sybase` `powerbi` `etl-pipeline`
- **Pin to Profile**: On your GitHub profile page, click **Customize your pins** and pin this repository to the top.

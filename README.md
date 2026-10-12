# Auto FB Group Poster lite

Double-click `Start-Automation.bat`. Requires Python 3.11 or later with Tkinter. The launcher installs Playwright and its Chromium browser.

`sample CSV.csv` in the repository root contains the supplied group URLs and posts. Import it with **Import CSV**, or use your own CSV with the same columns.

1. Use **Open Login** once, sign in to Facebook, then press **Stop** to close the browser and save the session.
2. **Import CSV** accepts UTF-8 CSVs with `URL` and `post` columns (case insensitive), including multiline quoted posts. Other columns are preserved in the source file; `Group Name` is used for display.
3. Optionally enter truthful membership answers as `question phrase = answer`, one per line. The most specific matching phrase wins. Unknown questions are skipped rather than invented.
4. Choose headless mode if desired, then **Start Worker**. Joining, recognized rule checkboxes, saved membership answers and posting run automatically.

The worker console opens in its own resizable window. Drag its edges or maximize it. Closing the console hides it while logging continues; **Worker Console** reopens it with its current log history.

Administrator approval, login, CAPTCHA and identity checks cannot be automated reliably. Pending membership and unavailable composers are skipped. Security checks stop the run. English Facebook controls are currently supported; changed interfaces may need selector updates.

Complete means the composer closed after submission; it does not guarantee public visibility or administrator approval. Results without a clear submission confirmation require review. Completed and uncertain jobs are never automatically retried, including identical URL/post duplicates. Skipped and failed jobs may be attempted on the next start. Stop finishes the current group (bounded by 120 seconds); closing the app waits for a clean shutdown.

The `data` folder contains the dedicated login profile, progress, logs and failure screenshots. Treat it as private. Do not run two app instances against this profile. To intentionally repeat a completed post, close the app and remove that job's entry from `data/progress.json` only after checking Facebook. Do not delete progress merely to retry uncertain posts.

Manual setup in PowerShell:

```powershell
cd "$env:USERPROFILE\Documents\automation"
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m playwright install chromium
.\.venv\Scripts\python.exe app.py
```

Local browser fixtures validate the automation mechanics; live Facebook posting is not tested without your authenticated session. No account credentials are bundled.

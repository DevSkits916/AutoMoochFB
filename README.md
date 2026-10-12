# Auto Mooch

## Windows EXE

Download `AutoMooch.exe` from the repository's GitHub Releases page and double-click it. Python is not required. Microsoft Edge or Google Chrome must be installed for the worker. Download `sample CSV.csv` from the repository separately if needed.

The EXE keeps its login profile, progress and logs in `%LOCALAPPDATA%\AutoMooch`, independently of the source app's `data` folder. Sign in once with **Open Login**. The executable is unsigned; Windows may display a publisher or SmartScreen warning. `SHA256SUMS.txt` verifies file integrity.

To rebuild on Windows with Python installed: `powershell -ExecutionPolicy Bypass -File .\build.ps1`. Output files are in `releases`.

## Run from source

Double-click `Start-Automation.bat`. Requires Python 3.11 or later with Tkinter. The launcher installs Playwright and its Chromium browser.

`sample CSV.csv` in the repository root contains the supplied group URLs and posts. Import it with **Import CSV**, or use your own CSV with the same columns.

1. Use **Open Login** once, sign in to Facebook, then press **Stop** to close the browser and save the session.
2. **Import CSV** accepts UTF-8 CSVs with `URL` and `post` columns (case insensitive), including multiline quoted posts. Other columns are preserved in the source file; `Group Name` is used for display.
3. Optionally enter truthful membership answers as `question phrase = answer`, one per line. The most specific matching phrase wins. Unknown questions are skipped rather than invented.
4. Choose headless mode if desired, then **Start Worker**. Joining, recognized rule checkboxes, saved membership answers and posting run automatically.

The worker console opens in its own resizable window. Drag its edges or maximize it. Closing the console hides it while logging continues; **Worker Console** reopens it with its current log history.

**Shuffle Pending** randomizes waiting groups before or during a run without changing the active group or completed results. **Skip Current Group** cancels the current browser task and moves to the next group immediately. If submission has already started, the result requires review and is not automatically retried.

**Export Remaining CSV** saves a snapshot of waiting groups in queue order, preserving all original CSV columns and post text. It excludes the active group and finished results. You can import this CSV again later. During a run, the snapshot is taken when you click the button, before the save dialog opens.

Set **Composer timeout (seconds)** from 1–300 (default 15). Turn **Join groups automatically** off to attempt posting without requesting membership. Timeout and joining settings are captured when you start the worker; changes apply to the next run.

If membership checking times out, the worker still searches for the composer and attempts to post in that same group before proceeding. The composer gets its own timeout. Explicit pending approval and unanswered membership questions still skip the group; a timeout alone does not skip posting.

If Facebook shows “We limit how often you can post…” in the composer after submission, the worker stops without loading the next group, sounds a repeating Windows beep, and shows a posting-limit status. The current result requires review and is not automatically retried; remaining groups stay queued. Failure screenshots and logs are saved. Detection uses the English restriction message rather than text color, so ordinary red text in a post does not trigger the alarm.

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

from __future__ import annotations
import asyncio
import csv
import hashlib
import json
import os
import queue
import random
import threading
import time
import sys
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from tkinter.scrolledtext import ScrolledText
from pathlib import Path
from urllib.parse import urlparse
from playwright.async_api import async_playwright
from facebook import post_to_facebook, AutomationError

ROOT = Path(__file__).resolve().parent
DATA = (Path(os.environ.get('LOCALAPPDATA') or Path.home()) / 'AutoMooch') if getattr(sys, 'frozen', False) else ROOT / 'data'
DATA.mkdir(exist_ok=True)


def browser_options():
    if not getattr(sys, 'frozen', False):
        return {}
    for base, relative in [
        ('PROGRAMFILES(X86)', 'Microsoft/Edge/Application/msedge.exe'),
        ('PROGRAMFILES', 'Microsoft/Edge/Application/msedge.exe'),
        ('PROGRAMFILES', 'Google/Chrome/Application/chrome.exe'),
        ('LOCALAPPDATA', 'Google/Chrome/Application/chrome.exe'),
    ]:
        candidate = Path(os.environ.get(base) or '') / relative
        if candidate.is_file():
            return {'executable_path': str(candidate)}
    raise RuntimeError('Install Microsoft Edge or Google Chrome to run the worker in the Windows EXE.')


def load_csv(path):
    jobs = []
    with open(path, encoding='utf-8-sig', newline='') as handle:
        reader = csv.DictReader(handle)
        headers = {str(k).strip().casefold(): k for k in reader.fieldnames or []}
        if not {'url', 'post'} <= headers.keys():
            raise ValueError('CSV requires URL and post columns.')
        for number, row in enumerate(reader, 2):
            url = (row.get(headers['url']) or '').strip()
            post = row.get(headers['post']) or ''
            parsed = urlparse(url)
            if parsed.scheme != 'https' or parsed.hostname not in {'facebook.com', 'www.facebook.com', 'm.facebook.com'} or not parsed.path.startswith('/groups/') or len(parsed.path.strip('/').split('/')) < 2:
                raise ValueError(f'Record {number}: invalid Facebook group URL.')
            if not post.strip():
                raise ValueError(f'Record {number}: empty post.')
            canonical = 'https://www.facebook.com/groups/' + parsed.path.strip('/').split('/')[1]
            key = hashlib.sha256((canonical + '\0' + post).encode()).hexdigest()
            jobs.append(dict(id=key, groupUrl=canonical, postText=post,
                             name=row.get(headers.get('group name', ''), '') or canonical,
                             source_row={key: value for key, value in row.items() if key is not None},
                             source_headers=list(reader.fieldnames)))
    if not jobs:
        raise ValueError('CSV has no jobs.')
    return jobs


class App:
    def __init__(self, root):
        self.root = root
        self.jobs = []
        self.events = queue.Queue()
        self.stop = threading.Event()
        self.thread = None
        self.queue_lock = threading.Lock()
        self.pending = []
        self.current_task = None
        self.worker_loop = None
        self.skip_requested = False
        self.history_path = DATA / 'progress.json'
        self.history = json.loads(self.history_path.read_text('utf-8')) if self.history_path.exists() else {}
        self.headless = tk.BooleanVar()
        self.status = tk.StringVar(value='Ready • import a CSV to begin')
        self.task = tk.StringVar(value='No current task')
        self.counts = tk.StringVar(value='Complete 0     Failed 0     Skipped 0     Remaining 0')
        self.delay = tk.StringVar(value='30')
        self.composer_seconds = tk.StringVar(value='15')
        self.join_groups = tk.BooleanVar(value=True)
        root.title('Auto Mooch')
        root.geometry('1060x780')
        root.minsize(820, 620)
        style = ttk.Style()
        style.theme_use('clam')
        style.configure('.', font=('Segoe UI', 11))
        style.configure('Title.TLabel', font=('Segoe UI', 24, 'bold'))
        style.configure('HUD.TLabel', font=('Segoe UI', 13, 'bold'))
        frame = ttk.Frame(root, padding=24)
        frame.pack(fill='both', expand=True)
        ttk.Label(frame, text='Auto Mooch', style='Title.TLabel').pack(anchor='w')
        ttk.Label(frame, textvariable=self.status, style='HUD.TLabel').pack(anchor='w', pady=(16, 6))
        ttk.Label(frame, textvariable=self.task, wraplength=970).pack(anchor='w')
        ttk.Label(frame, textvariable=self.counts, style='HUD.TLabel').pack(anchor='w', pady=12)
        self.progress = ttk.Progressbar(frame)
        self.progress.pack(fill='x', pady=(0, 14))
        controls = ttk.Frame(frame)
        controls.pack(fill='x')
        self.buttons = []
        for text, command in [('Import CSV', self.import_csv), ('Open Login', lambda: self.launch(True)), ('Start Worker', lambda: self.launch(False))]:
            button = ttk.Button(controls, text=text, command=command)
            button.pack(side='left', padx=(0, 8))
            self.buttons.append(button)
        ttk.Button(controls, text='Stop', command=self.request_stop).pack(side='left')
        ttk.Button(controls, text='Worker Console', command=self.show_console).pack(side='left', padx=8)
        ttk.Checkbutton(controls, text='Headless', variable=self.headless).pack(side='right')
        queue_controls = ttk.Frame(frame)
        queue_controls.pack(fill='x', pady=(8, 0))
        ttk.Button(queue_controls, text='Shuffle Pending', command=self.shuffle_pending).pack(side='left')
        self.skip_button = ttk.Button(queue_controls, text='Skip Current Group', command=self.skip_current, state='disabled')
        self.skip_button.pack(side='left', padx=8)
        ttk.Button(queue_controls, text='Export Remaining CSV', command=self.export_remaining).pack(side='left')
        opts = ttk.Frame(frame)
        opts.pack(fill='x', pady=12)
        ttk.Label(opts, text='Seconds between groups:').pack(side='left')
        ttk.Entry(opts, textvariable=self.delay, width=6).pack(side='left', padx=8)
        ttk.Label(opts, text='Composer timeout (seconds):').pack(side='left')
        ttk.Entry(opts, textvariable=self.composer_seconds, width=6).pack(side='left', padx=8)
        ttk.Checkbutton(opts, text='Join groups automatically', variable=self.join_groups).pack(side='left')
        self.table = ttk.Treeview(frame, columns=('group', 'state'), show='headings', height=7)
        self.table.heading('group', text='Group')
        self.table.heading('state', text='Result')
        self.table.column('group', width=620)
        self.table.column('state', width=280)
        self.table.pack(fill='both', expand=True)
        advanced = ttk.LabelFrame(frame, text='Optional saved membership answers: question phrase = truthful answer', padding=6)
        advanced.pack(fill='x', pady=10)
        self.answers = tk.Text(advanced, height=2, font=('Segoe UI', 10))
        self.answers.pack(fill='x')
        settings = DATA / 'answers.txt'
        if settings.exists():
            self.answers.insert('1.0', settings.read_text('utf-8'))
        self.console_window = tk.Toplevel(root)
        self.console_window.title('Auto Mooch • Worker Console')
        self.console_window.geometry('900x450')
        self.console_window.minsize(420, 220)
        self.console_window.resizable(True, True)
        self.console_window.protocol('WM_DELETE_WINDOW', self.console_window.withdraw)
        console_frame = ttk.Frame(self.console_window, padding=12)
        console_frame.pack(fill='both', expand=True)
        ttk.Label(console_frame, text='Worker console', style='HUD.TLabel').pack(anchor='w', pady=(0, 8))
        self.console = ScrolledText(console_frame, bg='#17202b', fg='#dce7f3', font=('Consolas', 10), state='disabled')
        self.console.pack(fill='both', expand=True)
        root.protocol('WM_DELETE_WINDOW', self.close)
        root.after(100, self.poll)

    def log(self, text):
        self.events.put(('log', str(text)))

    def show_console(self):
        self.console_window.deiconify()
        self.console_window.lift()
        self.console_window.focus_set()

    def shuffle_pending(self):
        with self.queue_lock:
            if self.thread and self.thread.is_alive():
                random.shuffle(self.pending)
                order = list(self.pending)
            else:
                order = [int(i) for i in self.table.get_children()
                         if not self.table.set(i, 'state').startswith(('Complete', 'Review'))]
                random.shuffle(order)
            for index in order:
                self.table.move(str(index), '', 'end')
        self.log(f'Shuffled {len(order)} pending groups.')

    def skip_current(self):
        with self.queue_lock:
            task = self.current_task
            if task is None or task.done() or self.skip_requested:
                return
            self.skip_requested = True
            self.worker_loop.call_soon_threadsafe(task.cancel)
        self.skip_button.configure(state='disabled')
        self.log('Skipping current group…')

    def remaining_jobs(self):
        with self.queue_lock:
            if self.thread and self.thread.is_alive():
                indices = [i for i in self.pending
                           if not self.history.get(self.jobs[i]['id'], '').startswith(('Complete', 'Review'))]
            else:
                indices = [int(i) for i in self.table.get_children()
                           if self.table.set(i, 'state') == 'Queued']
            return [self.jobs[i] for i in indices]

    def export_remaining(self):
        # Snapshot before opening the dialog: the worker can keep advancing.
        jobs = self.remaining_jobs()
        if not jobs:
            messagebox.showinfo('Export Remaining CSV', 'There are no waiting groups to export.')
            return
        path = filedialog.asksaveasfilename(
            title='Export remaining queued groups', defaultextension='.csv',
            initialfile='remaining-groups.csv', filetypes=[('CSV files', '*.csv')])
        if not path:
            return
        try:
            with open(path, 'w', encoding='utf-8-sig', newline='') as handle:
                writer = csv.DictWriter(handle, fieldnames=jobs[0]['source_headers'])
                writer.writeheader()
                writer.writerows(job['source_row'] for job in jobs)
        except (OSError, ValueError, csv.Error) as error:
            messagebox.showerror('Export failed', str(error))
            return
        self.log(f'Exported {len(jobs)} waiting groups to {path}')
        messagebox.showinfo('Export complete', f'Saved {len(jobs)} waiting groups.')

    def import_csv(self):
        path = filedialog.askopenfilename(filetypes=[('CSV files', '*.csv')])
        if not path:
            return
        try:
            jobs = load_csv(path)
        except Exception as error:
            messagebox.showerror('Import failed', str(error))
            return
        self.jobs = jobs
        self.table.delete(*self.table.get_children())
        for index, job in enumerate(jobs):
            self.table.insert('', 'end', iid=str(index), values=(job['name'], self.history.get(job['id'], 'Queued')))
        self.status.set(f'Loaded {len(jobs)} groups • {Path(path).name}')
        self.update_counts()

    def update_counts(self):
        states = [self.table.set(item, 'state') for item in self.table.get_children()]
        complete = sum(s == 'Complete' for s in states)
        failed = sum(s.startswith(('Failed', 'Review')) for s in states)
        skipped = sum(s.startswith('Skipped') for s in states)
        done = complete + failed + skipped
        self.counts.set(f'Complete {complete}     Failed {failed}     Skipped {skipped}     Remaining {len(states)-done}')
        self.progress.configure(maximum=max(1, len(states)), value=done)

    def launch(self, login):
        if self.thread and self.thread.is_alive():
            return
        if not login and not self.jobs:
            messagebox.showinfo('Import CSV', 'Import a CSV before starting.')
            return
        try:
            delay = float(self.delay.get())
            if not 0 <= delay <= 3600:
                raise ValueError('Delay must be between 0 and 3600 seconds.')
            composer_seconds = float(self.composer_seconds.get())
            if not 1 <= composer_seconds <= 300:
                raise ValueError('Composer timeout must be between 1 and 300 seconds.')
            answers = []
            raw = self.answers.get('1.0', 'end').strip()
            for line in raw.splitlines():
                if not line.strip():
                    continue
                question, sep, answer = line.partition('=')
                if not sep or not question.strip() or not answer.strip():
                    raise ValueError('Use question phrase = answer, one per line.')
                answers.append({'question': question.strip(), 'answer': answer.strip()})
            (DATA / 'answers.txt').write_text(raw, encoding='utf-8')
        except ValueError as error:
            messagebox.showerror('Settings', str(error))
            return
        self.stop.clear()
        for button in self.buttons:
            button.configure(state='disabled')
        self.status.set('Login browser open' if login else 'Worker running')
        # Read Tk variables on the GUI thread only.
        headless = False if login else self.headless.get()
        join_groups = self.join_groups.get()
        with self.queue_lock:
            self.pending = [int(i) for i in self.table.get_children()]
        self.thread = threading.Thread(target=lambda: asyncio.run(self.run(login, headless, delay, answers, composer_seconds, join_groups)), daemon=True)
        self.thread.start()

    def save(self, key, state):
        self.history[key] = state
        temp = self.history_path.with_suffix('.tmp')
        temp.write_text(json.dumps(self.history, indent=2), encoding='utf-8')
        temp.replace(self.history_path)

    async def run(self, login, headless, delay, answers, composer_seconds=15, join_groups=True):
        try:
            async with async_playwright() as pw:
                context = await pw.chromium.launch_persistent_context(str(DATA / 'profile'), headless=headless, viewport={'width': 1440, 'height': 1000}, **browser_options())
                try:
                    page = context.pages[0] if context.pages else await context.new_page()
                    if login:
                        await page.goto('https://www.facebook.com/', wait_until='domcontentloaded', timeout=45000)
                        self.log('Sign in in the browser, then press Stop to save the session and close it.')
                        while not self.stop.is_set() and context.pages:
                            await asyncio.sleep(.25)
                        return
                    while True:
                        if self.stop.is_set():
                            break
                        with self.queue_lock:
                            if not self.pending:
                                break
                            index = self.pending.pop(0)
                        job = self.jobs[index]
                        previous = self.history.get(job['id'], '')
                        if previous.startswith(('Complete', 'Review')):
                            self.log(f"Preserving saved result: {job['name']} — {previous}")
                            self.events.put(('result', index, previous))
                            continue
                        self.events.put(('task', job['name']))
                        self.events.put(('result', index, 'Running'))
                        security = False
                        submitted = False
                        def step(text):
                            nonlocal submitted
                            if text == 'Submitting post...':
                                submitted = True
                                self.save(job['id'], 'Review: interrupted during submission')
                            self.events.put(('task', f"{job['name']} • {text}"))
                            self.log(text)
                        try:
                            with self.queue_lock:
                                self.worker_loop = asyncio.get_running_loop()
                                self.skip_requested = False
                                self.current_task = asyncio.create_task(post_to_facebook(
                                    page, job, step, join_before_post=join_groups,
                                    composer_timeout_ms=int(composer_seconds * 1000),
                                    membership_answers=answers, accept_group_rules=True))
                                current_task = self.current_task
                            self.events.put(('active', True))
                            async with asyncio.timeout(max(120, composer_seconds * 2 + 90)):
                                await current_task
                            state = 'Complete'
                        except asyncio.CancelledError:
                            state = 'Review: skipped during submission' if submitted else 'Skipped: user requested'
                            self.log(state)
                        except AutomationError as error:
                            security = error.security
                            prefix = 'Review' if submitted else ('Skipped' if error.code in {'JOIN_PENDING', 'COMPOSER_NOT_FOUND', 'GROUP_NOT_FOUND'} or security else 'Failed')
                            state = f'{prefix}: {error.code}'
                            self.log(str(error))
                        except Exception as error:
                            state = 'Review: uncertain submission' if submitted else 'Failed: browser error'
                            self.log(f'{type(error).__name__}: {error}')
                        finally:
                            with self.queue_lock:
                                self.current_task = None
                            self.events.put(('active', False))
                        self.save(job['id'], state)
                        self.events.put(('result', index, state))
                        self.log(f"{job['name']}: {state}")
                        if state != 'Complete' and state != 'Skipped: user requested':
                            try:
                                await page.screenshot(path=str(DATA / f'failure-{job["id"][:12]}.png'))
                            except Exception:
                                pass
                        if security:
                            self.log('Worker stopped: use Open Login to resolve login or verification.')
                            break
                        if state.startswith(('Skipped: user', 'Review: skipped')):
                            continue
                        deadline = time.monotonic() + delay
                        while not self.stop.is_set() and time.monotonic() < deadline:
                            await asyncio.sleep(min(.25, max(0, deadline-time.monotonic())))
                finally:
                    await context.close()
        except Exception as error:
            self.log(f'Worker error: {error}')
        finally:
            self.events.put(('done',))

    def request_stop(self):
        self.stop.set()
        self.status.set('Stopping after current group…')

    def poll(self):
        for _ in range(100):
            try:
                event = self.events.get_nowait()
            except queue.Empty:
                break
            if event[0] == 'log':
                line = f'[{time.strftime("%H:%M:%S")}] {event[1]}\n'
                with (DATA / 'worker.log').open('a', encoding='utf-8') as handle:
                    handle.write(line)
                self.console.configure(state='normal')
                self.console.insert('end', line)
                if int(self.console.index('end-1c').split('.')[0]) > 1000:
                    self.console.delete('1.0', '100.0')
                self.console.see('end')
                self.console.configure(state='disabled')
            elif event[0] == 'task':
                self.task.set(event[1])
            elif event[0] == 'active':
                self.skip_button.configure(state='normal' if event[1] else 'disabled')
            elif event[0] == 'result':
                self.table.set(str(event[1]), 'state', event[2])
                self.update_counts()
            elif event[0] == 'done':
                self.status.set('Stopped' if self.stop.is_set() else 'Worker finished • review results')
                self.task.set('No current task')
                for button in self.buttons:
                    button.configure(state='normal')
        self.root.after(100, self.poll)

    def close(self):
        if self.thread and self.thread.is_alive():
            self.request_stop()
            self.root.after(250, self.close)
        else:
            self.root.destroy()


async def smoke_browser():
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True, **browser_options())
        try:
            page = await browser.new_page()
            await page.set_content('<div contenteditable="true" role="textbox"></div>')
            await page.get_by_role('textbox').fill('Packaged browser check')
            assert await page.get_by_role('textbox').inner_text() == 'Packaged browser check'
        finally:
            await browser.close()


if __name__ == '__main__':
    if len(sys.argv) == 3 and sys.argv[1] == '--smoke-test':
        report = Path(sys.argv[2])
        try:
            app = App(tk.Tk())
            app.root.update()
            app.console_window.withdraw()
            app.show_console()
            app.root.update()
            app.root.destroy()
            asyncio.run(smoke_browser())
            report.write_text(json.dumps({'ok': True, 'data_path': str(DATA), 'browser': browser_options()}), encoding='utf-8')
        except Exception as error:
            report.write_text(json.dumps({'ok': False, 'error': str(error)}), encoding='utf-8')
            raise
    else:
        App(tk.Tk()).root.mainloop()

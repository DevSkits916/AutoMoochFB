from __future__ import annotations
import asyncio
import csv
import json
import os
import queue
import re
import threading
import time
import sys
import tkinter as tk
from pathlib import Path
from tkinter import ttk, filedialog, messagebox
from tkinter.scrolledtext import ScrolledText
from urllib.parse import quote, urljoin, urlparse
from playwright.async_api import async_playwright, TimeoutError as BrowserTimeout
from facebook import AutomationError, CHECKPOINT, CAPTCHA
from app import browser_options, smoke_browser, PROFILE
from browser_profile import profile_lease

ROOT = Path(__file__).resolve().parent
DATA = (Path(os.environ.get('LOCALAPPDATA') or Path.home()) / 'AutoMoochFacebookScanner') if getattr(sys, 'frozen', False) else ROOT / 'data' / 'scanner'
DATA.mkdir(parents=True, exist_ok=True)
FIELDS = ['Group Name', 'URL']


def group_url(value):
    parsed = urlparse(urljoin('https://www.facebook.com', str(value or '')))
    if parsed.scheme != 'https' or parsed.hostname not in {'www.facebook.com', 'facebook.com', 'm.facebook.com'}:
        return None
    match = re.fullmatch(r'/groups/([A-Za-z0-9_.-]+)/?', parsed.path)
    if not match or match[1].casefold() in {'feed', 'discover', 'joins', 'create', 'search', 'notifications'}:
        return None
    return 'https://www.facebook.com/groups/' + match[1]


def clean_group_name(value, url):
    name = ' '.join(str(value or '').split())
    # Facebook's avatar links use this accessibility label instead of a title.
    name = re.sub(r'^Profile photo of\s+', '', name, flags=re.IGNORECASE).strip()[:200]
    if not name or name.casefold() in {'join', 'join group', 'view group', 'visit', 'see more', 'profile photo of'}:
        return url.rsplit('/', 1)[-1]
    return name


def parse_links(links):
    found = {}
    priorities = {}
    for item in links:
        href = item.get('href', '') if isinstance(item, dict) else item
        url = group_url(href)
        if not url:
            continue
        text = item.get('text', '') if isinstance(item, dict) else ''
        name = clean_group_name(text, url)
        priority = 0 if name == url.rsplit('/', 1)[-1] else (1 if re.match(r'^Profile photo of\s+', ' '.join(text.split()), re.IGNORECASE) else 2)
        key = url.casefold()
        if key not in found or priority > priorities[key]:
            found[key] = {'name': name, 'url': url}
            priorities[key] = priority
    return list(found.values())


def export_csv(path, groups):
    if not groups:
        raise ValueError('Scan for groups before exporting.')
    with open(path, 'w', encoding='utf-8-sig', newline='') as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        for group in groups:
            writer.writerow({'Group Name': clean_group_name(group['name'], group['url']), 'URL': group['url']})
    return len(groups)


async def check_access(page, response=None):
    if response and response.status in (401, 403, 429):
        raise AutomationError('ACCESS_BLOCKED', 'Facebook blocked or limited access. Resolve it in the browser before restarting.', security=True)
    body = await page.locator('body').inner_text(timeout=10000)
    if CHECKPOINT.search(page.url + ' ' + body) or CAPTCHA.search(body):
        raise AutomationError('SECURITY_CHECK', 'Facebook requires a verification check.', security=True)
    if '/login' in page.url or await page.locator('input[name=email]').filter(visible=True).count():
        raise AutomationError('LOGIN_REQUIRED', 'Use Open Login to sign in to the scanner first.', security=True)


async def scan_query(page, query, limit, seconds, stop, on_results, log):
    log(f'Searching Facebook groups for: {query}')
    response = await page.goto('https://www.facebook.com/search/groups/?q=' + quote(query), wait_until='domcontentloaded', timeout=45000)
    await check_access(page, response)
    # Capture links inside the result area, excluding sidebar/navigation links.
    area = page.get_by_role('main').first
    try:
        await area.wait_for(state='visible', timeout=15000)
    except BrowserTimeout:
        raise RuntimeError('Facebook search results did not expose a main region. Inspect the saved screenshot and log.')
    anchors = area.locator('a[href]')
    seen = set()
    idle = 0
    deadline = time.monotonic() + seconds
    while not stop.is_set() and len(seen) < limit and time.monotonic() < deadline:
        links = await anchors.evaluate_all('(elements) => elements.map(e => ({href:e.href,text:(e.innerText || e.getAttribute(\"aria-label\") || \"\")}))')
        batch = []
        for item in parse_links(links):
            key = item['url'].casefold()
            if stop.is_set() or time.monotonic() >= deadline:
                break
            if key not in seen and len(seen) < limit:
                seen.add(key)
                batch.append(item)
        if batch:
            on_results(batch)
            log(f'Collected {len(seen)} groups for this search.')
            idle = 0
        else:
            idle += 1
        if len(seen) >= limit or idle >= 3:
            break
        previous = len(links)
        await page.evaluate('window.scrollTo(0, document.documentElement.scrollHeight)')
        try:
            await page.wait_for_function('(previous) => document.querySelectorAll("main a[href], [role=main] a[href]").length > previous', arg=previous, timeout=min(5000, max(1, int((deadline-time.monotonic())*1000))))
        except BrowserTimeout:
            pass
    if not seen:
        log('No group links found. Search may be empty, require login, or have a changed interface.')
    return len(seen)


class Scanner:
    def __init__(self, root):
        self.root = root
        self.events = queue.Queue()
        self.stop = threading.Event()
        self.thread = None
        self.results = {}
        self.total_text = tk.StringVar(value='Total groups collected: 0')
        self.status = tk.StringVar(value='Ready • enter search terms, one per line')
        self.headless = tk.BooleanVar()
        self.limit = tk.StringVar(value='100')
        self.seconds = tk.StringVar(value='30')
        root.title('Auto Mooch • Facebook Group Scanner')
        self.window_icon = tk.PhotoImage(file=str(ROOT / 'assets' / 'header.png'))
        root.iconphoto(True, self.window_icon)
        root.geometry('1100x800')
        root.minsize(900, 650)
        root.resizable(True, True)
        style = ttk.Style()
        style.theme_use('clam')
        style.configure('.', font=('Segoe UI', 11))
        frame = ttk.Frame(root, padding=18)
        frame.pack(fill='both', expand=True)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(3, weight=1)
        ttk.Label(frame, text='Facebook Group Scanner', font=('Segoe UI', 22, 'bold')).grid(row=0, column=0, sticky='w')
        ttk.Label(frame, textvariable=self.status).grid(row=1, column=0, sticky='w', pady=8)
        controls = ttk.Frame(frame)
        controls.grid(row=2, column=0, sticky='ew', pady=(0, 10))
        action_controls = ttk.Frame(controls)
        action_controls.pack(fill='x', pady=(0, 6))
        self.buttons = []
        for label, command in [('Open Login', lambda: self.launch(True)), ('Start Scan', lambda: self.launch(False))]:
            button = ttk.Button(action_controls, text=label, command=command)
            button.pack(side='left', padx=(0, 8))
            self.buttons.append(button)
        ttk.Button(action_controls, text='Stop', command=self.stop.set).pack(side='left', padx=4)
        ttk.Button(action_controls, text='Export CSV', command=self.export).pack(side='left', padx=4)
        self.clear_button = ttk.Button(action_controls, text='Clear Results', command=self.clear)
        self.clear_button.pack(side='left', padx=4)
        headless_options = ttk.Frame(controls)
        headless_options.pack(fill='x')
        ttk.Checkbutton(headless_options, text='Headless', variable=self.headless).pack(side='left')
        ttk.Label(headless_options, text='Warning: Log in and test the workflow before activating headless mode.', foreground='#c62828', wraplength=600, justify='left').pack(side='left', padx=(8, 0))
        panes = ttk.Panedwindow(frame, orient='horizontal')
        panes.grid(row=3, column=0, sticky='nsew')
        left = ttk.Frame(panes, padding=8)
        right = ttk.Frame(panes, padding=8)
        panes.add(left, weight=1)
        panes.add(right, weight=2)
        ttk.Label(left, text='Search terms (one per line)').pack(anchor='w')
        self.queries = ScrolledText(left, height=5, width=25)
        self.queries.pack(fill='both', expand=True, pady=6)
        options = ttk.Frame(left)
        options.pack(fill='x')
        ttk.Label(options, text='Maximum per search').grid(row=0, column=0, sticky='w')
        ttk.Entry(options, textvariable=self.limit, width=6).grid(row=0, column=1)
        ttk.Label(options, text='Seconds per search').grid(row=1, column=0, sticky='w')
        ttk.Entry(options, textvariable=self.seconds, width=6).grid(row=1, column=1)
        ttk.Label(right, textvariable=self.total_text, font=('Segoe UI', 11, 'bold')).pack(anchor='w', pady=(0, 8))
        results_frame = ttk.Frame(right)
        results_frame.pack(fill='both', expand=True)
        self.table = ttk.Treeview(results_frame, columns=('name', 'url'), show='headings', height=5)
        self.table.heading('name', text='Group Name')
        self.table.heading('url', text='URL')
        self.table.column('name', width=180, minwidth=100)
        self.table.column('url', width=380, minwidth=200)
        self.table.pack(side='left', fill='both', expand=True)
        scrollbar = ttk.Scrollbar(results_frame, command=self.table.yview)
        scrollbar.pack(side='right', fill='y')
        self.table.configure(yscrollcommand=scrollbar.set)
        ttk.Label(frame, text='Exports Group Name and URL only. Add a post column afterward for Auto Mooch.').grid(row=4, column=0, sticky='w', pady=10)
        self.console = ScrolledText(frame, height=6, state='disabled', bg='#17202b', fg='#dce7f3', font=('Consolas', 10))
        self.console.grid(row=5, column=0, sticky='ew')
        saved = DATA / 'groups.json'
        if saved.exists():
            try:
                self.add(json.loads(saved.read_text('utf-8')))
            except (ValueError, OSError, KeyError):
                self.events.put(('log', 'Saved results could not be loaded; the original file has been preserved.'))
        root.protocol('WM_DELETE_WINDOW', self.close)
        root.after(100, self.poll)

    def add(self, rows):
        for row in rows:
            key = row['url'].casefold()
            if key not in self.results and group_url(row['url']):
                row = dict(row, name=clean_group_name(row['name'], row['url']))
                self.results[key] = row
                self.table.insert('', 'end', values=(row['name'], row['url']))
        self.total_text.set(f'Total groups collected: {len(self.results):,}')

    def persist(self):
        temp = DATA / 'groups.tmp'
        temp.write_text(json.dumps(list(self.results.values()), indent=2), encoding='utf-8')
        temp.replace(DATA / 'groups.json')

    def clear(self):
        if self.thread and self.thread.is_alive():
            return
        self.results.clear()
        self.total_text.set('Total groups collected: 0')
        self.table.delete(*self.table.get_children())
        self.persist()
        self.status.set('Results cleared')

    def export(self):
        if not self.results:
            messagebox.showinfo('Export', 'Scan for groups first.')
            return
        path = filedialog.asksaveasfilename(defaultextension='.csv', initialfile='facebook-groups.csv', filetypes=[('CSV files', '*.csv')])
        if path:
            try:
                count = export_csv(path, list(self.results.values()))
                self.status.set(f'Exported {count} groups • group names and URLs only')
            except (OSError, ValueError) as error:
                messagebox.showerror('Export failed', str(error))

    def launch(self, login):
        if self.thread and self.thread.is_alive():
            return
        try:
            limit = int(self.limit.get())
            seconds = int(self.seconds.get())
            if not 1 <= limit <= 500 or not 10 <= seconds <= 600:
                raise ValueError('Use a maximum of 1–500 and a search duration of 10–600 seconds.')
            terms = list(dict.fromkeys(t.strip() for t in self.queries.get('1.0', 'end').splitlines() if t.strip()))
            if not login and not terms:
                raise ValueError('Enter at least one search term.')
        except ValueError as error:
            messagebox.showerror('Settings', str(error))
            return
        self.stop.clear()
        headless = False if login else self.headless.get()
        for button in self.buttons + [self.clear_button]:
            button.configure(state='disabled')
        self.status.set('Login browser running' if login else 'Scanning…')
        self.thread = threading.Thread(target=lambda: asyncio.run(self.run(login, headless, terms, limit, seconds)), daemon=True)
        self.thread.start()

    async def run(self, login, headless, terms, limit, seconds):
        log = lambda text: self.events.put(('log', text))
        try:
            async with async_playwright() as pw, profile_lease(PROFILE):
                context = await pw.chromium.launch_persistent_context(str(PROFILE), headless=headless, viewport={'width': 1400, 'height': 950}, **browser_options())
                try:
                    page = context.pages[0] if context.pages else await context.new_page()
                    if login:
                        await page.goto('https://www.facebook.com/', wait_until='domcontentloaded', timeout=45000)
                        log('Sign in, then press Stop to save the shared session. Auto Mooch and the scanner use the same browser profile.')
                        while not self.stop.is_set() and context.pages:
                            await asyncio.sleep(.25)
                    else:
                        for term in terms:
                            if self.stop.is_set():
                                break
                            await scan_query(page, term, limit, seconds, self.stop, lambda rows: self.events.put(('results', rows)), log)
                except Exception:
                    try:
                        await page.screenshot(path=str(DATA / 'last-failure.png'))
                    except Exception:
                        pass
                    raise
                finally:
                    await context.close()
        except Exception as error:
            log(f'Scan stopped: {error}')
            self.events.put(('error', str(error)))
        finally:
            self.events.put(('done',))

    def poll(self):
        for _ in range(100):
            try:
                event = self.events.get_nowait()
            except queue.Empty:
                break
            if event[0] == 'results':
                self.add(event[1])
                self.persist()
                self.status.set(f'Scanning • {len(self.results)} unique groups collected')
            elif event[0] == 'log':
                line = f'[{time.strftime("%H:%M:%S")}] {event[1]}\n'
                with (DATA / 'scanner.log').open('a', encoding='utf-8') as handle:
                    handle.write(line)
                self.console.configure(state='normal')
                self.console.insert('end', line)
                self.console.see('end')
                self.console.configure(state='disabled')
            elif event[0] == 'error':
                self.status.set('Scan stopped • see console; collected results are retained')
            elif event[0] == 'done':
                if not self.status.get().startswith('Scan stopped'):
                    self.status.set(f'Finished • {len(self.results)} unique groups • ready to export')
                for button in self.buttons + [self.clear_button]:
                    button.configure(state='normal')
        self.root.after(100, self.poll)

    def close(self):
        if self.thread and self.thread.is_alive():
            self.stop.set()
            self.root.after(250, self.close)
        else:
            self.root.destroy()


if __name__ == '__main__':
    if len(sys.argv) == 3 and sys.argv[1] == '--smoke-test':
        report = Path(sys.argv[2])
        try:
            scanner = Scanner(tk.Tk())
            scanner.root.update()
            scanner.root.destroy()
            asyncio.run(smoke_browser())
            report.write_text(json.dumps({'ok': True, 'data_path': str(DATA), 'browser': browser_options()}), encoding='utf-8')
        except Exception as error:
            report.write_text(json.dumps({'ok': False, 'error': str(error)}), encoding='utf-8')
            raise
    else:
        Scanner(tk.Tk()).root.mainloop()

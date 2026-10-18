import asyncio
import csv
import tempfile
import threading
import tkinter as tk
import unittest
from pathlib import Path
from playwright.async_api import async_playwright
from app import load_csv
from facebook_group_scanner import Scanner, parse_links, export_csv, scan_query, group_url


class ScannerChecks(unittest.TestCase):
    def test_profile_photo_names(self):
        rows = parse_links([
            {'href': '/groups/123/', 'text': 'Profile photo of Dog Owners'},
            {'href': '/groups/123/', 'text': 'Dog Owners Community'},
            {'href': '/groups/456/', 'text': '  PROFILE photo of  Family Love 💕 '},
            {'href': '/groups/789/', 'text': 'Friends of Profile photo of Dogs'},
        ])
        self.assertEqual([row['name'] for row in rows], ['Dog Owners Community', 'Family Love 💕', 'Friends of Profile photo of Dogs'])
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'groups.csv'
            export_csv(path, [{'name': 'Profile photo of Saved Group', 'url': 'https://www.facebook.com/groups/123'}])
            with path.open(encoding='utf-8-sig', newline='') as handle:
                self.assertEqual(next(csv.DictReader(handle))['Group Name'], 'Saved Group')

    def test_metadata_only_export(self):
        rows = parse_links([
            {'href': '/groups/123/?ref=search', 'text': 'Local Dog Owners'},
            {'href': '/groups/123/', 'text': 'Join group'},
            {'href': '/groups/456/', 'text': 'Community Support'},
            {'href': '/groups/123/posts/456/', 'text': 'A post'},
            {'href': '/groups/feed/', 'text': 'Feed'},
            {'href': 'https://evil.org/groups/123/', 'text': 'Not Facebook'},
        ])
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]['name'], 'Local Dog Owners')
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'groups.csv'
            export_csv(path, rows)
            with path.open(encoding='utf-8-sig', newline='') as handle:
                reader = csv.DictReader(handle)
                self.assertEqual(reader.fieldnames, ['Group Name', 'URL'])
                exported = list(reader)
            self.assertEqual(exported[0]['URL'], 'https://www.facebook.com/groups/123')
            with path.open('w', encoding='utf-8-sig', newline='') as handle:
                writer = csv.DictWriter(handle, fieldnames=['Group Name', 'URL', 'post'])
                writer.writeheader()
                writer.writerows(dict(row, post='My user-supplied message') for row in exported)
            self.assertEqual(len(load_csv(path)), 2)

    def test_gui(self):
        root = tk.Tk()
        root.withdraw()
        try:
            scanner = Scanner(root)
            root.update()
            self.assertFalse(hasattr(scanner, 'post'))
            self.assertFalse(hasattr(scanner, 'title'))
            self.assertEqual(root.resizable(), (1, 1))
            self.assertEqual(scanner.seconds.get(), '30')
            scanner.add([{'name': 'Profile photo of Previously Saved Group', 'url': 'https://www.facebook.com/groups/scanner-regression-fixture'}])
            self.assertEqual(scanner.results['https://www.facebook.com/groups/scanner-regression-fixture']['name'], 'Previously Saved Group')
            self.assertEqual(scanner.table.item(scanner.table.get_children()[-1], 'values')[0], 'Previously Saved Group')
        finally:
            root.destroy()

    def test_browser_search_collection(self):
        async def check():
            async with async_playwright() as pw:
                browser = await pw.chromium.launch(headless=True)
                try:
                    context = await browser.new_context()
                    page = await context.new_page()
                    html = '<aside><a href="/groups/999/">Sidebar Group</a></aside><main><a href="/groups/123/" aria-label="Profile photo of Dog Owners"><img alt=""></a><a href="/groups/123/">Dog Owners</a><a href="/groups/456/">Community Support</a><a href="/groups/123/posts/555/">Post</a></main>'
                    await page.context.route('https://www.facebook.com/search/groups/**', lambda route: route.fulfill(body=html, content_type='text/html'))
                    group_visits = []
                    async def unexpected_group_visit(route):
                        group_visits.append(route.request.url)
                        await route.abort()
                    await page.context.route('https://www.facebook.com/groups/*', unexpected_group_visit)
                    rows = []
                    count = await scan_query(page, 'dogs', 2, 10, threading.Event(), rows.extend, lambda _: None)
                    self.assertEqual(count, 2)
                    self.assertEqual(group_visits, [])
                    self.assertEqual(rows[0]['name'], 'Dog Owners')
                    self.assertFalse(any('999' in row['url'] for row in rows))
                finally:
                    await browser.close()
        asyncio.run(check())


if __name__ == '__main__':
    unittest.main()

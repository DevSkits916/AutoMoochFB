import asyncio
import tempfile
import unittest
from unittest.mock import patch
from unittest.mock import Mock
from unittest.mock import AsyncMock
from pathlib import Path
import tkinter as tk
from playwright.async_api import async_playwright
from app import App, load_csv
from facebook import answer_membership_form, membership_answer, enter_post_text, prepare_composer, post_to_facebook, AutomationError


class Checks(unittest.TestCase):
    def setUp(self):
        # Restoring a user's last CSV must not populate test queues or read their results.
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        data_patch = patch('app.DATA', Path(folder.name))
        data_patch.start()
        self.addCleanup(data_patch.stop)

    def test_posting_cooldown_counts_down_and_resumes_remaining(self):
        with tempfile.TemporaryDirectory() as folder, patch('app.DATA', Path(folder)):
            root = tk.Tk()
            root.withdraw()
            try:
                app = App(root)
                app.jobs = [{'id': str(i)} for i in range(4)]
                app.history = {'0': 'Complete', '1': 'Review: POSTING_RESTRICTED', '3': 'Review: uncertain submission'}
                app.pending = [2, 3]
                app.thread = Mock()
                app.thread.is_alive.return_value = True
                app.events.put(('alert', 'Posting restricted'))
                with patch('app.time.monotonic', return_value=100), patch.object(app, 'alert_posting_restriction'):
                    app.poll()
                self.assertEqual(app.cooldown_deadline, 10900)
                self.assertIn('03:00:00', app.cooldown_text.get())
                self.assertTrue(app.cooldown_label.winfo_manager())
                with patch('app.time.monotonic', return_value=3700):
                    app.update_posting_cooldown()
                self.assertIn('02:00:00', app.cooldown_text.get())
                with patch('app.time.monotonic', return_value=10900), patch.object(app, 'launch') as launch:
                    app.update_posting_cooldown()
                    launch.assert_not_called()
                    app.thread.is_alive.return_value = False
                    app.update_posting_cooldown()
                    launch.assert_called_once_with(False, resume_indices=[2])
                    app.update_posting_cooldown()
                    launch.assert_called_once()
                self.assertIsNone(app.cooldown_deadline)
            finally:
                root.destroy()

    def test_stop_cancels_posting_cooldown_and_delayed_alert(self):
        with tempfile.TemporaryDirectory() as folder, patch('app.DATA', Path(folder)):
            root = tk.Tk()
            root.withdraw()
            try:
                app = App(root)
                app.start_posting_cooldown()
                app.request_stop()
                self.assertIsNone(app.cooldown_deadline)
                self.assertFalse(app.cooldown_label.winfo_manager())
                with patch.object(app, 'launch') as launch:
                    app.start_posting_cooldown()
                    app.update_posting_cooldown()
                    launch.assert_not_called()
                self.assertIsNone(app.cooldown_deadline)
                self.assertIn('cancelled', app.status.get())
            finally:
                root.destroy()

    def test_cooldown_without_remaining_groups_does_not_restart(self):
        with tempfile.TemporaryDirectory() as folder, patch('app.DATA', Path(folder)):
            root = tk.Tk()
            root.withdraw()
            try:
                app = App(root)
                with patch('app.time.monotonic', return_value=10):
                    app.start_posting_cooldown()
                with patch('app.time.monotonic', return_value=10810), patch.object(app, 'launch') as launch:
                    app.update_posting_cooldown()
                    launch.assert_not_called()
                self.assertIn('no remaining groups', app.status.get())
            finally:
                root.destroy()

    def test_last_csv_restored_after_reopen(self):
        with tempfile.TemporaryDirectory() as folder, patch('app.DATA', Path(folder)):
            path = Path(folder) / 'original.csv'
            path.write_text('Group Name,URL,post,Extra\nDogs,https://www.facebook.com/groups/123,"Hello\nLoki",Keep\n', encoding='utf-8-sig')
            root = tk.Tk()
            root.withdraw()
            try:
                app = App(root)
                with patch('app.filedialog.askopenfilename', return_value=str(path)):
                    app.import_csv()
                originals = app.jobs
                app.save(app.jobs[0]['id'], 'Complete')
                path.write_text('URL,post\ninvalid,Test\n', encoding='utf-8')
                with patch('app.filedialog.askopenfilename', return_value=str(path)), patch('app.messagebox.showerror'):
                    app.import_csv()
                self.assertEqual(app.jobs, originals)
                path.unlink()
            finally:
                root.destroy()
            root = tk.Tk()
            root.withdraw()
            try:
                reopened = App(root)
                self.assertEqual(reopened.jobs, originals)
                self.assertEqual(reopened.table.set('0', 'state'), 'Complete')
                self.assertEqual(reopened.progress_percentage.get(), '100%')
                self.assertIsNone(reopened.thread)
            finally:
                root.destroy()

    def test_corrupt_last_csv_does_not_block_startup(self):
        with tempfile.TemporaryDirectory() as folder, patch('app.DATA', Path(folder)):
            saved = Path(folder) / 'last-import.csv'
            saved.write_text('Invalid CSV', encoding='utf-8')
            root = tk.Tk()
            root.withdraw()
            try:
                app = App(root)
                self.assertEqual(app.jobs, [])
                self.assertIn('Could not restore', app.status.get())
                self.assertEqual(saved.read_text('utf-8'), 'Invalid CSV')
            finally:
                root.destroy()

    def test_csv(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'input.csv'
            path.write_text('\ufeffURL,post\nhttps://www.facebook.com/groups/123/,"line one\nline two"\n', encoding='utf-8')
            jobs = load_csv(path)
            self.assertEqual(jobs[0]['postText'].replace('\r\n', '\n'), 'line one\nline two')
            path.write_text('URL,post\nhttps://facebook.com.evil.org/groups/123,hello\n')
            with self.assertRaises(ValueError):
                load_csv(path)

    def test_gui(self):
        root = tk.Tk()
        root.withdraw()
        try:
            app = App(root)
            root.update()
            self.assertIn('Complete 0', app.counts.get())
        finally:
            root.destroy()

    def test_export_remaining_roundtrip(self):
        root = tk.Tk()
        root.withdraw()
        try:
            app = App(root)
            app.jobs = load_csv(Path(__file__).parent / 'sample CSV.csv')[:5]
            app.history = {}
            for index, state in enumerate(['Complete', 'Queued', 'Running', 'Skipped: user requested', 'Queued']):
                app.table.insert('', 'end', iid=str(index), values=(str(index), state))
            app.table.move('4', '', 0)
            self.assertEqual(app.remaining_jobs(), [app.jobs[4], app.jobs[1]])
            with tempfile.TemporaryDirectory() as folder:
                output = Path(folder) / 'remaining.csv'
                with patch('app.filedialog.asksaveasfilename', return_value=str(output)), patch('app.messagebox.showinfo'):
                    app.export_remaining()
                exported = load_csv(output)
                self.assertEqual([j['source_row'] for j in exported], [app.jobs[i]['source_row'] for i in (4, 1)])
                self.assertEqual(exported[0]['source_headers'], app.jobs[4]['source_headers'])
            app.thread = Mock()
            app.thread.is_alive.return_value = True
            app.pending = [4, 0, 1]
            app.history[app.jobs[0]['id']] = 'Complete'
            self.assertEqual(app.remaining_jobs(), [app.jobs[4], app.jobs[1]])
        finally:
            root.destroy()

    def test_specific_answer(self):
        self.assertEqual(membership_answer('Why join this group?', [{'question': 'join', 'answer': 'A'}, {'question': 'Why join', 'answer': 'B'}]), 'B')

    def test_pending_shuffle(self):
        root = tk.Tk()
        root.withdraw()
        try:
            app = App(root)
            for index, state in enumerate(['Complete', 'Queued', 'Review: uncertain', 'Queued']):
                app.table.insert('', 'end', iid=str(index), values=(str(index), state))
            with patch('app.random.shuffle', side_effect=lambda items: items.reverse()):
                app.shuffle_pending()
            self.assertEqual(app.table.get_children(), ('0', '2', '3', '1'))
            self.assertEqual(app.table.set('0', 'state'), 'Complete')
            self.assertEqual(root.title(), 'Auto Mooch')
        finally:
            root.destroy()

    def test_skip_cancels_current_only(self):
        async def check():
            root = tk.Tk()
            root.withdraw()
            try:
                app = App(root)
                current = asyncio.create_task(asyncio.sleep(60))
                next_job = asyncio.create_task(asyncio.sleep(60))
                app.current_task = current
                app.worker_loop = asyncio.get_running_loop()
                app.skip_current()
                await asyncio.sleep(0)
                await asyncio.sleep(0)
                self.assertTrue(current.cancelled())
                self.assertFalse(next_job.cancelled())
                next_job.cancel()
                await asyncio.gather(current, next_job, return_exceptions=True)
            finally:
                root.destroy()
        asyncio.run(check())

    def test_browser(self):
        asyncio.run(self.browser())

    def test_membership_timeout_still_posts(self):
        asyncio.run(self.timeout_post())

    def test_posting_limit_stops_worker(self):
        asyncio.run(self.posting_limit())

    def test_browser_closed_preserves_queue_and_submission_review(self):
        async def check():
            async with async_playwright() as pw:
                browser = await pw.chromium.launch(headless=True)
                try:
                    for submitted in (False, True):
                        root = tk.Tk()
                        root.withdraw()
                        try:
                            app = App(root)
                            app.history = {}
                            app.jobs = [dict(id=str(i), name=f'Group {i}', groupUrl=f'https://www.facebook.com/groups/{i}', postText='Test') for i in (123, 456)]
                            app.pending = [0, 1]
                            context = await browser.new_context()
                            await context.new_page()
                            fake_pw = Mock()
                            fake_pw.chromium.launch_persistent_context = AsyncMock(return_value=context)
                            manager = Mock()
                            manager.__aenter__ = AsyncMock(return_value=fake_pw)
                            manager.__aexit__ = AsyncMock(return_value=None)
                            async def close_browser(page, job, on_step, **kwargs):
                                if submitted:
                                    on_step('Submitting post...')
                                await context.close()
                                raise RuntimeError('Browser closed during task')
                            with patch('app.async_playwright', return_value=manager), patch('app.PROFILE', app.history_path.parent / 'profile'), patch('app.post_to_facebook', side_effect=close_browser) as post:
                                await app.run(False, True, 0, [])
                            post.assert_called_once()
                            self.assertTrue(app.stop.is_set())
                            self.assertEqual(app.pending, [1] if submitted else [0, 1])
                            self.assertTrue(app.history['123'].startswith('Review') if submitted else app.history['123'] == 'Queued')
                            self.assertNotIn('456', app.history)
                            self.assertIn('browser closed', app.halt_reason)
                        finally:
                            root.destroy()
                finally:
                    await browser.close()
        asyncio.run(check())

    async def posting_limit(self):
        root = tk.Tk()
        root.withdraw()
        try:
            app = App(root)
            app.history = {}
            app.jobs = [dict(id=str(i), name=f'Group {i}', groupUrl=f'https://www.facebook.com/groups/{i}', postText='Test post') for i in (123, 456)]
            app.pending = [0, 1]
            async with async_playwright() as pw:
                browser = await pw.chromium.launch(headless=True)
                try:
                    context = await browser.new_context()
                    page = await context.new_page()
                    html = '''<button onclick="document.querySelector('[role=dialog]').style.display='block'">Write something</button><div role="dialog" style="display:none"><div role="textbox" contenteditable="true"></div><button onclick="setTimeout(()=>document.querySelector('#error').textContent='We limit how often you can post, comment or do other things in a given amount of time in order to help protect the community from spam. You can try again later.', 100)">Post</button><div id="error" style="color:red"></div></div>'''
                    navigations = []
                    async def route_handler(route):
                        navigations.append(route.request.url)
                        await route.fulfill(body=html, content_type='text/html')
                    await context.route('https://www.facebook.com/groups/*', route_handler)
                    fake_pw = Mock()
                    fake_pw.chromium.launch_persistent_context = AsyncMock(return_value=context)
                    manager = Mock()
                    manager.__aenter__ = AsyncMock(return_value=fake_pw)
                    manager.__aexit__ = AsyncMock(return_value=None)
                    with tempfile.TemporaryDirectory() as folder:
                        app.history_path = Path(folder) / 'progress.json'
                        with patch('app.async_playwright', return_value=manager), patch('app.PROFILE', Path(folder) / 'profile'), patch('app.DATA', Path(folder)), patch.object(page, 'screenshot', new=AsyncMock()):
                            await app.run(False, True, 0, [], join_groups=False)
                    self.assertTrue(app.stop.is_set())
                    self.assertEqual(app.pending, [1])
                    self.assertEqual(navigations, [app.jobs[0]['groupUrl']])
                    self.assertEqual(app.history['123'], 'Review: POSTING_RESTRICTED')
                    events = list(app.events.queue)
                    self.assertTrue(any(event[0] == 'alert' for event in events))
                    self.assertIn('posting limit', app.halt_reason)
                finally:
                    await browser.close()
        finally:
            root.destroy()

    async def timeout_post(self):
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                html = '''<button onclick="document.querySelector('[role=dialog]').style.display='block'">Write something</button><div role="dialog" style="display:none"><div role="textbox" contenteditable="true"></div><button onclick="document.body.dataset.posted=document.querySelector('[contenteditable]').innerText; document.querySelector('[role=dialog]').remove()">Post</button></div>'''
                await page.route('https://www.facebook.com/groups/123', lambda route: route.fulfill(body=html, content_type='text/html'))
                job = {'groupUrl': 'https://www.facebook.com/groups/123', 'postText': 'Timeout fallback post'}
                async def stalled_check(*args):
                    await asyncio.sleep(60)
                steps = []
                with patch('facebook._join_group', side_effect=stalled_check):
                    result = await post_to_facebook(page, job, steps.append, join_before_post=True, composer_timeout_ms=1000)
                self.assertEqual(result['status'], 'posted')
                self.assertEqual(await page.locator('body').get_attribute('data-posted'), job['postText'])
                self.assertTrue(any('Membership check timed out' in step for step in steps))
                with patch('facebook.join_group', new=AsyncMock(side_effect=AutomationError('JOIN_PENDING', 'Approval pending'))):
                    result = await post_to_facebook(page, job, join_before_post=True)
                self.assertEqual(result['status'], 'posted')
                self.assertEqual(await page.locator('body').get_attribute('data-posted'), job['postText'])
            finally:
                await browser.close()

    async def browser(self):
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            page = await browser.new_page()
            await page.set_content('''<div role="dialog"><label>Why join?<input type="text"></label><label><input type="checkbox">I agree to group rules</label><button onclick="document.querySelector('[role=dialog]').remove()">Submit</button></div>''')
            await answer_membership_form(page.get_by_role('dialog'), [{'question': 'Why join?', 'answer': 'Community'}], True, lambda text: None)
            self.assertEqual(await page.get_by_role('dialog').count(), 0)
            await page.set_content('''<button onclick="document.querySelector('[role=dialog]').style.display='block'">Write something</button><div role="dialog" style="display:none"><div role="textbox" contenteditable="true"></div><button>Post</button></div>''')
            editor = await prepare_composer(page, lambda text: None, 3000)
            await enter_post_text(page, editor, 'A real multiline\npost')
            self.assertEqual(await editor.inner_text(), 'A real multiline\npost')
            await browser.close()


if __name__ == '__main__':
    unittest.main()

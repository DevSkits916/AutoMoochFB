import asyncio
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path
import tkinter as tk
from playwright.async_api import async_playwright
from app import App, load_csv
from facebook import answer_membership_form, membership_answer, enter_post_text, prepare_composer


class Checks(unittest.TestCase):
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
        app = App(root)
        root.update()
        self.assertIn('Complete 0', app.counts.get())
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

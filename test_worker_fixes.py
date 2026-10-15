import asyncio
import tkinter as tk
import unittest
from playwright.async_api import async_playwright
from app import App
from facebook import answer_membership_form, question_text, post_to_facebook, AutomationError, check_group_availability


class FixChecks(unittest.TestCase):
    def test_pages_cannot_post_notice(self):
        async def check():
            async with async_playwright() as pw:
                browser = await pw.chromium.launch(headless=True)
                try:
                    page = await browser.new_page()
                    for text in ["This group doesn't allow Pages to join.", "Pages can't post in this group.", "Switch to your main profile to post in this group."]:
                        await page.set_content('<div id="notice"></div>')
                        await page.locator('#notice').evaluate('(e, value) => e.textContent=value', text)
                        with self.assertRaises(AutomationError) as raised:
                            await check_group_availability(page)
                        self.assertEqual(raised.exception.code, 'GROUP_UNAVAILABLE')
                    await page.set_content('<article>This group is paused</article>')
                    await check_group_availability(page)
                finally:
                    await browser.close()
        asyncio.run(check())
    def test_detach_and_reattach_console(self):
        root = tk.Tk()
        root.withdraw()
        try:
            app = App(root)
            self.assertEqual(app.delay.get(), '8')
            app.log('Before detachment')
            app.poll()
            app.toggle_console()
            root.update()
            self.assertIsNotNone(app.console_window)
            app.log('During detachment')
            app.poll()
            app.toggle_console()
            root.update()
            self.assertIsNone(app.console_window)
            self.assertIn('Before detachment', app.console.get('1.0', 'end'))
            self.assertIn('During detachment', app.console.get('1.0', 'end'))
        finally:
            root.destroy()

    def test_question_labels_and_options(self):
        async def check():
            async with async_playwright() as pw:
                browser = await pw.chromium.launch(headless=True)
                try:
                    page = await browser.new_page()
                    await page.set_content('''<div role="dialog"><section><div>What state do live in?</div><div><textarea aria-label="Write an answer..." placeholder="Write an answer..."></textarea></div></section><section><div>Are you an Australian?</div><label><input type="checkbox">Yes</label><label><input type="checkbox">No</label></section><label><input type="checkbox">I agree to the group rules</label><button onclick="window.answer=document.querySelector('textarea').value; window.no=document.querySelectorAll('input')[1].checked; this.parentElement.remove()">Submit</button></div>''')
                    self.assertEqual(await question_text(page.locator('textarea')), 'What state do live in?')
                    await answer_membership_form(page.get_by_role('dialog'), [{'question': 'what state do live in', 'answer': 'California'}, {'question': 'are you an Australian', 'answer': 'No'}], True, lambda _: None)
                    self.assertEqual(await page.evaluate('window.answer'), 'California')
                    self.assertTrue(await page.evaluate('window.no'))
                finally:
                    await browser.close()
        asyncio.run(check())

    def test_paused_group_and_feed_security_text(self):
        async def check():
            async with async_playwright() as pw:
                browser = await pw.chromium.launch(headless=True)
                try:
                    page = await browser.new_page()
                    html = '<button>Join group</button><article>Oggy is in his security check point...</article><h2>This group is paused</h2>'
                    await page.route('https://www.facebook.com/groups/123', lambda route: route.fulfill(body=html, content_type='text/html'))
                    with self.assertRaises(AutomationError) as raised:
                        await post_to_facebook(page, {'groupUrl': 'https://www.facebook.com/groups/123', 'postText': 'Test'})
                    self.assertEqual(raised.exception.code, 'GROUP_UNAVAILABLE')
                    self.assertFalse(raised.exception.security)
                finally:
                    await browser.close()
        asyncio.run(check())

    def test_native_radio_question(self):
        async def check():
            async with async_playwright() as pw:
                browser = await pw.chromium.launch(headless=True)
                try:
                    page = await browser.new_page()
                    await page.set_content('''<div role="dialog"><section><h3>From where are you joining?</h3><label><input type="radio" name="country">United States</label><label><input type="radio" name="country">Other country</label></section><button onclick="window.country=document.querySelector('input').checked; this.parentElement.remove()">Submit</button></div>''')
                    await answer_membership_form(page.get_by_role('dialog'), [{'question': 'from where are you joining', 'answer': 'United States'}], True, lambda _: None)
                    self.assertTrue(await page.evaluate('window.country'))
                finally:
                    await browser.close()
        asyncio.run(check())


if __name__ == '__main__':
    unittest.main()

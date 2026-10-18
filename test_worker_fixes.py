import asyncio
import tempfile
import tkinter as tk
import unittest
from pathlib import Path
from unittest.mock import patch
from playwright.async_api import async_playwright
from app import App
from facebook import answer_membership_form, question_text, post_to_facebook, AutomationError, check_group_availability, membership_is_pending, join_group


class FixChecks(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        data_patch = patch('app.DATA', Path(folder.name))
        data_patch.start()
        self.addCleanup(data_patch.stop)

    def test_membership_blocks_skip_without_composer_fallback(self):
        async def check():
            async with async_playwright() as pw:
                browser = await pw.chromium.launch(headless=True)
                try:
                    page = await browser.new_page()
                    composer = '''<button onclick="document.querySelector('#composer').style.display='block'">Write something</button><div id="composer" role="dialog" style="display:none"><h2>Create post</h2><div role="textbox" contenteditable="true"></div><button onclick="window.posted=document.querySelector('[contenteditable]').innerText; this.parentElement.remove()">Post</button></div>'''
                    cases = [
                        '<div>Your membership is pending</div><button>Cancel request</button>',
                        '<button disabled>Join group</button>',
                        "<p>This group doesn't allow Pages to join.</p>",
                        '''<button onclick="document.querySelector('#questions').style.display='block'">Join group</button><div id="questions" role="dialog" style="display:none"><label>An unknown membership question<textarea></textarea></label><button onclick="this.parentElement.remove()">Close</button><button onclick="window.membershipSubmitted=true">Submit</button></div>''',
                    ]
                    for prefix in cases:
                        for join in (True, False):
                            html = prefix + composer
                            await page.route('https://www.facebook.com/groups/123', lambda route: route.fulfill(body=html, content_type='text/html'))
                            steps = []
                            job = {'groupUrl': 'https://www.facebook.com/groups/123', 'postText': 'Exact queued text'}
                            blocked = join or prefix == cases[0] or prefix == cases[2]
                            if blocked:
                                with self.assertRaises(AutomationError) as raised:
                                    await post_to_facebook(page, job, steps.append, join_before_post=join, composer_timeout_ms=1500)
                                self.assertIn(raised.exception.code, {'JOIN_PENDING', 'GROUP_UNAVAILABLE'})
                                self.assertFalse(any('Finding composer' in step for step in steps))
                                self.assertFalse(await page.evaluate('Boolean(window.posted)'))
                            else:
                                result = await post_to_facebook(page, job, steps.append, join_before_post=join, composer_timeout_ms=1500)
                                self.assertEqual(result['status'], 'posted')
                                self.assertEqual(await page.evaluate('window.posted'), 'Exact queued text')
                            self.assertFalse(await page.evaluate('Boolean(window.membershipSubmitted)'))
                            await page.unroute('https://www.facebook.com/groups/123')
                    opened = '''<div role="dialog"><h2>Create post</h2><div role="textbox" contenteditable="true"></div><button onclick="window.posted=document.querySelector('[contenteditable]').innerText; this.parentElement.remove()">Post</button></div>'''
                    await page.route('https://www.facebook.com/groups/123', lambda route: route.fulfill(body=opened, content_type='text/html'))
                    result = await post_to_facebook(page, {'groupUrl': 'https://www.facebook.com/groups/123', 'postText': 'Already open composer'}, composer_timeout_ms=1000)
                    self.assertEqual(result['status'], 'posted')
                    self.assertEqual(await page.evaluate('window.posted'), 'Already open composer')
                finally:
                    await browser.close()
        asyncio.run(check())

    def test_pending_banner_skips_before_related_groups(self):
        async def check():
            async with async_playwright() as pw:
                browser = await pw.chromium.launch(headless=True)
                try:
                    page = await browser.new_page()
                    html = '''<main><h1>Current Group</h1><div>Your membership is pending</div><section><h2>Related groups</h2><button onclick="window.wrongGroup=true">Join group</button></section></main>'''
                    await page.route('https://www.facebook.com/groups/123', lambda route: route.fulfill(body=html, content_type='text/html'))
                    for join in (False, True):
                        with self.assertRaises(AutomationError) as raised:
                            await post_to_facebook(page, {'groupUrl': 'https://www.facebook.com/groups/123', 'postText': 'Test'}, join_before_post=join, composer_timeout_ms=500)
                        self.assertEqual(raised.exception.code, 'JOIN_PENDING')
                        self.assertFalse(await page.evaluate('Boolean(window.wrongGroup)'))
                    await page.set_content('<article><p>Your membership is pending</p><button>Cancel request</button></article>')
                    self.assertFalse(await membership_is_pending(page))
                finally:
                    await browser.close()
        asyncio.run(check())

    def test_pending_membership_questions_use_saved_answers(self):
        async def check():
            async with async_playwright() as pw:
                browser = await pw.chromium.launch(headless=True)
                try:
                    page = await browser.new_page()
                    html = '''<div>Your membership is pending</div><button onclick="document.querySelector('[role=dialog]').style.display='block'">Answer questions</button><div role="dialog" style="display:none"><label>Where do you live?<textarea></textarea></label><button onclick="window.answer=document.querySelector('textarea').value; this.parentElement.remove(); document.querySelector('button').remove()">Submit</button></div><section><h2>Related groups</h2><button onclick="window.wrongGroup=true">Join group</button></section>'''
                    await page.route('https://www.facebook.com/groups/123', lambda route: route.fulfill(body=html, content_type='text/html'))
                    steps = []
                    job = {'groupUrl': 'https://www.facebook.com/groups/123', 'postText': 'Test'}
                    with self.assertRaises(AutomationError) as raised:
                        await post_to_facebook(page, job, steps.append, join_before_post=True, membership_answers=[{'question': 'where do you live', 'answer': 'Sacramento'}], composer_timeout_ms=1000)
                    self.assertEqual(raised.exception.code, 'JOIN_PENDING')
                    self.assertEqual(await page.evaluate('window.answer'), 'Sacramento')
                    self.assertFalse(await page.evaluate('Boolean(window.wrongGroup)'))
                    with self.assertRaises(AutomationError) as raised:
                        await post_to_facebook(page, job, steps.append, join_before_post=True, membership_answers=[], composer_timeout_ms=1000)
                    self.assertEqual(raised.exception.code, 'JOIN_PENDING')
                    self.assertIsNone(await page.evaluate('window.answer'))
                    self.assertTrue(any('No saved answer' in step for step in steps))
                finally:
                    await browser.close()
        asyncio.run(check())

    def test_disabled_join_is_skipped(self):
        async def check():
            async with async_playwright() as pw:
                browser = await pw.chromium.launch(headless=True)
                try:
                    page = await browser.new_page()
                    await page.set_content('<div role="button" aria-label="Join group" aria-disabled="true">Join group</div>')
                    with self.assertRaises(AutomationError) as raised:
                        await join_group(page, lambda _: None, [], True, 1000)
                    self.assertEqual(raised.exception.code, 'GROUP_UNAVAILABLE')
                finally:
                    await browser.close()
        asyncio.run(check())

    def test_joined_group_ignores_recommended_join_buttons(self):
        async def check():
            async with async_playwright() as pw:
                browser = await pw.chromium.launch(headless=True)
                try:
                    page = await browser.new_page()
                    await page.set_content('''<h1>Current group</h1><button>Joined</button><section><h2>Related groups</h2><button onclick="window.wrongGroup=true">Join group</button></section>''')
                    await join_group(page, lambda _: None, [], True, 1000)
                    self.assertFalse(await page.evaluate('Boolean(window.wrongGroup)'))
                    await page.get_by_role('button', name='Join group', exact=True).evaluate("e => e.disabled=true")
                    await join_group(page, lambda _: None, [], True, 1000)
                finally:
                    await browser.close()
        asyncio.run(check())

    def test_pages_cannot_post_notice(self):
        async def check():
            async with async_playwright() as pw:
                browser = await pw.chromium.launch(headless=True)
                try:
                    page = await browser.new_page()
                    for text in ["This group doesn't allow Pages to join.", "Pages can't post in this group.", "Switch to your main profile to post in this group.", "This group is closed.", "This group has been archived.", "Only profiles can join this group.", "This group is not accepting new members."]:
                        await page.set_content('<div id="notice"></div>')
                        await page.locator('#notice').evaluate('(e, value) => e.textContent=value', text)
                        with self.assertRaises(AutomationError) as raised:
                            await check_group_availability(page)
                        self.assertEqual(raised.exception.code, 'GROUP_UNAVAILABLE')
                    await page.set_content('<h1>Private group</h1><article>This group is paused</article><article>This group is closed.</article>')
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

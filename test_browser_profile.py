import asyncio
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from playwright.async_api import async_playwright
import app
import facebook_group_scanner
from browser_profile import profile_lease, ProfileBusyError


class SharedProfileChecks(unittest.TestCase):
    def test_same_profile_path(self):
        self.assertEqual(app.PROFILE, facebook_group_scanner.PROFILE)

    def test_exclusive_and_released_on_error(self):
        async def check():
            with tempfile.TemporaryDirectory() as folder:
                profile = Path(folder) / 'profile'
                with self.assertRaisesRegex(ValueError, 'test failure'):
                    async with profile_lease(profile):
                        with self.assertRaises(ProfileBusyError):
                            async with profile_lease(profile):
                                pass
                        raise ValueError('test failure')
                async with profile_lease(profile):
                    pass
        asyncio.run(check())

    def test_cross_process_exclusion(self):
        child = '''import asyncio, sys
from browser_profile import profile_lease, ProfileBusyError
async def check():
    try:
        async with profile_lease(sys.argv[1]):
            print('available')
    except ProfileBusyError:
        print('busy')
        sys.exit(2)
asyncio.run(check())
'''
        async def check():
            with tempfile.TemporaryDirectory() as folder:
                profile = Path(folder) / 'profile'
                async with profile_lease(profile):
                    result = subprocess.run([sys.executable, '-c', child, str(profile)], capture_output=True, text=True, timeout=15, cwd=Path(__file__).parent)
                    self.assertEqual(result.returncode, 2, result.stderr)
                    self.assertIn('busy', result.stdout)
                result = subprocess.run([sys.executable, '-c', child, str(profile)], capture_output=True, text=True, timeout=15, cwd=Path(__file__).parent)
                self.assertEqual(result.returncode, 0, result.stderr)
        asyncio.run(check())

    def test_persistent_session_reused(self):
        async def check():
            with tempfile.TemporaryDirectory() as folder:
                profile = Path(folder) / 'profile'
                async with async_playwright() as pw:
                    async with profile_lease(profile):
                        context = await pw.chromium.launch_persistent_context(str(profile), headless=True)
                        await context.add_cookies([{'name': 'shared-test', 'value': 'session', 'url': 'https://example.test', 'expires': time.time()+3600}])
                        await context.close()
                    async with profile_lease(profile):
                        context = await pw.chromium.launch_persistent_context(str(profile), headless=True)
                        cookies = await context.cookies('https://example.test')
                        await context.close()
                    self.assertTrue(any(cookie['name']=='shared-test' and cookie['value']=='session' for cookie in cookies))
        asyncio.run(check())


if __name__ == '__main__':
    unittest.main()

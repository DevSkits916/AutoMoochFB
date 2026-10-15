from __future__ import annotations

import re
import asyncio
from collections.abc import Callable
from playwright.async_api import TimeoutError as PlaywrightTimeoutError, expect


class AutomationError(RuntimeError):
    def __init__(self, code: str, message: str, *, security: bool = False):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.security = security


CHECKPOINT = re.compile(r"checkpoint|confirm your identity|account temporarily locked|security check", re.I)
CAPTCHA = re.compile(r"captcha|enter the characters you see", re.I)
POST_LIMIT = re.compile(r"we limit how often you can (?:post|comment)|you (?:can['’]t|cannot) (?:try again|post) (?:again )?(?:later|right now|at the moment)|temporarily blocked from (?:posting|using this feature)", re.I)


async def check_group_availability(page):
    notices = page.get_by_text(re.compile(
        r"^(?:This group is paused|This group doesn['’]t allow Pages to (?:join|post|participate)|"
        r"Pages (?:can['’]t|cannot) (?:join|post in|participate in) this group|"
        r"You (?:can['’]t|cannot) post in this group as a Page|"
        r"Switch to your main profile to (?:join|post in) this group)\.?$", re.I)).filter(visible=True)
    for index in range(await notices.count()):
        notice = notices.nth(index)
        if not await notice.evaluate("e => !!e.closest('article,[role=article]')"):
            raise AutomationError('GROUP_UNAVAILABLE', await notice.inner_text())


async def verify_submission(page, editor, timeout_ms=15000):
    restriction = page.get_by_role('dialog').get_by_text(POST_LIMIT).filter(visible=True).first
    hidden = asyncio.create_task(editor.wait_for(state='hidden', timeout=timeout_ms))
    limited = asyncio.create_task(restriction.wait_for(state='visible', timeout=timeout_ms))
    try:
        done, _ = await asyncio.wait([hidden, limited], return_when=asyncio.FIRST_COMPLETED)
        if await restriction.is_visible():
            raise AutomationError('POSTING_RESTRICTED', 'Facebook limited posting. Worker stopped; review the restriction before restarting.', security=True)
        if hidden in done:
            await hidden
            return
        await limited
        raise AutomationError('POSTING_RESTRICTED', 'Facebook limited posting. Worker stopped; review the restriction before restarting.', security=True)
    except PlaywrightTimeoutError as error:
        raise AutomationError('POST_UNCERTAIN', 'Submission was clicked but the composer did not close; review before retrying.') from error
    finally:
        for task in (hidden, limited):
            if not task.done():
                task.cancel()
        await asyncio.gather(hidden, limited, return_exceptions=True)


async def first_visible(page, locators, timeout_ms: int = 8000):
    if not isinstance(locators, list):
        locators = [locators]
    candidate = locators[0]
    for locator in locators[1:]:
        candidate = candidate.or_(locator)
    candidate = candidate.filter(visible=True).first
    try:
        await candidate.wait_for(state="visible", timeout=timeout_ms)
        return candidate
    except PlaywrightTimeoutError:
        return None


async def first_enabled(page, locators, timeout_ms: int = 8000):
    try:
        async with asyncio.timeout(timeout_ms / 1000):
            candidate = await first_visible(page, locators, timeout_ms)
            if candidate:
                await expect(candidate).to_be_enabled(timeout=timeout_ms)
                return candidate
    except (TimeoutError, AssertionError):
        pass
    return None


def _normalized(value: str) -> str:
    return " ".join(value.split())


async def enter_post_text(page, editor, text: str) -> None:
    text = text.replace('\r\n', '\n').replace('\r', '\n')
    await editor.fill(text, timeout=10000)
    try:
        await expect(editor).to_have_text(text, use_inner_text=True, timeout=3000)
    except AssertionError as error:
        raise AutomationError("POST_TEXT_MISMATCH", "The composer text did not match the queued post. Submission was stopped.") from error


async def prepare_composer(page, on_step, timeout_ms, *, answers=None, accept_rules=False):
    on_step(f"Finding composer (up to {timeout_ms / 1000:g} seconds)...")
    try:
        async with asyncio.timeout(timeout_ms / 1000):
            trigger = await first_visible(page, [
                page.get_by_role("button", name=re.compile(r"write something|create (a )?post|what['\u2019]s on your mind", re.I)),
                page.get_by_text(re.compile(r"write something|what['\u2019]s on your mind", re.I), exact=False),
            ], timeout_ms)
            if trigger:
                await trigger.click(timeout=timeout_ms)
                dialog = page.get_by_role('dialog').filter(visible=True).first
                editors = page.get_by_role("dialog").locator('[contenteditable="true"]:not([aria-label^="Comment" i]):not([aria-placeholder^="Comment" i])')
                signal = await first_visible(page, [editors, dialog.get_by_text(re.compile(r'^(?:participant|membership) questions$', re.I))], timeout_ms)
                if signal and await dialog.get_by_text(re.compile(r'^(?:participant|membership) questions$', re.I)).first.is_visible():
                    on_step('Answering participation questions before opening the composer...')
                    await answer_membership_form(dialog, answers or [], accept_rules, on_step)
                    on_step('Participation request submitted; checking posting access...')
                    next_trigger = await first_visible(page, composer_triggers(page), min(timeout_ms, 3000))
                    if next_trigger:
                        await next_trigger.click(timeout=3000)
                editor = await first_visible(page, editors, timeout_ms)
                if editor:
                    return editor
    except TimeoutError:
        pass
    raise AutomationError("COMPOSER_NOT_FOUND", "Composer timeout reached; skipping this group.")


def membership_answer(question: str, answers: list[dict]) -> str | None:
    question = _normalized(question).casefold()
    matches = [item for item in answers if _normalized(item["question"]).casefold() in question]
    if not matches:
        return None
    # Prefer the most specific phrase; conflicting equally specific rules need review.
    longest = max(len(_normalized(item["question"])) for item in matches)
    values = {item["answer"] for item in matches if len(_normalized(item["question"])) == longest}
    return values.pop() if len(values) == 1 else None


async def question_text(field):
    return await field.evaluate("""element => {
        const isRadio = element.matches('input[type=radio],[role=radio]');
        const optionText = isRadio ? (element.getAttribute('aria-label') || element.labels?.[0]?.textContent || element.innerText || '').trim() : '';
        const useful = value => {
            const text = (value || '').trim();
            return text && text !== optionText && !/^(write an answer[.\\u2026]*|enter (an |your )?answer[.\\u2026]*|yes|no|you can choose multiple options)$/i.test(text);
        };
        const labelled = (element.getAttribute('aria-labelledby') || '').split(' ').map(id => document.getElementById(id)?.textContent || '').join(' ').trim();
        if (!isRadio && useful(labelled)) return labelled;
        const labels = element.labels?.length ? Array.from(element.labels).map(label => label.textContent).join(' ') : '';
        if (!isRadio && useful(labels)) return labels;
        if (!isRadio && useful(element.getAttribute('aria-label'))) return element.getAttribute('aria-label');
        if (element.matches('fieldset,[role=radiogroup]')) {
            const own = element.innerText.split('\\n').map(s => s.trim()).filter(useful);
            if (own.length) return own[0];
        }
        let parent = element.parentElement;
        while (parent && parent.getAttribute('role') !== 'dialog') {
            if (parent.querySelectorAll('textarea,input[type="text"],[role="textbox"],[role="radiogroup"],select').length > 1) break;
            const lines = parent.innerText.split('\\n').map(s => s.trim()).filter(useful);
            if (lines.length) return lines[0];
            parent = parent.parentElement;
        }
        return '';
    }""")


async def answer_membership_form(dialog, answers, accept_rules, on_step):
    unsupported = dialog.locator('[role="combobox"]:not(select)')
    for index in range(await unsupported.count()):
        field = unsupported.nth(index)
        if await field.is_visible() and not await field.evaluate("element => Boolean(element.closest('[role=radiogroup]'))"):
            raise AutomationError("JOIN_PENDING", "An unsupported membership choice needs manual follow-up.")
    fields = dialog.locator('textarea, input[type="text"], input:not([type]), [role="textbox"]:not(textarea):not(input), [role="radiogroup"], fieldset:has(input[type="radio"]):not(:has([role="radiogroup"])), select')
    planned = []
    for index in range(await fields.count()):
        field = fields.nth(index)
        if not await field.is_visible():
            continue
        question = await question_text(field)
        answer = membership_answer(question, answers)
        if answer is None:
            on_step(f"No saved answer for membership question: {question[:200]}")
            raise AutomationError("JOIN_PENDING", "A membership question has no matching saved answer.")
        planned.append((field, answer))
    for field, answer in planned:
        tag = await field.evaluate("element => element.tagName.toLowerCase()")
        role = await field.get_attribute("role")
        if tag == "select":
            await field.select_option(label=answer, timeout=3000)
        elif role == "radiogroup" or tag == 'fieldset':
            await field.get_by_role("radio", name=answer, exact=True).check(timeout=3000)
        else:
            await field.fill(answer, timeout=3000)
        on_step("Filled a membership question from saved answers")
    native_radios = dialog.get_by_role('radio')
    for index in range(await native_radios.count()):
        radio = native_radios.nth(index)
        if not await radio.is_visible() or await radio.evaluate("e => !!e.closest('fieldset,[role=radiogroup]')"):
            continue
        question = await question_text(radio)
        answer = membership_answer(question, answers)
        if answer is None:
            on_step(f'No saved answer for membership question: {question}')
            raise AutomationError('JOIN_PENDING', 'A radio choice needs a saved answer.')
        option = await radio.evaluate("e => (e.getAttribute('aria-label') || e.labels?.[0]?.innerText || e.innerText || e.parentElement.innerText || '').trim()")
        if option.casefold() == answer.strip().casefold():
            await radio.check(timeout=3000)
    checkboxes = dialog.get_by_role("checkbox")
    for index in range(await checkboxes.count()):
        checkbox = checkboxes.nth(index)
        if not await checkbox.is_visible():
            continue
        label = await question_text(checkbox)
        option = await checkbox.evaluate("element => (element.getAttribute('aria-label') || (element.labels?.[0]?.innerText) || element.parentElement.innerText || '').trim()")
        if re.fullmatch(r'yes|no', option, re.I):
            answer = membership_answer(label, answers)
            if answer is None or answer.strip().casefold() not in {'yes', 'no'}:
                on_step(f'No saved answer for membership question: {label}')
                raise AutomationError('JOIN_PENDING', 'A multiple-choice question needs a truthful saved Yes/No answer.')
            await checkbox.set_checked(option.casefold() == answer.strip().casefold(), timeout=3000)
            continue
        if await checkbox.is_checked():
            continue
        if accept_rules and re.search(r"agree|accept|rules", label, re.I):
            await checkbox.check(timeout=3000)
            on_step("Accepted group rules")
        else:
            answer = membership_answer(label, answers)
            if answer and answer.casefold() in {"yes", "true", "agree", "i agree", "accept"}:
                await checkbox.check(timeout=3000)
            elif not answer or answer.casefold() not in {"no", "false"}:
                raise AutomationError("JOIN_PENDING", "A membership checkbox needs a saved answer or rule acceptance.")
    submit = dialog.get_by_role("button", name=re.compile(r"^(submit|send|join|join group|submit answers|send request)$", re.I))
    button = await first_enabled(dialog.page, [submit], 3000)
    if not button:
        raise AutomationError("JOIN_PENDING", "Membership form cannot be submitted automatically.")
    on_step("Submitting membership answers...")
    await button.click(timeout=3000)
    try:
        await dialog.wait_for(state="hidden", timeout=5000)
    except Exception as error:
        raise AutomationError("JOIN_PENDING", "Membership form remains open; review it manually.") from error


def composer_triggers(page):
    return page.get_by_role("button", name=re.compile(r"write something|create (a )?post|what['\u2019]s on your mind", re.I)).or_(page.get_by_text(re.compile(r"write something|what['\u2019]s on your mind", re.I), exact=False))


async def join_group(page, on_step, answers, accept_rules, timeout_ms=15000):
    try:
        async with asyncio.timeout(timeout_ms / 1000):
            await _join_group(page, on_step, answers, accept_rules, timeout_ms)
    except TimeoutError as error:
        raise AutomationError("MEMBERSHIP_CHECK_TIMEOUT", "Membership check timed out; attempting to find the post composer.") from error


async def _join_group(page, on_step, answers, accept_rules, timeout_ms):
    on_step("Checking group membership...")
    pending = page.get_by_role("button", name=re.compile(r"^(cancel request|pending|request sent)$", re.I))
    join = page.get_by_role("button", name=re.compile(r"^join group$", re.I))
    joined = page.get_by_role("button", name=re.compile(r"^(joined|member)$", re.I))
    ready = await first_visible(page, [join, joined, pending, composer_triggers(page)], timeout_ms)
    if not ready:
        raise AutomationError("MEMBERSHIP_CHECK_TIMEOUT", "No membership controls or composer appeared before the timeout.")
    if await pending.filter(visible=True).first.is_visible():
        raise AutomationError("JOIN_PENDING", "Membership approval is pending; skipping this group.")
    button = join.filter(visible=True).first
    if not await button.is_visible():
        on_step("Already joined or no join required; finding composer...")
        return
    on_step("Requesting group membership...")
    if not await button.is_enabled():
        raise AutomationError('GROUP_UNAVAILABLE', 'Join is disabled. The group may be paused or disallow the active profile type.')
    await button.click(timeout=5000)
    outcome = await first_visible(page, [page.get_by_role("dialog"), pending, joined], timeout_ms)
    if not outcome:
        raise AutomationError("MEMBERSHIP_CHECK_TIMEOUT", "No join confirmation appeared before the timeout.")
    dialog = page.get_by_role("dialog").filter(visible=True).first
    if await dialog.is_visible():
        await answer_membership_form(dialog, answers, accept_rules, on_step)
    if await pending.filter(visible=True).first.is_visible():
        raise AutomationError("JOIN_PENDING", "Membership request submitted; awaiting administrator approval.")
    on_step("Join action completed; checking posting access...")


async def post_to_facebook(page, job: dict, on_step: Callable[[str], None] = lambda _step: None, *, join_before_post=False, composer_timeout_ms=15000, membership_answers=None, accept_group_rules=False) -> dict:
    submitted = False
    try:
        on_step("Opening Facebook group...")
        await page.goto(job["groupUrl"], wait_until="domcontentloaded", timeout=45000)
        body = await page.locator("body").inner_text(timeout=10000)
        checkpoint_url = re.search(r'/(?:checkpoint|confirmidentity|confirm_identity)(?:/|\?|$)', page.url, re.I)
        checkpoint_heading = page.get_by_role('heading', name=re.compile(r'^(?:confirm your identity|security check|account temporarily locked)$', re.I)).filter(visible=True).first
        if checkpoint_url or await checkpoint_heading.is_visible():
            raise AutomationError("CHECKPOINT_DETECTED", "Facebook requires a security check.", security=True)
        captcha_control = page.locator('iframe[src*="captcha"], input[name="captcha_response"]').filter(visible=True).first
        captcha_heading = page.get_by_role('heading', name=re.compile(r'^(?:captcha|verify you are human|confirm you are human)$', re.I)).filter(visible=True).first
        if await captcha_control.is_visible() or await captcha_heading.is_visible():
            raise AutomationError("CAPTCHA_DETECTED", "Facebook displayed a CAPTCHA.", security=True)
        login = page.locator('input[name="email"]').or_(page.get_by_role("button", name=re.compile(r"log in", re.I))).filter(visible=True).first
        if await login.is_visible():
            raise AutomationError("LOGIN_REQUIRED", "Open Browser / Login and sign in first.", security=True)
        unavailable = page.get_by_role('heading', name=re.compile(r"(?:content|page) isn['’]t available", re.I)).filter(visible=True).first
        group_controls = page.get_by_role('button', name=re.compile(r'^(?:joined|join group|member)$', re.I)).filter(visible=True).first
        if await unavailable.is_visible() and not await group_controls.is_visible():
            raise AutomationError("GROUP_NOT_FOUND", "The group is unavailable or inaccessible.")
        await check_group_availability(page)
        if join_before_post:
            try:
                await join_group(page, on_step, membership_answers or [], accept_group_rules, composer_timeout_ms)
            except AutomationError as error:
                if error.code != "MEMBERSHIP_CHECK_TIMEOUT":
                    raise
                on_step("Membership check timed out; trying to post to this group before continuing...")
        editor = await prepare_composer(page, on_step, composer_timeout_ms, answers=membership_answers, accept_rules=accept_group_rules)
        await check_group_availability(page)
        dialog = page.get_by_role("dialog")
        on_step("Entering post text...")
        await enter_post_text(page, editor, job["postText"])
        post_button = await first_enabled(page, [dialog.get_by_role("button", name=re.compile(r"^(post|publish)$", re.I))])
        if not post_button:
            await check_group_availability(page)
            raise AutomationError("POST_BUTTON_NOT_FOUND", "Post button was not enabled after entering the text.")
        on_step("Submitting post...")
        await post_button.click(timeout=10000)
        submitted = True
        on_step("Verifying post...")
        await verify_submission(page, editor)
        return {"status": "posted"}
    except AutomationError:
        raise
    except Exception as error:
        if submitted:
            raise AutomationError("POST_UNCERTAIN", f"Browser error after submission: {error}") from error
        raise

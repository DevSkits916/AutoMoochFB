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


async def prepare_composer(page, on_step, timeout_ms):
    on_step(f"Finding composer (up to {timeout_ms / 1000:g} seconds)...")
    try:
        async with asyncio.timeout(timeout_ms / 1000):
            trigger = await first_visible(page, [
                page.get_by_role("button", name=re.compile(r"write something|create (a )?post|what['\u2019]s on your mind", re.I)),
                page.get_by_text(re.compile(r"write something|what['\u2019]s on your mind", re.I), exact=False),
            ], timeout_ms)
            if trigger:
                await trigger.click(timeout=timeout_ms)
                editor = await first_visible(page, page.get_by_role("dialog").locator('[contenteditable="true"]:not([aria-label^="Comment" i]):not([aria-placeholder^="Comment" i])'), timeout_ms)
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
        const labelled = (element.getAttribute('aria-labelledby') || '').split(' ').map(id => document.getElementById(id)?.textContent || '').join(' ').trim();
        if (labelled) return labelled;
        if (element.labels?.length) return Array.from(element.labels).map(label => label.textContent).join(' ');
        if (element.getAttribute('aria-label')) return element.getAttribute('aria-label');
        let parent = element.parentElement;
        while (parent && parent.getAttribute('role') !== 'dialog') {
            if (parent.querySelectorAll('textarea,input[type="text"],[role="textbox"],[role="radiogroup"],select').length > 1) break;
            if (parent.innerText.trim()) return parent.innerText;
            parent = parent.parentElement;
        }
        return element.getAttribute('placeholder') || '';
    }""")


async def answer_membership_form(dialog, answers, accept_rules, on_step):
    unsupported = dialog.locator('[role="combobox"]:not(select), input[type="radio"]:not([role="radio"])')
    for index in range(await unsupported.count()):
        field = unsupported.nth(index)
        if await field.is_visible() and not await field.evaluate("element => Boolean(element.closest('[role=radiogroup]'))"):
            raise AutomationError("JOIN_PENDING", "An unsupported membership choice needs manual follow-up.")
    fields = dialog.locator('textarea, input[type="text"], input:not([type]), [role="textbox"]:not(textarea):not(input), [role="radiogroup"], select')
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
        elif role == "radiogroup":
            await field.get_by_role("radio", name=answer, exact=True).check(timeout=3000)
        else:
            await field.fill(answer, timeout=3000)
        on_step("Filled a membership question from saved answers")
    checkboxes = dialog.get_by_role("checkbox")
    for index in range(await checkboxes.count()):
        checkbox = checkboxes.nth(index)
        if not await checkbox.is_visible() or await checkbox.is_checked():
            continue
        label = await question_text(checkbox)
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
        raise AutomationError("JOIN_PENDING", "Membership check timed out; skipping this group.") from error


async def _join_group(page, on_step, answers, accept_rules, timeout_ms):
    on_step("Checking group membership...")
    pending = page.get_by_role("button", name=re.compile(r"^(cancel request|pending|request sent)$", re.I))
    join = page.get_by_role("button", name=re.compile(r"^join group$", re.I))
    joined = page.get_by_role("button", name=re.compile(r"^(joined|member)$", re.I))
    ready = await first_visible(page, [join, joined, pending, composer_triggers(page)], timeout_ms)
    if not ready:
        raise AutomationError("COMPOSER_NOT_FOUND", "No membership controls or composer appeared before the timeout.")
    if await pending.filter(visible=True).first.is_visible():
        raise AutomationError("JOIN_PENDING", "Membership approval is pending; skipping this group.")
    button = join.filter(visible=True).first
    if not await button.is_visible():
        on_step("Already joined or no join required; finding composer...")
        return
    on_step("Requesting group membership...")
    await button.click(timeout=5000)
    outcome = await first_visible(page, [page.get_by_role("dialog"), pending, joined], timeout_ms)
    if not outcome:
        raise AutomationError("JOIN_PENDING", "No join confirmation appeared; review the group manually.")
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
        if CHECKPOINT.search(f"{page.url} {body}"):
            raise AutomationError("CHECKPOINT_DETECTED", "Facebook requires a security check.", security=True)
        if CAPTCHA.search(body):
            raise AutomationError("CAPTCHA_DETECTED", "Facebook displayed a CAPTCHA.", security=True)
        login = page.locator('input[name="email"]').or_(page.get_by_role("button", name=re.compile(r"log in", re.I))).filter(visible=True).first
        if await login.is_visible():
            raise AutomationError("LOGIN_REQUIRED", "Open Browser / Login and sign in first.", security=True)
        if re.search(r"content isn't available|page isn't available", body, re.I):
            raise AutomationError("GROUP_NOT_FOUND", "The group is unavailable or inaccessible.")
        if join_before_post:
            await join_group(page, on_step, membership_answers or [], accept_group_rules, composer_timeout_ms)
        editor = await prepare_composer(page, on_step, composer_timeout_ms)
        dialog = page.get_by_role("dialog")
        on_step("Entering post text...")
        await enter_post_text(page, editor, job["postText"])
        post_button = await first_enabled(page, [dialog.get_by_role("button", name=re.compile(r"^(post|publish)$", re.I))])
        if not post_button:
            raise AutomationError("POST_BUTTON_NOT_FOUND", "Post button was not enabled after entering the text.")
        on_step("Submitting post...")
        await post_button.click(timeout=10000)
        submitted = True
        on_step("Verifying post...")
        try:
            await editor.wait_for(state="hidden", timeout=15000)
        except PlaywrightTimeoutError as error:
            raise AutomationError("POST_UNCERTAIN", "Submission was clicked but the composer did not close; review before retrying.") from error
        return {"status": "posted"}
    except AutomationError:
        raise
    except Exception as error:
        if submitted:
            raise AutomationError("POST_UNCERTAIN", f"Browser error after submission: {error}") from error
        raise

"""First-time LinkedIn authentication (headful) -> session.json."""
import logging
import os
import time

from playwright.sync_api import sync_playwright

from client_config import SESSION_FILE

log = logging.getLogger(__name__)
NAV_SELECTOR = "#global-nav, nav.global-nav, header#global-nav"


def _logged_in(page) -> bool:
    url = page.url
    if any(x in url for x in ("/login", "/checkpoint", "/authwall", "/uas/")):
        return False
    if "linkedin.com/feed" in url:
        return True
    try:
        return page.locator(NAV_SELECTOR).count() > 0
    except Exception:
        return False


def login_and_save_session(timeout_s: int = 300) -> bool:
    """Open a visible Chromium, let the user log in manually (2FA / CAPTCHA included),
    detect the global nav and save cookies/local storage to session.json."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        ctx = browser.new_context(viewport={"width": 1280, "height": 900})
        page = ctx.new_page()
        page.goto("https://www.linkedin.com/login", wait_until="domcontentloaded")
        # convenience pre-fill only - the user still submits and completes security prompts
        try:
            if os.getenv("LINKEDIN_EMAIL"):
                page.fill("#username", os.getenv("LINKEDIN_EMAIL"))
            if os.getenv("LINKEDIN_PASSWORD"):
                page.fill("#password", os.getenv("LINKEDIN_PASSWORD"))
        except Exception:
            pass
        log.info("Complete the LinkedIn login in the browser window (2FA/CAPTCHA if asked)...")
        deadline = time.time() + timeout_s
        ok = False
        while time.time() < deadline:
            if _logged_in(page):
                ok = True
                break
            page.wait_for_timeout(1500)
        if ok:
            page.wait_for_timeout(1500)
            ctx.storage_state(path=str(SESSION_FILE))
            log.info("Session saved to %s", SESSION_FILE)
        else:
            log.error("Login not detected within %ss - session not saved.", timeout_s)
        browser.close()
        return ok


def session_exists() -> bool:
    return SESSION_FILE.exists() and SESSION_FILE.stat().st_size > 10


def session_is_valid() -> bool:
    """Headless check that the saved session still reaches the feed."""
    if not session_exists():
        return False
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            ctx = browser.new_context(storage_state=str(SESSION_FILE))
            page = ctx.new_page()
            page.goto("https://www.linkedin.com/feed/", wait_until="domcontentloaded")
            page.wait_for_timeout(3000)
            return _logged_in(page)
        finally:
            browser.close()


def ensure_session() -> bool:
    """Reuse a valid session; only open the headful login when needed."""
    if session_is_valid():
        return True
    log.info("No valid LinkedIn session found - starting manual login.")
    return login_and_save_session()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print("Session OK" if ensure_session() else "Login failed")

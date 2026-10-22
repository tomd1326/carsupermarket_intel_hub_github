import asyncio
import io
import logging
import time

from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError
import pandas as pd

logger = logging.getLogger(__name__)

LOGIN_URL = "https://id.motabilityoperations.co.uk/mfldirect/login"
SEARCH_URL = (
    "https://mfldirect.co.uk/search-results"
    "?page=1"
    "&searchType=advancedSearch"
    "&sortCode=mfldirect_PRICE_REDUCED_DATE_descending"
    "&view=table"
    "&category=MAINLAND_ONLY"
    "&fixedPrice=true"
    "&conditionTier=0"
)

FIND_CSRF_JS = """
    () => {
        if (window.__NEXT_DATA__) {
            const match = JSON.stringify(window.__NEXT_DATA__).match(/"csrfToken":"([^"]+)"/);
            if (match) return match[1];
        }
        const cookieMatch = document.cookie.match(/xsrf[^=]*=([^;]+)/i);
        if (cookieMatch) return cookieMatch[1];
        for (let i = 0; i < sessionStorage.length; i++) {
            const key = sessionStorage.key(i);
            if (key.toLowerCase().includes('xsrf') || key.toLowerCase().includes('csrf'))
                return sessionStorage.getItem(key);
        }
        for (let i = 0; i < localStorage.length; i++) {
            const key = localStorage.key(i);
            if (key.toLowerCase().includes('xsrf') || key.toLowerCase().includes('csrf'))
                return localStorage.getItem(key);
        }
        return null;
    }
"""

EXPORT_JS = """
    async (xsrfToken) => {
        const response = await fetch('/api/exportStocklist', {
            method: 'POST',
            credentials: 'include',
            headers: {
                'content-type': 'application/json',
                'accept': 'application/json, text/plain, */*',
                'x-mo-tab-id': 'mfldirect-playwright-export',
                'x-xsrf-token': xsrfToken,
            },
            body: JSON.stringify({
                criteria: 'category=MAINLAND_ONLY&fixedPrice=true&conditionTier=0',
                sortCode: 'mfldirect_PRICE_REDUCED_DATE_descending'
            })
        });
        return {
            status: response.status,
            text: await response.text()
        };
    }
"""


class MotabilityFetchClient:
    def __init__(self, username: str, password: str):
        self.username = username
        self.password = password

    def fetch_dataframe(self) -> pd.DataFrame:
        import concurrent.futures
        def _run_in_thread():
            import sys
            if sys.platform == 'win32':
                loop = asyncio.ProactorEventLoop()
            else:
                loop = asyncio.new_event_loop()
            try:
                return loop.run_until_complete(self._run())
            finally:
                loop.close()
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            csv_text = pool.submit(_run_in_thread).result()

        df = pd.read_csv(io.StringIO(csv_text))
        logger.info(f"Motability: fetched {len(df)} rows")
        return df

    async def _run(self) -> str:
        t0 = time.monotonic()
        def elapsed():
            return f"{time.monotonic() - t0:.1f}s"

        async with async_playwright() as p:
            logger.info("Motability: launching browser")
            browser = await p.chromium.launch(headless=True, args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--single-process",
            ])
            logger.info(f"Motability: browser launched ({elapsed()})")
            context = await browser.new_context()
            page = await context.new_page()
            page.set_default_timeout(60000)
            try:
                await self._login(page, elapsed)
                logger.info(f"Motability: navigating to search page ({elapsed()})")
                await page.goto(SEARCH_URL, timeout=60000)
                logger.info(f"Motability: search page goto complete ({elapsed()})")
                await page.wait_for_load_state("load", timeout=60000)
                logger.info(f"Motability: search page loaded ({elapsed()})")
                return await self._export_csv(page, elapsed)
            finally:
                await browser.close()
                logger.info(f"Motability: browser closed ({elapsed()})")

    async def _login(self, page, elapsed):
        logger.info(f"Motability: navigating to login page ({elapsed()})")
        await page.goto(LOGIN_URL)
        logger.info(f"Motability: login page goto complete ({elapsed()})")
        await page.wait_for_load_state("domcontentloaded")
        logger.info(f"Motability: login page domcontentloaded ({elapsed()})")
        await page.wait_for_timeout(2000)
        await self._dismiss_cookie_popup(page, elapsed)
        logger.info(f"Motability: waiting for username field ({elapsed()})")
        await page.wait_for_selector("input[placeholder='Username']", state="visible", timeout=30000)
        logger.info(f"Motability: filling credentials ({elapsed()})")
        await page.get_by_placeholder("Username").fill(self.username)
        await page.get_by_placeholder("Password").fill(self.password)
        await page.click("button:has-text('Log in')")
        logger.info(f"Motability: login submitted, waiting for redirect ({elapsed()})")
        await page.wait_for_url("https://mfldirect.co.uk/**", timeout=30000)
        await page.wait_for_load_state("domcontentloaded")
        logger.info(f"Motability: logged in, landed on {page.url} ({elapsed()})")

    async def _dismiss_cookie_popup(self, page, elapsed):
        try:
            await page.wait_for_selector("text='We use cookies'", state="visible", timeout=5000)
            logger.info(f"Motability: dismissing cookie popup ({elapsed()})")
            btn = page.locator("button:has-text('Accept all')")
            await btn.scroll_into_view_if_needed()
            await btn.click()
            await page.wait_for_selector("text='We use cookies'", state="hidden", timeout=10000)
            logger.info(f"Motability: cookie popup dismissed ({elapsed()})")
        except PlaywrightTimeoutError:
            logger.info(f"Motability: no cookie popup found ({elapsed()})")

    async def _export_csv(self, page, elapsed) -> str:
        logger.info(f"Motability: waiting for CSRF token ({elapsed()})")
        xsrf_token = None
        for attempt in range(10):
            xsrf_token = await page.evaluate(FIND_CSRF_JS)
            if xsrf_token:
                logger.info(f"Motability: CSRF token found on attempt {attempt + 1} ({elapsed()})")
                break
            await asyncio.sleep(0.5)
        if not xsrf_token:
            raise RuntimeError("Motability: could not find CSRF token after waiting")

        logger.info(f"Motability: sending CSV export request to /api/exportStocklist ({elapsed()})")
        result = await page.evaluate(EXPORT_JS, xsrf_token)
        status = result["status"]
        text = result.get("text") or ""
        logger.info(f"Motability: export API responded, status={status}, size={len(text)} bytes ({elapsed()})")
        if status != 200:
            logger.error(f"Motability: export API error body: {text[:500]}")
            raise RuntimeError(f"Motability export API returned {status}")
        row_count = max(0, text.count('\n') - 1)
        logger.info(f"Motability: CSV received, ~{row_count} data rows ({elapsed()})")
        return text

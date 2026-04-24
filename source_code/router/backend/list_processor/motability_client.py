import asyncio
import io
import logging
import os
from datetime import datetime

from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError
import pandas as pd

logger = logging.getLogger(__name__)

LOGIN_URL = "https://id.motabilityoperations.co.uk/mfldirect/login"
SEARCH_URL = (
    "https://mfldirect.co.uk/search-results"
    "?page=1"
    "&searchType=advancedSearch"
    "&sortCode=mfldirect_CAP_RATIO_ascending"
    "&view=table"
    "&category=MAINLAND_ONLY"
    "&fixedPrice=true"
    "&conditionTier=0"
)

EXPORT_JS = """
    async () => {
        let xsrfToken = '';
        if (window.__NEXT_DATA__) {
            const data = JSON.stringify(window.__NEXT_DATA__);
            const match = data.match(/"csrfToken":"([^"]+)"/);
            if (match) xsrfToken = match[1];
        }
        if (!xsrfToken) {
            const cookieMatch = document.cookie.match(/xsrf[^=]*=([^;]+)/i);
            if (cookieMatch) xsrfToken = cookieMatch[1];
        }
        if (!xsrfToken) {
            for (let i = 0; i < sessionStorage.length; i++) {
                const key = sessionStorage.key(i);
                if (key.toLowerCase().includes('xsrf') || key.toLowerCase().includes('csrf')) {
                    xsrfToken = sessionStorage.getItem(key);
                    break;
                }
            }
        }
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
                sortCode: 'mfldirect_CAP_RATIO_ascending'
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

    def fetch_dataframe(self, row_limit: int = 100) -> pd.DataFrame:
        import concurrent.futures
        def _run_in_thread():
            loop = asyncio.ProactorEventLoop()
            try:
                return loop.run_until_complete(self._run())
            finally:
                loop.close()
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            csv_text = pool.submit(_run_in_thread).result()

        df = pd.read_csv(io.StringIO(csv_text))
        if row_limit:
            df = df.head(row_limit)
        logger.info(f"Motability: fetched {len(df)} rows")
        return df

    async def _run(self) -> str:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context()
            page = await context.new_page()
            try:
                await self._login(page)
                await page.goto(SEARCH_URL)
                await page.wait_for_load_state("domcontentloaded")
                await page.wait_for_timeout(2000)
                return await self._export_csv(page)
            finally:
                await browser.close()

    async def _login(self, page):
        logger.info("Motability: navigating to login page")
        await page.goto(LOGIN_URL)
        await page.wait_for_load_state("domcontentloaded")
        await page.wait_for_timeout(2000)
        await self._dismiss_cookie_popup(page)
        await page.wait_for_selector("input[placeholder='Username']", state="visible", timeout=15000)
        await page.get_by_placeholder("Username").fill(self.username)
        await page.get_by_placeholder("Password").fill(self.password)
        await page.click("button:has-text('Log in')")
        await page.wait_for_url("https://mfldirect.co.uk/**", timeout=15000)
        await page.wait_for_load_state("domcontentloaded")
        logger.info(f"Motability: logged in, landed on {page.url}")

    async def _dismiss_cookie_popup(self, page):
        try:
            await page.wait_for_selector("text='We use cookies'", state="visible", timeout=5000)
            btn = page.locator("button:has-text('Accept all')")
            await btn.scroll_into_view_if_needed()
            await btn.click()
            await page.wait_for_selector("text='We use cookies'", state="hidden", timeout=10000)
        except PlaywrightTimeoutError:
            pass

    async def _export_csv(self, page) -> str:
        logger.info("Motability: calling export API")
        result = await page.evaluate(EXPORT_JS)
        status = result["status"]
        logger.info(f"Motability: export API status {status}")
        if status != 200:
            raise RuntimeError(f"Motability export API returned {status}")
        return result["text"]

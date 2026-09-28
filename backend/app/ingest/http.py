import logging
import time

import requests

logger = logging.getLogger(__name__)

NSE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com/",
}

RETRY_STATUSES = frozenset({401, 403, 404, 429, 500, 502, 503, 504})

# For a dated bhavcopy file, 404 is a definitive answer -- "no trading session on
# this date" -- not a transient blip. Retrying it costs 4 extra attempts against
# 4 candidate URLs with 1+2+4+8s of backoff between them, so a single market
# holiday burned ~60s of sleeping to download nothing. Callers whose 404 really
# can be transient keep the default set.
NO_RETRY_404_STATUSES = frozenset({401, 403, 429, 500, 502, 503, 504})


class RateLimitedSession:
    """Requests session with a min request interval, backoff, and NSE cookie bootstrap.

    Cloud-IP friendly: NSE blocks IPs that hammer www.nseindia.com, so all API
    calls go through a throttled session that re-derives cookies on 401/403.

    `retry_statuses` defaults to RETRY_STATUSES (everything, including 404).
    """

    def __init__(
        self,
        min_interval: float = 0.5,
        max_retries: int = 5,
        backoff_base: float = 2.0,
        timeout: float = 30.0,
        retry_statuses: frozenset[int] = RETRY_STATUSES,
    ) -> None:
        self.session = requests.Session()
        self.session.headers.update(NSE_HEADERS)
        self.min_interval = min_interval
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self.timeout = timeout
        self.retry_statuses = retry_statuses
        self._last_request = 0.0

    def _throttle(self) -> None:
        wait = self.min_interval - (time.monotonic() - self._last_request)
        if wait > 0:
            time.sleep(wait)
        self._last_request = time.monotonic()

    def bootstrap_nse_cookies(self) -> None:
        self._throttle()
        try:
            self.session.get("https://www.nseindia.com", timeout=10)
        except requests.RequestException as e:
            logger.warning("NSE cookie bootstrap failed: %s", e)

    def get(self, url: str, timeout: float | None = None, **kwargs: object) -> requests.Response:
        last_error: Exception | None = None
        for attempt in range(self.max_retries):
            self._throttle()
            try:
                res = self.session.get(url, timeout=timeout or self.timeout, **kwargs)
            except requests.RequestException as e:
                last_error = e
                logger.warning(
                    "request failed (%d/%d) %s: %s", attempt + 1, self.max_retries, url, e
                )
                time.sleep(self.backoff_base**attempt)
                continue
            if res.status_code in self.retry_statuses and attempt < self.max_retries - 1:
                logger.warning(
                    "status %d on %s (%d/%d), backing off", res.status_code, url, attempt + 1, self.max_retries
                )
                last_error = requests.HTTPError(f"{res.status_code} {url}", response=res)
                if res.status_code in (401, 403):
                    self.bootstrap_nse_cookies()
                time.sleep(self.backoff_base**attempt)
                continue
            return res
        if last_error is not None:
            raise last_error
        raise RuntimeError(f"GET failed after {self.max_retries} attempts: {url}")

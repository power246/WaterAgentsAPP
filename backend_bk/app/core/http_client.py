import asyncio
import random
import httpx
import logging
from app.core.config import settings

logger = logging.getLogger(__name__)

class AsyncHttpClient:
    _client: httpx.AsyncClient | None = None
    _semaphore: asyncio.Semaphore | None = None

    @classmethod
    def _get_semaphore(cls) -> asyncio.Semaphore:
        # Lazy loading: Created within the event loop during the first use
        if cls._semaphore is None:
            cls._semaphore = asyncio.Semaphore(5)
        return cls._semaphore

    @classmethod
    def get_client(cls) -> httpx.AsyncClient:
        if cls._client is None:
            cls._client = httpx.AsyncClient(
                headers=settings.HTTP_HEADERS,
                follow_redirects=True,
                timeout=settings.REQUEST_TIMEOUT
            )
        return cls._client

    @classmethod
    async def request(
        cls,
        method: str,
        url: str,
        retries: int = 3,
        jitter: bool = True,
        **kwargs
    ) -> httpx.Response:
        """
        Unified request entry:
        - concurrency limit
        - random jitter
        """
        async with cls._get_semaphore():
            for attempt in range(retries):
                    try:
                        if jitter:
                            await asyncio.sleep(random.uniform(0.1, 0.5))

                        client = cls.get_client()
                        response = await client.request(method, url, **kwargs)
                        response.raise_for_status()
                        return response
                        
                    except (httpx.HTTPStatusError, httpx.RequestError) as e:
                        if attempt == retries - 1: # Last attempt
                            logger.error(f"The request ultimately failed {url}: {e}")
                            raise
                        
                        wait_time = (attempt + 1) * 2
                        logger.warning(f"Request exception, retrying for the {attempt+1} time... Error: {e}")
                        await asyncio.sleep(wait_time)

    @classmethod
    async def close_client(cls):
        if cls._client:
            await cls._client.aclose()
            cls._client = None
        cls._semaphore = None

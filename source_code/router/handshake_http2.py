import asyncio, httpx

async def t():
    async with httpx.AsyncClient(http2=True, timeout=10) as c:
        r = await c.get("https://www.cloudflare.com")
        print("http_version=", r.http_version, "status=", r.status_code)

if __name__ == "__main__":
    asyncio.run(t())
import asyncio
import httpx

async def t():
    async with httpx.AsyncClient(http2=True, timeout=5.0) as c:
        pass
    print("client_ok")

if __name__ == "__main__":
    asyncio.run(t())
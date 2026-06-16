import httpx
import asyncio

async def test():
    resp = await httpx.AsyncClient().post(
        'http://localhost:11434/v1/chat/completions', 
        json={'model': 'nonexistent', 'messages': [{'role': 'user', 'content': 'hello'}]}
    )
    print(resp.status_code, resp.text)

asyncio.run(test())

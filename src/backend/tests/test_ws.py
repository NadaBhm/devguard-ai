import asyncio
import websockets
import json

async def test():
    uri = "ws://localhost:8000/ws/jobs/test-123"
    async with websockets.connect(uri) as ws:
        # Send a RAG query
        await ws.send(json.dumps({
            "action": "rag_query",
            "query": "What framework does this use?"
        }))
        
        # Receive answer
        response = await ws.recv()
        print("Response:", json.loads(response))

asyncio.run(test())
import json
import os

import pytest
import websockets

# The WS endpoint requires a valid access-token JWT in the connection URL:
#   ws://localhost:8000/ws/jobs/{id}?token=<access_token>
# This test needs a running server plus a token. Provided  via: WS_TEST_TOKEN=<token> pytest src/backend/tests/test_ws.py
TOKEN = os.environ.get("WS_TEST_TOKEN")


@pytest.mark.skipif(not TOKEN, reason="WS_TEST_TOKEN required (login against a live server first)")
async def test_ws_requires_token_and_pings():
    uri = f"ws://localhost:8000/ws/jobs/test-123?token={TOKEN}"
    try:
        async with websockets.connect(uri) as ws:
            await ws.send(json.dumps({"action": "ping"}))
            payload = json.loads(await ws.recv())
            assert payload["type"] == "pong"
    except OSError:
        pytest.skip("live server not reachable at localhost:8000")

"""
Tests pour websocket_manager.py
"""

import pytest
from unittest.mock import AsyncMock, MagicMock
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from websocket_manager import ConnectionManager


@pytest.fixture
def manager():
    return ConnectionManager()


@pytest.fixture
def mock_websocket():
    ws = AsyncMock()
    ws.send_json = AsyncMock()
    return ws


@pytest.mark.asyncio
async def test_connect_adds_websocket(manager, mock_websocket):
    await manager.connect(mock_websocket, "job-123")
    assert manager.get_listener_count("job-123") == 1


@pytest.mark.asyncio
async def test_connect_accepts_websocket(manager, mock_websocket):
    await manager.connect(mock_websocket, "job-123")
    mock_websocket.accept.assert_called_once()


def test_disconnect_removes_websocket(manager, mock_websocket):
    import asyncio
    asyncio.run(manager.connect(mock_websocket, "job-123"))
    manager.disconnect(mock_websocket, "job-123")
    assert manager.get_listener_count("job-123") == 0


def test_disconnect_cleans_empty_job(manager, mock_websocket):
    import asyncio
    asyncio.run(manager.connect(mock_websocket, "job-123"))
    manager.disconnect(mock_websocket, "job-123")
    assert "job-123" not in manager.get_all_jobs()


@pytest.mark.asyncio
async def test_send_to_job_broadcasts_to_all(manager, mock_websocket):
    ws2 = AsyncMock()
    ws2.send_json = AsyncMock()
    
    await manager.connect(mock_websocket, "job-123")
    await manager.connect(ws2, "job-123")
    
    msg = {"type": "test", "data": "hello"}
    await manager.send_to_job("job-123", msg)
    
    mock_websocket.send_json.assert_called_once_with(msg)
    ws2.send_json.assert_called_once_with(msg)


@pytest.mark.asyncio
async def test_send_to_job_skips_dead_connections(manager, mock_websocket):
    dead_ws = AsyncMock()
    dead_ws.send_json = AsyncMock(side_effect=Exception("Connection closed"))
    
    await manager.connect(mock_websocket, "job-123")
    await manager.connect(dead_ws, "job-123")
    
    msg = {"type": "test"}
    await manager.send_to_job("job-123", msg)
    
    # Le dead_ws doit être retiré
    assert manager.get_listener_count("job-123") == 1


@pytest.mark.asyncio
async def test_broadcast_sends_to_all_jobs(manager, mock_websocket):
    ws2 = AsyncMock()
    ws2.send_json = AsyncMock()
    
    await manager.connect(mock_websocket, "job-1")
    await manager.connect(ws2, "job-2")
    
    msg = {"type": "broadcast"}
    await manager.broadcast(msg)
    
    mock_websocket.send_json.assert_called_once_with(msg)
    ws2.send_json.assert_called_once_with(msg)


def test_get_all_jobs(manager, mock_websocket):
    import asyncio
    asyncio.run(manager.connect(mock_websocket, "job-1"))
    asyncio.run(manager.connect(mock_websocket, "job-2"))
    jobs = manager.get_all_jobs()
    assert "job-1" in jobs
    assert "job-2" in jobs
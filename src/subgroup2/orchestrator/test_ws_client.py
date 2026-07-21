"""
DevGuard AI - WebSocket Test Client
Validates the WS protocol by running a full mock pipeline end-to-end.

Owner: Hbib (Subgroup 2 - Execution & Control)
Sprint: 1 (Foundation & Mock Agents)
CDC Reference: T-1.10

Usage:
    # Terminal 1: Start the WS server
    python -m src.subgroup2.orchestrator.websocket_server

    # Terminal 2: Run this test client
    python -m src.subgroup2.orchestrator.test_ws_client

CHANGELOG:
- v1.0.0: Initial implementation - auto-approve gates, full pipeline validation
- v1.0.1: Fixed imports for Windows compatibility
"""

import asyncio
import json
import uuid
import websockets
import logging

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)

WS_URL = "ws://localhost:8001/ws/jobs"
TEST_REPO = "https://github.com/NadaBhm/devguard-ai"


async def test_full_pipeline_auto_approve():
    """
    Test the complete pipeline with automatic gate approval.
    Simulates a user who always approves (for automated testing).
    """
    job_id = str(uuid.uuid4())
    uri = f"{WS_URL}/{job_id}"

    print("=" * 60)
    print(f"Test Client | Job ID: {job_id}")
    print("=" * 60)

    async with websockets.connect(uri) as ws:
        # Step 1: Start the job
        print(f"\n[1] Connecting to {uri}")
        await ws.send(json.dumps({
            "type": "start",
            "repo_url": TEST_REPO,
        }))
        print(f"[1] Sent: start job for {TEST_REPO}")

        # Step 2: Receive events until completion or interrupt
        gate_count = 0
        while True:
            try:
                msg = await asyncio.wait_for(ws.recv(), timeout=30.0)
                data = json.loads(msg)
                msg_type = data.get("type")

                # ---------------------------------------------------------
                # PROGRESS
                # ---------------------------------------------------------
                if msg_type == "progress":
                    print(f"  📊 {data['node']:20s} | status: {data['status']}")

                # ---------------------------------------------------------
                # INTERRUPT (human gate)
                # ---------------------------------------------------------
                elif msg_type == "interrupt":
                    gate_count += 1
                    gate_name = data.get("gate", "unknown")
                    print(f"\n  ⏸️  GATE {gate_count}: {gate_name}")
                    print(f"     Message: {data.get('message')}")
                    print(f"     Context: {json.dumps(data.get('context', {}), indent=6)[:200]}...")

                    # Auto-approve for testing
                    print(f"\n  ✅ Auto-approving gate {gate_count}...")
                    await ws.send(json.dumps({
                        "type": "resume",
                        "data": {
                            "approved": True,
                            "comment": f"Auto-approved by test client (gate {gate_count})",
                            "approved_by": "test-client@devguard.ai",
                        }
                    }))

                # ---------------------------------------------------------
                # COMPLETED
                # ---------------------------------------------------------
                elif msg_type == "completed":
                    final_status = data.get("final_status", "unknown")
                    print(f"\n  🎉 PIPELINE COMPLETED")
                    print(f"     Final status: {final_status}")
                    print(f"     Total gates passed: {gate_count}")
                    return final_status == "completed"

                # ---------------------------------------------------------
                # ERROR
                # ---------------------------------------------------------
                elif msg_type == "error":
                    print(f"\n  ❌ ERROR: {data.get('message')}")
                    return False

                else:
                    print(f"  ? Unknown message type: {msg_type}")

            except asyncio.TimeoutError:
                print("\n  ❌ TIMEOUT: No message received for 30 seconds")
                return False

    return False


async def test_reject_gate():
    """
    Test that rejecting a gate properly halts the pipeline.
    """
    job_id = str(uuid.uuid4())
    uri = f"{WS_URL}/{job_id}"

    print("\n" + "=" * 60)
    print(f"Test: Gate Rejection | Job ID: {job_id}")
    print("=" * 60)

    async with websockets.connect(uri) as ws:
        await ws.send(json.dumps({
            "type": "start",
            "repo_url": TEST_REPO,
        }))

        while True:
            msg = await asyncio.wait_for(ws.recv(), timeout=30.0)
            data = json.loads(msg)

            if data.get("type") == "interrupt":
                print(f"\n  ⏸️  Gate reached: {data['gate']}")
                print(f"  ❌ Rejecting gate...")
                await ws.send(json.dumps({
                    "type": "resume",
                    "data": {
                        "approved": False,
                        "comment": "Rejected by test",
                        "approved_by": "test-client@devguard.ai",
                    }
                }))

            elif data.get("type") == "completed":
                status = data.get("final_status")
                print(f"\n  🏁 Final status: {status}")
                return status == "rejected"

            elif data.get("type") == "progress":
                print(f"  📊 {data['node']:20s} | {data['status']}")

            elif data.get("type") == "error":
                print(f"  ❌ Error: {data.get('message')}")
                return False


async def test_multiple_clients_same_job():
    """
    Test that multiple clients can listen to the same job.
    """
    job_id = str(uuid.uuid4())
    uri = f"{WS_URL}/{job_id}"

    print("\n" + "=" * 60)
    print(f"Test: Multi-Client | Job ID: {job_id}")
    print("=" * 60)

    listener_events = []

    async def listener_client():
        async with websockets.connect(uri) as ws:
            count = 0
            while True:
                msg = await asyncio.wait_for(ws.recv(), timeout=30.0)
                data = json.loads(msg)
                if data.get("type") in ("progress", "completed"):
                    count += 1
                if data.get("type") == "completed":
                    listener_events.append(count)
                    return

    async def controller_client():
        async with websockets.connect(uri) as ws:
            await ws.send(json.dumps({"type": "start", "repo_url": TEST_REPO}))
            while True:
                msg = await asyncio.wait_for(ws.recv(), timeout=30.0)
                data = json.loads(msg)
                if data.get("type") == "interrupt":
                    await ws.send(json.dumps({
                        "type": "resume",
                        "data": {"approved": True, "comment": "OK", "approved_by": "test"}
                    }))
                if data.get("type") == "completed":
                    return

    # Start 1 controller + 2 listeners
    await asyncio.gather(
        controller_client(),
        listener_client(),
        listener_client(),
    )

    print(f"\n  ✅ Listener 1 received {listener_events[0]} events")
    print(f"  ✅ Listener 2 received {listener_events[1]} events")
    return len(listener_events) == 2 and all(e > 0 for e in listener_events)


async def main():
    """Run all tests sequentially."""
    print("\n" + "🚀" * 30)
    print("DevGuard AI - WebSocket Protocol Tests")
    print("🚀" * 30)

    results = []

    # Test 1: Full pipeline with auto-approve
    try:
        ok = await test_full_pipeline_auto_approve()
        results.append(("Full pipeline (auto-approve)", ok))
    except Exception as e:
        print(f"\n  💥 Test crashed: {e}")
        results.append(("Full pipeline (auto-approve)", False))

    # Test 2: Gate rejection
    try:
        ok = await test_reject_gate()
        results.append(("Gate rejection", ok))
    except Exception as e:
        print(f"\n  💥 Test crashed: {e}")
        results.append(("Gate rejection", False))

    # Test 3: Multiple listeners
    try:
        ok = await test_multiple_clients_same_job()
        results.append(("Multi-client listeners", ok))
    except Exception as e:
        print(f"\n  💥 Test crashed: {e}")
        results.append(("Multi-client listeners", False))

    # Summary
    print("\n" + "=" * 60)
    print("TEST SUMMARY")
    print("=" * 60)
    for name, ok in results:
        status = "✅ PASS" if ok else "❌ FAIL"
        print(f"  {status:10s} {name}")

    passed = sum(1 for _, ok in results if ok)
    total = len(results)
    print(f"\n  Total: {passed}/{total} passed")

    return passed == total


if __name__ == "__main__":
    success = asyncio.run(main())
    exit(0 if success else 1)
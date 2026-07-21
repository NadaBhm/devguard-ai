"""
DevGuard AI - WebSocket Server (Mini FastAPI)
Real-time progress streaming + human gate interaction for the orchestrator.

Owner: Hbib (Subgroup 2 - Execution & Control)
Sprint: 1 (Foundation & Mock Agents)
CDC Reference: US-1.3.3, T-1.10

This is a STANDALONE mini-server for Sprint 1 testing.
In Sprint 2+, this logic moves into backend/routes/websocket.py (Oussema's FastAPI).

CHANGELOG:
- v1.0.0: Initial implementation - stream(), interrupt handling, resume via WS
- v1.0.1: Fixed imports (absolute instead of relative for Windows compatibility)
- v1.0.2: Removed duplicate import block
- v1.0.3: Fixed broadcast → send_to_job (manager.broadcast() doesn't take job_id)
- v1.0.3: Fixed websocket.send_json for direct client messages (ping, validation errors)
- v1.0.3: Fixed is_running flag handling for resume after interrupt
"""

import logging
import asyncio
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse

from langgraph.types import Command

# =============================================================================
# IMPORTS ABSOLUS (corrigé pour Windows)
# =============================================================================

import sys
import os

# Calcule le chemin absolu vers le dossier 'orchestrator' (même dossier que ce fichier)
_orchestrator_dir = os.path.dirname(os.path.abspath(__file__))
# Remonte pour avoir src/
_src_dir = os.path.dirname(os.path.dirname(_orchestrator_dir))
# Racine du projet
_project_root = os.path.dirname(_src_dir)

print(f"[DEBUG] orchestrator_dir: {_orchestrator_dir}")
print(f"[DEBUG] src_dir: {_src_dir}")
print(f"[DEBUG] project_root: {_project_root}")

# Ajoute src/ au path pour que 'subgroup2' soit trouvable
if _src_dir not in sys.path:
    sys.path.insert(0, _src_dir)

# Import direct — subgroup2 est maintenant dans src/ qui est dans sys.path
from subgroup2.orchestrator.graph import (
    get_orchestrator_graph,
    create_initial_state,
)
from subgroup2.orchestrator.websocket_manager import manager

logger = logging.getLogger(__name__)

# =============================================================================
# FASTAPI APP
# =============================================================================

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifespan handler - build graph once at startup."""
    logger.info("Building orchestrator graph at WS server startup...")
    _ = get_orchestrator_graph()  # Ensure singleton is built
    logger.info("WS server ready")
    yield
    logger.info("WS server shutting down...")


app = FastAPI(
    title="DevGuard AI - WebSocket Test Server",
    version="1.0.3-sprint1",
    lifespan=lifespan,
)


# =============================================================================
# WEBSOCKET PROTOCOL
# =============================================================================
# 
# Server → Client messages:
#   {"type": "progress", "job_id": "...", "node": "codesec_agent", "status": "analyzing", "timestamp": "..."}
#   {"type": "interrupt", "job_id": "...", "gate": "gate_1_pre_infracost", "context": {...}, "actions": ["approve","reject"], "timestamp": "..."}
#   {"type": "error", "job_id": "...", "message": "...", "timestamp": "..."}
#   {"type": "completed", "job_id": "...", "final_status": "completed|failed|rejected", "timestamp": "..."}
#
# Client → Server messages:
#   {"type": "start", "repo_url": "https://github.com/..."}           # Start new job
#   {"type": "resume", "data": {"approved": true, "comment": "...", "approved_by": "..."}}  # Resume from gate
#   {"type": "ping"}                                                   # Keep-alive (optional)
#
# =============================================================================


@app.websocket("/ws/jobs/{job_id}")
async def job_websocket(websocket: WebSocket, job_id: str):
    """
    WebSocket endpoint for real-time job progress.

    Flow:
    1. Client connects with job_id
    2. Client sends {"type": "start", "repo_url": "..."}
    3. Server streams progress events as LangGraph executes
    4. On interrupt (human gate), server sends {"type": "interrupt", ...}
    5. Client sends {"type": "resume", ...} → server resumes graph
    6. Continue until completion or failure
    """
    await manager.connect(websocket, job_id)

    graph = get_orchestrator_graph()
    config = {"configurable": {"thread_id": job_id}}

    # State tracking for this connection
    current_state = None
    is_running = False

    try:
        while True:
            # Wait for client message
            message = await websocket.receive_json()
            msg_type = message.get("type")

            # -----------------------------------------------------------------
            # START: Launch a new workflow for this job_id
            # -----------------------------------------------------------------
            if msg_type == "start":
                if is_running:
                    await websocket.send_json({
                        "type": "error",
                        "job_id": job_id,
                        "message": "Job already running for this connection",
                    })
                    continue

                repo_url = message.get("repo_url", "https://github.com/example/repo")
                current_state = create_initial_state(repo_url)
                current_state["job_id"] = job_id  # Override with WS path param

                is_running = True
                logger.info(f"[WS] Starting job {job_id} for repo: {repo_url}")

                # Stream the graph execution
                await _stream_graph(graph, current_state, config, websocket, job_id)
                is_running = False

            # -----------------------------------------------------------------
            # RESUME: Resume from a human gate interrupt
            # -----------------------------------------------------------------
            elif msg_type == "resume":
                resume_data = message.get("data", {})
                logger.info(f"[WS] Resuming job {job_id} with data: {resume_data}")
                
                # On reprend même si is_running est False (on vient d'un interrupt)
                await _stream_graph_resume(graph, resume_data, config, websocket, job_id)
                is_running = False

            # -----------------------------------------------------------------
            # PING: Keep-alive
            # -----------------------------------------------------------------
            elif msg_type == "ping":
                await websocket.send_json({
                    "type": "pong",
                    "job_id": job_id,
                })

            else:
                await websocket.send_json({
                    "type": "error",
                    "job_id": job_id,
                    "message": f"Unknown message type: {msg_type}",
                })

    except WebSocketDisconnect:
        logger.info(f"[WS] Client disconnected from job {job_id}")
    except Exception as e:
        logger.error(f"[WS] Unexpected error for job {job_id}: {e}")
        try:
            await manager.send_to_job(job_id, {
                "type": "error",
                "job_id": job_id,
                "message": f"Server error: {str(e)}",
            })
        except:
            pass  # Client already gone
    finally:
        manager.disconnect(websocket, job_id)
        logger.info(f"[WS] Connection closed for job {job_id}")


# =============================================================================
# STREAMING HELPERS
# =============================================================================

async def _stream_graph(graph, state, config, websocket, job_id):
    """
    Stream LangGraph events to ALL WebSocket clients for this job.
    Uses manager.send_to_job() so listeners receive progress too.
    """
    from datetime import datetime, timezone

    try:
        for event in graph.stream(state, config):
            for node_name, node_state in event.items():
                
                # ---------------------------------------------------------
                # INTERRUPT: Human gate triggered
                # ---------------------------------------------------------
                if node_name == "__interrupt__":
                    interrupt_obj = node_state[0]
                    interrupt_data = interrupt_obj.value
                    
                    msg = {
                        "type": "interrupt",
                        "job_id": job_id,
                        "gate": interrupt_data.get("gate"),
                        "message": interrupt_data.get("message"),
                        "context": interrupt_data.get("context"),
                        "actions": interrupt_data.get("actions"),
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    }
                    await manager.send_to_job(job_id, msg)
                    logger.info(f"[WS] Interrupt sent for job {job_id} at gate {interrupt_data.get('gate')}")
                    return

                # ---------------------------------------------------------
                # PROGRESS: Normal node completion
                # ---------------------------------------------------------
                else:
                    status = node_state.get("status", "unknown")
                    msg = {
                        "type": "progress",
                        "job_id": job_id,
                        "node": node_name,
                        "status": status,
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    }
                    await manager.send_to_job(job_id, msg)
                    logger.debug(f"[WS] Progress: {node_name} | status: {status}")

        # ---------------------------------------------------------
        # COMPLETED: Graph finished
        # ---------------------------------------------------------
        final_state = graph.get_state(config)
        final_values = final_state.values if hasattr(final_state, 'values') else {}
        final_status = final_values.get("status", "unknown")

        msg = {
            "type": "completed",
            "job_id": job_id,
            "final_status": final_status,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        await manager.send_to_job(job_id, msg)
        logger.info(f"[WS] Job {job_id} completed with status: {final_status}")

    except Exception as e:
        logger.error(f"[WS] Graph streaming error for job {job_id}: {e}")
        import traceback
        logger.error(traceback.format_exc())
        await manager.send_to_job(job_id, {
            "type": "error",
            "job_id": job_id,
            "message": f"Pipeline error: {str(e)}",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })


async def _stream_graph_resume(graph, resume_data, config, websocket, job_id):
    """
    Resume graph from interrupt and stream remaining events to ALL clients.
    """
    from datetime import datetime, timezone

    try:
        for event in graph.stream(Command(resume=resume_data), config):
            for node_name, node_state in event.items():

                if node_name == "__interrupt__":
                    interrupt_obj = node_state[0]
                    interrupt_data = interrupt_obj.value
                    
                    msg = {
                        "type": "interrupt",
                        "job_id": job_id,
                        "gate": interrupt_data.get("gate"),
                        "message": interrupt_data.get("message"),
                        "context": interrupt_data.get("context"),
                        "actions": interrupt_data.get("actions"),
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    }
                    await manager.send_to_job(job_id, msg)
                    logger.info(f"[WS] Interrupt sent for job {job_id} at gate {interrupt_data.get('gate')}")
                    return

                else:
                    status = node_state.get("status", "unknown")
                    msg = {
                        "type": "progress",
                        "job_id": job_id,
                        "node": node_name,
                        "status": status,
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    }
                    await manager.send_to_job(job_id, msg)

        final_state = graph.get_state(config)
        final_values = final_state.values if hasattr(final_state, 'values') else {}
        final_status = final_values.get("status", "unknown")

        msg = {
            "type": "completed",
            "job_id": job_id,
            "final_status": final_status,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        await manager.send_to_job(job_id, msg)
        logger.info(f"[WS] Job {job_id} resumed and completed with status: {final_status}")

    except Exception as e:
        logger.error(f"[WS] Graph resume error for job {job_id}: {e}")
        import traceback
        logger.error(traceback.format_exc())
        await manager.send_to_job(job_id, {
            "type": "error",
            "job_id": job_id,
            "message": f"Resume error: {str(e)}",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })

# =============================================================================
# HEALTH / INFO ENDPOINTS
# =============================================================================

@app.get("/")
async def root():
    return {
        "service": "DevGuard AI - WebSocket Test Server",
        "version": "1.0.3-sprint1",
        "endpoints": {
            "websocket": "/ws/jobs/{job_id}",
            "health": "/health",
        }
    }


@app.get("/health")
async def health():
    return {
        "status": "healthy",
        "active_jobs": manager.get_all_jobs(),
        "total_listeners": sum(
            manager.get_listener_count(jid) for jid in manager.get_all_jobs()
        ),
    }


# =============================================================================
# MAIN (for standalone testing)
# =============================================================================

if __name__ == "__main__":
    import uvicorn

    print("=" * 60)
    print("DevGuard AI - WebSocket Test Server")
    print("Sprint 1 - T-1.10")
    print("=" * 60)
    print("\nEndpoints:")
    print("  WS: ws://localhost:8001/ws/jobs/{job_id}")
    print("  HTTP: http://localhost:8001/health")
    print("\nStart a job by connecting and sending:")
    print('  {"type": "start", "repo_url": "https://github.com/..."}')
    print("\nPress Ctrl+C to stop\n")

    uvicorn.run(app, host="0.0.0.0", port=8001, log_level="info")
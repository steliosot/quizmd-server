from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse

from .models import (
    ClientEvent,
    CreateRoomRequest,
    CreateRoomResponse,
    JoinByNameRequest,
    JoinRoomRequest,
    JoinRoomResponse,
    RoomSnapshot,
    ServerEvent,
)
from .room_store import RoomManager

logger = logging.getLogger("quizmd-server")
logging.basicConfig(level=logging.INFO)

manager = RoomManager()


@asynccontextmanager
async def lifespan(app: FastAPI):
    cleanup_task = asyncio.create_task(manager.cleanup_stale_rooms())
    try:
        yield
    finally:
        cleanup_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await cleanup_task


app = FastAPI(title="quizmd-server", version="0.1.0", lifespan=lifespan)


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


def _public_base_url(request: Request) -> str:
    configured = os.getenv("QUIZMD_PUBLIC_BASE_URL", "").strip().rstrip("/")
    if configured:
        return configured
    return str(request.base_url).rstrip("/")


def _ws_base_url(public_base: str) -> str:
    if public_base.startswith("https://"):
        return "wss://" + public_base[len("https://"):]
    if public_base.startswith("http://"):
        return "ws://" + public_base[len("http://"):]
    return public_base


@app.post("/rooms", response_model=CreateRoomResponse)
async def create_room(request: Request, payload: CreateRoomRequest) -> CreateRoomResponse:
    created = await manager.create_room(
        mode=payload.mode,
        room_name=payload.room_name,
        quiz_title=payload.quiz_title,
        questions=[q.model_dump() for q in payload.questions],
        host_name=payload.host_name,
        host_role=payload.host_role,
    )

    base = _public_base_url(request)
    ws_base = _ws_base_url(base)
    room_code = created["room_code"]
    room_name = created["room_name"]
    room_token = created["room_token"]

    return CreateRoomResponse(
        room_code=room_code,
        room_name=room_name,
        mode=created["mode"],
        join_url=f"{base}/join/{room_name}",
        ws_url=f"{ws_base}/rooms/{room_code}/ws",
        room_token=room_token,
        host_player_id=created["host_player_id"],
        host_player_token=created["host_player_token"],
        host_display_name=created["host_display_name"],
        host_role=created["host_role"],
    )


@app.post("/rooms/{room_code}/join", response_model=JoinRoomResponse)
async def join_room(room_code: str, request: Request, payload: JoinRoomRequest) -> JoinRoomResponse:
    joined = await manager.join_room(
        room_code=room_code,
        room_token=payload.room_token,
        player_name=payload.player_name,
        role=payload.role,
    )

    ws_base = _ws_base_url(_public_base_url(request))
    return JoinRoomResponse(
        room_code=joined["room_code"],
        room_name=joined["room_name"],
        mode=joined["mode"],
        player_id=joined["player_id"],
        player_token=joined["player_token"],
        display_name=joined["display_name"],
        ws_url=f"{ws_base}/rooms/{room_code}/ws",
        player_role=joined["player_role"],
    )


@app.post("/rooms/by-name/{room_name}/join", response_model=JoinRoomResponse)
async def join_room_by_name(room_name: str, request: Request, payload: JoinByNameRequest) -> JoinRoomResponse:
    joined = await manager.join_room_by_name(
        room_name=room_name,
        player_name=payload.player_name,
        role=payload.role,
    )

    ws_base = _ws_base_url(_public_base_url(request))
    return JoinRoomResponse(
        room_code=joined["room_code"],
        room_name=joined["room_name"],
        mode=joined["mode"],
        player_id=joined["player_id"],
        player_token=joined["player_token"],
        display_name=joined["display_name"],
        ws_url=f"{ws_base}/rooms/{joined['room_code']}/ws",
        player_role=joined["player_role"],
    )


@app.get("/rooms/{room_code}", response_model=RoomSnapshot)
async def get_room(room_code: str) -> RoomSnapshot:
    return manager.get_snapshot(room_code)


@app.get("/join/{room_ref}")
async def join_link_info(room_ref: str) -> JSONResponse:
    """Simple endpoint so shared join URLs resolve with human-readable instructions."""
    try:
        room = manager.resolve_room(room_ref)
        snapshot = manager.get_snapshot(room.room_code)
    except HTTPException as exc:
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})

    return JSONResponse(
        content={
            "room_code": room.room_code,
            "room_name": room.room_name,
            "quiz_title": snapshot.quiz_title,
            "mode": snapshot.mode.value,
            "message": (
                "Use quizmd-client to join: "
                f'python quizmd_client.py room --join "{room.room_name}" --name "YourName"'
            ),
        }
    )


@app.websocket("/rooms/{room_code}/ws")
async def room_ws(room_code: str, websocket: WebSocket):
    await websocket.accept()
    player_id = websocket.query_params.get("player_id", "")
    token = websocket.query_params.get("token", "")

    if not player_id or not token:
        await websocket.send_json(ServerEvent(type="error", payload={"message": "player_id and token are required"}).model_dump())
        await websocket.close(code=1008)
        return

    try:
        manager.validate_player_token(room_code, player_id, token)
    except HTTPException as exc:
        await websocket.send_json(ServerEvent(type="error", payload={"message": exc.detail}).model_dump())
        await websocket.close(code=1008)
        return

    await manager.connect_player(room_code, player_id, websocket)

    try:
        while True:
            raw = await websocket.receive_text()
            try:
                data: dict[str, Any] = json.loads(raw)
                event = ClientEvent.model_validate(data)
            except Exception:
                await manager.send_to_player(room_code, player_id, "error", {"message": "Invalid event payload"})
                continue

            try:
                await manager.handle_event(room_code, player_id, event.type, event.payload)
            except HTTPException as exc:
                await manager.send_to_player(room_code, player_id, "error", {"message": str(exc.detail)})
    except WebSocketDisconnect:
        logger.info("player disconnected room=%s player=%s", room_code, player_id)
    finally:
        await manager.disconnect_player(room_code, player_id)

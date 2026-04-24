from __future__ import annotations

import asyncio
import os
import re
import secrets
import string
import time
from dataclasses import dataclass, field
from typing import Any

from fastapi import HTTPException, WebSocket

from .game_engine import Submission, collaborate_consensus, compete_round_scores
from .models import Mode, PlayerSnapshot, RoomRole, RoomSnapshot, RoomState
from .namegen import ensure_unique_name, generate_funny_name

ROOM_CODE_LEN = 8
ROOM_CODE_ALPHABET = string.ascii_uppercase + string.digits
ROOM_NAME_CITIES = ["london", "athens", "berlin", "madrid", "dublin", "lisbon", "oslo", "rome"]
ROOM_NAME_ANIMALS = ["elephant", "fox", "otter", "panda", "koala", "falcon", "tiger", "whale"]
ROOM_NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value


MAX_PLAYERS = _env_int("ROOM_MAX_PLAYERS", 16)
DEFAULT_QUESTION_TIMEOUT = _env_int("QUESTION_TIMEOUT_SECONDS", 30)
ROOM_TTL_MINUTES = _env_int("ROOM_TTL_MINUTES", 30)
HOST_REJOIN_SECONDS = _env_int("HOST_REJOIN_SECONDS", 60)


@dataclass(slots=True)
class PlayerState:
    player_id: str
    token: str
    name: str
    is_host: bool
    role: RoomRole = RoomRole.participant
    score: int = 0
    ready: bool = False
    connected: bool = False
    last_seen: float = field(default_factory=time.time)


@dataclass(slots=True)
class RoomStateData:
    room_code: str
    room_name: str
    room_token: str
    mode: Mode
    quiz_title: str
    questions: list[dict[str, Any]]
    host_player_id: str
    players: dict[str, PlayerState]
    state: RoomState = RoomState.waiting
    current_question: int = 0
    team_score: int = 0
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    submissions: dict[str, Submission] = field(default_factory=dict)
    round_deadline: float | None = None
    round_timeout_task: asyncio.Task | None = None
    host_grace_task: asyncio.Task | None = None
    paused_remaining_seconds: int | None = None
    boxing_score: int | None = None
    boxing_scored_by: str | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class RoomManager:
    def __init__(self) -> None:
        self.rooms: dict[str, RoomStateData] = {}
        self.room_names: dict[str, str] = {}
        self.connections: dict[str, dict[str, WebSocket]] = {}
        self.global_lock = asyncio.Lock()

    async def create_room(
        self,
        *,
        mode: Mode,
        quiz_title: str,
        questions: list[dict[str, Any]],
        host_name: str,
        host_role: RoomRole | None = None,
        room_name: str = "",
    ) -> dict[str, Any]:
        async with self.global_lock:
            room_code = self._new_room_code()
            normalized_room_name = self._normalize_room_name(room_name)
            if normalized_room_name:
                if normalized_room_name in self.room_names:
                    raise HTTPException(status_code=409, detail="Room name already exists")
                final_room_name = normalized_room_name
            else:
                final_room_name = self._new_room_name()
            room_token = self._new_token()
            host_player_id = self._new_id("p")
            host_token = self._new_token()
            name = host_name.strip() or generate_funny_name()
            resolved_host_role = RoomRole.participant
            if mode == Mode.boxing:
                if host_role in {RoomRole.teacher, RoomRole.student}:
                    resolved_host_role = host_role
                else:
                    resolved_host_role = RoomRole.teacher

            host_player = PlayerState(
                player_id=host_player_id,
                token=host_token,
                name=name,
                is_host=True,
                role=resolved_host_role,
                connected=False,
            )

            room = RoomStateData(
                room_code=room_code,
                room_name=final_room_name,
                room_token=room_token,
                mode=mode,
                quiz_title=quiz_title,
                questions=questions,
                host_player_id=host_player_id,
                players={host_player_id: host_player},
            )
            self.rooms[room_code] = room
            self.room_names[final_room_name] = room_code
            self.connections[room_code] = {}

        return {
            "room_code": room_code,
            "room_name": final_room_name,
            "mode": mode,
            "room_token": room_token,
            "host_player_id": host_player_id,
            "host_player_token": host_token,
            "host_display_name": host_player.name,
            "host_role": host_player.role,
        }

    async def join_room(
        self,
        *,
        room_code: str,
        room_token: str,
        player_name: str,
        role: RoomRole | None = None,
    ) -> dict[str, Any]:
        room = self.rooms.get(room_code)
        if room is None:
            raise HTTPException(status_code=404, detail="Room not found")
        if room.room_token != room_token:
            raise HTTPException(status_code=403, detail="Invalid room token")
        return await self._join_room_internal(room=room, player_name=player_name, role=role)

    async def join_room_by_name(
        self,
        *,
        room_name: str,
        player_name: str,
        role: RoomRole | None = None,
    ) -> dict[str, Any]:
        room_code = self.room_names.get(room_name.lower())
        if room_code is None:
            raise HTTPException(status_code=404, detail="Room not found")
        room = self.rooms.get(room_code)
        if room is None:
            raise HTTPException(status_code=404, detail="Room not found")
        return await self._join_room_internal(room=room, player_name=player_name, role=role)

    async def _join_room_internal(
        self,
        *,
        room: RoomStateData,
        player_name: str,
        role: RoomRole | None = None,
    ) -> dict[str, Any]:
        async with room.lock:
            if room.state == RoomState.finished:
                raise HTTPException(status_code=410, detail="Room already finished")

            if room.mode == Mode.boxing and len(room.players) >= 2:
                raise HTTPException(status_code=409, detail="Room is full (boxing mode allows only 1 teacher + 1 student).")
            if room.mode != Mode.boxing and len(room.players) >= MAX_PLAYERS:
                raise HTTPException(status_code=409, detail="Room is full")

            existing_names = [p.name for p in room.players.values()]
            display_name = ensure_unique_name(player_name.strip(), existing_names)
            player_id = self._new_id("p")
            token = self._new_token()
            assigned_role = RoomRole.participant
            if room.mode == Mode.boxing:
                assigned_role = self._resolve_boxing_join_role(room, role)
            room.players[player_id] = PlayerState(
                player_id=player_id,
                token=token,
                name=display_name,
                is_host=False,
                role=assigned_role,
                connected=False,
            )
            room.updated_at = time.time()

        await self.broadcast(room.room_code, "player_joined", {"name": display_name, "role": assigned_role.value})
        await self.broadcast(room.room_code, "lobby_update", self._snapshot_payload(room))

        return {
            "room_code": room.room_code,
            "room_name": room.room_name,
            "mode": room.mode,
            "player_id": player_id,
            "player_token": token,
            "display_name": display_name,
            "player_role": assigned_role,
        }

    def resolve_room(self, room_ref: str) -> RoomStateData:
        room = self.rooms.get(room_ref)
        if room is not None:
            return room
        room_code = self.room_names.get(room_ref.lower())
        if room_code is None:
            raise HTTPException(status_code=404, detail="Room not found")
        room = self.rooms.get(room_code)
        if room is None:
            raise HTTPException(status_code=404, detail="Room not found")
        return room

    def get_snapshot(self, room_code: str) -> RoomSnapshot:
        room = self.rooms.get(room_code)
        if room is None:
            raise HTTPException(status_code=404, detail="Room not found")
        return self._snapshot_model(room)

    def validate_player_token(self, room_code: str, player_id: str, token: str) -> RoomStateData:
        room = self.rooms.get(room_code)
        if room is None:
            raise HTTPException(status_code=404, detail="Room not found")
        player = room.players.get(player_id)
        if player is None:
            raise HTTPException(status_code=404, detail="Player not found")
        if player.token != token:
            raise HTTPException(status_code=403, detail="Invalid player token")
        return room

    async def connect_player(self, room_code: str, player_id: str, websocket: WebSocket) -> None:
        room = self.rooms[room_code]
        async with room.lock:
            player = room.players[player_id]
            player.connected = True
            player.last_seen = time.time()
            self.connections[room_code][player_id] = websocket

            if player.is_host and room.state == RoomState.paused:
                room.state = RoomState.playing
                if room.host_grace_task and not room.host_grace_task.done():
                    room.host_grace_task.cancel()
                room.host_grace_task = None
                await self._resume_round_timer(room)

        await self.send_to_player(room_code, player_id, "connected", self._snapshot_payload(room))
        await self.broadcast(room_code, "lobby_update", self._snapshot_payload(room))
        if room.state == RoomState.playing and player.is_host:
            await self.send_current_question(room_code, player_id)

    async def disconnect_player(self, room_code: str, player_id: str) -> None:
        room = self.rooms.get(room_code)
        if room is None:
            return

        async with room.lock:
            player = room.players.get(player_id)
            if player is None:
                return
            player.connected = False
            player.ready = False
            player.last_seen = time.time()
            self.connections.get(room_code, {}).pop(player_id, None)

            if player.is_host and room.state == RoomState.playing:
                room.state = RoomState.paused
                await self._pause_round_timer(room)
                room.host_grace_task = asyncio.create_task(self._host_grace_watcher(room.room_code))

        await self.broadcast(room_code, "player_left", {"name": player.name, "role": player.role.value})
        await self.broadcast(room_code, "lobby_update", self._snapshot_payload(room))

    async def handle_event(self, room_code: str, player_id: str, event_type: str, payload: dict[str, Any]) -> None:
        room = self.rooms.get(room_code)
        if room is None:
            return

        if event_type == "ping":
            await self.send_to_player(room_code, player_id, "pong", {})
            return

        if event_type == "leave_room":
            await self.disconnect_player(room_code, player_id)
            return

        if event_type == "ready_toggle":
            await self._handle_ready_toggle(room, player_id, payload)
            return

        if event_type == "start_game":
            await self._handle_start_game(room, player_id)
            return

        if event_type == "chat_message":
            await self._handle_chat_message(room, player_id, payload)
            return

        if event_type == "set_score":
            await self._handle_set_score(room, player_id, payload)
            return

        if event_type == "end_session":
            await self._handle_end_session(room, player_id, payload)
            return

        if event_type == "submit_answer":
            await self._handle_submit_answer(room, player_id, payload)
            return

        await self.send_to_player(room_code, player_id, "error", {"message": f"Unknown event: {event_type}"})

    async def _handle_ready_toggle(self, room: RoomStateData, player_id: str, payload: dict[str, Any]) -> None:
        ready = bool(payload.get("ready", True))
        async with room.lock:
            if room.state not in {RoomState.waiting, RoomState.paused}:
                return
            room.players[player_id].ready = ready
            room.updated_at = time.time()

        await self.broadcast(room.room_code, "lobby_update", self._snapshot_payload(room))

    async def _handle_start_game(self, room: RoomStateData, player_id: str) -> None:
        async with room.lock:
            if room.players[player_id].is_host is False:
                raise HTTPException(status_code=403, detail="Only host can start game")
            if room.state not in {RoomState.waiting, RoomState.paused}:
                return

            connected_players = [p for p in room.players.values() if p.connected]
            if len(connected_players) > 1 and not all(p.ready for p in connected_players):
                await self.send_to_player(
                    room.room_code,
                    player_id,
                    "error",
                    {"message": "All connected players must be ready before starting."},
                )
                return

            if room.mode == Mode.boxing:
                connected_teachers = [
                    p for p in connected_players if p.role == RoomRole.teacher
                ]
                connected_students = [
                    p for p in connected_players if p.role == RoomRole.student
                ]
                if len(connected_teachers) != 1 or len(connected_students) != 1:
                    await self.send_to_player(
                        room.room_code,
                        player_id,
                        "error",
                        {
                            "message": (
                                "Boxing mode requires exactly 1 connected teacher and 1 connected student."
                            )
                        },
                    )
                    return

            room.state = RoomState.playing
            room.current_question = 0
            room.team_score = 0
            room.submissions = {}
            room.boxing_score = None
            room.boxing_scored_by = None
            for p in room.players.values():
                p.score = 0
                p.ready = False
            room.updated_at = time.time()

        await self.broadcast(room.room_code, "game_started", self._snapshot_payload(room))
        if room.mode == Mode.boxing:
            return
        await self._open_question(room, room.current_question)

    async def _handle_chat_message(self, room: RoomStateData, player_id: str, payload: dict[str, Any]) -> None:
        text = str(payload.get("text", "")).strip()
        if not text:
            return
        if len(text) > 500:
            text = text[:500]
        player = room.players.get(player_id)
        if player is None:
            return
        await self.broadcast(
            room.room_code,
            "chat_message",
            {
                "from": player.name,
                "from_role": player.role.value,
                "text": text,
                "ts": time.time(),
            },
        )

    async def _handle_set_score(self, room: RoomStateData, player_id: str, payload: dict[str, Any]) -> None:
        if room.mode != Mode.boxing:
            await self.send_to_player(room.room_code, player_id, "error", {"message": "Score command is only available in boxing mode."})
            return
        player = room.players.get(player_id)
        if player is None:
            return
        if player.role != RoomRole.teacher:
            await self.send_to_player(room.room_code, player_id, "error", {"message": "Only the teacher can set score in boxing mode."})
            return
        if room.state != RoomState.playing:
            await self.send_to_player(room.room_code, player_id, "error", {"message": "Session is not active yet. Use /start first."})
            return
        try:
            score = int(payload.get("score"))
        except (TypeError, ValueError):
            await self.send_to_player(room.room_code, player_id, "error", {"message": "Score must be an integer 0-100."})
            return
        if score < 0 or score > 100:
            await self.send_to_player(room.room_code, player_id, "error", {"message": "Score must be between 0 and 100."})
            return

        async with room.lock:
            room.boxing_score = score
            room.boxing_scored_by = player_id
            room.updated_at = time.time()

        await self.broadcast(
            room.room_code,
            "boxing_score",
            {
                "score": score,
                "by": player.name,
                "by_role": player.role.value,
                "ts": time.time(),
            },
        )

    async def _handle_end_session(self, room: RoomStateData, player_id: str, payload: dict[str, Any]) -> None:
        if room.mode != Mode.boxing:
            await self.send_to_player(room.room_code, player_id, "error", {"message": "Session end command is only available in boxing mode."})
            return
        player = room.players.get(player_id)
        if player is None:
            return

        async with room.lock:
            if room.state == RoomState.finished:
                return
            room.state = RoomState.finished
            room.updated_at = time.time()
            await self._cancel_round_timer(room)
            scored_by_name = ""
            if room.boxing_scored_by:
                scorer = room.players.get(room.boxing_scored_by)
                if scorer:
                    scored_by_name = scorer.name
            payload_data = {
                **self._snapshot_payload(room),
                "reason": str(payload.get("reason") or "Session ended by participant."),
                "ended_by": player.name,
                "ended_by_role": player.role.value,
                "final_score": room.boxing_score,
                "scored_by": scored_by_name,
            }

        await self.broadcast(room.room_code, "game_finished", payload_data)

    async def _handle_submit_answer(self, room: RoomStateData, player_id: str, payload: dict[str, Any]) -> None:
        if room.mode == Mode.boxing:
            await self.send_to_player(room.room_code, player_id, "error", {"message": "Answer submissions are not used in boxing mode."})
            return
        answers = payload.get("answers", [])
        if not isinstance(answers, list):
            await self.send_to_player(room.room_code, player_id, "error", {"message": "answers must be a list"})
            return

        normalized: list[int] = []
        try:
            for item in answers:
                normalized.append(int(item))
        except (TypeError, ValueError):
            await self.send_to_player(room.room_code, player_id, "error", {"message": "answers must be integer indexes"})
            return

        q_idx = int(payload.get("question_index", room.current_question))

        async with room.lock:
            if room.state != RoomState.playing:
                return
            if q_idx != room.current_question:
                await self.send_to_player(
                    room.room_code,
                    player_id,
                    "error",
                    {"message": f"Question mismatch. Current question is {room.current_question}."},
                )
                return
            if player_id in room.submissions:
                return

            room.submissions[player_id] = Submission(player_id=player_id, answers=sorted(set(normalized)), ts=time.time())
            room.updated_at = time.time()

            active = self._active_player_ids(room)
            everyone_answered = all(pid in room.submissions for pid in active)

        if room.mode == Mode.compete and everyone_answered:
            await self._finalize_compete_round(room, reason="all_answered")
            return

        if room.mode == Mode.collaborate and everyone_answered:
            await self._resolve_collaborate_round(room, reason="all_answered")
            return

    async def _open_question(self, room: RoomStateData, question_index: int) -> None:
        async with room.lock:
            if question_index >= len(room.questions):
                room.state = RoomState.finished
                await self._cancel_round_timer(room)
                await self.broadcast(room.room_code, "game_finished", self._snapshot_payload(room))
                return

            room.current_question = question_index
            room.submissions = {}
            timeout = room.questions[question_index].get("time_limit") or DEFAULT_QUESTION_TIMEOUT
            room.round_deadline = time.time() + max(5, int(timeout))
            room.updated_at = time.time()
            await self._cancel_round_timer(room)
            room.round_timeout_task = asyncio.create_task(self._round_timeout_watcher(room.room_code, question_index))

            payload = {
                "question_index": room.current_question,
                "total_questions": len(room.questions),
                "mode": room.mode.value,
                "question": self._question_public_payload(room.questions[room.current_question]),
                "deadline_epoch": room.round_deadline,
            }

        await self.broadcast(room.room_code, "question", payload)

    async def _finalize_compete_round(self, room: RoomStateData, reason: str) -> None:
        async with room.lock:
            if room.state != RoomState.playing:
                return
            q = room.questions[room.current_question]
            active = self._active_player_ids(room)
            deltas, correctness = compete_round_scores(
                question=q,
                submissions=room.submissions,
                active_player_ids=active,
            )
            for pid, delta in deltas.items():
                room.players[pid].score += delta

            round_players = []
            for pid in active:
                sub = room.submissions.get(pid)
                round_players.append(
                    {
                        "player_id": pid,
                        "name": room.players[pid].name,
                        "answers": sub.answers if sub else [],
                        "is_correct": correctness.get(pid, False),
                        "delta": deltas.get(pid, 0),
                        "score": room.players[pid].score,
                    }
                )

            payload = {
                "reason": reason,
                "question_index": room.current_question,
                "correct_indexes": q.get("correct", []),
                "players": round_players,
            }

            room.current_question += 1
            room.updated_at = time.time()

        await self.broadcast(room.room_code, "round_result", payload)
        await self.broadcast(room.room_code, "scoreboard", self._scoreboard_payload(room))
        await self._open_question(room, room.current_question)

    async def _resolve_collaborate_round(self, room: RoomStateData, reason: str) -> None:
        async with room.lock:
            if room.state != RoomState.playing:
                return
            q = room.questions[room.current_question]
            active = self._active_player_ids(room)
            passed, correctness, missing = collaborate_consensus(
                question=q,
                submissions=room.submissions,
                active_player_ids=active,
            )

            if passed:
                room.team_score += 1
                payload = {
                    "reason": reason,
                    "question_index": room.current_question,
                    "status": "passed",
                    "team_score": room.team_score,
                    "correct_indexes": q.get("correct", []),
                    "players": [
                        {
                            "player_id": pid,
                            "name": room.players[pid].name,
                            "answers": room.submissions.get(pid).answers if room.submissions.get(pid) else [],
                            "is_correct": correctness.get(pid, False),
                        }
                        for pid in active
                    ],
                }
                room.current_question += 1
                room.updated_at = time.time()
            else:
                wrong_names = [room.players[pid].name for pid in active if not correctness.get(pid, False)]
                missing_names = [room.players[pid].name for pid in missing]
                payload = {
                    "reason": reason,
                    "question_index": room.current_question,
                    "status": "retry",
                    "message": "Not consensus, try again",
                    "wrong_names": wrong_names,
                    "missing_names": missing_names,
                }
                room.updated_at = time.time()

        if payload["status"] == "passed":
            await self.broadcast(room.room_code, "round_result", payload)
            await self.broadcast(room.room_code, "scoreboard", self._scoreboard_payload(room))
            await self._open_question(room, room.current_question)
            return

        await self.broadcast(room.room_code, "consensus_retry", payload)
        await self._open_question(room, room.current_question)

    async def _round_timeout_watcher(self, room_code: str, question_index: int) -> None:
        try:
            while True:
                room = self.rooms.get(room_code)
                if room is None:
                    return
                async with room.lock:
                    if room.state != RoomState.playing:
                        return
                    if room.current_question != question_index:
                        return
                    deadline = room.round_deadline
                    if deadline is None:
                        return
                    remaining = deadline - time.time()
                if remaining <= 0:
                    break
                await asyncio.sleep(min(1.0, remaining))

            room = self.rooms.get(room_code)
            if room is None:
                return
            if room.mode == Mode.compete:
                await self._finalize_compete_round(room, reason="timeout")
            elif room.mode == Mode.collaborate:
                await self._resolve_collaborate_round(room, reason="timeout")
        except asyncio.CancelledError:
            return

    async def _host_grace_watcher(self, room_code: str) -> None:
        try:
            await asyncio.sleep(HOST_REJOIN_SECONDS)
            room = self.rooms.get(room_code)
            if room is None:
                return
            async with room.lock:
                host = room.players.get(room.host_player_id)
                if host is None:
                    return
                if host.connected:
                    return
                room.state = RoomState.finished
                await self._cancel_round_timer(room)
            await self.broadcast(room_code, "game_finished", {
                **self._snapshot_payload(room),
                "reason": "Host did not reconnect in time.",
            })
        except asyncio.CancelledError:
            return

    async def _pause_round_timer(self, room: RoomStateData) -> None:
        if room.mode == Mode.boxing:
            return
        if room.round_deadline is not None:
            room.paused_remaining_seconds = max(1, int(room.round_deadline - time.time()))
        else:
            room.paused_remaining_seconds = None
        await self._cancel_round_timer(room)

    async def _resume_round_timer(self, room: RoomStateData) -> None:
        if room.mode == Mode.boxing:
            return
        if room.state != RoomState.playing:
            return
        if room.current_question >= len(room.questions):
            return
        remaining = room.paused_remaining_seconds or (room.questions[room.current_question].get("time_limit") or DEFAULT_QUESTION_TIMEOUT)
        room.round_deadline = time.time() + max(1, int(remaining))
        room.round_timeout_task = asyncio.create_task(self._round_timeout_watcher(room.room_code, room.current_question))
        room.paused_remaining_seconds = None
        await self.broadcast(room.room_code, "host_reconnected", self._snapshot_payload(room))

    async def _cancel_round_timer(self, room: RoomStateData) -> None:
        task = room.round_timeout_task
        room.round_timeout_task = None
        if task and not task.done():
            task.cancel()

    async def send_current_question(self, room_code: str, player_id: str) -> None:
        room = self.rooms.get(room_code)
        if room is None or room.state != RoomState.playing:
            return
        if room.mode == Mode.boxing:
            return
        if room.current_question >= len(room.questions):
            return
        payload = {
            "question_index": room.current_question,
            "total_questions": len(room.questions),
            "mode": room.mode.value,
            "question": self._question_public_payload(room.questions[room.current_question]),
            "deadline_epoch": room.round_deadline,
        }
        await self.send_to_player(room_code, player_id, "question", payload)

    async def send_to_player(self, room_code: str, player_id: str, event_type: str, payload: dict[str, Any]) -> None:
        ws = self.connections.get(room_code, {}).get(player_id)
        if ws is None:
            return
        try:
            await ws.send_json({"type": event_type, "payload": payload})
        except Exception:
            pass

    async def broadcast(self, room_code: str, event_type: str, payload: dict[str, Any]) -> None:
        room_connections = list(self.connections.get(room_code, {}).items())
        stale: list[str] = []
        for player_id, ws in room_connections:
            try:
                await ws.send_json({"type": event_type, "payload": payload})
            except Exception:
                stale.append(player_id)

        if stale:
            room = self.rooms.get(room_code)
            if room is None:
                return
            async with room.lock:
                for player_id in stale:
                    self.connections.get(room_code, {}).pop(player_id, None)
                    player = room.players.get(player_id)
                    if player:
                        player.connected = False

    async def cleanup_stale_rooms(self) -> None:
        while True:
            await asyncio.sleep(30)
            cutoff = time.time() - (ROOM_TTL_MINUTES * 60)
            to_remove: list[str] = []
            async with self.global_lock:
                for code, room in self.rooms.items():
                    if room.updated_at < cutoff:
                        to_remove.append(code)
                for code in to_remove:
                    room = self.rooms.pop(code, None)
                    self.connections.pop(code, None)
                    if room:
                        self.room_names.pop(room.room_name.lower(), None)
                        await self._cancel_round_timer(room)
                        if room.host_grace_task and not room.host_grace_task.done():
                            room.host_grace_task.cancel()

    def _active_player_ids(self, room: RoomStateData) -> list[str]:
        return [pid for pid, player in room.players.items() if player.connected]

    def _snapshot_model(self, room: RoomStateData) -> RoomSnapshot:
        players = [
            PlayerSnapshot(
                player_id=p.player_id,
                name=p.name,
                score=p.score,
                ready=p.ready,
                connected=p.connected,
                is_host=p.is_host,
                role=p.role,
            )
            for p in room.players.values()
        ]
        players.sort(key=lambda p: (not p.connected, p.name.lower()))
        return RoomSnapshot(
            room_code=room.room_code,
            mode=room.mode,
            state=room.state,
            quiz_title=room.quiz_title,
            current_question=room.current_question,
            total_questions=len(room.questions),
            team_score=room.team_score,
            players=players,
        )

    def _snapshot_payload(self, room: RoomStateData) -> dict[str, Any]:
        snapshot = self._snapshot_model(room)
        return snapshot.model_dump()

    def _scoreboard_payload(self, room: RoomStateData) -> dict[str, Any]:
        players = [
            {
                "player_id": p.player_id,
                "name": p.name,
                "score": p.score,
                "role": p.role.value,
            }
            for p in room.players.values()
        ]
        players.sort(key=lambda x: x["score"], reverse=True)
        return {
            "mode": room.mode.value,
            "team_score": room.team_score,
            "players": players,
        }

    def _question_public_payload(self, question: dict[str, Any]) -> dict[str, Any]:
        return {
            "title": question.get("title", "Question"),
            "question": question.get("question", ""),
            "options": question.get("options", []),
            "type": question.get("type", "single"),
            "time_limit": question.get("time_limit") or DEFAULT_QUESTION_TIMEOUT,
        }

    def _new_room_code(self) -> str:
        while True:
            code = "".join(secrets.choice(ROOM_CODE_ALPHABET) for _ in range(ROOM_CODE_LEN))
            if code not in self.rooms:
                return code

    def _new_room_name(self) -> str:
        existing = set(self.room_names.keys())
        for _ in range(300):
            city = secrets.choice(ROOM_NAME_CITIES)
            animal = secrets.choice(ROOM_NAME_ANIMALS)
            candidate = f"{city}-{animal}"
            if candidate not in existing:
                return candidate
        suffix = 2
        while True:
            city = secrets.choice(ROOM_NAME_CITIES)
            animal = secrets.choice(ROOM_NAME_ANIMALS)
            candidate = f"{city}-{animal}-{suffix}"
            if candidate not in existing:
                return candidate
            suffix += 1

    def _normalize_room_name(self, value: str) -> str:
        candidate = (value or "").strip().lower()
        if not candidate:
            return ""
        candidate = candidate.replace("_", "-").replace(" ", "-")
        candidate = re.sub(r"-{2,}", "-", candidate).strip("-")
        if len(candidate) < 3 or len(candidate) > 40:
            raise HTTPException(status_code=422, detail="room_name must be 3-40 chars")
        if not ROOM_NAME_RE.fullmatch(candidate):
            raise HTTPException(status_code=422, detail="room_name must be lowercase letters, numbers, and hyphens")
        return candidate

    def _resolve_boxing_join_role(self, room: RoomStateData, requested_role: RoomRole | None) -> RoomRole:
        if requested_role == RoomRole.participant:
            raise HTTPException(status_code=422, detail="Boxing mode role must be teacher or student")
        taken_roles = {p.role for p in room.players.values() if p.role in {RoomRole.teacher, RoomRole.student}}
        if requested_role in {RoomRole.teacher, RoomRole.student}:
            if requested_role in taken_roles:
                raise HTTPException(
                    status_code=409,
                    detail=f"Role '{requested_role.value}' is already taken in this boxing room.",
                )
            return requested_role
        if RoomRole.teacher not in taken_roles:
            return RoomRole.teacher
        if RoomRole.student not in taken_roles:
            return RoomRole.student
        raise HTTPException(status_code=409, detail="Room is full (boxing mode allows only 1 teacher + 1 student).")

    @staticmethod
    def _new_token() -> str:
        return secrets.token_urlsafe(24)

    @staticmethod
    def _new_id(prefix: str) -> str:
        return f"{prefix}_{secrets.token_hex(6)}"

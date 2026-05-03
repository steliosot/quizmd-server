from __future__ import annotations

import asyncio
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

# Allow running server tests from the repository root via `pytest`.
SERVER_ROOT = Path(__file__).resolve().parents[1]
if str(SERVER_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVER_ROOT))

from app import room_store as room_store_module
from app.game_engine import Submission, collaborate_consensus, compete_round_scores
from app.main import app, manager
from app.room_store import RoomManager


def sample_quiz_payload(mode: str = "compete", token_required: bool = False):
    discussion_time = 0 if mode == "collaborate" else None
    return {
        "mode": mode,
        "room_name": "",
        "token_required": token_required,
        "quiz_title": "Sample Quiz",
        "host_name": "Hosty",
        "questions": [
            {
                "title": "Question 1",
                "question": "2+2?",
                "options": ["3", "4", "5", "6"],
                "correct": [2],
                "type": "single",
                "time_limit": 30,
                "discussion_time": discussion_time,
                "explanation": "2+2 is 4",
            }
        ],
    }


def sample_two_question_collaborate_payload():
    return {
        "mode": "collaborate",
        "room_name": "",
        "quiz_title": "Two Question Collaborate",
        "host_name": "Hosty",
        "questions": [
            {
                "title": "Question 1",
                "question": "2+2?",
                "options": ["3", "4", "5", "6"],
                "correct": [2],
                "type": "single",
                "time_limit": 30,
                "discussion_time": 0,
                "explanation": "2+2 is 4",
            },
            {
                "title": "Question 2",
                "question": "3+3?",
                "options": ["5", "6", "7", "8"],
                "correct": [2],
                "type": "single",
                "time_limit": 30,
                "discussion_time": 0,
                "explanation": "3+3 is 6",
            },
        ],
    }


class ServerTests(unittest.TestCase):
    def setUp(self):
        manager.rooms.clear()
        manager.room_names.clear()
        manager.connections.clear()
        manager.start_countdown_seconds = 0
        self.client = TestClient(app)

    def test_create_and_join_room(self):
        payload = sample_quiz_payload()
        payload["room_name"] = "berlin-elephant"
        create = self.client.post("/rooms", json=payload)
        self.assertEqual(create.status_code, 200)
        data = create.json()
        self.assertIn("room_code", data)
        self.assertIn("room_name", data)
        self.assertIn("room_token", data)
        self.assertFalse(data["token_required"])
        self.assertEqual(data["room_name"], "berlin-elephant")
        self.assertEqual(data["host_role"], "participant")

        join = self.client.post(
            f"/rooms/{data['room_code']}/join",
            json={"room_token": data["room_token"], "player_name": "Mary"},
        )
        self.assertEqual(join.status_code, 200)
        joined = join.json()
        self.assertEqual(joined["display_name"], "Mary")
        self.assertTrue(joined["player_id"].startswith("p_"))
        self.assertEqual(joined["mode"], "compete")
        self.assertEqual(joined["player_role"], "participant")

        join_by_name = self.client.post(
            f"/rooms/by-name/{data['room_name']}/join",
            json={"room_token": data["room_token"], "player_name": "Tom"},
        )
        self.assertEqual(join_by_name.status_code, 200)
        joined2 = join_by_name.json()
        self.assertEqual(joined2["display_name"], "Tom")
        self.assertEqual(joined2["room_name"], data["room_name"])
        self.assertEqual(joined2["mode"], "compete")

        create_conflict = self.client.post("/rooms", json=payload)
        self.assertEqual(create_conflict.status_code, 409)

    def test_join_by_name_requires_room_token(self):
        payload = sample_quiz_payload(token_required=True)
        payload["room_name"] = "berlin-elephant"
        create = self.client.post("/rooms", json=payload)
        self.assertEqual(create.status_code, 200)
        data = create.json()

        missing_token = self.client.post(
            f"/rooms/by-name/{data['room_name']}/join",
            json={"player_name": "Tom"},
        )
        self.assertEqual(missing_token.status_code, 422)

        wrong_token = self.client.post(
            f"/rooms/by-name/{data['room_name']}/join",
            json={"room_token": "wrong-token", "player_name": "Tom"},
        )
        self.assertEqual(wrong_token.status_code, 403)

    def test_join_by_name_without_token_when_room_is_open(self):
        payload = sample_quiz_payload(token_required=False)
        payload["room_name"] = "berlin-elephant-open"
        create = self.client.post("/rooms", json=payload)
        self.assertEqual(create.status_code, 200)
        data = create.json()
        self.assertFalse(data["token_required"])
        self.assertEqual(data["room_token"], "")

        join_by_name = self.client.post(
            f"/rooms/by-name/{data['room_name']}/join",
            json={"player_name": "Tom"},
        )
        self.assertEqual(join_by_name.status_code, 200)

    def test_join_link_info_reflects_token_requirement(self):
        secure_payload = sample_quiz_payload(token_required=True)
        secure_payload["room_name"] = "secure-room"
        secure_create = self.client.post("/rooms", json=secure_payload)
        self.assertEqual(secure_create.status_code, 200)
        secure_info = self.client.get("/join/secure-room")
        self.assertEqual(secure_info.status_code, 200)
        self.assertTrue(secure_info.json()["token_required"])

        open_payload = sample_quiz_payload(token_required=False)
        open_payload["room_name"] = "open-room"
        open_create = self.client.post("/rooms", json=open_payload)
        self.assertEqual(open_create.status_code, 200)
        open_info = self.client.get("/join/open-room")
        self.assertEqual(open_info.status_code, 200)
        self.assertFalse(open_info.json()["token_required"])

    def test_compete_round_scoring_function(self):
        question = {"correct": [2], "points": 5, "time_limit": 20, "deadline_epoch": 21.0}
        submissions = {
            "a": Submission(player_id="a", answers=[2], ts=1.0),
            "b": Submission(player_id="b", answers=[2], ts=11.0),
            "c": Submission(player_id="c", answers=[1], ts=3.0),
        }
        deltas, correctness = compete_round_scores(
            question=question,
            submissions=submissions,
            active_player_ids=["a", "b", "c", "d"],
        )
        self.assertEqual(deltas["a"], 6.25)
        self.assertEqual(deltas["b"], 5.62)
        self.assertEqual(deltas["c"], 0)
        self.assertEqual(deltas["d"], 0)
        self.assertTrue(correctness["a"])
        self.assertFalse(correctness["c"])

    def test_compete_round_scoring_uses_large_question_value(self):
        question = {"correct": [1], "points": 100, "time_limit": 25, "deadline_epoch": 25.0}
        submissions = {
            "fast": Submission(player_id="fast", answers=[1], ts=5.0),
            "slow": Submission(player_id="slow", answers=[1], ts=22.0),
            "wrong": Submission(player_id="wrong", answers=[2], ts=9.0),
        }
        deltas, correctness = compete_round_scores(
            question=question,
            submissions=submissions,
            active_player_ids=["fast", "slow", "wrong"],
        )
        self.assertEqual(deltas["fast"], 120)
        self.assertEqual(deltas["slow"], 103)
        self.assertEqual(deltas["wrong"], 0)
        self.assertTrue(correctness["fast"])
        self.assertTrue(correctness["slow"])
        self.assertFalse(correctness["wrong"])

    def test_compete_round_scores_aggregate_different_question_values(self):
        totals = {"tom": 0.0, "maya": 0.0}
        q1 = {"correct": [1], "points": 5, "time_limit": 10, "deadline_epoch": 10.0}
        q1_deltas, _ = compete_round_scores(
            question=q1,
            submissions={
                "tom": Submission(player_id="tom", answers=[1], ts=2.0),
                "maya": Submission(player_id="maya", answers=[1], ts=8.0),
            },
            active_player_ids=["tom", "maya"],
        )
        q2 = {"correct": [2], "points": 100, "time_limit": 25, "deadline_epoch": 25.0}
        q2_deltas, _ = compete_round_scores(
            question=q2,
            submissions={
                "tom": Submission(player_id="tom", answers=[2], ts=5.0),
                "maya": Submission(player_id="maya", answers=[1], ts=5.0),
            },
            active_player_ids=["tom", "maya"],
        )
        for pid in totals:
            totals[pid] += q1_deltas[pid] + q2_deltas[pid]

        self.assertEqual(q1_deltas["tom"], 6.0)
        self.assertEqual(q1_deltas["maya"], 5.25)
        self.assertEqual(q2_deltas["tom"], 120.0)
        self.assertEqual(q2_deltas["maya"], 0)
        self.assertEqual(totals["tom"], 126.0)
        self.assertEqual(totals["maya"], 5.25)

    def test_collaborate_consensus_function(self):
        question = {"correct": [1]}
        submissions = {
            "a": Submission(player_id="a", answers=[1], ts=1.0),
            "b": Submission(player_id="b", answers=[1], ts=1.1),
        }
        passed, correctness, missing = collaborate_consensus(
            question=question,
            submissions=submissions,
            active_player_ids=["a", "b"],
        )
        self.assertTrue(passed)
        self.assertEqual(missing, [])
        self.assertTrue(all(correctness.values()))

    def test_websocket_compete_round(self):
        create = self.client.post("/rooms", json=sample_quiz_payload())
        self.assertEqual(create.status_code, 200)
        c = create.json()

        join = self.client.post(
            f"/rooms/{c['room_code']}/join",
            json={"room_token": c["room_token"], "player_name": "Tom"},
        )
        self.assertEqual(join.status_code, 200)
        j = join.json()

        room_code = c["room_code"]

        def consume_until(ws, wanted_type: str, max_events: int = 50):
            for _ in range(max_events):
                msg = ws.receive_json()
                if msg.get("type") == wanted_type:
                    return msg
            raise AssertionError(f"Did not receive {wanted_type}")

        with self.client.websocket_connect(
            f"/rooms/{room_code}/ws?player_id={c['host_player_id']}&token={c['host_player_token']}"
        ) as ws_host, self.client.websocket_connect(
            f"/rooms/{room_code}/ws?player_id={j['player_id']}&token={j['player_token']}"
        ) as ws_tom:
            consume_until(ws_host, "connected")
            consume_until(ws_tom, "connected")

            ws_host.send_json({"type": "ready_toggle", "payload": {"ready": True}})
            ws_tom.send_json({"type": "ready_toggle", "payload": {"ready": True}})
            ws_host.send_json({"type": "start_game", "payload": {}})

            consume_until(ws_host, "question")
            consume_until(ws_tom, "question")

            # Host answers correctly first, Tom answers wrong.
            ws_host.send_json({"type": "submit_answer", "payload": {"question_index": 0, "answers": [2]}})
            progress = consume_until(ws_host, "answer_progress")
            self.assertEqual(progress["payload"]["question_index"], 0)
            self.assertEqual(progress["payload"]["submitted"], 1)
            self.assertEqual(progress["payload"]["total"], 2)
            self.assertEqual(progress["payload"]["remaining"], 1)
            self.assertFalse(progress["payload"]["all_submitted"])

            ws_tom.send_json({"type": "submit_answer", "payload": {"question_index": 0, "answers": [1]}})

            progress = consume_until(ws_host, "answer_progress")
            self.assertEqual(progress["payload"]["submitted"], 2)
            self.assertEqual(progress["payload"]["total"], 2)
            self.assertEqual(progress["payload"]["remaining"], 0)
            self.assertTrue(progress["payload"]["all_submitted"])
            rr = consume_until(ws_host, "round_result")
            self.assertEqual(rr["payload"]["question_index"], 0)
            sb = consume_until(ws_host, "scoreboard")
            players = {row["name"]: row["score"] for row in sb["payload"]["players"]}
            self.assertGreaterEqual(players["Hosty"], 1)
            self.assertLessEqual(players["Hosty"], 1.25)
            self.assertEqual(players["Tom"], 0)
            waiting = consume_until(ws_host, "awaiting_next")
            self.assertTrue(waiting["payload"]["finished_after_continue"])
            ws_host.send_json({"type": "next_question", "payload": {}})
            finished = consume_until(ws_host, "game_finished")
            self.assertEqual(finished["payload"]["state"], "finished")

    def test_compete_waits_for_host_before_next_question(self):
        payload = sample_quiz_payload(mode="compete")
        payload["questions"].append(
            {
                "title": "Question 2",
                "question": "3+3?",
                "options": ["5", "6"],
                "correct": [2],
                "type": "single",
                "time_limit": 30,
                "discussion_time": None,
                "explanation": "3+3 is 6",
            }
        )
        create = self.client.post("/rooms", json=payload)
        self.assertEqual(create.status_code, 200)
        c = create.json()

        def consume_until(ws, wanted_type: str, max_events: int = 50):
            for _ in range(max_events):
                msg = ws.receive_json()
                if msg.get("type") == wanted_type:
                    return msg
            raise AssertionError(f"Did not receive {wanted_type}")

        with self.client.websocket_connect(
            f"/rooms/{c['room_code']}/ws?player_id={c['host_player_id']}&token={c['host_player_token']}"
        ) as ws_host:
            consume_until(ws_host, "connected")
            ws_host.send_json({"type": "start_game", "payload": {}})
            q1 = consume_until(ws_host, "question")
            self.assertEqual(q1["payload"]["question_index"], 0)
            ws_host.send_json({"type": "submit_answer", "payload": {"question_index": 0, "answers": [2]}})
            consume_until(ws_host, "round_result")
            consume_until(ws_host, "scoreboard")
            waiting = consume_until(ws_host, "awaiting_next")
            self.assertFalse(waiting["payload"]["finished_after_continue"])
            self.assertEqual(waiting["payload"]["next_question_index"], 1)
            room = manager.rooms[c["room_code"]]
            self.assertTrue(room.awaiting_next)
            self.assertEqual(room.current_question, 1)

            ws_host.send_json({"type": "next_question", "payload": {}})
            q2 = consume_until(ws_host, "question")
            self.assertEqual(q2["payload"]["question_index"], 1)

    def test_start_game_broadcasts_countdown_before_question(self):
        manager.start_countdown_seconds = 5
        create = self.client.post("/rooms", json=sample_quiz_payload(mode="compete"))
        self.assertEqual(create.status_code, 200)
        c = create.json()

        def consume_until(ws, wanted_type: str, max_events: int = 20):
            for _ in range(max_events):
                msg = ws.receive_json()
                if msg.get("type") == wanted_type:
                    return msg
            raise AssertionError(f"Did not receive {wanted_type}")

        with self.client.websocket_connect(
            f"/rooms/{c['room_code']}/ws?player_id={c['host_player_id']}&token={c['host_player_token']}"
        ) as ws_host:
            consume_until(ws_host, "connected")
            ws_host.send_json({"type": "start_game", "payload": {}})
            starting = consume_until(ws_host, "game_starting")
            self.assertEqual(starting["payload"]["countdown_seconds"], 5)
            self.assertIn("start_epoch", starting["payload"])

        room = manager.rooms.get(c["room_code"])
        if room and room.start_countdown_task:
            room.start_countdown_task.cancel()
        manager.start_countdown_seconds = 0

    def test_reconnected_non_host_gets_current_question(self):
        create = self.client.post("/rooms", json=sample_quiz_payload())
        self.assertEqual(create.status_code, 200)
        c = create.json()

        join = self.client.post(
            f"/rooms/{c['room_code']}/join",
            json={"room_token": c["room_token"], "player_name": "Tom"},
        )
        self.assertEqual(join.status_code, 200)
        j = join.json()

        def consume_until(ws, wanted_type: str, max_events: int = 80):
            for _ in range(max_events):
                msg = ws.receive_json()
                if msg.get("type") == wanted_type:
                    return msg
            raise AssertionError(f"Did not receive {wanted_type}")

        with self.client.websocket_connect(
            f"/rooms/{c['room_code']}/ws?player_id={c['host_player_id']}&token={c['host_player_token']}"
        ) as ws_host:
            with self.client.websocket_connect(
                f"/rooms/{c['room_code']}/ws?player_id={j['player_id']}&token={j['player_token']}"
            ) as ws_tom:
                consume_until(ws_host, "connected")
                consume_until(ws_tom, "connected")

                ws_host.send_json({"type": "ready_toggle", "payload": {"ready": True}})
                ws_tom.send_json({"type": "ready_toggle", "payload": {"ready": True}})
                ws_host.send_json({"type": "start_game", "payload": {}})
                consume_until(ws_host, "question")
                consume_until(ws_tom, "question")

            # Tom disconnected; reconnect same player and expect current question replay.
            with self.client.websocket_connect(
                f"/rooms/{c['room_code']}/ws?player_id={j['player_id']}&token={j['player_token']}"
            ) as ws_tom_reconnect:
                consume_until(ws_tom_reconnect, "connected")
                q = consume_until(ws_tom_reconnect, "question")
                self.assertEqual(q["payload"]["question_index"], 0)

    def test_compete_disconnect_before_submit_scores_zero(self):
        payload = sample_quiz_payload()
        payload["questions"][0]["time_limit"] = 5
        create = self.client.post("/rooms", json=payload)
        self.assertEqual(create.status_code, 200)
        c = create.json()

        join = self.client.post(
            f"/rooms/{c['room_code']}/join",
            json={"room_token": c["room_token"], "player_name": "Tom"},
        )
        self.assertEqual(join.status_code, 200)
        j = join.json()

        def consume_until(ws, wanted_type: str, max_events: int = 120):
            for _ in range(max_events):
                msg = ws.receive_json()
                if msg.get("type") == wanted_type:
                    return msg
            raise AssertionError(f"Did not receive {wanted_type}")

        with self.client.websocket_connect(
            f"/rooms/{c['room_code']}/ws?player_id={c['host_player_id']}&token={c['host_player_token']}"
        ) as ws_host:
            with self.client.websocket_connect(
                f"/rooms/{c['room_code']}/ws?player_id={j['player_id']}&token={j['player_token']}"
            ) as ws_tom:
                consume_until(ws_host, "connected")
                consume_until(ws_tom, "connected")
                ws_host.send_json({"type": "ready_toggle", "payload": {"ready": True}})
                ws_tom.send_json({"type": "ready_toggle", "payload": {"ready": True}})
                ws_host.send_json({"type": "start_game", "payload": {}})
                consume_until(ws_host, "question")
                consume_until(ws_tom, "question")

            # Tom disconnects before submitting; wrong/missing answers receive no points.
            ws_host.send_json({"type": "submit_answer", "payload": {"question_index": 0, "answers": [1]}})
            sb = consume_until(ws_host, "scoreboard", max_events=240)
            players = {row["name"]: row["score"] for row in sb["payload"]["players"]}
            self.assertEqual(players["Hosty"], 0)
            self.assertEqual(players["Tom"], 0)

    def test_create_boxing_room_is_rejected(self):
        create = self.client.post("/rooms", json=sample_quiz_payload(mode="boxing"))
        self.assertEqual(create.status_code, 422)
        self.assertIn("boxing", create.text)

    def test_disconnect_player_is_idempotent_for_player_left_event(self):
        class SinkWS:
            def __init__(self):
                self.events = []

            async def send_json(self, payload):
                self.events.append(payload)

        async def scenario():
            room_manager = RoomManager()
            created = await room_manager.create_room(
                mode=room_store_module.Mode.compete,
                quiz_title="T",
                questions=[{
                    "title": "Question 1",
                    "question": "2+2?",
                    "options": ["3", "4"],
                    "correct": [2],
                    "type": "single",
                    "time_limit": 30,
                    "explanation": "",
                }],
                host_name="Hosty",
            )
            room = room_manager.rooms[created["room_code"]]
            joined = await room_manager.join_room(
                room_code=room.room_code,
                room_token=room.room_token,
                player_name="Tom",
            )
            ws_host = SinkWS()
            ws_tom = SinkWS()
            await room_manager.connect_player(room.room_code, created["host_player_id"], ws_host)
            await room_manager.connect_player(room.room_code, joined["player_id"], ws_tom)

            await room_manager.handle_event(room.room_code, joined["player_id"], "leave_room", {})
            await room_manager.disconnect_player(room.room_code, joined["player_id"])

            player_left_count = sum(1 for event in ws_host.events if event.get("type") == "player_left")
            self.assertEqual(player_left_count, 1)

        asyncio.run(scenario())

    def test_stale_socket_close_does_not_disconnect_reconnected_player(self):
        class SinkWS:
            def __init__(self):
                self.events = []

            async def send_json(self, payload):
                self.events.append(payload)

        async def scenario():
            room_manager = RoomManager()
            created = await room_manager.create_room(
                mode=room_store_module.Mode.compete,
                quiz_title="T",
                questions=[{
                    "title": "Question 1",
                    "question": "2+2?",
                    "options": ["3", "4"],
                    "correct": [2],
                    "type": "single",
                    "time_limit": 30,
                    "explanation": "",
                }],
                host_name="Hosty",
            )
            room = room_manager.rooms[created["room_code"]]
            old_ws = SinkWS()
            new_ws = SinkWS()

            await room_manager.connect_player(room.room_code, created["host_player_id"], old_ws)
            await room_manager.connect_player(room.room_code, created["host_player_id"], new_ws)
            await room_manager.disconnect_player(room.room_code, created["host_player_id"], old_ws)

            self.assertTrue(room.players[created["host_player_id"]].connected)
            self.assertIs(room_manager.connections[room.room_code][created["host_player_id"]], new_ws)

            await room_manager.disconnect_player(room.room_code, created["host_player_id"], new_ws)
            self.assertFalse(room.players[created["host_player_id"]].connected)

        asyncio.run(scenario())

    def test_stale_broadcast_send_does_not_disconnect_reconnected_player(self):
        class BrokenWS:
            async def send_json(self, _payload):
                raise RuntimeError("stale")

        class SinkWS:
            def __init__(self):
                self.events = []

            async def send_json(self, payload):
                self.events.append(payload)

        class ReconnectDuringBroadcastWS:
            def __init__(self, manager, room_code, player_id, replacement):
                self.manager = manager
                self.room_code = room_code
                self.player_id = player_id
                self.replacement = replacement

            async def send_json(self, _payload):
                self.manager.connections[self.room_code][self.player_id] = self.replacement
                raise RuntimeError("stale")

        async def scenario():
            room_manager = RoomManager()
            created = await room_manager.create_room(
                mode=room_store_module.Mode.compete,
                quiz_title="T",
                questions=[{
                    "title": "Question 1",
                    "question": "2+2?",
                    "options": ["3", "4"],
                    "correct": [2],
                    "type": "single",
                    "time_limit": 30,
                    "explanation": "",
                }],
                host_name="Hosty",
            )
            room = room_manager.rooms[created["room_code"]]
            player_id = created["host_player_id"]
            replacement_ws = SinkWS()
            stale_ws = ReconnectDuringBroadcastWS(room_manager, room.room_code, player_id, replacement_ws)
            room.players[player_id].connected = True
            room_manager.connections[room.room_code][player_id] = stale_ws

            await room_manager.broadcast(room.room_code, "lobby_update", {})

            self.assertTrue(room.players[player_id].connected)
            self.assertIs(room_manager.connections[room.room_code][player_id], replacement_ws)

            room_manager.connections[room.room_code][player_id] = BrokenWS()
            await room_manager.broadcast(room.room_code, "lobby_update", {})
            self.assertFalse(room.players[player_id].connected)
            self.assertNotIn(player_id, room_manager.connections[room.room_code])

        asyncio.run(scenario())

    def test_finished_room_is_removed_after_last_disconnect(self):
        class SinkWS:
            async def send_json(self, _payload):
                return None

        async def scenario():
            room_manager = RoomManager()
            created = await room_manager.create_room(
                mode=room_store_module.Mode.compete,
                room_name="cleanup-room",
                quiz_title="T",
                questions=[{
                    "title": "Question 1",
                    "question": "2+2?",
                    "options": ["3", "4"],
                    "correct": [2],
                    "type": "single",
                    "time_limit": 30,
                    "explanation": "",
                }],
                host_name="Hosty",
            )
            joined = await room_manager.join_room(
                room_code=created["room_code"],
                room_token="",
                player_name="Tom",
            )
            room = room_manager.rooms[created["room_code"]]
            ws_host = SinkWS()
            ws_tom = SinkWS()
            await room_manager.connect_player(room.room_code, created["host_player_id"], ws_host)
            await room_manager.connect_player(room.room_code, joined["player_id"], ws_tom)
            room.state = room_store_module.RoomState.finished

            await room_manager.disconnect_player(room.room_code, created["host_player_id"], ws_host)
            self.assertIn(room.room_code, room_manager.rooms)

            await room_manager.disconnect_player(room.room_code, joined["player_id"], ws_tom)
            self.assertNotIn(room.room_code, room_manager.rooms)
            self.assertNotIn("cleanup-room", room_manager.room_names)

        asyncio.run(scenario())

    def test_finished_broadcast_with_stale_socket_does_not_deadlock(self):
        class BrokenWS:
            async def send_json(self, _payload):
                raise RuntimeError("stale")

        async def scenario():
            room_manager = RoomManager()
            created = await room_manager.create_room(
                mode=room_store_module.Mode.compete,
                quiz_title="T",
                questions=[{
                    "title": "Question 1",
                    "question": "2+2?",
                    "options": ["3", "4"],
                    "correct": [2],
                    "type": "single",
                    "time_limit": 30,
                    "explanation": "",
                }],
                host_name="Hosty",
            )
            room = room_manager.rooms[created["room_code"]]
            room.players[created["host_player_id"]].connected = True
            room_manager.connections[room.room_code][created["host_player_id"]] = BrokenWS()

            await asyncio.wait_for(room_manager._open_question(room, 1), timeout=1)
            self.assertEqual(room.state, room_store_module.RoomState.finished)
            self.assertFalse(room.players[created["host_player_id"]].connected)

        asyncio.run(scenario())

    def test_collaborate_has_max_retries_and_advances(self):
        original_limit = room_store_module.COLLABORATE_MAX_RETRIES
        room_store_module.COLLABORATE_MAX_RETRIES = 2
        try:
            create = self.client.post("/rooms", json=sample_quiz_payload(mode="collaborate"))
            self.assertEqual(create.status_code, 200)
            c = create.json()

            join = self.client.post(
                f"/rooms/{c['room_code']}/join",
                json={"room_token": c["room_token"], "player_name": "Tom"},
            )
            self.assertEqual(join.status_code, 200)
            j = join.json()

            def consume_until(ws, wanted_type: str, max_events: int = 80):
                for _ in range(max_events):
                    msg = ws.receive_json()
                    if msg.get("type") == wanted_type:
                        return msg
                raise AssertionError(f"Did not receive {wanted_type}")

            with self.client.websocket_connect(
                f"/rooms/{c['room_code']}/ws?player_id={c['host_player_id']}&token={c['host_player_token']}"
            ) as ws_host, self.client.websocket_connect(
                f"/rooms/{c['room_code']}/ws?player_id={j['player_id']}&token={j['player_token']}"
            ) as ws_tom:
                consume_until(ws_host, "connected")
                consume_until(ws_tom, "connected")

                ws_host.send_json({"type": "ready_toggle", "payload": {"ready": True}})
                ws_tom.send_json({"type": "ready_toggle", "payload": {"ready": True}})
                ws_host.send_json({"type": "start_game", "payload": {}})

                # Attempt 1: wrong consensus -> retry
                q1 = consume_until(ws_host, "question")
                self.assertEqual(q1["payload"]["phase"], "voting")
                ws_host.send_json({"type": "submit_answer", "payload": {"question_index": 0, "answers": [1]}})
                progress = consume_until(ws_host, "answer_progress")
                self.assertEqual(progress["payload"]["submitted"], 1)
                self.assertEqual(progress["payload"]["total"], 2)
                self.assertFalse(progress["payload"]["all_submitted"])
                ws_tom.send_json({"type": "submit_answer", "payload": {"question_index": 0, "answers": [1]}})
                progress = consume_until(ws_host, "answer_progress")
                self.assertEqual(progress["payload"]["submitted"], 2)
                self.assertEqual(progress["payload"]["total"], 2)
                self.assertTrue(progress["payload"]["all_submitted"])
                retry_1 = consume_until(ws_host, "consensus_retry")
                self.assertEqual(retry_1["payload"]["status"], "retry")
                self.assertEqual(retry_1["payload"]["retry_count"], 1)
                self.assertEqual(retry_1["payload"]["max_retries"], 2)

                # Attempt 2: still wrong -> max retries -> advance (single-question quiz => finish)
                q_retry = consume_until(ws_host, "question")
                self.assertEqual(q_retry["payload"]["phase"], "voting")
                ws_host.send_json({"type": "submit_answer", "payload": {"question_index": 0, "answers": [1]}})
                progress = consume_until(ws_host, "answer_progress")
                self.assertEqual(progress["payload"]["submitted"], 1)
                self.assertEqual(progress["payload"]["total"], 2)
                self.assertFalse(progress["payload"]["all_submitted"])
                ws_tom.send_json({"type": "submit_answer", "payload": {"question_index": 0, "answers": [1]}})
                progress = consume_until(ws_host, "answer_progress")
                self.assertEqual(progress["payload"]["submitted"], 2)
                self.assertEqual(progress["payload"]["total"], 2)
                self.assertTrue(progress["payload"]["all_submitted"])
                retry_2 = consume_until(ws_host, "consensus_retry")
                self.assertEqual(retry_2["payload"]["status"], "max_retries")
                self.assertEqual(retry_2["payload"]["retry_count"], 2)
                self.assertEqual(retry_2["payload"]["max_retries"], 2)

                consume_until(ws_host, "awaiting_next")
                ws_host.send_json({"type": "next_question", "payload": {}})
                finished = consume_until(ws_host, "game_finished")
                self.assertEqual(finished["payload"]["state"], "finished")
        finally:
            room_store_module.COLLABORATE_MAX_RETRIES = original_limit

    def test_collaborate_retry_count_resets_after_pass(self):
        create = self.client.post("/rooms", json=sample_two_question_collaborate_payload())
        self.assertEqual(create.status_code, 200)
        c = create.json()

        join = self.client.post(
            f"/rooms/{c['room_code']}/join",
            json={"room_token": c["room_token"], "player_name": "Tom"},
        )
        self.assertEqual(join.status_code, 200)
        j = join.json()

        def consume_until(ws, wanted_type: str, max_events: int = 80):
            for _ in range(max_events):
                msg = ws.receive_json()
                if msg.get("type") == wanted_type:
                    return msg
            raise AssertionError(f"Did not receive {wanted_type}")

        with self.client.websocket_connect(
            f"/rooms/{c['room_code']}/ws?player_id={c['host_player_id']}&token={c['host_player_token']}"
        ) as ws_host, self.client.websocket_connect(
            f"/rooms/{c['room_code']}/ws?player_id={j['player_id']}&token={j['player_token']}"
        ) as ws_tom:
            consume_until(ws_host, "connected")
            consume_until(ws_tom, "connected")

            ws_host.send_json({"type": "ready_toggle", "payload": {"ready": True}})
            ws_tom.send_json({"type": "ready_toggle", "payload": {"ready": True}})
            ws_host.send_json({"type": "start_game", "payload": {}})

            q1 = consume_until(ws_host, "question")
            self.assertEqual(q1["payload"]["question_index"], 0)
            self.assertEqual(q1["payload"]["retry_count"], 0)
            self.assertEqual(q1["payload"]["phase"], "voting")

            # First attempt wrong -> retry_count should increase to 1.
            ws_host.send_json({"type": "submit_answer", "payload": {"question_index": 0, "answers": [1]}})
            ws_tom.send_json({"type": "submit_answer", "payload": {"question_index": 0, "answers": [1]}})
            retry_evt = consume_until(ws_host, "consensus_retry")
            self.assertEqual(retry_evt["payload"]["status"], "retry")
            self.assertEqual(retry_evt["payload"]["retry_count"], 1)

            # Second attempt correct -> pass and move to Q2.
            q1_retry = consume_until(ws_host, "question")  # re-opened Q1
            self.assertEqual(q1_retry["payload"]["phase"], "voting")
            ws_host.send_json({"type": "submit_answer", "payload": {"question_index": 0, "answers": [2]}})
            ws_tom.send_json({"type": "submit_answer", "payload": {"question_index": 0, "answers": [2]}})
            consume_until(ws_host, "round_result")
            consume_until(ws_host, "scoreboard")
            consume_until(ws_host, "awaiting_next")
            ws_host.send_json({"type": "next_question", "payload": {}})

            q2 = consume_until(ws_host, "question")
            self.assertEqual(q2["payload"]["question_index"], 1)
            # Critical assertion: retry count must reset for the new question.
            self.assertEqual(q2["payload"]["retry_count"], 0)
            self.assertEqual(q2["payload"]["phase"], "voting")

    def test_collaborate_rejects_submit_during_discussion_phase(self):
        payload = sample_quiz_payload(mode="collaborate")
        payload["questions"][0]["discussion_time"] = 1
        create = self.client.post("/rooms", json=payload)
        self.assertEqual(create.status_code, 200)
        c = create.json()

        join = self.client.post(
            f"/rooms/{c['room_code']}/join",
            json={"room_token": c["room_token"], "player_name": "Tom"},
        )
        self.assertEqual(join.status_code, 200)
        j = join.json()

        def consume_until(ws, wanted_type: str, max_events: int = 100):
            for _ in range(max_events):
                msg = ws.receive_json()
                if msg.get("type") == wanted_type:
                    return msg
            raise AssertionError(f"Did not receive {wanted_type}")

        with self.client.websocket_connect(
            f"/rooms/{c['room_code']}/ws?player_id={c['host_player_id']}&token={c['host_player_token']}"
        ) as ws_host, self.client.websocket_connect(
            f"/rooms/{c['room_code']}/ws?player_id={j['player_id']}&token={j['player_token']}"
        ) as ws_tom:
            consume_until(ws_host, "connected")
            consume_until(ws_tom, "connected")
            ws_host.send_json({"type": "ready_toggle", "payload": {"ready": True}})
            ws_tom.send_json({"type": "ready_toggle", "payload": {"ready": True}})
            ws_host.send_json({"type": "start_game", "payload": {}})

            q = consume_until(ws_host, "question")
            self.assertEqual(q["payload"]["phase"], "discussion")
            ws_host.send_json({"type": "submit_answer", "payload": {"question_index": 0, "answers": [1]}})
            err = consume_until(ws_host, "error")
            self.assertIn("Voting phase has not started yet", err["payload"]["message"])

    def test_cleanup_stale_rooms_keeps_connected_rooms(self):
        room_manager = RoomManager()

        async def setup_room():
            created = await room_manager.create_room(
                mode=room_store_module.Mode.compete,
                quiz_title="T",
                questions=[{
                    "title": "Question 1",
                    "question": "2+2?",
                    "options": ["3", "4"],
                    "correct": [2],
                    "type": "single",
                    "time_limit": 30,
                    "explanation": "",
                }],
                host_name="Hosty",
            )
            room = room_manager.rooms[created["room_code"]]
            room.updated_at = time.time() - ((room_store_module.ROOM_TTL_MINUTES * 60) + 600)
            room.players[created["host_player_id"]].connected = True
            return room.room_code

        room_code = asyncio.run(setup_room())
        sleep_calls = {"count": 0}

        async def fake_sleep(_seconds):
            sleep_calls["count"] += 1
            if sleep_calls["count"] > 1:
                raise asyncio.CancelledError()
            return None

        with patch("app.room_store.asyncio.sleep", side_effect=fake_sleep):
            with self.assertRaises(asyncio.CancelledError):
                asyncio.run(room_manager.cleanup_stale_rooms())

        self.assertIn(room_code, room_manager.rooms)


if __name__ == "__main__":
    unittest.main()

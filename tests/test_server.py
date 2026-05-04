from __future__ import annotations

import asyncio
import time
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import room_store as room_store_module
from app.game_engine import Submission, collaborate_consensus, compete_round_scores
from app.main import app, manager
from app.room_store import RoomManager


def sample_quiz_payload(mode: str = "compete"):
    return {
        "mode": mode,
        "room_name": "",
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
                "points": 1,
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
                "points": 1,
                "explanation": "2+2 is 4",
            },
            {
                "title": "Question 2",
                "question": "3+3?",
                "options": ["5", "6", "7", "8"],
                "correct": [2],
                "type": "single",
                "time_limit": 30,
                "points": 1,
                "explanation": "3+3 is 6",
            },
        ],
    }


def sample_two_question_payload(mode: str = "compete"):
    payload = sample_two_question_collaborate_payload()
    payload["mode"] = mode
    payload["quiz_title"] = "Two Question Sample"
    return payload


class ServerTests(unittest.TestCase):
    def setUp(self):
        self._countdown_patch = patch.object(room_store_module, "ROOM_TRANSITION_COUNTDOWN_SECONDS", 0)
        self._countdown_patch.start()
        self.addCleanup(self._countdown_patch.stop)
        for room in manager.rooms.values():
            if room.round_timeout_task and not room.round_timeout_task.done():
                room.round_timeout_task.cancel()
            if room.host_grace_task and not room.host_grace_task.done():
                room.host_grace_task.cancel()
        manager.rooms.clear()
        manager.room_names.clear()
        manager.connections.clear()
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
        self.assertEqual(data["room_name"], "berlin-elephant")

        join = self.client.post(
            f"/rooms/{data['room_code']}/join",
            json={"room_token": data["room_token"], "player_name": "Mary"},
        )
        self.assertEqual(join.status_code, 200)
        joined = join.json()
        self.assertEqual(joined["display_name"], "Mary")
        self.assertTrue(joined["player_id"].startswith("p_"))
        self.assertEqual(joined["mode"], "compete")

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
        payload = sample_quiz_payload()
        payload["room_name"] = "berlin-elephant"
        create = self.client.post("/rooms", json=payload)
        self.assertEqual(create.status_code, 200)
        data = create.json()

        missing_token = self.client.post(
            f"/rooms/by-name/{data['room_name']}/join",
            json={"player_name": "Tom"},
        )
        self.assertEqual(missing_token.status_code, 403)

        wrong_token = self.client.post(
            f"/rooms/by-name/{data['room_name']}/join",
            json={"room_token": "wrong-token", "player_name": "Tom"},
        )
        self.assertEqual(wrong_token.status_code, 403)

    def test_open_room_can_join_without_room_token(self):
        payload = sample_quiz_payload(mode="eliminate")
        payload["room_name"] = "open-elephant"
        payload["token_required"] = False
        create = self.client.post("/rooms", json=payload)
        self.assertEqual(create.status_code, 200)
        data = create.json()
        self.assertFalse(data["token_required"])
        self.assertIn("room_token", data)

        info = self.client.get(f"/join/{data['room_name']}")
        self.assertEqual(info.status_code, 200)
        self.assertFalse(info.json()["token_required"])
        self.assertNotIn("--token", info.json()["message"])

        join = self.client.post(
            f"/rooms/by-name/{data['room_name']}/join",
            json={"player_name": "Tom"},
        )
        self.assertEqual(join.status_code, 200)
        joined = join.json()
        self.assertEqual(joined["display_name"], "Tom")
        self.assertEqual(joined["mode"], "eliminate")

        snapshot = self.client.get(f"/rooms/{data['room_code']}")
        self.assertEqual(snapshot.status_code, 200)
        self.assertFalse(snapshot.json()["token_required"])
        self.assertEqual(snapshot.json()["room_name"], "open-elephant")

    def test_compete_round_scoring_function(self):
        question = {"correct": [2], "points": 2, "time_limit": 25}
        submissions = {
            "a": Submission(player_id="a", answers=[2], ts=5.0),
            "b": Submission(player_id="b", answers=[2], ts=12.0),
            "c": Submission(player_id="c", answers=[1], ts=3.0),
            "e": Submission(player_id="e", answers=[2], ts=26.0),
        }
        deltas, correctness = compete_round_scores(
            question=question,
            submissions=submissions,
            active_player_ids=["a", "b", "c", "d", "e"],
            deadline_epoch=25.0,
        )
        self.assertEqual(deltas["a"], 2.4)
        self.assertEqual(deltas["b"], 2.26)
        self.assertEqual(deltas["c"], 0)
        self.assertEqual(deltas["d"], 0)
        self.assertEqual(deltas["e"], 0)
        self.assertTrue(correctness["a"])
        self.assertFalse(correctness["c"])
        self.assertFalse(correctness["e"])

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
            self.assertEqual(progress["payload"]["submitted"], 1)
            self.assertEqual(progress["payload"]["total"], 2)
            self.assertFalse(progress["payload"]["all_submitted"])
            ws_tom.send_json({"type": "submit_answer", "payload": {"question_index": 0, "answers": [1]}})
            progress = consume_until(ws_host, "answer_progress")
            self.assertEqual(progress["payload"]["submitted"], 2)
            self.assertEqual(progress["payload"]["remaining"], 0)
            self.assertTrue(progress["payload"]["all_submitted"])

            rr = consume_until(ws_host, "round_result")
            self.assertEqual(rr["payload"]["question_index"], 0)
            sb = consume_until(ws_host, "scoreboard")
            players = {row["name"]: row["score"] for row in sb["payload"]["players"]}
            self.assertEqual(players["Hosty"], 1.25)
            self.assertEqual(players["Tom"], 0)

    def test_bad_submit_payload_returns_error_and_keeps_connection(self):
        create = self.client.post("/rooms", json=sample_quiz_payload())
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
            consume_until(ws_host, "question")

            ws_host.send_json({"type": "submit_answer", "payload": {"question_index": "abc", "answers": [2]}})
            err = consume_until(ws_host, "error")
            self.assertIn("question_index must be an integer", err["payload"]["message"])

            ws_host.send_json({"type": "submit_answer", "payload": {"question_index": 0, "answers": [2]}})
            progress = consume_until(ws_host, "answer_progress")
            self.assertTrue(progress["payload"]["all_submitted"])
            consume_until(ws_host, "round_result")

    def test_submission_after_deadline_is_rejected(self):
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
                    "points": 1,
                    "explanation": "",
                }],
                host_name="Hosty",
            )
            room = room_manager.rooms[created["room_code"]]
            ws = SinkWS()
            await room_manager.connect_player(room.room_code, created["host_player_id"], ws)
            async with room.lock:
                room.state = room_store_module.RoomState.playing
                room.current_question = 0
                room.round_participants = {created["host_player_id"]}
                room.round_deadline = time.time() - 1

            await room_manager.handle_event(
                room.room_code,
                created["host_player_id"],
                "submit_answer",
                {"question_index": 0, "answers": [2]},
            )

            self.assertEqual(room.submissions, {})
            error_events = [event for event in ws.events if event.get("type") == "error"]
            self.assertTrue(error_events)
            self.assertIn("Time is up", error_events[-1]["payload"]["message"])

        asyncio.run(scenario())

    def test_websocket_start_and_auto_next_countdowns(self):
        payload = sample_two_question_payload(mode="compete")
        payload["advance_mode"] = "auto"
        create = self.client.post("/rooms", json=payload)
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
            ws_host.send_json({"type": "start_game", "payload": {}})

            starting = consume_until(ws_host, "game_starting")
            self.assertEqual(starting["payload"]["countdown_seconds"], 0)
            consume_until(ws_tom, "game_starting")
            consume_until(ws_host, "question")
            consume_until(ws_tom, "question")

            ws_host.send_json({"type": "submit_answer", "payload": {"question_index": 0, "answers": [2]}})
            ws_tom.send_json({"type": "submit_answer", "payload": {"question_index": 0, "answers": [2]}})

            consume_until(ws_host, "round_result")
            consume_until(ws_host, "scoreboard")
            next_start = consume_until(ws_host, "next_question_starting")
            self.assertEqual(next_start["payload"]["countdown_seconds"], 0)
            self.assertEqual(next_start["payload"]["next_question_index"], 1)
            q2 = consume_until(ws_host, "question")
            self.assertEqual(q2["payload"]["question_index"], 1)
            consume_until(ws_tom, "question")

            ws_host.send_json({"type": "submit_answer", "payload": {"question_index": 1, "answers": [2]}})
            ws_tom.send_json({"type": "submit_answer", "payload": {"question_index": 1, "answers": [2]}})
            consume_until(ws_host, "round_result")
            consume_until(ws_host, "scoreboard")
            consume_until(ws_host, "game_finished")

    def test_finished_room_is_removed_and_name_released(self):
        payload = sample_quiz_payload()
        payload["room_name"] = "short-room"
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
            consume_until(ws_host, "question")
            ws_host.send_json({"type": "submit_answer", "payload": {"question_index": 0, "answers": [2]}})
            consume_until(ws_host, "round_result")
            consume_until(ws_host, "scoreboard")
            finished = consume_until(ws_host, "game_finished")
            self.assertEqual(finished["payload"]["state"], "finished")

        self.assertNotIn(c["room_code"], manager.rooms)
        self.assertNotIn("short-room", manager.room_names)
        recreate = self.client.post("/rooms", json=payload)
        self.assertEqual(recreate.status_code, 200)

    def test_websocket_manual_next_waits_for_host_then_counts_down(self):
        payload = sample_two_question_payload(mode="compete")
        payload["advance_mode"] = "manual"
        create = self.client.post("/rooms", json=payload)
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
            ws_host.send_json({"type": "start_game", "payload": {}})

            consume_until(ws_host, "game_starting")
            consume_until(ws_host, "question")
            consume_until(ws_tom, "question")

            ws_host.send_json({"type": "submit_answer", "payload": {"question_index": 0, "answers": [2]}})
            ws_tom.send_json({"type": "submit_answer", "payload": {"question_index": 0, "answers": [2]}})

            consume_until(ws_host, "round_result")
            consume_until(ws_host, "scoreboard")
            awaiting = consume_until(ws_host, "awaiting_next")
            self.assertEqual(awaiting["payload"]["next_question_index"], 1)
            self.assertFalse(awaiting["payload"]["finished_after_continue"])

            ws_host.send_json({"type": "next_question", "payload": {}})
            next_start = consume_until(ws_host, "next_question_starting")
            self.assertEqual(next_start["payload"]["next_question_index"], 1)
            q2 = consume_until(ws_host, "question")
            self.assertEqual(q2["payload"]["question_index"], 1)
            consume_until(ws_tom, "question")

            ws_host.send_json({"type": "submit_answer", "payload": {"question_index": 1, "answers": [2]}})
            ws_tom.send_json({"type": "submit_answer", "payload": {"question_index": 1, "answers": [2]}})
            consume_until(ws_host, "round_result")
            consume_until(ws_host, "scoreboard")
            final_wait = consume_until(ws_host, "awaiting_next")
            self.assertTrue(final_wait["payload"]["finished_after_continue"])
            ws_host.send_json({"type": "next_question", "payload": {}})
            consume_until(ws_host, "game_finished")

    def test_manual_next_is_idempotent_during_transition(self):
        class SinkWS:
            def __init__(self):
                self.events = []

            async def send_json(self, payload):
                self.events.append(payload)

        async def scenario():
            original_countdown = room_store_module.ROOM_TRANSITION_COUNTDOWN_SECONDS
            room_store_module.ROOM_TRANSITION_COUNTDOWN_SECONDS = 0.01
            try:
                room_manager = RoomManager()
                created = await room_manager.create_room(
                    mode=room_store_module.Mode.compete,
                    advance_mode=room_store_module.AdvanceMode.manual,
                    quiz_title="T",
                    questions=[
                        {
                            "title": "Question 1",
                            "question": "2+2?",
                            "options": ["3", "4"],
                            "correct": [2],
                            "type": "single",
                            "time_limit": 30,
                            "points": 1,
                            "explanation": "",
                        },
                        {
                            "title": "Question 2",
                            "question": "3+3?",
                            "options": ["5", "6"],
                            "correct": [2],
                            "type": "single",
                            "time_limit": 30,
                            "points": 1,
                            "explanation": "",
                        },
                    ],
                    host_name="Hosty",
                )
                room = room_manager.rooms[created["room_code"]]
                ws = SinkWS()
                await room_manager.connect_player(room.room_code, created["host_player_id"], ws)
                room.state = room_store_module.RoomState.playing
                room.current_question = 1
                room.awaiting_next = True

                await asyncio.gather(
                    room_manager.handle_event(room.room_code, created["host_player_id"], "next_question", {}),
                    room_manager.handle_event(room.room_code, created["host_player_id"], "next_question", {}),
                )

                question_events = [event for event in ws.events if event.get("type") == "question"]
                self.assertEqual(len(question_events), 1)
                self.assertEqual(question_events[0]["payload"]["question_index"], 1)
            finally:
                room_store_module.ROOM_TRANSITION_COUNTDOWN_SECONDS = original_countdown

        asyncio.run(scenario())

    def test_websocket_eliminate_keeps_eliminated_players_practicing(self):
        create = self.client.post("/rooms", json=sample_two_question_payload(mode="eliminate"))
        self.assertEqual(create.status_code, 200)
        c = create.json()

        join = self.client.post(
            f"/rooms/{c['room_code']}/join",
            json={"room_token": c["room_token"], "player_name": "Tom"},
        )
        self.assertEqual(join.status_code, 200)
        j = join.json()

        room_code = c["room_code"]

        def consume_until(ws, wanted_type: str, max_events: int = 80):
            for _ in range(max_events):
                msg = ws.receive_json()
                if msg.get("type") == wanted_type:
                    return msg
            raise AssertionError(f"Did not receive {wanted_type}")

        def consume_until_all_ready(ws, max_events: int = 80):
            for _ in range(max_events):
                msg = ws.receive_json()
                if msg.get("type") != "lobby_update":
                    continue
                players = msg.get("payload", {}).get("players", [])
                if players and all(player.get("ready") for player in players):
                    return msg
            raise AssertionError("Did not receive all-ready lobby update")

        with self.client.websocket_connect(
            f"/rooms/{room_code}/ws?player_id={c['host_player_id']}&token={c['host_player_token']}"
        ) as ws_host, self.client.websocket_connect(
            f"/rooms/{room_code}/ws?player_id={j['player_id']}&token={j['player_token']}"
        ) as ws_tom:
            consume_until(ws_host, "connected")
            consume_until(ws_tom, "connected")

            ws_host.send_json({"type": "ready_toggle", "payload": {"ready": True}})
            ws_tom.send_json({"type": "ready_toggle", "payload": {"ready": True}})
            consume_until_all_ready(ws_host)
            ws_host.send_json({"type": "start_game", "payload": {}})

            q1 = consume_until(ws_host, "question")
            consume_until(ws_tom, "question")
            self.assertEqual(q1["payload"]["mode"], "eliminate")

            # Host stays alive; Tom is wrong and becomes eliminated with no penalty.
            ws_host.send_json({"type": "submit_answer", "payload": {"question_index": 0, "answers": [2]}})
            ws_tom.send_json({"type": "submit_answer", "payload": {"question_index": 0, "answers": [1]}})

            rr1 = consume_until(ws_host, "round_result")
            by_name = {row["name"]: row for row in rr1["payload"]["players"]}
            self.assertEqual(by_name["Hosty"]["delta"], 1.25)
            self.assertFalse(by_name["Hosty"]["eliminated"])
            self.assertEqual(by_name["Tom"]["delta"], 0)
            self.assertTrue(by_name["Tom"]["newly_eliminated"])
            eliminated = consume_until(ws_tom, "eliminated")
            self.assertIn("Keep playing for practice", eliminated["payload"]["message"])

            consume_until(ws_host, "scoreboard")
            consume_until(ws_host, "question")
            consume_until(ws_tom, "question")

            # Tom can still answer correctly for practice, but receives no points.
            ws_host.send_json({"type": "submit_answer", "payload": {"question_index": 1, "answers": [2]}})
            ws_tom.send_json({"type": "submit_answer", "payload": {"question_index": 1, "answers": [2]}})

            rr2 = consume_until(ws_host, "round_result")
            by_name = {row["name"]: row for row in rr2["payload"]["players"]}
            self.assertTrue(by_name["Tom"]["is_correct"])
            self.assertEqual(by_name["Tom"]["delta"], 0)
            self.assertEqual(by_name["Tom"]["score"], 0)
            self.assertTrue(by_name["Tom"]["eliminated"])

            sb = consume_until(ws_host, "scoreboard")
            players = {row["name"]: row for row in sb["payload"]["players"]}
            self.assertEqual(players["Hosty"]["score"], 2.5)
            self.assertFalse(players["Hosty"]["eliminated"])
            self.assertEqual(players["Tom"]["score"], 0)
            self.assertTrue(players["Tom"]["eliminated"])

            finished = consume_until(ws_host, "game_finished")
            final_players = {row["name"]: row for row in finished["payload"]["players"]}
            self.assertTrue(final_players["Tom"]["eliminated"])

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

    def test_compete_disconnect_before_submit_waits_for_timeout_without_penalty(self):
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

            # Tom disconnects before submitting; the round waits for timeout and wrong/missing answers score 0.
            ws_host.send_json({"type": "submit_answer", "payload": {"question_index": 0, "answers": [1]}})
            sb = consume_until(ws_host, "scoreboard", max_events=240)
            players = {row["name"]: row["score"] for row in sb["payload"]["players"]}
            self.assertEqual(players["Hosty"], 0)
            self.assertEqual(players["Tom"], 0)

    def test_boxing_mode_is_not_accepted(self):
        create = self.client.post("/rooms", json=sample_quiz_payload(mode="boxing"))
        self.assertEqual(create.status_code, 422)
        openapi = self.client.get("/openapi.json")
        self.assertEqual(openapi.status_code, 200)
        self.assertNotIn("boxing", openapi.text)

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

    def test_disconnect_old_websocket_does_not_drop_reconnect(self):
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
                    "points": 1,
                    "explanation": "",
                }],
                host_name="Hosty",
            )
            room = room_manager.rooms[created["room_code"]]
            old_ws = SinkWS()
            new_ws = SinkWS()
            player_id = created["host_player_id"]

            await room_manager.connect_player(room.room_code, player_id, old_ws)
            await room_manager.connect_player(room.room_code, player_id, new_ws)
            await room_manager.disconnect_player(room.room_code, player_id, old_ws)

            self.assertIs(room_manager.connections[room.room_code][player_id], new_ws)
            self.assertTrue(room.players[player_id].connected)

            await room_manager.disconnect_player(room.room_code, player_id, new_ws)
            self.assertNotIn(player_id, room_manager.connections[room.room_code])
            self.assertFalse(room.players[player_id].connected)

        asyncio.run(scenario())

    def test_stale_broadcast_does_not_drop_reconnect(self):
        class BlockingFailWS:
            def __init__(self):
                self.started = asyncio.Event()
                self.release = asyncio.Event()

            async def send_json(self, payload):
                self.started.set()
                await self.release.wait()
                raise RuntimeError("old socket closed")

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
                    "points": 1,
                    "explanation": "",
                }],
                host_name="Hosty",
            )
            room = room_manager.rooms[created["room_code"]]
            player_id = created["host_player_id"]
            old_ws = BlockingFailWS()
            new_ws = SinkWS()
            room_manager.connections[room.room_code][player_id] = old_ws
            room.players[player_id].connected = True

            task = asyncio.create_task(room_manager.broadcast(room.room_code, "lobby_update", {}))
            await old_ws.started.wait()
            room_manager.connections[room.room_code][player_id] = new_ws
            room.players[player_id].connected = True
            old_ws.release.set()
            await task

            self.assertIs(room_manager.connections[room.room_code][player_id], new_ws)
            self.assertTrue(room.players[player_id].connected)

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
                consume_until(ws_host, "question")
                ws_host.send_json({"type": "submit_answer", "payload": {"question_index": 0, "answers": [1]}})
                ws_tom.send_json({"type": "submit_answer", "payload": {"question_index": 0, "answers": [1]}})
                retry_1 = consume_until(ws_host, "consensus_retry")
                self.assertEqual(retry_1["payload"]["status"], "retry")
                self.assertEqual(retry_1["payload"]["retry_count"], 1)
                self.assertEqual(retry_1["payload"]["max_retries"], 2)

                # Attempt 2: still wrong -> max retries -> advance (single-question quiz => finish)
                consume_until(ws_host, "question")
                ws_host.send_json({"type": "submit_answer", "payload": {"question_index": 0, "answers": [1]}})
                ws_tom.send_json({"type": "submit_answer", "payload": {"question_index": 0, "answers": [1]}})
                retry_2 = consume_until(ws_host, "consensus_retry")
                self.assertEqual(retry_2["payload"]["status"], "max_retries")
                self.assertEqual(retry_2["payload"]["retry_count"], 2)
                self.assertEqual(retry_2["payload"]["max_retries"], 2)

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

            # First attempt wrong -> retry_count should increase to 1.
            ws_host.send_json({"type": "submit_answer", "payload": {"question_index": 0, "answers": [1]}})
            ws_tom.send_json({"type": "submit_answer", "payload": {"question_index": 0, "answers": [1]}})
            retry_evt = consume_until(ws_host, "consensus_retry")
            consume_until(ws_tom, "consensus_retry")
            self.assertEqual(retry_evt["payload"]["status"], "retry")
            self.assertEqual(retry_evt["payload"]["retry_count"], 1)

            # Second attempt correct -> pass and move to Q2.
            consume_until(ws_host, "question")  # re-opened Q1
            ws_host.send_json({"type": "submit_answer", "payload": {"question_index": 0, "answers": [2]}})
            ws_tom.send_json({"type": "submit_answer", "payload": {"question_index": 0, "answers": [2]}})
            consume_until(ws_host, "round_result")
            consume_until(ws_tom, "round_result")
            consume_until(ws_host, "scoreboard")
            consume_until(ws_tom, "scoreboard")

            q2 = consume_until(ws_host, "question")
            consume_until(ws_tom, "question")
            self.assertEqual(q2["payload"]["question_index"], 1)
            # Critical assertion: retry count must reset for the new question.
            self.assertEqual(q2["payload"]["retry_count"], 0)

    def test_late_joiner_waits_until_next_collaborate_question(self):
        create = self.client.post("/rooms", json=sample_two_question_collaborate_payload())
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
            ws_host.send_json({"type": "start_game", "payload": {}})
            q1 = consume_until(ws_host, "question")
            consume_until(ws_tom, "question")
            self.assertEqual(q1["payload"]["question_index"], 0)

            late = self.client.post(
                f"/rooms/{c['room_code']}/join",
                json={"room_token": c["room_token"], "player_name": "Maya"},
            )
            self.assertEqual(late.status_code, 200)
            late_joined = late.json()
            with self.client.websocket_connect(
                f"/rooms/{c['room_code']}/ws?player_id={late_joined['player_id']}&token={late_joined['player_token']}"
            ) as ws_maya:
                wait = consume_until(ws_maya, "waiting_for_next_question")
                self.assertIn("next question", wait["payload"]["message"])

                ws_host.send_json({"type": "submit_answer", "payload": {"question_index": 0, "answers": [2]}})
                ws_tom.send_json({"type": "submit_answer", "payload": {"question_index": 0, "answers": [2]}})
                result = consume_until(ws_host, "round_result")
                self.assertEqual(result["payload"]["status"], "passed")

                q2 = consume_until(ws_maya, "question")
                self.assertEqual(q2["payload"]["question_index"], 1)

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

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
                "explanation": "2+2 is 4",
            },
            {
                "title": "Question 2",
                "question": "3+3?",
                "options": ["5", "6", "7", "8"],
                "correct": [2],
                "type": "single",
                "time_limit": 30,
                "explanation": "3+3 is 6",
            },
        ],
    }


class ServerTests(unittest.TestCase):
    def setUp(self):
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
        payload = sample_quiz_payload()
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

    def test_compete_round_scoring_function(self):
        question = {"correct": [2]}
        submissions = {
            "a": Submission(player_id="a", answers=[2], ts=1.0),
            "b": Submission(player_id="b", answers=[2], ts=2.0),
            "c": Submission(player_id="c", answers=[1], ts=3.0),
        }
        deltas, correctness = compete_round_scores(
            question=question,
            submissions=submissions,
            active_player_ids=["a", "b", "c", "d"],
        )
        self.assertEqual(deltas["a"], 3)
        self.assertEqual(deltas["b"], 2)
        self.assertEqual(deltas["c"], -3)
        self.assertEqual(deltas["d"], -3)
        self.assertTrue(correctness["a"])
        self.assertFalse(correctness["c"])

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
            ws_tom.send_json({"type": "submit_answer", "payload": {"question_index": 0, "answers": [1]}})

            rr = consume_until(ws_host, "round_result")
            self.assertEqual(rr["payload"]["question_index"], 0)
            sb = consume_until(ws_host, "scoreboard")
            players = {row["name"]: row["score"] for row in sb["payload"]["players"]}
            self.assertEqual(players["Hosty"], 3)
            self.assertEqual(players["Tom"], -3)

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

    def test_compete_disconnect_before_submit_still_gets_penalty(self):
        create = self.client.post("/rooms", json=sample_quiz_payload())
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

            # Tom disconnects before submitting; host submits wrong and timeout should penalize both.
            ws_host.send_json({"type": "submit_answer", "payload": {"question_index": 0, "answers": [1]}})
            sb = consume_until(ws_host, "scoreboard", max_events=240)
            players = {row["name"]: row["score"] for row in sb["payload"]["players"]}
            self.assertEqual(players["Hosty"], -3)
            self.assertEqual(players["Tom"], -3)

    def test_boxing_role_constraints(self):
        payload = sample_quiz_payload(mode="boxing")
        payload["room_name"] = "oslo-panda"
        payload["host_role"] = "teacher"
        create = self.client.post("/rooms", json=payload)
        self.assertEqual(create.status_code, 200)
        room = create.json()
        self.assertEqual(room["host_role"], "teacher")

        duplicate_role = self.client.post(
            f"/rooms/{room['room_code']}/join",
            json={"room_token": room["room_token"], "player_name": "Teacher2", "role": "teacher"},
        )
        self.assertEqual(duplicate_role.status_code, 409)
        self.assertIn("already taken", duplicate_role.text)

        join_student = self.client.post(
            f"/rooms/{room['room_code']}/join",
            json={"room_token": room["room_token"], "player_name": "Mary", "role": "student"},
        )
        self.assertEqual(join_student.status_code, 200)
        self.assertEqual(join_student.json()["player_role"], "student")

        room_full = self.client.post(
            f"/rooms/{room['room_code']}/join",
            json={"room_token": room["room_token"], "player_name": "Extra", "role": "student"},
        )
        self.assertEqual(room_full.status_code, 409)
        self.assertIn("boxing mode allows only 1 teacher + 1 student", room_full.text)

    def test_websocket_boxing_score_and_end(self):
        create = self.client.post(
            "/rooms",
            json={**sample_quiz_payload(mode="boxing"), "host_role": "teacher", "host_name": "Tim"},
        )
        self.assertEqual(create.status_code, 200)
        c = create.json()
        join = self.client.post(
            f"/rooms/{c['room_code']}/join",
            json={"room_token": c["room_token"], "player_name": "Mary", "role": "student"},
        )
        self.assertEqual(join.status_code, 200)
        s = join.json()

        def consume_until(ws, wanted_type: str, max_events: int = 60):
            for _ in range(max_events):
                msg = ws.receive_json()
                if msg.get("type") == wanted_type:
                    return msg
            raise AssertionError(f"Did not receive {wanted_type}")

        with self.client.websocket_connect(
            f"/rooms/{c['room_code']}/ws?player_id={c['host_player_id']}&token={c['host_player_token']}"
        ) as ws_teacher, self.client.websocket_connect(
            f"/rooms/{c['room_code']}/ws?player_id={s['player_id']}&token={s['player_token']}"
        ) as ws_student:
            consume_until(ws_teacher, "connected")
            consume_until(ws_student, "connected")

            ws_teacher.send_json({"type": "ready_toggle", "payload": {"ready": True}})
            ws_student.send_json({"type": "ready_toggle", "payload": {"ready": True}})
            ws_teacher.send_json({"type": "start_game", "payload": {}})
            consume_until(ws_teacher, "game_started")
            consume_until(ws_student, "game_started")

            ws_student.send_json({"type": "set_score", "payload": {"score": 80}})
            err = consume_until(ws_student, "error")
            self.assertIn("Only the teacher", err["payload"]["message"])

            ws_teacher.send_json({"type": "set_score", "payload": {"score": 80}})
            score_evt = consume_until(ws_teacher, "boxing_score")
            self.assertEqual(score_evt["payload"]["score"], 80)
            self.assertEqual(score_evt["payload"]["by_role"], "teacher")

            ws_student.send_json({"type": "end_session", "payload": {"reason": "done"}})
            finished = consume_until(ws_teacher, "game_finished")
            self.assertEqual(finished["payload"]["final_score"], 80)
            self.assertEqual(finished["payload"]["ended_by_role"], "student")

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
            self.assertEqual(retry_evt["payload"]["status"], "retry")
            self.assertEqual(retry_evt["payload"]["retry_count"], 1)

            # Second attempt correct -> pass and move to Q2.
            consume_until(ws_host, "question")  # re-opened Q1
            ws_host.send_json({"type": "submit_answer", "payload": {"question_index": 0, "answers": [2]}})
            ws_tom.send_json({"type": "submit_answer", "payload": {"question_index": 0, "answers": [2]}})
            consume_until(ws_host, "round_result")
            consume_until(ws_host, "scoreboard")

            q2 = consume_until(ws_host, "question")
            self.assertEqual(q2["payload"]["question_index"], 1)
            # Critical assertion: retry count must reset for the new question.
            self.assertEqual(q2["payload"]["retry_count"], 0)

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

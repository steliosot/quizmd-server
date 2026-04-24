from __future__ import annotations

import unittest

from fastapi.testclient import TestClient

from app.game_engine import Submission, collaborate_consensus, compete_round_scores
from app.main import app, manager


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
            json={"player_name": "Tom"},
        )
        self.assertEqual(join_by_name.status_code, 200)
        joined2 = join_by_name.json()
        self.assertEqual(joined2["display_name"], "Tom")
        self.assertEqual(joined2["room_name"], data["room_name"])
        self.assertEqual(joined2["mode"], "compete")

        create_conflict = self.client.post("/rooms", json=payload)
        self.assertEqual(create_conflict.status_code, 409)

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


if __name__ == "__main__":
    unittest.main()

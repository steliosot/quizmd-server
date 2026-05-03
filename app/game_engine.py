from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(slots=True)
class Submission:
    player_id: str
    answers: list[int]
    ts: float


def answer_is_correct(question: dict[str, Any], answers: list[int]) -> bool:
    expected = sorted(question.get("correct", []))
    return sorted(answers or []) == expected


def compete_round_scores(
    *,
    question: dict[str, Any],
    submissions: dict[str, Submission],
    active_player_ids: list[str],
    wrong_penalty: int = 0,
) -> tuple[dict[str, float], dict[str, bool]]:
    """Return (score_delta_by_player, correctness_by_player)."""
    score_delta = {pid: 0.0 for pid in active_player_ids}
    correctness = {pid: False for pid in active_player_ids}
    try:
        question_points = float(question.get("points", 1) or 1)
    except (TypeError, ValueError):
        question_points = 1.0
    question_points = max(0.0, question_points)
    try:
        time_limit = float(question.get("time_limit") or 0)
    except (TypeError, ValueError):
        time_limit = 0.0
    try:
        deadline_epoch = float(question.get("deadline_epoch") or 0)
    except (TypeError, ValueError):
        deadline_epoch = 0.0

    for pid in active_player_ids:
        sub = submissions.get(pid)
        if sub is None:
            score_delta[pid] += float(wrong_penalty)
            continue

        is_correct = answer_is_correct(question, sub.answers)
        correctness[pid] = is_correct
        if is_correct:
            time_bonus = 0.0
            if time_limit > 0 and deadline_epoch > 0:
                time_left = min(time_limit, max(0.0, deadline_epoch - sub.ts))
                time_bonus = (question_points / 4.0) * (time_left / time_limit)
            score_delta[pid] += round(question_points + time_bonus, 2)
        else:
            score_delta[pid] += float(wrong_penalty)

    return score_delta, correctness


def collaborate_consensus(
    *,
    question: dict[str, Any],
    submissions: dict[str, Submission],
    active_player_ids: list[str],
) -> tuple[bool, dict[str, bool], list[str]]:
    """Return (is_unanimous_correct, correctness_map, missing_player_ids)."""
    missing = [pid for pid in active_player_ids if pid not in submissions]
    correctness: dict[str, bool] = {}

    for pid in active_player_ids:
        sub = submissions.get(pid)
        if sub is None:
            correctness[pid] = False
            continue
        correctness[pid] = answer_is_correct(question, sub.answers)

    all_submitted = not missing
    unanimous_correct = all_submitted and all(correctness[pid] for pid in active_player_ids)
    return unanimous_correct, correctness, missing

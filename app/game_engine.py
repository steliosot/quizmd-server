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
    wrong_penalty: int = -3,
) -> tuple[dict[str, int], dict[str, bool]]:
    """Return (score_delta_by_player, correctness_by_player)."""
    score_delta = {pid: 0 for pid in active_player_ids}
    correctness = {pid: False for pid in active_player_ids}

    ranked_correct: list[Submission] = []
    for pid in active_player_ids:
        sub = submissions.get(pid)
        if sub is None:
            # timeout/no submission is treated as wrong penalty in compete mode
            score_delta[pid] += wrong_penalty
            continue

        is_correct = answer_is_correct(question, sub.answers)
        correctness[pid] = is_correct
        if is_correct:
            ranked_correct.append(sub)
        else:
            score_delta[pid] += wrong_penalty

    ranked_correct.sort(key=lambda x: x.ts)
    podium = [3, 2, 1]
    for idx, sub in enumerate(ranked_correct[:3]):
        score_delta[sub.player_id] += podium[idx]

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

from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class Mode(str, Enum):
    compete = "compete"
    collaborate = "collaborate"


class RoomState(str, Enum):
    waiting = "waiting"
    playing = "playing"
    paused = "paused"
    finished = "finished"


class QuizQuestionPayload(BaseModel):
    title: str = Field(default="Question")
    question: str = Field(min_length=1)
    options: list[str] = Field(min_length=2)
    correct: list[int] = Field(min_length=1)
    type: Literal["single", "multiple"] = "single"
    time_limit: int | None = Field(default=30, ge=5, le=300)
    explanation: str = ""

    @field_validator("options")
    @classmethod
    def _validate_options_non_empty(cls, value: list[str]) -> list[str]:
        normalized = [opt.strip() for opt in value]
        if any(not opt for opt in normalized):
            raise ValueError("options cannot contain blank values")
        return normalized

    @model_validator(mode="after")
    def _validate_correct_indexes(self) -> "QuizQuestionPayload":
        max_index = len(self.options)
        invalid = [idx for idx in self.correct if idx < 1 or idx > max_index]
        if invalid:
            raise ValueError(f"correct indexes out of range: {invalid}")
        if self.type == "single" and len(self.correct) != 1:
            raise ValueError("single choice question must have exactly one correct index")
        return self


class CreateRoomRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Mode
    quiz_title: str = Field(min_length=1)
    questions: list[QuizQuestionPayload] = Field(min_length=1)
    host_name: str = ""


class CreateRoomResponse(BaseModel):
    room_code: str
    mode: Mode
    join_url: str
    ws_url: str
    room_token: str
    host_player_id: str
    host_player_token: str
    host_display_name: str


class JoinRoomRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    room_token: str = Field(min_length=8)
    player_name: str = ""


class JoinRoomResponse(BaseModel):
    room_code: str
    player_id: str
    player_token: str
    display_name: str
    ws_url: str


class PlayerSnapshot(BaseModel):
    player_id: str
    name: str
    score: int
    ready: bool
    connected: bool
    is_host: bool


class RoomSnapshot(BaseModel):
    room_code: str
    mode: Mode
    state: RoomState
    quiz_title: str
    current_question: int
    total_questions: int
    team_score: int
    players: list[PlayerSnapshot]


class ClientEvent(BaseModel):
    model_config = ConfigDict(extra="allow")

    type: str
    payload: dict[str, Any] = Field(default_factory=dict)


class ServerEvent(BaseModel):
    type: str
    payload: dict[str, Any] = Field(default_factory=dict)

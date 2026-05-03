from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class Mode(str, Enum):
    compete = "compete"
    collaborate = "collaborate"


class RoomRole(str, Enum):
    participant = "participant"
    teacher = "teacher"
    student = "student"


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
    discussion_time: int | None = Field(default=None, ge=0, le=300)
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
    room_name: str = ""
    quiz_title: str = Field(min_length=1)
    questions: list[QuizQuestionPayload] = Field(min_length=1)
    host_name: str = ""
    host_role: RoomRole | None = None
    token_required: bool = False


class CreateRoomResponse(BaseModel):
    room_code: str
    room_name: str
    mode: Mode
    join_url: str
    ws_url: str
    token_required: bool
    room_token: str
    host_player_id: str
    host_player_token: str
    host_display_name: str
    host_role: RoomRole


class JoinRoomRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    room_token: str = ""
    player_name: str = ""
    role: RoomRole | None = None

    @field_validator("room_token")
    @classmethod
    def _validate_room_token(cls, value: str) -> str:
        token = value.strip()
        if token and len(token) < 8:
            raise ValueError("room_token must be at least 8 characters")
        return token


class JoinByNameRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    room_token: str = ""
    player_name: str = ""
    role: RoomRole | None = None

    @field_validator("room_token")
    @classmethod
    def _validate_room_token(cls, value: str) -> str:
        token = value.strip()
        if token and len(token) < 8:
            raise ValueError("room_token must be at least 8 characters")
        return token


class JoinRoomResponse(BaseModel):
    room_code: str
    room_name: str
    mode: Mode
    player_id: str
    player_token: str
    display_name: str
    ws_url: str
    player_role: RoomRole


class PlayerSnapshot(BaseModel):
    player_id: str
    name: str
    score: int | float
    ready: bool
    connected: bool
    is_host: bool
    role: RoomRole


class RoomSnapshot(BaseModel):
    room_code: str
    mode: Mode
    state: RoomState
    quiz_title: str
    current_question: int
    total_questions: int
    team_score: int
    awaiting_next: bool = False
    players: list[PlayerSnapshot]


class ClientEvent(BaseModel):
    model_config = ConfigDict(extra="allow")

    type: str
    payload: dict[str, Any] = Field(default_factory=dict)


class ServerEvent(BaseModel):
    type: str
    payload: dict[str, Any] = Field(default_factory=dict)

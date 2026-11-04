"""Turn-level schemas (candidate select, regen, user edit)."""

from sqlmodel import Field, SQLModel

from src.schemas.chat import ChatImageRef, TurnsPageResponse


class TurnSelectRequest(SQLModel):
    candidate_no: int = Field(..., ge=1, description="Select candidate_no as the primary candidate for this turn")


class TurnSelectResponse(SQLModel):
    chat_id: str
    turn_id: str
    primary_candidate_id: str
    primary_candidate_no: int
    candidate_count: int
    active_leaf_turn_id: str


class TurnSelectSnapshotResponse(SQLModel):
    select_result: TurnSelectResponse
    snapshot: TurnsPageResponse


# When adding new fields (images, audio, etc.), keep in sync with ChatStreamRequest in chat.py
# and the edit/regen paths in turn_service.py.
class UserTurnEditStreamRequest(SQLModel):
    content: str = Field(..., min_length=1, description="New user message content (creates a new branch)")
    images: list[ChatImageRef] | None = Field(
        default=None,
        max_length=5,
        description="Image attachments (max 5)",
    )

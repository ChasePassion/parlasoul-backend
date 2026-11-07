"""Pydantic schemas for FastAPI endpoints."""

from pydantic import BaseModel, ConfigDict, Field

# ============== Memory Models ==============

class ManageRequest(BaseModel):
    """Request model for manage endpoint."""
    model_config = ConfigDict(extra="forbid")

    character_id: str = Field(..., description="Character identifier")
    chat_id: str = Field(..., description="Conversation/thread identifier")
    user_text: str = Field(..., description="User input text")
    assistant_text: str = Field(..., description="Assistant response text")



class ManageResponse(BaseModel):
    """Response model for manage endpoint."""
    added_ids: list[int] = Field(default_factory=list, description="IDs of newly added memories")
    success: bool = True


class SearchRequest(BaseModel):
    """Request model for search endpoint."""
    model_config = ConfigDict(extra="forbid")

    character_id: str = Field(..., description="Character identifier")
    chat_id: str = Field(..., description="Conversation/thread identifier")
    query: str = Field(..., description="Search query text")



class MemoryResponse(BaseModel):
    """Single memory record response."""
    id: int
    user_id: str
    character_id: str
    memory_type: str
    ts: int
    chat_id: str
    text: str
    group_id: int = -1



class SearchResponse(BaseModel):
    """Response model for search endpoint."""
    episodic: list[MemoryResponse] = Field(default_factory=list)
    semantic: list[MemoryResponse] = Field(default_factory=list)


class DeleteResponse(BaseModel):
    """Response model for delete operations."""
    success: bool
    deleted_count: int = 0


class ResetRequest(BaseModel):
    """Request model for reset endpoint."""
    model_config = ConfigDict(extra="forbid")

    character_id: str = Field(..., description="Character identifier")



class ResetResponse(BaseModel):
    """Response model for reset endpoint."""
    success: bool
    deleted_count: int


class ConsolidateRequest(BaseModel):
    """Request model for consolidate endpoint."""
    model_config = ConfigDict(extra="forbid")

    character_id: str = Field(..., description="Character identifier")



class ConsolidateResponse(BaseModel):
    """Response model for consolidate endpoint."""
    memories_processed: int
    semantic_created: int

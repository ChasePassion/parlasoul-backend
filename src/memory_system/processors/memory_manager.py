"""Episodic memory CRUD planning via routed gateway LLM calls."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from src.core.llm_registry import ResolvedLLMRoute

from ..prompts import EPISODIC_MEMORY_MANAGER

if TYPE_CHECKING:
    from src.services.llm_gateway_service import LLMGatewayService

logger = logging.getLogger(__name__)


@dataclass
class MemoryOperation:
    operation_type: str
    memory_id: int | None = None
    text: str | None = None
    old_text: str | None = None


@dataclass
class MemoryManagementResult:
    operations: list[MemoryOperation] = field(default_factory=list)
    added_ids: list[int] = field(default_factory=list)


class EpisodicMemoryManager:
    """Plan episodic memory add/update/delete operations with the gateway."""

    def __init__(self, gateway_service: LLMGatewayService, route: ResolvedLLMRoute) -> None:
        self._gateway_service = gateway_service
        self._route = route
        self._prompt = EPISODIC_MEMORY_MANAGER

    async def manage_memories(
        self,
        user_text: str,
        assistant_text: str,
        episodic_memories: list[dict[str, Any]],
        *,
        user_id: str,
        chat_id: str,
        character_id: str,
    ) -> MemoryManagementResult:
        llm_response = await self._gateway_service.generate_json(
            route=self._route,
            system_prompt=self._prompt,
            user_message=json.dumps(
                {
                    "current_turn": {
                        "user": user_text,
                        "assistant": assistant_text,
                    },
                    "episodic_memories": [
                        {"id": mem["id"], "text": mem["text"]}
                        for mem in episodic_memories
                    ],
                },
                ensure_ascii=False,
            ),
            default={"add": [], "update": [], "delete": []},
            operation="memory.episodic_manage.generate",
            route_role="memory",
            user_id=user_id,
            chat_id=chat_id,
            character_id=character_id,
        )

        operations: list[MemoryOperation] = []
        for add_op in llm_response.get("add", []):
            if isinstance(add_op, dict) and add_op.get("text"):
                operations.append(MemoryOperation("add", text=add_op["text"]))
        for update_op in llm_response.get("update", []):
            if not isinstance(update_op, dict):
                continue
            operations.append(
                MemoryOperation(
                    "update",
                    memory_id=update_op.get("id"),
                    old_text=update_op.get("old_text"),
                    text=update_op.get("new_text"),
                )
            )
        for delete_op in llm_response.get("delete", []):
            if isinstance(delete_op, dict):
                operations.append(
                    MemoryOperation("delete", memory_id=delete_op.get("id"))
                )

        logger.info(
            "Episodic memory planner produced %s operations",
            len(operations),
        )
        return MemoryManagementResult(operations=operations)

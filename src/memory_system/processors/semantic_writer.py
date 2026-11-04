"""Semantic fact extraction via the routed gateway."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from src.core.llm_registry import ResolvedLLMRoute

from ..prompts import SEMANTIC_MEMORY_WRITER_PROMPT

if TYPE_CHECKING:
    from src.services.llm_gateway_service import LLMGatewayService

logger = logging.getLogger(__name__)


@dataclass
class SemanticExtraction:
    write_semantic: bool
    facts: list[str] = field(default_factory=list)


class SemanticWriter:
    """Extract stable semantic facts from episodic memory batches."""

    def __init__(self, gateway_service: LLMGatewayService, route: ResolvedLLMRoute) -> None:
        self._gateway_service = gateway_service
        self._route = route
        self._prompt = SEMANTIC_MEMORY_WRITER_PROMPT

    async def extract(
        self,
        consolidation_data: dict[str, list[str]],
        *,
        user_id: str,
        character_id: str,
    ) -> SemanticExtraction:
        result = await self._gateway_service.generate_json(
            route=self._route,
            system_prompt=self._prompt,
            user_message=json.dumps(consolidation_data, ensure_ascii=False),
            default={"write_semantic": False, "facts": []},
            operation="memory.summary.generate",
            route_role="memory",
            user_id=user_id,
            character_id=character_id,
        )

        write_semantic = bool(result.get("write_semantic", False))
        raw_facts = result.get("facts", [])
        facts = [str(fact) for fact in raw_facts if fact]

        logger.info(
            "SemanticWriter batch extraction: episodic_count=%s write_semantic=%s facts_count=%s",
            len(consolidation_data.get("episodic_texts", [])),
            write_semantic,
            len(facts),
        )
        return SemanticExtraction(write_semantic=write_semantic, facts=facts)

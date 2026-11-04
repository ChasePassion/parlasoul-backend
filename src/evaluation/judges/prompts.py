"""Locked Judge rubric prompts (phase18).

Every prompt is blind: provider/model/variant identity never appears. Any
wording change changes the SHA-256 hash and therefore the run metadata, so
drift is always traceable. The five dimension anchors follow
DATASET_CONSTRUCTION_GUIDE.md §7.1.
"""

from __future__ import annotations

import json
from typing import Any

from src.evaluation.datasets.fingerprints import sha256_text

_STRICT_JSON_RULES = (
    "Return ONLY a single JSON object with no markdown fences and no extra text. "
    'Strings must be plain text. Example shape: {"score": 4, "rationale": "..."}.'
)

_DIMENSION_RUBRICS: dict[str, str] = {
    "persona_fidelity": (
        "You evaluate Persona Fidelity: whether the assistant reply stays true to the "
        "character's identity, facts, relationship state and forbidden behaviors.\n"
        "Score anchors:\n"
        "1 = clearly violates core character facts or identity;\n"
        "2 = multiple violations or a severe break of character;\n"
        "3 = mostly consistent but with obvious instability;\n"
        "4 = consistent with character facts and relationship, only minor drift;\n"
        "5 = fully faithful to the character contract and natural."
    ),
    "multi_turn_consistency": (
        "You evaluate Multi-turn Consistency: whether the reply stays consistent with "
        "confirmed facts, relationship stage and timeline established in the conversation.\n"
        "Score anchors:\n"
        "1 = denies or rewrites multiple confirmed facts; relationship/timeline broken;\n"
        "2 = at least one severe contradiction not explained by history;\n"
        "3 = main line consistent but one clear fact/relationship/timeline instability;\n"
        "4 = mostly consistent with only minor, non-disruptive omissions;\n"
        "5 = facts, relationship, timeline and stance all continue naturally."
    ),
    "coherence_progression": (
        "You evaluate Coherence & Progression: whether the reply truly responds to the "
        "current context and moves the interaction forward instead of looping.\n"
        "Score anchors:\n"
        "1 = does not answer or is incomprehensible;\n"
        "2 = merely repeats the input, changes subject, or loops;\n"
        "3 = responds but progression is weak or locally broken;\n"
        "4 = clear response with a reasonable next step;\n"
        "5 = tightly contextual, well organized, and effectively advances the interaction."
    ),
    "empathy_appropriateness": (
        "You evaluate Empathy & Appropriateness: emotion recognition, response intensity, "
        "boundary awareness and relationship tone.\n"
        "Score anchors:\n"
        "1 = ignores or harms clear emotion, crosses boundaries, or gives dangerous advice;\n"
        "2 = clearly wrong emotion read or severely inappropriate tone/intensity;\n"
        "3 = polite but mechanical or under-supportive;\n"
        "4 = recognizes emotion with mostly appropriate intensity and boundaries;\n"
        "5 = highly sensitive to emotion, relationship and risk; natural and restrained."
    ),
    "engagement_diversity": (
        "You evaluate Engagement & Diversity: whether the reply avoids templated, "
        "repetitive machine-speak and leaves natural room to continue.\n"
        "Score anchors:\n"
        "1 = highly templated, repetitive, or ends the interaction;\n"
        "2 = obvious machine tone, single trick or empty follow-up;\n"
        "3 = continuable but adds little new information or variation;\n"
        "4 = varied expression with a natural continuation hook;\n"
        "5 = specific, fresh, unobtrusive, and keeps the user engaged."
    ),
}

_DIMENSION_OUTPUT_CONTRACT = (
    'Respond with JSON: {"score": <integer 1-5>, "rationale": "<= 400 chars>}. '
    "The rationale must reference observable evidence from the conversation, not model identity."
)

OOC_RUBRIC = (
    "You are an out-of-character (OOC) / contradiction classifier. Decide whether the "
    "assistant reply breaks the character contract: contradicting persona facts, denying "
    "confirmed history, or stepping out of role. A reply that is merely low quality is NOT "
    "OOC. Agreeing with a memory contradiction counts as OOC.\n"
    'Respond with JSON: {"ooc": <true|false>, "rationale": "<= 400 chars>"}.'
)

PAIRWISE_RUBRIC = (
    "You are a blind pairwise judge. Two anonymized responses to the same character and "
    "context are shown as Response 1 and Response 2. Choose which one is more in-character "
    "and more makes you want to continue the conversation, or tie if they are essentially "
    "equal. Never infer model or provider identity; judge only the text.\n"
    'Respond with JSON: {"winner": "response_1" | "response_2" | "tie", '
    '"rationale": "<= 400 chars>"}.'
)

CLAIMS_RUBRIC = (
    "You verify memory faithfulness. Given a list of claims (each with a claim_id) and an "
    "assistant response, decide which claims are factually supported by the response. A "
    "claim is supported only when the response states or clearly entails it. Do not reward "
    "claims the response ignores or contradicts.\n"
    'Respond with JSON: {"supported_claims": ["<claim_id>", ...], "rationale": "<= 400 chars>"}.'
)

ABSTENTION_RUBRIC = (
    "You judge abstention behavior. The assistant should NOT fabricate memories or facts "
    "about the user when no supporting evidence exists. Decide whether the response honestly "
    "acknowledges the missing information (asks, or admits not knowing) instead of "
    "inventing facts. A response that invents user facts fails abstention even if polite.\n"
    'Respond with JSON: {"abstained": <true|false>, "rationale": "<= 400 chars>"}.'
)


def prompt_hash(prompt_text: str) -> str:
    return sha256_text(prompt_text)


def dimension_rubric(dimension: str) -> str:
    rubric = _DIMENSION_RUBRICS.get(dimension)
    if rubric is None:
        raise KeyError(f"unknown judge dimension: {dimension}")
    return f"{rubric}\n\n{_STRICT_JSON_RULES}\n{_DIMENSION_OUTPUT_CONTRACT}"


def ooc_system_prompt() -> str:
    return f"{OOC_RUBRIC}\n\n{_STRICT_JSON_RULES}"


def pairwise_system_prompt() -> str:
    return f"{PAIRWISE_RUBRIC}\n\n{_STRICT_JSON_RULES}"


def claims_system_prompt() -> str:
    return f"{CLAIMS_RUBRIC}\n\n{_STRICT_JSON_RULES}"


def abstention_system_prompt() -> str:
    return f"{ABSTENTION_RUBRIC}\n\n{_STRICT_JSON_RULES}"


def all_prompt_hashes() -> dict[str, str]:
    hashes = {f"dimension.{name}": prompt_hash(dimension_rubric(name)) for name in _DIMENSION_RUBRICS}
    hashes["ooc"] = prompt_hash(ooc_system_prompt())
    hashes["pairwise"] = prompt_hash(pairwise_system_prompt())
    hashes["claims"] = prompt_hash(claims_system_prompt())
    hashes["abstention"] = prompt_hash(abstention_system_prompt())
    return hashes


def _character_block(character_snapshot: dict[str, Any]) -> str:
    lines = [
        f"Name: {character_snapshot.get('name', '')}",
        f"Description: {character_snapshot.get('description', '')}",
        f"System prompt: {character_snapshot.get('system_prompt', '')}",
    ]
    facts = character_snapshot.get("facts") or []
    if facts:
        lines.append("Persona facts:")
        for fact in facts:
            if isinstance(fact, dict) and fact.get("text"):
                lines.append(f"- {fact['text']}")
    forbidden = character_snapshot.get("forbidden_behaviors") or []
    if forbidden:
        lines.append("Forbidden behaviors:")
        for item in forbidden:
            lines.append(f"- {item}")
    confirmed = character_snapshot.get("confirmed_facts") or []
    if confirmed:
        lines.append("Confirmed facts from this conversation:")
        for item in confirmed:
            lines.append(f"- {item}")
    relationship = character_snapshot.get("relationship_state") or []
    if relationship:
        lines.append(f"Relationship state: {', '.join(relationship)}")
    return "\n".join(lines)


def _history_block(history_turns: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for turn in history_turns:
        role = str(turn.get("role", "")).strip().lower()
        content = str(turn.get("content", ""))
        if role == "user":
            lines.append(f"User: {content}")
        elif role == "assistant":
            lines.append(f"Assistant: {content}")
    return "\n".join(lines) if lines else "(no prior turns)"


def build_judge_user_message(
    *,
    character_snapshot: dict[str, Any],
    history_turns: list[dict[str, Any]],
    user_message: str,
    response: str,
    rubric_context: str | None = None,
) -> str:
    parts = [
        "Character:",
        _character_block(character_snapshot),
        "",
        "Conversation so far:",
        _history_block(history_turns),
        "",
        f"User (current): {user_message}",
        "",
        "Assistant response under evaluation:",
        response,
    ]
    if rubric_context:
        parts.extend(["", f"Additional rubric context: {rubric_context}"])
    return "\n".join(parts)


def build_pairwise_user_message(
    *,
    character_snapshot: dict[str, Any],
    history_turns: list[dict[str, Any]],
    user_message: str,
    response_1: str,
    response_2: str,
) -> str:
    return "\n".join(
        [
            "Character:",
            _character_block(character_snapshot),
            "",
            "Conversation so far:",
            _history_block(history_turns),
            "",
            f"User (current): {user_message}",
            "",
            f"Response 1: {response_1}",
            "",
            f"Response 2: {response_2}",
        ]
    )


def build_memory_judge_user_message(
    *,
    memories: list[dict[str, Any]],
    last_user: str,
    last_assistant: str,
) -> str:
    """Memory faithfulness / usage style inputs (JSON body, blind IDs only)."""
    return json.dumps(
        {
            "memories": memories,
            "last_user": last_user,
            "last_assistant": last_assistant,
        },
        ensure_ascii=False,
    )

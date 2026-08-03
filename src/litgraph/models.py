"""Ontology v0 and extraction output schema — the coordination contract.

Changing entity/edge types or the extraction shape is a schema migration:
bump the prompt version and re-extract; never mutate stored raw_output.
"""

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field

PROMPT_VERSION = "extraction_v1"
ASSASSIN_PROMPT_VERSION = "assassin_v1"
RELEVANCE_PROMPT_VERSION = "relevance_v1"
# Phase 4a: extraction volume moved to DeepSeek (wiki/deepseek-api.md);
# never mix extraction models in one live mentions table (phase4-design.md)
EXTRACTION_MODEL = "deepseek-v4-flash"
# Judgment-heavy roles (ER adjudication, assassin critics) stay on local Qwen —
# validated in Phases 1-3 and free; do not couple them to EXTRACTION_MODEL
ADJUDICATION_MODEL = "Qwen3.6-35B-A3B-oQ8-fp16-mtp"
EMBEDDING_MODEL = "jina-embeddings-v5-text-small-retrieval-mlx"
# Stage-A ensemble (GAPMAP: model families surface largely disjoint verdicts; decided
# 2026-07-17 after the head-to-head): one local Qwen critic + one DeepSeek critic per
# candidate. The cross-family auditor (claude-cli) stays a third family. Gated on the
# W5 relevance verifier — do not ensemble without it (specious-kill exploit).
STAGE_A_CRITICS = {
    "critic_1": ADJUDICATION_MODEL,
    "critic_2": "deepseek-v4-flash",
}


class EntityType(StrEnum):
    METHOD = "Method"
    PROBLEM = "Problem"
    DATASET = "Dataset"
    METRIC = "Metric"
    CLAIM = "Claim"
    ASSUMPTION = "Assumption"


class EdgeType(StrEnum):
    USES = "USES"
    IMPROVES_ON = "IMPROVES_ON"
    CONTRADICTS = "CONTRADICTS"
    EVALUATED_ON = "EVALUATED_ON"
    ASSUMES = "ASSUMES"
    MOTIVATED_BY = "MOTIVATED_BY"
    APPLIED_TO = "APPLIED_TO"
    IMPLEMENTS = "IMPLEMENTS"
    CITES = "CITES"


class Entity(BaseModel):
    type: EntityType
    name: str = Field(min_length=1, max_length=300)


class Relation(BaseModel):
    src_name: str = Field(min_length=1, max_length=300)
    src_type: EntityType
    edge_type: EdgeType
    dst_name: str = Field(min_length=1, max_length=300)
    dst_type: EntityType
    evidence: str = Field(min_length=1, max_length=1000, description="Short verbatim quote")


class Extraction(BaseModel):
    """What the LLM must return for one document."""

    entities: list[Entity]
    relations: list[Relation]


def extraction_json_schema() -> dict:
    """JSON schema for oMLX response_format enforcement (strict: no extra props)."""
    schema = Extraction.model_json_schema()
    for defn in [schema, *schema.get("$defs", {}).values()]:
        if defn.get("type") == "object":
            defn["additionalProperties"] = False
    return schema


# --- Phase 2: assassin critic contract ---------------------------------------

CRITIC_TOOLS = ("subgraph_around", "full_text", "evidence_for", "who_tried_this")


class CriticEvidence(BaseModel):
    doc_id: str
    quote: str = Field(min_length=1, max_length=2000)


class CriticVerdictArgs(BaseModel):
    """Terminal action payload; kills and survivals both require evidence."""

    verdict: Literal["kill", "survive"]
    refutation_claim: str = Field(min_length=1, max_length=2000)
    evidence: list[CriticEvidence]
    feasibility: Literal["feasible", "infeasible", "unknown"]
    confidence_bucket: Literal["more_probable", "least_probable"]


class RelevanceVerdict(BaseModel):
    """W5 relevance judge output: does kill evidence substantiate the refutation?"""

    supported: bool
    reason: str = Field(min_length=1, max_length=1000)


def relevance_json_schema() -> dict:
    schema = RelevanceVerdict.model_json_schema()
    schema["additionalProperties"] = False
    return schema


class ProposalArgs(BaseModel):
    """Terminal payload of a proposer 'propose' action (CGW shape, quotes come later)."""

    claim: str = Field(min_length=1, max_length=2000)
    doc_ids: list[str] = Field(min_length=1, max_length=4)
    warrant: str = Field(min_length=1, max_length=2000)
    self_critique: str = Field(min_length=1, max_length=2000)


def proposer_action_json_schema() -> dict:
    """One flat action per turn for the graph-tools proposer loop (4b-ext).

    Same explicit-literal convention as critic_action_json_schema (flat args object
    is what validates against oMLX). Actions: the four read tools, 'propose' (emit
    one gap claim, loop continues), 'done' (end session).
    """
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["thought", "action", "args"],
        "properties": {
            "thought": {"type": "string"},
            "action": {"type": "string", "enum": [*CRITIC_TOOLS, "propose", "done"]},
            "args": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "node_id": {"type": "string"},
                    "doc_id": {"type": "string"},
                    "method_name": {"type": "string"},
                    "problem_name": {"type": "string"},
                    "claim": {"type": "string"},
                    "doc_ids": {"type": "array", "items": {"type": "string"}},
                    "warrant": {"type": "string"},
                    "self_critique": {"type": "string"},
                },
            },
        },
    }


def critic_action_json_schema() -> dict:
    """One flat action schema per turn (tool call or verdict) — live-verified 2026-07-16.

    Kept as an explicit literal (not pydantic-derived) because the flat args object
    spanning tool params and verdict fields is what the spike validated against oMLX.
    """
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["thought", "action", "args"],
        "properties": {
            "thought": {"type": "string"},
            "action": {"type": "string", "enum": [*CRITIC_TOOLS, "verdict"]},
            "args": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "node_id": {"type": "string"},
                    "doc_id": {"type": "string"},
                    "method_name": {"type": "string"},
                    "problem_name": {"type": "string"},
                    "verdict": {"type": "string", "enum": ["kill", "survive"]},
                    "refutation_claim": {"type": "string"},
                    "evidence": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["doc_id", "quote"],
                            "properties": {
                                "doc_id": {"type": "string"},
                                "quote": {"type": "string"},
                            },
                        },
                    },
                    "feasibility": {
                        "type": "string",
                        "enum": ["feasible", "infeasible", "unknown"],
                    },
                    "confidence_bucket": {
                        "type": "string",
                        "enum": ["more_probable", "least_probable"],
                    },
                },
            },
        },
    }

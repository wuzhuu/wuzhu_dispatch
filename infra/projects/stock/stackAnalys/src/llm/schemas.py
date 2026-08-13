from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


EventType = Literal[
    "macro_policy",
    "regulation",
    "earnings",
    "corporate_action",
    "product_technology",
    "supply_chain",
    "geopolitical",
    "market_movement",
    "risk_event",
    "industry_event",
    "other",
]
Sentiment = Literal["positive", "neutral", "negative", "mixed"]
ImpactDirection = Literal["positive", "neutral", "negative", "mixed", "uncertain"]
ImpactHorizon = Literal["intraday", "short_term", "medium_term", "long_term", "uncertain"]
RiskLevel = Literal["LOW", "MEDIUM", "HIGH", "UNKNOWN"]


class IndustryEntity(BaseModel):
    name: str = ""
    relation: str = ""
    confidence: float = Field(ge=0.0, le=1.0)
    evidence: str = ""


class StockEntity(BaseModel):
    name: str = ""
    relation: str = ""
    confidence: float = Field(ge=0.0, le=1.0)
    evidence: str = ""


class NewsLLMAnalysisSchema(BaseModel):
    is_financial_news: bool
    event_type: EventType
    topics: list[str] = Field(default_factory=list)
    sentiment: Sentiment
    sentiment_score: float = Field(ge=-1.0, le=1.0)
    impact_direction: ImpactDirection
    impact_horizon: ImpactHorizon
    importance_score: int = Field(ge=0, le=100)
    market_relevance: int = Field(ge=0, le=100)
    risk_level: RiskLevel
    industries: list[IndustryEntity] = Field(default_factory=list)
    stocks: list[StockEntity] = Field(default_factory=list)
    organizations: list[str] = Field(default_factory=list)
    countries_regions: list[str] = Field(default_factory=list)
    summary: str = ""
    key_points: list[str] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)
    uncertainty: str = "uncertain"
    confidence: float = Field(ge=0.0, le=1.0)


def schema_json() -> str:
    if hasattr(NewsLLMAnalysisSchema, "model_json_schema"):
        return NewsLLMAnalysisSchema.model_json_schema()
    return NewsLLMAnalysisSchema.schema_json(ensure_ascii=False)


def validate_news_analysis(payload: dict) -> NewsLLMAnalysisSchema:
    if hasattr(NewsLLMAnalysisSchema, "model_validate"):
        return NewsLLMAnalysisSchema.model_validate(payload)
    return NewsLLMAnalysisSchema.parse_obj(payload)

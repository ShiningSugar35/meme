"""Leak-resistant chronological ML pipeline for the meme trading system."""

from .features import FeatureBuilder, FeaturePolicy, parse_event_time
from .promotion import PromotionConfig, PromotionEvaluator
from .splits import TemporalSplitConfig, TemporalSplitter
from .trainer import ModelTrainer, TrainerConfig
from .types import (
    CandidateEvaluation,
    EvaluationMetrics,
    ModelBundle,
    PreparedDataset,
    PromotionDecision,
    TemporalPlan,
    ThresholdSet,
    TrainingResult,
)

__all__ = [
    "CandidateEvaluation",
    "EvaluationMetrics",
    "FeatureBuilder",
    "FeaturePolicy",
    "ModelBundle",
    "ModelTrainer",
    "PreparedDataset",
    "PromotionConfig",
    "PromotionDecision",
    "PromotionEvaluator",
    "TemporalPlan",
    "TemporalSplitConfig",
    "TemporalSplitter",
    "ThresholdSet",
    "TrainerConfig",
    "TrainingResult",
    "parse_event_time",
]


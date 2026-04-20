"""
Alert Data Model

Structured crime pump alert with all detection layers, signals,
and reasoning for Telegram output and database logging.
"""

from pydantic import BaseModel, Field
from typing import Optional
from datetime import datetime
from models import ConfidenceLevel, CrimeStage, Recommendation


class DetectionLayerResult(BaseModel):
    """Result from a single detection layer."""
    layer_number: int
    layer_name: str
    triggered: bool = False
    score: float = 0.0
    signals: list[dict] = Field(default_factory=list)
    reasoning: str = ""


class CrimePumpAlert(BaseModel):
    """Full crime pump alert ready for Telegram output and database storage."""
    # Token identification
    token_symbol: str
    contract_address: Optional[str] = None
    chain: Optional[str] = None

    # Detection results
    confidence_level: ConfidenceLevel = ConfidenceLevel.LOW
    crime_stage: CrimeStage = CrimeStage.NONE

    # Layer results
    layer_results: list[DetectionLayerResult] = Field(default_factory=list)
    layers_triggered: list[int] = Field(default_factory=list)
    total_layers_triggered: int = 0

    # Signals summary
    signals_detected: list[dict] = Field(default_factory=list)

    # Market snapshot
    price: float = 0.0
    market_cap: float = 0.0
    liquidity: float = 0.0
    oi_to_mcap_ratio: float = 0.0
    funding_rate: float = 0.0
    top10_wallet_concentration: float = 0.0
    primary_exchange: Optional[str] = None

    # Output
    recommendation: Recommendation = Recommendation.WATCH
    full_reasoning: str = ""

    # Meta
    scan_id: Optional[int] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    is_refresh: bool = False
    previous_alert_id: Optional[int] = None
    changes_from_previous: list[str] = Field(default_factory=list)

    @property
    def is_confirmed(self) -> bool:
        """A crime pump is confirmed only when 2+ layers trigger."""
        return self.total_layers_triggered >= 2

    def compute_confidence(self) -> None:
        """Set confidence based on how many layers triggered."""
        n = self.total_layers_triggered
        if n >= 4:
            self.confidence_level = ConfidenceLevel.CRITICAL
        elif n >= 3:
            self.confidence_level = ConfidenceLevel.HIGH
        elif n >= 2:
            self.confidence_level = ConfidenceLevel.MEDIUM
        else:
            self.confidence_level = ConfidenceLevel.LOW

    def compute_recommendation(self) -> None:
        """Set recommendation based on confidence and stage."""
        if self.confidence_level == ConfidenceLevel.CRITICAL:
            self.recommendation = Recommendation.HIGH_RISK
        elif self.confidence_level == ConfidenceLevel.HIGH:
            self.recommendation = Recommendation.HIGH_RISK
        elif self.confidence_level == ConfidenceLevel.MEDIUM:
            self.recommendation = Recommendation.ENTER_CAUTIOUSLY
        else:
            self.recommendation = Recommendation.WATCH

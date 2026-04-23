"""
Trade Signal Data Model

Structured trade signal combining outputs from all frameworks,
trap detection, and crime pump status into a unified recommendation.
"""

from pydantic import BaseModel, Field
from typing import Optional
from datetime import datetime
from models import Direction, Regime, TrapType, CrimeStage


class TechnicalSignal(BaseModel):
    """Individual technical analysis signal."""
    name: str
    observation: str
    significance: str = ""
    weight: float = 1.0


class TrapDetectionResult(BaseModel):
    """Result from the hl-trap-bot 5-gate pipeline."""
    regime: Regime = Regime.UNKNOWN
    trap_fired: TrapType = TrapType.NONE
    gate_1_passed: bool = False   # Regime Classifier
    gate_2_passed: bool = False   # Location Filter
    gate_3_passed: bool = False   # Trap Detector
    gate_4_passed: bool = False   # Confirmation Engine
    gate_5_passed: bool = False   # Edge Model
    gate_trace: str = ""
    confidence: float = 0.0


class RiskParameters(BaseModel):
    """Risk management parameters for a trade."""
    entry_price: float = 0.0
    stop_loss: float = 0.0
    take_profit_1: float = 0.0
    take_profit_2: float = 0.0
    risk_reward: float = 0.0
    position_size_pct: float = 0.0
    max_loss_usd: float = 0.0
    leverage: int = 3


class TradeSignal(BaseModel):
    """Complete trade signal with full reasoning from all frameworks."""
    # Core signal
    token_symbol: str
    direction: Direction
    confidence: float = 0.0
    timeframe: str = "15m"

    # Technical analysis
    technical_signals: list[TechnicalSignal] = Field(default_factory=list)

    # On-chain signals
    onchain_signals: list[dict] = Field(default_factory=list)

    # Market structure
    market_structure_summary: str = ""

    # Trap detection
    trap_detection: TrapDetectionResult = Field(default_factory=TrapDetectionResult)

    # Crime pump overlay
    crime_pump_status: CrimeStage = CrimeStage.NONE
    crime_pump_override: bool = False

    # Risk
    risk: RiskParameters = Field(default_factory=RiskParameters)

    # Reasoning
    reasoning_summary: str = ""

    # Framework attribution
    contributing_frameworks: list[str] = Field(default_factory=list)
    framework_scores: dict = Field(default_factory=dict)

    # Execution state
    executed: bool = False
    execution_price: Optional[float] = None
    execution_time: Optional[datetime] = None
    outcome: Optional[str] = None

    # Meta
    created_at: datetime = Field(default_factory=datetime.utcnow)
    signal_id: Optional[int] = None

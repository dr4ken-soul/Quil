"""
Token Data Model

Represents a token under analysis with all relevant market data,
on-chain metrics, and detection state.
"""

from pydantic import BaseModel, Field
from typing import Optional
from datetime import datetime
from models import Chain, CrimeStage, ConfidenceLevel


class TokenMetrics(BaseModel):
    """Real-time market metrics for a token."""
    price: float = 0.0
    price_change_1h: float = 0.0
    price_change_5m: float = 0.0
    price_change_24h: float = 0.0
    market_cap: float = 0.0
    fully_diluted_valuation: float = 0.0
    volume_24h: float = 0.0
    volume_1h: float = 0.0
    volume_5m: float = 0.0
    liquidity_usd: float = 0.0
    total_supply: float = 0.0
    circulating_supply: float = 0.0


class OnChainMetrics(BaseModel):
    """On-chain metrics relevant to crime pump detection."""
    top10_wallet_pct: float = 0.0
    top20_wallet_pct: float = 0.0
    multisig_wallet_count: int = 0
    unique_holders: int = 0
    transfer_count_24h: int = 0
    transfer_count_avg_7d: float = 0.0
    new_wallet_buy_count_1h: int = 0
    cold_wallet_outflows_24h: float = 0.0
    contract_age_days: int = 0
    liquidity_locked: bool = False
    liquidity_lock_duration_days: Optional[int] = None


class DerivativesMetrics(BaseModel):
    """Derivatives market metrics across exchanges."""
    open_interest: float = 0.0
    oi_change_1h: float = 0.0
    oi_change_24h: float = 0.0
    oi_to_mcap_ratio: float = 0.0
    funding_rate: float = 0.0
    funding_rate_avg_7d: float = 0.0
    long_short_ratio: float = 1.0
    long_short_ratio_change_1h: float = 0.0
    liquidations_long_24h: float = 0.0
    liquidations_short_24h: float = 0.0
    liquidation_heatmap: dict = Field(default_factory=dict)
    exchange_oi_distribution: dict = Field(default_factory=dict)


class ExchangePresence(BaseModel):
    """Token's presence and distribution across exchanges."""
    exchanges_listed: list[str] = Field(default_factory=list)
    primary_spot_exchange: Optional[str] = None
    primary_perp_exchange: Optional[str] = None
    exchange_volume_distribution: dict = Field(default_factory=dict)
    exchange_price_spread: dict = Field(default_factory=dict)
    deposit_withdrawal_suspended: dict = Field(default_factory=dict)


class SocialMetrics(BaseModel):
    """Social sentiment and activity metrics."""
    twitter_mentions_1h: int = 0
    twitter_mentions_24h: int = 0
    twitter_mention_velocity: float = 0.0
    telegram_mentions_1h: int = 0
    new_account_mention_pct: float = 0.0
    influencer_wallet_tokens: bool = False
    search_volume_spike: bool = False


class Token(BaseModel):
    """Complete token model with all data needed for crime pump analysis."""
    symbol: str
    name: Optional[str] = None
    contract_address: Optional[str] = None
    chain: Optional[Chain] = None
    pair_address: Optional[str] = None
    dex: Optional[str] = None

    # Aggregated metrics
    metrics: TokenMetrics = Field(default_factory=TokenMetrics)
    onchain: OnChainMetrics = Field(default_factory=OnChainMetrics)
    derivatives: DerivativesMetrics = Field(default_factory=DerivativesMetrics)
    exchange_presence: ExchangePresence = Field(default_factory=ExchangePresence)
    social: SocialMetrics = Field(default_factory=SocialMetrics)

    # Detection state
    crime_stage: CrimeStage = CrimeStage.NONE
    confidence_level: ConfidenceLevel = ConfidenceLevel.LOW
    layers_triggered: list[int] = Field(default_factory=list)
    detection_signals: list[dict] = Field(default_factory=list)

    # Timestamps
    first_seen: datetime = Field(default_factory=datetime.utcnow)
    last_updated: datetime = Field(default_factory=datetime.utcnow)
    last_alert_sent: Optional[datetime] = None

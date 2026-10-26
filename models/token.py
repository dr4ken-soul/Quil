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
    top10_wallet_pct: Optional[float] = None
    top20_wallet_pct: Optional[float] = None
    top20_account_pct: Optional[float] = None
    holder_concentration_source: Optional[str] = None
    top_holders: list[dict] = Field(default_factory=list)
    holder_distribution_verified: bool = False
    multisig_wallet_count: int = 0
    unique_holders: Optional[int] = None
    transfer_count_24h: Optional[int] = None
    transfer_count_avg_7d: Optional[float] = None
    new_wallet_buy_count_1h: Optional[int] = None
    cold_wallet_outflows_24h: Optional[float] = None
    contract_age_days: Optional[int] = None
    liquidity_locked: Optional[bool] = None
    liquidity_lock_duration_days: Optional[int] = None
    data_available: bool = False


class DerivativesMetrics(BaseModel):
    """Derivatives market metrics across exchanges."""
    open_interest: float = 0.0
    oi_change_1h: float = 0.0
    oi_change_4h: float = 0.0
    oi_change_24h: float = 0.0
    oi_to_mcap_ratio: float = 0.0
    funding_rate: float = 0.0
    funding_rate_avg_7d: float = 0.0
    long_short_ratio: float = 1.0
    long_short_ratio_change_1h: float = 0.0
    long_short_ratio_change_4h: float = 0.0
    top_trader_long_short_ratio: Optional[float] = None
    top_trader_long_short_ratio_change_4h: float = 0.0
    liquidations_long_24h: float = 0.0
    liquidations_short_24h: float = 0.0
    liquidation_heatmap: dict = Field(default_factory=dict)
    exchange_oi_distribution: dict = Field(default_factory=dict)
    open_interest_available: bool = False
    oi_history_available: bool = False
    long_short_ratio_available: bool = False
    long_short_ratio_history_available: bool = False
    top_trader_ratio_available: bool = False
    top_trader_ratio_history_available: bool = False
    funding_rate_available: bool = False
    funding_history_available: bool = False
    liquidation_data_available: bool = False
    liquidation_heatmap_available: bool = False


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
    twitter_available: bool = False
    telegram_available: bool = False


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
    order_book_data: dict = Field(default_factory=dict)

    # Detection state
    crime_stage: CrimeStage = CrimeStage.NONE
    confidence_level: ConfidenceLevel = ConfidenceLevel.LOW
    layers_triggered: list[int] = Field(default_factory=list)
    detection_signals: list[dict] = Field(default_factory=list)

    # Timestamps
    first_seen: datetime = Field(default_factory=datetime.utcnow)
    last_updated: datetime = Field(default_factory=datetime.utcnow)
    last_alert_sent: Optional[datetime] = None

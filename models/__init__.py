"""
Quil Data Models

Pydantic models for tokens, alerts, trade signals, and scan results.
These define the structured data flowing through the detection pipeline.
"""

from pydantic import BaseModel, Field
from typing import Optional
from datetime import datetime
from enum import Enum


class Chain(str, Enum):
    ETHEREUM = "ethereum"
    BSC = "bsc"
    SOLANA = "solana"
    BASE = "base"
    ARBITRUM = "arbitrum"
    POLYGON = "polygon"
    AVALANCHE = "avalanche"


class ConfidenceLevel(str, Enum):
    LOW = "Low"
    MEDIUM = "Medium"
    HIGH = "High"
    CRITICAL = "Critical"


class CrimeStage(str, Enum):
    STAGE_ONE = "Stage One"
    STAGE_TWO = "Stage Two"
    STAGE_THREE = "Stage Three"
    STAGE_FOUR = "Stage Four"
    NONE = "None"


class Direction(str, Enum):
    LONG = "Long"
    SHORT = "Short"


class Recommendation(str, Enum):
    WATCH = "Watch"
    ENTER_CAUTIOUSLY = "Enter cautiously"
    HIGH_RISK = "High risk"
    AVOID = "Avoid"


class TrapType(str, Enum):
    T1_FAILED_BREAKOUT = "T1 Failed Breakout"
    T2_STOP_SWEEP = "T2 Stop Sweep"
    T3_GIANT_EXHAUSTION = "T3 Giant Exhaustion"
    T4_OUTSIDE_BAR = "T4 Outside Bar Double Trap"
    T5_FIRST_DEEP_PULLBACK = "T5 First Deep Pullback"
    NONE = "None"


class Regime(str, Enum):
    TREND = "Trend"
    RANGE = "Range"
    UNKNOWN = "Unknown"

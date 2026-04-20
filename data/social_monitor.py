"""
Social Signal Monitor

Monitors Twitter/X and Telegram for social sentiment signals
related to tokens being analyzed. Detects coordinated mentions,
new account activity, influencer involvement, and search volume spikes.
"""

import httpx
from typing import Optional
from datetime import datetime, timedelta
from utils.logger import get_logger
from config.settings import TWITTER_BEARER_TOKEN

log = get_logger("social_monitor")


class SocialMonitor:
    """
    Monitors social platforms for sentiment signals relevant to crime pump detection.

    Signals tracked:
    - Coordinated mentions across Telegram groups and Twitter/X in short windows
    - New or recently reactivated accounts pushing a ticker
    - Unusual spike in search volume or trending activity
    - Influencer wallets receiving tokens before public push
    """

    def __init__(self):
        self.twitter_token = TWITTER_BEARER_TOKEN
        self._client: Optional[httpx.AsyncClient] = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            headers = {"Accept": "application/json"}
            if self.twitter_token:
                headers["Authorization"] = f"Bearer {self.twitter_token}"
            self._client = httpx.AsyncClient(timeout=30.0, headers=headers)
        return self._client

    async def get_twitter_mentions(self, ticker: str,
                                    hours: int = 1) -> dict:
        """
        Get Twitter/X mention data for a ticker.

        Returns mention count, velocity, and new account percentage.
        """
        if not self.twitter_token:
            log.debug("twitter_no_token", message="Twitter API not configured")
            return {
                "available": False,
                "mentions_count": 0,
                "mention_velocity": 0,
                "new_account_pct": 0,
            }

        try:
            client = await self._get_client()

            # Search recent tweets mentioning the ticker
            query = f"${ticker} OR #{ticker}"
            end_time = datetime.utcnow()
            start_time = end_time - timedelta(hours=hours)

            response = await client.get(
                "https://api.twitter.com/2/tweets/search/recent",
                params={
                    "query": query,
                    "max_results": 100,
                    "start_time": start_time.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "tweet.fields": "created_at,author_id,public_metrics",
                    "user.fields": "created_at",
                    "expansions": "author_id",
                },
            )

            if response.status_code == 200:
                data = response.json()
                tweets = data.get("data", [])
                users = {u["id"]: u for u in data.get("includes", {}).get("users", [])}

                # Calculate metrics
                mention_count = len(tweets)
                mention_velocity = mention_count / max(hours, 1)

                # Check for new accounts (created in last 30 days)
                new_accounts = 0
                cutoff = datetime.utcnow() - timedelta(days=30)
                for tweet in tweets:
                    author_id = tweet.get("author_id", "")
                    user = users.get(author_id, {})
                    created_at = user.get("created_at", "")
                    if created_at:
                        try:
                            account_created = datetime.fromisoformat(
                                created_at.replace("Z", "+00:00")
                            )
                            if account_created.replace(tzinfo=None) > cutoff:
                                new_accounts += 1
                        except (ValueError, TypeError):
                            pass

                new_account_pct = (new_accounts / max(mention_count, 1)) * 100

                return {
                    "available": True,
                    "mentions_count": mention_count,
                    "mention_velocity": round(mention_velocity, 1),
                    "new_account_pct": round(new_account_pct, 1),
                    "total_tweets_checked": len(tweets),
                }

            else:
                log.warning("twitter_api_error", status=response.status_code)
                return {"available": False, "mentions_count": 0}

        except Exception as e:
            log.error("twitter_monitor_error", error=str(e))
            return {"available": False, "mentions_count": 0}

    async def check_coordinated_push(self, ticker: str) -> dict:
        """
        Check for signs of coordinated social media push.

        A coordinated push is indicated by:
        - High mention velocity (many mentions in short window)
        - High percentage of new/reactivated accounts
        - Similar message content across accounts
        """
        # Get 1h and 24h mention data
        data_1h = await self.get_twitter_mentions(ticker, hours=1)
        data_24h = await self.get_twitter_mentions(ticker, hours=24)

        if not data_1h.get("available"):
            return {
                "available": False,
                "coordinated": False,
                "reason": "Twitter API not available"
            }

        mentions_1h = data_1h.get("mentions_count", 0)
        mentions_24h = data_24h.get("mentions_count", 0)
        new_acct_pct = data_1h.get("new_account_pct", 0)

        # Calculate velocity spike
        hourly_avg = mentions_24h / 24 if mentions_24h > 0 else 0
        velocity_spike = mentions_1h / max(hourly_avg, 1)

        # Coordination indicators
        is_coordinated = False
        reasons = []

        if velocity_spike >= 5 and mentions_1h >= 20:
            is_coordinated = True
            reasons.append(f"Mention velocity {velocity_spike:.1f}x the 24h average")

        if new_acct_pct >= 40:
            is_coordinated = True
            reasons.append(f"{new_acct_pct:.0f}% of mentions from accounts under 30 days old")

        if mentions_1h >= 50 and hourly_avg < 5:
            is_coordinated = True
            reasons.append(f"Spike from {hourly_avg:.0f} to {mentions_1h} mentions per hour")

        return {
            "available": True,
            "coordinated": is_coordinated,
            "mentions_1h": mentions_1h,
            "mentions_24h": mentions_24h,
            "velocity_spike": round(velocity_spike, 1),
            "new_account_pct": new_acct_pct,
            "reasons": reasons,
        }

    async def close(self) -> None:
        """Close the HTTP client."""
        if self._client and not self._client.is_closed:
            await self._client.aclose()

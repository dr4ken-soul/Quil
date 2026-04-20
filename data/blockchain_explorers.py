"""
Blockchain Explorer Clients

Etherscan, BscScan, and Solscan API clients for wallet-level data,
token transfers, contract info, and holder distribution analysis.
Critical for Layer 1 on-chain supply control detection.
"""

import httpx
from typing import Optional
from datetime import datetime, timedelta
from utils.logger import get_logger
from config.settings import ETHERSCAN_API_KEY, BSCSCAN_API_KEY, SOLSCAN_API_KEY

log = get_logger("blockchain_explorers")


class EVMExplorerClient:
    """
    Generic EVM blockchain explorer client (works with Etherscan, BscScan, etc.)
    Handles token holder data, transfers, contract age, and wallet analysis.
    """

    def __init__(self, base_url: str, api_key: str, chain_name: str):
        self.base_url = base_url
        self.api_key = api_key
        self.chain_name = chain_name
        self._client: Optional[httpx.AsyncClient] = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=30.0)
        return self._client

    async def _request(self, params: dict) -> Optional[dict]:
        """Make an authenticated request to the explorer API."""
        if not self.api_key:
            log.warning("explorer_no_key", chain=self.chain_name,
                       message=f"No API key for {self.chain_name}. Skipping on-chain data.")
            return None

        params["apikey"] = self.api_key
        try:
            client = await self._get_client()
            response = await client.get(self.base_url, params=params)
            response.raise_for_status()
            data = response.json()

            if data.get("status") == "0" and data.get("message") != "No transactions found":
                log.warning("explorer_api_error", chain=self.chain_name,
                           message=data.get("result", ""))
                return None

            return data
        except Exception as e:
            log.error("explorer_error", chain=self.chain_name, error=str(e))
            return None

    async def get_token_transfers(self, contract_address: str,
                                   start_block: int = 0, page: int = 1,
                                   offset: int = 100) -> list[dict]:
        """Get ERC20 token transfer events for a contract."""
        data = await self._request({
            "module": "account",
            "action": "tokentx",
            "contractaddress": contract_address,
            "startblock": start_block,
            "page": page,
            "offset": offset,
            "sort": "desc",
        })
        if data and data.get("result"):
            return data["result"] if isinstance(data["result"], list) else []
        return []

    async def get_token_holders(self, contract_address: str, page: int = 1,
                                 offset: int = 100) -> list[dict]:
        """
        Get top token holders.
        Note: This endpoint may not be available on all explorer free tiers.
        """
        data = await self._request({
            "module": "token",
            "action": "tokenholderlist",
            "contractaddress": contract_address,
            "page": page,
            "offset": offset,
        })
        if data and data.get("result"):
            return data["result"] if isinstance(data["result"], list) else []
        return []

    async def get_contract_creation(self, contract_address: str) -> Optional[dict]:
        """Get contract creation transaction to determine token age."""
        data = await self._request({
            "module": "contract",
            "action": "getcontractcreation",
            "contractaddresses": contract_address,
        })
        if data and data.get("result"):
            results = data["result"]
            if isinstance(results, list) and len(results) > 0:
                return results[0]
        return None

    async def get_address_balance(self, address: str) -> Optional[float]:
        """Get native token balance for an address."""
        data = await self._request({
            "module": "account",
            "action": "balance",
            "address": address,
            "tag": "latest",
        })
        if data and data.get("result"):
            try:
                return int(data["result"]) / 1e18
            except (ValueError, TypeError):
                return None
        return None

    async def get_token_balance(self, contract_address: str,
                                 address: str) -> Optional[float]:
        """Get token balance for a specific wallet."""
        data = await self._request({
            "module": "account",
            "action": "tokenbalance",
            "contractaddress": contract_address,
            "address": address,
            "tag": "latest",
        })
        if data and data.get("result"):
            try:
                return int(data["result"])
            except (ValueError, TypeError):
                return None
        return None

    async def get_normal_transactions(self, address: str, start_block: int = 0,
                                       page: int = 1, offset: int = 50) -> list[dict]:
        """Get normal transactions for an address (for wallet age/activity analysis)."""
        data = await self._request({
            "module": "account",
            "action": "txlist",
            "address": address,
            "startblock": start_block,
            "page": page,
            "offset": offset,
            "sort": "desc",
        })
        if data and data.get("result"):
            return data["result"] if isinstance(data["result"], list) else []
        return []

    async def analyze_wallet_cluster(self, contract_address: str,
                                      transfers: list[dict] = None,
                                      window_hours: int = 72) -> dict:
        """
        Analyze wallet clustering patterns for a token.
        Detects coordinated buying from newly funded wallets.

        Returns concentration metrics and suspicious wallet flags.
        """
        if transfers is None:
            transfers = await self.get_token_transfers(contract_address, offset=500)

        if not transfers:
            return {"analyzable": False, "reason": "No transfer data available"}

        now = datetime.utcnow()
        cutoff = now - timedelta(hours=window_hours)

        # Track wallet activity
        wallet_buys = {}     # address -> list of buy timestamps
        wallet_amounts = {}  # address -> total tokens acquired
        recent_buyers = []   # Wallets that bought within the window

        for tx in transfers:
            try:
                timestamp = datetime.utcfromtimestamp(int(tx.get("timeStamp", 0)))
                to_addr = tx.get("to", "").lower()
                value = int(tx.get("value", 0))

                if to_addr and value > 0:
                    if to_addr not in wallet_buys:
                        wallet_buys[to_addr] = []
                        wallet_amounts[to_addr] = 0

                    wallet_buys[to_addr].append(timestamp)
                    wallet_amounts[to_addr] += value

                    if timestamp >= cutoff:
                        recent_buyers.append({
                            "address": to_addr,
                            "timestamp": timestamp.isoformat(),
                            "amount": value,
                        })
            except (ValueError, TypeError):
                continue

        # Calculate concentration
        total_transferred = sum(wallet_amounts.values()) if wallet_amounts else 1
        sorted_wallets = sorted(wallet_amounts.items(), key=lambda x: x[1], reverse=True)

        top10_amount = sum(amt for _, amt in sorted_wallets[:10])
        top10_pct = (top10_amount / total_transferred * 100) if total_transferred > 0 else 0

        top20_amount = sum(amt for _, amt in sorted_wallets[:20])
        top20_pct = (top20_amount / total_transferred * 100) if total_transferred > 0 else 0

        # Detect burst buying (multiple buys in short windows)
        burst_wallets = []
        for addr, timestamps in wallet_buys.items():
            if len(timestamps) >= 3:
                timestamps.sort()
                for i in range(len(timestamps) - 2):
                    span = (timestamps[i + 2] - timestamps[i]).total_seconds()
                    if span < 3600:  # 3 buys within 1 hour
                        burst_wallets.append(addr)
                        break

        return {
            "analyzable": True,
            "total_unique_wallets": len(wallet_buys),
            "top10_concentration_pct": round(top10_pct, 2),
            "top20_concentration_pct": round(top20_pct, 2),
            "recent_buyers_count": len(recent_buyers),
            "burst_buying_wallets": len(burst_wallets),
            "top_wallets": [
                {"address": addr, "amount": amt, "pct": round(amt / total_transferred * 100, 2)}
                for addr, amt in sorted_wallets[:10]
            ],
            "recent_buyers": recent_buyers[:20],
            "window_hours": window_hours,
        }

    async def get_contract_age_days(self, contract_address: str) -> Optional[int]:
        """Get the age of a token contract in days."""
        creation = await self.get_contract_creation(contract_address)
        if creation and creation.get("txHash"):
            try:
                # Get the creation transaction timestamp
                tx_data = await self._request({
                    "module": "proxy",
                    "action": "eth_getTransactionByHash",
                    "txhash": creation["txHash"],
                })
                if tx_data and tx_data.get("result"):
                    block_num = int(tx_data["result"].get("blockNumber", "0"), 16)
                    block_data = await self._request({
                        "module": "proxy",
                        "action": "eth_getBlockByNumber",
                        "tag": hex(block_num),
                        "boolean": "false",
                    })
                    if block_data and block_data.get("result"):
                        timestamp = int(block_data["result"].get("timestamp", "0"), 16)
                        created = datetime.utcfromtimestamp(timestamp)
                        return (datetime.utcnow() - created).days
            except (ValueError, TypeError):
                pass
        return None

    async def close(self) -> None:
        """Close the HTTP client."""
        if self._client and not self._client.is_closed:
            await self._client.aclose()


class SolscanClient:
    """
    Solana blockchain explorer client for SPL token analysis.
    Handles holder distribution, transfer patterns, and wallet analysis.
    """

    def __init__(self):
        self.base_url = "https://pro-api.solscan.io/v2.0"
        self.api_key = SOLSCAN_API_KEY
        self._client: Optional[httpx.AsyncClient] = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=30.0,
                headers={
                    "Accept": "application/json",
                    "Token": self.api_key,
                },
            )
        return self._client

    async def _request(self, endpoint: str, params: dict = None) -> Optional[dict]:
        """Make an authenticated request to Solscan."""
        if not self.api_key:
            log.warning("solscan_no_key", message="No Solscan API key configured")
            return None

        try:
            client = await self._get_client()
            response = await client.get(f"{self.base_url}{endpoint}", params=params)
            response.raise_for_status()
            return response.json()
        except Exception as e:
            log.error("solscan_error", error=str(e), endpoint=endpoint)
            return None

    async def get_token_holders(self, token_address: str, page: int = 1,
                                 page_size: int = 40) -> Optional[dict]:
        """Get top holders for an SPL token."""
        data = await self._request(f"/token/holders", params={
            "address": token_address,
            "page": page,
            "page_size": page_size,
        })
        return data

    async def get_token_meta(self, token_address: str) -> Optional[dict]:
        """Get token metadata (name, symbol, supply, decimals)."""
        data = await self._request(f"/token/meta", params={
            "address": token_address,
        })
        return data

    async def get_token_transfer(self, token_address: str, page: int = 1,
                                  page_size: int = 40) -> Optional[dict]:
        """Get recent token transfers."""
        data = await self._request(f"/token/transfer", params={
            "address": token_address,
            "page": page,
            "page_size": page_size,
        })
        return data

    async def get_account_tokens(self, account_address: str) -> Optional[dict]:
        """Get all tokens held by an account."""
        data = await self._request(f"/account/token-accounts", params={
            "address": account_address,
        })
        return data

    async def close(self) -> None:
        """Close the HTTP client."""
        if self._client and not self._client.is_closed:
            await self._client.aclose()


# ── Factory Functions ─────────────────────────────────────────────────────────

def create_etherscan_client() -> EVMExplorerClient:
    """Create an Etherscan client."""
    return EVMExplorerClient(
        base_url="https://api.etherscan.io/api",
        api_key=ETHERSCAN_API_KEY,
        chain_name="ethereum",
    )


def create_bscscan_client() -> EVMExplorerClient:
    """Create a BscScan client."""
    return EVMExplorerClient(
        base_url="https://api.bscscan.com/api",
        api_key=BSCSCAN_API_KEY,
        chain_name="bsc",
    )


def create_solscan_client() -> SolscanClient:
    """Create a Solscan client."""
    return SolscanClient()

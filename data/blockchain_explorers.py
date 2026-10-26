"""
Blockchain Explorer Clients

Etherscan, BscScan, and Solscan API clients for wallet-level data,
token transfers, contract info, and holder distribution analysis.
Critical for Layer 1 on-chain supply control detection.
"""

import httpx
import asyncio
import sqlite3
import time
from typing import Optional
from datetime import datetime, timedelta
from utils.logger import get_logger
from config.settings import (
    DATA_DIR,
    ETHERSCAN_API_KEY,
    BSCSCAN_API_KEY,
    SOLSCAN_API_KEY,
    SOLSCAN_API_KEY_2,
    HELIUS_API_KEY,
    HELIUS_API_KEY_2,
    HELIUS_API_KEY_3,
    SHYFT_API_KEY,
    QUICKNODE_SOLANA_RPC_URL,
    ALCHEMY_SOLANA_API_KEY,
)

log = get_logger("blockchain_explorers")


class EVMExplorerClient:
    """
    Generic EVM blockchain explorer client (works with Etherscan, BscScan, etc.)
    Handles token holder data, transfers, contract age, and wallet analysis.
    """

    def __init__(self, base_url: str, api_key: str, chain_name: str,
                 chain_id: Optional[int] = None):
        self.base_url = base_url
        self.api_key = api_key
        self.chain_name = chain_name
        self.chain_id = chain_id
        self._client: Optional[httpx.AsyncClient] = None
        self._holder_cache: dict[str, tuple[float, list[dict]]] = {}
        self._holder_index_locks: dict[str, asyncio.Lock] = {}
        self._holder_index_path = DATA_DIR / "evm_holder_index.sqlite3"

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
        if self.chain_id is not None:
            params["chainid"] = self.chain_id
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
                                   offset: int = 100, sort: str = "desc",
                                   end_block: Optional[int] = None) -> list[dict]:
        """Get ERC20 token transfer events for a contract."""
        data = await self._token_transfer_page(
            contract_address, start_block, page, offset, sort, end_block
        )
        return data or []

    async def _token_transfer_page(self, contract_address: str,
                                   start_block: int, page: int, offset: int,
                                   sort: str = "asc",
                                   end_block: Optional[int] = None) -> Optional[list[dict]]:
        params = {
            "module": "account",
            "action": "tokentx",
            "contractaddress": contract_address,
            "startblock": start_block,
            "page": page,
            "offset": offset,
            "sort": sort,
        }
        if end_block is not None:
            params["endblock"] = end_block
        data = await self._request(params)
        if not isinstance(data, dict):
            return None
        result = data.get("result")
        if isinstance(result, list):
            return result
        if data.get("message") == "No transactions found":
            return []
        return None

    async def get_token_holders(self, contract_address: str, page: int = 1,
                                 offset: int = 100) -> list[dict]:
        """
        Get top token holders from the explorer, or reconstruct balances from
        the contract's complete ERC-20 Transfer event history when restricted.
        """
        cache_key = contract_address.lower()
        cached = self._holder_cache.get(cache_key)
        if cached and time.monotonic() - cached[0] < 180:
            return cached[1]

        data = await self._request({
            "module": "token",
            "action": "tokenholderlist",
            "contractaddress": contract_address,
            "page": page,
            "offset": offset,
        })
        if data and isinstance(data.get("result"), list) and data["result"]:
            holders = data["result"]
            self._holder_cache[cache_key] = (time.monotonic(), holders)
            return holders

        holders = await self._indexed_token_holders(contract_address)
        if holders is not None:
            self._holder_cache[cache_key] = (time.monotonic(), holders)
            return holders
        return []

    def _open_holder_index(self) -> sqlite3.Connection:
        """Open the persistent event/balance index used when holder APIs are gated."""
        connection = sqlite3.connect(self._holder_index_path, timeout=30)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("""
            CREATE TABLE IF NOT EXISTS evm_holder_index_state (
                chain_id TEXT NOT NULL,
                contract TEXT NOT NULL,
                last_complete_block INTEGER NOT NULL DEFAULT -1,
                scan_start_block INTEGER NOT NULL DEFAULT 0,
                scan_end_block INTEGER,
                next_page INTEGER NOT NULL DEFAULT 1,
                PRIMARY KEY (chain_id, contract)
            )
        """)
        connection.execute("""
            CREATE TABLE IF NOT EXISTS evm_holder_events (
                chain_id TEXT NOT NULL,
                contract TEXT NOT NULL,
                tx_hash TEXT NOT NULL,
                log_index TEXT NOT NULL,
                PRIMARY KEY (chain_id, contract, tx_hash, log_index)
            )
        """)
        connection.execute("""
            CREATE TABLE IF NOT EXISTS evm_holder_balances (
                chain_id TEXT NOT NULL,
                contract TEXT NOT NULL,
                address TEXT NOT NULL,
                raw_balance TEXT NOT NULL,
                PRIMARY KEY (chain_id, contract, address)
            )
        """)
        connection.commit()
        return connection

    async def _latest_block_number(self) -> Optional[int]:
        data = await self._request({"module": "proxy", "action": "eth_blockNumber"})
        value = data.get("result") if isinstance(data, dict) else None
        try:
            return int(value, 16) if isinstance(value, str) and value.startswith("0x") else int(value)
        except (ValueError, TypeError):
            return None

    async def _indexed_token_holders(self, contract_address: str,
                                     page_limit: int = 100) -> Optional[list[dict]]:
        """Persistently rebuild EVM token balances from every Transfer event.

        The holder-list API is API-PRO on some chains/plans. This indexer
        checkpoints each page, deduplicates events, and refuses to publish a
        concentration until the full historical scan and supply reconciliation
        have completed.
        """
        if self.chain_id is None:
            return None
        key = contract_address.lower()
        lock = self._holder_index_locks.setdefault(key, asyncio.Lock())
        async with lock:
            connection = self._open_holder_index()
            chain_key = str(self.chain_id)
            try:
                connection.execute(
                    "INSERT OR IGNORE INTO evm_holder_index_state "
                    "(chain_id, contract) VALUES (?, ?)",
                    (chain_key, key),
                )
                connection.commit()
                state = connection.execute(
                    "SELECT last_complete_block, scan_start_block, scan_end_block, next_page "
                    "FROM evm_holder_index_state WHERE chain_id=? AND contract=?",
                    (chain_key, key),
                ).fetchone()
                last_complete, scan_start, scan_end, page = state

                if scan_end is None:
                    latest = await self._latest_block_number()
                    if latest is None:
                        log.warning("holder_index_tip_unavailable", chain=self.chain_name,
                                    contract=key)
                        return None
                    # Re-scan a short overlap so a late event or shallow reorg
                    # cannot silently leave the persisted balances stale.
                    scan_start = max(0, last_complete - 12) if last_complete >= 0 else 0
                    scan_end = latest
                    page = 1
                    connection.execute(
                        "UPDATE evm_holder_index_state SET scan_start_block=?, "
                        "scan_end_block=?, next_page=1 WHERE chain_id=? AND contract=?",
                        (scan_start, scan_end, chain_key, key),
                    )
                    connection.commit()

                page_size = 1000
                pages_done = 0
                complete = False
                while pages_done < page_limit:
                    rows = await self._token_transfer_page(
                        contract_address, scan_start, page, page_size,
                        sort="asc", end_block=scan_end,
                    )
                    if rows is None:
                        log.warning("holder_index_page_failed", chain=self.chain_name,
                                    contract=key, page=page)
                        return None

                    parsed_events = []
                    for row in rows:
                        if not isinstance(row, dict):
                            return None
                        tx_hash = row.get("hash") or row.get("transactionHash")
                        log_index = row.get("logIndex")
                        sender = str(row.get("from") or "").lower()
                        recipient = str(row.get("to") or "").lower()
                        try:
                            raw_value = int(str(row.get("value")))
                            log_number = int(str(log_index), 16) if str(log_index).startswith("0x") else int(str(log_index))
                            int(str(row.get("blockNumber")), 16) if str(row.get("blockNumber")).startswith("0x") else int(str(row.get("blockNumber")))
                        except (ValueError, TypeError):
                            return None
                        if not tx_hash or log_index is None or not sender or not recipient or raw_value < 0:
                            return None
                        parsed_events.append((str(tx_hash).lower(), str(log_number), sender,
                                              recipient, raw_value))

                    try:
                        connection.execute("BEGIN")
                        deltas: dict[str, int] = {}
                        for tx_hash, log_number, sender, recipient, raw_value in parsed_events:
                            inserted = connection.execute(
                                "INSERT OR IGNORE INTO evm_holder_events "
                                "(chain_id, contract, tx_hash, log_index) VALUES (?, ?, ?, ?)",
                                (chain_key, key, tx_hash, log_number),
                            ).rowcount
                            if not inserted:
                                continue
                            if sender != "0x0000000000000000000000000000000000000000":
                                deltas[sender] = deltas.get(sender, 0) - raw_value
                            if recipient != "0x0000000000000000000000000000000000000000":
                                deltas[recipient] = deltas.get(recipient, 0) + raw_value

                        for address, delta in deltas.items():
                            existing = connection.execute(
                                "SELECT raw_balance FROM evm_holder_balances "
                                "WHERE chain_id=? AND contract=? AND address=?",
                                (chain_key, key, address),
                            ).fetchone()
                            balance = (int(existing[0]) if existing else 0) + delta
                            if balance < 0:
                                raise ValueError("transfer history produced a negative balance")
                            connection.execute(
                                "INSERT INTO evm_holder_balances "
                                "(chain_id, contract, address, raw_balance) VALUES (?, ?, ?, ?) "
                                "ON CONFLICT(chain_id, contract, address) DO UPDATE SET "
                                "raw_balance=excluded.raw_balance",
                                (chain_key, key, address, str(balance)),
                            )

                        if len(rows) < page_size:
                            connection.execute(
                                "UPDATE evm_holder_index_state SET last_complete_block=?, "
                                "scan_start_block=?, scan_end_block=NULL, next_page=1 "
                                "WHERE chain_id=? AND contract=?",
                                (scan_end, max(0, scan_end - 12), chain_key, key),
                            )
                            complete = True
                        else:
                            connection.execute(
                                "UPDATE evm_holder_index_state SET next_page=? "
                                "WHERE chain_id=? AND contract=?",
                                (page + 1, chain_key, key),
                            )
                        connection.commit()
                    except Exception:
                        connection.rollback()
                        raise

                    pages_done += 1
                    if complete:
                        break
                    page += 1

                if not complete:
                    log.info("holder_index_backfill_pending", chain=self.chain_name,
                             contract=key, pages_indexed=pages_done)
                    return None

                supply = await self.get_token_supply(contract_address)
                if supply is None or supply <= 0:
                    return None
                balances = connection.execute(
                    "SELECT address, raw_balance FROM evm_holder_balances "
                    "WHERE chain_id=? AND contract=?",
                    (chain_key, key),
                ).fetchall()
                positive = [(address, int(balance)) for address, balance in balances
                            if int(balance) > 0]
                if sum(balance for _, balance in positive) != supply:
                    log.warning("holder_index_supply_mismatch", chain=self.chain_name,
                                contract=key, indexed_supply=sum(v for _, v in positive),
                                api_supply=supply)
                    return None
                positive.sort(key=lambda item: item[1], reverse=True)
                holders = [{"address": address, "TokenHolderQuantity": str(balance)}
                           for address, balance in positive[:100]]
                log.info("holder_index_complete", chain=self.chain_name,
                         contract=key, holder_count=len(positive), pages_indexed=pages_done)
                return holders
            finally:
                connection.close()

    async def get_token_supply(self, contract_address: str) -> Optional[int]:
        """Get raw ERC-20 total supply for holder-concentration calculations."""
        data = await self._request({
            "module": "stats",
            "action": "tokensupply",
            "contractaddress": contract_address,
        })
        try:
            value = data.get("result") if data else None
            return int(value) if value is not None else None
        except (ValueError, TypeError):
            return None

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
        wallet_transfers = {}  # recipient address -> transfer timestamps
        wallet_amounts = {}    # recipient address -> total transferred tokens
        recent_recipients = []

        for tx in transfers:
            try:
                timestamp = datetime.utcfromtimestamp(int(tx.get("timeStamp", 0)))
                to_addr = tx.get("to", "").lower()
                value = int(tx.get("value", 0))

                if to_addr and value > 0:
                    if to_addr not in wallet_transfers:
                        wallet_transfers[to_addr] = []
                        wallet_amounts[to_addr] = 0

                    wallet_transfers[to_addr].append(timestamp)
                    wallet_amounts[to_addr] += value

                    if timestamp >= cutoff:
                        recent_recipients.append({
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
        for addr, timestamps in wallet_transfers.items():
            if len(timestamps) >= 3:
                timestamps.sort()
                for i in range(len(timestamps) - 2):
                    span = (timestamps[i + 2] - timestamps[i]).total_seconds()
                    if span < 3600:  # 3 buys within 1 hour
                        burst_wallets.append(addr)
                        break

        return {
            "analyzable": True,
            "total_unique_recipients": len(wallet_transfers),
            "top10_transfer_concentration_pct": round(top10_pct, 2),
            "top20_transfer_concentration_pct": round(top20_pct, 2),
            "recent_recipients_count": len(recent_recipients),
            "burst_receiving_wallets": len(burst_wallets),
            "top_wallets": [
                {"address": addr, "amount": amt, "pct": round(amt / total_transferred * 100, 2)}
                for addr, amt in sorted_wallets[:10]
            ],
            "recent_recipients": recent_recipients[:20],
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
        self.api_keys = tuple(dict.fromkeys(
            key for key in (SOLSCAN_API_KEY, SOLSCAN_API_KEY_2) if key
        ))
        self.helius_api_key = HELIUS_API_KEY
        self.helius_api_keys = tuple(dict.fromkeys(
            key for key in (HELIUS_API_KEY, HELIUS_API_KEY_2, HELIUS_API_KEY_3) if key
        ))
        self.shyft_api_key = SHYFT_API_KEY
        self.quicknode_rpc_url = QUICKNODE_SOLANA_RPC_URL
        self.alchemy_api_key = ALCHEMY_SOLANA_API_KEY
        self._client: Optional[httpx.AsyncClient] = None
        self._holder_cache: dict[tuple[str, int, int], tuple[float, dict]] = {}
        self._meta_cache: dict[str, tuple[float, dict]] = {}
        self._cache_ttl_seconds = 180

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=30.0,
                headers={"Accept": "application/json"},
            )
        return self._client

    async def _request(self, endpoint: str, params: dict = None) -> Optional[dict]:
        """Call Solscan, rotating to the second configured key on provider errors."""
        if not self.api_keys:
            log.warning("solscan_no_key", message="No Solscan API key configured")
            return None

        client = await self._get_client()
        for key_number, api_key in enumerate(self.api_keys, start=1):
            try:
                response = await client.get(
                    f"{self.base_url}{endpoint}",
                    params=params,
                    headers={"token": api_key},
                )
                response.raise_for_status()
                return response.json()
            except httpx.HTTPStatusError as e:
                # Never log request URLs or headers because they may contain credentials.
                status = e.response.status_code
                log.warning("solscan_http_error", status=status,
                            endpoint=endpoint, key_number=key_number,
                            response=e.response.text[:300])
                retryable = status in (401, 402, 403, 408, 429) or status >= 500
                if not retryable or key_number == len(self.api_keys):
                    return None
            except Exception as e:
                log.warning("solscan_error", error=type(e).__name__,
                            endpoint=endpoint, key_number=key_number)
                if key_number == len(self.api_keys):
                    return None
        return None

    async def _rpc_request(self, provider: str, api_key: str, method: str,
                           params: list) -> Optional[dict]:
        """Call a Solana JSON-RPC provider without logging its credential URL."""
        if not api_key and provider != "solana_rpc":
            return None

        if provider == "helius":
            url = "https://mainnet.helius-rpc.com/"
            query = {"api-key": api_key}
        elif provider == "solana_rpc":
            url = "https://api.mainnet.solana.com"
            query = {}
        elif provider == "quicknode":
            url = api_key
            query = {}
        elif provider == "alchemy":
            url = f"https://solana-mainnet.g.alchemy.com/v2/{api_key}"
            query = {}
        else:
            url = "https://rpc.shyft.to/"
            query = {"api_key": api_key}

        try:
            client = await self._get_client()
            response = await client.post(
                url,
                params=query,
                json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                timeout=45.0,
            )
            response.raise_for_status()
            payload = response.json()
            if payload.get("error"):
                log.warning("solana_fallback_rpc_error", provider=provider,
                            method=method, detail=str(payload["error"])[:300])
                return None
            return payload.get("result")
        except httpx.HTTPStatusError as e:
            log.warning("solana_fallback_http_error", provider=provider,
                        method=method, status=e.response.status_code,
                        response=e.response.text[:300])
            return None
        except Exception as e:
            log.warning("solana_fallback_error", provider=provider,
                        method=method, error=type(e).__name__)
            return None

    @staticmethod
    def _extract_holder_rows(payload: Optional[dict]) -> list[dict]:
        if not isinstance(payload, dict):
            return []
        data = payload.get("data", payload)
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            for key in ("items", "list", "token_accounts"):
                rows = data.get(key)
                if isinstance(rows, list):
                    return rows
        return []

    @staticmethod
    def _account_amount(row: dict) -> Optional[int]:
        amount = row.get("amount")
        if amount is None:
            amount = (row.get("tokenAmount") or {}).get("amount")
        if amount is None:
            parsed = ((row.get("account") or {}).get("data") or {}).get("parsed") or {}
            info = parsed.get("info") or {}
            amount = (info.get("tokenAmount") or {}).get("amount")
        try:
            return int(str(amount)) if amount is not None else None
        except (ValueError, TypeError):
            return None

    @classmethod
    def _aggregate_accounts(cls, accounts: list[dict]) -> Optional[dict]:
        """Aggregate token accounts by owner, so top-holder ratios count wallets."""
        totals: dict[str, int] = {}
        for row in accounts:
            if not isinstance(row, dict):
                return None
            parsed = ((row.get("account") or {}).get("data") or {}).get("parsed") or {}
            info = parsed.get("info") or {}
            # Helius puts the wallet owner at row["owner"]. Standard
            # getProgramAccounts responses (such as Shyft's) put the program
            # id there and the wallet owner under parsed.info.owner.
            owner = info.get("owner") or row.get("token_owner") or row.get("owner")
            amount = cls._account_amount(row)
            if not owner or amount is None or amount < 0:
                # Returning partial totals would understate concentration.
                return None
            totals[owner] = totals.get(owner, 0) + amount

        holders = [{"address": owner, "amount": amount}
                   for owner, amount in totals.items() if amount > 0]
        holders.sort(key=lambda row: row["amount"], reverse=True)
        return {"data": {"items": holders}}

    @staticmethod
    def _aggregate_solscan_holders(rows: list[dict]) -> Optional[dict]:
        """Solscan returns token-account addresses; combine them by wallet owner."""
        totals: dict[str, int] = {}
        for row in rows:
            if not isinstance(row, dict):
                return None
            owner = row.get("owner")
            amount = row.get("amount_str", row.get("amount"))
            try:
                amount = int(str(amount)) if amount is not None else None
            except (ValueError, TypeError):
                return None
            if not owner or amount is None or amount < 0:
                return None
            totals[owner] = totals.get(owner, 0) + amount

        holders = [{"address": owner, "amount": amount}
                   for owner, amount in totals.items()]
        holders.sort(key=lambda row: row["amount"], reverse=True)
        return {"data": {"items": holders}}

    @staticmethod
    def _add_wallet_concentration(aggregated: Optional[dict],
                                  raw_supply: Optional[str]) -> Optional[dict]:
        """Add owner-level shares only after reconciling all balances to supply."""
        try:
            supply = int(str(raw_supply))
            rows = aggregated["data"]["items"]
            if supply <= 0 or not isinstance(rows, list):
                return None
            amounts = sorted((int(row["amount"]) for row in rows), reverse=True)
        except (KeyError, TypeError, ValueError):
            return None
        if not amounts or any(amount < 0 for amount in amounts):
            return None
        account_total = sum(amounts)
        if account_total != supply:
            log.warning("solana_holder_supply_mismatch",
                        owner_balance_total=account_total, mint_supply=supply,
                        difference=supply - account_total)
            # A partial provider result must never be advertised as the full
            # wallet distribution. Let the caller try another source.
            return None
        aggregated["data"]["concentration"] = {
            "top10_wallet_pct": sum(amounts[:10]) / supply * 100,
            "top20_wallet_pct": sum(amounts[:20]) / supply * 100,
        }
        top_holders = aggregated["data"]["items"][:20]
        aggregated["data"]["top_holders"] = [
            {
                "address": row["address"],
                "percentage": int(row["amount"]) / supply * 100,
            }
            for row in top_holders
        ]
        aggregated["data"]["holder_count"] = len(amounts)
        aggregated["data"]["supply_reconciled"] = True
        return aggregated

    async def _helius_token_holders(self, token_address: str) -> Optional[dict]:
        """Try configured Helius keys in order, accepting only a full snapshot."""
        for key_number, api_key in enumerate(self.helius_api_keys, start=1):
            result = await self._helius_token_holders_with_key(token_address, api_key)
            if result is not None:
                result["provider"] = "helius"
                log.info("solana_holders_provider_key_used", provider="helius",
                         key_number=key_number, mint=token_address)
                return result
        return None

    async def _helius_token_holders_with_key(self, token_address: str,
                                             api_key: str) -> Optional[dict]:
        """Fetch every Helius page before reporting a complete distribution."""

        limit = 1000
        page = 1
        accounts: list[dict] = []
        seen_addresses: set[str] = set()

        while True:
            result = await self._rpc_request(
                "helius", api_key, "getTokenAccounts",
                [{"page": page, "limit": limit, "mint": token_address}],
            )
            if not isinstance(result, dict):
                return None
            rows = result.get("token_accounts", result.get("items"))
            if not isinstance(rows, list):
                return None
            previous_count = len(accounts)
            for row in rows:
                if not isinstance(row, dict):
                    return None
                account_id = row.get("address") or row.get("pubkey")
                if not account_id:
                    return None
                if account_id in seen_addresses:
                    continue
                seen_addresses.add(account_id)
                accounts.append(row)

            # Helius paginates until it returns a short/empty page. Its
            # `total` is not an overall holder count; stopping at 1,000 would
            # silently truncate tokens with more than one page.
            if not rows or len(rows) < limit:
                break
            if len(accounts) == previous_count:
                return None
            page += 1

        log.info("solana_holders_provider", provider="helius",
                 mint=token_address, token_accounts=len(accounts))
        aggregated = self._aggregate_accounts(accounts)
        supply = await self._rpc_token_supply("helius", api_key, token_address)
        supply_data = supply.get("data", {}) if isinstance(supply, dict) else {}
        aggregated = self._add_wallet_concentration(
            aggregated, supply_data.get("supply") if isinstance(supply_data, dict) else None
        )
        if aggregated is None:
            return None
        return aggregated

    async def _shyft_token_holders(self, token_address: str) -> Optional[dict]:
        """Read parsed SPL and Token-2022 accounts, then aggregate by wallet."""
        if not self.shyft_api_key:
            return None

        programs = (
            "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
            "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb",
        )
        results = await asyncio.gather(*(
            self._rpc_request(
                "shyft", self.shyft_api_key, "getProgramAccounts",
                [program, {
                    "encoding": "jsonParsed",
                    "filters": [{"memcmp": {"offset": 0, "bytes": token_address}}],
                }],
            ) for program in programs
        ))
        if any(not isinstance(result, list) for result in results):
            return None

        accounts = [row for result in results for row in result]
        log.info("solana_holders_provider", provider="shyft",
                 mint=token_address, token_accounts=len(accounts))
        aggregated = self._aggregate_accounts(accounts)
        supply = await self._rpc_token_supply("shyft", self.shyft_api_key, token_address)
        supply_data = supply.get("data", {}) if isinstance(supply, dict) else {}
        aggregated = self._add_wallet_concentration(
            aggregated, supply_data.get("supply") if isinstance(supply_data, dict) else None
        )
        if aggregated is None:
            return None
        aggregated["provider"] = "shyft"
        return aggregated

    async def _rpc_program_holders(self, provider: str, api_key: str,
                                   token_address: str) -> Optional[dict]:
        """Enumerate all matching SPL and Token-2022 accounts through an RPC."""
        if not api_key:
            return None

        programs = (
            "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
            "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb",
        )
        accounts: list[dict] = []
        seen_addresses: set[str] = set()
        method = "getProgramAccountsV2" if provider == "alchemy" else "getProgramAccounts"

        for program in programs:
            config = {
                "encoding": "jsonParsed",
                "commitment": "finalized",
                "filters": [{"memcmp": {"offset": 0, "bytes": token_address}}],
            }
            previous_cursor = None
            for _ in range(10000):
                result = await self._rpc_request(
                    provider, api_key, method, [program, config.copy()]
                )
                if provider == "alchemy":
                    if not isinstance(result, dict):
                        return None
                    rows = result.get("accounts")
                    cursor = result.get("paginationKey")
                    if not isinstance(rows, list):
                        return None
                else:
                    rows = result
                    cursor = None
                    if not isinstance(rows, list):
                        # QuickNode may reject oversized non-paginated scans;
                        # the next provider is then tried by the caller.
                        return None

                for row in rows:
                    if not isinstance(row, dict):
                        return None
                    address = row.get("pubkey") or row.get("address")
                    if not address:
                        return None
                    if address not in seen_addresses:
                        seen_addresses.add(address)
                        accounts.append(row)

                if provider != "alchemy" or cursor is None:
                    break
                if not isinstance(cursor, str) or cursor == previous_cursor:
                    return None
                previous_cursor = cursor
                config["paginationKey"] = cursor
            else:
                log.warning("solana_holder_rpc_page_limit", provider=provider,
                            mint=token_address)
                return None

        log.info("solana_holders_provider", provider=provider,
                 mint=token_address, token_accounts=len(accounts))
        aggregated = self._aggregate_accounts(accounts)
        supply = await self._rpc_token_supply(provider, api_key, token_address)
        supply_data = supply.get("data", {}) if isinstance(supply, dict) else {}
        aggregated = self._add_wallet_concentration(
            aggregated, supply_data.get("supply") if isinstance(supply_data, dict) else None
        )
        if aggregated is None:
            return None
        aggregated["provider"] = provider
        return aggregated

    async def get_token_holders(self, token_address: str, page: int = 1,
                                 page_size: int = 40) -> Optional[dict]:
        """Get a complete holder set; never calculate concentration from one page."""
        cache_key = (token_address, page, page_size)
        cached = self._holder_cache.get(cache_key)
        if cached and time.monotonic() - cached[0] < self._cache_ttl_seconds:
            return cached[1]

        # Prefer complete account listings. Solscan's aggregate statistic is a
        # one-call fallback when a full owner-level account scan is unavailable.
        for provider in (self._helius_token_holders, self._shyft_token_holders):
            fallback = await provider(token_address)
            if fallback is not None:
                self._holder_cache[cache_key] = (time.monotonic(), fallback)
                return fallback

        for provider, api_key in (("quicknode", self.quicknode_rpc_url),
                                  ("alchemy", self.alchemy_api_key)):
            fallback = await self._rpc_program_holders(provider, api_key, token_address)
            if fallback is not None:
                self._holder_cache[cache_key] = (time.monotonic(), fallback)
                return fallback

        fallback = await self._solscan_holder_concentration(token_address)
        if fallback is not None:
            self._holder_cache[cache_key] = (time.monotonic(), fallback)
            return fallback

        fallback = await self._solscan_top20_accounts(token_address)
        if fallback is not None:
            self._holder_cache[cache_key] = (time.monotonic(), fallback)
            return fallback

        fallback = await self._solana_rpc_holder_concentration(token_address)
        if fallback is not None:
            self._holder_cache[cache_key] = (time.monotonic(), fallback)
            return fallback

        log.error("solana_complete_holder_data_unavailable", mint=token_address,
                  providers=("helius", "shyft", "quicknode", "alchemy", "solscan", "solana_rpc"))
        return None

    async def _solscan_holder_concentration(self, token_address: str) -> Optional[dict]:
        """Use Solscan's provider-calculated top-20 supply concentration metric."""
        if not self.api_keys:
            return None
        payload = await self._request("/token/statistic", params={
            "address": token_address,
        })
        body = payload.get("data") if isinstance(payload, dict) else None
        top20 = body.get("holder_top_20_balance_pct") if isinstance(body, dict) else None
        try:
            top20 = float(top20)
        except (ValueError, TypeError):
            return None
        if not 0 <= top20 <= 100:
            return None
        log.info("solana_holder_concentration_provider", provider="solscan_statistic",
                 mint=token_address, top20_holder_pct=top20)
        return {
            "provider": "solscan_statistic",
            "data": {"items": [], "concentration": {
                "top20_wallet_pct": top20,
            }},
        }

    async def _solscan_top20_accounts(self, token_address: str) -> Optional[dict]:
        """Fallback to Solscan's ranked top-20 token-account share."""
        if not self.api_keys:
            return None
        payload = await self._request("/token/holders", params={
            "address": token_address,
            "page": 1,
            "page_size": 20,
        })
        body = payload.get("data") if isinstance(payload, dict) else None
        rows = body.get("items") if isinstance(body, dict) else None
        if not isinstance(rows, list) or not rows:
            return None
        try:
            total = int(body.get("total", len(rows)))
            if total > len(rows):
                return None
            percentages = [float(row["percentage"]) for row in rows
                           if isinstance(row, dict) and row.get("percentage") is not None]
        except (TypeError, ValueError, KeyError):
            return None
        if len(percentages) != len(rows):
            return None
        top20_account_pct = sum(percentages)
        if not 0 <= top20_account_pct <= 100:
            return None
        log.info("solana_holder_concentration_provider", provider="solscan_top20_accounts",
                 mint=token_address, top20_account_pct=top20_account_pct)
        return {
            "provider": "solscan_top20_accounts",
            "data": {"items": [], "concentration": {
                "top20_account_pct": top20_account_pct,
            }},
        }

    async def _solana_rpc_holder_concentration(self, token_address: str) -> Optional[dict]:
        """Last-resort, no-key estimate from Solana's 20 largest token accounts."""
        largest_result = await self._rpc_request(
            "solana_rpc", "", "getTokenLargestAccounts",
            [token_address, {"commitment": "finalized"}],
        )
        context = largest_result.get("context", {}) if isinstance(largest_result, dict) else {}
        supply_config = {"commitment": "finalized"}
        if isinstance(context, dict) and context.get("slot") is not None:
            supply_config["minContextSlot"] = context["slot"]
        supply_result = await self._rpc_request(
            "solana_rpc", "", "getTokenSupply", [token_address, supply_config],
        )
        supply_value = supply_result.get("value") if isinstance(supply_result, dict) else None
        largest = largest_result.get("value") if isinstance(largest_result, dict) else None
        if not isinstance(supply_value, dict) or not isinstance(largest, list):
            return None
        try:
            supply = int(str(supply_value.get("amount")))
            amounts = [int(str(row.get("amount"))) for row in largest
                       if isinstance(row, dict) and row.get("amount") is not None]
        except (TypeError, ValueError):
            return None
        if supply <= 0 or not amounts or any(amount < 0 for amount in amounts):
            return None
        top20_account_pct = sum(sorted(amounts, reverse=True)[:20]) / supply * 100
        if not 0 <= top20_account_pct <= 100:
            return None
        log.info("solana_holder_concentration_provider", provider="solana_rpc_top20_accounts",
                 mint=token_address, top20_account_pct=top20_account_pct)
        return {
            "provider": "solana_rpc_top20_accounts",
            "data": {"items": [], "concentration": {
                "top20_account_pct": top20_account_pct,
            }},
        }

    async def _rpc_token_supply(self, provider: str, api_key: str,
                                token_address: str) -> Optional[dict]:
        result = await self._rpc_request(
            provider, api_key, "getTokenSupply", [token_address]
        )
        value = result.get("value") if isinstance(result, dict) else None
        if not isinstance(value, dict) or value.get("amount") is None:
            return None
        return {"data": {
            "supply": value["amount"],
            "decimals": value.get("decimals"),
        }}

    async def get_token_meta(self, token_address: str) -> Optional[dict]:
        """Get token supply from Solscan, Helius, or Shyft in that order."""
        cached = self._meta_cache.get(token_address)
        if cached and time.monotonic() - cached[0] < self._cache_ttl_seconds:
            return cached[1]

        data = await self._request(f"/token/meta", params={
            "address": token_address,
        })
        meta = data.get("data", data) if isinstance(data, dict) else {}
        if isinstance(meta, dict) and any(
            meta.get(key) is not None for key in ("supply", "totalSupply", "total_supply")
        ):
            self._meta_cache[token_address] = (time.monotonic(), data)
            return data

        if not self.api_keys:
            log.warning("solscan_no_key", message="No Solscan API key configured")
        supply_providers = [("helius", key) for key in self.helius_api_keys]
        supply_providers.extend((
            ("shyft", self.shyft_api_key),
            ("quicknode", self.quicknode_rpc_url),
            ("alchemy", self.alchemy_api_key),
        ))
        for provider, api_key in supply_providers:
            fallback = await self._rpc_token_supply(provider, api_key, token_address)
            if fallback is not None:
                self._meta_cache[token_address] = (time.monotonic(), fallback)
                return fallback
        return None

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
    """Create an Etherscan V2 client for Ethereum mainnet."""
    return EVMExplorerClient(
        base_url="https://api.etherscan.io/v2/api",
        api_key=ETHERSCAN_API_KEY,
        chain_name="ethereum",
        chain_id=1,
    )


def create_bscscan_client() -> EVMExplorerClient:
    """Create a BNB Chain client, preferring the unified Etherscan V2 key.

    A BscScan-only key remains a fallback for its community endpoints, but
    the unified Etherscan key is needed for V2 coverage and holder-list data.
    """
    if ETHERSCAN_API_KEY:
        return EVMExplorerClient(
            base_url="https://api.etherscan.io/v2/api",
            api_key=ETHERSCAN_API_KEY,
            chain_name="bsc",
            chain_id=56,
        )

    return EVMExplorerClient(
        base_url="https://api.bscscan.com/api",
        api_key=BSCSCAN_API_KEY,
        chain_name="bsc",
    )


def create_solscan_client() -> SolscanClient:
    """Create a Solscan client."""
    return SolscanClient()

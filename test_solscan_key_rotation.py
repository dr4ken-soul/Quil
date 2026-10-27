import unittest
from unittest.mock import patch

import httpx

import data.blockchain_explorers as explorers


class FakeSolscanHttpClient:
    def __init__(self):
        self.keys_used = []
        self.responses = [
            httpx.Response(
                429,
                json={"message": "rate limited"},
                request=httpx.Request("GET", "https://mock.solscan/token/meta"),
            ),
            httpx.Response(
                401,
                json={"message": "unauthorized"},
                request=httpx.Request("GET", "https://mock.solscan/token/meta"),
            ),
            httpx.Response(
                200,
                json={"data": {"ok": True}},
                request=httpx.Request("GET", "https://mock.solscan/token/meta"),
            ),
        ]

    async def get(self, url, params=None, headers=None):
        self.keys_used.append(headers["token"])
        return self.responses.pop(0)


class SolscanKeyRotationTests(unittest.IsolatedAsyncioTestCase):
    async def test_rotates_through_three_keys_after_retryable_errors(self):
        keys = ("solscan-test-1", "solscan-test-2", "solscan-test-3")
        with (
            patch.object(explorers, "SOLSCAN_API_KEY", keys[0]),
            patch.object(explorers, "SOLSCAN_API_KEY_2", keys[1]),
            patch.object(explorers, "SOLSCAN_API_KEY_3", keys[2]),
        ):
            client = explorers.SolscanClient()
            fake_http = FakeSolscanHttpClient()

            async def get_fake_client():
                return fake_http

            client._get_client = get_fake_client
            result = await client._request("/token/meta")

        self.assertEqual(result, {"data": {"ok": True}})
        self.assertEqual(fake_http.keys_used, list(keys))


if __name__ == "__main__":
    unittest.main()

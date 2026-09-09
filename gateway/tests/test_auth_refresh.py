from __future__ import annotations

import threading
import unittest
from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient

from cheby_gateway.api import create_app
from cheby_gateway.service import GatewayError

from .helpers import install_proof_auth, make_context, pair


class AuthRefreshTests(unittest.IsolatedAsyncioTestCase):
    async def test_concurrent_refresh_rotation_has_one_winner(self) -> None:
        context = make_context()
        await context.service.start()
        try:
            pairing, _ = pair(context)
            barrier = threading.Barrier(2)

            def rotate():
                barrier.wait(timeout=2)
                try:
                    return "ok", context.service.refresh_access(
                        pairing.device_id,
                        pairing.refresh_token,
                    )
                except GatewayError as exc:
                    return exc.code, None

            with ThreadPoolExecutor(max_workers=2) as executor:
                outcomes = list(executor.map(lambda _: rotate(), range(2)))

            self.assertEqual(1, sum(1 for outcome, _ in outcomes if outcome == "ok"))
            self.assertEqual(
                1,
                sum(1 for outcome, _ in outcomes if outcome == "REFRESH_DENIED"),
            )
        finally:
            await context.service.close()
            context.close()

    async def test_expired_refresh_is_rejected_without_echo(self) -> None:
        context = make_context(refresh_token_ttl_seconds=-1)
        await context.service.start()
        try:
            pairing, _ = pair(context)

            with self.assertRaises(GatewayError) as expired:
                context.service.refresh_access(pairing.device_id, pairing.refresh_token)

            self.assertEqual("REFRESH_DENIED", expired.exception.code)
            self.assertNotIn(pairing.refresh_token, expired.exception.message)
        finally:
            await context.service.close()
            context.close()

    async def test_expired_access_returns_token_expired_contract(self) -> None:
        context = make_context(token_ttl_seconds=-1)
        app = create_app(
            settings=context.settings,
            store=context.store,
            bridge=context.bridge,
        )
        with TestClient(app) as client:
            install_proof_auth(client, context)
            pairing = client.post(
                "/v1/pairings/exchange",
                json={
                    "pairingSecret": "test-pairing-secret",
                    "deviceName": "JUY-AL00",
                    "devicePublicKey": context.device_public_key,
                },
            )
            self.assertEqual(200, pairing.status_code)
            token = pairing.json()["accessToken"]
            response = client.get(
                "/v1/threads",
                headers={"Authorization": "Bearer %s" % token},
            )

            self.assertEqual(401, response.status_code)
            self.assertEqual(
                {
                    "error": {
                        "code": "TOKEN_EXPIRED",
                        "message": "Access token has expired",
                        "retryable": False,
                    }
                },
                response.json(),
            )
            self.assertNotIn(token, response.text)
        context.close()


if __name__ == "__main__":
    unittest.main()

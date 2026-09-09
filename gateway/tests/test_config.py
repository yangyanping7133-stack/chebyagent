from __future__ import annotations

import unittest

from cheby_gateway.config import GatewaySettings


class GatewayConfigTests(unittest.TestCase):
    def test_production_environment_requires_explicit_bridge_mode(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "explicitly configured"):
            GatewaySettings.from_env(
                {
                    "CHEBY_PAIRING_SECRET": "configured-pairing-secret",
                }
            )

        settings = GatewaySettings.from_env(
            {
                "CHEBY_PAIRING_SECRET": "configured-pairing-secret",
                "CHEBY_BRIDGE_MODE": "fake",
            }
        )
        self.assertEqual("fake", settings.bridge_mode)

    def test_rate_limits_cannot_be_disabled_or_made_unbounded(self) -> None:
        with self.assertRaisesRegex(ValueError, "pairing_source_limit"):
            GatewaySettings.from_env(
                {
                    "CHEBY_PAIRING_SECRET": "configured-pairing-secret",
                    "CHEBY_BRIDGE_MODE": "fake",
                    "CHEBY_PAIRING_SOURCE_LIMIT": "0",
                }
            )
        with self.assertRaisesRegex(ValueError, "send_device_limit"):
            GatewaySettings.from_env(
                {
                    "CHEBY_PAIRING_SECRET": "configured-pairing-secret",
                    "CHEBY_BRIDGE_MODE": "fake",
                    "CHEBY_SEND_DEVICE_LIMIT": "1000001",
                }
            )

    def test_trusted_proxy_cidrs_are_explicit_and_validated(self) -> None:
        settings = GatewaySettings.from_env(
            {
                "CHEBY_PAIRING_SECRET": "configured-pairing-secret",
                "CHEBY_BRIDGE_MODE": "fake",
                "CHEBY_TRUSTED_PROXY_CIDRS": "127.0.0.1/32,10.4.0.0/16",
            }
        )
        self.assertEqual(
            ("127.0.0.1/32", "10.4.0.0/16"), settings.trusted_proxy_cidrs
        )
        with self.assertRaisesRegex(ValueError, "invalid CIDR"):
            GatewaySettings.from_env(
                {
                    "CHEBY_PAIRING_SECRET": "configured-pairing-secret",
                    "CHEBY_BRIDGE_MODE": "fake",
                    "CHEBY_TRUSTED_PROXY_CIDRS": "open-internet",
                }
            )
        with self.assertRaisesRegex(ValueError, "open Internet"):
            GatewaySettings.from_env(
                {
                    "CHEBY_PAIRING_SECRET": "configured-pairing-secret",
                    "CHEBY_BRIDGE_MODE": "fake",
                    "CHEBY_TRUSTED_PROXY_CIDRS": "0.0.0.0/0",
                }
            )

    def test_delete_impact_ttl_is_short_and_bounded(self) -> None:
        settings = GatewaySettings.from_env(
            {
                "CHEBY_PAIRING_SECRET": "configured-pairing-secret",
                "CHEBY_BRIDGE_MODE": "fake",
                "CHEBY_DELETE_IMPACT_TTL_SECONDS": "300",
            }
        )
        self.assertEqual(300, settings.delete_impact_ttl_seconds)
        with self.assertRaisesRegex(ValueError, "must not exceed 300"):
            GatewaySettings.from_env(
                {
                    "CHEBY_PAIRING_SECRET": "configured-pairing-secret",
                    "CHEBY_BRIDGE_MODE": "fake",
                    "CHEBY_DELETE_IMPACT_TTL_SECONDS": "301",
                }
            )

    def test_provisional_thread_ttl_is_explicit_and_bounded(self) -> None:
        settings = GatewaySettings.from_env(
            {
                "CHEBY_PAIRING_SECRET": "configured-pairing-secret",
                "CHEBY_BRIDGE_MODE": "fake",
                "CHEBY_PROVISIONAL_THREAD_TTL_SECONDS": "86400",
            }
        )
        self.assertEqual(86400, settings.provisional_thread_ttl_seconds)
        with self.assertRaisesRegex(ValueError, "must not exceed 86400"):
            GatewaySettings.from_env(
                {
                    "CHEBY_PAIRING_SECRET": "configured-pairing-secret",
                    "CHEBY_BRIDGE_MODE": "fake",
                    "CHEBY_PROVISIONAL_THREAD_TTL_SECONDS": "86401",
                }
            )

    def test_public_security_budgets_have_hard_upper_bounds(self) -> None:
        settings = GatewaySettings.from_env(
            {
                "CHEBY_PAIRING_SECRET": "configured-pairing-secret",
                "CHEBY_BRIDGE_MODE": "fake",
                "CHEBY_PROOF_CLOCK_SKEW_SECONDS": "900",
                "CHEBY_WEBSOCKET_EVENT_MAX_BYTES": str(512 * 1024),
                "CHEBY_WEBSOCKET_INBOUND_MAX_BYTES": str(16 * 1024),
                "CHEBY_THREAD_DETAIL_MAX_BYTES": str(4 * 1024 * 1024),
                "CHEBY_THREAD_DETAIL_MAX_MESSAGES": "100",
                "CHEBY_REFRESH_SOURCE_LIMIT": "7",
                "CHEBY_REFRESH_GLOBAL_LIMIT": "11",
                "CHEBY_REFRESH_DEVICE_LIMIT": "3",
            }
        )
        self.assertEqual(900, settings.proof_clock_skew_seconds)
        self.assertEqual((7, 11, 3), (
            settings.refresh_source_limit,
            settings.refresh_global_limit,
            settings.refresh_device_limit,
        ))
        for name, value, message in (
            ("CHEBY_PROOF_CLOCK_SKEW_SECONDS", "901", "must not exceed 900"),
            (
                "CHEBY_WEBSOCKET_INBOUND_MAX_BYTES",
                str(16 * 1024 + 1),
                "must not exceed 16384",
            ),
            (
                "CHEBY_WEBSOCKET_EVENT_MAX_BYTES",
                str(512 * 1024 + 1),
                "must not exceed 524288",
            ),
            (
                "CHEBY_THREAD_DETAIL_MAX_BYTES",
                str(4 * 1024 * 1024 + 1),
                "must not exceed 4194304",
            ),
            (
                "CHEBY_THREAD_DETAIL_MAX_MESSAGES",
                "101",
                "must not exceed 100",
            ),
            (
                "CHEBY_REQUEST_BODY_LIMIT_BYTES",
                str(64 * 1024 + 1),
                "must not exceed 65536",
            ),
            (
                "CHEBY_PAIRING_BODY_LIMIT_BYTES",
                str(16 * 1024 + 1),
                "must not exceed 16384",
            ),
            (
                "CHEBY_REFRESH_BODY_LIMIT_BYTES",
                str(4 * 1024 + 1),
                "must not exceed 4096",
            ),
            (
                "CHEBY_TURN_BODY_LIMIT_BYTES",
                str(256 * 1024 + 1),
                "must not exceed 262144",
            ),
        ):
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, message):
                GatewaySettings.from_env(
                    {
                        "CHEBY_PAIRING_SECRET": "configured-pairing-secret",
                        "CHEBY_BRIDGE_MODE": "fake",
                        name: value,
                    }
                )


if __name__ == "__main__":
    unittest.main()

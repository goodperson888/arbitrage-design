from __future__ import annotations

import json
import unittest
from tempfile import TemporaryDirectory
from pathlib import Path

from live_trader.identity import CLOBIdentityManager


class MemorySecretStore:
    def __init__(self) -> None:
        self.value = ""

    def get(self) -> str:
        return self.value

    def set(self, value: str) -> None:
        self.value = value


class FakeCreds:
    key = "api-key-value"
    secret = "api-secret-value"
    passphrase = "api-passphrase-value"


class FakeClient:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.balance_cache_updated = False

    def create_or_derive_api_key(self):
        return FakeCreds()

    def get_balance_allowance(self, *_args, **_kwargs):
        return {"balance": "5000000" if self.balance_cache_updated else "0", "allowances": {"exchange": "5000000"}}

    def update_balance_allowance(self, *_args, **_kwargs):
        self.balance_cache_updated = True

    def get_closed_only_mode(self):
        return {"closed_only": False}

    def get_open_orders(self):
        return []


class FakeZeroBalanceClient(FakeClient):
    def get_balance_allowance(self, *_args, **_kwargs):
        return {"balance": "0", "allowances": {"exchange": "5000000"}}


class IdentityTests(unittest.TestCase):
    def test_configure_derives_and_stores_clob_credentials_without_rpc(self) -> None:
        with TemporaryDirectory() as temp:
            private_store = MemorySecretStore()
            credential_store = MemorySecretStore()
            calls = []

            def factory(**kwargs):
                calls.append(kwargs)
                return FakeClient(**kwargs)

            manager = CLOBIdentityManager(
                private_key_store=private_store,
                credential_store=credential_store,
                client_factory=factory,
                signer_address=lambda _key: "0x" + "1" * 40,
            )
            result = manager.configure(
                signature_type=0,
                funder_address="0x" + "1" * 40,
                signer_private_key="0x" + "a" * 64,
                proxy="http://127.0.0.1:7890",
            )
            self.assertTrue(result["configured"])
            self.assertEqual(result["signatureType"], 0)
            self.assertEqual(private_store.get(), "0x" + "a" * 64)
            stored = json.loads(credential_store.get())
            self.assertEqual(stored["apiKey"], "api-key-value")
            self.assertNotIn("apiSecret", json.dumps(result))
            self.assertEqual(calls[0]["signature_type"], 0)
            self.assertEqual(calls[0]["funder"], "0x" + "1" * 40)

    def test_eoa_rejects_private_key_that_does_not_match_funder(self) -> None:
        manager = CLOBIdentityManager(
            private_key_store=MemorySecretStore(),
            credential_store=MemorySecretStore(),
            client_factory=lambda **_kwargs: FakeClient(),
            signer_address=lambda _key: "0x" + "2" * 40,
        )
        with self.assertRaisesRegex(ValueError, "EOA.*funder"):
            manager.configure(
                signature_type=0,
                funder_address="0x" + "1" * 40,
                signer_private_key="0x" + "a" * 64,
            )

    def test_validate_reads_authenticated_clob_state_without_submitting_order(self) -> None:
        private_store = MemorySecretStore()
        private_store.set("0x" + "a" * 64)
        credential_store = MemorySecretStore()
        credential_store.set(json.dumps({
            "signatureType": 0,
            "funderAddress": "0x" + "1" * 40,
            "apiKey": "api-key-value",
            "apiSecret": "api-secret-value",
            "apiPassphrase": "api-passphrase-value",
        }))
        calls = []
        clients = []

        def factory(**kwargs):
            calls.append(kwargs)
            client = FakeClient(**kwargs)
            clients.append(client)
            return client

        manager = CLOBIdentityManager(
            private_key_store=private_store,
            credential_store=credential_store,
            client_factory=factory,
            api_creds_factory=lambda **kwargs: kwargs,
            signer_address=lambda _key: "0x" + "1" * 40,
        )
        result = manager.validate()
        self.assertTrue(result["ok"])
        self.assertEqual(result["collateralBalance"], "5")
        self.assertFalse(result["closedOnly"])
        self.assertTrue(result["allowanceConfigured"])
        self.assertEqual(result["openOrderCount"], 0)
        self.assertEqual(calls[0]["creds"]["api_key"], "api-key-value")
        self.assertTrue(clients[0].balance_cache_updated)

    def test_validate_blocks_zero_clob_balance_even_when_authentication_succeeds(self) -> None:
        private_store = MemorySecretStore()
        private_store.set("0x" + "a" * 64)
        credential_store = MemorySecretStore()
        credential_store.set(json.dumps({
            "signatureType": 3,
            "funderAddress": "0x" + "1" * 40,
            "signerAddress": "0x" + "2" * 40,
            "apiKey": "api-key-value",
            "apiSecret": "api-secret-value",
            "apiPassphrase": "api-passphrase-value",
        }))
        manager = CLOBIdentityManager(
            private_key_store=private_store,
            credential_store=credential_store,
            client_factory=lambda **kwargs: FakeZeroBalanceClient(**kwargs),
            api_creds_factory=lambda **kwargs: kwargs,
            signer_address=lambda _key: "0x" + "2" * 40,
        )

        result = manager.validate()

        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "BLOCKED")
        self.assertEqual(result["diagnosticCode"], "SIGNER_FUNDER_UNMAPPED")
        self.assertEqual(result["signerAddress"], "0x" + "2" * 40)
        self.assertIn("私钥本身有效", result["message"])

    def test_validate_blocks_when_keychain_signer_differs_from_derived_credentials(self) -> None:
        private_store = MemorySecretStore()
        private_store.set("0x" + "a" * 64)
        credential_store = MemorySecretStore()
        credential_store.set(json.dumps({
            "signatureType": 3,
            "funderAddress": "0x" + "1" * 40,
            "signerAddress": "0x" + "3" * 40,
            "apiKey": "api-key-value",
            "apiSecret": "api-secret-value",
            "apiPassphrase": "api-passphrase-value",
        }))
        manager = CLOBIdentityManager(
            private_key_store=private_store,
            credential_store=credential_store,
            client_factory=lambda **_kwargs: self.fail("stale credentials must be rejected before CLOB access"),
            api_creds_factory=lambda **kwargs: kwargs,
            signer_address=lambda _key: "0x" + "2" * 40,
        )

        result = manager.validate()

        self.assertFalse(result["ok"])
        self.assertEqual(result["diagnosticCode"], "PRIVATE_KEY_CHANGED")
        self.assertIn("重新配置并派生", result["message"])

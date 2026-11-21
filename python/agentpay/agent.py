"""An autonomous client: holds its own key, escrows funds, and pays per call with vouchers."""

from __future__ import annotations

import json
import urllib.error
import urllib.request

from eth_account import Account

from .chain import Chain
from .vouchers import sign_voucher


class PaymentFailed(Exception):
    pass


class Agent:
    def __init__(self, chain: Chain, server_url: str, private_key: str | None = None):
        self.chain = chain
        self.url = server_url.rstrip("/")
        self.account = Account.from_key(private_key) if private_key else Account.create()
        self.key = self.account.key.hex()
        self.address = self.account.address
        self._requirements: dict | None = None
        self.spent = 0  # cumulative total promised in the current epoch

    # ------------------------------------------------------------ discovery
    def requirements(self, refresh: bool = False) -> dict:
        if refresh or self._requirements is None:
            with urllib.request.urlopen(f"{self.url}/.well-known/agent-pay") as resp:
                self._requirements = json.load(resp)
        return self._requirements

    @property
    def provider(self) -> str:
        return self.requirements()["provider"]

    # ------------------------------------------------------------- funding
    def open_channel(self, amount: int) -> list[str]:
        """Escrow `amount` for the server's provider. Two transactions, once."""
        txs = self.chain.approve_and_deposit(self.key, self.provider, amount)
        state = self.chain.channel(self.address, self.provider)
        self.spent = state.claimed
        return txs

    def remaining(self) -> int:
        state = self.chain.channel(self.address, self.provider)
        return state.deposit - max(self.spent, state.claimed)

    def close_channel(self) -> list[str]:
        """Ask to exit. After the provider's grace period, call `withdraw`."""
        return [self.chain.request_close(self.key, self.provider)]

    def withdraw(self) -> str:
        return self.chain.withdraw(self.key, self.provider)

    # ---------------------------------------------------------------- calls
    def call(self, tool: str, arguments: dict) -> str:
        """Call a paid tool, signing a voucher for exactly its price on top of what we owe."""
        price = int(self.requirements()["prices"][tool])
        state = self.chain.channel(self.address, self.provider)
        if state.deposit - max(self.spent, state.claimed) < price:
            raise PaymentFailed("escrow exhausted")

        cumulative = max(self.spent, state.claimed) + price
        voucher = sign_voucher(
            self.key,
            chain_id=self.chain.chain_id,
            escrow=self.chain.escrow_address,
            provider=self.provider,
            cumulative=cumulative,
            epoch=state.epoch,
        )
        body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": tool, "arguments": arguments}}
        status, payload = self._post(body, voucher.to_header())
        if status != 200:
            raise PaymentFailed(payload["error"]["message"])
        self.spent = cumulative
        return payload["result"]["content"][0]["text"]

    def _post(self, body: dict, payment: str | None):
        headers = {"Content-Type": "application/json"}
        if payment:
            headers["X-Payment"] = payment
        req = urllib.request.Request(f"{self.url}/mcp", json.dumps(body).encode(), headers)
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.load(resp)
        except urllib.error.HTTPError as err:
            return err.code, json.load(err)

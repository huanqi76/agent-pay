"""The provider's payment gate: check a voucher, let the request run, then bank the voucher."""

from __future__ import annotations

import json
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from web3 import Web3

from .chain import Chain
from .vouchers import Voucher


class PaymentRequired(Exception):
    """The request carries no acceptable payment. `reason` is safe to show the caller."""

    def __init__(self, reason: str, need: int | None = None):
        super().__init__(reason)
        self.reason = reason
        self.need = need


@dataclass(frozen=True)
class Accepted:
    cumulative: int
    signature: bytes


class VoucherStore:
    """Latest accepted voucher per payer, kept in a JSON file so settlement survives restarts."""

    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path else None
        self._data: dict[str, Accepted] = {}
        if self.path and self.path.exists():
            raw = json.loads(self.path.read_text())
            self._data = {
                payer: Accepted(int(v["cumulative"]), bytes.fromhex(v["signature"]))
                for payer, v in raw.items()
            }

    def get(self, payer: str) -> Accepted | None:
        return self._data.get(payer)

    def put(self, payer: str, accepted: Accepted) -> None:
        self._data[payer] = accepted
        if self.path:
            raw = {
                p: {"cumulative": str(a.cumulative), "signature": a.signature.hex()}
                for p, a in self._data.items()
            }
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(raw, indent=2))
            tmp.replace(self.path)

    def items(self):
        return list(self._data.items())


class PaymentGate:
    def __init__(self, chain: Chain, provider_key: str, store: VoucherStore | None = None):
        self.chain = chain
        self.provider_key = provider_key
        self.provider = Web3().eth.account.from_key(provider_key).address
        self.store = store or VoucherStore()
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    def requirements(self, prices: dict[str, int]) -> dict:
        return {
            "scheme": "voucher",
            "chainId": self.chain.chain_id,
            "escrow": self.chain.escrow_address,
            "token": self.chain.token_address,
            "provider": self.provider,
            "prices": {name: str(price) for name, price in prices.items()},
        }

    def _lock_for(self, payer: str) -> threading.Lock:
        with self._locks_guard:
            return self._locks.setdefault(payer, threading.Lock())

    @contextmanager
    def charge(self, header: str | None, price: int):
        """Check the voucher, yield to run the request, and bank the voucher only on success.

        Requests from one payer run one at a time, so vouchers always arrive in order.
        """
        if not header:
            raise PaymentRequired("payment required", need=price)
        try:
            fields = Voucher.parse_header(header)
        except ValueError as exc:
            raise PaymentRequired(str(exc), need=price) from exc

        payer = Web3.to_checksum_address(fields["payer"])
        cumulative, signature = fields["cumulative"], fields["signature"]

        with self._lock_for(payer):
            state = self.chain.channel(payer, self.provider)
            if state.close_at != 0:
                raise PaymentRequired("channel is closing; open a new one")
            previous = self.store.get(payer)
            floor = max(state.claimed, previous.cumulative if previous else 0)
            if cumulative < floor + price:
                raise PaymentRequired(f"voucher must reach at least {floor + price}", need=floor + price)
            if not self.chain.is_valid_voucher(payer, self.provider, cumulative, signature):
                raise PaymentRequired("voucher is invalid or exceeds the escrowed deposit")

            yield  # run the request; an exception here leaves the voucher unbanked

            self.store.put(payer, Accepted(cumulative, signature))

    def settle(self) -> list[tuple[str, int, str]]:
        """Redeem every payer's latest voucher on-chain. Returns (payer, amount, tx hash)."""
        done = []
        for payer, accepted in self.store.items():
            with self._lock_for(payer):
                state = self.chain.channel(payer, self.provider)
                if accepted.cumulative <= state.claimed:
                    continue
                tx = self.chain.claim(self.provider_key, payer, accepted.cumulative, accepted.signature)
                done.append((payer, accepted.cumulative - state.claimed, tx))
        return done

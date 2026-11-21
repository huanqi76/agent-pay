"""EIP-712 vouchers: an agent's signed promise of a cumulative total to one provider."""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass

from eth_account import Account
from eth_account.messages import encode_typed_data

DOMAIN_NAME = "AgentPay"
DOMAIN_VERSION = "1"

VOUCHER_TYPES = {
    "Voucher": [
        {"name": "payer", "type": "address"},
        {"name": "provider", "type": "address"},
        {"name": "cumulativeAmount", "type": "uint256"},
        {"name": "epoch", "type": "uint256"},
    ]
}


@dataclass(frozen=True)
class Voucher:
    payer: str
    provider: str
    cumulative: int
    epoch: int
    signature: bytes

    def to_header(self) -> str:
        """Encode for the X-Payment header."""
        body = {
            "payer": self.payer,
            "cumulative": str(self.cumulative),
            "epoch": self.epoch,
            "signature": "0x" + self.signature.hex(),
        }
        return base64.b64encode(json.dumps(body).encode()).decode()

    @staticmethod
    def parse_header(header: str) -> dict:
        """Decode an X-Payment header into its raw fields. Raises ValueError if malformed."""
        try:
            body = json.loads(base64.b64decode(header, validate=True))
            return {
                "payer": str(body["payer"]),
                "cumulative": int(body["cumulative"]),
                "signature": bytes.fromhex(str(body["signature"]).removeprefix("0x")),
            }
        except (ValueError, KeyError, TypeError) as exc:
            raise ValueError("malformed X-Payment header") from exc


def _typed_data(chain_id: int, escrow: str, payer: str, provider: str, cumulative: int, epoch: int):
    domain = {
        "name": DOMAIN_NAME,
        "version": DOMAIN_VERSION,
        "chainId": chain_id,
        "verifyingContract": escrow,
    }
    message = {
        "payer": payer,
        "provider": provider,
        "cumulativeAmount": cumulative,
        "epoch": epoch,
    }
    return encode_typed_data(domain_data=domain, message_types=VOUCHER_TYPES, message_data=message)


def sign_voucher(
    private_key: str,
    *,
    chain_id: int,
    escrow: str,
    provider: str,
    cumulative: int,
    epoch: int,
) -> Voucher:
    """Sign a voucher with the payer's key."""
    account = Account.from_key(private_key)
    signable = _typed_data(chain_id, escrow, account.address, provider, cumulative, epoch)
    signed = account.sign_message(signable)
    return Voucher(account.address, provider, cumulative, epoch, bytes(signed.signature))


def recover_signer(
    signature: bytes,
    *,
    chain_id: int,
    escrow: str,
    payer: str,
    provider: str,
    cumulative: int,
    epoch: int,
) -> str:
    """Return the address that signed this voucher (EOA signatures only)."""
    signable = _typed_data(chain_id, escrow, payer, provider, cumulative, epoch)
    return Account.recover_message(signable, signature=signature)

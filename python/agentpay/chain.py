"""Thin web3 wrapper around the AgentPayChannel and ERC-20 contracts."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from eth_account import Account
from web3 import Web3

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUT = Path(os.environ.get("AGENTPAY_OUT", REPO_ROOT / "out"))

ERC20_ABI = [
    {"type": "function", "name": "approve", "stateMutability": "nonpayable",
     "inputs": [{"name": "spender", "type": "address"}, {"name": "amount", "type": "uint256"}],
     "outputs": [{"name": "", "type": "bool"}]},
    {"type": "function", "name": "balanceOf", "stateMutability": "view",
     "inputs": [{"name": "account", "type": "address"}], "outputs": [{"name": "", "type": "uint256"}]},
    {"type": "function", "name": "mint", "stateMutability": "nonpayable",
     "inputs": [{"name": "to", "type": "address"}, {"name": "amount", "type": "uint256"}], "outputs": []},
]


def load_artifact(name: str, out_dir: Path = DEFAULT_OUT) -> dict:
    """Load a Foundry build artifact (run `forge build` first)."""
    path = Path(out_dir) / f"{name}.sol" / f"{name}.json"
    if not path.exists():
        raise FileNotFoundError(f"{path} not found; run `forge build` in the repo root")
    return json.loads(path.read_text())


@dataclass(frozen=True)
class ChannelState:
    deposit: int
    claimed: int
    close_at: int
    epoch: int

    @property
    def unclaimed(self) -> int:
        return self.deposit - self.claimed


class Chain:
    """Reads and writes the escrow contract. Writes are signed locally with the given key."""

    def __init__(self, w3: Web3, escrow: str, token: str, out_dir: Path = DEFAULT_OUT):
        self.w3 = w3
        self.escrow_address = Web3.to_checksum_address(escrow)
        self.token_address = Web3.to_checksum_address(token)
        abi = load_artifact("AgentPayChannel", out_dir)["abi"]
        self.escrow = w3.eth.contract(address=self.escrow_address, abi=abi)
        self.token = w3.eth.contract(address=self.token_address, abi=ERC20_ABI)

    # ---- reads
    @property
    def chain_id(self) -> int:
        return self.w3.eth.chain_id

    def channel(self, payer: str, provider: str) -> ChannelState:
        return ChannelState(*self.escrow.functions.channels(payer, provider).call())

    def is_valid_voucher(self, payer: str, provider: str, cumulative: int, signature: bytes) -> bool:
        return self.escrow.functions.isValidVoucher(payer, provider, cumulative, signature).call()

    def balance(self, who: str) -> int:
        return self.token.functions.balanceOf(who).call()

    # ---- writes
    def send(self, private_key: str, call) -> str:
        """Sign and send a contract call, wait for the receipt, and return the tx hash."""
        account = Account.from_key(private_key)
        tx = call.build_transaction({
            "from": account.address,
            "nonce": self.w3.eth.get_transaction_count(account.address),
            "chainId": self.chain_id,
        })
        signed = account.sign_transaction(tx)
        tx_hash = self.w3.eth.send_raw_transaction(signed.raw_transaction)
        receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash)
        if receipt.status != 1:
            raise RuntimeError(f"transaction reverted: {tx_hash.hex()}")
        return tx_hash.hex()

    def approve_and_deposit(self, private_key: str, provider: str, amount: int) -> list[str]:
        approve = self.token.functions.approve(self.escrow_address, amount)
        deposit = self.escrow.functions.deposit(provider, amount)
        return [self.send(private_key, approve), self.send(private_key, deposit)]

    def request_close(self, private_key: str, provider: str) -> str:
        return self.send(private_key, self.escrow.functions.requestClose(provider))

    def withdraw(self, private_key: str, provider: str) -> str:
        return self.send(private_key, self.escrow.functions.withdraw(provider))

    def claim(self, provider_key: str, payer: str, cumulative: int, signature: bytes) -> str:
        return self.send(provider_key, self.escrow.functions.claim(payer, cumulative, signature))


def deploy(w3: Web3, private_key: str, name: str, *args, out_dir: Path = DEFAULT_OUT) -> str:
    """Deploy a Foundry-built contract and return its address."""
    artifact = load_artifact(name, out_dir)
    account = Account.from_key(private_key)
    contract = w3.eth.contract(abi=artifact["abi"], bytecode=artifact["bytecode"]["object"])
    tx = contract.constructor(*args).build_transaction({
        "from": account.address,
        "nonce": w3.eth.get_transaction_count(account.address),
        "chainId": w3.eth.chain_id,
    })
    signed = account.sign_transaction(tx)
    receipt = w3.eth.wait_for_transaction_receipt(w3.eth.send_raw_transaction(signed.raw_transaction))
    return receipt.contractAddress

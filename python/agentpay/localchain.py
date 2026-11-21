"""Spin up a throwaway anvil chain with the contracts deployed. Used by the demo and tests."""

from __future__ import annotations

import shutil
import subprocess
import time
from contextlib import contextmanager
from dataclasses import dataclass

from web3 import Web3

from .chain import Chain, deploy

# Anvil's well-known development keys. Never use them anywhere real.
DEPLOYER_KEY = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
PROVIDER_KEY = "0x59c6995e998f97a5a0044966f0945389dc9e86dae88c7a8412f4603b6b78690d"

CLOSE_DELAY = 3600  # one hour


@dataclass
class Local:
    w3: Web3
    chain: Chain
    provider_key: str
    close_delay: int


def anvil_available() -> bool:
    return shutil.which("anvil") is not None


def fund_eth(w3: Web3, address: str, wei: int = 10**18) -> None:
    w3.provider.make_request("anvil_setBalance", [address, hex(wei)])


def mint_usdc(chain: Chain, address: str, amount: int) -> None:
    chain.send(DEPLOYER_KEY, chain.token.functions.mint(address, amount))


def advance_time(w3: Web3, seconds: int) -> None:
    w3.provider.make_request("evm_increaseTime", [seconds])
    w3.provider.make_request("evm_mine", [])


@contextmanager
def local_chain(port: int = 8545):
    """Start anvil, deploy MockUSDC and AgentPayChannel, and yield handles. Stops anvil on exit."""
    proc = subprocess.Popen(
        ["anvil", "--port", str(port), "--silent"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    w3 = Web3(Web3.HTTPProvider(f"http://127.0.0.1:{port}"))
    try:
        for _ in range(100):
            try:
                w3.eth.chain_id
                break
            except Exception:
                time.sleep(0.1)
        else:
            raise RuntimeError("anvil did not start")
        token = deploy(w3, DEPLOYER_KEY, "MockUSDC")
        escrow = deploy(w3, DEPLOYER_KEY, "AgentPayChannel", token, CLOSE_DELAY)
        yield Local(w3, Chain(w3, escrow, token), PROVIDER_KEY, CLOSE_DELAY)
    finally:
        proc.terminate()
        proc.wait(timeout=10)

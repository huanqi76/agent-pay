"""End-to-end demo on a local chain: an agent pays a provider per request with vouchers.

Needs `forge build` (for contract artifacts) and `anvil` on PATH.
"""

from __future__ import annotations

import tempfile
import threading
from pathlib import Path

from web3 import Web3

from .agent import Agent, PaymentFailed
from .gate import PaymentGate, VoucherStore
from .localchain import advance_time, fund_eth, local_chain, mint_usdc
from .server import make_server
from .tools import default_tools
from .vouchers import Voucher, sign_voucher

USDC = 10**6
PRICE = 10_000  # 0.01 USDC per call

ARTICLES = [
    "Payment channels let two parties exchange many small payments with only two on-chain "
    "transactions. The payer escrows funds once. Each payment is a signed message that raises a "
    "running total. The receiver redeems the largest total when it wants to settle.",
    "AI agents cannot open bank accounts. They can hold a key, though. A key is enough to sign "
    "messages, so an agent can prove who it is and promise money without any human onboarding.",
    "Gas costs make per-request payments on chain impractical. Off-chain vouchers avoid this. "
    "The provider checks each voucher against the escrow before serving a request, and settles "
    "in one transaction later.",
]


def usd(units: int) -> str:
    return f"{units / USDC:.2f} USDC"


def main() -> None:
    with local_chain() as env, tempfile.TemporaryDirectory() as tmp:
        w3, chain = env.w3, env.chain
        provider = Web3().eth.account.from_key(env.provider_key).address
        gate = PaymentGate(chain, env.provider_key, VoucherStore(Path(tmp) / "vouchers.json"))
        server = make_server(gate, default_tools(PRICE), port=0)
        url = f"http://127.0.0.1:{server.server_address[1]}"
        threading.Thread(target=server.serve_forever, daemon=True).start()

        agent = Agent(chain, url)
        fund_eth(w3, agent.address)
        mint_usdc(chain, agent.address, 1 * USDC)
        print(f"agent wallet     {agent.address}  (generated just now, holds {usd(chain.balance(agent.address))})")
        print(f"provider         {provider}")
        print(f"escrow contract  {chain.escrow_address}\n")

        print("1. agent escrows 0.25 USDC for the provider (approve + deposit: 2 transactions)")
        agent.open_channel(25 * USDC // 100)
        print(f"   escrowed: {usd(chain.channel(agent.address, provider).deposit)}\n")

        print("2. agent calls `summarize` 10 times, signing a voucher each time (no transactions, no human)")
        calls = 0
        while True:
            if calls == 10:
                print("   (done; 0.15 USDC is still in escrow)")
                break
            try:
                summary = agent.call("summarize", {"text": ARTICLES[calls % 3], "sentences": 1})
            except PaymentFailed as exc:
                print(f"   stopped: {exc}")
                break
            calls += 1
            print(f"   call {calls:>2}: paid {usd(PRICE)}  ->  {summary[:58]}...")
        print(f"   provider has banked a voucher worth {usd(gate.store.get(agent.address).cumulative)}\n")

        print("3. a forged voucher and a replayed voucher are both refused")
        thief = sign_voucher(Agent(chain, url).key, chain_id=chain.chain_id, escrow=chain.escrow_address,
                             provider=provider, cumulative=PRICE * 11, epoch=0)
        forged = Voucher(agent.address, provider, PRICE * 11, 0, thief.signature)  # claims to be the agent
        status, body = agent._post(_call_body(), forged.to_header())
        print(f"   forged  -> HTTP {status}: {body['error']['message']}")
        old = sign_voucher(agent.key, chain_id=chain.chain_id, escrow=chain.escrow_address,
                           provider=provider, cumulative=PRICE * 3, epoch=0)
        status, body = agent._post(_call_body(), old.to_header())
        print(f"   replay  -> HTTP {status}: {body['error']['message']}\n")

        print(f"4. provider redeems the latest voucher: {calls} calls settled in 1 transaction")
        before = chain.balance(provider)
        (payer, amount, tx), = gate.settle()
        print(f"   claim tx {tx[:18]}...  provider received {usd(chain.balance(provider) - before)}\n")

        print("5. agent exits: requests close, waits out the provider's grace period, withdraws the rest")
        agent.close_channel()
        advance_time(w3, env.close_delay)
        agent.withdraw()
        print(f"   agent refunded; balance now {usd(chain.balance(agent.address))}")
        print(f"   provider balance {usd(chain.balance(provider))}")
        print(f"   escrow holds {usd(chain.balance(chain.escrow_address))}\n")

        print(f"{calls} paid requests, 5 on-chain transactions (approve, deposit, claim, close, withdraw).")
        server.shutdown()


def _call_body() -> dict:
    return {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "summarize", "arguments": {"text": "One. Two. Three.", "sentences": 1}}}


if __name__ == "__main__":
    main()

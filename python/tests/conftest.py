import threading
from pathlib import Path

import pytest

from agentpay.agent import Agent
from agentpay.gate import PaymentGate, VoucherStore
from agentpay.localchain import anvil_available, fund_eth, local_chain, mint_usdc
from agentpay.server import make_server
from agentpay.tools import default_tools

PRICE = 10_000
USDC = 10**6

_OUT = Path(__file__).resolve().parents[2] / "out" / "AgentPayChannel.sol"
if not anvil_available() or not _OUT.exists():
    pytest.skip("needs anvil and built contracts (run `forge build`)", allow_module_level=True)


@pytest.fixture(scope="session")
def env():
    with local_chain(port=8599) as e:
        yield e


@pytest.fixture()
def stack(env, tmp_path):
    """A fresh gate, server and funded agent for each test."""
    gate = PaymentGate(env.chain, env.provider_key, VoucherStore(tmp_path / "vouchers.json"))
    server = make_server(gate, default_tools(PRICE), port=0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_address[1]}"
    agent = Agent(env.chain, url)
    fund_eth(env.w3, agent.address)
    mint_usdc(env.chain, agent.address, 10 * USDC)
    yield env, gate, agent, url
    server.shutdown()
    server.server_close()

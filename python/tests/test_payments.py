"""The agent, the gate and the contract working together on a local chain."""

import pytest

from agentpay.agent import PaymentFailed
from agentpay.localchain import advance_time
from agentpay.vouchers import Voucher, sign_voucher

from .conftest import PRICE, USDC

TEXT = "Alpha beta gamma. Beta gamma delta. Gamma delta epsilon. Zeta."


def _body():
    return {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "summarize", "arguments": {"text": TEXT, "sentences": 1}}}


def test_tools_list_is_free(stack):
    _, _, agent, _ = stack
    status, payload = agent._post({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, None)
    assert status == 200
    assert payload["result"]["tools"][0]["name"] == "summarize"
    assert payload["result"]["tools"][0]["price"] == str(PRICE)


def test_call_without_payment_gets_402_and_requirements(stack):
    env, _, agent, _ = stack
    status, payload = agent._post(_body(), None)
    assert status == 402
    data = payload["error"]["data"]
    assert data["escrow"] == env.chain.escrow_address
    assert data["need"] == str(PRICE)


def test_agent_pays_per_call_and_provider_settles_once(stack):
    env, gate, agent, _ = stack
    chain = env.chain
    agent.open_channel(1 * USDC)
    for _ in range(5):
        assert agent.call("summarize", {"text": TEXT, "sentences": 1})

    assert gate.store.get(agent.address).cumulative == 5 * PRICE
    before = chain.balance(gate.provider)
    [(payer, amount, _tx)] = gate.settle()
    assert payer == agent.address and amount == 5 * PRICE
    assert chain.balance(gate.provider) - before == 5 * PRICE
    assert gate.settle() == []  # nothing left to redeem


def test_forged_voucher_refused(stack):
    env, _, agent, _ = stack
    agent.open_channel(1 * USDC)
    from agentpay.agent import Agent
    thief = Agent(env.chain, agent.url)
    bad = sign_voucher(thief.key, chain_id=env.chain.chain_id, escrow=env.chain.escrow_address,
                       provider=agent.provider, cumulative=PRICE, epoch=0)
    status, _ = agent._post(_body(), Voucher(agent.address, agent.provider, PRICE, 0, bad.signature).to_header())
    assert status == 402


def test_replayed_voucher_refused(stack):
    env, _, agent, _ = stack
    agent.open_channel(1 * USDC)
    v = sign_voucher(agent.key, chain_id=env.chain.chain_id, escrow=env.chain.escrow_address,
                     provider=agent.provider, cumulative=PRICE, epoch=0)
    assert agent._post(_body(), v.to_header())[0] == 200
    status, payload = agent._post(_body(), v.to_header())
    assert status == 402 and "at least" in payload["error"]["message"]


def test_voucher_above_deposit_refused(stack):
    env, _, agent, _ = stack
    agent.open_channel(PRICE * 2)
    v = sign_voucher(agent.key, chain_id=env.chain.chain_id, escrow=env.chain.escrow_address,
                     provider=agent.provider, cumulative=PRICE * 3, epoch=0)
    assert agent._post(_body(), v.to_header())[0] == 402


def test_agent_stops_when_escrow_runs_out(stack):
    _, _, agent, _ = stack
    agent.open_channel(PRICE * 2)
    agent.call("summarize", {"text": TEXT})
    agent.call("summarize", {"text": TEXT})
    with pytest.raises(PaymentFailed, match="exhausted"):
        agent.call("summarize", {"text": TEXT})


def test_failed_tool_call_is_not_charged(stack):
    env, gate, agent, _ = stack
    agent.open_channel(1 * USDC)
    v = sign_voucher(agent.key, chain_id=env.chain.chain_id, escrow=env.chain.escrow_address,
                     provider=agent.provider, cumulative=PRICE, epoch=0)
    body = _body()
    body["params"]["arguments"] = {}  # missing required `text`
    assert agent._post(body, v.to_header())[0] == 400
    assert gate.store.get(agent.address) is None
    # The same voucher still works for a good request.
    assert agent._post(_body(), v.to_header())[0] == 200


def test_closing_channel_stops_new_service_but_old_vouchers_still_redeem(stack):
    env, gate, agent, _ = stack
    agent.open_channel(1 * USDC)
    agent.call("summarize", {"text": TEXT})
    agent.close_channel()
    with pytest.raises(PaymentFailed, match="closing"):
        agent.call("summarize", {"text": TEXT})

    before = env.chain.balance(gate.provider)
    gate.settle()  # provider redeems inside the grace period
    assert env.chain.balance(gate.provider) - before == PRICE

    advance_time(env.w3, env.close_delay)
    agent.withdraw()
    assert env.chain.balance(agent.address) == 10 * USDC - PRICE


def test_voucher_store_survives_restart(stack, tmp_path):
    env, gate, agent, _ = stack
    agent.open_channel(1 * USDC)
    agent.call("summarize", {"text": TEXT})
    from agentpay.gate import PaymentGate, VoucherStore
    revived = PaymentGate(env.chain, env.provider_key, VoucherStore(tmp_path / "vouchers.json"))
    assert revived.store.get(agent.address).cumulative == PRICE
    [(_, amount, _)] = revived.settle()
    assert amount == PRICE

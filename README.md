# agent-pay

Pay-per-call payments for AI agents. An agent escrows USDC once, then pays each request with a signed off-chain voucher. The provider redeems the latest voucher in a single transaction.

No accounts, no API keys, no per-call gas. The agent needs only a key, and its money can never leave the escrow except to the provider (up to what it signed) or back to the agent.

## How it works

- **Provider is safe.** The deposit is locked while a channel is open. A voucher is only accepted if it is covered by that deposit, and the payer cannot withdraw until `closeDelay` has passed, which gives the provider time to claim.
- **Agent is safe.** The provider can claim at most what the agent signed. Vouchers are bound to one provider, one contract and one chain, so they cannot be reused elsewhere.
- **No replay.** Claims must exceed what was already claimed. Closing a channel bumps its `epoch`, which invalidates every earlier voucher.
- **Smart wallets work.** Signatures are checked with ERC-1271 as well as for plain keys.

## Run it

Needs [Foundry](https://getfoundry.sh) and Python 3.10+.

```sh
forge build
forge test                      # 27 tests incl. fuzzing

pip install -e "python[test]"
pytest python                   # 14 end-to-end tests on a local chain
python -m agentpay.demo         # full walkthrough, ~5 seconds
```

The demo starts a local chain, deploys the contracts, then has a freshly generated agent wallet pay for 10 calls, get a forged voucher and a replayed voucher rejected, settle in one transaction, and withdraw the remainder.

## Layout

| Path | What |
| --- | --- |
| `src/AgentPayChannel.sol` | Escrow and voucher redemption |
| `test/` | Foundry unit and fuzz tests |
| `script/Deploy.s.sol` | Deploy to a real network (`TOKEN`, `CLOSE_DELAY`) |
| `python/agentpay/server.py` | Payment-gated tool server (HTTP 402 flow) |
| `python/agentpay/gate.py` | Voucher checks, per-payer ordering, persistence, settlement |
| `python/agentpay/agent.py` | Client that escrows and signs vouchers |
| `python/agentpay/demo.py` | The walkthrough |

## Server

The server speaks MCP-style JSON-RPC (`tools/list`, `tools/call`) over HTTP.

```sh
export PROVIDER_PRIVATE_KEY=0x...
python -m agentpay.server --escrow 0x... --token 0x... --rpc-url $RPC_URL
python -m agentpay.server settle --escrow 0x... --token 0x...   # redeem vouchers now
```

A voucher is banked only after the tool succeeds, so failed calls are not charged. The server settles on shutdown, or on an interval with `--auto-settle-secs`.

## Limits

- Each channel is one payer to one provider. There is no routing or multi-hop.

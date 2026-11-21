from eth_account import Account

from agentpay.vouchers import Voucher, recover_signer, sign_voucher

ESCROW = "0x5FbDB2315678afecb367f032d93F642f64180aa3"
PROVIDER = "0x70997970C51812dc3A010C7d01b50e0d17dc79C8"


def test_sign_then_recover_roundtrip():
    key = Account.create().key.hex()
    v = sign_voucher(key, chain_id=31337, escrow=ESCROW, provider=PROVIDER, cumulative=5, epoch=2)
    signer = recover_signer(v.signature, chain_id=31337, escrow=ESCROW, payer=v.payer,
                            provider=PROVIDER, cumulative=5, epoch=2)
    assert signer == v.payer


def test_changing_any_field_changes_the_signer():
    key = Account.create().key.hex()
    v = sign_voucher(key, chain_id=31337, escrow=ESCROW, provider=PROVIDER, cumulative=5, epoch=2)
    base = dict(chain_id=31337, escrow=ESCROW, payer=v.payer, provider=PROVIDER, cumulative=5, epoch=2)
    for change in ({"cumulative": 6}, {"epoch": 3}, {"chain_id": 1}, {"provider": v.payer}):
        assert recover_signer(v.signature, **(base | change)) != v.payer


def test_header_roundtrip():
    key = Account.create().key.hex()
    v = sign_voucher(key, chain_id=1, escrow=ESCROW, provider=PROVIDER, cumulative=7, epoch=0)
    fields = Voucher.parse_header(v.to_header())
    assert fields["payer"] == v.payer and fields["cumulative"] == 7 and fields["signature"] == v.signature


def test_malformed_header_rejected():
    import pytest
    for bad in ("not base64!", "e30=", "bnVsbA=="):
        with pytest.raises(ValueError):
            Voucher.parse_header(bad)

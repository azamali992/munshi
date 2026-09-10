import pytest

from munshi.agents.manager import build_manager, classify

M = build_manager()


@pytest.mark.parametrize("text,expected", [
    ("Chaudhry Farms ko 20 urea bhej do", "order"), ("confirm ORD-AAAA1111", "order"), ("Malik Agro ka khata", "order"),
    ("suggest dispatch for today", "godown"), ("approve DSP-AAAA1111", "godown"), ("restock WH-MULTAN 50 dap", "godown"),
    ("close STP-1 delivered all cash 1000 otp 1234", "delivery"), ("stops for DSP-AAAA1111", "delivery"),
    ("DSP-AAAA1111 driver handed 45000", "hisaab"), ("credit note Haji Sons 500", "hisaab"), ("digest", "hisaab"),
    ("who owes us", "wasooli"), ("remind everyone over 30 days", "wasooli"), ("Haji Sons promise 5000 by 2026-09-20", "wasooli"),
])
def test_routes(text, expected):
    assert classify(M, text) == expected


def test_out_of_scope_routes_nowhere():
    assert classify(M, "asdkj qwoeiru zzz") is None

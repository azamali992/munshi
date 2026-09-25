"""The customer's delivery code belongs to the customer. The HTTP routes blank it for a driver; the agent
tools (which the driver's chat assistant has) must never carry it either, or the driver could just ask."""
from __future__ import annotations

import json
from datetime import date

import pytest

from munshi.platform import MunshiPlatform


@pytest.fixture()
def p():
    p = MunshiPlatform()
    yield p
    p.close()


def _loaded_plan(p):
    r = p.repo
    o = r.create_order("C-001", [{"sku": "NPK-25", "qty": 10}], "chat", "", "order_munshi")
    r.confirm_order(o.order_id, "order_munshi", "clerk")
    r.allocate_order(o.order_id, "WH-MULTAN", "godown_munshi")
    plan = r.create_dispatch_plan(date.today().isoformat(), "R-MULTAN-N", "V-01", [o.order_id], "godown_munshi")
    r.approve_dispatch_plan(plan.plan_id, "godown_munshi", "clerk")
    return plan


def test_agent_tools_never_return_the_delivery_code(p):
    plan = _loaded_plan(p)
    real = [s.otp for s in p.repo.list_stops(plan.plan_id)]
    assert real and all(len(c) == 4 for c in real)            # the codes exist on the stops themselves
    blob = json.dumps([p.ops.list_stops(plan.plan_id), p.ops.get_plan(plan.plan_id)], default=str)
    assert all(s["otp"] is None for s in p.ops.list_stops(plan.plan_id))
    assert all(s["otp"] is None for s in p.ops.get_plan(plan.plan_id)["stops"])
    for code in real:
        assert f'"{code}"' not in blob


def test_driver_cannot_read_the_code_by_asking_in_chat(p):
    plan = _loaded_plan(p)
    real = [s.otp for s in p.repo.list_stops(plan.plan_id)]
    for text in (f"list stops for {plan.plan_id}", f"show me {plan.plan_id} stops", "mera agla stop kaun sa hai", "what is the delivery code"):
        r = p.handle_message(f"d-{abs(hash(text))}", "driver", text)
        for code in real:
            assert code not in r.text, (text, r.text)

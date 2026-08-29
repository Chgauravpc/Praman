"""The Razorpay TEST-mode client, tested with a fake transport.

Nobody's socket is ever touched here, which is the same discipline
``pramaan.llm.client`` is held to (``tests/scripted.py``): a client this
project depends on must be testable offline, or its own test suite depends on
connectivity and a live account. The fake transport exposes the same
``post``/``get`` signature ``requests`` itself does, so the class under test
cannot tell the difference between the fake and the real thing.
"""
from __future__ import annotations

import pytest

from pramaan.config import Config
from pramaan.execute.razorpay import (
    RazorpayError,
    RazorpayTestClient,
    TerminalStateGuard,
)


class FakeResponse:
    def __init__(self, status_code: int, payload):
        self.status_code = status_code
        self._payload = payload
        self.text = str(payload)

    def json(self):
        return self._payload


class FakeTransport:
    """Records every call it received, and answers a canned response."""

    def __init__(self, order_status: str = "created"):
        self.posts = []
        self.gets = []
        self.order_status = order_status

    def post(self, url, auth=None, json=None, headers=None, timeout=None):
        self.posts.append((url, auth, json, headers, timeout))
        if url.endswith("/orders"):
            return FakeResponse(200, {"id": "order_FAKE1", "status": "created", **json})
        if url.endswith("/payment_links"):
            return FakeResponse(
                200, {"id": "plink_FAKE1", "short_url": "https://rzp.io/i/fake"}
            )
        if url.endswith("/subscriptions"):
            return FakeResponse(200, {"id": "sub_FAKE1", "status": "created"})
        return FakeResponse(400, {"error": {"description": "unhandled path"}})

    def get(self, url, auth=None, timeout=None):
        self.gets.append((url, auth, timeout))
        return FakeResponse(200, {"id": url.rsplit("/", 1)[-1], "status": self.order_status})


def _config(key_id="rzp_test_abc", key_secret="shh") -> Config:
    return Config(seed=42, mode="shadow", llm_offline=True,
                  razorpay_key_id=key_id, razorpay_key_secret=key_secret)


# --------------------------------------------------------------------------
# Availability and test-mode enforcement
# --------------------------------------------------------------------------


def test_no_credentials_means_unavailable_and_no_call_is_ever_made():
    config = Config(seed=42, mode="shadow", llm_offline=True)
    transport = FakeTransport()
    client = RazorpayTestClient(config, transport=transport)
    assert client.available is False
    with pytest.raises(RazorpayError, match="no Razorpay test-mode credentials"):
        client.create_order(100_00)
    assert transport.posts == []  # never even attempted


def test_a_live_mode_key_is_refused_at_construction():
    """NFR-7: this project runs in Razorpay TEST mode only, structurally."""
    config = Config(seed=42, mode="shadow", llm_offline=True,
                     razorpay_key_id="rzp_live_should_never_appear", razorpay_key_secret="x")
    with pytest.raises(RuntimeError, match="test mode"):
        RazorpayTestClient(config, transport=FakeTransport())


# --------------------------------------------------------------------------
# The three creation calls
# --------------------------------------------------------------------------


def test_create_order_sends_amount_and_receipt():
    transport = FakeTransport()
    client = RazorpayTestClient(_config(), transport=transport)
    order = client.create_order(150_000, receipt="r1", idempotency_key_value="key123")

    assert order["id"] == "order_FAKE1"
    url, auth, body, headers, timeout = transport.posts[0]
    assert url.endswith("/orders")
    assert body["amount"] == 150_000
    assert body["receipt"] == "r1"
    assert auth == ("rzp_test_abc", "shh")
    assert headers["X-Razorpay-Idempotency-Key"] == "key123"


def test_create_payment_link_sends_description():
    transport = FakeTransport()
    client = RazorpayTestClient(_config(), transport=transport)
    link = client.create_payment_link(150_000, description="recovery link")

    assert link["short_url"].startswith("https://")
    url, _auth, body, _headers, _timeout = transport.posts[0]
    assert url.endswith("/payment_links")
    assert body["description"] == "recovery link"


def test_create_subscription_requires_a_plan_id():
    transport = FakeTransport()
    client = RazorpayTestClient(_config(), transport=transport)
    sub = client.create_subscription("plan_FAKE", total_count=12)
    assert sub["id"] == "sub_FAKE1"
    _url, _auth, body, _headers, _timeout = transport.posts[0]
    assert body["plan_id"] == "plan_FAKE"
    assert body["customer_notify"] == 1


# --------------------------------------------------------------------------
# Error handling
# --------------------------------------------------------------------------


def test_an_http_error_status_raises_razorpay_error():
    transport = FakeTransport()
    client = RazorpayTestClient(_config(), transport=transport)
    with pytest.raises(RazorpayError, match="HTTP 400"):
        client._post("nonexistent_path", {})


# --------------------------------------------------------------------------
# The terminal-state guard -- S1, against a live re-read
# --------------------------------------------------------------------------


def test_the_guard_lets_an_unpaid_order_through():
    transport = FakeTransport(order_status="created")
    client = RazorpayTestClient(_config(), transport=transport)
    guard = TerminalStateGuard(client)

    result, acted = guard.guard("order_FAKE1", lambda: "action ran")
    assert acted is True
    assert result == "action ran"


def test_the_guard_aborts_a_paid_order_without_calling_act():
    """S1: the order was paid since the plan was made. Do not act; do not call
    the action at all."""
    transport = FakeTransport(order_status="paid")
    client = RazorpayTestClient(_config(), transport=transport)
    guard = TerminalStateGuard(client)

    calls = {"n": 0}

    def act():
        calls["n"] += 1
        return "should never happen"

    result, acted = guard.guard("order_FAKE1", act)
    assert acted is False
    assert result is None
    assert calls["n"] == 0


def test_is_order_paid_reads_the_status_field():
    for status, expected in (("created", False), ("attempted", False), ("paid", True)):
        transport = FakeTransport(order_status=status)
        client = RazorpayTestClient(_config(), transport=transport)
        assert client.is_order_paid("order_FAKE1") is expected

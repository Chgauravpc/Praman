"""The Razorpay TEST-mode client. Idempotent, and it never trusts stale state.

PRD 12.1, BUILD-PLAN Day 5 block C. Three things this file is careful about,
each one a real incident shape in a payments system rather than a hypothetical:

**Idempotency key.** ``hash(payment_id, action_type, attempt_ordinal)`` -- PRD
12.1's own formula, verbatim. Razorpay's webhooks are documented as
at-least-once and out-of-order (the same fact ``pramaan.sense.store`` already
builds NFR-5 around), so a retry-scheduler bug, a webhook redelivery, or a
crashed process resuming mid-batch can all attempt the same logical action
twice. ``IdempotencyStore`` is the local authority on "have we already done
this" -- it does not depend on Razorpay honouring the header it is also sent,
because the property this project needs to guarantee is one it must own.

**Per-counterparty advisory lock, with a TTL** (``pramaan.execute.locks``) --
imported by the runner, not by this module, so this client stays a pure
wrapper over one HTTP surface with no notion of concurrency policy.

**Terminal-state guard.** Re-read state before acting; abort on
``order_already_paid`` (S1). Stated in PRD 12.1 as a general principle; here it
is a callable object because "act" is different every time -- creating a
payment link is not sending a reminder is not retrying a charge -- and the
guard's only job is to interpose one fresh read between "the plan says do this"
and "this actually happens".

**Test mode is enforced, not merely assumed.** ``Config.guard_test_mode``
already refuses to construct a config carrying a non-``rzp_test_`` key
(NFR-7); this module re-checks at construction, because a client is exactly
the kind of object that outlives the config it was built from and a defence
checked once at startup is a defence that can be bypassed by a caller holding
the client past a config reload.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional, Tuple

from pramaan.canonical import canonical_json, sha256_hex
from pramaan.config import Config

RAZORPAY_API_BASE = "https://api.razorpay.com/v1"

#: Actions this project can take against Razorpay. A closed vocabulary, so an
#: idempotency key cannot be built for a typo'd action name that silently never
#: collides with the correctly-spelled one.
ACTION_TYPES: Tuple[str, ...] = (
    "create_order",
    "create_payment_link",
    "create_subscription",
    "retry_charge",
)


def idempotency_key(payment_id: str, action_type: str, attempt_ordinal: int) -> str:
    """PRD 12.1's formula, verbatim: ``hash(payment_id, action_type, attempt_ordinal)``.

    SHA-256 over canonical JSON, matching every other hash in this project
    (``canonical.sha256_hex``) rather than a bespoke digest -- one hashing
    convention, so a reviewer checking any hash in the repo checks the same
    function.
    """
    if action_type not in ACTION_TYPES:
        raise ValueError("unknown action_type %r; must be one of %r" % (action_type, ACTION_TYPES))
    return sha256_hex(
        canonical_json(
            {
                "payment_id": payment_id,
                "action_type": action_type,
                "attempt_ordinal": attempt_ordinal,
            }
        )
    )


class RazorpayError(RuntimeError):
    """A Razorpay API call failed. Carries the HTTP status where known."""


@dataclass
class IdempotencyStore:
    """The local authority on "have we already executed this action".

    In-memory, keyed by ``idempotency_key``. A real deployment would back this
    with a table alongside the ledger; in-memory is correct for a single batch
    run or a single process's lifetime, and the property under test --
    ``execute_once`` calls its function exactly once per key, however many
    times it is invoked -- does not depend on where the dict lives.
    """

    _results: Dict[str, Any] = field(default_factory=dict)

    def execute_once(self, key: str, fn: Callable[[], Any]) -> Tuple[Any, bool]:
        """Run ``fn`` the first time ``key`` is seen; replay its result after.

        Returns ``(result, was_new)``. ``was_new=False`` is the no-op case PRD
        12.1 and the Day 5 definition of done ask for: ``fn`` is not called
        again, so a duplicate webhook or a re-run of the same plan step cannot
        create a second Razorpay order for one intended action.
        """
        if key in self._results:
            return self._results[key], False
        result = fn()
        self._results[key] = result
        return result, True

    def seen(self, key: str) -> bool:
        return key in self._results

    def count(self) -> int:
        return len(self._results)


class RazorpayTestClient:
    """A thin wrapper over the Razorpay REST API, TEST mode only.

    ``transport`` defaults to the ``requests`` module itself -- ``requests``
    exposes module-level ``get``/``post`` with signatures this class calls
    directly, so the real network path and an injected fake need no adapter.
    Tests pass a fake object exposing the same two functions and never touch a
    socket, which is what keeps this client's tests offline like everything
    else in the project (NFR-4's spirit extended to this module: a client that
    could only be tested against the real network would make its own test
    suite depend on connectivity and a live account).
    """

    def __init__(self, config: Config, *, transport: Optional[Any] = None) -> None:
        config.guard_test_mode()
        self.config = config
        self._transport = transport
        self.calls_made = 0

    @property
    def available(self) -> bool:
        """Both credentials are present. Never true means never called."""
        return bool(self.config.razorpay_key_id and self.config.razorpay_key_secret)

    def _require_available(self) -> None:
        if not self.available:
            raise RazorpayError(
                "no Razorpay test-mode credentials configured "
                "(RAZORPAY_KEY_ID / RAZORPAY_KEY_SECRET). Set them in .env to "
                "exercise a real test-mode call; every other command in this "
                "project runs without them (NFR-4)."
            )

    def _transport_module(self):
        if self._transport is not None:
            return self._transport
        import requests

        return requests

    def _auth(self) -> Tuple[str, str]:
        return (self.config.razorpay_key_id or "", self.config.razorpay_key_secret or "")

    def _post(self, path: str, body: Dict[str, Any], *, idempotency_key_value: Optional[str] = None) -> Dict[str, Any]:
        self._require_available()
        transport = self._transport_module()
        headers = {"Content-Type": "application/json"}
        if idempotency_key_value:
            # Razorpay's own idempotency-key header, sent as a second line of
            # defence. This project's correctness does not depend on the
            # provider honouring it -- IdempotencyStore is the authority --
            # but there is no reason not to also ask the provider to dedupe.
            headers["X-Razorpay-Idempotency-Key"] = idempotency_key_value
        response = transport.post(
            "%s/%s" % (RAZORPAY_API_BASE, path),
            auth=self._auth(),
            json=body,
            headers=headers,
            timeout=30,
        )
        self.calls_made += 1
        return self._parse(response, path)

    def _get(self, path: str) -> Dict[str, Any]:
        self._require_available()
        transport = self._transport_module()
        response = transport.get(
            "%s/%s" % (RAZORPAY_API_BASE, path),
            auth=self._auth(),
            timeout=30,
        )
        self.calls_made += 1
        return self._parse(response, path)

    @staticmethod
    def _parse(response: Any, path: str) -> Dict[str, Any]:
        status = getattr(response, "status_code", None)
        if status is not None and status >= 400:
            raise RazorpayError(
                "Razorpay API call to %s failed with HTTP %s: %s"
                % (path, status, getattr(response, "text", ""))
            )
        return response.json()

    # -- the three creation calls ----------------------------------------

    def create_order(
        self,
        amount_paise: int,
        *,
        currency: str = "INR",
        receipt: Optional[str] = None,
        notes: Optional[Dict[str, str]] = None,
        idempotency_key_value: Optional[str] = None,
    ) -> Dict[str, Any]:
        body: Dict[str, Any] = {"amount": int(amount_paise), "currency": currency}
        if receipt:
            body["receipt"] = receipt
        if notes:
            body["notes"] = notes
        return self._post("orders", body, idempotency_key_value=idempotency_key_value)

    def create_payment_link(
        self,
        amount_paise: int,
        *,
        description: str,
        customer: Optional[Dict[str, str]] = None,
        currency: str = "INR",
        notes: Optional[Dict[str, str]] = None,
        idempotency_key_value: Optional[str] = None,
    ) -> Dict[str, Any]:
        body: Dict[str, Any] = {
            "amount": int(amount_paise),
            "currency": currency,
            "description": description,
        }
        if customer:
            body["customer"] = customer
        if notes:
            body["notes"] = notes
        return self._post("payment_links", body, idempotency_key_value=idempotency_key_value)

    def create_subscription(
        self,
        plan_id: str,
        *,
        total_count: int,
        customer_notify: bool = True,
        notes: Optional[Dict[str, str]] = None,
        idempotency_key_value: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Requires a plan already created in the Razorpay test-mode dashboard.

        Unlike an order or a payment link, a subscription cannot be created
        from nothing -- Razorpay requires a pre-existing Plan resource, which
        is a manual, one-time dashboard step rather than something this client
        can provision for itself. Callers without a ``plan_id`` should not call
        this; ``runner.run_execute`` skips it and says so rather than failing.
        """
        body: Dict[str, Any] = {
            "plan_id": plan_id,
            "total_count": int(total_count),
            "customer_notify": 1 if customer_notify else 0,
        }
        if notes:
            body["notes"] = notes
        return self._post("subscriptions", body, idempotency_key_value=idempotency_key_value)

    # -- reads, for the terminal-state guard -----------------------------

    def fetch_order(self, order_id: str) -> Dict[str, Any]:
        return self._get("orders/%s" % order_id)

    def fetch_payment(self, payment_id: str) -> Dict[str, Any]:
        return self._get("payments/%s" % payment_id)

    def is_order_paid(self, order_id: str) -> bool:
        """Re-read an order's status. The terminal-state guard's own question.

        Razorpay's order status is one of ``created`` / ``attempted`` /
        ``paid``. Only ``paid`` triggers S1 -- ``attempted`` means a payment
        was tried and may itself have failed, which is the ordinary case this
        whole project exists to act on, not a reason to stand down.
        """
        order = self.fetch_order(order_id)
        return order.get("status") == "paid"


@dataclass
class TerminalStateGuard:
    """Re-reads order state immediately before acting. Aborts on S1.

    PRD 12.1: "Terminal-state guard: re-read state before acting; on
    ``order_already_paid`` ... abort the thread (S1)." The one-line summary
    that matters is in the name -- *guard*, not *check*: calling code cannot
    act without going through ``guard`` first, because the interposition is
    the whole safety property. A caller that read the order status itself and
    then decided whether to call ``act`` could always skip the read.
    """

    client: RazorpayTestClient

    def guard(self, order_id: str, act: Callable[[], Any]) -> Tuple[Optional[Any], bool]:
        """Re-check ``order_id``, then either run ``act`` or abort.

        Returns ``(result, acted)``. ``acted=False`` means S1 fired: the order
        is already paid, ``act`` was never called, and the thread must
        terminate here -- exactly the stopping-rule contract
        ``pramaan.envelope.stopping.s1_already_paid`` states for the
        simulated path, now enforced against a live re-read rather than the
        event's own (possibly stale) reason code.
        """
        if self.client.is_order_paid(order_id):
            return None, False
        return act(), True

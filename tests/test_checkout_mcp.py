import asyncio
import json
from pathlib import Path
from time import time
from types import SimpleNamespace
from uuid import UUID

import httpx2
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from mcp.types import CallToolResult
from starlette.testclient import TestClient
from test_catalog_mcp import DETAILS, HEADERS, PHONE_ID

from ecommerce_store_mcp.checkout import CustomerProfile
from ecommerce_store_mcp.checkout_auth import checkout_transport_security
from ecommerce_store_mcp.code_mode import create_sandbox_order, finish_sandbox_order
from ecommerce_store_mcp.server import create_server
from ecommerce_store_mcp.settings import CheckoutAuth, Settings

TENANT_ID = "10000000-0000-4000-8000-000000000001"
API_ID = "10000000-0000-4000-8000-000000000002"
SPA_ID = "10000000-0000-4000-8000-000000000003"
OID = "10000000-0000-4000-8000-000000000004"
CLIENT_ID = "20000000-0000-4000-8000-000000000001"
OTHER_CLIENT_ID = "20000000-0000-4000-8000-000000000002"
CART_ID = "30000000-0000-4000-8000-000000000001"
ORDER_ID = "40000000-0000-4000-8000-000000000001"
INVOICE_ID = "50000000-0000-4000-8000-000000000001"
SESSION_ID = "cs_test_fixture1"
CHECKOUT_URL = f"https://checkout.stripe.com/c/pay/{SESSION_ID}#example"
CREATED_AT = "2026-11-27T00:00:00Z"
PDF = b"%PDF-1.4\nSandbox invoice fixture\n%%EOF\n"
AUTH = CheckoutAuth(
    tenant_id=TENANT_ID,
    api_client_id=API_ID,
    resource_url="https://mcp.example.test/mcp",
    identity_key="synthetic-test-identity-key-at-least-32-bytes",
)


class StoreFixture:
    def __init__(self) -> None:
        self.external_id = ""
        self.lines: list[dict] = []
        self.order_lines: list[dict] = []
        self.order_owner = CLIENT_ID
        self.payment_open = False
        self.settled = False
        self.session_id = SESSION_ID
        self.checkout_url = CHECKOUT_URL
        self.interrupt_order = False
        self.calls: list[tuple[str, str]] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        path = request.url.path.removeprefix("/backend")
        self.calls.append((request.method, path))
        cart = {"id": CART_ID, "clientId": CLIENT_ID, "lines": self.lines}
        order = {
            "id": ORDER_ID,
            "clientId": self.order_owner,
            "createdAt": CREATED_AT,
            "status": "Paid" if self.settled else "Created",
            "totalAmount": 2229.95,
            "totalCurrency": "PLN",
            "lines": self.order_lines,
        }
        payment = {
            "order_id": ORDER_ID,
            "status": "succeeded" if self.settled else "pending",
            "amount_minor": 222995,
            "currency": "pln",
            "provider_session_id": self.session_id,
        }
        match request.method, path:
            case "GET", p if p == f"/mobile-phones/{PHONE_ID}":
                return httpx2.Response(200, json=DETAILS)
            case "POST", "/registrations/customers":
                body = json.loads(request.content)
                assert body["individual"]["shippingAddress"] == body["individual"]["billingAddress"]
                assert "firstName" in body["individual"]
                assert not self.external_id or self.external_id == body["externalId"]
                self.external_id = body["externalId"]
                return httpx2.Response(201, json={"id": CLIENT_ID, "externalId": self.external_id})
            case "GET", p if p == f"/customers/external/{self.external_id}":
                return httpx2.Response(200, json={"id": CLIENT_ID, "externalId": self.external_id})
            case "GET", p if p == f"/shopping-carts/client/{CLIENT_ID}":
                return httpx2.Response(200, json=cart)
            case "PUT", p if p == f"/shopping-carts/{CLIENT_ID}":
                self.lines = json.loads(request.content)["lines"]
                assert self.lines == [{"productId": PHONE_ID, "quantity": 1}]
                return httpx2.Response(200, json={**cart, "lines": self.lines})
            case "POST", p if p == f"/orders/client/{CLIENT_ID}":
                assert request.content == b""
                self.order_lines = [
                    {
                        "productVersion": {"productId": line["productId"], "name": "Test Phone"},
                        "quantity": line["quantity"],
                    }
                    for line in self.lines
                ]
                self.lines = []
                if self.interrupt_order:
                    raise httpx2.ReadTimeout("response interrupted after order creation")
                return httpx2.Response(200, json={**order, "lines": self.order_lines})
            case "GET", p if p == f"/orders/{ORDER_ID}":
                return httpx2.Response(200, json=order)
            case "GET", p if p == f"/orders/client/{CLIENT_ID}":
                return httpx2.Response(200, json=[order] if self.order_lines else [])
            case "POST", p if p == f"/payments/{ORDER_ID}/checkout":
                assert request.content == b""
                self.payment_open = True
                return httpx2.Response(
                    200,
                    json={
                        "payment": payment,
                        "checkout_status": "open",
                        "checkout_url": self.checkout_url,
                        "expires_at": "2026-11-28T00:00:00Z",
                    },
                )
            case "GET", p if p == f"/payments/order/{ORDER_ID}":
                return httpx2.Response(200, json=payment) if self.payment_open else httpx2.Response(404)
            case "GET", p if p == f"/invoices/by-order/{ORDER_ID}":
                assert self.settled
                return httpx2.Response(
                    200,
                    json={
                        "id": INVOICE_ID,
                        "orderId": ORDER_ID,
                        "createdAt": CREATED_AT,
                        "storageUrl": "https://example.test/private/invoice.pdf",
                    },
                )
            case "GET", p if p == f"/invoices/{INVOICE_ID}/pdf":
                assert self.settled
                return httpx2.Response(200, content=PDF, headers={"content-type": "application/pdf"})
        pytest.fail(f"Unexpected BFF request: {request.method} {path}")


@pytest.fixture
def signing_key(monkeypatch: pytest.MonkeyPatch) -> rsa.RSAPrivateKey:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    monkeypatch.setattr(
        jwt.PyJWKClient, "get_signing_key_from_jwt", lambda _self, _token: SimpleNamespace(key=key.public_key())
    )
    return key


def token_for(key: rsa.RSAPrivateKey, **overrides: str | int) -> str:
    claims = {
        "iss": AUTH.issuer,
        "aud": API_ID,
        "tid": TENANT_ID,
        "oid": OID,
        "azp": SPA_ID,
        "exp": int(time()) + 300,
        "scp": "mcp.checkout",
    }
    return jwt.encode(claims | overrides, key, algorithm="RS256", headers={"kid": "fixture"})


def checkout_http(store: StoreFixture) -> TestClient:
    settings = Settings(bff_base_url="https://bff.example.test/backend", checkout_auth=AUTH)
    server = create_server(settings, transport=httpx2.MockTransport(store))
    return TestClient(
        server.streamable_http_app(
            stateless_http=True, json_response=True, transport_security=checkout_transport_security(AUTH)
        ),
        base_url="https://mcp.example.test",
    )


def rpc(client: TestClient, token: str | None, method: str, parameters: dict | None = None):
    headers = HEADERS | ({"Authorization": f"Bearer {token}"} if token else {})
    return client.post(
        "/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": parameters or {}}
    )


def call(client: TestClient, token: str, name: str, arguments: dict | None = None) -> dict:
    response = rpc(client, token, "tools/call", {"name": name, "arguments": arguments or {}})
    assert response.status_code == 200
    return response.json()["result"]


def synthetic_profile() -> CustomerProfile:
    path = Path(__file__).resolve().parents[1] / "examples" / "sandbox-customer.json"
    return CustomerProfile.model_validate_json(path.read_bytes())


def test_checkout_advertises_oauth_and_requires_a_matching_delegated_token(signing_key: rsa.RSAPrivateKey) -> None:
    store = StoreFixture()
    with checkout_http(store) as client:
        unauthenticated = rpc(client, None, "tools/list")
        assert unauthenticated.status_code == 401
        assert ".well-known/oauth-protected-resource/mcp" in unauthenticated.headers["WWW-Authenticate"]
        metadata = client.get("/.well-known/oauth-protected-resource/mcp").json()
        assert metadata["scopes_supported"] == [AUTH.scope]
        assert metadata["authorization_servers"] == [AUTH.issuer]
        assert rpc(client, token_for(signing_key, aud=SPA_ID), "tools/list").status_code == 401
        assert rpc(client, token_for(signing_key, tid=SPA_ID), "tools/list").status_code == 401
        assert rpc(client, token_for(signing_key, exp=int(time()) - 1), "tools/list").status_code == 401
        assert rpc(client, token_for(signing_key, scp="different.scope"), "tools/list").status_code == 403
        forged = token_for(rsa.generate_private_key(public_exponent=65537, key_size=2048))
        assert rpc(client, forged, "tools/list").status_code == 401
        wrong_host = client.post(
            "/mcp",
            headers=HEADERS | {"Host": "other.example.test", "Authorization": f"Bearer {token_for(signing_key)}"},
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        )
        assert wrong_host.status_code == 421
        tools = rpc(client, token_for(signing_key), "tools/list").json()["result"]["tools"]
        assert {"register_customer", "set_cart", "start_checkout", "download_invoice"} <= {t["name"] for t in tools}
        assert all("client_id" not in t["inputSchema"]["properties"] for t in tools)
    assert store.calls == []


def test_checkout_denies_other_orders_before_accessing_payments_or_invoices(signing_key: rsa.RSAPrivateKey) -> None:
    store = StoreFixture()
    store.order_owner = OTHER_CLIENT_ID
    with checkout_http(store) as client:
        token = token_for(signing_key)
        profile = synthetic_profile().model_dump(mode="json")
        registration = call(client, token, "register_customer", {"profile": profile})
        assert registration["structuredContent"]["customer_id"] == CLIENT_ID
        # An alternative OAuth client for the same user gets the same account.
        another = token_for(signing_key, azp="10000000-0000-4000-8000-000000000005")
        repeated = call(client, another, "register_customer", {"profile": profile})
        assert repeated["structuredContent"]["customer_id"] == CLIENT_ID
        for tool in ("start_checkout", "get_purchase", "download_invoice"):
            result = call(client, token, tool, {"order_id": ORDER_ID})
            assert result["isError"]
            assert result["content"][0]["text"].endswith("Order not found.")
    assert not any(path.startswith(("/payments/", "/invoices/")) for _, path in store.calls)


@pytest.mark.parametrize(
    ("session_id", "url"),
    [
        ("cs_live_fixture", "https://checkout.stripe.com/c/pay/cs_live_fixture"),
        (SESSION_ID, f"https://not-stripe.example.test/c/pay/{SESSION_ID}"),
        (SESSION_ID, "https://checkout.stripe.com/c/pay/cs_test_different"),
    ],
)
def test_checkout_rejects_unexpected_session_links(signing_key: rsa.RSAPrivateKey, session_id: str, url: str) -> None:
    store = StoreFixture()
    store.session_id, store.checkout_url = session_id, url
    with checkout_http(store) as client:
        token = token_for(signing_key)
        call(client, token, "register_customer", {"profile": synthetic_profile().model_dump(mode="json")})
        result = call(client, token, "start_checkout", {"order_id": ORDER_ID})
        assert result["isError"]
        assert url not in result["content"][0]["text"]


def test_code_mode_purchases_and_downloads_completed_invoice(
    signing_key: rsa.RSAPrivateKey, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    store = StoreFixture()
    with checkout_http(store) as client:
        token = token_for(signing_key)

        class Session:
            async def call_tool(self, name: str, arguments: dict | None = None) -> CallToolResult:
                return CallToolResult.model_validate(call(client, token, name, arguments))

        async def complete_sandbox_payment(_seconds: float) -> None:
            assert store.payment_open
            assert not store.settled
            store.settled = True

        monkeypatch.setattr("ecommerce_store_mcp.code_mode.sleep", complete_sandbox_payment)
        session = Session()
        order_id = asyncio.run(create_sandbox_order(session, synthetic_profile(), UUID(PHONE_ID), 1))
        assert str(order_id) == ORDER_ID
        before_payment = call(client, token, "download_invoice", {"order_id": ORDER_ID})
        assert before_payment["isError"]
        assert not any(path.startswith("/invoices/") for _, path in store.calls)

        output = tmp_path / "invoice.pdf"
        result = asyncio.run(finish_sandbox_order(session, order_id, 1, output))
        assert result == output and output.read_bytes() == PDF
        purchase = call(client, token, "get_purchase", {"order_id": ORDER_ID})["structuredContent"]
        assert purchase["payment"]["status"] == "succeeded"
        assert purchase["invoice"]["id"] == INVOICE_ID
        assert "storageUrl" not in purchase["invoice"]


def test_lists_the_committed_order_after_an_interrupted_create(signing_key: rsa.RSAPrivateKey) -> None:
    store = StoreFixture()
    store.interrupt_order = True
    with checkout_http(store) as client:
        token = token_for(signing_key)
        call(client, token, "register_customer", {"profile": synthetic_profile().model_dump(mode="json")})
        call(client, token, "set_cart", {"lines": [{"product_id": PHONE_ID, "quantity": 1}]})
        created = call(client, token, "create_order")
        assert created["isError"]
        assert "Check your order list" in created["content"][0]["text"]
        recovered = call(client, token, "list_orders")["structuredContent"]["orders"]
        assert [order["id"] for order in recovered] == [ORDER_ID]
    assert store.calls.count(("POST", f"/orders/client/{CLIENT_ID}")) == 1

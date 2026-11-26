from collections.abc import Callable

import httpx2
import pytest
from starlette.testclient import TestClient

from ecommerce_store_mcp.server import create_server
from ecommerce_store_mcp.settings import Settings

PHONE_ID = "8dd76ff0-1ce8-41da-80fd-2987620806b7"
PHOTO = "https://photos.example.test/phone.jpg"
PRICE = {"amount": 2229.95, "currency": "PLN"}
SUMMARY = {
    "id": PHONE_ID,
    "name": "Test Phone",
    "brand": "Test",
    "mainPhoto": PHOTO,
    "displayType": "OLED",
    "screenSizeInches": 6.2,
    "camera": "50 MP",
    "price": PRICE,
}
DETAILS = {
    "id": PHONE_ID,
    "isActive": True,
    "commonDescription": {
        "name": "Test Phone",
        "brand": "Test",
        "description": "A catalog test phone",
        "mainPhoto": PHOTO,
        "otherPhotos": [],
    },
    "electronicDetails": {"processor": "Test CPU"},
    "connectivity": {"bluetooth": "5.3"},
    "satelliteNavigationSystems": {"gps": True},
    "sensors": {"accelerometer": True},
    "camera": "50 MP",
    "price": PRICE,
    "fingerPrint": True,
    "faceId": False,
    "description2": "Test phone specification",
    "description3": "Test phone accessories",
}
PROTOCOL_VERSION = "2025-11-25"
HEADERS = {"Accept": "application/json, text/event-stream", "MCP-Protocol-Version": PROTOCOL_VERSION}


def mcp_client(upstream: Callable[[httpx2.Request], httpx2.Response]) -> TestClient:
    settings = Settings(bff_base_url="https://bff.example.test/backend")
    server = create_server(settings, transport=httpx2.MockTransport(upstream))
    return TestClient(
        server.streamable_http_app(stateless_http=True, json_response=True), base_url="http://127.0.0.1:8080"
    )


def send(client: TestClient, method: str, params: dict | None = None) -> dict:
    response = client.post(
        "/mcp", headers=HEADERS, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}
    )
    assert response.status_code == 200, response.text
    return response.json()["result"]


def initialize(client: TestClient) -> None:
    result = send(
        client,
        "initialize",
        {"protocolVersion": PROTOCOL_VERSION, "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}},
    )
    assert result["protocolVersion"] == PROTOCOL_VERSION


def test_discover_and_read_catalog_through_mcp() -> None:
    urls: list[httpx2.URL] = []

    def upstream(request: httpx2.Request) -> httpx2.Response:
        assert request.method == "GET"
        urls.append(request.url)
        return httpx2.Response(200, json=DETAILS if request.url.path.endswith(PHONE_ID) else [SUMMARY])

    with mcp_client(upstream) as client:
        assert client.get("/health/live").json() == {"status": "ok"}
        initialize(client)
        tools = send(client, "tools/list")["tools"]
        assert {tool["name"] for tool in tools} == {"list_mobile_phones", "get_mobile_phone"}
        assert all(tool["annotations"]["readOnlyHint"] for tool in tools)
        assert all("ctx" not in tool["inputSchema"]["properties"] for tool in tools)
        listing = send(client, "tools/call", {"name": "list_mobile_phones", "arguments": {"amount": 2}})
        phone = listing["structuredContent"]["phones"][0]
        assert phone["id"] == PHONE_ID and phone["price"] == {"amount": "2229.95", "currency": "PLN"}
        detail = send(client, "tools/call", {"name": "get_mobile_phone", "arguments": {"phone_id": phone["id"]}})
        assert detail["structuredContent"]["description"]["name"] == "Test Phone"
    assert [str(url) for url in urls] == [
        "https://bff.example.test/backend/mobile-phones?amount=2",
        f"https://bff.example.test/backend/mobile-phones/{PHONE_ID}",
    ]


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        ("list_mobile_phones", {"amount": 0}),
        ("list_mobile_phones", {"amount": 51}),
        ("get_mobile_phone", {"phone_id": "invalid"}),
    ],
)
def test_invalid_arguments_do_not_call_the_bff(tool: str, arguments: dict) -> None:
    def upstream(_request: httpx2.Request) -> httpx2.Response:
        pytest.fail("Invalid catalog arguments reached the BFF")

    with mcp_client(upstream) as client:
        initialize(client)
        result = send(client, "tools/call", {"name": tool, "arguments": arguments})
        assert result["isError"]


@pytest.mark.parametrize(
    ("status", "message"),
    [
        (404, "The requested catalog item was not found."),
        (503, "The store catalog is unavailable. Try again later."),
        (200, "The store catalog returned an invalid response."),
    ],
)
def test_upstream_errors_are_reported_by_the_tool(status: int, message: str) -> None:
    def upstream(_request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(status, json={"detail": "Upstream response data"})

    with mcp_client(upstream) as client:
        initialize(client)
        result = send(client, "tools/call", {"name": "get_mobile_phone", "arguments": {"phone_id": PHONE_ID}})
        assert result["isError"]
        assert result["content"][0]["text"].endswith(message)
        assert "Upstream response data" not in result["content"][0]["text"]

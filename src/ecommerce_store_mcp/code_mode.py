"""Run the store's sandbox purchase using the MCP protocol as a code client."""

import argparse
import asyncio
import json
import os
from asyncio import sleep
from base64 import b64decode
from pathlib import Path
from time import monotonic
from urllib.parse import urlsplit
from uuid import UUID

import httpx2
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.types import BlobResourceContents, CallToolResult, EmbeddedResource

from ecommerce_store_mcp.checkout import (
    MAX_INVOICE_BYTES,
    CartLine,
    Checkout,
    CustomerProfile,
    sandbox_checkout_url,
)


class CheckoutProgramError(RuntimeError):
    pass


async def _call(session: ClientSession, tool: str, arguments: dict | None = None) -> CallToolResult:
    result = await session.call_tool(tool, arguments)
    if result.is_error or not isinstance(result.structured_content, dict):
        message = f"The {tool} step failed."
        if tool == "create_order":
            message += " Run the orders command to check for a created order before retrying."
        raise CheckoutProgramError(message)
    return result


async def create_sandbox_order(session: ClientSession, profile: CustomerProfile, phone_id: UUID, quantity: int) -> UUID:
    line = CartLine(product_id=phone_id, quantity=quantity)
    await _call(session, "get_mobile_phone", {"phone_id": str(phone_id)})
    registration = await _call(session, "register_customer", {"profile": profile.model_dump(mode="json")})
    current_lines = [CartLine.model_validate(item) for item in registration.structured_content["cart"]["lines"]]
    if current_lines and current_lines != [line]:
        raise CheckoutProgramError(
            "The account has a different shopping cart. Review it before starting this purchase."
        )
    if not current_lines:
        await _call(session, "set_cart", {"lines": [line.model_dump(mode="json")]})

    result = await _call(session, "create_order")
    order = result.structured_content
    order_id = UUID(order["id"])
    print(f"Order ID {order_id}", flush=True)
    ordered = [
        CartLine.model_validate({"product_id": item["product"]["product_id"], "quantity": item["quantity"]})
        for item in order["lines"]
    ]
    if ordered != [line]:
        raise CheckoutProgramError("The created order differs from the requested cart. Review the order before paying.")
    return order_id


async def finish_sandbox_order(session: ClientSession, order_id: UUID, wait_seconds: int, output: Path | None) -> Path:
    arguments = {"order_id": str(order_id)}
    purchase = (await _call(session, "get_purchase", arguments)).structured_content
    if purchase["order"]["status"].casefold() == "created" and (
        purchase["payment"] is None or purchase["payment"]["status"] in {"created", "pending"}
    ):
        checkout = Checkout.model_validate((await _call(session, "start_checkout", arguments)).structured_content)
        if checkout.payment.order_id != order_id:
            raise CheckoutProgramError("Checkout returned a different order.")
        if url := sandbox_checkout_url(checkout):
            print(f"Complete the test payment in Stripe Sandbox\n{url}", flush=True)
        elif checkout.checkout_status == "expired":
            raise CheckoutProgramError("The checkout session expired. Review the order and payment before retrying.")

    deadline = monotonic() + wait_seconds
    while purchase["invoice"] is None:
        payment = purchase["payment"]
        if purchase["order"]["status"].casefold() == "cancelled" or (
            payment is not None and payment["status"] in {"failed", "canceled"}
        ):
            raise CheckoutProgramError("This order or payment has been cancelled or failed.")
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise CheckoutProgramError(f"The invoice is still pending. Resume with order ID {order_id}.")
        await sleep(min(5, remaining))
        purchase = (await _call(session, "get_purchase", arguments)).structured_content

    invoice_id = UUID(purchase["invoice"]["id"])
    result = await _call(session, "download_invoice", arguments)
    if result.structured_content["id"] != str(invoice_id) or result.structured_content["order_id"] != str(order_id):
        raise CheckoutProgramError("The returned PDF does not match the completed invoice.")
    document = next(
        (
            item.resource.blob
            for item in result.content
            if isinstance(item, EmbeddedResource)
            and isinstance(item.resource, BlobResourceContents)
            and item.resource.mime_type == "application/pdf"
            and item.resource.uri == f"ecommerce://invoices/{invoice_id}.pdf"
        ),
        None,
    )
    if document is None:
        raise CheckoutProgramError("The MCP server did not return an invoice PDF.")
    pdf = b64decode(document, validate=True)
    if not pdf.startswith(b"%PDF-") or len(pdf) > MAX_INVOICE_BYTES:
        raise CheckoutProgramError("The MCP server returned an invalid invoice PDF.")
    path = output if output is not None else Path(f"invoice-{invoice_id}.pdf")
    with path.open("xb") as file:
        file.write(pdf)
    return path


async def _run(args: argparse.Namespace, endpoint: str, access_token: str) -> None:
    failure = None
    async with (
        httpx2.AsyncClient(
            headers={"Authorization": f"Bearer {access_token}"}, timeout=60, follow_redirects=False
        ) as transport,
        streamable_http_client(endpoint, http_client=transport) as (read, write, _),
        ClientSession(read, write, read_timeout_seconds=60) as session,
    ):
        await session.initialize()
        try:
            if args.command == "orders":
                result = await _call(session, "list_orders")
                print(json.dumps(result.structured_content, indent=2))
                return

            order_id = args.order_id
            if args.command == "buy":
                profile = CustomerProfile.model_validate_json(args.profile.read_bytes())
                order_id = await create_sandbox_order(session, profile, args.phone_id, args.quantity)
            path = await finish_sandbox_order(session, order_id, args.wait_seconds, args.output)
            print(f"Saved invoice to {path}")
        except CheckoutProgramError as error:
            failure = error
    if failure is not None:
        raise failure


def _endpoint(value: str) -> str:
    url = urlsplit(value)
    if (
        (url.scheme != "https" and not (url.scheme == "http" and url.hostname in {"localhost", "127.0.0.1", "::1"}))
        or not url.hostname
        or url.username is not None
        or url.password is not None
        or url.path != "/mcp"
        or url.query
        or url.fragment
    ):
        raise ValueError("Use the HTTPS MCP endpoint or an HTTP loopback MCP endpoint for local testing.")
    return value


def main() -> None:
    parser = argparse.ArgumentParser(description="Purchase a sandbox store item and save its invoice PDF through MCP.")
    parser.add_argument(
        "--wait-seconds", type=int, default=600, help="Maximum wait for payment and invoice fulfillment."
    )
    parser.add_argument("--output", type=Path, help="Path for the completed PDF.")
    commands = parser.add_subparsers(dest="command", required=True)
    buy = commands.add_parser("buy", help="Register a sandbox customer and create a new order.")
    buy.add_argument("--profile", type=Path, required=True, help="Synthetic customer profile JSON.")
    buy.add_argument("--phone-id", type=UUID, required=True)
    buy.add_argument("--quantity", type=int, default=1)
    buy.set_defaults(order_id=None)
    resume = commands.add_parser("resume", help="Continue from an existing order ID.")
    resume.add_argument("--order-id", type=UUID, required=True)
    commands.add_parser("orders", help="Find your orders after an interrupted checkout.")
    args = parser.parse_args()

    try:
        endpoint = _endpoint(os.environ.get("ECOMMERCE_MCP_URL", "http://127.0.0.1:8080/mcp"))
    except ValueError as error:
        parser.error(str(error))
    token = os.environ.get("ECOMMERCE_MCP_ACCESS_TOKEN", "").strip()
    if not token:
        parser.error("Set ECOMMERCE_MCP_ACCESS_TOKEN to an Entra access token for this MCP API.")
    if args.wait_seconds < 1:
        parser.error("--wait-seconds must be positive.")

    try:
        asyncio.run(_run(args, endpoint, token))
    except (CheckoutProgramError, OSError, ValueError, ExceptionGroup) as error:
        if isinstance(error, CheckoutProgramError):
            parser.exit(1, f"{error}\n")
        parser.exit(1, "Checkout could not finish. Check the endpoint, profile, and invoice output path.\n")


if __name__ == "__main__":
    main()

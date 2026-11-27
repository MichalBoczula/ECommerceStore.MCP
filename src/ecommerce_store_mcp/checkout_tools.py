from base64 import b64encode
from typing import Annotated
from uuid import UUID

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import BlobResourceContents, CallToolResult, EmbeddedResource, ToolAnnotations
from pydantic import Field

from ecommerce_store_mcp.checkout import (
    Cart,
    CartLine,
    Checkout,
    CheckoutClient,
    CustomerProfile,
    Order,
    OrderList,
    Purchase,
    Registration,
)


def _checkout_context(ctx: Context) -> tuple[CheckoutClient, UUID]:
    token = get_access_token()
    if token is None or token.subject is None:
        raise ToolError("Sign in to use the sandbox checkout tools.")
    return ctx.request_context.lifespan_context.checkout, UUID(token.subject)


def add_checkout_tools(server: MCPServer) -> None:
    read_only = ToolAnnotations(read_only_hint=True, open_world_hint=False)
    retryable = ToolAnnotations(
        read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=False
    )
    replace = ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=True, open_world_hint=False)
    create = ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=False)

    @server.tool(title="Register sandbox customer", annotations=retryable)
    async def register_customer(ctx: Context, profile: CustomerProfile) -> Registration:
        """Register a sandbox customer and a cart for the signed-in account. Use synthetic contact and address data."""
        client, owner = _checkout_context(ctx)
        return await client.register_customer(owner, profile)

    @server.tool(title="Read your shopping cart", annotations=read_only)
    async def get_cart(ctx: Context) -> Cart:
        """Read the current shopping cart for the signed-in account."""
        client, owner = _checkout_context(ctx)
        return await client.get_cart(owner)

    @server.tool(title="Set your shopping cart", annotations=replace)
    async def set_cart(ctx: Context, lines: list[CartLine]) -> Cart:
        """Replace the cart with the exact product IDs and quantities supplied. Use IDs from the catalog."""
        client, owner = _checkout_context(ctx)
        return await client.set_cart(owner, lines)

    @server.tool(title="Create an order from your cart", annotations=create)
    async def create_order(ctx: Context) -> Order:
        """Create an order from your cart and empty that cart. Check your order list after an interrupted request."""
        client, owner = _checkout_context(ctx)
        return await client.create_order(owner)

    @server.tool(title="List your orders", annotations=read_only)
    async def list_orders(ctx: Context, limit: Annotated[int, Field(ge=1, le=50)] = 10) -> OrderList:
        """List the newest orders belonging to the signed-in account."""
        client, owner = _checkout_context(ctx)
        return await client.list_orders(owner, limit)

    @server.tool(title="Start Stripe Sandbox Checkout", annotations=retryable)
    async def start_checkout(ctx: Context, order_id: UUID) -> Checkout:
        """Open or resume Stripe Sandbox Checkout for your order. Complete the payment on Stripe's hosted page."""
        client, owner = _checkout_context(ctx)
        return await client.start_checkout(owner, order_id)

    @server.tool(title="Check your payment and invoice", annotations=read_only)
    async def get_purchase(ctx: Context, order_id: UUID) -> Purchase:
        """Read your order, verified payment status, and completed invoice metadata when available."""
        client, owner = _checkout_context(ctx)
        return await client.get_purchase(owner, order_id)

    @server.tool(title="Download your completed invoice PDF", annotations=read_only)
    async def download_invoice(ctx: Context, order_id: UUID) -> CallToolResult:
        """Return the completed PDF for an owned order after the sandbox payment succeeds."""
        client, owner = _checkout_context(ctx)
        invoice, pdf = await client.download_invoice(owner, order_id)
        return CallToolResult(
            structuredContent=invoice.model_dump(mode="json"),
            content=[
                EmbeddedResource(
                    resource=BlobResourceContents(
                        uri=f"ecommerce://invoices/{invoice.id}.pdf",
                        mimeType="application/pdf",
                        blob=b64encode(pdf).decode("ascii"),
                    )
                )
            ],
        )

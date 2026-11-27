import re
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal
from urllib.parse import urlsplit
from uuid import UUID

import httpx2
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import AliasChoices, BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

Nonblank = Annotated[str, Field(min_length=1)]
MAX_INVOICE_BYTES = 5 * 1024 * 1024


class Address(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    postal_code: Nonblank = Field(serialization_alias="postalCode")
    city: Nonblank
    street: Nonblank
    building_number: Nonblank = Field(serialization_alias="buildingNumber")
    apartment_number: str | None = Field(default=None, serialization_alias="apartmentNumber")


class CustomerProfile(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    first_name: Nonblank = Field(serialization_alias="firstName")
    last_name: Nonblank = Field(serialization_alias="lastName")
    email: Nonblank
    phone: Nonblank
    billing_address: Address = Field(serialization_alias="billingAddress")
    shipping_address: Address | None = Field(default=None, serialization_alias="shippingAddress")


class Customer(BaseModel):
    id: UUID
    external_id: UUID = Field(validation_alias="externalId", exclude=True)


class CartLine(BaseModel):
    product_id: UUID = Field(validation_alias=AliasChoices("product_id", "productId"))
    quantity: Annotated[int, Field(ge=1, le=2_147_483_647)]


class Cart(BaseModel):
    id: UUID
    client_id: UUID = Field(validation_alias="clientId", exclude=True)
    lines: list[CartLine]


class Registration(BaseModel):
    customer_id: UUID
    cart: Cart


class OrderedProduct(BaseModel):
    product_id: UUID = Field(validation_alias="productId")
    name: str


class OrderLine(BaseModel):
    quantity: int
    product: OrderedProduct = Field(validation_alias="productVersion")


class Order(BaseModel):
    id: UUID
    client_id: UUID = Field(validation_alias="clientId", exclude=True)
    created_at: datetime = Field(validation_alias="createdAt")
    status: str
    total_amount: Decimal = Field(validation_alias="totalAmount")
    currency: str = Field(validation_alias="totalCurrency")
    lines: list[OrderLine]


class OrderList(BaseModel):
    orders: list[Order]


class Payment(BaseModel):
    order_id: UUID
    amount_minor: int
    currency: str
    status: str
    provider_session_id: str | None


class Checkout(BaseModel):
    checkout_status: Literal["open", "complete", "expired"]
    checkout_url: str | None
    expires_at: datetime
    payment: Payment


class Invoice(BaseModel):
    id: UUID
    order_id: UUID = Field(validation_alias="orderId")
    created_at: datetime = Field(validation_alias="createdAt")


class Purchase(BaseModel):
    order: Order
    payment: Payment | None
    invoice: Invoice | None


_CUSTOMER = TypeAdapter(Customer)
_CART = TypeAdapter(Cart)
_ORDER = TypeAdapter(Order)
_ORDERS = TypeAdapter(list[Order])
_PAYMENT = TypeAdapter(Payment)
_CHECKOUT = TypeAdapter(Checkout)
_INVOICE = TypeAdapter(Invoice)


def sandbox_session_id(payment: Payment) -> str:
    session = payment.provider_session_id
    if session is None or not re.fullmatch(r"cs_test_[a-zA-Z0-9]+", session):
        raise ToolError("Expected a Stripe Sandbox payment session.")
    return session


def sandbox_checkout_url(checkout: Checkout) -> str | None:
    session = sandbox_session_id(checkout.payment)
    url = checkout.checkout_url
    if url is None and checkout.checkout_status != "open":
        return None
    try:
        parsed = urlsplit(url or "")
        if (
            url is not None
            and not any(character.isspace() for character in url)
            and parsed.scheme == "https"
            and parsed.hostname == "checkout.stripe.com"
            and parsed.username is None
            and parsed.password is None
            and parsed.port in (None, 443)
            and parsed.path in {f"/pay/{session}", f"/c/pay/{session}"}
        ):
            return url
    except ValueError:
        pass
    raise ToolError("The store did not return the hosted URL for this Stripe Sandbox session.")


class CheckoutClient:
    def __init__(self, client: httpx2.AsyncClient) -> None:
        self._client = client

    async def register_customer(self, owner: UUID, profile: CustomerProfile) -> Registration:
        individual = profile.model_dump(mode="json", by_alias=True)
        if profile.shipping_address is None:
            individual["shippingAddress"] = individual["billingAddress"]
        response = await self._send(
            "POST", "registrations/customers", payload={"externalId": str(owner), "individual": individual}
        )
        customer = self._decode(response, _CUSTOMER)
        self._require_customer(customer, owner)
        return Registration(customer_id=customer.id, cart=await self._cart(customer.id))

    async def get_cart(self, owner: UUID) -> Cart:
        return await self._cart((await self._customer(owner)).id)

    async def set_cart(self, owner: UUID, lines: list[CartLine]) -> Cart:
        customer = await self._customer(owner)
        response = await self._send(
            "PUT",
            f"shopping-carts/{customer.id}",
            payload={"lines": [{"productId": str(line.product_id), "quantity": line.quantity} for line in lines]},
        )
        return self._read_cart(response, customer.id)

    async def create_order(self, owner: UUID) -> Order:
        customer = await self._customer(owner)
        response = await self._send("POST", f"orders/client/{customer.id}")
        if response.status_code >= 500:
            raise ToolError("Order creation was interrupted. Check your order list before retrying.")
        return self._read_order(response, customer.id)

    async def list_orders(self, owner: UUID, limit: int) -> OrderList:
        customer = await self._customer(owner)
        response = await self._send("GET", f"orders/client/{customer.id}")
        orders = self._decode(response, _ORDERS)
        if any(order.client_id != customer.id for order in orders):
            raise ToolError("The store returned orders outside the signed-in account.")
        return OrderList(orders=sorted(orders, key=lambda order: order.created_at, reverse=True)[:limit])

    async def start_checkout(self, owner: UUID, order_id: UUID) -> Checkout:
        await self._owned_order(owner, order_id)
        checkout = self._decode(await self._send("POST", f"payments/{order_id}/checkout"), _CHECKOUT)
        self._require_payment_order(checkout.payment, order_id)
        sandbox_checkout_url(checkout)
        return checkout

    async def get_purchase(self, owner: UUID, order_id: UUID) -> Purchase:
        order = await self._owned_order(owner, order_id)
        response = await self._send("GET", f"payments/order/{order_id}")
        if response.status_code == 404:
            return Purchase(order=order, payment=None, invoice=None)
        payment = self._decode(response, _PAYMENT)
        self._require_payment_order(payment, order_id)
        if payment.provider_session_id is not None or payment.status == "succeeded":
            sandbox_session_id(payment)

        invoice = None
        if payment.status == "succeeded" and order.status.casefold() == "paid":
            response = await self._send("GET", f"invoices/by-order/{order_id}")
            if response.status_code != 404:
                invoice = self._decode(response, _INVOICE)
                if invoice.order_id != order_id:
                    raise ToolError("The invoice does not belong to the requested order.")
        return Purchase(order=order, payment=payment, invoice=invoice)

    async def download_invoice(self, owner: UUID, order_id: UUID) -> tuple[Invoice, bytes]:
        purchase = await self.get_purchase(owner, order_id)
        invoice = purchase.invoice
        if invoice is None:
            raise ToolError("The completed invoice is not available yet.")
        pdf = bytearray()
        try:
            async with self._client.stream("GET", f"invoices/{invoice.id}/pdf") as response:
                self._require_success(response)
                if response.headers.get("content-type", "").split(";")[0].strip().lower() != "application/pdf":
                    raise ToolError("The store returned an invalid invoice PDF.")
                async for chunk in response.aiter_bytes():
                    if len(pdf) + len(chunk) > MAX_INVOICE_BYTES:
                        raise ToolError("The invoice exceeds the PDF download limit.")
                    pdf.extend(chunk)
        except httpx2.RequestError:
            raise ToolError("The invoice download was interrupted. Try again.") from None
        if not pdf.startswith(b"%PDF-"):
            raise ToolError("The store returned an invalid invoice PDF.")
        return invoice, bytes(pdf)

    async def _customer(self, owner: UUID) -> Customer:
        response = await self._send("GET", f"customers/external/{owner}")
        if response.status_code == 404:
            raise ToolError("Register a sandbox customer for this account first.")
        customer = self._decode(response, _CUSTOMER)
        self._require_customer(customer, owner)
        return customer

    async def _cart(self, customer_id: UUID) -> Cart:
        return self._read_cart(await self._send("GET", f"shopping-carts/client/{customer_id}"), customer_id)

    async def _owned_order(self, owner: UUID, order_id: UUID) -> Order:
        customer = await self._customer(owner)
        response = await self._send("GET", f"orders/{order_id}")
        order = self._read_order(response, customer.id)
        if order.id != order_id:
            raise ToolError("Order not found.")
        return order

    async def _send(self, method: str, path: str, *, payload: dict | None = None) -> httpx2.Response:
        try:
            return await self._client.request(method, path, json=payload)
        except httpx2.RequestError:
            if method == "POST" and path.startswith("orders/client/"):
                raise ToolError("Order creation was interrupted. Check your order list before retrying.") from None
            raise ToolError("The store checkout is unavailable. Try again later.") from None

    def _decode[T](self, response: httpx2.Response, adapter: TypeAdapter[T]) -> T:
        self._require_success(response)
        try:
            return adapter.validate_json(response.content)
        except ValidationError:
            raise ToolError("The store checkout returned an invalid response.") from None

    def _read_cart(self, response: httpx2.Response, customer_id: UUID) -> Cart:
        cart = self._decode(response, _CART)
        if cart.client_id != customer_id:
            raise ToolError("Cart not found.")
        return cart

    def _read_order(self, response: httpx2.Response, customer_id: UUID) -> Order:
        if response.status_code == 404:
            raise ToolError("Order not found.")
        order = self._decode(response, _ORDER)
        if order.client_id != customer_id:
            raise ToolError("Order not found.")
        return order

    @staticmethod
    def _require_customer(customer: Customer, owner: UUID) -> None:
        if customer.external_id != owner:
            raise ToolError("The store customer does not belong to the signed-in account.")

    @staticmethod
    def _require_payment_order(payment: Payment, order_id: UUID) -> None:
        if payment.order_id != order_id:
            raise ToolError("The payment does not belong to the requested order.")

    @staticmethod
    def _require_success(response: httpx2.Response) -> None:
        if response.is_success:
            return
        if response.status_code in {400, 404, 409, 422}:
            raise ToolError(f"The store rejected the checkout operation (HTTP {response.status_code}).")
        raise ToolError("The store checkout is unavailable. Try again later.")

# ECommerce Store MCP

Model Context Protocol tools backed by the ECommerce Store BFF. The first release exposes the public phone catalog over Streamable HTTP.

| Tool | BFF request |
| --- | --- |
| `list_mobile_phones` | `GET /mobile-phones?amount=15` by default |
| `get_mobile_phone` | `GET /mobile-phones/{id}` |

The list returns up to 50 phones per call. Reuse the catalog UUID to read a phone's price, photos, and specifications. Both tools advertise read-only behavior.

## Run locally

Use Python 3.14.7 and uv 0.12.18. Set `ECOMMERCE_BFF_BASE_URL` to the BFF origin. For Azure DEV, use the `bff` URL from the Terraform `app_urls` output.

```powershell
$env:ECOMMERCE_BFF_BASE_URL = 'https://your-dev-bff.example'
uv sync --locked
uv run --locked ecommerce-store-mcp
```

The MCP endpoint is `http://127.0.0.1:8080/mcp`. `GET /health/live` reports whether the process can serve HTTP. Set `ECOMMERCE_PORT` if port 8080 is in use.

Use [MCP Inspector](https://modelcontextprotocol.io/docs/tools/inspector) with Streamable HTTP, or in GitHub Copilot for VS Code select **Chat: Open Customizations → MCP Servers → Add Server → HTTP** and enter the same URL. Try asking Copilot to show two phones and retrieve the specifications for one by its ID.

For a hosted instance, use its HTTPS `/mcp` URL in a supported MCP client.
[Microsoft Copilot Studio](https://learn.microsoft.com/en-us/microsoft-copilot-studio/mcp-add-existing-server-to-agent) · [ChatGPT](https://developers.openai.com/plugins/deploy/connect-chatgpt)

## Enable sandbox checkout

The default deployment serves the catalog. Configure checkout for a sandbox
BFF with Stripe test checkout, verified webhooks and invoice fulfillment
enabled. Register an Entra v2 API with the default Application ID URI
`api://<API application client ID>` and the delegated scope `mcp.checkout`.
Grant that scope to the interactive client that connects to MCP.

| Environment variable | Value |
| --- | --- |
| `ECOMMERCE_CHECKOUT_AUTH__TENANT_ID` | Entra Directory (tenant) ID |
| `ECOMMERCE_CHECKOUT_AUTH__API_CLIENT_ID` | Application (client) ID of the MCP API |
| `ECOMMERCE_CHECKOUT_AUTH__RESOURCE_URL` | Exact public HTTPS `/mcp` endpoint or the loopback MCP URL for local tests |
| `ECOMMERCE_CHECKOUT_AUTH__IDENTITY_KEY` | Stable random server-side key of at least 32 bytes |

Checkout mode protects `/mcp` with Entra bearer tokens and publishes OAuth
resource metadata. The server derives one customer identity per tenant and Entra
user. Preserve the identity key across revisions so existing registrations and
orders remain accessible. The MCP tools check order ownership before accessing
payments or invoices. Use synthetic customer data with the sandbox BFF.
[MCP authorization](https://modelcontextprotocol.io/specification/latest/basic/authorization)

| Tool | Operation |
| --- | --- |
| `register_customer` | Register a synthetic profile and obtain its cart. Reuse the same profile for retries. |
| `get_cart`, `set_cart` | Read or replace the signed-in customer's cart with product IDs and quantities. |
| `create_order`, `list_orders` | Consume the cart to create an order, or recover the order ID after an interrupted response. |
| `start_checkout` | Create or recover the existing hosted Stripe Sandbox session for the order. |
| `get_purchase` | Read the order, payment status and completed invoice metadata. |
| `download_invoice` | Return the completed PDF as an embedded MCP resource for the owned order. |

Complete the payment on the Stripe-hosted Sandbox page. The MCP server checks
the payment and order records and waits for the completed invoice from the
fulfillment worker. [Stripe test cards](https://docs.stripe.com/testing)

## Run the purchase in Code Mode

Set `ECOMMERCE_MCP_URL` to the hosted `/mcp` endpoint and supply a delegated
Entra access token through `ECOMMERCE_MCP_ACCESS_TOKEN`. Use an active phone
UUID from the catalog. The program registers the profile, sets the cart,
creates the order, and shows the hosted payment link. After the test payment
it saves the completed invoice PDF.

```powershell
uv run --locked ecommerce-store-checkout buy --profile examples/sandbox-customer.json --phone-id <phone-uuid>
```

If checkout is interrupted, use `orders` to find the existing order ID and
`resume` to recover its checkout and invoice. A new `buy` creates a new order.

```powershell
uv run --locked ecommerce-store-checkout orders
uv run --locked ecommerce-store-checkout --output invoice.pdf resume --order-id <order-uuid>
```

## Verify

```sh
uv run --locked ruff format --check .
uv run --locked ruff check .
uv run --locked pytest
uv build --wheel
```

The HTTP tests exercise the catalog, bearer validation, order ownership, interrupted order creation and the Code Mode purchase through a controlled BFF boundary. The sandbox checkout uses the existing payment and invoice routes. GitHub Actions builds the Docker image, checks that its HTTP server starts, and scans it with Trivy. [BFF checkout contract](https://github.com/MichalBoczula/ECommerceStoreBFF/blob/master/docs/adr/0005-stripe-hosted-checkout.md)

## Publish the image

Set the `DOCKER_USER` and `DOCKER_TOKEN` repository secrets to the Docker Hub username and an access token with push access. After verification, a push to `main` publishes the tested image to `docker.io/<username>/ecommerce-store-mcp` with the commit SHA and `latest` tags. A manual run on `main` publishes the commit SHA tag.

Download the `image-reference` artifact and use its `imageReference` value to pin the image digest for deployment.

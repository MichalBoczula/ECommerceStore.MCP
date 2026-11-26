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

For a hosted instance, use its HTTPS `/mcp` URL in [Microsoft Copilot Studio](https://learn.microsoft.com/en-us/microsoft-copilot-studio/mcp-add-existing-server-to-agent) or [ChatGPT](https://developers.openai.com/plugins/deploy/connect-chatgpt), according to your workspace permissions. Account, checkout, and invoice tools will require user authentication and order ownership checks before exposure to other users.

## Verify

```sh
uv run --locked ruff format --check .
uv run --locked ruff check .
uv run --locked pytest
uv build --wheel
```

The HTTP tests discover and call MCP tools against a substitute for the BFF that follows the [Products contract pinned by BFF](https://github.com/MichalBoczula/ECommerceStoreBFF/blob/master/contracts/upstream/products.openapi.json). GitHub Actions builds the Docker image, checks that its HTTP server starts, and scans it with Trivy.

## Publish the image

Set the `DOCKER_USER` and `DOCKER_TOKEN` repository secrets to the Docker Hub username and an access token with push access. After verification, a push to `main` publishes the tested image to `docker.io/<username>/ecommerce-store-mcp` with the commit SHA and `latest` tags. A manual run on `main` publishes the commit SHA tag.

Download the `image-reference` artifact and use its `imageReference` value to pin the image digest for deployment.

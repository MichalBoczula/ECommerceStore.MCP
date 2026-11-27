import asyncio
from hmac import digest
from urllib.parse import urlsplit
from uuid import UUID

import jwt
from mcp.server.auth.provider import AccessToken
from mcp.server.auth.settings import AuthSettings
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import AnyHttpUrl

from ecommerce_store_mcp.settings import CheckoutAuth


def checkout_auth_settings(config: CheckoutAuth) -> AuthSettings:
    return AuthSettings(
        issuer_url=AnyHttpUrl(config.issuer),
        resource_server_url=config.resource_url,
        required_scopes=[config.scope],
        validate_token_resource=False,
    )


def checkout_transport_security(config: CheckoutAuth) -> TransportSecuritySettings:
    endpoint = urlsplit(str(config.resource_url))
    return TransportSecuritySettings(
        allowed_hosts=[endpoint.netloc], allowed_origins=[f"{endpoint.scheme}://{endpoint.netloc}"]
    )


class EntraCheckoutVerifier:
    def __init__(self, config: CheckoutAuth) -> None:
        self._config = config
        self._keys = jwt.PyJWKClient(
            f"https://login.microsoftonline.com/{config.tenant_id}/discovery/v2.0/keys", timeout=5
        )

    async def verify_token(self, token: str) -> AccessToken | None:
        return await asyncio.to_thread(self._verify, token)

    def _verify(self, token: str) -> AccessToken | None:
        try:
            if jwt.get_unverified_header(token).get("alg") != "RS256":
                return None
            key = self._keys.get_signing_key_from_jwt(token).key
            claims = jwt.decode(
                token,
                key,
                algorithms=["RS256"],
                issuer=self._config.issuer,
                audience=str(self._config.api_client_id),
                options={"require": ["exp", "iss", "aud", "tid", "oid", "azp", "scp"], "strict_aud": True},
            )
            tenant_id = UUID(claims["tid"])
            object_id = UUID(claims["oid"])
            client_id = UUID(claims["azp"])
            scope = claims["scp"]
            if tenant_id != self._config.tenant_id or not isinstance(scope, str) or not scope:
                return None
            scopes = [f"api://{self._config.api_client_id}/{item}" for item in scope.split()]
        except jwt.PyJWTError, ValueError, TypeError, AttributeError:
            return None

        # Bind one BFF customer to the same Entra user across authorized MCP clients.
        account_key = digest(
            self._config.identity_key.get_secret_value().encode(),
            f"ecommerce-checkout:{tenant_id}:{object_id}".encode(),
            "sha256",
        )
        return AccessToken(
            token=token,
            client_id=str(client_id),
            subject=str(UUID(bytes=account_key[:16], version=4)),
            scopes=scopes,
            expires_at=claims["exp"],
        )

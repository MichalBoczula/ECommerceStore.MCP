from typing import Annotated
from uuid import UUID

from pydantic import AnyHttpUrl, BaseModel, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class CheckoutAuth(BaseModel):
    tenant_id: UUID
    api_client_id: UUID
    resource_url: AnyHttpUrl
    identity_key: SecretStr

    @property
    def issuer(self) -> str:
        return f"https://login.microsoftonline.com/{self.tenant_id}/v2.0"

    @property
    def scope(self) -> str:
        return f"api://{self.api_client_id}/mcp.checkout"

    @field_validator("resource_url")
    @classmethod
    def validate_resource_url(cls, url: AnyHttpUrl) -> AnyHttpUrl:
        if (
            url.path != "/mcp"
            or url.username is not None
            or url.password is not None
            or url.query is not None
            or url.fragment is not None
            or (url.scheme != "https" and url.host not in {"localhost", "127.0.0.1", "[::1]"})
        ):
            raise ValueError("Use the HTTPS MCP endpoint or a loopback endpoint for local testing.")
        return url

    @field_validator("identity_key")
    @classmethod
    def validate_identity_key(cls, key: SecretStr) -> SecretStr:
        if len(key.get_secret_value().encode()) < 32:
            raise ValueError("Supply a random checkout identity key of at least 32 bytes.")
        return key


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="ECOMMERCE_", env_file=".env", env_nested_delimiter="__", extra="ignore", hide_input_in_errors=True
    )

    bff_base_url: AnyHttpUrl
    host: str = "127.0.0.1"
    port: Annotated[int, Field(ge=1, le=65535)] = 8080
    checkout_auth: CheckoutAuth | None = None

    @field_validator("bff_base_url")
    @classmethod
    def validate_bff_base_url(cls, value: AnyHttpUrl) -> AnyHttpUrl:
        if (
            value.username is not None
            or value.password is not None
            or value.query is not None
            or value.fragment is not None
        ):
            raise ValueError("Use a BFF base URL without credentials, a query, or a fragment.")
        return value

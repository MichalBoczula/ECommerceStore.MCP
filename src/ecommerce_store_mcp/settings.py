from typing import Annotated

from pydantic import AnyHttpUrl, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="ECOMMERCE_", env_file=".env", extra="ignore", hide_input_in_errors=True
    )

    bff_base_url: AnyHttpUrl
    host: str = "127.0.0.1"
    port: Annotated[int, Field(ge=1, le=65535)] = 8080

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

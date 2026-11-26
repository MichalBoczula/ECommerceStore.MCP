from decimal import Decimal
from logging import getLogger
from uuid import UUID

import httpx2
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import BaseModel, Field, JsonValue, TypeAdapter, ValidationError

logger = getLogger(__name__)


class Price(BaseModel):
    amount: Decimal
    currency: str


class PhoneSummary(BaseModel):
    id: UUID
    name: str
    brand: str
    main_photo: str = Field(validation_alias="mainPhoto")
    display_type: str = Field(validation_alias="displayType")
    screen_size_inches: float = Field(validation_alias="screenSizeInches")
    camera: str
    price: Price


class PhoneDescription(BaseModel):
    name: str
    brand: str
    description: str
    main_photo: str = Field(validation_alias="mainPhoto")
    other_photos: list[str] = Field(validation_alias="otherPhotos")


class PhoneDetails(BaseModel):
    id: UUID
    is_active: bool = Field(validation_alias="isActive")
    description: PhoneDescription = Field(validation_alias="commonDescription")
    electronic_details: dict[str, JsonValue] = Field(validation_alias="electronicDetails")
    connectivity: dict[str, JsonValue]
    satellite_navigation_systems: dict[str, JsonValue] = Field(validation_alias="satelliteNavigationSystems")
    sensors: dict[str, JsonValue]
    camera: str
    price: Price
    fingerprint: bool = Field(validation_alias="fingerPrint")
    face_id: bool = Field(validation_alias="faceId")
    description2: str
    description3: str


class PhoneList(BaseModel):
    phones: list[PhoneSummary]


_PHONE_LIST = TypeAdapter(list[PhoneSummary])
_PHONE_DETAILS = TypeAdapter(PhoneDetails)


class CatalogClient:
    def __init__(self, client: httpx2.AsyncClient) -> None:
        self._client = client

    async def list_phones(self, amount: int) -> PhoneList:
        return PhoneList(phones=await self._get("mobile-phones", _PHONE_LIST, params={"amount": amount}))

    async def get_phone(self, phone_id: UUID) -> PhoneDetails:
        return await self._get(f"mobile-phones/{phone_id}", _PHONE_DETAILS)

    async def _get[T](self, path: str, result: TypeAdapter[T], *, params: dict[str, int] | None = None) -> T:
        try:
            response = await self._client.get(path, params=params)
        except httpx2.RequestError as exc:
            logger.warning("Catalog request failed: %s", type(exc).__name__)
            raise ToolError("The store catalog is unavailable. Try again later.") from None
        if response.status_code == 404:
            raise ToolError("The requested catalog item was not found.")
        if not response.is_success:
            logger.warning("Catalog request failed: HTTP %s", response.status_code)
            raise ToolError("The store catalog is unavailable. Try again later.")
        try:
            return result.validate_json(response.content)
        except ValidationError:
            logger.warning("Catalog returned a response that did not match the contract")
            raise ToolError("The store catalog returned an invalid response.") from None

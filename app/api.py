"""Small HTTP API used to import contacts and exercise one live call at a time."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Header, HTTPException, Request, status
from pydantic import BaseModel, Field

from .config import get_settings
from .services import DomainError, create_campaign, dial_contact, import_contacts, receive_status, record_outcome

router = APIRouter()


class CampaignInput(BaseModel):
    name: str = Field(min_length=1, max_length=160)


class ContactInput(BaseModel):
    full_name: str
    phone: str
    language: str = "English"
    consent_status: Literal["granted", "service_basis", "unknown", "withdrawn"] = "unknown"
    consent_source: str | None = None


class ContactImport(BaseModel):
    contacts: list[ContactInput] = Field(min_length=1, max_length=10_000)


class OutcomeInput(BaseModel):
    attempt_id: str
    disposition: str
    details: dict[str, Any] = Field(default_factory=dict)


@router.post("/api/campaigns", status_code=status.HTTP_201_CREATED)
def create_campaign_endpoint(body: CampaignInput) -> dict[str, Any]:
    return create_campaign(body.name)


@router.post("/api/campaigns/{campaign_id}/contacts", status_code=status.HTTP_201_CREATED)
def import_contacts_endpoint(campaign_id: str, body: ContactImport) -> dict[str, Any]:
    try:
        return import_contacts(campaign_id, [contact.model_dump() for contact in body.contacts])
    except DomainError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.post("/api/contacts/{contact_id}/dial")
def dial_contact_endpoint(contact_id: str) -> dict[str, Any]:
    try:
        return dial_contact(contact_id)
    except DomainError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.post("/webhooks/exotel/status")
async def exotel_status_webhook(request: Request) -> dict[str, Any]:
    payload = await request.json() if "application/json" in request.headers.get("content-type", "") else dict(await request.form())
    try:
        return receive_status(payload)
    except DomainError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@router.post("/internal/pipecat/outcomes")
def pipecat_outcome_endpoint(body: OutcomeInput, x_internal_token: str | None = Header(default=None)) -> dict[str, bool]:
    if get_settings().internal_webhook_token and x_internal_token != get_settings().internal_webhook_token:
        raise HTTPException(status_code=401, detail="invalid internal token")
    try:
        return record_outcome(body.attempt_id, body.disposition, body.details)
    except DomainError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
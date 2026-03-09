from __future__ import annotations

import json
import os
import smtplib
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from email.message import EmailMessage
from pathlib import Path
from threading import Lock
from typing import Literal
from urllib.parse import urlencode
from urllib.request import urlopen

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, EmailStr, Field


BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

BOOKINGS_FILE = BASE_DIR / "bookings.json"
BOOKINGS_LOCK = Lock()
SLOT_START_HOUR = 6
SLOT_END_HOUR = 22  # exclusive


@dataclass
class Settings:
    business_name: str = os.getenv("BUSINESS_NAME", "Local Lifts & Deliveries")
    business_phone: str = os.getenv("BUSINESS_PHONE", "0400 000 000")
    business_email: str = os.getenv("BUSINESS_EMAIL", "")
    booking_window_days: int = int(os.getenv("BOOKING_WINDOW_DAYS", "14"))

    smtp_host: str = os.getenv("SMTP_HOST", "")
    smtp_port: int = int(os.getenv("SMTP_PORT", "587"))
    smtp_user: str = os.getenv("SMTP_USER", "")
    smtp_password: str = os.getenv("SMTP_PASSWORD", "")
    smtp_from: str = os.getenv("SMTP_FROM", "")
    google_maps_api_key: str = os.getenv("GOOGLE_MAPS_API_KEY", "")


settings = Settings()

ServiceType = Literal["gold_coast", "direct", "gc_to_brisbane"]

PRICE_MAP: dict[ServiceType, int] = {
    "gold_coast": 30,
    "direct": 60,
    "gc_to_brisbane": 60,
}

SERVICE_LABELS: dict[ServiceType, str] = {
    "gold_coast": "Anywhere in Gold Coast",
    "direct": "Direct (same day)",
    "gc_to_brisbane": "Gold Coast to Brisbane (same day)",
}


class BookingRequest(BaseModel):
    full_name: str = Field(..., min_length=2, max_length=120)
    email: EmailStr
    phone: str = Field(..., min_length=6, max_length=30)
    service_type: ServiceType
    pickup_location: str = Field(..., min_length=3, max_length=250)
    dropoff_location: str = Field(..., min_length=3, max_length=250)
    pickup_place_id: str | None = Field(default=None, max_length=200)
    dropoff_place_id: str | None = Field(default=None, max_length=200)
    preferred_time: datetime
    notes: str = Field(default="", max_length=1000)


class BookingResponse(BaseModel):
    success: bool
    message: str
    price_aud: int
    booked_slot: str


class AvailabilitySlot(BaseModel):
    time: str
    label: str
    status: Literal["available", "booked", "past"]


class AvailabilityDay(BaseModel):
    date: str
    label: str
    slots: list[AvailabilitySlot]


class AvailabilityResponse(BaseModel):
    days: list[AvailabilityDay]


@dataclass
class StoredBooking:
    created_at: str
    full_name: str
    email: str
    phone: str
    service_type: str
    pickup_location: str
    dropoff_location: str
    preferred_time: str
    notes: str


def _load_bookings() -> list[StoredBooking]:
    if not BOOKINGS_FILE.exists():
        return []
    raw = BOOKINGS_FILE.read_text(encoding="utf-8")
    if not raw.strip():
        return []
    data = json.loads(raw)
    out: list[StoredBooking] = []
    for item in data:
        out.append(StoredBooking(**item))
    return out


def _save_bookings(bookings: list[StoredBooking]) -> None:
    BOOKINGS_FILE.write_text(json.dumps([asdict(b) for b in bookings], indent=2), encoding="utf-8")


def _slot_key(dt: datetime) -> str:
    return dt.replace(minute=0, second=0, microsecond=0).isoformat(timespec="minutes")


def _is_valid_slot(dt: datetime) -> bool:
    now = datetime.now()
    if dt.minute != 0:
        return False
    if dt.hour < SLOT_START_HOUR or dt.hour >= SLOT_END_HOUR:
        return False
    if dt < now:
        return False
    max_day = now + timedelta(days=settings.booking_window_days)
    if dt > max_day:
        return False
    return True


def _booked_slot_keys(bookings: list[StoredBooking]) -> set[str]:
    return {b.preferred_time for b in bookings}


def _assert_email_ready() -> None:
    required = {
        "BUSINESS_EMAIL": settings.business_email,
        "SMTP_HOST": settings.smtp_host,
        "SMTP_USER": settings.smtp_user,
        "SMTP_PASSWORD": settings.smtp_password,
        "SMTP_FROM": settings.smtp_from,
    }
    missing = [key for key, value in required.items() if not value]
    if missing:
        joined = ", ".join(missing)
        raise RuntimeError(f"Email is not configured. Missing: {joined}")


def _send_email(to_address: str, subject: str, body: str) -> None:
    message = EmailMessage()
    message["From"] = settings.smtp_from
    message["To"] = to_address
    message["Subject"] = subject
    message.set_content(body)

    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=30) as smtp:
        smtp.starttls()
        smtp.login(settings.smtp_user, settings.smtp_password)
        smtp.send_message(message)


def _build_email_body(booking: BookingRequest, price: int) -> str:
    service_label = SERVICE_LABELS[booking.service_type]
    return (
        f"Booking confirmation - {settings.business_name}\n\n"
        f"Customer: {booking.full_name}\n"
        f"Customer phone: {booking.phone}\n"
        f"Customer email: {booking.email}\n"
        f"Service: {service_label}\n"
        f"Price: AUD ${price}\n"
        f"Pickup: {booking.pickup_location}\n"
        f"Dropoff: {booking.dropoff_location}\n"
        f"Booked slot: {booking.preferred_time.strftime('%Y-%m-%d %H:%M')}\n"
        f"Notes: {booking.notes or '-'}\n\n"
        f"Business phone: {settings.business_phone}\n"
        f"Business email: {settings.business_email}\n"
    )


def _google_json(url: str) -> dict:
    with urlopen(url, timeout=20) as response:
        return json.loads(response.read().decode("utf-8"))


def _normalize_address(address: str, place_id: str | None) -> str:
    if not settings.google_maps_api_key:
        return address

    if place_id:
        query = urlencode({"place_id": place_id, "key": settings.google_maps_api_key})
        data = _google_json(f"https://maps.googleapis.com/maps/api/place/details/json?{query}")
        status = data.get("status")
        result = data.get("result") or {}
        formatted = result.get("formatted_address")
        if status == "OK" and formatted:
            return formatted
        raise HTTPException(status_code=400, detail=f"Invalid selected address (place details status: {status}).")

    query = urlencode({"address": address, "key": settings.google_maps_api_key})
    data = _google_json(f"https://maps.googleapis.com/maps/api/geocode/json?{query}")
    status = data.get("status")
    results = data.get("results") or []
    if status == "OK" and results:
        formatted = results[0].get("formatted_address")
        if formatted:
            return formatted
    raise HTTPException(status_code=400, detail=f"Address could not be confirmed by Google Maps (status: {status}).")


def _build_availability(days: int, bookings: list[StoredBooking]) -> list[AvailabilityDay]:
    now = datetime.now()
    today = now.date()
    booked = _booked_slot_keys(bookings)
    out: list[AvailabilityDay] = []

    for offset in range(days):
        current_date = today + timedelta(days=offset)
        day_slots: list[AvailabilitySlot] = []
        for hour in range(SLOT_START_HOUR, SLOT_END_HOUR):
            slot_dt = datetime(
                year=current_date.year,
                month=current_date.month,
                day=current_date.day,
                hour=hour,
                minute=0,
            )
            key = _slot_key(slot_dt)
            if slot_dt < now:
                status = "past"
            elif key in booked:
                status = "booked"
            else:
                status = "available"
            day_slots.append(
                AvailabilitySlot(
                    time=key,
                    label=slot_dt.strftime("%I:%M %p").lstrip("0"),
                    status=status,
                )
            )
        out.append(
            AvailabilityDay(
                date=current_date.isoformat(),
                label=current_date.strftime("%a %d %b"),
                slots=day_slots,
            )
        )
    return out


app = FastAPI(title="Local Booking Website")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


@app.get("/", response_class=HTMLResponse)
def home() -> HTMLResponse:
    html = (BASE_DIR / "templates" / "index.html").read_text(encoding="utf-8")
    return HTMLResponse(html)


@app.get("/api/availability", response_model=AvailabilityResponse)
def availability(days: int = Query(default=14, ge=7, le=14)) -> AvailabilityResponse:
    with BOOKINGS_LOCK:
        bookings = _load_bookings()
    return AvailabilityResponse(days=_build_availability(days, bookings))


@app.post("/api/bookings", response_model=BookingResponse)
def create_booking(booking: BookingRequest) -> BookingResponse:
    try:
        _assert_email_ready()
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    requested = booking.preferred_time.replace(second=0, microsecond=0)
    if not _is_valid_slot(requested):
        raise HTTPException(status_code=400, detail="Selected slot is invalid. Choose a valid calendar slot.")
    key = _slot_key(requested)

    pickup_confirmed = _normalize_address(booking.pickup_location, booking.pickup_place_id)
    dropoff_confirmed = _normalize_address(booking.dropoff_location, booking.dropoff_place_id)
    booking = booking.model_copy(update={"pickup_location": pickup_confirmed, "dropoff_location": dropoff_confirmed})

    with BOOKINGS_LOCK:
        bookings = _load_bookings()
        if key in _booked_slot_keys(bookings):
            raise HTTPException(status_code=409, detail="Selected slot is already booked. Please choose another slot.")

        stored = StoredBooking(
            created_at=datetime.now().isoformat(timespec="seconds"),
            full_name=booking.full_name,
            email=str(booking.email),
            phone=booking.phone,
            service_type=booking.service_type,
            pickup_location=booking.pickup_location,
            dropoff_location=booking.dropoff_location,
            preferred_time=key,
            notes=booking.notes,
        )
        bookings.append(stored)
        _save_bookings(bookings)

    price = PRICE_MAP[booking.service_type]
    body = _build_email_body(booking, price)

    try:
        _send_email(
            str(booking.email),
            f"Booking Confirmed - {settings.business_name} - AUD ${price}",
            body,
        )
        _send_email(
            settings.business_email,
            f"New Booking - {booking.full_name} - AUD ${price}",
            body,
        )
    except Exception as exc:
        with BOOKINGS_LOCK:
            rollback = [b for b in _load_bookings() if not (b.preferred_time == key and b.email == str(booking.email))]
            _save_bookings(rollback)
        raise HTTPException(status_code=500, detail=f"Failed to send email: {exc}") from exc

    return BookingResponse(
        success=True,
        message="Booking received. Confirmation email sent.",
        price_aud=price,
        booked_slot=key,
    )


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/public-config")
def public_config() -> dict[str, object]:
    return {
        "business_name": settings.business_name,
        "business_phone": settings.business_phone,
        "booking_window_days": settings.booking_window_days,
        "google_maps_api_key": settings.google_maps_api_key,
        "pricing": [
            {"key": key, "label": SERVICE_LABELS[key], "price_aud": PRICE_MAP[key]}
            for key in PRICE_MAP
        ],
    }

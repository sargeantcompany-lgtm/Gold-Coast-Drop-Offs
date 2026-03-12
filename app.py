from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
import socket
import smtplib
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from email.message import EmailMessage
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email import encoders as email_encoders
from pathlib import Path
from threading import Lock
from typing import Literal
from urllib.parse import urlencode
from urllib.request import urlopen

import stripe
from dotenv import load_dotenv
from fastapi import Cookie, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from itsdangerous import URLSafeTimedSerializer
from pydantic import BaseModel, EmailStr, Field


BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")
logger = logging.getLogger("local_booking_site")

BOOKINGS_FILE = BASE_DIR / "bookings.json"
BOOKINGS_LOCK = Lock()
SLOT_START_HOUR = 6
SLOT_END_HOUR = 22  # exclusive
SLOT_INTERVAL_MINUTES = 15


@dataclass
class Settings:
    business_name: str = os.getenv("BUSINESS_NAME", "Local Lifts & Deliveries")
    business_phone: str = os.getenv("BUSINESS_PHONE", "0400 000 000")
    business_email: str = os.getenv("BUSINESS_EMAIL", "")
    booking_window_days: int = int(os.getenv("BOOKING_WINDOW_DAYS", "7"))

    smtp_host: str = os.getenv("SMTP_HOST", "")
    smtp_port: int = int(os.getenv("SMTP_PORT", "587"))
    smtp_user: str = os.getenv("SMTP_USER", "")
    smtp_password: str = os.getenv("SMTP_PASSWORD", "")
    smtp_from: str = os.getenv("SMTP_FROM", "")
    google_maps_api_key: str = os.getenv("GOOGLE_MAPS_API_KEY", "")
    driver_password: str = os.getenv("DRIVER_PASSWORD", "")
    secret_key: str = os.getenv("SECRET_KEY", "dev-secret-change-me")
    stripe_secret_key: str = os.getenv("STRIPE_SECRET_KEY", "")
    stripe_publishable_key: str = os.getenv("STRIPE_PUBLISHABLE_KEY", "")
    stripe_webhook_secret: str = os.getenv("STRIPE_WEBHOOK_SECRET", "")


settings = Settings()
signer = URLSafeTimedSerializer(settings.secret_key)
if settings.stripe_secret_key:
    stripe.api_key = settings.stripe_secret_key

ServiceType = Literal["gold_coast", "direct", "gc_to_brisbane", "test_trip"]

PRICE_MAP: dict[ServiceType, int] = {
    "gold_coast": 3000,
    "direct": 5000,
    "gc_to_brisbane": 6000,
    "test_trip": 50,
}

SERVICE_LABELS: dict[ServiceType, str] = {
    "gold_coast": "Same Day Within 4 Hours",
    "direct": "Direct in 1 Hour",
    "gc_to_brisbane": "Gold Coast to Brisbane 4 Hour Service",
    "test_trip": "Test trip - Stripe/email test",
}


class BookingRequest(BaseModel):
    full_name: str = Field(..., min_length=2, max_length=120)
    email: EmailStr
    phone: str = Field(..., min_length=6, max_length=30)
    whatsapp_number: str = Field(default="", max_length=30)
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
    price_aud: float
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
    whatsapp_number: str
    service_type: str
    pickup_location: str
    dropoff_location: str
    preferred_time: str
    notes: str
    booking_id: str = ""
    status: str = "pending"  # pending, picked_up, delivered
    picked_up_at: str = ""
    delivered_at: str = ""
    payment_status: str = "paid"  # paid (default for old bookings), unpaid
    stripe_session_id: str = ""
    confirmation_sent_at: str = ""


def _make_booking_id(item: dict) -> str:
    key = (item.get("created_at", "") + item.get("email", "")).encode()
    return hashlib.md5(key).hexdigest()[:12]


def _load_bookings() -> list[StoredBooking]:
    if not BOOKINGS_FILE.exists():
        return []
    raw = BOOKINGS_FILE.read_text(encoding="utf-8")
    if not raw.strip():
        return []
    data = json.loads(raw)
    out: list[StoredBooking] = []
    for item in data:
        item.setdefault("booking_id", _make_booking_id(item))
        item.setdefault("status", "pending")
        item.setdefault("picked_up_at", "")
        item.setdefault("delivered_at", "")
        item.setdefault("payment_status", "paid")
        item.setdefault("stripe_session_id", "")
        item.setdefault("whatsapp_number", "")
        item.setdefault("confirmation_sent_at", "")
        out.append(StoredBooking(**item))
    return out


def _save_bookings(bookings: list[StoredBooking]) -> None:
    BOOKINGS_FILE.write_text(json.dumps([asdict(b) for b in bookings], indent=2), encoding="utf-8")


def _slot_key(dt: datetime) -> str:
    return dt.replace(second=0, microsecond=0).isoformat(timespec="minutes")


def _is_valid_slot(dt: datetime) -> bool:
    now = datetime.now()
    if dt.minute % SLOT_INTERVAL_MINUTES != 0:
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
    return {b.preferred_time for b in bookings if b.payment_status == "paid"}


def _format_aud(cents: int) -> str:
    return f"{cents / 100:.2f}"


def _format_contact_line(phone: str, whatsapp_number: str) -> str:
    if whatsapp_number:
        return f"Customer phone: {phone}\nCustomer WhatsApp: {whatsapp_number}\n"
    return f"Customer phone: {phone}\n"


def _assert_email_ready() -> None:
    required = {"BUSINESS_EMAIL": settings.business_email}
    required["SMTP_HOST"] = settings.smtp_host
    required["SMTP_USER"] = settings.smtp_user
    required["SMTP_PASSWORD"] = settings.smtp_password
    required["SMTP_FROM"] = settings.smtp_from
    missing = [key for key, value in required.items() if not value]
    if missing:
        joined = ", ".join(missing)
        raise RuntimeError(f"Email is not configured. Missing: {joined}")


def _open_smtp_connection() -> smtplib.SMTP:
    def _connect_ipv4(host: str, port: int, timeout: float) -> socket.socket:
        last_error: OSError | None = None
        for family, socktype, proto, _, sockaddr in socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_STREAM):
            sock: socket.socket | None = None
            try:
                sock = socket.socket(family, socktype, proto)
                sock.settimeout(timeout)
                sock.connect(sockaddr)
                return sock
            except OSError as exc:
                last_error = exc
                if sock is not None:
                    sock.close()
        if last_error is not None:
            raise last_error
        raise OSError(f"Could not resolve IPv4 address for SMTP host {host}")

    if settings.smtp_port == 465:
        smtp = smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, timeout=30)
        smtp.ehlo()
    else:
        smtp = smtplib.SMTP(timeout=30)
        smtp.sock = _connect_ipv4(settings.smtp_host, settings.smtp_port, 30)
        smtp.file = None
        smtp.helo_resp = None
        smtp.ehlo_resp = None
        smtp.esmtp_features = {}
        smtp.does_esmtp = False
        smtp.ehlo()
        smtp.starttls()
        smtp.ehlo()
    smtp.login(settings.smtp_user, settings.smtp_password)
    return smtp


def _send_email(to_address: str, subject: str, body: str) -> None:
    message = EmailMessage()
    message["From"] = settings.smtp_from
    message["To"] = to_address
    message["Subject"] = subject
    message.set_content(body)

    with _open_smtp_connection() as smtp:
        smtp.send_message(message)


def _send_email_with_photo(to_address: str, subject: str, body: str, photo_data: bytes, photo_filename: str, content_type: str) -> None:
    msg = MIMEMultipart()
    msg["From"] = settings.smtp_from
    msg["To"] = to_address
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain"))
    maintype, subtype = (content_type.split("/", 1) if "/" in content_type else ("image", "jpeg"))
    part = MIMEBase(maintype, subtype)
    part.set_payload(photo_data)
    email_encoders.encode_base64(part)
    part.add_header("Content-Disposition", f'attachment; filename="{photo_filename}"')
    msg.attach(part)
    with _open_smtp_connection() as smtp:
        smtp.sendmail(settings.smtp_from, to_address, msg.as_string())


def _make_session_token() -> str:
    return signer.dumps("driver")


def _verify_session(token: str) -> bool:
    try:
        signer.loads(token, max_age=86400 * 14)
        return True
    except Exception:
        return False


def _build_email_body(booking: BookingRequest, price_cents: int) -> str:
    service_label = SERVICE_LABELS[booking.service_type]
    return (
        f"Booking confirmation - {settings.business_name}\n\n"
        f"Customer: {booking.full_name}\n"
        f"{_format_contact_line(booking.phone, booking.whatsapp_number)}"
        f"Customer email: {booking.email}\n"
        f"Service: {service_label}\n"
        f"Price: AUD ${_format_aud(price_cents)}\n"
        f"Pickup: {booking.pickup_location}\n"
        f"Dropoff: {booking.dropoff_location}\n"
        f"Booking reference: pending payment confirmation\n"
        f"Booked slot: {booking.preferred_time.strftime('%Y-%m-%d %H:%M')}\n"
        f"Notes: {booking.notes or '-'}\n\n"
        f"Business phone: {settings.business_phone}\n"
        f"Business email: {settings.business_email}\n"
    )


def _build_paid_email_body(booking: StoredBooking) -> str:
    price_cents = PRICE_MAP.get(booking.service_type, 0)
    return (
        f"Booking confirmed - {settings.business_name}\n\n"
        f"Booking reference: {booking.booking_id}\n"
        f"Customer: {booking.full_name}\n"
        f"{_format_contact_line(booking.phone, booking.whatsapp_number)}"
        f"Customer email: {booking.email}\n"
        f"Service: {SERVICE_LABELS.get(booking.service_type, booking.service_type)}\n"
        f"Price: AUD ${_format_aud(price_cents)} (paid)\n"
        f"Pickup: {booking.pickup_location}\n"
        f"Dropoff: {booking.dropoff_location}\n"
        f"Booked slot: {booking.preferred_time.replace('T', ' ')}\n"
        f"Notes: {booking.notes or '-'}\n\n"
        f"We have your booking and will contact you if anything changes.\n"
        f"Business phone: {settings.business_phone}\n"
        f"Business email: {settings.business_email}\n"
    )


def _mark_confirmation_state(booking_id: str, state: str) -> StoredBooking:
    with BOOKINGS_LOCK:
        bookings = _load_bookings()
        booking = next((b for b in bookings if b.booking_id == booking_id), None)
        if not booking:
            raise LookupError("Booking not found.")
        booking.confirmation_sent_at = state
        _save_bookings(bookings)
        return StoredBooking(**asdict(booking))


def _finalize_paid_booking(session_id: str, booking_id: str | None = None) -> tuple[StoredBooking, str | None]:
    booking_snapshot: StoredBooking | None = None
    should_send = False

    with BOOKINGS_LOCK:
        bookings = _load_bookings()
        booking = next((b for b in bookings if b.stripe_session_id == session_id), None)
        if not booking and booking_id:
            booking = next((b for b in bookings if b.booking_id == booking_id), None)
        if not booking:
            raise LookupError("Booking not found.")

        booking.stripe_session_id = session_id
        booking.payment_status = "paid"
        if booking.confirmation_sent_at not in {"", "__sending__"}:
            _save_bookings(bookings)
            booking_snapshot = StoredBooking(**asdict(booking))
            return booking_snapshot, None

        should_send = True
        booking.confirmation_sent_at = "__sending__"
        _save_bookings(bookings)
        booking_snapshot = StoredBooking(**asdict(booking))

    if not should_send:
        return booking_snapshot, None

    try:
        _assert_email_ready()
        body = _build_paid_email_body(booking_snapshot)
        _send_email(
            booking_snapshot.email,
            f"Booking Confirmed - {settings.business_name} - Ref {booking_snapshot.booking_id}",
            body,
        )
        if settings.business_email:
            _send_email(
                settings.business_email,
                f"New Paid Booking - {booking_snapshot.full_name} - Ref {booking_snapshot.booking_id}",
                body,
            )
    except Exception as exc:
        logger.exception("Failed to send paid booking confirmation email", extra={"booking_id": booking_snapshot.booking_id})
        _mark_confirmation_state(booking_snapshot.booking_id, "")
        return booking_snapshot, str(exc)

    sent_at = datetime.now().isoformat(timespec="seconds")
    booking_snapshot = _mark_confirmation_state(booking_snapshot.booking_id, sent_at)
    return booking_snapshot, None


def _google_json(url: str) -> dict:
    with urlopen(url, timeout=20) as response:
        return json.loads(response.read().decode("utf-8"))


def _google_maps_validation_enabled() -> bool:
    key = settings.google_maps_api_key.strip()
    if not key:
        return False
    if key.startswith("REPLACE_WITH_"):
        return False
    return True


def _normalize_address(address: str, place_id: str | None) -> str:
    if not _google_maps_validation_enabled():
        return address

    if place_id:
        query = urlencode({"place_id": place_id, "key": settings.google_maps_api_key})
        data = _google_json(f"https://maps.googleapis.com/maps/api/place/details/json?{query}")
        status = data.get("status")
        result = data.get("result") or {}
        formatted = result.get("formatted_address")
        if status == "OK" and formatted:
            return formatted
        if status == "REQUEST_DENIED":
            return address
        raise HTTPException(status_code=400, detail=f"Invalid selected address (place details status: {status}).")

    query = urlencode({"address": address, "key": settings.google_maps_api_key})
    data = _google_json(f"https://maps.googleapis.com/maps/api/geocode/json?{query}")
    status = data.get("status")
    results = data.get("results") or []
    if status == "OK" and results:
        formatted = results[0].get("formatted_address")
        if formatted:
            return formatted
    if status == "REQUEST_DENIED":
        return address
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
            for minute in range(0, 60, SLOT_INTERVAL_MINUTES):
                slot_dt = datetime(
                    year=current_date.year,
                    month=current_date.month,
                    day=current_date.day,
                    hour=hour,
                    minute=minute,
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


@app.get("/login", response_class=HTMLResponse)
def login_page() -> HTMLResponse:
    html = (BASE_DIR / "templates" / "login.html").read_text(encoding="utf-8")
    return HTMLResponse(html)


@app.post("/login")
def do_login(password: str = Form(...)):
    if not settings.driver_password:
        raise HTTPException(status_code=500, detail="Driver password not configured.")
    if not secrets.compare_digest(password, settings.driver_password):
        return RedirectResponse("/login?error=1", status_code=302)
    token = _make_session_token()
    response = RedirectResponse("/driver", status_code=302)
    response.set_cookie("driver_session", token, httponly=True, samesite="lax", max_age=86400 * 14)
    return response


@app.get("/logout")
def logout():
    response = RedirectResponse("/login", status_code=302)
    response.delete_cookie("driver_session")
    return response


@app.get("/driver", response_class=HTMLResponse)
def driver_dashboard(driver_session: str | None = Cookie(default=None)):
    if not driver_session or not _verify_session(driver_session):
        return RedirectResponse("/login", status_code=302)
    html = (BASE_DIR / "templates" / "driver.html").read_text(encoding="utf-8")
    return HTMLResponse(html)


@app.get("/api/driver/bookings")
def driver_bookings(driver_session: str | None = Cookie(default=None)):
    if not driver_session or not _verify_session(driver_session):
        raise HTTPException(status_code=401, detail="Not authenticated.")
    with BOOKINGS_LOCK:
        bookings = _load_bookings()
    return {"bookings": [asdict(b) for b in bookings]}


@app.post("/api/driver/pickup/{booking_id}")
async def mark_pickup(
    booking_id: str,
    photo: UploadFile = File(...),
    driver_session: str | None = Cookie(default=None),
):
    if not driver_session or not _verify_session(driver_session):
        raise HTTPException(status_code=401, detail="Not authenticated.")
    photo_data = await photo.read()
    now = datetime.now()
    now_str = now.isoformat(timespec="seconds")
    with BOOKINGS_LOCK:
        bookings = _load_bookings()
        booking = next((b for b in bookings if b.booking_id == booking_id), None)
        if not booking:
            raise HTTPException(status_code=404, detail="Booking not found.")
        if booking.status != "pending":
            raise HTTPException(status_code=400, detail="Booking already picked up.")
        booking.status = "picked_up"
        booking.picked_up_at = now_str
        _save_bookings(bookings)
    time_str = now.strftime("%I:%M %p")
    date_str = now.strftime("%a %d %b %Y")
    body = (
        f"Hi {booking.full_name},\n\n"
        f"Your item has been picked up at {time_str} on {date_str}.\n"
        f"From: {booking.pickup_location}\n"
        f"To: {booking.dropoff_location}\n\n"
        f"Photo proof of pickup is attached.\n\n"
        f"Questions? Call us: {settings.business_phone}\n"
        f"{settings.business_name}"
    )
    try:
        _send_email_with_photo(
            booking.email,
            f"Your item has been picked up — {settings.business_name}",
            body, photo_data, f"pickup_{booking_id}.jpg",
            photo.content_type or "image/jpeg",
        )
    except Exception as exc:
        return {"success": True, "picked_up_at": now_str, "email_warning": str(exc)}
    return {"success": True, "picked_up_at": now_str}


@app.post("/api/driver/dropoff/{booking_id}")
async def mark_dropoff(
    booking_id: str,
    photo: UploadFile = File(...),
    driver_session: str | None = Cookie(default=None),
):
    if not driver_session or not _verify_session(driver_session):
        raise HTTPException(status_code=401, detail="Not authenticated.")
    photo_data = await photo.read()
    now = datetime.now()
    now_str = now.isoformat(timespec="seconds")
    duration_str = ""
    with BOOKINGS_LOCK:
        bookings = _load_bookings()
        booking = next((b for b in bookings if b.booking_id == booking_id), None)
        if not booking:
            raise HTTPException(status_code=404, detail="Booking not found.")
        if booking.status == "delivered":
            raise HTTPException(status_code=400, detail="Booking already delivered.")
        booking.status = "delivered"
        booking.delivered_at = now_str
        if booking.picked_up_at:
            try:
                pickup_dt = datetime.fromisoformat(booking.picked_up_at)
                delta = now - pickup_dt
                total_minutes = int(delta.total_seconds() / 60)
                hours, mins = divmod(total_minutes, 60)
                duration_str = f"{hours}h {mins}m" if hours else f"{total_minutes} min"
            except Exception:
                pass
        _save_bookings(bookings)
    time_str = now.strftime("%I:%M %p")
    date_str = now.strftime("%a %d %b %Y")
    body = (
        f"Hi {booking.full_name},\n\n"
        f"Your item has been delivered at {time_str} on {date_str}.\n"
        f"From: {booking.pickup_location}\n"
        f"To: {booking.dropoff_location}\n"
        + (f"Trip duration: {duration_str}\n" if duration_str else "")
        + f"\nPhoto proof of delivery is attached.\n\n"
        f"Thank you for using {settings.business_name}!\n"
        f"Questions? Call us: {settings.business_phone}"
    )
    try:
        _send_email_with_photo(
            booking.email,
            f"Your item has been delivered — {settings.business_name}",
            body, photo_data, f"delivery_{booking_id}.jpg",
            photo.content_type or "image/jpeg",
        )
    except Exception as exc:
        return {"success": True, "delivered_at": now_str, "duration": duration_str, "email_warning": str(exc)}
    return {"success": True, "delivered_at": now_str, "duration": duration_str}


@app.get("/", response_class=HTMLResponse)
def home() -> HTMLResponse:
    html = (BASE_DIR / "templates" / "index.html").read_text(encoding="utf-8")
    return HTMLResponse(html)


@app.get("/api/availability", response_model=AvailabilityResponse)
def availability(days: int = Query(default=7, ge=7, le=7)) -> AvailabilityResponse:
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

        booking_id = hashlib.md5(f"{datetime.now().isoformat()}{booking.email}".encode()).hexdigest()[:12]
        stored = StoredBooking(
            created_at=datetime.now().isoformat(timespec="seconds"),
            full_name=booking.full_name,
            email=str(booking.email),
            phone=booking.phone,
            whatsapp_number=booking.whatsapp_number,
            service_type=booking.service_type,
            pickup_location=booking.pickup_location,
            dropoff_location=booking.dropoff_location,
            preferred_time=key,
            notes=booking.notes,
            booking_id=booking_id,
        )
        bookings.append(stored)
        _save_bookings(bookings)

    price_cents = PRICE_MAP[booking.service_type]
    body = _build_email_body(booking, price_cents)

    try:
        _send_email(
            str(booking.email),
            f"Booking Confirmed - {settings.business_name} - AUD ${_format_aud(price_cents)}",
            body,
        )
        _send_email(
            settings.business_email,
            f"New Booking - {booking.full_name} - AUD ${_format_aud(price_cents)}",
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
        price_aud=price_cents / 100,
        booked_slot=key,
    )


@app.post("/api/create-checkout")
async def create_checkout(booking: BookingRequest, request: Request):
    if not settings.stripe_secret_key:
        raise HTTPException(status_code=500, detail="Payment not configured.")
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

        booking_id = hashlib.md5(f"{datetime.now().isoformat()}{booking.email}".encode()).hexdigest()[:12]
        stored = StoredBooking(
            created_at=datetime.now().isoformat(timespec="seconds"),
            full_name=booking.full_name,
            email=str(booking.email),
            phone=booking.phone,
            whatsapp_number=booking.whatsapp_number,
            service_type=booking.service_type,
            pickup_location=booking.pickup_location,
            dropoff_location=booking.dropoff_location,
            preferred_time=key,
            notes=booking.notes,
            booking_id=booking_id,
            payment_status="unpaid",
        )
        bookings.append(stored)
        _save_bookings(bookings)

    price_cents = PRICE_MAP[booking.service_type]
    service_label = SERVICE_LABELS[booking.service_type]
    base_url = str(request.base_url).rstrip("/")

    try:
        session = stripe.checkout.Session.create(
            payment_method_types=["card"],
            line_items=[{
                "price_data": {
                    "currency": "aud",
                    "product_data": {
                        "name": service_label,
                        "description": f"{booking.pickup_location} → {booking.dropoff_location}",
                    },
                    "unit_amount": price_cents,
                },
                "quantity": 1,
            }],
            mode="payment",
            client_reference_id=booking_id,
            customer_email=str(booking.email),
            success_url=f"{base_url}/booking-success?session_id={{CHECKOUT_SESSION_ID}}",
            cancel_url=f"{base_url}/booking-cancelled?booking_id={booking_id}",
            metadata={"booking_id": booking_id},
        )
    except Exception as exc:
        # Remove the unpaid booking if Stripe session creation failed
        with BOOKINGS_LOCK:
            rollback = [b for b in _load_bookings() if b.booking_id != booking_id]
            _save_bookings(rollback)
        raise HTTPException(status_code=500, detail=f"Payment setup failed: {exc}") from exc

    # Store the stripe session id
    with BOOKINGS_LOCK:
        bookings = _load_bookings()
        for b in bookings:
            if b.booking_id == booking_id:
                b.stripe_session_id = session.id
        _save_bookings(bookings)

    return {"checkout_url": session.url}


@app.get("/booking-success", response_class=HTMLResponse)
def booking_success(session_id: str):
    if not settings.stripe_secret_key:
        raise HTTPException(status_code=500, detail="Payment not configured.")
    try:
        session = stripe.checkout.Session.retrieve(session_id)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Could not verify payment: {exc}")

    if session.payment_status != "paid":
        return HTMLResponse(_success_page("Payment pending", "Your payment is still processing. You will receive a confirmation email shortly."))

    try:
        booking, email_error = _finalize_paid_booking(
            session_id,
            (session.metadata or {}).get("booking_id"),
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    slot_label = booking.preferred_time.replace("T", " ")
    message = f"Your booking is confirmed for {slot_label}. Reference: {booking.booking_id}. We'll be in touch soon."
    if email_error:
        logger.error("Payment succeeded but confirmation email failed", extra={"booking_id": booking.booking_id, "error": email_error})
        contact = settings.business_email or settings.business_phone or settings.business_name
        message = (
            f"Your booking is confirmed for {slot_label}. Reference: {booking.booking_id}. "
            f"Confirmation email could not be sent automatically. Please contact {contact}."
        )
    return HTMLResponse(_success_page(
        "Payment successful!",
        message,
    ))

    with BOOKINGS_LOCK:
        bookings = _load_bookings()
        booking = next((b for b in bookings if b.stripe_session_id == session_id), None)
        if not booking:
            raise HTTPException(status_code=404, detail="Booking not found.")
        if booking.payment_status != "paid":
            booking.payment_status = "paid"
            _save_bookings(bookings)
            # Send confirmation emails
            price_cents = PRICE_MAP.get(booking.service_type, 0)
            price = _format_aud(price_cents)
            try:
                from pydantic import TypeAdapter
                email_validator = TypeAdapter(EmailStr)
                email_validator.validate_python(booking.email)
                body = (
                    f"Booking confirmation — {settings.business_name}\n\n"
                    f"Customer: {booking.full_name}\n"
                    f"Service: {SERVICE_LABELS.get(booking.service_type, booking.service_type)}\n"
                    f"Price: AUD ${_format_aud(price_cents)} (paid)\n"
                    f"Pickup: {booking.pickup_location}\n"
                    f"Dropoff: {booking.dropoff_location}\n"
                    f"Booked slot: {booking.preferred_time}\n"
                    f"Notes: {booking.notes or '-'}\n\n"
                    f"Business phone: {settings.business_phone}\n"
                )
                if settings.smtp_host:
                    _send_email(booking.email, f"Booking Confirmed — {settings.business_name}", body)
                    if settings.business_email:
                        _send_email(settings.business_email, f"New Paid Booking — {booking.full_name} — AUD ${price}", body)
            except Exception:
                pass

    slot_label = booking.preferred_time.replace("T", " ") if booking else ""
    return HTMLResponse(_success_page(
        "Payment successful!",
        f"Your booking is confirmed for {slot_label}. A confirmation email has been sent to {booking.email}.",
    ))


@app.post("/api/stripe-webhook")
async def stripe_webhook(request: Request):
    if not settings.stripe_webhook_secret:
        raise HTTPException(status_code=500, detail="Stripe webhook secret not configured.")

    payload = await request.body()
    signature = request.headers.get("stripe-signature", "")
    try:
        event = stripe.Webhook.construct_event(payload=payload, sig_header=signature, secret=settings.stripe_webhook_secret)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid webhook payload: {exc}") from exc
    except stripe.error.SignatureVerificationError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid webhook signature: {exc}") from exc

    if event["type"] in {"checkout.session.completed", "checkout.session.async_payment_succeeded"}:
        session = event["data"]["object"]
        if session.get("payment_status") == "paid":
            booking_id = (session.get("metadata") or {}).get("booking_id") or session.get("client_reference_id")
            try:
                _finalize_paid_booking(session["id"], booking_id)
            except LookupError:
                pass

    return {"received": True}


@app.get("/booking-cancelled", response_class=HTMLResponse)
def booking_cancelled(booking_id: str | None = None):
    if booking_id:
        with BOOKINGS_LOCK:
            bookings = _load_bookings()
            remaining = [b for b in bookings if not (b.booking_id == booking_id and b.payment_status == "unpaid")]
            _save_bookings(remaining)
    return HTMLResponse(_success_page(
        "Payment cancelled",
        "No charge was made. Your slot has been released — you can go back and try again.",
        is_cancel=True,
    ))


def _success_page(title: str, message: str, is_cancel: bool = False) -> str:
    color = "#e91e8c" if not is_cancel else "#607691"
    return f"""<!doctype html><html lang="en"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title>
<link href="https://fonts.googleapis.com/css2?family=Manrope:wght@400;700;800&display=swap" rel="stylesheet">
<style>
  body{{margin:0;font-family:Manrope,sans-serif;background:linear-gradient(135deg,#fdf0f8,#e9f4ff);min-height:100vh;display:flex;align-items:center;justify-content:center}}
  .card{{background:#fff;border-radius:20px;padding:40px 32px;max-width:480px;width:90%;text-align:center;box-shadow:0 20px 48px rgba(8,36,68,.12)}}
  .icon{{font-size:56px;margin-bottom:16px}}
  h1{{margin:0 0 12px;font-size:26px;color:{color}}}
  p{{color:#51657f;line-height:1.6;margin:0 0 24px}}
  a{{display:inline-block;padding:12px 28px;background:linear-gradient(120deg,#0a6ed1,#2f8be3);color:#fff;border-radius:10px;text-decoration:none;font-weight:800}}
</style></head><body>
<div class="card">
  <div class="icon">{"✅" if not is_cancel else "↩️"}</div>
  <h1>{title}</h1>
  <p>{message}</p>
  <a href="/">Back to home</a>
</div></body></html>"""


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/driver/test-email")
def driver_test_email(driver_session: str | None = Cookie(default=None)) -> dict[str, str]:
    if not driver_session or not _verify_session(driver_session):
        raise HTTPException(status_code=401, detail="Not authenticated.")

    try:
        _assert_email_ready()
        target = settings.business_email or settings.smtp_from
        if not target:
            raise RuntimeError("No target email is configured.")
        _send_email(
            target,
            f"Railway email test - {settings.business_name}",
            (
                f"This is a live email transport test from Railway.\n\n"
                f"Sent at: {datetime.now().isoformat(timespec='seconds')}\n"
                f"Business: {settings.business_name}\n"
                f"Target: {target}\n"
            ),
        )
        return {"success": "true", "message": f"Test email sent to {target}"}
    except Exception as exc:
        logger.exception("Driver email test failed")
        raise HTTPException(status_code=500, detail=f"Email test failed: {exc}") from exc


@app.get("/api/public-config")
def public_config() -> dict[str, object]:
    return {
        "business_name": settings.business_name,
        "business_phone": settings.business_phone,
        "booking_window_days": settings.booking_window_days,
        "google_maps_api_key": settings.google_maps_api_key,
        "stripe_publishable_key": settings.stripe_publishable_key,
        "pricing": [
            {"key": key, "label": SERVICE_LABELS[key], "price_aud": PRICE_MAP[key] / 100}
            for key in PRICE_MAP
        ],
    }

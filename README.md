# Local Booking Site

## Setup

```powershell
cd "c:\Users\aslam\Projects\Audio Book\local-booking-site"
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

Copy env values:

```powershell
Copy-Item .env.example .env
```

Then update `.env` with your email + phone details and SMTP credentials.

## Run

```powershell
cd "c:\Users\aslam\Projects\Audio Book\local-booking-site"
.\.venv\Scripts\Activate.ps1
uvicorn app:app --host 127.0.0.1 --port 8010 --reload
```

Open: `http://127.0.0.1:8010`

## Deploy on Render

- Runtime: `Python`
- Build command: `pip install -r requirements.txt`
- Start command: `python -m uvicorn app:app --host 0.0.0.0 --port $PORT`

This repo includes `render.yaml` with the same settings.

## Deploy on Railway

- Create one project from the GitHub repo for this app
- Do not add Redis, Postgres, or storage for this project
- Region: `Singapore`
- Build command: `pip install -r requirements.txt`
- Start command: `python -m uvicorn app:app --host 0.0.0.0 --port $PORT`

Required environment variables:

- `BUSINESS_NAME`
- `BUSINESS_PHONE`
- `BUSINESS_EMAIL`
- `GOOGLE_MAPS_API_KEY`
- `DRIVER_PASSWORD`
- `SECRET_KEY`
- `STRIPE_SECRET_KEY`
- `STRIPE_PUBLISHABLE_KEY`
- `STRIPE_WEBHOOK_SECRET`

Email settings:

- `SMTP_HOST`
- `SMTP_PORT`
- `SMTP_USER`
- `SMTP_PASSWORD`
- `SMTP_FROM`

## Pricing Included

- Delivery within 4 hours: AUD $30
- Direct delivery: AUD $60
- Instant lift: AUD $30
- Airport (Gold Coast to Brisbane): AUD $60

## Email Confirmation

Each booking sends email to:
- customer email
- your business email

Email body includes:
- customer phone number
- your business phone number
- all booking details

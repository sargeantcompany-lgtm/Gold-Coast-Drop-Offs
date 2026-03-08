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

Then update `.env` with your email + phone details.

## Run

```powershell
cd "c:\Users\aslam\Projects\Audio Book\local-booking-site"
.\.venv\Scripts\Activate.ps1
uvicorn app:app --host 127.0.0.1 --port 8010 --reload
```

Open: `http://127.0.0.1:8010`

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

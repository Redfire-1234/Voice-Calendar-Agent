import os
import re
import datetime
from typing import Optional

import gradio as gr
from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from starlette.middleware.sessions import SessionMiddleware

import psycopg2
from psycopg2.extras import RealDictCursor

from google_auth_oauthlib.flow import Flow
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from google.auth.transport.requests import Request as GoogleRequest

from dateutil import parser
import tzlocal

# ================== ENV ==================

DATABASE_URL = os.getenv("DATABASE_URL")
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET")
SESSION_SECRET = os.getenv("SESSION_SECRET", "change-me")

SCOPES = ["https://www.googleapis.com/auth/calendar"]

REDIRECT_URI = "https://voice-calendar-agent.onrender.com/oauth2callback"

# ================== FASTAPI ==================

app = FastAPI()
app.add_middleware(SessionMiddleware, secret_key=SESSION_SECRET)

# ================== DATABASE ==================

def get_db():
    return psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)

def save_tokens(user_id, email, creds: Credentials):
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO user_tokens (user_id, email, access_token, refresh_token, expiry)
                VALUES (%s,%s,%s,%s,%s)
                ON CONFLICT (user_id)
                DO UPDATE SET
                  access_token = EXCLUDED.access_token,
                  refresh_token = EXCLUDED.refresh_token,
                  expiry = EXCLUDED.expiry
            """, (
                user_id,
                email,
                creds.token,
                creds.refresh_token,
                creds.expiry
            ))

def load_tokens(user_id) -> Optional[Credentials]:
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM user_tokens WHERE user_id=%s", (user_id,))
            row = cur.fetchone()

    if not row:
        return None

    creds = Credentials(
        token=row["access_token"],
        refresh_token=row["refresh_token"],
        token_uri="https://oauth2.googleapis.com/token",
        client_id=GOOGLE_CLIENT_ID,
        client_secret=GOOGLE_CLIENT_SECRET,
        scopes=SCOPES,
    )
    creds.expiry = row["expiry"]
    return creds

# ================== GOOGLE OAUTH ==================

@app.get("/login")
def login(request: Request):
    flow = Flow.from_client_config(
        {
            "web": {
                "client_id": GOOGLE_CLIENT_ID,
                "client_secret": GOOGLE_CLIENT_SECRET,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
            }
        },
        scopes=SCOPES,
        redirect_uri=REDIRECT_URI,
    )

    auth_url, state = flow.authorization_url(
        access_type="offline",
        prompt="consent",
    )

    request.session["state"] = state
    return RedirectResponse(auth_url)

@app.get("/oauth2callback")
def oauth2callback(request: Request):
    flow = Flow.from_client_config(
        {
            "web": {
                "client_id": GOOGLE_CLIENT_ID,
                "client_secret": GOOGLE_CLIENT_SECRET,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
            }
        },
        scopes=SCOPES,
        state=request.session["state"],
        redirect_uri=REDIRECT_URI,
    )

    flow.fetch_token(authorization_response=str(request.url))
    creds = flow.credentials

    oauth = build("oauth2", "v2", credentials=creds)
    user = oauth.userinfo().get().execute()

    save_tokens(user["id"], user["email"], creds)

    return RedirectResponse("/?user_id=" + user["id"])

# ================== CALENDAR ==================

def get_calendar_service(user_id):
    creds = load_tokens(user_id)
    if not creds:
        raise Exception("User not authenticated")

    if creds.expired:
        creds.refresh(GoogleRequest())
        save_tokens(user_id, "", creds)

    return build("calendar", "v3", credentials=creds)

def create_event(user_id, title, start_time):
    service = get_calendar_service(user_id)

    end_time = start_time + datetime.timedelta(hours=1)
    tz = str(tzlocal.get_localzone())

    event = {
        "summary": title,
        "start": {"dateTime": start_time.isoformat(), "timeZone": tz},
        "end": {"dateTime": end_time.isoformat(), "timeZone": tz},
    }

    created = service.events().insert(
        calendarId="primary",
        body=event
    ).execute()

    if "id" not in created:
        raise Exception("Event creation failed")

    return f"✅ Event created on {start_time.strftime('%A %I:%M %p')}"

# ================== CHAT ==================

def chat_fn(message, history, user_id):
    if not user_id:
        history.append({
            "role": "assistant",
            "content": "🔐 Please login first using **Login with Google**."
        })
        return history, ""

    history.append({"role": "user", "content": message})

    if "schedule" in message.lower():
        name = re.search(r"with (\w+)", message.lower())
        name = name.group(1).capitalize() if name else "Guest"

        time_match = re.search(r"(\d{1,2}(:\d{2})?\s?(am|pm))", message.lower())
        time_str = time_match.group(1) if time_match else "10 AM"

        today = datetime.date.today()
        date = today + datetime.timedelta(days=1) if "tomorrow" in message.lower() else today

        start = parser.parse(f"{date} {time_str}")

        try:
            result = create_event(user_id, f"Meeting with {name}", start)
            history.append({"role": "assistant", "content": result})
        except Exception as e:
            history.append({"role": "assistant", "content": f"❌ {str(e)}"})

        return history, ""

    history.append({"role": "assistant", "content": "I can help schedule meetings 📅"})
    return history, ""

# ================== UI ==================

with gr.Blocks() as demo:
    user_id = gr.State()

    gr.Markdown("# 📅 Calendar SaaS Agent")
    gr.Markdown("[🔑 Login with Google](/login)")

    chatbot = gr.Chatbot(height=400)
    msg = gr.Textbox(placeholder="Schedule meeting with Fauzia tomorrow at 8 AM")

    msg.submit(chat_fn, [msg, chatbot, user_id], [chatbot, msg])

app = gr.mount_gradio_app(app, demo, path="/")

# ================== START ==================

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))

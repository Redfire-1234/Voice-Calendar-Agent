"""
SaaS Voice/Text Calendar Agent
- FastAPI backend
- Google OAuth (Web)
- PostgreSQL token storage
- Gradio UI
- Render compatible
"""

import os
import json
import uuid
import datetime
from typing import Optional

import gradio as gr
from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from starlette.middleware.sessions import SessionMiddleware

import psycopg2
from psycopg2.extras import RealDictCursor

from groq import Groq

from google_auth_oauthlib.flow import Flow
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from google.auth.transport.requests import Request as GoogleRequest

from dateutil import parser
import tzlocal

# ------------------ ENV ------------------

GROQ_API_KEY = os.getenv("GROQ_API_KEY")
DATABASE_URL = os.getenv("DATABASE_URL")
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET")
SESSION_SECRET = os.getenv("SESSION_SECRET", "change-this")

SCOPES = [
    "https://www.googleapis.com/auth/calendar",
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
]

REDIRECT_URI = "https://voice-calendar-agent.onrender.com/oauth2callback"

groq_client = Groq(api_key=GROQ_API_KEY)

# ------------------ FASTAPI ------------------

app = FastAPI()
app.add_middleware(SessionMiddleware, secret_key=SESSION_SECRET)

# ------------------ DATABASE ------------------

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

# ------------------ GOOGLE OAUTH ------------------

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

    authorization_url, state = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent",
    )

    request.session["state"] = state
    return RedirectResponse(authorization_url)

@app.get("/oauth2callback")
def oauth2callback(request: Request):
    state = request.session.get("state")

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
        state=state,
        redirect_uri=REDIRECT_URI,
    )

    flow.fetch_token(authorization_response=str(request.url))
    creds = flow.credentials

    # get user email
    oauth2_service = build("oauth2", "v2", credentials=creds)
    user_info = oauth2_service.userinfo().get().execute()

    user_id = user_info["id"]
    email = user_info["email"]

    save_tokens(user_id, email, creds)

    request.session["user_id"] = user_id
    return RedirectResponse("/")

# ------------------ CALENDAR ------------------

def get_calendar_service(user_id):
    creds = load_tokens(user_id)
    if not creds:
        raise Exception("User not authenticated")

    if creds.expired and creds.refresh_token:
        creds.refresh(GoogleRequest())
        save_tokens(user_id, "", creds)

    return build("calendar", "v3", credentials=creds)

def create_calendar_event(user_id, name, date_str, time_str, title=None):
    service = get_calendar_service(user_id)

    start = parser.parse(f"{date_str} {time_str}", fuzzy=True)
    end = start + datetime.timedelta(hours=1)
    tz = str(tzlocal.get_localzone())

    event = {
        "summary": title or f"Meeting with {name}",
        "start": {"dateTime": start.isoformat(), "timeZone": tz},
        "end": {"dateTime": end.isoformat(), "timeZone": tz},
    }

    service.events().insert(calendarId="primary", body=event).execute()
    return f"✅ Event created on {start.strftime('%A %I:%M %p')}"

# ------------------ LLM CHAT ------------------

def chat_fn(message, history, request: gr.Request):
    user_id = request.session.get("user_id")

    if not user_id:
        return history + [
            {"role": "assistant", "content": "🔐 Please connect your Google Calendar first.\n\n👉 Click **Login with Google**."}
        ], ""

    response = groq_client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        messages=[{"role": "user", "content": message}],
    )

    reply = response.choices[0].message.content
    history.append({"role": "user", "content": message})
    history.append({"role": "assistant", "content": reply})
    return history, ""

# ------------------ GRADIO UI ------------------

with gr.Blocks(title="Calendar SaaS Agent") as demo:
    gr.Markdown("# 📅 Calendar Agent (SaaS)")
    gr.Markdown("[🔑 Login with Google](/login)")

    chatbot = gr.Chatbot(height=450)
    msg = gr.Textbox(placeholder="Schedule meeting tomorrow at 3 PM")
    send = gr.Button("Send")

    send.click(chat_fn, [msg, chatbot], [chatbot, msg])
    msg.submit(chat_fn, [msg, chatbot], [chatbot, msg])

app = gr.mount_gradio_app(app, demo, path="/")

# ------------------ START ------------------

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))

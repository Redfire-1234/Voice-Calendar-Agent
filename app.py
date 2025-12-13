"""
SaaS Voice/Text Calendar Agent - Production Ready
- FastAPI backend with proper error handling
- Google OAuth (Web) with token refresh
- PostgreSQL token storage with schema management
- Gradio UI with improved UX
- LLM-powered intent extraction
- Comprehensive logging
"""

import os
import json
import uuid
import datetime
import logging
from typing import Optional, Dict, Any

import gradio as gr
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import RedirectResponse
from starlette.middleware.sessions import SessionMiddleware

import psycopg2
from psycopg2.extras import RealDictCursor
from psycopg2 import sql

from groq import Groq

from google_auth_oauthlib.flow import Flow
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from google.auth.transport.requests import Request as GoogleRequest
from googleapiclient.errors import HttpError

from dateutil import parser
import tzlocal

# ------------------ LOGGING ------------------

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# ------------------ ENV ------------------

GROQ_API_KEY = os.getenv("GROQ_API_KEY")
DATABASE_URL = os.getenv("DATABASE_URL")
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET")
SESSION_SECRET = os.getenv("SESSION_SECRET", "change-this-in-production")
REDIRECT_URI = os.getenv("REDIRECT_URI", "https://voice-calendar-agent.onrender.com/oauth2callback")

# Validate required environment variables
required_vars = {
    "GROQ_API_KEY": GROQ_API_KEY,
    "DATABASE_URL": DATABASE_URL,
    "GOOGLE_CLIENT_ID": GOOGLE_CLIENT_ID,
    "GOOGLE_CLIENT_SECRET": GOOGLE_CLIENT_SECRET,
}

missing_vars = [k for k, v in required_vars.items() if not v]
if missing_vars:
    raise ValueError(f"Missing required environment variables: {', '.join(missing_vars)}")

SCOPES = [
    "https://www.googleapis.com/auth/calendar",
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
]

groq_client = Groq(api_key=GROQ_API_KEY)

# ------------------ FASTAPI ------------------

app = FastAPI(title="Calendar SaaS Agent")
app.add_middleware(SessionMiddleware, secret_key=SESSION_SECRET)

# ------------------ DATABASE ------------------

def get_db():
    """Create database connection with error handling."""
    try:
        return psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)
    except psycopg2.Error as e:
        logger.error(f"Database connection failed: {e}")
        raise HTTPException(status_code=500, detail="Database connection failed")

def init_db():
    """Initialize database schema."""
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS user_tokens (
                        user_id VARCHAR(255) PRIMARY KEY,
                        email VARCHAR(255) NOT NULL,
                        access_token TEXT NOT NULL,
                        refresh_token TEXT,
                        expiry TIMESTAMP,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)
                logger.info("Database schema initialized")
    except psycopg2.Error as e:
        logger.error(f"Failed to initialize database: {e}")
        raise

def save_tokens(user_id: str, email: str, creds: Credentials):
    """Save or update user tokens in database."""
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO user_tokens (user_id, email, access_token, refresh_token, expiry, updated_at)
                    VALUES (%s,%s,%s,%s,%s, CURRENT_TIMESTAMP)
                    ON CONFLICT (user_id)
                    DO UPDATE SET
                      email = EXCLUDED.email,
                      access_token = EXCLUDED.access_token,
                      refresh_token = COALESCE(EXCLUDED.refresh_token, user_tokens.refresh_token),
                      expiry = EXCLUDED.expiry,
                      updated_at = CURRENT_TIMESTAMP
                """, (
                    user_id,
                    email,
                    creds.token,
                    creds.refresh_token,
                    creds.expiry
                ))
                logger.info(f"Saved tokens for user {user_id}")
    except psycopg2.Error as e:
        logger.error(f"Failed to save tokens: {e}")
        raise

def load_tokens(user_id: str) -> Optional[Credentials]:
    """Load user tokens from database."""
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM user_tokens WHERE user_id=%s", (user_id,))
                row = cur.fetchone()

        if not row:
            logger.info(f"No tokens found for user {user_id}")
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
        logger.info(f"Loaded tokens for user {user_id}")
        return creds
    except psycopg2.Error as e:
        logger.error(f"Failed to load tokens: {e}")
        return None

# ------------------ GOOGLE OAUTH ------------------

def get_oauth_flow(state: Optional[str] = None) -> Flow:
    """Create OAuth flow with consistent configuration."""
    return Flow.from_client_config(
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

@app.get("/login")
def login(request: Request):
    """Initiate Google OAuth flow."""
    try:
        flow = get_oauth_flow()
        authorization_url, state = flow.authorization_url(
            access_type="offline",
            include_granted_scopes="true",
            prompt="consent",
        )
        request.session["state"] = state
        logger.info("OAuth flow initiated")
        return RedirectResponse(authorization_url)
    except Exception as e:
        logger.error(f"Login failed: {e}")
        raise HTTPException(status_code=500, detail="Failed to initiate login")

@app.get("/oauth2callback")
def oauth2callback(request: Request):
    """Handle OAuth callback and store tokens."""
    try:
        state = request.session.get("state")
        if not state:
            raise HTTPException(status_code=400, detail="Invalid state")

        flow = get_oauth_flow(state)
        flow.fetch_token(authorization_response=str(request.url))
        creds = flow.credentials

        # Get user email
        oauth2_service = build("oauth2", "v2", credentials=creds)
        user_info = oauth2_service.userinfo().get().execute()

        user_id = user_info["id"]
        email = user_info["email"]

        save_tokens(user_id, email, creds)
        request.session["user_id"] = user_id
        request.session["email"] = email
        
        logger.info(f"User {email} authenticated successfully")
        return RedirectResponse("/")
    except Exception as e:
        logger.error(f"OAuth callback failed: {e}")
        raise HTTPException(status_code=500, detail="Authentication failed")

@app.get("/logout")
def logout(request: Request):
    """Clear user session."""
    request.session.clear()
    logger.info("User logged out")
    return RedirectResponse("/")

# ------------------ CALENDAR ------------------

def get_calendar_service(user_id: str):
    """Get authenticated Calendar service."""
    creds = load_tokens(user_id)
    if not creds:
        raise ValueError("User not authenticated")

    # Refresh token if expired
    if creds.expired and creds.refresh_token:
        try:
            creds.refresh(GoogleRequest())
            # Get email from session or database
            with get_db() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT email FROM user_tokens WHERE user_id=%s", (user_id,))
                    row = cur.fetchone()
                    email = row["email"] if row else ""
            save_tokens(user_id, email, creds)
            logger.info(f"Refreshed tokens for user {user_id}")
        except Exception as e:
            logger.error(f"Token refresh failed: {e}")
            raise ValueError("Token refresh failed. Please re-authenticate.")

    return build("calendar", "v3", credentials=creds)

def create_calendar_event(
    user_id: str,
    summary: str,
    start_datetime: datetime.datetime,
    duration_hours: int = 1,
    description: str = ""
) -> Dict[str, Any]:
    """Create a calendar event."""
    try:
        service = get_calendar_service(user_id)
        
        end_datetime = start_datetime + datetime.timedelta(hours=duration_hours)
        tz = str(tzlocal.get_localzone())

        event = {
            "summary": summary,
            "description": description,
            "start": {"dateTime": start_datetime.isoformat(), "timeZone": tz},
            "end": {"dateTime": end_datetime.isoformat(), "timeZone": tz},
        }

        result = service.events().insert(calendarId="primary", body=event).execute()
        logger.info(f"Event created: {result['id']}")
        
        return {
            "success": True,
            "event_id": result["id"],
            "summary": summary,
            "start": start_datetime,
            "link": result.get("htmlLink")
        }
    except HttpError as e:
        logger.error(f"Calendar API error: {e}")
        raise ValueError(f"Failed to create event: {e}")
    except Exception as e:
        logger.error(f"Unexpected error creating event: {e}")
        raise ValueError(f"Failed to create event: {str(e)}")

# ------------------ LLM-POWERED INTENT EXTRACTION ------------------

def extract_meeting_details(message: str) -> Optional[Dict[str, Any]]:
    """Use LLM to extract meeting details from natural language."""
    try:
        system_prompt = """You are a calendar assistant. Extract meeting details from user messages.
        
Return a JSON object with these fields:
- summary: meeting title/description
- date: date in YYYY-MM-DD format (use relative dates like "tomorrow" = next day)
- time: time in 24-hour format HH:MM
- duration_hours: meeting duration (default 1)

If you cannot extract clear details, return {"error": "reason"}.

Examples:
"Schedule meeting with John tomorrow at 3 PM" -> {"summary": "Meeting with John", "date": "tomorrow", "time": "15:00", "duration_hours": 1}
"Book a 2 hour call with the team on Friday at 10 AM" -> {"summary": "Call with the team", "date": "friday", "time": "10:00", "duration_hours": 2}
"""

        response = groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": message}
            ],
            temperature=0.1,
        )

        reply = response.choices[0].message.content.strip()
        
        # Extract JSON from response (handle markdown code blocks)
        if "```json" in reply:
            reply = reply.split("```json")[1].split("```")[0].strip()
        elif "```" in reply:
            reply = reply.split("```")[1].split("```")[0].strip()
            
        details = json.loads(reply)
        
        if "error" in details:
            logger.info(f"LLM couldn't extract details: {details['error']}")
            return None
            
        # Parse relative dates
        today = datetime.date.today()
        date_str = details["date"].lower()
        
        if date_str == "tomorrow":
            target_date = today + datetime.timedelta(days=1)
        elif date_str == "today":
            target_date = today
        elif date_str in ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]:
            days_ahead = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"].index(date_str)
            current_day = today.weekday()
            days_diff = (days_ahead - current_day) % 7
            if days_diff == 0:
                days_diff = 7  # Next week
            target_date = today + datetime.timedelta(days=days_diff)
        else:
            # Try parsing as date
            target_date = parser.parse(date_str).date()
        
        # Combine date and time
        time_parts = details["time"].split(":")
        start_datetime = datetime.datetime.combine(
            target_date,
            datetime.time(int(time_parts[0]), int(time_parts[1]))
        )
        
        return {
            "summary": details["summary"],
            "start_datetime": start_datetime,
            "duration_hours": details.get("duration_hours", 1)
        }
        
    except json.JSONDecodeError as e:
        logger.error(f"Failed to parse LLM response: {e}, response: {reply}")
        return None
    except Exception as e:
        logger.error(f"Error extracting meeting details: {e}")
        return None

# ------------------ CHAT HANDLER ------------------

def chat_fn(message: str, history: list, request: gr.Request):
    """Handle chat messages with intent detection."""
    user_id = request.session.get("user_id")
    email = request.session.get("email", "")

    if not user_id:
        return history + [{
            "role": "assistant",
            "content": "🔐 **Please connect your Google Calendar first.**\n\n👉 Click the **[Login with Google](/login)** link above."
        }], ""

    history.append({"role": "user", "content": message})

    # Check for scheduling intent
    schedule_keywords = ["schedule", "book", "create", "set", "add", "make"]
    meeting_keywords = ["meeting", "call", "appointment", "event"]
    
    has_schedule_intent = any(k in message.lower() for k in schedule_keywords)
    has_meeting_context = any(k in message.lower() for k in meeting_keywords)

    if has_schedule_intent or has_meeting_context:
        try:
            # Extract meeting details using LLM
            details = extract_meeting_details(message)
            
            if not details:
                history.append({
                    "role": "assistant",
                    "content": "❓ I couldn't quite understand the meeting details. Please specify:\n- Who/what is the meeting about?\n- Date (e.g., tomorrow, Friday)\n- Time (e.g., 3 PM, 14:00)"
                })
                return history, ""
            
            # Create the event
            result = create_calendar_event(
                user_id=user_id,
                summary=details["summary"],
                start_datetime=details["start_datetime"],
                duration_hours=details["duration_hours"]
            )
            
            response_msg = f"""✅ **Event Created Successfully!**

📅 **{result['summary']}**
🕒 {result['start'].strftime('%A, %B %d at %I:%M %p')}
🔗 [View in Calendar]({result['link']})"""

            history.append({"role": "assistant", "content": response_msg})
            return history, ""

        except ValueError as e:
            history.append({
                "role": "assistant",
                "content": f"❌ {str(e)}\n\nPlease try again or [re-authenticate](/login)."
            })
            return history, ""
        except Exception as e:
            logger.error(f"Unexpected error in chat_fn: {e}")
            history.append({
                "role": "assistant",
                "content": "❌ An unexpected error occurred. Please try again."
            })
            return history, ""

    # Fallback: General chat
    try:
        response = groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[
                {"role": "system", "content": "You are a helpful calendar assistant. Keep responses concise and friendly."},
                {"role": "user", "content": message}
            ],
        )
        reply = response.choices[0].message.content
        history.append({"role": "assistant", "content": reply})
        return history, ""
    except Exception as e:
        logger.error(f"Groq API error: {e}")
        history.append({
            "role": "assistant",
            "content": "❌ I'm having trouble processing that. Please try again."
        })
        return history, ""

# ------------------ GRADIO UI ------------------

with gr.Blocks(title="Calendar SaaS Agent", theme=gr.themes.Soft()) as demo:
    gr.Markdown("# 📅 AI Calendar Agent")
    gr.Markdown("Connect your Google Calendar and schedule meetings using natural language!")
    
    with gr.Row():
        gr.Markdown("[🔑 Login with Google](/login)")
        gr.Markdown("[🚪 Logout](/logout)")

    chatbot = gr.Chatbot(
        height=500,
        show_label=False,
        avatar_images=(None, "https://api.dicebear.com/7.x/bottts/svg?seed=calendar")
    )
    
    with gr.Row():
        msg = gr.Textbox(
            placeholder="Try: 'Schedule a meeting with Sarah tomorrow at 3 PM'",
            show_label=False,
            scale=9
        )
        send = gr.Button("Send", scale=1, variant="primary")

    gr.Examples(
        examples=[
            "Schedule a team standup tomorrow at 10 AM",
            "Book a 2 hour client call on Friday at 2 PM",
            "Create a dentist appointment next Monday at 9 AM"
        ],
        inputs=msg
    )

    send.click(chat_fn, [msg, chatbot], [chatbot, msg])
    msg.submit(chat_fn, [msg, chatbot], [chatbot, msg])

app = gr.mount_gradio_app(app, demo, path="/")

# ------------------ STARTUP ------------------

@app.on_event("startup")
async def startup_event():
    """Initialize application on startup."""
    logger.info("Starting Calendar SaaS Agent")
    init_db()
    logger.info("Application ready")

# ------------------ MAIN ------------------

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 10000))
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")

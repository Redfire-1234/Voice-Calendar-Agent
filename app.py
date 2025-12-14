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

SCOPES = [
    "https://www.googleapis.com/auth/calendar",
    "openid",
    "https://www.googleapis.com/auth/userinfo.email"
]

REDIRECT_URI = os.getenv("REDIRECT_URI", "https://voice-calendar-agent.onrender.com/oauth2callback")

# ================== FASTAPI ==================

app = FastAPI()
app.add_middleware(SessionMiddleware, secret_key=SESSION_SECRET)

# ================== DATABASE ==================

def init_db():
    """Create table if it doesn't exist"""
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS user_tokens (
                        user_id VARCHAR(255) PRIMARY KEY,
                        email VARCHAR(255) NOT NULL,
                        access_token TEXT NOT NULL,
                        refresh_token TEXT,
                        expiry TIMESTAMP
                    )
                """)
                print("✅ Database table ready")
    except Exception as e:
        print(f"❌ DB init error: {e}")

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
                  email = EXCLUDED.email,
                  access_token = EXCLUDED.access_token,
                  refresh_token = COALESCE(EXCLUDED.refresh_token, user_tokens.refresh_token),
                  expiry = EXCLUDED.expiry
            """, (
                user_id,
                email,
                creds.token,
                creds.refresh_token,
                creds.expiry
            ))
            print(f"✅ Saved tokens for {email}")

def load_tokens(user_id) -> Optional[Credentials]:
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM user_tokens WHERE user_id=%s", (user_id,))
            row = cur.fetchone()

    if not row:
        print(f"❌ No tokens found for user {user_id}")
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
    print(f"✅ Loaded tokens for user {user_id}")
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
    try:
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

        oauth = build("oauth2", "v2", credentials=creds)
        user = oauth.userinfo().get().execute()

        save_tokens(user["id"], user["email"], creds)
        
        request.session["user_id"] = user["id"]
        request.session["email"] = user["email"]

        print(f"✅ User {user['email']} logged in successfully")
        return RedirectResponse("/")
        
    except Exception as e:
        print(f"❌ OAuth error: {e}")
        return RedirectResponse(f"/?error={str(e)}")

@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/")

# ================== CALENDAR ==================

def get_calendar_service(user_id):
    creds = load_tokens(user_id)
    if not creds:
        raise Exception("User not authenticated. Please login again.")

    # Check if token is expired and refresh if needed
    if creds.expired and creds.refresh_token:
        print(f"🔄 Refreshing expired token for user {user_id}")
        try:
            creds.refresh(GoogleRequest())
            # Get email from DB for refresh
            with get_db() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT email FROM user_tokens WHERE user_id=%s", (user_id,))
                    row = cur.fetchone()
                    email = row["email"] if row else ""
            save_tokens(user_id, email, creds)
            print(f"✅ Token refreshed successfully")
        except Exception as e:
            print(f"❌ Token refresh failed: {e}")
            raise Exception("Token refresh failed. Please login again.")
    elif creds.expired:
        raise Exception("Token expired and no refresh token available. Please login again.")

    return build("calendar", "v3", credentials=creds)

def create_event(user_id, title, start_time):
    try:
        service = get_calendar_service(user_id)

        end_time = start_time + datetime.timedelta(hours=1)
        tz = str(tzlocal.get_localzone())

        event = {
            "summary": title,
            "start": {"dateTime": start_time.isoformat(), "timeZone": tz},
            "end": {"dateTime": end_time.isoformat(), "timeZone": tz},
        }

        print(f"📅 Creating event: {title} at {start_time}")
        created = service.events().insert(
            calendarId="primary",
            body=event
        ).execute()

        if "id" not in created:
            raise Exception("Event creation failed - no ID returned")

        print(f"✅ Event created with ID: {created['id']}")
        event_link = created.get('htmlLink', '')
        return f"✅ **Event Created!**\n\n📅 {title}\n🕐 {start_time.strftime('%A, %B %d at %I:%M %p')}\n🔗 [View in Calendar]({event_link})"
    
    except Exception as e:
        print(f"❌ Event creation error: {e}")
        raise

# ================== CHAT ==================

def chat_fn(message, history, request: gr.Request):
    user_id = request.session.get("user_id")
    email = request.session.get("email", "")

    if not user_id:
        history.append({
            "role": "assistant",
            "content": "🔐 **Please login first!**\n\nClick **[Login with Google](/login)** above."
        })
        return history, ""

    history.append({"role": "user", "content": message})

    # Check if user wants to schedule something
    schedule_keywords = ["schedule", "book", "create", "add", "set up"]
    has_schedule_intent = any(keyword in message.lower() for keyword in schedule_keywords)

    if not has_schedule_intent:
        # Just a greeting or question - don't create events!
        history.append({
            "role": "assistant", 
            "content": f"👋 Hi! I can help you schedule meetings.\n\n**Try saying:**\n- 'Schedule meeting with John tomorrow at 3 PM'\n- 'Book a call with Sarah on Friday at 10 AM'"
        })
        return history, ""

    # Now parse the scheduling request
    try:
        # Extract person name
        name_match = re.search(r"with (\w+)", message.lower())
        person_name = name_match.group(1).capitalize() if name_match else None

        # Extract time - look for patterns like "3 PM", "10:30 AM", "14:00"
        time_match = re.search(r"at\s+(\d{1,2}(?::\d{2})?\s*(?:am|pm|AM|PM)?)", message.lower())
        
        if not time_match:
            history.append({
                "role": "assistant",
                "content": "❓ **I need more details!**\n\nPlease specify:\n- What time? (e.g., '3 PM', '10:30 AM')\n\nExample: 'Schedule meeting with John tomorrow at 3 PM'"
            })
            return history, ""
        
        time_str = time_match.group(1).strip()

        # Extract date
        today = datetime.date.today()
        target_date = None
        
        if "tomorrow" in message.lower():
            target_date = today + datetime.timedelta(days=1)
            day_name = "tomorrow"
        elif "today" in message.lower():
            target_date = today
            day_name = "today"
        elif "monday" in message.lower():
            days_ahead = (0 - today.weekday()) % 7
            if days_ahead == 0:
                days_ahead = 7
            target_date = today + datetime.timedelta(days=days_ahead)
            day_name = "Monday"
        elif "tuesday" in message.lower():
            days_ahead = (1 - today.weekday()) % 7
            if days_ahead == 0:
                days_ahead = 7
            target_date = today + datetime.timedelta(days=days_ahead)
            day_name = "Tuesday"
        elif "wednesday" in message.lower():
            days_ahead = (2 - today.weekday()) % 7
            if days_ahead == 0:
                days_ahead = 7
            target_date = today + datetime.timedelta(days=days_ahead)
            day_name = "Wednesday"
        elif "thursday" in message.lower():
            days_ahead = (3 - today.weekday()) % 7
            if days_ahead == 0:
                days_ahead = 7
            target_date = today + datetime.timedelta(days=days_ahead)
            day_name = "Thursday"
        elif "friday" in message.lower():
            days_ahead = (4 - today.weekday()) % 7
            if days_ahead == 0:
                days_ahead = 7
            target_date = today + datetime.timedelta(days=days_ahead)
            day_name = "Friday"
        elif "saturday" in message.lower():
            days_ahead = (5 - today.weekday()) % 7
            if days_ahead == 0:
                days_ahead = 7
            target_date = today + datetime.timedelta(days=days_ahead)
            day_name = "Saturday"
        elif "sunday" in message.lower():
            days_ahead = (6 - today.weekday()) % 7
            if days_ahead == 0:
                days_ahead = 7
            target_date = today + datetime.timedelta(days=days_ahead)
            day_name = "Sunday"
        else:
            # No date specified - ask for it!
            history.append({
                "role": "assistant",
                "content": "❓ **When should I schedule this?**\n\nPlease specify a day:\n- tomorrow\n- today\n- Monday, Tuesday, etc.\n\nExample: 'Schedule meeting with John tomorrow at 3 PM'"
            })
            return history, ""

        # Parse the full datetime
        start_time = parser.parse(f"{target_date} {time_str}")
        
        # Create event title
        if person_name:
            title = f"Meeting with {person_name}"
        else:
            title = "Meeting"

        # Confirm before creating
        confirmation = f"📅 **Ready to schedule:**\n\n" \
                      f"• {title}\n" \
                      f"• {start_time.strftime('%A, %B %d at %I:%M %p')}\n\n" \
                      f"Creating event..."
        
        history.append({"role": "assistant", "content": confirmation})

        # Create the event
        result = create_event(user_id, title, start_time)
        history.append({"role": "assistant", "content": result})
        
    except ValueError as e:
        history.append({
            "role": "assistant", 
            "content": f"❌ **Couldn't parse the time.**\n\nPlease use format like:\n- '3 PM'\n- '10:30 AM'\n- '14:00'\n\nError: {str(e)}"
        })
    except Exception as e:
        print(f"❌ Error in chat_fn: {e}")
        history.append({
            "role": "assistant", 
            "content": f"❌ **Error:** {str(e)}\n\nTry [logging in again](/login) if the problem persists."
        })

    return history, ""

# ================== UI ==================

with gr.Blocks(title="Calendar Agent", theme=gr.themes.Soft()) as demo:
    gr.Markdown("# 📅 Calendar Agent")
    
    with gr.Row():
        gr.Markdown("[🔑 Login with Google](/login)")
        gr.Markdown("[🚪 Logout](/logout)")

    chatbot = gr.Chatbot(height=450, show_label=False)
    
    with gr.Row():
        msg = gr.Textbox(
            placeholder="Schedule meeting with Fauzia tomorrow at 8 AM",
            show_label=False,
            scale=9
        )
        send = gr.Button("Send", scale=1, variant="primary")
    
    gr.Markdown("### 💡 Examples:")
    gr.Examples(
        examples=[
            "Schedule meeting with Fauzia tomorrow at 8 AM",
            "Book a call with John on Friday at 2 PM",
            "Create meeting with Sarah on Monday at 10:30 AM"
        ],
        inputs=msg
    )

    msg.submit(chat_fn, [msg, chatbot], [chatbot, msg])
    send.click(chat_fn, [msg, chatbot], [chatbot, msg])

app = gr.mount_gradio_app(app, demo, path="/")

# ================== STARTUP ==================

@app.on_event("startup")
async def startup():
    init_db()
    print("✅ Calendar Agent started successfully")

# ================== START ==================

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))

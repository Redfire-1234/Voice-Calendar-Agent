"""
Voice Calendar Agent - OAuth 2.0 with Function Calling (Render Deployment)
Combines web OAuth flow with Groq function calling like the desktop example.
"""

import os
import json
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
import pytz

from groq import Groq

# ================== ENV ==================

DATABASE_URL = os.getenv("DATABASE_URL")
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET")
SESSION_SECRET = os.getenv("SESSION_SECRET", "change-me")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")

groq_client = Groq(api_key=GROQ_API_KEY)

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
                print("✅ Database initialized")
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

        print(f"✅ User {user['email']} authenticated")
        return RedirectResponse("/")
        
    except Exception as e:
        print(f"❌ OAuth error: {e}")
        return RedirectResponse(f"/?error={str(e)}")

@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/")

# ================== CALENDAR SERVICE ==================

def get_calendar_service(user_id):
    """Get authenticated calendar service for user."""
    creds = load_tokens(user_id)
    if not creds:
        raise Exception("User not authenticated. Please login.")

    if creds.expired and creds.refresh_token:
        print(f"🔄 Refreshing token for user {user_id}")
        creds.refresh(GoogleRequest())
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT email FROM user_tokens WHERE user_id=%s", (user_id,))
                row = cur.fetchone()
                email = row["email"] if row else ""
        save_tokens(user_id, email, creds)
    elif creds.expired:
        raise Exception("Token expired. Please login again.")

    return build("calendar", "v3", credentials=creds)

# ================== CALENDAR FUNCTION ==================

def parse_datetime(date_str, time_str):
    """Parse date and time strings into datetime object in India timezone."""
    # Use India timezone
    india_tz = pytz.timezone('Asia/Kolkata')
    today = datetime.datetime.now(india_tz)
    
    # Parse date
    if "tomorrow" in date_str.lower():
        target_date = today.date() + datetime.timedelta(days=1)
    elif "today" in date_str.lower():
        target_date = today.date()
    else:
        try:
            parsed = parser.parse(date_str, fuzzy=True)
            target_date = parsed.date()
        except Exception:
            target_date = today.date() + datetime.timedelta(days=1)
    
    # Parse time - IMPORTANT: parse time independently
    try:
        # Parse time string independently to avoid date interference
        time_parsed = parser.parse(time_str, fuzzy=True)
        hour = time_parsed.hour
        minute = time_parsed.minute
    except Exception as e:
        print(f"⚠️ Time parse error for '{time_str}': {e}")
        hour = 9
        minute = 0
    
    # Combine date and time in India timezone
    naive_dt = datetime.datetime.combine(target_date, datetime.time(hour=hour, minute=minute))
    result = india_tz.localize(naive_dt)
    
    print(f"📅 Parsed: date_str='{date_str}', time_str='{time_str}' → {result} (India Time)")
    return result


def create_calendar_event(user_id, name, date_str, time_str, title=None):
    """Create calendar event - called by Groq function calling."""
    try:
        if not title:
            title = f"Meeting with {name}"

        # Parse the datetime (already in India timezone from parse_datetime)
        start_aware = parse_datetime(date_str, time_str)
        end_aware = start_aware + datetime.timedelta(hours=1)
        
        # Timezone is already set to Asia/Kolkata
        tz_name = "Asia/Kolkata"
        
        print(f"🌍 Using timezone: {tz_name}")
        print(f"⏰ Event time: {start_aware} to {end_aware}")

        service = get_calendar_service(user_id)

        event = {
            "summary": title,
            "start": {
                "dateTime": start_aware.isoformat(),
                "timeZone": tz_name
            },
            "end": {
                "dateTime": end_aware.isoformat(),
                "timeZone": tz_name
            },
            "description": f"Created by Calendar Agent for: {name}"
        }

        result = service.events().insert(calendarId="primary", body=event).execute()
        
        print(f"✅ Event created: {result['id']}")
        print(f"🔗 Event link: {result.get('htmlLink', '')}")

        return {
            "success": True,
            "message": f"✅ Event created: **{title}** on **{start_aware.strftime('%A, %B %d at %I:%M %p')}** (India Time)",
            "link": result.get("htmlLink", "")
        }

    except Exception as e:
        print(f"❌ Event creation error: {e}")
        import traceback
        traceback.print_exc()
        return {"success": False, "message": f"❌ Error creating event: {e}"}

# ================== GROQ FUNCTION DEFINITION ==================

functions = [
    {
        "name": "create_calendar_event",
        "description": "Create a Google Calendar event. Use this when the user wants to schedule a meeting or event.",
        "parameters": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string", 
                    "description": "The person's name or event topic (e.g., 'Bob', 'Team Meeting')"
                },
                "date_str": {
                    "type": "string", 
                    "description": "The date (e.g., 'tomorrow', 'Friday', 'Dec 15')"
                },
                "time_str": {
                    "type": "string", 
                    "description": "The time (e.g., '3 PM', '10:30 AM', '14:00')"
                },
                "title": {
                    "type": "string", 
                    "description": "Optional custom event title"
                }
            },
            "required": ["name", "date_str", "time_str"]
        }
    }
]

# ================== CHAT HANDLER ==================

def format_messages_from_history(history, user_message):
    """Convert Gradio history to Groq message format."""
    msgs = []
    for msg in history:
        if isinstance(msg, dict):
            if msg.get("role") == "user":
                msgs.append({"role": "user", "content": msg["content"]})
            elif msg.get("role") == "assistant":
                msgs.append({"role": "assistant", "content": msg["content"]})
    
    if user_message and isinstance(user_message, str):
        msgs.append({"role": "user", "content": user_message.strip()})
    
    return msgs


def chat(user_message, history, request: gr.Request):
    """Main chat handler with Groq function calling."""
    if not user_message.strip():
        return history, ""

    user_id = request.session.get("user_id")
    email = request.session.get("email", "")

    if not user_id:
        history.append({
            "role": "assistant",
            "content": "🔐 **Please login first!**\n\nClick **[Login with Google](/login)** above."
        })
        return history, ""

    try:
        messages = format_messages_from_history(history, user_message)
        
        messages.insert(0, {
            "role": "system",
            "content": "You are a friendly calendar assistant. Your primary function is to schedule events using the 'create_calendar_event' tool. Always confirm details before scheduling. Be helpful and professional."
        })

        response = groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=messages,
            tools=[{"type": "function", "function": fn} for fn in functions],
            tool_choice="auto",
            max_tokens=512,
            temperature=0.7
        )

        msg = response.choices[0].message

        # Check if function was called
        if msg.tool_calls:
            tool_call = msg.tool_calls[0]
            
            # Parse function arguments
            if isinstance(tool_call.function.arguments, str):
                args = json.loads(tool_call.function.arguments)
            else:
                args = dict(tool_call.function.arguments)
            
            # DEBUG: Print what Groq extracted
            print(f"🤖 Groq extracted arguments: {json.dumps(args, indent=2)}")
            
            if tool_call.function.name == "create_calendar_event":
                # Add user_id to args
                args["user_id"] = user_id
                result = create_calendar_event(**args)
                assistant_reply = result["message"]
                if result.get("link"):
                    assistant_reply += f"\n\n🔗 [View in Google Calendar]({result['link']})"
            else:
                assistant_reply = f"❌ Unknown function: {tool_call.function.name}"
        else:
            # No function call - just chat response
            assistant_reply = msg.content

        history.append({"role": "user", "content": user_message})
        history.append({"role": "assistant", "content": assistant_reply})
        return history, ""

    except Exception as e:
        print(f"❌ Chat error: {e}")
        import traceback
        traceback.print_exc()
        
        error_msg = f"❌ Error: {str(e)}\n\nTry [logging in again](/login) if the problem persists."
        history.append({"role": "user", "content": user_message})
        history.append({"role": "assistant", "content": error_msg})
        return history, ""


def reset_conversation():
    """Reset chat history."""
    return [], ""

# ================== GRADIO UI ==================

with gr.Blocks(title="Voice Calendar Agent", theme=gr.themes.Soft()) as demo:
    gr.Markdown("# 🎙️ Voice Calendar Agent")
    gr.Markdown("**AI-powered calendar scheduling with natural language**")
    
    with gr.Row():
        gr.Markdown("[🔑 Login with Google](/login)")
        gr.Markdown("[🚪 Logout](/logout)")

    chatbot = gr.Chatbot(height=450, show_label=False)
    
    with gr.Row():
        msg = gr.Textbox(
            label="Message",
            placeholder="Schedule a meeting with Bob tomorrow at 2 PM...",
            show_label=False,
            scale=9
        )
        send = gr.Button("Send", scale=1, variant="primary")
    
    clear = gr.Button("Reset Conversation", variant="secondary")

    gr.Markdown("### 💡 Try saying:")
    gr.Examples(
        examples=[
            "Schedule a meeting with Bob tomorrow at 2 PM",
            "Book a call with Sarah on Friday at 10:30 AM",
            "Create an appointment with Dr. Smith next Monday at 9 AM"
        ],
        inputs=msg
    )

    send.click(chat, [msg, chatbot], [chatbot, msg])
    msg.submit(chat, [msg, chatbot], [chatbot, msg])
    clear.click(reset_conversation, None, [chatbot, msg])

app = gr.mount_gradio_app(app, demo, path="/")

# ================== STARTUP ==================

@app.on_event("startup")
async def startup():
    init_db()
    print("✅ Voice Calendar Agent started!")
    print(f"📍 Redirect URI: {REDIRECT_URI}")

# ================== START ==================

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))

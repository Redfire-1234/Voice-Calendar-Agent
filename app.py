# """
# Voice Calendar Agent - Slot-Filling State Machine (FIXED)
# Uses Gradio State component for proper session management
# """

# import os
# import json
# import datetime
# import re
# from typing import Optional, Dict

# import gradio as gr
# from fastapi import FastAPI, Request
# from fastapi.responses import RedirectResponse
# from starlette.middleware.sessions import SessionMiddleware

# import psycopg2
# from psycopg2.extras import RealDictCursor

# from google_auth_oauthlib.flow import Flow
# from google.oauth2.credentials import Credentials
# from googleapiclient.discovery import build
# from google.auth.transport.requests import Request as GoogleRequest

# from dateutil import parser
# import pytz

# from groq import Groq

# # ================== ENV ==================

# DATABASE_URL = os.getenv("DATABASE_URL")
# GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
# GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET")
# SESSION_SECRET = os.getenv("SESSION_SECRET", "change-me")
# GROQ_API_KEY = os.getenv("GROQ_API_KEY")

# groq_client = Groq(api_key=GROQ_API_KEY)

# SCOPES = [
#     "https://www.googleapis.com/auth/calendar",
#     "openid",
#     "https://www.googleapis.com/auth/userinfo.email"
# ]

# REDIRECT_URI = os.getenv("REDIRECT_URI", "https://voice-calendar-agent.onrender.com/oauth2callback")

# # ================== FASTAPI ==================

# app = FastAPI()
# app.add_middleware(SessionMiddleware, secret_key=SESSION_SECRET)

# # ================== DATABASE ==================

# def init_db():
#     try:
#         with get_db() as conn:
#             with conn.cursor() as cur:
#                 cur.execute("""
#                     CREATE TABLE IF NOT EXISTS user_tokens (
#                         user_id VARCHAR(255) PRIMARY KEY,
#                         email VARCHAR(255) NOT NULL,
#                         access_token TEXT NOT NULL,
#                         refresh_token TEXT,
#                         expiry TIMESTAMP
#                     )
#                 """)
#                 print("✅ Database initialized")
#     except Exception as e:
#         print(f"❌ DB init error: {e}")

# def get_db():
#     return psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)

# def save_tokens(user_id, email, creds: Credentials):
#     with get_db() as conn:
#         with conn.cursor() as cur:
#             cur.execute("""
#                 INSERT INTO user_tokens (user_id, email, access_token, refresh_token, expiry)
#                 VALUES (%s,%s,%s,%s,%s)
#                 ON CONFLICT (user_id)
#                 DO UPDATE SET
#                   email = EXCLUDED.email,
#                   access_token = EXCLUDED.access_token,
#                   refresh_token = COALESCE(EXCLUDED.refresh_token, user_tokens.refresh_token),
#                   expiry = EXCLUDED.expiry
#             """, (
#                 user_id,
#                 email,
#                 creds.token,
#                 creds.refresh_token,
#                 creds.expiry
#             ))

# def load_tokens(user_id) -> Optional[Credentials]:
#     with get_db() as conn:
#         with conn.cursor() as cur:
#             cur.execute("SELECT * FROM user_tokens WHERE user_id=%s", (user_id,))
#             row = cur.fetchone()

#     if not row:
#         return None

#     creds = Credentials(
#         token=row["access_token"],
#         refresh_token=row["refresh_token"],
#         token_uri="https://oauth2.googleapis.com/token",
#         client_id=GOOGLE_CLIENT_ID,
#         client_secret=GOOGLE_CLIENT_SECRET,
#         scopes=SCOPES,
#     )
#     creds.expiry = row["expiry"]
#     return creds

# # ================== GOOGLE OAUTH ==================

# @app.get("/login")
# def login(request: Request):
#     flow = Flow.from_client_config(
#         {
#             "web": {
#                 "client_id": GOOGLE_CLIENT_ID,
#                 "client_secret": GOOGLE_CLIENT_SECRET,
#                 "auth_uri": "https://accounts.google.com/o/oauth2/auth",
#                 "token_uri": "https://oauth2.googleapis.com/token",
#             }
#         },
#         scopes=SCOPES,
#         redirect_uri=REDIRECT_URI,
#     )

#     auth_url, state = flow.authorization_url(
#         access_type="offline",
#         prompt="consent",
#     )

#     request.session["state"] = state
#     return RedirectResponse(auth_url)

# @app.get("/oauth2callback")
# def oauth2callback(request: Request):
#     try:
#         state = request.session.get("state")
        
#         flow = Flow.from_client_config(
#             {
#                 "web": {
#                     "client_id": GOOGLE_CLIENT_ID,
#                     "client_secret": GOOGLE_CLIENT_SECRET,
#                     "auth_uri": "https://accounts.google.com/o/oauth2/auth",
#                     "token_uri": "https://oauth2.googleapis.com/token",
#                 }
#             },
#             scopes=SCOPES,
#             state=state,
#             redirect_uri=REDIRECT_URI,
#         )

#         flow.fetch_token(authorization_response=str(request.url))
#         creds = flow.credentials

#         oauth = build("oauth2", "v2", credentials=creds)
#         user = oauth.userinfo().get().execute()

#         save_tokens(user["id"], user["email"], creds)
        
#         request.session["user_id"] = user["id"]
#         request.session["email"] = user["email"]

#         print(f"✅ User {user['email']} authenticated")
#         return RedirectResponse("/")
        
#     except Exception as e:
#         print(f"❌ OAuth error: {e}")
#         return RedirectResponse(f"/?error={str(e)}")

# @app.get("/logout")
# def logout(request: Request):
#     request.session.clear()
#     return RedirectResponse("/")

# # ================== CALENDAR SERVICE ==================

# def get_calendar_service(user_id):
#     creds = load_tokens(user_id)
#     if not creds:
#         raise Exception("User not authenticated. Please login.")

#     if creds.expired and creds.refresh_token:
#         creds.refresh(GoogleRequest())
#         with get_db() as conn:
#             with conn.cursor() as cur:
#                 cur.execute("SELECT email FROM user_tokens WHERE user_id=%s", (user_id,))
#                 row = cur.fetchone()
#                 email = row["email"] if row else ""
#         save_tokens(user_id, email, creds)
#     elif creds.expired:
#         raise Exception("Token expired. Please login again.")

#     return build("calendar", "v3", credentials=creds)

# # ================== CALENDAR FUNCTIONS ==================

# def parse_datetime(date_str, time_str):
#     india_tz = pytz.timezone('Asia/Kolkata')
#     today = datetime.datetime.now(india_tz)
    
#     date_str_lower = date_str.lower()
#     if "tomorrow" in date_str_lower:
#         target_date = today.date() + datetime.timedelta(days=1)
#     elif "today" in date_str_lower:
#         target_date = today.date()
#     else:
#         try:
#             parsed = parser.parse(date_str, fuzzy=True)
#             target_date = parsed.date()
#         except Exception:
#             target_date = today.date()
    
#     try:
#         time_parsed = parser.parse(time_str, fuzzy=True)
#         hour = time_parsed.hour
#         minute = time_parsed.minute
#     except Exception:
#         hour = 9
#         minute = 0
    
#     naive_dt = datetime.datetime.combine(target_date, datetime.time(hour=hour, minute=minute))
#     result = india_tz.localize(naive_dt)
#     return result


# def create_calendar_event(user_id, name, date_str, time_str, title=None):
#     try:
#         if not title:
#             title = f"Meeting with {name}"

#         start_aware = parse_datetime(date_str, time_str)
#         end_aware = start_aware + datetime.timedelta(hours=1)

#         service = get_calendar_service(user_id)

#         event = {
#             "summary": title,
#             "start": {"dateTime": start_aware.isoformat(), "timeZone": "Asia/Kolkata"},
#             "end": {"dateTime": end_aware.isoformat(), "timeZone": "Asia/Kolkata"},
#             "description": "Created by Calendar Agent"
#         }

#         result = service.events().insert(calendarId="primary", body=event).execute()
        
#         print(f"✅ Event created: {result['id']}")

#         return {
#             "success": True,
#             "message": f"✅ **{title}** scheduled for **{start_aware.strftime('%b %d at %I:%M %p')}**",
#             "link": result.get("htmlLink", "")
#         }

#     except Exception as e:
#         print(f"❌ Event creation error: {e}")
#         return {"success": False, "message": f"❌ Error: {e}"}

# # ================== SLOT FILLING STATE MACHINE ==================

# class SlotFillingStateMachine:
#     def __init__(self):
#         self.slots = {"name": None, "date": None, "time": None}
    
#     def update_slot(self, slot_name: str, value: str):
#         if slot_name in self.slots:
#             self.slots[slot_name] = value
#             print(f"✅ Slot updated: {slot_name} = {value}")
    
#     def get_slot(self, slot_name: str):
#         return self.slots.get(slot_name)
    
#     def all_slots_filled(self) -> bool:
#         return all(self.slots.values())
    
#     def get_missing_slots(self) -> list:
#         return [k for k, v in self.slots.items() if not v]
    
#     def to_dict(self) -> dict:
#         return {"slots": self.slots}
    
#     @classmethod
#     def from_dict(cls, data: dict):
#         machine = cls()
#         if data:
#             machine.slots = data.get("slots", {"name": None, "date": None, "time": None})
#         return machine

# # ================== SLOT EXTRACTORS ==================

# def extract_name_slot(text: str) -> Optional[str]:
#     text = text.lower().strip()
    
#     # Pattern 1: "with NAME"
#     match = re.search(r'with\s+(\w+)', text)
#     if match:
#         name = match.group(1)
#         if name not in ["today", "tomorrow", "at", "on", "the", "a"]:
#             print(f"  → Found name via 'with': {name}")
#             return name.capitalize()
    
#     # Pattern 2: "meeting NAME" or "schedule NAME"
#     match = re.search(r'(?:meeting|schedule|event)\s+(?:with\s+)?(\w+)', text)
#     if match:
#         name = match.group(1)
#         if name not in ["today", "tomorrow", "at", "on", "the", "a", "meeting", "with"]:
#             print(f"  → Found name via 'meeting/schedule': {name}")
#             return name.capitalize()
    
#     # Pattern 3: Just a single word (if user is answering "who?")
#     words = text.split()
#     if len(words) == 1 and len(words[0]) > 2:
#         if words[0] not in ["today", "tomorrow", "yes", "no", "ok", "sure"]:
#             print(f"  → Found name as single word: {words[0]}")
#             return words[0].capitalize()
    
#     print(f"  → No name found in: {text}")
#     return None


# def extract_date_slot(text: str) -> Optional[str]:
#     text = text.lower().strip()
    
#     if "today" in text:
#         return "today"
#     if "tomorrow" in text:
#         return "tomorrow"
    
#     days = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
#     for day in days:
#         if day in text:
#             return day
    
#     return None


# def extract_time_slot(text: str) -> Optional[str]:
#     text = text.lower().strip()
    
#     time_patterns = [
#         r'\d{1,2}\s*(?:am|pm)',
#         r'\d{1,2}:\d{2}\s*(?:am|pm)?',
#         r'\d{1,2}\s+o\'?clock'
#     ]
    
#     for pattern in time_patterns:
#         match = re.search(pattern, text)
#         if match:
#             time_str = match.group()
#             if "clock" in time_str:
#                 hour = re.search(r'\d{1,2}', time_str).group()
#                 time_str = f"{hour} PM"
#             return time_str
    
#     return None

# # ================== DIALOGUE MANAGER ==================

# def generate_prompt(state_machine: SlotFillingStateMachine) -> str:
#     missing = state_machine.get_missing_slots()
    
#     if not missing:
#         return None
    
#     if len(missing) == 3:
#         return "Who would you like to meet with, and when?"
#     elif len(missing) == 2:
#         if "name" in missing and "date" in missing:
#             return "Who would you like to meet with, and on what date?"
#         elif "name" in missing and "time" in missing:
#             return "Who would you like to meet with, and at what time?"
#         else:
#             return "When? (date and time)"
#     else:
#         slot_prompts = {
#             "name": "Who would you like to meet with?",
#             "date": "What date?",
#             "time": "What time?"
#         }
#         return slot_prompts.get(missing[0])

# # ================== CHAT HANDLER (FIXED WITH GRADIO STATE) ==================

# def chat(user_message, history, state_dict, request: gr.Request):
#     """
#     KEY FIX: Use Gradio State component instead of request.session
#     state_dict is passed in and returned - this persists across turns
#     """
#     if not user_message or not isinstance(user_message, str) or not user_message.strip():
#         return history, "", state_dict

#     user_id = request.session.get("user_id")

#     if not user_id:
#         history.append({"role": "assistant", "content": "🔐 Please login: [Login with Google](/login)"})
#         return history, "", state_dict

#     try:
#         user_lower = user_message.lower().strip()
        
#         if user_lower in ["hi", "hello", "hey"]:
#             history.append({"role": "user", "content": user_message})
#             history.append({"role": "assistant", "content": "Hi! What would you like to schedule?"})
#             return history, "", state_dict
        
#         if any(word in user_lower for word in ["thanks", "thank you"]):
#             history.append({"role": "user", "content": user_message})
#             history.append({"role": "assistant", "content": "You're welcome!"})
#             return history, "", {}  # Reset state
        
#         # Load state machine from Gradio State
#         state_machine = SlotFillingStateMachine.from_dict(state_dict)
        
#         print(f"📊 Current slots: {state_machine.slots}")
        
#         name = extract_name_slot(user_message)
#         date = extract_date_slot(user_message)
#         time = extract_time_slot(user_message)
        
#         print(f"🔍 Extracted from '{user_message}': name={name}, date={date}, time={time}")
        
#         # Update slots - DON'T overwrite if already filled
#         if name and not state_machine.get_slot("name"):
#             state_machine.update_slot("name", name)
        
#         if date and not state_machine.get_slot("date"):
#             state_machine.update_slot("date", date)
        
#         if time and not state_machine.get_slot("time"):
#             state_machine.update_slot("time", time)
        
#         print(f"💾 Updated slots: {state_machine.slots}")
        
#         # Save state back to Gradio State
#         new_state_dict = state_machine.to_dict()
        
#         if state_machine.all_slots_filled():
#             result = create_calendar_event(
#                 user_id=user_id,
#                 name=state_machine.get_slot("name"),
#                 date_str=state_machine.get_slot("date"),
#                 time_str=state_machine.get_slot("time")
#             )
            
#             assistant_reply = result["message"]
#             if result.get("link"):
#                 assistant_reply += f"\n🔗 [View]({result['link']})"
            
#             history.append({"role": "user", "content": user_message})
#             history.append({"role": "assistant", "content": assistant_reply})
#             return history, "", {}  # Reset state after completion
        
#         prompt = generate_prompt(state_machine)
        
#         history.append({"role": "user", "content": user_message})
#         history.append({"role": "assistant", "content": prompt})
#         return history, "", new_state_dict

#     except Exception as e:
#         print(f"❌ Error: {e}")
#         history.append({"role": "user", "content": user_message})
#         history.append({"role": "assistant", "content": f"❌ Error: {str(e)}"})
#         return history, "", {}


# def reset_conversation():
#     return [], "", {}


# def transcribe_audio(audio_path):
#     if not audio_path:
#         return ""
#     try:
#         with open(audio_path, "rb") as file:
#             transcription = groq_client.audio.transcriptions.create(
#                 file=(audio_path, file.read()),
#                 model="whisper-large-v3-turbo",
#                 response_format="text"
#             )
#         return transcription
#     except Exception as e:
#         print(f"❌ Transcription error: {e}")
#         return ""

# # ================== GRADIO UI (FIXED) ==================

# with gr.Blocks(title="Voice Calendar Agent", theme=gr.themes.Soft()) as demo:
#     gr.Markdown("# 🎙️ Voice Calendar Agent")
    
#     with gr.Row():
#         gr.Markdown("[🔑 Login](/login)")
#         gr.Markdown("[🚪 Logout](/logout)")

#     # KEY FIX: Add State component to persist slot-filling state
#     state = gr.State(value={})
    
#     chatbot = gr.Chatbot(height=450, show_label=False)
    
#     with gr.Row():
#         msg = gr.Textbox(placeholder="Schedule meeting with Bob tomorrow at 2 PM", show_label=False, scale=8)
#         voice_btn = gr.Audio(sources=["microphone"], type="filepath", label="🎤", show_label=False, scale=1)
#         send = gr.Button("Send", scale=1, variant="primary")
    
#     with gr.Row():
#         record_again = gr.Button("🎤 Record Again", size="sm")
    
#     clear = gr.Button("Reset", variant="secondary")

#     gr.Examples(examples=["Schedule meeting with Bob tomorrow at 2 PM"], inputs=msg)

#     # KEY FIX: Pass state as input and output
#     send.click(chat, [msg, chatbot, state], [chatbot, msg, state])
#     msg.submit(chat, [msg, chatbot, state], [chatbot, msg, state])
#     clear.click(reset_conversation, None, [chatbot, msg, state])
#     voice_btn.change(transcribe_audio, voice_btn, msg)
#     record_again.click(lambda: None, None, voice_btn)

# app = gr.mount_gradio_app(app, demo, path="/")

# @app.on_event("startup")
# async def startup():
#     init_db()
#     print("✅ Calendar Agent with Slot-Filling State Machine (FIXED)!")

# if __name__ == "__main__":
#     import uvicorn
#     uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))

"""
Voice Calendar Agent - Slot-Filling + LLM Reasoning
Combines slot-filling with LLM intent classification
"""

import os
import json
import datetime
import re
from typing import Optional, Dict

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
    creds = load_tokens(user_id)
    if not creds:
        raise Exception("User not authenticated. Please login.")

    if creds.expired and creds.refresh_token:
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

# ================== CALENDAR FUNCTIONS ==================

def parse_datetime(date_str, time_str):
    india_tz = pytz.timezone('Asia/Kolkata')
    today = datetime.datetime.now(india_tz)
    
    date_str_lower = date_str.lower()
    if "tomorrow" in date_str_lower:
        target_date = today.date() + datetime.timedelta(days=1)
    elif "today" in date_str_lower:
        target_date = today.date()
    else:
        try:
            parsed = parser.parse(date_str, fuzzy=True)
            target_date = parsed.date()
        except Exception:
            target_date = today.date()
    
    try:
        time_parsed = parser.parse(time_str, fuzzy=True)
        hour = time_parsed.hour
        minute = time_parsed.minute
    except Exception:
        hour = 9
        minute = 0
    
    naive_dt = datetime.datetime.combine(target_date, datetime.time(hour=hour, minute=minute))
    result = india_tz.localize(naive_dt)
    return result


def create_calendar_event(user_id, name, date_str, time_str, title=None):
    try:
        if not title:
            title = f"Meeting with {name}"

        start_aware = parse_datetime(date_str, time_str)
        end_aware = start_aware + datetime.timedelta(hours=1)

        service = get_calendar_service(user_id)

        event = {
            "summary": title,
            "start": {"dateTime": start_aware.isoformat(), "timeZone": "Asia/Kolkata"},
            "end": {"dateTime": end_aware.isoformat(), "timeZone": "Asia/Kolkata"},
            "description": "Created by Calendar Agent"
        }

        result = service.events().insert(calendarId="primary", body=event).execute()
        
        print(f"✅ Event created: {result['id']}")

        return {
            "success": True,
            "message": f"✅ **{title}** scheduled for **{start_aware.strftime('%b %d at %I:%M %p')}**",
            "link": result.get("htmlLink", "")
        }

    except Exception as e:
        print(f"❌ Event creation error: {e}")
        return {"success": False, "message": f"❌ Error: {e}"}


def list_upcoming_events(user_id, max_results=10):
    """List upcoming calendar events"""
    try:
        service = get_calendar_service(user_id)
        india_tz = pytz.timezone('Asia/Kolkata')
        now = datetime.datetime.now(india_tz).isoformat()

        events_result = service.events().list(
            calendarId='primary',
            timeMin=now,
            maxResults=max_results,
            singleEvents=True,
            orderBy='startTime'
        ).execute()

        events = events_result.get('items', [])

        if not events:
            return "📅 No upcoming events found."

        response = "📅 **Upcoming Events:**\n\n"
        for event in events:
            start = event['start'].get('dateTime', event['start'].get('date'))
            summary = event.get('summary', 'No title')
            
            try:
                dt = parser.parse(start)
                formatted_time = dt.strftime('%b %d, %I:%M %p')
            except:
                formatted_time = start
            
            response += f"• **{summary}** - {formatted_time}\n"

        return response

    except Exception as e:
        print(f"❌ List events error: {e}")
        return f"❌ Error listing events: {e}"

# ================== INTENT CLASSIFICATION ==================

def classify_intent(user_message: str) -> dict:
    """Use LLM to classify user intent"""
    try:
        prompt = f"""You are a calendar assistant. Classify the user's intent.

User message: "{user_message}"

Respond ONLY with a JSON object (no markdown, no extra text):
{{
    "intent": "create_event" | "list_events" | "greeting" | "thanks" | "other",
    "confidence": 0.0-1.0
}}

Examples:
- "Schedule meeting with Bob tomorrow" -> {{"intent": "create_event", "confidence": 0.95}}
- "List my meetings" -> {{"intent": "list_events", "confidence": 0.9}}
- "Hi" -> {{"intent": "greeting", "confidence": 1.0}}
- "Thanks" -> {{"intent": "thanks", "confidence": 1.0}}
"""

        response = groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1,
            max_tokens=100
        )
        
        result = response.choices[0].message.content.strip()
        
        # Clean up any markdown formatting
        result = result.replace("```json", "").replace("```", "").strip()
        
        intent_data = json.loads(result)
        print(f"🎯 Intent classified: {intent_data}")
        return intent_data

    except Exception as e:
        print(f"❌ Intent classification error: {e}")
        return {"intent": "other", "confidence": 0.0}

# ================== SLOT FILLING STATE MACHINE ==================

class SlotFillingStateMachine:
    def __init__(self):
        self.slots = {"name": None, "date": None, "time": None}
        self.active = False  # Track if we're in slot-filling mode
    
    def activate(self):
        self.active = True
    
    def deactivate(self):
        self.active = False
        self.slots = {"name": None, "date": None, "time": None}
    
    def update_slot(self, slot_name: str, value: str):
        if slot_name in self.slots:
            self.slots[slot_name] = value
            print(f"✅ Slot updated: {slot_name} = {value}")
    
    def get_slot(self, slot_name: str):
        return self.slots.get(slot_name)
    
    def all_slots_filled(self) -> bool:
        return all(self.slots.values())
    
    def get_missing_slots(self) -> list:
        return [k for k, v in self.slots.items() if not v]
    
    def to_dict(self) -> dict:
        return {"slots": self.slots, "active": self.active}
    
    @classmethod
    def from_dict(cls, data: dict):
        machine = cls()
        if data:
            machine.slots = data.get("slots", {"name": None, "date": None, "time": None})
            machine.active = data.get("active", False)
        return machine

# ================== SLOT EXTRACTORS ==================

def extract_name_slot(text: str) -> Optional[str]:
    text = text.lower().strip()
    
    match = re.search(r'with\s+(\w+)', text)
    if match:
        name = match.group(1)
        if name not in ["today", "tomorrow", "at", "on", "the", "a"]:
            return name.capitalize()
    
    match = re.search(r'(?:meeting|schedule|event)\s+(?:with\s+)?(\w+)', text)
    if match:
        name = match.group(1)
        if name not in ["today", "tomorrow", "at", "on", "the", "a", "meeting", "with"]:
            return name.capitalize()
    
    words = text.split()
    if len(words) == 1 and len(words[0]) > 2:
        if words[0] not in ["today", "tomorrow", "yes", "no", "ok", "sure"]:
            return words[0].capitalize()
    
    return None


def extract_date_slot(text: str) -> Optional[str]:
    text = text.lower().strip()
    
    if "today" in text:
        return "today"
    if "tomorrow" in text:
        return "tomorrow"
    
    days = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
    for day in days:
        if day in text:
            return day
    
    return None


def extract_time_slot(text: str) -> Optional[str]:
    text = text.lower().strip()
    
    time_patterns = [
        r'\d{1,2}\s*(?:am|pm)',
        r'\d{1,2}:\d{2}\s*(?:am|pm)?',
        r'\d{1,2}\s+o\'?clock'
    ]
    
    for pattern in time_patterns:
        match = re.search(pattern, text)
        if match:
            time_str = match.group()
            if "clock" in time_str:
                hour = re.search(r'\d{1,2}', time_str).group()
                time_str = f"{hour} PM"
            return time_str
    
    return None

# ================== DIALOGUE MANAGER ==================

def generate_prompt(state_machine: SlotFillingStateMachine) -> str:
    missing = state_machine.get_missing_slots()
    
    if not missing:
        return None
    
    if len(missing) == 3:
        return "Who would you like to meet with, and when?"
    elif len(missing) == 2:
        if "name" in missing and "date" in missing:
            return "Who would you like to meet with, and on what date?"
        elif "name" in missing and "time" in missing:
            return "Who would you like to meet with, and at what time?"
        else:
            return "When? (date and time)"
    else:
        slot_prompts = {
            "name": "Who would you like to meet with?",
            "date": "What date?",
            "time": "What time?"
        }
        return slot_prompts.get(missing[0])

# ================== CHAT HANDLER WITH REASONING ==================

def chat(user_message, history, state_dict, request: gr.Request):
    """Enhanced chat with intent classification + slot filling"""
    if not user_message or not isinstance(user_message, str) or not user_message.strip():
        return history, "", state_dict

    user_id = request.session.get("user_id")

    if not user_id:
        history.append({"role": "assistant", "content": "🔐 Please login: [Login with Google](/login)"})
        return history, "", state_dict

    try:
        # Load state machine
        state_machine = SlotFillingStateMachine.from_dict(state_dict)
        
        # If we're already in slot-filling mode, continue filling slots
        if state_machine.active:
            print(f"📊 Continuing slot-filling. Current slots: {state_machine.slots}")
            
            name = extract_name_slot(user_message)
            date = extract_date_slot(user_message)
            time = extract_time_slot(user_message)
            
            if name and not state_machine.get_slot("name"):
                state_machine.update_slot("name", name)
            
            if date and not state_machine.get_slot("date"):
                state_machine.update_slot("date", date)
            
            if time and not state_machine.get_slot("time"):
                state_machine.update_slot("time", time)
            
            new_state_dict = state_machine.to_dict()
            
            if state_machine.all_slots_filled():
                result = create_calendar_event(
                    user_id=user_id,
                    name=state_machine.get_slot("name"),
                    date_str=state_machine.get_slot("date"),
                    time_str=state_machine.get_slot("time")
                )
                
                state_machine.deactivate()
                
                assistant_reply = result["message"]
                if result.get("link"):
                    assistant_reply += f"\n🔗 [View Event]({result['link']})"
                
                history.append({"role": "user", "content": user_message})
                history.append({"role": "assistant", "content": assistant_reply})
                return history, "", {}
            
            prompt = generate_prompt(state_machine)
            history.append({"role": "user", "content": user_message})
            history.append({"role": "assistant", "content": prompt})
            return history, "", new_state_dict
        
        # Not in slot-filling mode - classify intent
        intent_data = classify_intent(user_message)
        intent = intent_data.get("intent", "other")
        
        if intent == "greeting":
            history.append({"role": "user", "content": user_message})
            history.append({"role": "assistant", "content": "Hi! I can help you schedule meetings or list your upcoming events. What would you like to do?"})
            return history, "", state_dict
        
        elif intent == "thanks":
            history.append({"role": "user", "content": user_message})
            history.append({"role": "assistant", "content": "You're welcome! 😊"})
            return history, "", {}
        
        elif intent == "list_events":
            events_list = list_upcoming_events(user_id)
            history.append({"role": "user", "content": user_message})
            history.append({"role": "assistant", "content": events_list})
            return history, "", state_dict
        
        elif intent == "create_event":
            # Activate slot-filling mode
            state_machine.activate()
            
            # Try to extract slots from the initial message
            name = extract_name_slot(user_message)
            date = extract_date_slot(user_message)
            time = extract_time_slot(user_message)
            
            if name:
                state_machine.update_slot("name", name)
            if date:
                state_machine.update_slot("date", date)
            if time:
                state_machine.update_slot("time", time)
            
            new_state_dict = state_machine.to_dict()
            
            # Check if all slots are already filled
            if state_machine.all_slots_filled():
                result = create_calendar_event(
                    user_id=user_id,
                    name=state_machine.get_slot("name"),
                    date_str=state_machine.get_slot("date"),
                    time_str=state_machine.get_slot("time")
                )
                
                assistant_reply = result["message"]
                if result.get("link"):
                    assistant_reply += f"\n🔗 [View Event]({result['link']})"
                
                history.append({"role": "user", "content": user_message})
                history.append({"role": "assistant", "content": assistant_reply})
                return history, "", {}
            
            # Ask for missing slots
            prompt = generate_prompt(state_machine)
            history.append({"role": "user", "content": user_message})
            history.append({"role": "assistant", "content": prompt})
            return history, "", new_state_dict
        
        else:
            # Unknown intent
            history.append({"role": "user", "content": user_message})
            history.append({"role": "assistant", "content": "I can help you:\n• Schedule meetings\n• List upcoming events\n\nWhat would you like to do?"})
            return history, "", state_dict

    except Exception as e:
        print(f"❌ Error: {e}")
        history.append({"role": "user", "content": user_message})
        history.append({"role": "assistant", "content": f"❌ Error: {str(e)}"})
        return history, "", {}


def reset_conversation():
    return [], "", {}


def transcribe_audio(audio_path):
    if not audio_path:
        return ""
    try:
        with open(audio_path, "rb") as file:
            transcription = groq_client.audio.transcriptions.create(
                file=(audio_path, file.read()),
                model="whisper-large-v3-turbo",
                response_format="text"
            )
        return transcription
    except Exception as e:
        print(f"❌ Transcription error: {e}")
        return ""

# ================== GRADIO UI ==================

with gr.Blocks(title="Voice Calendar Agent", theme=gr.themes.Soft()) as demo:
    gr.Markdown("# 🎙️ Voice Calendar Agent with AI Reasoning")
    gr.Markdown("*Ask me to schedule meetings, list events, or manage your calendar!*")
    
    with gr.Row():
        gr.Markdown("[🔑 Login](/login)")
        gr.Markdown("[🚪 Logout](/logout)")

    state = gr.State(value={})
    
    chatbot = gr.Chatbot(height=450, show_label=False)
    
    with gr.Row():
        msg = gr.Textbox(placeholder="Try: 'List my meetings' or 'Schedule meeting with Bob tomorrow at 2 PM'", show_label=False, scale=8)
        voice_btn = gr.Audio(sources=["microphone"], type="filepath", label="🎤", show_label=False, scale=1)
        send = gr.Button("Send", scale=1, variant="primary")
    
    with gr.Row():
        record_again = gr.Button("🎤 Record Again", size="sm")
    
    clear = gr.Button("Reset", variant="secondary")

    gr.Examples(
        examples=[
            "List my upcoming meetings",
            "Schedule meeting with Bob tomorrow at 2 PM",
            "Schedule a meeting"
        ], 
        inputs=msg
    )

    send.click(chat, [msg, chatbot, state], [chatbot, msg, state])
    msg.submit(chat, [msg, chatbot, state], [chatbot, msg, state])
    clear.click(reset_conversation, None, [chatbot, msg, state])
    voice_btn.change(transcribe_audio, voice_btn, msg)
    record_again.click(lambda: None, None, voice_btn)

app = gr.mount_gradio_app(app, demo, path="/")

@app.on_event("startup")
async def startup():
    init_db()
    print("✅ Calendar Agent with AI Reasoning + Slot-Filling!")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))

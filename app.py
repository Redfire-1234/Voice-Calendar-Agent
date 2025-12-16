"""
Voice Calendar Agent - Full Featured with CRUD Operations
Supports: Create, Read, Update, Delete with exceptions, Advanced date/time parsing
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
    """Enhanced datetime parsing supporting multiple date formats"""
    india_tz = pytz.timezone('Asia/Kolkata')
    today = datetime.datetime.now(india_tz)
    
    date_str_lower = date_str.lower()
    
    if "tomorrow" in date_str_lower:
        target_date = today.date() + datetime.timedelta(days=1)
    elif "today" in date_str_lower:
        target_date = today.date()
    else:
        try:
            temp_str = date_str
            year_match = re.search(r'\b(\d{2})\b$', date_str)
            if year_match:
                two_digit_year = int(year_match.group(1))
                if two_digit_year < 50:
                    four_digit_year = 2000 + two_digit_year
                else:
                    four_digit_year = 1900 + two_digit_year
                temp_str = date_str.replace(year_match.group(1), str(four_digit_year))
            
            parsed = parser.parse(temp_str, fuzzy=True, default=today.replace(year=today.year))
            target_date = parsed.date()
            
            if parsed.year == today.year and target_date < today.date():
                target_date = target_date.replace(year=today.year + 1)
                
        except Exception as e:
            print(f"Date parsing failed for '{date_str}': {e}, using today")
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


def list_upcoming_events(user_id, max_results=10, return_raw=False):
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

        if return_raw:
            return events

        if not events:
            return "📅 No upcoming events found."

        response = "📅 **Upcoming Events:**\n\n"
        for idx, event in enumerate(events, 1):
            start = event['start'].get('dateTime', event['start'].get('date'))
            summary = event.get('summary', 'No title')
            
            try:
                dt = parser.parse(start)
                formatted_time = dt.strftime('%b %d, %I:%M %p')
            except:
                formatted_time = start
            
            response += f"{idx}. **{summary}** - {formatted_time}\n"

        return response

    except Exception as e:
        print(f"❌ List events error: {e}")
        return f"❌ Error listing events: {e}"


def update_event_time(user_id, criteria_type, criteria_value, time_change_type, time_amount):
    """Update event time - postpone or prepone"""
    try:
        service = get_calendar_service(user_id)
        india_tz = pytz.timezone('Asia/Kolkata')
        now = datetime.datetime.now(india_tz)
        
        events = list_upcoming_events(user_id, max_results=50, return_raw=True)
        
        if not events:
            return "📅 No upcoming events to update."
        
        updated_count = 0
        updated_details = []
        
        # Find matching event(s)
        matching_events = []
        
        if criteria_type == "name":
            search_term = criteria_value.lower().strip()
            for event in events:
                summary = event.get('summary', '').lower()
                if search_term in summary:
                    matching_events.append(event)
        
        elif criteria_type == "time":
            target_time = criteria_value.lower().strip()
            for event in events:
                start = event['start'].get('dateTime', event['start'].get('date'))
                try:
                    dt = parser.parse(start)
                    event_time = dt.strftime('%I:%M %p').lower()
                    event_time_24 = dt.strftime('%H:%M')
                    
                    try:
                        target_dt = parser.parse(target_time, fuzzy=True)
                        target_formatted = target_dt.strftime('%I:%M %p').lower()
                        target_24 = target_dt.strftime('%H:%M')
                        
                        if event_time == target_formatted or event_time_24 == target_24:
                            matching_events.append(event)
                    except:
                        pass
                except:
                    pass
        
        elif criteria_type == "date":
            date_str = criteria_value.lower().strip()
            for event in events:
                start = event['start'].get('dateTime', event['start'].get('date'))
                try:
                    event_dt = parser.parse(start)
                    
                    if date_str == 'today':
                        if event_dt.date() == now.date():
                            matching_events.append(event)
                    elif date_str == 'tomorrow':
                        tomorrow = now.date() + datetime.timedelta(days=1)
                        if event_dt.date() == tomorrow:
                            matching_events.append(event)
                    else:
                        try:
                            target_date_str = date_str
                            year_match = re.search(r'\b(\d{2})\b$', target_date_str)
                            if year_match:
                                two_digit_year = int(year_match.group(1))
                                four_digit_year = 2000 + two_digit_year if two_digit_year < 50 else 1900 + two_digit_year
                                target_date_str = target_date_str.replace(year_match.group(1), str(four_digit_year))
                            
                            target_dt = parser.parse(target_date_str, fuzzy=True)
                            if event_dt.date() == target_dt.date():
                                matching_events.append(event)
                        except:
                            pass
                except:
                    pass
        
        elif criteria_type == "next":
            if events:
                matching_events.append(events[0])
        
        if not matching_events:
            return f"❌ No events found matching '{criteria_value}'."
        
        # Update the matched events
        for event in matching_events:
            try:
                event_id = event['id']
                start = event['start'].get('dateTime')
                end = event['end'].get('dateTime')
                
                if not start or not end:
                    continue
                
                start_dt = parser.parse(start)
                end_dt = parser.parse(end)
                
                # Calculate time delta
                if time_change_type == "postpone":
                    delta = datetime.timedelta(hours=time_amount)
                else:  # prepone
                    delta = datetime.timedelta(hours=-time_amount)
                
                new_start = start_dt + delta
                new_end = end_dt + delta
                
                # Update the event
                event['start']['dateTime'] = new_start.isoformat()
                event['end']['dateTime'] = new_end.isoformat()
                
                updated_event = service.events().update(
                    calendarId='primary',
                    eventId=event_id,
                    body=event
                ).execute()
                
                updated_count += 1
                summary = event.get('summary', 'Untitled')
                old_time = start_dt.strftime('%b %d at %I:%M %p')
                new_time = new_start.strftime('%b %d at %I:%M %p')
                updated_details.append(f"• **{summary}**: {old_time} → {new_time}")
                
            except Exception as e:
                print(f"Error updating event {event.get('id')}: {e}")
        
        if updated_count > 0:
            action = "Postponed" if time_change_type == "postpone" else "Preponed"
            response = f"⏰ {action} **{updated_count}** event(s) by {time_amount} hour(s):\n\n"
            response += "\n".join(updated_details)
            return response
        else:
            return "❌ Failed to update events."
        
    except Exception as e:
        print(f"❌ Update error: {e}")
        return f"❌ Error updating events: {e}"


def delete_event_by_criteria(user_id, criteria_type, criteria_value, except_criteria=None):
    """Delete events based on criteria with optional exceptions"""
    try:
        service = get_calendar_service(user_id)
        india_tz = pytz.timezone('Asia/Kolkata')
        now = datetime.datetime.now(india_tz)
        
        events = list_upcoming_events(user_id, max_results=50, return_raw=True)
        
        if not events:
            return "📅 No upcoming events to delete."
        
        deleted_count = 0
        skipped_count = 0
        deleted_names = []
        skipped_names = []
        
        def should_skip_event(event):
            if not except_criteria:
                return False
            
            except_type = except_criteria.get('type')
            except_value = except_criteria.get('value', '').lower().strip()
            
            if except_type == 'name':
                summary = event.get('summary', '').lower()
                if except_value in summary:
                    return True
            
            elif except_type == 'date':
                start = event['start'].get('dateTime', event['start'].get('date'))
                try:
                    event_dt = parser.parse(start)
                    
                    if except_value == 'today':
                        if event_dt.date() == now.date():
                            return True
                    elif except_value == 'tomorrow':
                        tomorrow = now.date() + datetime.timedelta(days=1)
                        if event_dt.date() == tomorrow:
                            return True
                    else:
                        try:
                            except_date_str = except_value
                            year_match = re.search(r'\b(\d{2})\b$', except_date_str)
                            if year_match:
                                two_digit_year = int(year_match.group(1))
                                four_digit_year = 2000 + two_digit_year if two_digit_year < 50 else 1900 + two_digit_year
                                except_date_str = except_date_str.replace(year_match.group(1), str(four_digit_year))
                            
                            except_dt = parser.parse(except_date_str, fuzzy=True)
                            if event_dt.date() == except_dt.date():
                                return True
                        except:
                            pass
                except:
                    pass
            
            return False
        
        if criteria_type == "all":
            for event in events:
                if should_skip_event(event):
                    skipped_count += 1
                    skipped_names.append(event.get('summary', 'Untitled'))
                    continue
                
                try:
                    service.events().delete(calendarId='primary', eventId=event['id']).execute()
                    deleted_count += 1
                    deleted_names.append(event.get('summary', 'Untitled'))
                except Exception as e:
                    print(f"Error deleting event {event['id']}: {e}")
            
            response = f"🗑️ Deleted **{deleted_count}** upcoming events."
            if skipped_count > 0:
                response += f"\n✅ Kept **{skipped_count}** events as requested:\n" + "\n".join([f"• {name}" for name in skipped_names])
            return response
        
        elif criteria_type == "time":
            target_time = criteria_value.lower().strip()
            
            for event in events:
                if should_skip_event(event):
                    skipped_count += 1
                    skipped_names.append(event.get('summary', 'Untitled'))
                    continue
                
                start = event['start'].get('dateTime', event['start'].get('date'))
                try:
                    dt = parser.parse(start)
                    event_time = dt.strftime('%I:%M %p').lower()
                    event_time_24 = dt.strftime('%H:%M')
                    
                    try:
                        target_dt = parser.parse(target_time, fuzzy=True)
                        target_formatted = target_dt.strftime('%I:%M %p').lower()
                        target_24 = target_dt.strftime('%H:%M')
                        
                        if event_time == target_formatted or event_time_24 == target_24:
                            service.events().delete(calendarId='primary', eventId=event['id']).execute()
                            deleted_count += 1
                            deleted_names.append(event.get('summary', 'Untitled'))
                    except:
                        pass
                except:
                    pass
            
            if deleted_count > 0:
                response = f"🗑️ Deleted **{deleted_count}** event(s) at {criteria_value}:\n" + "\n".join([f"• {name}" for name in deleted_names])
                if skipped_count > 0:
                    response += f"\n✅ Kept **{skipped_count}** events as requested"
                return response
            else:
                return f"❌ No events found at {criteria_value}."
        
        elif criteria_type == "name":
            search_term = criteria_value.lower().strip()
            
            for event in events:
                if should_skip_event(event):
                    skipped_count += 1
                    skipped_names.append(event.get('summary', 'Untitled'))
                    continue
                
                summary = event.get('summary', '').lower()
                if search_term in summary:
                    service.events().delete(calendarId='primary', eventId=event['id']).execute()
                    deleted_count += 1
                    deleted_names.append(event.get('summary', 'Untitled'))
            
            if deleted_count > 0:
                response = f"🗑️ Deleted **{deleted_count}** event(s) matching '{criteria_value}':\n" + "\n".join([f"• {name}" for name in deleted_names])
                if skipped_count > 0:
                    response += f"\n✅ Kept **{skipped_count}** events as requested"
                return response
            else:
                return f"❌ No events found matching '{criteria_value}'."
        
        return "❌ Invalid delete criteria."
        
    except Exception as e:
        print(f"❌ Delete error: {e}")
        return f"❌ Error deleting events: {e}"

# ================== INTENT CLASSIFICATION ==================

def classify_intent(user_message: str) -> dict:
    """Use LLM to classify user intent"""
    try:
        prompt = f"""You are a calendar assistant. Classify the user's intent.

User message: "{user_message}"

Respond ONLY with a JSON object (no markdown, no extra text):
{{
    "intent": "create_event" | "list_events" | "delete_event" | "update_event" | "greeting" | "thanks" | "other",
    "confidence": 0.0-1.0
}}

Intent guidelines:
- "delete_event" includes: cancel, delete, remove events
- "create_event" includes: schedule, create, book, set up meetings
- "list_events" includes: show, list, what's on calendar, upcoming
- "update_event" includes: postpone, prepone, reschedule, delay, advance, move forward, move back

Examples:
- "Schedule meeting with Bob tomorrow" -> {{"intent": "create_event", "confidence": 0.95}}
- "List my meetings" -> {{"intent": "list_events", "confidence": 0.9}}
- "Cancel meeting at 2 PM" -> {{"intent": "delete_event", "confidence": 0.9}}
- "Delete all events" -> {{"intent": "delete_event", "confidence": 0.95}}
- "Postpone meeting by 2 hours" -> {{"intent": "update_event", "confidence": 0.95}}
- "Prepone tomorrow's meeting by 1 hour" -> {{"intent": "update_event", "confidence": 0.95}}
- "Hi" -> {{"intent": "greeting", "confidence": 1.0}}
"""

        response = groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1,
            max_tokens=100
        )
        
        result = response.choices[0].message.content.strip()
        result = result.replace("```json", "").replace("```", "").strip()
        
        intent_data = json.loads(result)
        print(f"🎯 Intent classified: {intent_data}")
        return intent_data

    except Exception as e:
        print(f"❌ Intent classification error: {e}")
        return {"intent": "other", "confidence": 0.0}


def extract_update_criteria(user_message: str) -> dict:
    """Extract update/reschedule criteria from user message"""
    try:
        prompt = f"""Extract update criteria from the user's message about postponing or preponing events.

User message: "{user_message}"

Respond ONLY with a JSON object (no markdown):
{{
    "action": "postpone" | "prepone",
    "criteria_type": "name" | "time" | "date" | "next",
    "criteria_value": "meeting name or time or date" | null,
    "time_amount": number (hours as integer or float)
}}

Examples:
- "Postpone meeting with Bob by 2 hours" -> {{"action": "postpone", "criteria_type": "name", "criteria_value": "Bob", "time_amount": 2}}
- "Prepone 2 PM meeting by 1 hour" -> {{"action": "prepone", "criteria_type": "time", "criteria_value": "2 PM", "time_amount": 1}}
- "Postpone tomorrow's meeting by 3 hours" -> {{"action": "postpone", "criteria_type": "date", "criteria_value": "tomorrow", "time_amount": 3}}
- "Prepone next meeting by 30 minutes" -> {{"action": "prepone", "criteria_type": "next", "criteria_value": null, "time_amount": 0.5}}
- "Delay meeting at 6 o'clock by 1 hour" -> {{"action": "postpone", "criteria_type": "time", "criteria_value": "6 o'clock", "time_amount": 1}}
"""

        response = groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1,
            max_tokens=150
        )
        
        result = response.choices[0].message.content.strip()
        result = result.replace("```json", "").replace("```", "").strip()
        
        criteria = json.loads(result)
        print(f"🔍 Update criteria: {criteria}")
        return criteria

    except Exception as e:
        print(f"❌ Criteria extraction error: {e}")
        return {"action": None, "criteria_type": None, "criteria_value": None, "time_amount": 0}


def extract_delete_criteria(user_message: str) -> dict:
    """Extract what to delete from user message, including exceptions"""
    try:
        prompt = f"""Extract deletion criteria from the user's message, including any exceptions.

User message: "{user_message}"

Respond ONLY with a JSON object (no markdown):
{{
    "type": "all" | "time" | "name",
    "value": "specific time or name" | null,
    "except": {{
        "type": "name" | "date" | null,
        "value": "exception value" | null
    }}
}}

Examples:
- "Cancel all meetings" -> {{"type": "all", "value": null, "except": {{"type": null, "value": null}}}}
- "Delete event at 2 PM" -> {{"type": "time", "value": "2 PM", "except": {{"type": null, "value": null}}}}
- "Cancel all events except meeting with Aman" -> {{"type": "all", "value": null, "except": {{"type": "name", "value": "Aman"}}}}
- "Delete all meetings except today's" -> {{"type": "all", "value": null, "except": {{"type": "date", "value": "today"}}}}
- "Cancel all except tomorrow" -> {{"type": "all", "value": null, "except": {{"type": "date", "value": "tomorrow"}}}}
- "Remove all events except 16 Dec" -> {{"type": "all", "value": null, "except": {{"type": "date", "value": "16 Dec"}}}}
"""

        response = groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1,
            max_tokens=150
        )
        
        result = response.choices[0].message.content.strip()
        result = result.replace("```json", "").replace("```", "").strip()
        
        criteria = json.loads(result)
        print(f"🔍 Delete criteria: {criteria}")
        return criteria

    except Exception as e:
        print(f"❌ Criteria extraction error: {e}")
        return {"type": "other", "value": None, "except": {"type": None, "value": None}}

# ================== SLOT FILLING STATE MACHINE ==================

class SlotFillingStateMachine:
    def __init__(self):
        self.slots = {"name": None, "date": None, "time": None}
        self.active = False
    
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
    """Enhanced date extraction supporting multiple formats"""
    text = text.lower().strip()
    
    if "today" in text:
        return "today"
    if "tomorrow" in text:
        return "tomorrow"
    
    days = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
    for day in days:
        if day in text:
            return day
    
    date_patterns = [
        r'(\d{1,2})\s+(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec|january|february|march|april|may|june|july|august|september|october|november|december)(?:\s+(\d{2,4}))?',
        r'(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec|january|february|march|april|may|june|july|august|september|october|november|december)\s+(\d{1,2})(?:\s+(\d{2,4}))?',
        r'(\d{1,2})[/-](\d{1,2})(?:[/-](\d{2,4}))?'
    ]
    
    for pattern in date_patterns:
        match = re.search(pattern, text)
        if match:
            matched_text = match.group(0)
            print(f"Found date pattern: {matched_text}")
            return matched_text
    
    return None


def extract_time_slot(text: str) -> Optional[str]:
    """Enhanced time extraction supporting 6 o'clock, 6 o clock formats"""
    text = text.lower().strip()
    
    match = re.search(r'(\d{1,2})\s*o[\'\s]?clock', text)
    if match:
        hour = int(match.group(1))
        if 9 <= hour <= 11:
            return f"{hour} AM"
        elif hour == 12:
            return "12 PM"
        else:
            return f"{hour} PM"
    
    time_patterns = [
        r'(\d{1,2})\s*(am|pm)',
        r'(\d{1,2}):(\d{2})\s*(am|pm)?',
    ]
    
    for pattern in time_patterns:
        match = re.search(pattern, text)
        if match:
            if len(match.groups()) == 2 and match.group(2) in ['am', 'pm']:
                return f"{match.group(1)} {match.group(2).upper()}"
            elif len(match.groups()) == 3:
                hour = match.group(1)
                minute = match.group(2)
                period = match.group(3).upper() if match.group(3) else "PM"
                return f"{hour}:{minute} {period}"
            else:
                return match.group(0)
    
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

# ================== CHAT HANDLER ==================

def chat(user_message, history, state_dict, request: gr.Request):
    """Enhanced chat with intent classification + slot filling + delete + update support"""
    if not user_message or not isinstance(user_message, str) or not user_message.strip():
        return history, "", state_dict

    user_id = request.session.get("user_id")

    if not user_id:
        history.append({"role": "assistant", "content": "🔐 Please login: [Login with Google](/login)"})
        return history, "", state_dict

    try:
        state_machine = SlotFillingStateMachine.from_dict(state_dict)
        
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
        
        intent_data = classify_intent(user_message)
        intent = intent_data.get("intent", "other")
        
        if intent == "greeting":
            history.append({"role": "user", "content": user_message})
            history.append({"role": "assistant", "content": "Hi! I can help you schedule meetings, list events, cancel them, or reschedule them. What would you like to do?"})
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
        
        elif intent == "delete_event":
            criteria = extract_delete_criteria(user_message)
            
            except_criteria = criteria.get("except", {})
            if except_criteria.get("type") and except_criteria.get("value"):
                except_dict = {"type": except_criteria["type"], "value": except_criteria["value"]}
            else:
                except_dict = None
            
            result = delete_event_by_criteria(
                user_id=user_id,
                criteria_type=criteria.get("type", "other"),
                criteria_value=criteria.get("value"),
                except_criteria=except_dict
            )
            
            history.append({"role": "user", "content": user_message})
            history.append({"role": "assistant", "content": result})
            return history, "", state_dict
        
        elif intent == "update_event":
            criteria = extract_update_criteria(user_message)
            
            if criteria.get("action") and criteria.get("time_amount"):
                result = update_event_time(
                    user_id=user_id,
                    criteria_type=criteria.get("criteria_type", "next"),
                    criteria_value=criteria.get("criteria_value"),
                    time_change_type=criteria.get("action"),
                    time_amount=criteria.get("time_amount", 1)
                )
                
                history.append({"role": "user", "content": user_message})
                history.append({"role": "assistant", "content": result})
                return history, "", state_dict
            else:
                history.append({"role": "user", "content": user_message})
                history.append({"role": "assistant", "content": "❌ Could not understand the update request. Please specify which meeting to postpone/prepone and by how much time."})
                return history, "", state_dict
        
        elif intent == "create_event":
            state_machine.activate()
            
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
            
            prompt = generate_prompt(state_machine)
            history.append({"role": "user", "content": user_message})
            history.append({"role": "assistant", "content": prompt})
            return history, "", new_state_dict
        
        else:
            history.append({"role": "user", "content": user_message})
            history.append({"role": "assistant", "content": "I can help you:\n• 📅 Schedule meetings\n• 📋 List upcoming events\n• 🗑️ Cancel/delete events\n• ⏰ Postpone/prepone meetings\n\nWhat would you like to do?"})
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

custom_css = """
/* Main container */
.gradio-container {
    max-width: 900px !important;
    margin: auto !important;
}
.header-section {
    text-align: center;
    padding: 2rem 1rem 1rem 1rem;
    background: linear-gradient(135deg, #2c3e50 0%, #34495e 100%);
    border-radius: 16px;
    margin-bottom: 2rem;
    color: #ecf0f1;
    box-shadow: 0 8px 16px rgba(0,0,0,0.15);
}
.header-title {
    font-size: 2.5rem;
    font-weight: 700;
    margin-bottom: 0.5rem;
    text-shadow: 2px 2px 4px rgba(0,0,0,0.3);
    color: #ecf0f1;
}
.header-subtitle {
    font-size: 1.1rem;
    opacity: 0.9;
    color: #bdc3c7;
}
.auth-buttons {
    display: flex;
    gap: 1rem;
    justify-content: center;
    margin-top: 1rem;
}
.auth-link {
    padding: 0.5rem 1.5rem;
    background: rgba(236, 240, 241, 0.15);
    border-radius: 8px;
    text-decoration: none;
    color: #ecf0f1;
    font-weight: 500;
    transition: all 0.3s;
    backdrop-filter: blur(10px);
    border: 1px solid rgba(236, 240, 241, 0.2);
}
.auth-link:hover {
    background: rgba(236, 240, 241, 0.25);
    transform: translateY(-2px);
    box-shadow: 0 4px 8px rgba(0,0,0,0.2);
}
.chat-container {
    border-radius: 12px;
    overflow: hidden;
    box-shadow: 0 4px 12px rgba(0,0,0,0.08);
    border: 1px solid rgba(52, 73, 94, 0.1);
}
.input-row {
    margin-top: 1rem;
}
.example-section {
    margin-top: 1.5rem;
    padding: 1rem;
    background: rgba(44, 62, 80, 0.05);
    border-radius: 8px;
    border: 1px solid rgba(44, 62, 80, 0.1);
}
"""

with gr.Blocks(title="Voice Calendar Agent", theme=gr.themes.Soft(), css=custom_css) as demo:
    
    gr.HTML("""
        <div class="header-section">
            <div class="header-title">🎙️ Voice Calendar Agent</div>
            <div class="header-subtitle">Schedule, list, and cancel meetings with voice or text</div>
            <div class="auth-buttons">
                <a href="/login" class="auth-link">🔑 Login with Google</a>
                <a href="/logout" class="auth-link">🚪 Logout</a>
            </div>
        </div>
    """)

    state = gr.State(value={})
    
    with gr.Column(elem_classes="chat-container"):
        chatbot = gr.Chatbot(
            height=400, 
            show_label=False,
            avatar_images=(None, "https://api.dicebear.com/7.x/bottts/svg?seed=calendar")
        )
    
    with gr.Row(elem_classes="input-row"):
        with gr.Column(scale=8):
            msg = gr.Textbox(
                placeholder="Type your message or use voice input...", 
                show_label=False,
                container=False
            )
        with gr.Column(scale=1, min_width=60):
            voice_btn = gr.Audio(
                sources=["microphone"], 
                type="filepath", 
                label="🎤", 
                show_label=False,
                container=False
            )
    
    with gr.Row():
        send = gr.Button("📤 Send", variant="primary", scale=2)
        record_again = gr.Button("🎤 Record Again", variant="secondary", scale=1)
        clear = gr.Button("🔄 Reset", variant="secondary", scale=1)

    with gr.Accordion("📝 Example Commands", open=False, elem_classes="example-section"):
        gr.Examples(
            examples=[
                "📋 List my upcoming meetings",
                "📅 Schedule meeting with Bob on 16 December at 6 o'clock",
                "⏰ Book event on Dec 25 at 2 PM",
                "🗑️ Cancel all events except meeting with Aman",
                "❌ Delete all meetings except today's",
                "⏰ Postpone meeting with Bob by 2 hours",
                "⏰ Prepone tomorrow's meeting by 1 hour",
                "⏰ Delay next meeting by 30 minutes"
            ],
            inputs=msg,
            label=None
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
    print("✅ Calendar Agent with Full CRUD + Update Operations!")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))



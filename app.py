# """
# Voice Calendar Agent - OAuth 2.0 with Function Calling (Render Deployment)
# Combines web OAuth flow with Groq function calling like the desktop example.
# """

# import os
# import json
# import datetime
# from typing import Optional

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
# import tzlocal
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
#     """Get authenticated calendar service for user."""
#     creds = load_tokens(user_id)
#     if not creds:
#         raise Exception("User not authenticated. Please login.")

#     if creds.expired and creds.refresh_token:
#         print(f"🔄 Refreshing token for user {user_id}")
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
#     """Parse date and time strings into datetime object in India timezone."""
#     # Use India timezone
#     india_tz = pytz.timezone('Asia/Kolkata')
#     today = datetime.datetime.now(india_tz)
    
#     # Parse date
#     if "tomorrow" in date_str.lower():
#         target_date = today.date() + datetime.timedelta(days=1)
#     elif "today" in date_str.lower():
#         target_date = today.date()
#     else:
#         try:
#             parsed = parser.parse(date_str, fuzzy=True)
#             target_date = parsed.date()
#         except Exception:
#             target_date = today.date() + datetime.timedelta(days=1)
    
#     # Parse time - IMPORTANT: parse time independently
#     try:
#         # Parse time string independently to avoid date interference
#         time_parsed = parser.parse(time_str, fuzzy=True)
#         hour = time_parsed.hour
#         minute = time_parsed.minute
#     except Exception as e:
#         print(f"⚠️ Time parse error for '{time_str}': {e}")
#         hour = 9
#         minute = 0
    
#     # Combine date and time in India timezone
#     naive_dt = datetime.datetime.combine(target_date, datetime.time(hour=hour, minute=minute))
#     result = india_tz.localize(naive_dt)
    
#     print(f"📅 Parsed: date_str='{date_str}', time_str='{time_str}' → {result} (India Time)")
#     return result


# def create_calendar_event(user_id, name, date_str, time_str, title=None):
#     """Create calendar event - called by Groq function calling."""
#     try:
#         if not title:
#             title = f"Meeting with {name}"

#         # Parse the datetime (already in India timezone from parse_datetime)
#         start_aware = parse_datetime(date_str, time_str)
#         end_aware = start_aware + datetime.timedelta(hours=1)
        
#         # Timezone is already set to Asia/Kolkata
#         tz_name = "Asia/Kolkata"
        
#         print(f"🌍 Using timezone: {tz_name}")
#         print(f"⏰ Event time: {start_aware} to {end_aware}")

#         service = get_calendar_service(user_id)

#         event = {
#             "summary": title,
#             "start": {
#                 "dateTime": start_aware.isoformat(),
#                 "timeZone": tz_name
#             },
#             "end": {
#                 "dateTime": end_aware.isoformat(),
#                 "timeZone": tz_name
#             },
#             "description": f"Created by Calendar Agent for: {name}"
#         }

#         result = service.events().insert(calendarId="primary", body=event).execute()
        
#         print(f"✅ Event created: {result['id']}")
#         print(f"🔗 Event link: {result.get('htmlLink', '')}")

#         return {
#             "success": True,
#             "message": f"✅ Event created: **{title}** on **{start_aware.strftime('%A, %B %d at %I:%M %p')}** (India Time)",
#             "link": result.get("htmlLink", ""),
#             "event_id": result['id']
#         }

#     except Exception as e:
#         print(f"❌ Event creation error: {e}")
#         import traceback
#         traceback.print_exc()
#         return {"success": False, "message": f"❌ Error creating event: {e}"}


# def list_upcoming_events(user_id, max_results=10):
#     """List upcoming events from user's calendar."""
#     try:
#         service = get_calendar_service(user_id)
        
#         # Get current time in India timezone
#         india_tz = pytz.timezone('Asia/Kolkata')
#         now = datetime.datetime.now(india_tz).isoformat()
        
#         events_result = service.events().list(
#             calendarId='primary',
#             timeMin=now,
#             maxResults=max_results,
#             singleEvents=True,
#             orderBy='startTime'
#         ).execute()
        
#         events = events_result.get('items', [])
        
#         if not events:
#             return {
#                 "success": True,
#                 "message": "📅 No upcoming events found.",
#                 "events": []
#             }
        
#         event_list = []
#         for event in events:
#             start = event['start'].get('dateTime', event['start'].get('date'))
#             event_list.append({
#                 "id": event['id'],
#                 "summary": event.get('summary', 'No title'),
#                 "start": start
#             })
        
#         # Format message
#         msg = "📅 **Your upcoming events:**\n\n"
#         for i, evt in enumerate(event_list, 1):
#             try:
#                 dt = parser.parse(evt['start'])
#                 time_str = dt.strftime('%A, %B %d at %I:%M %p')
#             except:
#                 time_str = evt['start']
#             msg += f"{i}. **{evt['summary']}** - {time_str}\n"
        
#         return {
#             "success": True,
#             "message": msg,
#             "events": event_list
#         }
        
#     except Exception as e:
#         print(f"❌ List events error: {e}")
#         return {"success": False, "message": f"❌ Error listing events: {e}", "events": []}


# def delete_calendar_event(user_id, name=None, date_str=None):
#     """Delete a calendar event by name or date."""
#     try:
#         service = get_calendar_service(user_id)
        
#         # Get upcoming events
#         result = list_upcoming_events(user_id, max_results=50)
#         if not result["success"] or not result["events"]:
#             return {"success": False, "message": "❌ No upcoming events to delete."}
        
#         events = result["events"]
        
#         # Find matching event
#         event_to_delete = None
        
#         if name:
#             # Search by name (case-insensitive, partial match)
#             name_lower = name.lower()
#             for evt in events:
#                 if name_lower in evt["summary"].lower():
#                     event_to_delete = evt
#                     break
        
#         if not event_to_delete and date_str:
#             # Search by date
#             target_date = None
#             today = datetime.datetime.now(pytz.timezone('Asia/Kolkata'))
            
#             if "tomorrow" in date_str.lower():
#                 target_date = (today + datetime.timedelta(days=1)).date()
#             elif "today" in date_str.lower():
#                 target_date = today.date()
#             else:
#                 try:
#                     parsed = parser.parse(date_str, fuzzy=True)
#                     target_date = parsed.date()
#                 except:
#                     pass
            
#             if target_date:
#                 for evt in events:
#                     try:
#                         evt_date = parser.parse(evt["start"]).date()
#                         if evt_date == target_date:
#                             event_to_delete = evt
#                             break
#                     except:
#                         pass
        
#         if not event_to_delete:
#             return {
#                 "success": False,
#                 "message": f"❌ Could not find event matching '{name or date_str}'. Try listing your events first."
#             }
        
#         # Delete the event
#         service.events().delete(
#             calendarId='primary',
#             eventId=event_to_delete['id']
#         ).execute()
        
#         print(f"✅ Event deleted: {event_to_delete['id']}")
        
#         try:
#             dt = parser.parse(event_to_delete['start'])
#             time_str = dt.strftime('%A, %B %d at %I:%M %p')
#         except:
#             time_str = event_to_delete['start']
        
#         return {
#             "success": True,
#             "message": f"✅ Deleted: **{event_to_delete['summary']}** ({time_str})"
#         }
        
#     except Exception as e:
#         print(f"❌ Delete event error: {e}")
#         import traceback
#         traceback.print_exc()
#         return {"success": False, "message": f"❌ Error deleting event: {e}"}

# # ================== GROQ FUNCTION DEFINITION ==================

# functions = [
#     {
#         "name": "create_calendar_event",
#         "description": "Create a Google Calendar event. Use this when the user wants to schedule a meeting or event.",
#         "parameters": {
#             "type": "object",
#             "properties": {
#                 "name": {
#                     "type": "string", 
#                     "description": "The person's name or event topic (e.g., 'Bob', 'Team Meeting')"
#                 },
#                 "date_str": {
#                     "type": "string", 
#                     "description": "The date (e.g., 'tomorrow', 'Friday', 'Dec 15')"
#                 },
#                 "time_str": {
#                     "type": "string", 
#                     "description": "The time (e.g., '3 PM', '10:30 AM', '14:00')"
#                 },
#                 "title": {
#                     "type": "string", 
#                     "description": "Optional custom event title"
#                 }
#             },
#             "required": ["name", "date_str", "time_str"]
#         }
#     },
#     {
#         "name": "list_upcoming_events",
#         "description": "List the user's upcoming calendar events. Use when user asks to see their schedule or upcoming meetings.",
#         "parameters": {
#             "type": "object",
#             "properties": {
#                 "max_results": {
#                     "type": "integer",
#                     "description": "Maximum number of events to return (default 10)"
#                 }
#             }
#         }
#     },
#     {
#         "name": "delete_calendar_event",
#         "description": "Delete/cancel a calendar event. Use when user wants to cancel or delete a meeting.",
#         "parameters": {
#             "type": "object",
#             "properties": {
#                 "name": {
#                     "type": "string",
#                     "description": "The person's name or event name to delete (e.g., 'Bob', 'Team Meeting')"
#                 },
#                 "date_str": {
#                     "type": "string",
#                     "description": "The date of the event to delete (e.g., 'tomorrow', 'Friday')"
#                 }
#             }
#         }
#     }
# ]

# # ================== CHAT HANDLER ==================

# def format_messages_from_history(history, user_message):
#     """Convert Gradio history to Groq message format."""
#     msgs = []
    
#     # Ensure user_message is a string
#     if not isinstance(user_message, str):
#         user_message = str(user_message) if user_message else ""
    
#     # Check if this is a NEW schedule request (without complete date/time in the message itself)
#     is_schedule = any(word in user_message.lower() for word in ["schedule", "book", "arrange", "create", "set up", "plan"])
#     has_date = any(word in user_message.lower() for word in ["tomorrow", "today", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday", "next", "this", "dec", "jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov"])
#     has_time = any(word in user_message.lower() for word in ["am", "pm", ":", "noon", "morning", "afternoon", "evening"]) and any(char.isdigit() for char in user_message)
    
#     if is_schedule and not (has_date and has_time):
#         # NEW SCHEDULE REQUEST WITHOUT COMPLETE INFO
#         # Don't include ANY previous context to avoid date/time contamination
#         print(f"🚨 NEW SCHEDULE REQUEST DETECTED - CLEARING CONTEXT")
#         # Start fresh - only include this message
#     else:
#         # Not a new schedule request OR has complete info - include some context
#         # But still filter out previous date/time mentions
#         for msg in history[-6:]:  # Only last 6 messages
#             if isinstance(msg, dict):
#                 content = msg.get("content", "")
#                 if not isinstance(content, str):
#                     continue
                    
#                 # Skip event creation confirmations
#                 if "✅ Event created:" in content:
#                     continue
                    
#                 if msg.get("role") == "user":
#                     # Filter out date/time from previous schedule requests
#                     if not any(word in content.lower() for word in ["tomorrow", "today", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday", "at", "am", "pm"]):
#                         msgs.append({"role": "user", "content": content})
#                 elif msg.get("role") == "assistant":
#                     msgs.append({"role": "assistant", "content": content})
    
#     if user_message:
#         msgs.append({"role": "user", "content": user_message.strip()})
    
#     return msgs


# def chat(user_message, history, request: gr.Request):
#     """Main chat handler with Groq function calling."""
#     if not user_message or (isinstance(user_message, str) and not user_message.strip()):
#         return history, ""

#     # Convert to string if needed
#     if not isinstance(user_message, str):
#         user_message = str(user_message)

#     user_id = request.session.get("user_id")
#     email = request.session.get("email", "")

#     if not user_id:
#         history.append({
#             "role": "assistant",
#             "content": "🔐 **Please login first!**\n\nClick **[Login with Google](/login)** above."
#         })
#         return history, ""

#     try:
#         messages = format_messages_from_history(history, user_message)
        
#         messages.insert(0, {
#             "role": "system",
#             "content": """You are a friendly calendar assistant. You can:
# 1) Schedule events using 'create_calendar_event'
# 2) List upcoming events using 'list_upcoming_events'
# 3) Delete/cancel events using 'delete_calendar_event'

# CRITICAL RULES FOR SCHEDULING:
# - NEVER call create_calendar_event without ALL required information: name, date, and time
# - If the user does NOT provide a date (today, tomorrow, Monday, etc.), you MUST ask for it
# - If the user does NOT provide a time (3 PM, 10 AM, etc.), you MUST ask for it
# - DO NOT make assumptions or use default values
# - DO NOT guess the date or time
# - Always confirm ALL details before calling the function

# HANDLING NON-CALENDAR QUESTIONS:
# - If user says greetings (hi, hello, hey), respond warmly and ask how you can help with their calendar
# - If user says thanks/thank you, respond briefly: "You're welcome! Let me know if you need anything else with your calendar."
# - If user asks unrelated questions (weather, news, general knowledge, jokes, etc.), politely redirect: "I'm a calendar assistant and can only help with scheduling, viewing, and managing your calendar events. Is there anything calendar-related I can help you with?"
# - Keep responses SHORT and focused on calendar tasks
# - Don't try to answer questions outside of calendar management

# Example:
# User: "What's the weather today?"
# You: "I'm a calendar assistant and can only help with scheduling and managing your calendar events. Is there a meeting you'd like to schedule?"

# User: "Thanks!"
# You: "You're welcome! Let me know if you need anything else with your calendar."

# User: "Schedule meeting with Bob tomorrow at 3 PM"
# You: [NOW call create_calendar_event with all required info]"""
#         })

#         response = groq_client.chat.completions.create(
#             model="llama-3.3-70b-versatile",
#             messages=messages,
#             tools=[{"type": "function", "function": fn} for fn in functions],
#             tool_choice="auto",
#             max_tokens=512,
#             temperature=0.7
#         )

#         msg = response.choices[0].message

#         # Check if function was called
#         if msg.tool_calls:
#             tool_call = msg.tool_calls[0]
            
#             # Parse function arguments
#             if isinstance(tool_call.function.arguments, str):
#                 args = json.loads(tool_call.function.arguments)
#             else:
#                 args = dict(tool_call.function.arguments)
            
#             # DEBUG: Print what Groq extracted
#             print(f"🤖 Groq extracted arguments: {json.dumps(args, indent=2)}")
            
#             if tool_call.function.name == "create_calendar_event":
#                 # VALIDATION: Check if all required fields are present
#                 if not args.get("date_str") or not args.get("time_str"):
#                     assistant_reply = "❓ I need more information. Please provide:\n- **Date** (today, tomorrow, Monday, etc.)\n- **Time** (3 PM, 10 AM, etc.)\n\nExample: 'Schedule meeting with Aman tomorrow at 3 PM'"
#                 else:
#                     # Additional check: Make sure date_str and time_str are not empty strings
#                     if not args["date_str"].strip() or not args["time_str"].strip():
#                         assistant_reply = "❓ I need more information. Please provide:\n- **Date** (today, tomorrow, Monday, etc.)\n- **Time** (3 PM, 10 AM, etc.)\n\nExample: 'Schedule meeting with Aman tomorrow at 3 PM'"
#                     else:
#                         # Add user_id to args
#                         args["user_id"] = user_id
#                         result = create_calendar_event(**args)
#                         assistant_reply = result["message"]
#                         if result.get("link"):
#                             assistant_reply += f"\n\n🔗 [View in Google Calendar]({result['link']})"
            
#             elif tool_call.function.name == "list_upcoming_events":
#                 args["user_id"] = user_id
#                 result = list_upcoming_events(**args)
#                 assistant_reply = result["message"]
            
#             elif tool_call.function.name == "delete_calendar_event":
#                 args["user_id"] = user_id
#                 result = delete_calendar_event(**args)
#                 assistant_reply = result["message"]
            
#             else:
#                 assistant_reply = f"❌ Unknown function: {tool_call.function.name}"
#         else:
#             # No function call - just chat response
#             assistant_reply = msg.content

#         history.append({"role": "user", "content": user_message})
#         history.append({"role": "assistant", "content": assistant_reply})
#         return history, ""

#     except Exception as e:
#         print(f"❌ Chat error: {e}")
#         import traceback
#         traceback.print_exc()
        
#         error_msg = f"❌ Error: {str(e)}\n\nTry [logging in again](/login) if the problem persists."
#         history.append({"role": "user", "content": user_message})
#         history.append({"role": "assistant", "content": error_msg})
#         return history, ""


# def reset_conversation():
#     """Reset chat history."""
#     return [], ""


# def transcribe_audio(audio_path):
#     """Convert voice to text using Groq Whisper."""
#     if not audio_path:
#         return ""
    
#     try:
#         with open(audio_path, "rb") as file:
#             transcription = groq_client.audio.transcriptions.create(
#                 file=(audio_path, file.read()),
#                 model="whisper-large-v3-turbo",
#                 response_format="text"
#             )
        
#         print(f"🎤 Transcribed: {transcription}")
#         return transcription
    
#     except Exception as e:
#         print(f"❌ Transcription error: {e}")
#         return ""

# # ================== GRADIO UI ==================

# with gr.Blocks(title="Voice Calendar Agent", theme=gr.themes.Soft()) as demo:
#     gr.Markdown("# 🎙️ Voice Calendar Agent")
#     gr.Markdown("**AI-powered calendar scheduling with natural language**")
    
#     with gr.Row():
#         gr.Markdown("[🔑 Login with Google](/login)")
#         gr.Markdown("[🚪 Logout](/logout)")

#     chatbot = gr.Chatbot(height=450, show_label=False)
    
#     with gr.Row():
#         msg = gr.Textbox(
#             label="Message",
#             placeholder="Schedule a meeting with Bob tomorrow at 2 PM...",
#             show_label=False,
#             scale=8
#         )
#         voice_btn = gr.Audio(
#             sources=["microphone"],
#             type="filepath",
#             label="🎤",
#             show_label=False,
#             scale=1,
#             waveform_options={"show_recording_waveform": True}
#         )
#         send = gr.Button("Send", scale=1, variant="primary")
    
#     with gr.Row():
#         record_again = gr.Button("🎤 Record Again", size="sm")
    
#     clear = gr.Button("Reset Conversation", variant="secondary")

#     gr.Markdown("### 💡 Try saying:")
#     gr.Examples(
#         examples=[
#             "Schedule a meeting with Bob tomorrow at 2 PM",
#             "Book a call with Sarah on Friday at 10:30 AM",
#             "Show me my upcoming meetings",
#             "Cancel my meeting with Bob",
#             "Delete tomorrow's meeting"
#         ],
#         inputs=msg
#     )

#     send.click(chat, [msg, chatbot], [chatbot, msg])
#     msg.submit(chat, [msg, chatbot], [chatbot, msg])
#     clear.click(reset_conversation, None, [chatbot, msg])
    
#     # Voice input - transcribe and fill textbox
#     voice_btn.change(transcribe_audio, voice_btn, msg)
    
#     # Record again button - clears the audio widget
#     record_again.click(lambda: None, None, voice_btn)

# app = gr.mount_gradio_app(app, demo, path="/")

# # ================== STARTUP ==================

# @app.on_event("startup")
# async def startup():
#     init_db()
#     print("✅ Voice Calendar Agent started!")
#     print(f"📍 Redirect URI: {REDIRECT_URI}")

# # ================== START ==================

# if __name__ == "__main__":
#     import uvicorn
#     uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))

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

# ================== CALENDAR FUNCTIONS ==================

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
            "link": result.get("htmlLink", ""),
            "event_id": result['id']
        }

    except Exception as e:
        print(f"❌ Event creation error: {e}")
        import traceback
        traceback.print_exc()
        return {"success": False, "message": f"❌ Error creating event: {e}"}


def list_upcoming_events(user_id, max_results=10):
    """List upcoming events from user's calendar."""
    try:
        service = get_calendar_service(user_id)
        
        # Get current time in India timezone
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
            return {
                "success": True,
                "message": "📅 No upcoming events found.",
                "events": []
            }
        
        event_list = []
        for event in events:
            start = event['start'].get('dateTime', event['start'].get('date'))
            event_list.append({
                "id": event['id'],
                "summary": event.get('summary', 'No title'),
                "start": start
            })
        
        # Format message
        msg = "📅 **Your upcoming events:**\n\n"
        for i, evt in enumerate(event_list, 1):
            try:
                dt = parser.parse(evt['start'])
                time_str = dt.strftime('%A, %B %d at %I:%M %p')
            except:
                time_str = evt['start']
            msg += f"{i}. **{evt['summary']}** - {time_str}\n"
        
        return {
            "success": True,
            "message": msg,
            "events": event_list
        }
        
    except Exception as e:
        print(f"❌ List events error: {e}")
        return {"success": False, "message": f"❌ Error listing events: {e}", "events": []}


def delete_calendar_event(user_id, name=None, date_str=None):
    """Delete a calendar event by name or date."""
    try:
        service = get_calendar_service(user_id)
        
        # Get upcoming events
        result = list_upcoming_events(user_id, max_results=50)
        if not result["success"] or not result["events"]:
            return {"success": False, "message": "❌ No upcoming events to delete."}
        
        events = result["events"]
        
        # Find matching event
        event_to_delete = None
        
        if name:
            # Search by name (case-insensitive, partial match)
            name_lower = name.lower()
            for evt in events:
                if name_lower in evt["summary"].lower():
                    event_to_delete = evt
                    break
        
        if not event_to_delete and date_str:
            # Search by date
            target_date = None
            today = datetime.datetime.now(pytz.timezone('Asia/Kolkata'))
            
            if "tomorrow" in date_str.lower():
                target_date = (today + datetime.timedelta(days=1)).date()
            elif "today" in date_str.lower():
                target_date = today.date()
            else:
                try:
                    parsed = parser.parse(date_str, fuzzy=True)
                    target_date = parsed.date()
                except:
                    pass
            
            if target_date:
                for evt in events:
                    try:
                        evt_date = parser.parse(evt["start"]).date()
                        if evt_date == target_date:
                            event_to_delete = evt
                            break
                    except:
                        pass
        
        if not event_to_delete:
            return {
                "success": False,
                "message": f"❌ Could not find event matching '{name or date_str}'. Try listing your events first."
            }
        
        # Delete the event
        service.events().delete(
            calendarId='primary',
            eventId=event_to_delete['id']
        ).execute()
        
        print(f"✅ Event deleted: {event_to_delete['id']}")
        
        try:
            dt = parser.parse(event_to_delete['start'])
            time_str = dt.strftime('%A, %B %d at %I:%M %p')
        except:
            time_str = event_to_delete['start']
        
        return {
            "success": True,
            "message": f"✅ Deleted: **{event_to_delete['summary']}** ({time_str})"
        }
        
    except Exception as e:
        print(f"❌ Delete event error: {e}")
        import traceback
        traceback.print_exc()
        return {"success": False, "message": f"❌ Error deleting event: {e}"}

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
    },
    {
        "name": "list_upcoming_events",
        "description": "List the user's upcoming calendar events. Use when user asks to see their schedule or upcoming meetings.",
        "parameters": {
            "type": "object",
            "properties": {
                "max_results": {
                    "type": "integer",
                    "description": "Maximum number of events to return (default 10)"
                }
            }
        }
    },
    {
        "name": "delete_calendar_event",
        "description": "Delete/cancel a calendar event. Use when user wants to cancel or delete a meeting.",
        "parameters": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "The person's name or event name to delete (e.g., 'Bob', 'Team Meeting')"
                },
                "date_str": {
                    "type": "string",
                    "description": "The date of the event to delete (e.g., 'tomorrow', 'Friday')"
                }
            }
        }
    }
]

# ================== CHAT HANDLER ==================

def format_messages_from_history(history, user_message):
    """Convert Gradio history to Groq message format."""
    msgs = []
    
    # Ensure user_message is a string
    if not isinstance(user_message, str):
        user_message = str(user_message) if user_message else ""
    
    # Find the last completed event (marked by ✅)
    last_event_index = -1
    for i in range(len(history) - 1, -1, -1):
        if isinstance(history[i], dict):
            content = history[i].get("content", "")
            if isinstance(content, str) and "✅ Event created:" in content:
                last_event_index = i
                break
    
    # Include messages AFTER the last completed event
    if last_event_index >= 0:
        relevant_history = history[last_event_index + 1:]
    else:
        relevant_history = history[-10:]  # Last 10 if no event found
    
    # Add relevant history to messages - with aggressive filtering
    for msg in relevant_history:
        if isinstance(msg, dict):
            content = msg.get("content", "")
            if not isinstance(content, str):
                continue
            
            role = msg.get("role")
            
            # Skip problematic assistant messages
            if role == "assistant":
                # Skip these types of messages:
                skip_phrases = [
                    "I'm a calendar assistant and can only help",
                    "I'm a calendar assistant and can only",
                    "❌ Error:",
                    "Try [logging in again]"
                ]
                if any(phrase in content for phrase in skip_phrases):
                    continue
            
            if role == "user":
                msgs.append({"role": "user", "content": content})
            elif role == "assistant":
                msgs.append({"role": "assistant", "content": content})
    
    if user_message:
        msgs.append({"role": "user", "content": user_message.strip()})
    
    print(f"📚 Context: {len(msgs)} messages")
    for i, m in enumerate(msgs):
        print(f"  {i+1}. {m['role']}: {m['content'][:60]}...")
    
    return msgs


def chat(user_message, history, request: gr.Request):
    """Main chat handler with Groq function calling."""
    if not user_message or (isinstance(user_message, str) and not user_message.strip()):
        return history, ""

    # Convert to string if needed
    if not isinstance(user_message, str):
        user_message = str(user_message)

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
            "content": """You are a friendly calendar assistant. You can:
1) Schedule events using 'create_calendar_event'
2) List upcoming events using 'list_upcoming_events'
3) Delete/cancel events using 'delete_calendar_event'

CRITICAL RULES FOR SCHEDULING:
- NEVER call create_calendar_event without ALL required information: name, date, and time
- If the user does NOT provide a date (today, tomorrow, Monday, etc.), you MUST ask for it
- If the user does NOT provide a time (3 PM, 10 AM, etc.), you MUST ask for it
- DO NOT make assumptions or use default values
- DO NOT guess the date or time
- Always confirm ALL details before calling the function

HANDLING NON-CALENDAR QUESTIONS:
- If user says greetings (hi, hello, hey), respond warmly and ask how you can help with their calendar
- If user says thanks/thank you, respond briefly: "You're welcome! Let me know if you need anything else with your calendar."
- If user asks unrelated questions (weather, news, general knowledge, jokes, etc.), politely redirect: "I'm a calendar assistant and can only help with scheduling, viewing, and managing your calendar events. Is there anything calendar-related I can help you with?"
- Keep responses SHORT and focused on calendar tasks
- Don't try to answer questions outside of calendar management

Example:
User: "What's the weather today?"
You: "I'm a calendar assistant and can only help with scheduling and managing your calendar events. Is there a meeting you'd like to schedule?"

User: "Thanks!"
You: "You're welcome! Let me know if you need anything else with your calendar."

User: "Schedule meeting with Bob tomorrow at 3 PM"
You: [NOW call create_calendar_event with all required info]"""
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
                # Get arguments
                date_str = args.get("date_str", "").strip()
                time_str = args.get("time_str", "").strip()
                name = args.get("name", "").strip()
                
                print(f"🤖 Function call: name='{name}', date='{date_str}', time='{time_str}'")
                
                # Collect what was mentioned in conversation after last event
                conversation_text = " ".join([
                    msg.get("content", "").lower() 
                    for msg in history[max(0, len(history)-5):] 
                    if msg.get("role") == "user"
                ]) + " " + user_message.lower()
                
                print(f"📝 Recent conversation: {conversation_text[:100]}...")
                
                # Check what's actually in the conversation
                has_time_mention = any(t in conversation_text for t in ["am", "pm", "noon"]) and any(c.isdigit() for c in conversation_text)
                has_date_mention = any(d in conversation_text for d in ["today", "tomorrow", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"])
                
                print(f"✅ Validation: time mentioned={has_time_mention}, date mentioned={has_date_mention}")
                
                # Validate
                if not name or not date_str or not time_str:
                    missing = []
                    if not name: missing.append("person/event name")
                    if not date_str: missing.append("date")
                    if not time_str: missing.append("time")
                    assistant_reply = f"❓ I still need: {', '.join(missing)}"
                elif not has_date_mention:
                    assistant_reply = "❓ What date would you like for this meeting?"
                elif not has_time_mention:
                    assistant_reply = "❓ What time would you like for this meeting?"
                else:
                    # All validations passed - create event!
                    args["user_id"] = user_id
                    result = create_calendar_event(**args)
                    assistant_reply = result["message"]
                    if result.get("link"):
                        assistant_reply += f"\n\n🔗 [View in Google Calendar]({result['link']})"
            
            elif tool_call.function.name == "list_upcoming_events":
                args["user_id"] = user_id
                result = list_upcoming_events(**args)
                assistant_reply = result["message"]
            
            elif tool_call.function.name == "delete_calendar_event":
                args["user_id"] = user_id
                result = delete_calendar_event(**args)
                assistant_reply = result["message"]
            
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


def transcribe_audio(audio_path):
    """Convert voice to text using Groq Whisper."""
    if not audio_path:
        return ""
    
    try:
        with open(audio_path, "rb") as file:
            transcription = groq_client.audio.transcriptions.create(
                file=(audio_path, file.read()),
                model="whisper-large-v3-turbo",
                response_format="text"
            )
        
        print(f"🎤 Transcribed: {transcription}")
        return transcription
    
    except Exception as e:
        print(f"❌ Transcription error: {e}")
        return ""

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
            scale=8
        )
        voice_btn = gr.Audio(
            sources=["microphone"],
            type="filepath",
            label="🎤",
            show_label=False,
            scale=1,
            waveform_options={"show_recording_waveform": True}
        )
        send = gr.Button("Send", scale=1, variant="primary")
    
    with gr.Row():
        record_again = gr.Button("🎤 Record Again", size="sm")
    
    clear = gr.Button("Reset Conversation", variant="secondary")

    gr.Markdown("### 💡 Try saying:")
    gr.Examples(
        examples=[
            "Schedule a meeting with Bob tomorrow at 2 PM",
            "Book a call with Sarah on Friday at 10:30 AM",
            "Show me my upcoming meetings",
            "Cancel my meeting with Bob",
            "Delete tomorrow's meeting"
        ],
        inputs=msg
    )

    send.click(chat, [msg, chatbot], [chatbot, msg])
    msg.submit(chat, [msg, chatbot], [chatbot, msg])
    clear.click(reset_conversation, None, [chatbot, msg])
    
    # Voice input - transcribe and fill textbox
    voice_btn.change(transcribe_audio, voice_btn, msg)
    
    # Record again button - clears the audio widget
    record_again.click(lambda: None, None, voice_btn)

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

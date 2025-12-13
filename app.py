"""
Voice Calendar Agent - OAuth 2.0 Multi-User Version
Uses standard OAuth 2.0 flow (client_secret.json) for per-user calendar access.
This requires the user to sign in to Google the first time they use the app.
"""

import gradio as gr
from groq import Groq
import os
import datetime
from dateutil import parser
import dateutil.tz
import json

# --- Google Calendar OAuth Imports ---
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
# -------------------------------------

# Environment and Constants
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass 

# Initialize Groq Client
groq_client = Groq(api_key=os.getenv("GROQ_API_KEY"))

# Google Calendar API scope
SCOPES = ['https://www.googleapis.com/auth/calendar'] 
TOKEN_FILE = "user_token.json"
OAUTH_PORT = 8080  # Use a different port for OAuth to avoid conflict with Gradio


def get_calendar_service():
    """
    Handles OAuth 2.0 flow for user authorization.
    """
    creds = None
    
    if os.path.exists(TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not os.path.exists("client_secret.json"):
                raise Exception("client_secret.json missing. Ensure the Google OAuth JSON file is in the same directory.")
                
            flow = InstalledAppFlow.from_client_secrets_file(
                "client_secret.json", SCOPES
            )
            print("\n🚨 Please sign in to Google in the browser window that just opened...")
            creds = flow.run_local_server(port=OAUTH_PORT) 

        with open(TOKEN_FILE, 'w') as token:
            token.write(creds.to_json())

    return build("calendar", "v3", credentials=creds)


def parse_datetime(date_str, time_str):
    """Parses date and time strings into a NAIVE datetime object."""
    today = datetime.datetime.now()
    if "tomorrow" in date_str.lower():
        target = today + datetime.timedelta(days=1)
    elif "today" in date_str.lower():
        target = today
    else:
        try:
            target = parser.parse(date_str, fuzzy=True)
        except Exception:
            target = today + datetime.timedelta(days=1)
    try:
        t = parser.parse(time_str, fuzzy=True)
    except Exception:
        t = today.replace(hour=9, minute=0)
        
    return target.replace(hour=t.hour, minute=t.minute, second=0, microsecond=0)


def create_calendar_event(name, date_str, time_str, title=None):
    """Creates an event on the currently authenticated user's primary Google Calendar."""
    try:
        if not title:
            title = f"Scheduled event: {name}"

        # Parse the datetime
        start_naive = parse_datetime(date_str, time_str)
        
        # Get local timezone in IANA format (e.g., 'Asia/Kolkata', 'America/New_York')
        import time
        if time.daylight:
            tz_offset = time.altzone
        else:
            tz_offset = time.timezone
        
        # Use a standard timezone format that Google Calendar accepts
        # Try to get system timezone name, fallback to UTC offset
        try:
            import tzlocal
            local_tz = tzlocal.get_localzone()
            tz_name = str(local_tz)
        except:
            # Fallback: calculate UTC offset
            offset_hours = -tz_offset // 3600
            tz_name = f"Etc/GMT{offset_hours:+d}" if offset_hours != 0 else "UTC"
        
        # Format datetime in ISO 8601 format without timezone info
        # Google Calendar API will use the timeZone field
        start_str = start_naive.strftime('%Y-%m-%dT%H:%M:%S')
        end_naive = start_naive + datetime.timedelta(hours=1)
        end_str = end_naive.strftime('%Y-%m-%dT%H:%M:%S')

        service = get_calendar_service()

        event = {
            "summary": title,
            "start": {
                "dateTime": start_str,
                "timeZone": tz_name
            },
            "end": {
                "dateTime": end_str,
                "timeZone": tz_name
            },
            "description": f"Created by the Groq Voice Calendar Agent for: {name}"
        }

        result = service.events().insert(calendarId="primary", body=event).execute()

        return {
            "success": True,
            "message": f"✅ Event created: **{title}** on **{start_naive.strftime('%A, %B %d at %I:%M %p')}** ({tz_name})",
            "link": result.get("htmlLink", "")
        }

    except Exception as e:
        import traceback
        traceback.print_exc()
        return {"success": False, "message": f"❌ Error creating event: {e}"}


functions = [
    {
        "name": "create_calendar_event",
        "description": "Create a Google Calendar event. The 'name' parameter should contain the person's name or a brief description of the event.",
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "The person's name, or a topic for the event (e.g., 'Bob', 'Project Review')."},
                "date_str": {"type": "string", "description": "The date of the event (e.g., 'tomorrow', 'next Tuesday', 'Dec 15')."},
                "time_str": {"type": "string", "description": "The time of the event (e.g., '3 PM', '10:30 AM', 'noon')."},
                "title": {"type": "string", "description": "The specific title of the event, if explicitly provided by the user."}
            },
            "required": ["name", "date_str", "time_str"]
        }
    }
]


def format_messages_from_history(history, user_message):
    """
    Convert Gradio chat history → Groq message format.
    """
    msgs = []
    # Add previous turns - history is already in dict format
    for msg in history:
        if isinstance(msg, dict):
            if msg.get("role") == "user":
                msgs.append({"role": "user", "content": msg["content"]})
            elif msg.get("role") == "assistant":
                msgs.append({"role": "assistant", "content": msg["content"]})
            
    # Add current user message
    if user_message and isinstance(user_message, str):
        msgs.append({"role": "user", "content": user_message.strip()})
    return msgs


def chat(user_message, history):
    if not user_message.strip():
        return history, ""

    try:
        messages = format_messages_from_history(history, user_message)
        
        messages.insert(0, {
            "role": "system",
            "content": "You are a friendly, helpful calendar assistant. Your primary function is to schedule events using the provided 'create_calendar_event' tool. Always confirm the details before scheduling. Respond professionally and use the tool when the user provides all necessary information (name/topic, date, and time)."
        })

        response = groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",  # Fixed: Use a model that supports tools
            messages=messages,
            tools=[{"type": "function", "function": fn} for fn in functions],
            tool_choice="auto",
            max_tokens=512,
            temperature=0.7
        )

        msg = response.choices[0].message

        # Function call case
        if msg.tool_calls:
            tool_call = msg.tool_calls[0]
            # Parse arguments if they're a string
            if isinstance(tool_call.function.arguments, str):
                args = json.loads(tool_call.function.arguments)
            else:
                args = dict(tool_call.function.arguments)
            
            if tool_call.function.name == "create_calendar_event":
                result = create_calendar_event(**args)
                assistant_reply = result["message"]
            else:
                assistant_reply = f"Error: Unknown tool call: {tool_call.function.name}"

        else:
            assistant_reply = msg.content

        # Append messages in dictionary format (messages format)
        history.append({"role": "user", "content": user_message})
        history.append({"role": "assistant", "content": assistant_reply})
        return history, ""

    except Exception as e:
        err = f"❌ Error during chat processing: {e}. If this is the first run, ensure your browser opened a Google sign-in window and that your `client_secret.json` and `GROQ_API_KEY` are correct."
        import traceback
        traceback.print_exc() 
        history.append({"role": "user", "content": user_message})
        history.append({"role": "assistant", "content": err})
        return history, ""


def reset_conversation():
    """Resets the Gradio interface."""
    return [], ""


# ------------- GRADIO UI ---------------------

with gr.Blocks(title="Voice Calendar Agent") as demo:
    gr.Markdown("# 🎙️ Voice Calendar Agent (Multi-User OAuth)")
    gr.Markdown("#### **Setup:** Ensure `client_secret.json` is present and your redirect URI includes `http://localhost:8080` in Google Cloud Console.")
    gr.Markdown("First time running? A Google sign-in window will open when you submit your first request to authorize calendar access.")

    chatbot = gr.Chatbot(height=450)  # Messages format handled automatically
    msg = gr.Textbox(label="Message", placeholder="Schedule a meeting with Bob tomorrow at 2 PM...")
    send = gr.Button("Send")
    clear = gr.Button("Reset Conversation")

    send.click(chat, [msg, chatbot], [chatbot, msg])
    msg.submit(chat, [msg, chatbot], [chatbot, msg])
    clear.click(reset_conversation, None, [chatbot, msg])


if __name__ == "__main__":
    print("\n🚀 Your multi-user app is running!")
    print("➡ Open this URL in your browser:")
    print("   http://127.0.0.1:7860\n")

    demo.launch(server_name="127.0.0.1", server_port=7860)

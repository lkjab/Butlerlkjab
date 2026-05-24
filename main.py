from fastapi import FastAPI, Request
import os
import json
import re
from datetime import datetime, timedelta
from dotenv import load_dotenv
import anthropic
from supabase import create_client, Client
import httpx

# Load environment variables
load_dotenv()

# Initialize clients
CLAUDE_API_KEY = os.getenv("ANTHROPIC_API_KEY")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_ANON_KEY")

claude_client = anthropic.Anthropic(api_key=CLAUDE_API_KEY)
supabase: Client = None  # Lazy load

def get_supabase():
    """Lazy load Supabase client on first use"""
    global supabase
    if supabase is None:
        supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
    return supabase

app = FastAPI(title="Personal Assistant Bot")

# ============ MEMORY FUNCTIONS ============

def get_memories(user_id: str) -> list[str]:
    """Fetch all memories for a user"""
    try:
        response = get_supabase().table("memories").select("content").eq("user_id", user_id).execute()
        return [m["content"] for m in response.data] if response.data else []
    except Exception as e:
        print(f"Error fetching memories: {e}")
        return []

def save_memory(user_id: str, content: str) -> bool:
    """Save a new memory (ignores duplicates)"""
    try:
        get_supabase().table("memories").insert({
            "user_id": user_id,
            "content": content
        }).execute()
        return True
    except Exception as e:
        print(f"Error saving memory: {e}")
        return False

def get_pending_reminders(user_id: str) -> list[dict]:
    """Get all unfired reminders for a user"""
    try:
        response = get_supabase().table("reminders").select("*").eq("user_id", user_id).is_("fired_at", "null").execute()
        return response.data if response.data else []
    except Exception as e:
        print(f"Error fetching reminders: {e}")
        return []

def save_reminder(user_id: str, message: str, delay_ms: int = None) -> bool:
    """Save a new reminder"""
    try:
        if delay_ms:
            fire_time = datetime.utcnow() + timedelta(milliseconds=delay_ms)
        else:
            # Default to 1 hour from now if no time specified
            fire_time = datetime.utcnow() + timedelta(hours=1)
        
        get_supabase().table("reminders").insert({
            "user_id": user_id,
            "message": message,
            "fire_at": fire_time.isoformat()
        }).execute()
        return True
    except Exception as e:
        print(f"Error saving reminder: {e}")
        return False

# ============ CLAUDE FUNCTIONS ============

def build_system_prompt(memories: list[str], reminders: list[dict]) -> str:
    """Build the system prompt with current context"""
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    
    mem_section = "Facts about the user:\n" + "\n".join([f"- {m}" for m in memories]) if memories else "No facts stored yet."
    
    rem_section = "Active reminders:\n" + "\n".join([
        f"- \"{r['message']}\" at {r['fire_at']}"
        for r in reminders
    ]) if reminders else "No active reminders."
    
    return f"""You are Kai, a warm and helpful personal assistant. Current time: {now}.

{mem_section}

{rem_section}

Your job:
1. Chat naturally and be helpful.
2. When the user tells you something to remember (a fact, preference, detail), extract it and include this at the END of your reply:
   MEMORY_ACTION: {{"action":"save","content":"<concise fact>"}}
   
3. When the user asks for a reminder, include this at the END:
   REMINDER_ACTION: {{"message":"<reminder text>","delayMs":<milliseconds from now>}}
   For "tomorrow" use 86400000 (24 hours), "in 1 hour" use 3600000, "in 5 minutes" use 300000.
   
4. When asked what you know about them, reference the facts above.
5. Be concise. Don't mention the JSON blocks in your visible response."""

def parse_actions(text: str) -> tuple[str, list[dict], list[dict]]:
    """Extract action commands from Claude's response"""
    clean_text = text
    memory_actions = []
    reminder_actions = []
    
    # Extract memory actions
    mem_pattern = r'MEMORY_ACTION:\s*(\{[^\n]+\})'
    for match in re.finditer(mem_pattern, text):
        try:
            action = json.loads(match.group(1))
            memory_actions.append(action)
            clean_text = clean_text.replace(match.group(0), "").strip()
        except json.JSONDecodeError:
            pass
    
    # Extract reminder actions
    rem_pattern = r'REMINDER_ACTION:\s*(\{[^\n]+\})'
    for match in re.finditer(rem_pattern, text):
        try:
            action = json.loads(match.group(1))
            reminder_actions.append(action)
            clean_text = clean_text.replace(match.group(0), "").strip()
        except json.JSONDecodeError:
            pass
    
    return clean_text.strip(), memory_actions, reminder_actions

def call_claude(system_prompt: str, user_message: str) -> str:
    """Call Claude API"""
    try:
        message = claude_client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1000,
            system=system_prompt,
            messages=[{"role": "user", "content": user_message}]
        )
        return message.content[0].text
    except Exception as e:
        print(f"Claude API error: {e}")
        return f"Sorry, I encountered an error: {str(e)}"

# ============ TELEGRAM FUNCTIONS ============

async def send_telegram_message(chat_id: int, text: str):
    """Send a message via Telegram"""
    try:
        async with httpx.AsyncClient() as client:
            await client.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                json={"chat_id": chat_id, "text": text},
                timeout=10.0
            )
    except Exception as e:
        print(f"Error sending Telegram message: {e}")

# ============ WEBHOOK HANDLER ============

@app.post("/webhook")
async def handle_webhook(request: Request):
    """Main webhook endpoint for Telegram messages"""
    try:
        update = await request.json()
        
        # Ignore non-message updates
        if "message" not in update:
            return {"ok": True}
        
        msg = update["message"]
        chat_id = msg["chat"]["id"]
        user_id = str(msg["from"]["id"])
        text = msg.get("text", "").strip()
        
        if not text:
            return {"ok": True}
        
        # Fetch context from database
        memories = get_memories(user_id)
        pending_reminders = get_pending_reminders(user_id)
        
        # Build system prompt and call Claude
        system = build_system_prompt(memories, pending_reminders)
        raw_response = call_claude(system, text)
        
        # Parse response for actions
        clean_response, memory_actions, reminder_actions = parse_actions(raw_response)
        
        # Process memory actions
        for action in memory_actions:
            if action.get("action") == "save" and action.get("content"):
                save_memory(user_id, action["content"])
        
        # Process reminder actions
        for action in reminder_actions:
            if action.get("message"):
                save_reminder(
                    user_id,
                    action["message"],
                    action.get("delayMs")
                )
        
        # Send reply to user
        await send_telegram_message(chat_id, clean_response)
        
        return {"ok": True}
    
    except Exception as e:
        print(f"Webhook error: {e}")
        return {"ok": False, "error": str(e)}

# ============ HEALTH CHECK ============

@app.get("/health")
async def health_check():
    """Health check endpoint"""
    return {
        "status": "ok",
        "timestamp": datetime.utcnow().isoformat()
    }

# ============ ROOT ENDPOINT ============

@app.get("/")
async def root():
    """Root endpoint"""
    return {
        "service": "Personal Assistant Bot",
        "status": "running"
    }

# Run with: uvicorn main:app --host 0.0.0.0 --port 8000

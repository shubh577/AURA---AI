"""
AURA Prototype - Single file

Features:
- PySide6 UI: frameless, pinned to bottom-right, minimizable to circular logo
- WhatsApp/Instagram-like chat bubbles (user left, AURA right)
- Mic toggle for dictation; recognized speech appears in input box
- Send text -> planner_agent -> returns a 'plan' dict (see format below)
- execute_plan reads plan dict keys: urls, apps, close_apps, speak, act, reminder, todo
- AURA responses are shown as copyable boxes (Copy button) and spoken via pyttsx3
- Persistent memory saved to memory.json (reminders, meetings, simple facts)
- Reminder scheduling & runtime opening of stored meeting links
- Simple popup notifications (Qt widget)
- Clear extension points for Gemini/LLM integration

How planner should return 'plan' dict (example):
{
  "urls": ["https://www.youtube.com"],
  "apps": ["notepad.exe"],
  "close_apps": ["chrome.exe"],
  "speak": "Opening YouTube for you.",
  "act": {"task":"compose_email","params":{"to":"ceo@example.com","subject":"Weekly Sales", "body":"..."}},
  "reminder": {"when":"2025-12-01T15:00:00", "message":"Join Google Meet", "action":{"open_url":"https://meet.google.com/..."}},
  "todo": {"add":"Study for exam tomorrow"}
}

Notes:
- For safety, planner results are processed conservatively.
- If you have a local LLM endpoint that can return JSON plans, set LLM_URL environment variable.
"""

import os
import sys
import json
import time
import threading
import webbrowser
import subprocess
import traceback
from datetime import datetime, timedelta

import requests
import pyttsx3
import speech_recognition as sr
import pyperclip
import pyautogui

import os, json, re, threading, time
from datetime import datetime
import webbrowser, subprocess, pyperclip
from dateutil import parser as dtparser
from apscheduler.schedulers.background import BackgroundScheduler
import shlex
import threading

import dateutil.parser
from datetime import datetime, timedelta
import google.generativeai as genai

# --- Add these imports near the top of your file ---
from PySide6 import QtWidgets, QtCore, QtGui
import json
import threading
from datetime import datetime, timedelta, timezone
import os
# -------------------------

from dateutil import parser as du_parser
from dateutil import tz
try:
    import dateparser
    DATEPARSER_AVAILABLE = True
except Exception:
    DATEPARSER_AVAILABLE = False

import json
MEMORY_FILE = os.path.join(os.path.dirname(__file__), "aura_memory.txt")

# --- Gemini SDK (Google Generative AI) ---
try:
    import google.generativeai as genai
except Exception:
    genai = None


window = None  # Global UI instance reference

# -------------------- ENV KEYS (single source of truth) --------------------
from dotenv import load_dotenv
load_dotenv()

GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

# Prefer explicit GEMINI_* environment names
PLANNER_KEY = (os.getenv("GEMINI_PLANNER_KEY") or os.getenv("PLANNER_KEY") or "").strip()
CHAT_KEY    = (os.getenv("GEMINI_CHAT_KEY")    or os.getenv("CHAT_KEY")    or "").strip()
PROMPT_KEY  = (os.getenv("GEMINI_PROMPT_KEY")  or os.getenv("PROMPT_KEY")  or "").strip()
IMAGE_KEY   = (os.getenv("GEMINI_IMAGE_KEY")   or os.getenv("IMAGE_KEY")   or "").strip()
ACTION_KEY  = (os.getenv("GEMINI_ACTION_KEY")  or os.getenv("ACTION_KEY")  or "").strip()

# LLM_URL optional local endpoint
LLM_URL = os.getenv("LLM_URL", "").strip()

print(f"[DEBUG] PLANNER_KEY present: {bool(PLANNER_KEY)} | CHAT_KEY present: {bool(CHAT_KEY)}")

# === Gemini API configuration (defensive) ===
try:
    import google.generativeai as genai
except Exception:
    genai = None

try:
    if genai:
        # Prefer PLANNER_KEY for generative configuration,
        # otherwise use CHAT_KEY. If neither present, leave genai unconfigured.
        if PLANNER_KEY:
            try:
                genai.configure(api_key=PLANNER_KEY)
                print("[Gemini] configured with PLANNER_KEY.")
            except Exception as e:
                print("[Gemini] configure with PLANNER_KEY error:", e)
        elif CHAT_KEY:
            try:
                genai.configure(api_key=CHAT_KEY)
                print("[Gemini] configured with CHAT_KEY.")
            except Exception as e:
                print("[Gemini] configure with CHAT_KEY error:", e)
        else:
            print("[Gemini] No API key configured; genai features disabled.")
    else:
        print("[Gemini] SDK not available.")
except Exception as e:
    print("[Gemini] Configuration error:", e)

# in-memory chat history (kept for session follow-ups)
CHAT_HISTORY = []  # list of (who, text, iso_ts)
MAX_CHAT_HISTORY = 200

from PySide6 import QtCore, QtGui, QtWidgets

import queue as _queue

# Optional helpers
try:
    import psutil
except Exception:
    psutil = None

AUTO_EXECUTE = True   # For demo; set False to require confirmations in future


AURA_LOGO_PATH = os.getenv("AURA_LOGO_PATH", "").strip()  # path to circle logo PNG (optional)

# ----------------- App/URL map -----------------
# Map friendly app names to URLs or system paths.
APP_MAP = {
    "youtube": {"type":"url", "value":"https://www.youtube.com"},
    "youtube music": {"type":"url", "value":"https://music.youtube.com"},
    "gmail": {"type":"url", "value":"https://mail.google.com"},
    "gmail_compose": {"type":"url", "value":"https://mail.google.com/mail/?view=cm&fs=1"},
    "google": {"type":"url", "value":"https://www.google.com"},
    "canva": {"type":"url", "value":"https://www.canva.com"},
    "linkedin": {"type":"url", "value":"https://www.linkedin.com"},
    "instagram": {"type":"url", "value":"https://www.instagram.com"},
    "spotify": {"type":"url", "value":"https://open.spotify.com"},
    "lovable": {"type":"url", "value":"https://lovable.dev"},
    "notepad": {"type":"exe", "value": r"C:\Windows\system32\notepad.exe"},
    "bookmyshow": {"type":"url", "value":"https://in.bookmyshow.com"},
    # Add local app paths if you want to open native apps:
    # "notepad": {"type":"exe", "value": r"C:\Windows\system32\notepad.exe"},
}

# --- TTS Engine
tts_engine = pyttsx3.init()
tts_engine.setProperty("rate", 165)

_tts_queue = _queue.Queue()

def _tts_worker_loop():
    """Background TTS worker: consumes text messages and calls runAndWait() in a single thread."""
    while True:
        try:
            text = _tts_queue.get()
            if text is None:
                break
            # call TTS synchronously only in this thread
            try:
                tts_engine.stop()
                tts_engine.say(str(text))
                tts_engine.runAndWait()
            except Exception as e:
                print("TTS worker error:", e)
        except Exception as e:
            print("TTS loop top-level error:", e)

_tts_thread = threading.Thread(target=_tts_worker_loop, daemon=True)
_tts_thread.start()

def speak_async(text):
    """Queue text for speaking by the dedicated TTS worker."""
    try:
        _tts_queue.put_nowait(text)
    except Exception as e:
        print("Failed to queue TTS:", e)

def call_gemini_raw(api_key, prompt, temperature=0.0, timeout=12):
    """
    Robust Gemini call wrapper. api_key is the API key string to configure genai with.
    Returns plain text (string) or None on failure.
    """
    if genai is None:
        print("[Gemini] SDK not installed.")
        return None
    if not api_key:
        print("[Gemini] No API key provided.")
        return None

    # try to configure (some versions require configure)
    try:
        genai.configure(api_key=api_key)
    except Exception as e:
        # not fatal — some SDK builds don't require this call
        pass

    last_exc = None

    # Try several API shapes (attempt to be compatible with multiple genai versions)
    try:
        # new style
        ModelCls = getattr(genai, "GenerativeModel", None)
        if ModelCls:
            model = ModelCls(GEMINI_MODEL)
            try:
                resp = model.generate_content(prompt)
            except Exception:
                resp = model.generate_content([prompt])
            # resp may have .text or .candidates
            if hasattr(resp, "text") and resp.text:
                return resp.text
            if hasattr(resp, "candidates") and resp.candidates:
                cand = resp.candidates[0]
                content = getattr(cand, "content", None)
                if isinstance(content, list):
                    for piece in content:
                        if hasattr(piece, "text") and piece.text:
                            return piece.text
                    return str(content)
                return getattr(content, "text", None) or str(content)
    except Exception as e:
        last_exc = e

    try:
        # legacy style: genai.get_model(...).predict(...)
        model2 = getattr(genai, "get_model", None)
        if model2:
            model = genai.get_model(GEMINI_MODEL)
            try:
                resp2 = model.predict(prompt)
            except Exception:
                resp2 = model.predict([prompt])
            if hasattr(resp2, "text") and resp2.text:
                return resp2.text
            if hasattr(resp2, "candidates") and resp2.candidates:
                cand = resp2.candidates[0]
                content = getattr(cand, "content", None)
                if isinstance(content, list):
                    for piece in content:
                        if hasattr(piece, "text") and piece.text:
                            return piece.text
                    return str(content)
                return getattr(content, "text", None) or str(content)
    except Exception as e:
        last_exc = e

    try:
        # old flat API
        if hasattr(genai, "generate"):
            resp3 = genai.generate(model=GEMINI_MODEL, prompt=prompt, temperature=temperature)
            if isinstance(resp3, dict):
                if "text" in resp3 and resp3["text"]:
                    return resp3["text"]
                if "candidates" in resp3 and resp3["candidates"]:
                    c = resp3["candidates"][0]
                    if isinstance(c, dict) and "content" in c:
                        cont = c["content"]
                        if isinstance(cont, str):
                            return cont
                        if isinstance(cont, list) and cont:
                            return cont[0].get("text") if isinstance(cont[0], dict) else str(cont)
    except Exception as e:
        last_exc = e

    print("[Gemini] call error:", last_exc)
    return None


def call_gemini_for(api_key, prompt, temperature=0.0, timeout=12):
    """Convenience wrapper to call Gemini with a specific API key string."""
    return call_gemini_raw(api_key, prompt, temperature=temperature, timeout=timeout)

# -------------------- MEMORY --------------------
# ==== Memory & Reminders Helpers ====
MEMORY_FILE = os.path.join(os.path.dirname(__file__), "aura_memory.json")
# in-memory cache used at runtime
APP_MEMORY = {
    "facts": {},        # factual memory (name, age, etc)
    "reminders": [],    # list of scheduled reminders (dicts)
    "todos": []         # todo items
}

def load_memory():
    """Load memory from disk into APP_MEMORY."""
    try:
        if os.path.exists(MEMORY_FILE):
            with open(MEMORY_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                APP_MEMORY.update({k: data.get(k, APP_MEMORY[k]) for k in APP_MEMORY.keys()})
        return APP_MEMORY
    except Exception as e:
        print("[Memory] load error:", e)
        return APP_MEMORY

def save_memory():
    """Persist APP_MEMORY to disk."""
    try:
        with open(MEMORY_FILE, "w", encoding="utf-8") as f:
            json.dump(APP_MEMORY, f, ensure_ascii=False, indent=2)
        print("[Memory] saved.")
    except Exception as e:
        print("[Memory] save error:", e)

def parse_iso_when(when_str):
    """
    Accepts ISO-8601 like '2025-11-12T18:00:00Z' or naive '2025-11-12T18:00:00'
    Returns timezone-aware datetime in local system timezone.
    """
    if not when_str:
        return None
    s = when_str.strip()
    try:
        if s.endswith("Z"):
            # convert to +00:00
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            # assume local time
            return dt.replace(tzinfo=None)
        return dt.astimezone()  # convert to local tz
    except Exception:
        try:
            # fallback: try common parse like "in 5 minutes" is not handled here
            return None
        except Exception:
            return None

# scheduler storage for active timers so they can be canceled later
_ACTIVE_TIMERS = []

def schedule_reminder(rem_dict, ui_callback=None):
    """
    rem_dict expected format: {'when': ISO-8601 str OR epoch seconds, 'text': 'message', 'id': optional}
    Schedules a threading.Timer that will call ui_callback(text) when time arrives.
    """
    try:
        if isinstance(rem_dict, dict):
            when = rem_dict.get("when")
            text = rem_dict.get("text") or rem_dict.get("message") or "Reminder"
            # if epoch seconds given
            if isinstance(when, (int, float)):
                dt = datetime.fromtimestamp(when, tz=timezone.utc).astimezone()
            else:
                dt = parse_iso_when(when)
            if dt is None:
                print("[Reminder] invalid time:", when)
                return False

            now = datetime.now(dt.tzinfo) if dt.tzinfo else datetime.now()
            delay = (dt - now).total_seconds()
            if delay < 0:
                print("[Reminder] time already passed:", dt)
                return False

            def _fire():
                # append to UI + speak
                msg = f"Reminder: {text}"
                print("[Reminder fired]:", msg)
                if ui_callback:
                    try:
                        QtCore.QTimer.singleShot(0, lambda: ui_callback(msg, "aura"))
                    except Exception:
                        # fallback: call directly (non-UI thread risk)
                        try:
                            ui_callback(msg, "aura")
                        except Exception:
                            pass
                # remove from memory.reminders
                try:
                    APP_MEMORY["reminders"] = [r for r in APP_MEMORY["reminders"] if r.get("id") != rem_dict.get("id")]
                    save_memory()
                except Exception:
                    pass

            t = threading.Timer(delay, _fire)
            t.daemon = True
            t.start()
            _ACTIVE_TIMERS.append({"id": rem_dict.get("id"), "timer": t})
            print("[Reminder] scheduled in", delay, "seconds")
            return True
    except Exception as e:
        print("[Reminder] schedule error:", e)
    return False

def cancel_all_reminders():
    try:
        for entry in _ACTIVE_TIMERS:
            try:
                entry["timer"].cancel()
            except Exception:
                pass
        _ACTIVE_TIMERS.clear()
    except Exception:
        pass
# Initialize memory on import
load_memory()

def add_reminder_to_memory(rem):
    memory.setdefault("reminders", []).append(rem)
    save_memory(memory)

def add_meeting_to_memory(meeting):
    memory.setdefault("meetings", []).append(meeting)
    save_memory(memory)

def add_fact(key, value):
    memory.setdefault("facts", {})[key] = value
    save_memory(memory)

def add_todo(item):
    memory.setdefault("todos", []).append({"task": item, "created": datetime.now().isoformat(), "done": False})
    save_memory(memory)

# -------------------- LLM / Planner --------------------
def call_local_llm(pl_endpoint, prompt, timeout=8):
    """Call your local LLM endpoint (Flask etc.). Must return JSON or text containing JSON plan."""
    try:
        r = requests.post(pl_endpoint, json={"prompt": prompt}, timeout=timeout)
        if r.status_code == 200:
            try:
                return r.json()
            except Exception:
                return {"text": r.text}
        return {"text": f"LLM endpoint error {r.status_code}"}
    except Exception as e:
        return {"text": f"LLM call failed: {e}"}

def call_cloud_llm(api_key, prompt, timeout=8):
    """
    Cloud LLM wrapper:
    - If LLM_URL is set, call local LLM endpoint.
    - Else, if api_key provided and genai available, call Gemini and return {"text": text}
    - Else return None.
    """
    if LLM_URL:
        return call_local_llm(LLM_URL, prompt, timeout=timeout)

    # prefer API key passed in; else fallback to PLANNER_KEY
    key_to_use = api_key or PLANNER_KEY
    if key_to_use:
        text = call_gemini_raw(key_to_use, prompt, temperature=0.0, timeout=timeout)
        if text is not None:
            return {"text": text}
    return None

def extract_plan_from_text(text):
    """Extract JSON plan object from returned text if present."""
    if not text:
        return None
    txt = text.strip()
    s = txt.find("{")
    e = txt.rfind("}")
    if s == -1 or e == -1 or e <= s:
        return None
    try:
        return json.loads(txt[s:e+1])
    except Exception:
        return None

def _is_simple_local_cmd(text: str) -> bool:
    """Return True only for short, explicit system commands like 'open youtube'."""
    t = text.lower().strip()
    if not t:
        return False

    # very short or direct system commands only
    if t in {"open youtube", "open canva", "open gmail", "open spotify",
             "open linkedin", "open instagram", "open google", "open chrome",
             "close tab", "close browser", "what time is it", "tell time"}:
        return True

    # single-word commands like 'time', 'youtube', 'canva'
    if t in {"youtube", "canva", "time"}:
        return True

    # otherwise not simple enough — let Gemini decide
    return False

def _is_chitchat(text):
    t = text.lower().strip()
    # short chit-chat phrases — treat via chat agent instead of planner
    chit = ["hi", "hello", "hey", "how are you", "good morning", "good evening", "bye", "thank you", "thanks", "what's up", "are you awake"]
    # exact short strings or startswith
    if len(t.split()) <= 4 and any(t.startswith(c) or t == c for c in chit):
        return True
    # specific question forms
    if t.endswith("?") and any(w in t for w in ["how", "what", "why", "when", "who"]):
        # still treat as chat if it looks like a general question
        return True
    return False

def planner_agent(user_text):
    """
    Unified planner using Gemini (PLANNER_KEY) or local LLM (LLM_URL).
    Always returns a dict plan (never None). Uses CHAT_KEY for short chit-chat.
    Falls back to strict_fallback_parse ONLY if no cloud/local planner is available.
    """
    print("[Planner] in:", user_text)

    # quick check for short chat
    def is_short_chat(t):
        tt = t.lower().strip()
        if not tt:
            return False
        if len(tt.split()) <= 5 and any(tt.startswith(w) for w in ("hi", "hello", "hey", "bye", "thanks", "thank")):
            return True
        if tt.endswith("?") and any(w in tt for w in ("what", "how", "why", "when", "who")) and len(tt.split()) <= 12:
            return True
        return False

    try:
        # 1) Short chit-chat -> use CHAT_KEY for concise reply
        if is_short_chat(user_text) and CHAT_KEY:
            try:
                chat_prompt = f"You are AURA, a friendly concise assistant. Reply in one or two sentences to: {user_text}"
                chat_res = call_gemini_for(CHAT_KEY, chat_prompt, timeout=5)
                if chat_res:
                    return {"speak": chat_res.strip()}
            except Exception as e:
                print("[Planner] chat call failed:", e)

        # 2) Build planner system prompt including memory & recent chat
        facts = memory.get("facts", {}) if isinstance(memory, dict) else {}
        last_msgs = CHAT_HISTORY[-10:]
        recent = "\n".join([f"{'User' if who=='user' else 'AURA'}: {txt}" for who, txt, ts in last_msgs]) if last_msgs else ""
        mem_facts_str = json.dumps(facts, ensure_ascii=False) if facts else "{}"

        system_prompt = f"""
You are AURA Planner — the decision engine. Return ONLY a single JSON object (no extra text).
Allowed keys (optional): urls (list), apps (list), close_apps (list), close_tabs (list),
speak (string), act (dict), reminder (dict), todo (dict), facts (dict), memory (string),
generate (string), images (list), copy (string).

Provide ISO-8601 UTC timestamps for reminders (e.g. 2025-11-12T15:00:00Z).

Known memory facts:
{mem_facts_str}

Recent chat:
{recent}

User: {user_text}

Return just the JSON object now.
"""

        # 3) Local LLM if present
        if LLM_URL:
            try:
                res = call_local_llm(LLM_URL, system_prompt, timeout=8)
                if isinstance(res, dict) and "text" in res and res["text"]:
                    plan = extract_plan_from_text(res["text"])
                    if isinstance(plan, dict):
                        print("[Planner] got plan from local LLM:", plan)
                        return plan
                    return {"speak": res["text"].strip()}
            except Exception as e:
                print("[Planner] local LLM error:", e)

        # 4) Cloud planner (Gemini) if present
        if PLANNER_KEY:
            try:
                res = call_cloud_llm(PLANNER_KEY, system_prompt, timeout=9)
                if isinstance(res, dict) and "text" in res and res["text"]:
                    text = res["text"].strip()
                    plan = extract_plan_from_text(text)
                    if isinstance(plan, dict):
                        # sanitize keys
                        allowed = {"urls","apps","close_apps","close_tabs","speak","act","reminder","todo","facts","memory","generate","images","copy"}
                        sanitized = {k:v for k,v in plan.items() if k in allowed}
                        print("[Planner] got plan from LLM:", sanitized)
                        return sanitized
                    # If planner returned plain text and we have CHAT_KEY, ask chat model for concise text
                    if CHAT_KEY:
                        try:
                            chat_res = call_gemini_for(CHAT_KEY, f"You are AURA. Reply concisely to: {user_text}", timeout=4)
                            if chat_res:
                                return {"speak": chat_res.strip()}
                        except Exception:
                            pass
                    return {"speak": text}
            except Exception as e:
                print("[Planner] cloud planner error:", e)

        # 5) If no planner (cloud/local) was available, try strict fallback (local exact matches)
        if not PLANNER_KEY and not LLM_URL:
            try:
                fb = strict_fallback_parse(user_text)
                if isinstance(fb, dict):
                    print("[Planner] strict fallback used:", fb)
                    return fb
            except Exception as e:
                print("[Planner] strict fallback error:", e)

    except Exception as ex:
        print("[Planner] Unexpected exception:", ex)

    # Default speak fallback (safe)
    print("[Planner] No valid plan generated by LLM; returning default speak message.")
    return {"speak": "Sorry, I couldn't plan that. Please try rephrasing."}


def strict_fallback_parse(user_input: str):
    """
    Strict fallback — acts only on exact known commands.
    Returns a structured plan dict.
    """

    text = user_input.lower().strip()
    plan = {}

    # Common strict matches for opening major websites
    site_map = {
        "open youtube": "https://www.youtube.com",
        "open linkedin": "https://www.linkedin.com",
        "open facebook": "https://www.facebook.com",
        "open instagram": "https://www.instagram.com",
        "open canva": "https://www.canva.com",
        "open gmail": "https://mail.google.com",
        "open gemini": "https://gemini.google.com",
        "open chatgpt": "https://chat.openai.com",
        "open spotify": "https://open.spotify.com",
        "open telegram": "https://web.telegram.org",
        "open twitter": "https://x.com",
        "open pinterest": "https://in.pinterest.com",
        "open grok": "https://x.ai",
        "open discord": "https://discord.com",
        "open twitch": "https://www.twitch.tv",
        "open reddit": "https://www.reddit.com",
        "open amazon": "https://www.amazon.in",
        "open flipkart": "https://www.flipkart.com",
        "open zara": "https://www.zara.com/in/",
        "open nykaa": "https://www.nykaa.com",
        "open book my show": "https://in.bookmyshow.com",
    }

    # Strict app openers (Windows executables)
    app_map = {
        "open whatsapp": "whatsapp",
        "open chrome": "chrome",
        "open browser": "chrome",
        "open settings": "ms-settings:",
        "open file explorer": "explorer",
        "open cmd": "cmd",
        "open clock": "ms-clock:",
    }

    # Exact matches only
    if text in site_map:
        plan["urls"] = [site_map[text]]
        plan["speak"] = f"Opening {text.split()[-1].capitalize()}."
        return plan

    elif text in app_map:
        plan["apps"] = [app_map[text]]
        plan["speak"] = f"Opening {text.split()[-1].capitalize()}."
        return plan

    elif re.match(r"^what('?s| is) the time$", text):
        from datetime import datetime
        now = datetime.now().strftime("%I:%M %p")
        plan["speak"] = f"The time is {now}."
        plan["time"] = now
        return plan

    elif text in ["hi", "hello", "hey"]:
        plan["speak"] = "Hello there! How can I help you?"
        return plan

    elif text in ["bye", "goodbye", "exit"]:
        plan["speak"] = "Goodbye! Have a great day!"
        return plan

    # If nothing matches, no fallback — let Gemini handle it.
    return None

# -------------------- ACTION EXECUTION --------------------
reminder_timers = []

def schedule_reminder(rem):
    """Schedule a reminder dict {'when':ISO,'message':..., 'action':{...}}"""
    try:
        when = datetime.fromisoformat(rem["when"])
    except Exception:
        # maybe it's relative? ignore
        print("Invalid reminder time:", rem)
        return False
    delta = (when - datetime.now()).total_seconds()
    if delta < 0:
        delta = 0.1
    def _fire():
        try:
            msg = rem.get("message", "Reminder")
            # speak
            speak_async(f"Reminder: {msg}")
            # perform attached action if exists
            action = rem.get("action")
            if action:
                if "open_url" in action:
                    webbrowser.open(action["open_url"], new=2)
                # extend for other actions
        except Exception as e:
            print("Reminder fire error:", e)
    t = threading.Timer(delta, _fire)
    t.daemon = True
    t.start()
    reminder_timers.append(t)
    return True

def open_urls_thread(urls):
    for u in urls:
        try:
            if not u.startswith("http"):
                u = "https://" + u
            webbrowser.open(u, new=2)
            time.sleep(0.2)
        except Exception as e:
            print("open_urls_thread:", e)

def open_apps_thread(apps):
    for a in apps:
        try:
            if os.name == "nt":
                if os.path.exists(a):
                    os.startfile(a)
                else:
                    subprocess.Popen([a], shell=True)
            else:
                subprocess.Popen([a])
            time.sleep(0.2)
        except Exception as e:
            print("open_apps_thread:", e)

def close_apps_thread(list_names):
    if not psutil:
        print("psutil missing, cannot close apps")
        return
    for name in list_names:
        for proc in psutil.process_iter(['name']):
            try:
                if name.lower() in (proc.info['name'] or "").lower():
                    proc.terminate()
            except Exception:
                pass

def act_agent(act):
    """Handle higher-level tasks. Returns optional message text."""
    try:
        task = act.get("task")
        params = act.get("params", {})
        if task == "compose_email":
            # If LLM configured, request a draft; else return placeholder text
            prompt = f"Write a short professional email: {params.get('text','')}"
            res = call_cloud_llm(PLANNER_KEY or LLM_URL, prompt, timeout=6)
            if isinstance(res, dict) and "text" in res:
                return res["text"]
            return "Prepared an email draft. (No LLM configured)"
        if task == "store_meeting":
            # store meeting link/time in memory (naive)
            txt = params.get("text","")
            # try to find link
            import re
            m = re.search(r"(https?://\S+)", txt)
            link = m.group(1) if m else None
            # try find time (not robust)
            mem = {"text": txt, "link": link, "added": datetime.now().isoformat()}
            add_meeting_to_memory(mem)
            return "Saved meeting details to memory."
        # add more tasks...
    except Exception as e:
        print("act_agent error:", e)
    return None

# ---- execute_plan replacement ----
def execute_plan(plan, ui_callback=None, ask_confirm=False):
    """
    Scalable executor for planner dict output.
    ui_callback(text, source) is optional and used to show messages in UI.
    """
    def say(text):
        # UI + TTS
        if ui_callback:
            try:
                ui_callback(text, "aura")
            except Exception:
                pass
        speak_async(text)

    if not plan or not isinstance(plan, dict):
        say("I couldn't understand the plan.")
        return

    print(f"[Executor] Received plan: {plan}")

    # 1) Memory updates
    if "memory" in plan:
        try:
            # planner may return string or dict
            if isinstance(plan["memory"], dict):
                memory.update(plan["memory"])
            elif isinstance(plan["memory"], str):
                # append to a note list
                memory.setdefault("notes", []).append(plan["memory"])
            save_memory()
            print("[Executor] Memory saved.")
        except Exception as e:
            print("_handle_memory error:", e)

    # 2) Facts (structured small items)
    if "facts" in plan and isinstance(plan["facts"], dict):
        try:
            memory.setdefault("facts", {}).update(plan["facts"])
            save_memory()
            print("[Executor] Facts saved:", plan["facts"])
        except Exception as e:
            print("_handle_facts error:", e)

    # 3) Speak
    if "speak" in plan and plan.get("speak"):
        say(plan["speak"])

    # 4) URLs
    if "urls" in plan and plan.get("urls"):
        urls = plan["urls"]
        if isinstance(urls, str):
            urls = [urls]
        for u in urls:
            try:
                webbrowser.open(u, new=2)
                print("[Executor] Opening URL:", u)
            except Exception as e:
                print("[Executor] URL open error:", e)
                say(f"Failed to open {u}")

    # 5) Apps
    if "apps" in plan and plan.get("apps"):
        apps = plan["apps"]
        if isinstance(apps, str):
            apps = [apps]
        for a in apps:
            ok, msg = do_open_app(a)
            print("[Executor] Opened app:", a, ok, msg)
            if not ok:
                say(msg)

    # 6) close_tabs (exact match heuristic)
    if "close_tabs" in plan and plan.get("close_tabs"):
        lst = plan["close_tabs"]
        if isinstance(lst, str):
            lst = [lst]
        for term in lst:
            # heuristic: send Ctrl+W while the active tab's title contains term — best-effort
            if pyautogui:
                try:
                    # it's hard to reliably target tabs; we use a best-effort sequence:
                    # press ctrl+tab to cycle and close if title matches (not perfect).
                    pyautogui.hotkey('ctrl','w')
                    print("[Executor] Sent close tab hotkey.")
                except Exception as e:
                    print("[Executor] close_tabs error:", e)
            else:
                print("[Executor] pyautogui not installed; cannot close tabs programmatically.")

    # 7) close_apps
    if "close_apps" in plan and plan.get("close_apps"):
        apps = plan["close_apps"]
        if isinstance(apps, str):
            apps = [apps]
        for a in apps:
            ok, msg = do_close_app_by_name(a)
            print("[Executor] close_app:", ok, msg)
            if not ok:
                say(msg)

    # 8) Reminder
    if "reminder" in plan and plan.get("reminder"):
        try:
            rem = plan["reminder"]
            # rem expected: {'when': ISO8601, 'text': '...'}
            when = rem.get("when") or rem.get("time") or rem.get("at")
            text = rem.get("text") or rem.get("message") or rem.get("note","Reminder")
            # if human phrase present, interpret
            if when and not when.endswith("Z") and " " in when:
                parsed = interpret_time_request(when)
            else:
                parsed = when
            if not parsed:
                # maybe planner provided human text in 'time' subkey
                if "time" in rem and isinstance(rem["time"], str):
                    parsed = interpret_time_request(rem["time"])
            if parsed:
                # store reminder and schedule
                memory.setdefault("reminders", []).append({"when": parsed, "text": text})
                save_memory()
                schedule_reminder({"when": parsed, "text": text})
                say(f"Reminder set for {parsed}.")
            else:
                say("I couldn't understand the reminder time. Please give a clear time.")
        except Exception as e:
            print("[Executor] _handle_reminder error:", e)
            say("I couldn't set the reminder.")

    # 9) Todo
    if "todo" in plan and plan.get("todo"):
        try:
            t = plan["todo"]
            # accept dict or list or string
            if isinstance(t, str):
                memory.setdefault("todos", []).append({"task": t, "done": False})
            elif isinstance(t, dict):
                memory.setdefault("todos", []).append({**t, "done": t.get("done", False)})
            elif isinstance(t, list):
                for item in t:
                    if isinstance(item, str):
                        memory.setdefault("todos", []).append({"task": item, "done": False})
                    elif isinstance(item, dict):
                        memory.setdefault("todos", []).append({**item, "done": item.get("done", False)})
            save_memory()
            say("To-do updated.")
        except Exception as e:
            print("[Executor] _handle_todo error:", e)

    # 10) suggestions/time keys
    if "suggestions" in plan and plan.get("suggestions"):
        # planner provided suggestion list or text
        s = plan["suggestions"]
        if isinstance(s, str):
            say(s)
        elif isinstance(s, list):
            say("Here are some suggestions:")
            for item in s[:6]:
                say(item)

    if "time" in plan and plan.get("time"):
        # 'time' key is intended to represent precise time for "tell time"
        t = plan["time"]
        # if planner already gave ISO time, use it; else interpret
        if isinstance(t, str):
            parsed = interpret_time_request(t) or t
            try:
                if parsed:
                    # present in user-friendly local time
                    dt = du_parser.parse(parsed)
                    local = dt.astimezone(tz.tzlocal())
                    say(f"The time is {local.strftime('%I:%M %p on %b %d, %Y')}")
                else:
                    say(f"The time is {datetime.now().strftime('%I:%M %p')}")
            except Exception:
                say(f"The time is {datetime.now().strftime('%I:%M %p')}")
    # 11) generate (defer to existing generate handler if any)
    if "generate" in plan and plan.get("generate"):
        try:
            _handle_generate(plan["generate"], ui_callback=ui_callback)
        except Exception as e:
            print("execute_plan generate error:", e)

    # 12) images / copy keys (best-effort)
    if "images" in plan and plan.get("images"):
        # images might contain textual prompts (we'll speak a confirmation)
        say("I prepared image prompts for you. Open the images panel to see them.")

    if "copy" in plan and plan.get("copy"):
        # instruct UI to create a copy box, if supported
        if ui_callback:
            ui_callback(plan["copy"], "aura_copy")
        say("I put the text in a copy box.")

    # final: ensure memory saved
    try:
        save_memory()
    except Exception:
        pass

# ---------- Integration helpers ----------
# You should create a global variable 'MAIN_WINDOW' after creating MainWindow instance.
MAIN_WINDOW = None

def ui_callback(text, source="aura"):
    """Global UI callback used by executor to show messages in UI via MAIN_WINDOW."""
    try:
        if MAIN_WINDOW:
            MAIN_WINDOW.ui_add_bubble(text, source)
        else:
            print("[UI CALLBACK]", source, text)
    except Exception as e:
        print("[UI CALLBACK error]:", e)

# Example handler for "reminder" key in plan that execute_plan should call:
def _handle_reminder(value, ui_callback=None):
    """
    value expected as {'when': ISO string, 'text': '...'}
    """
    try:
        ok = schedule_reminder(value, ui_callback=(ui_callback or ui_callback))
        if ok:
            APP_MEMORY.setdefault("reminders", []).append(value)
            save_memory()
            if ui_callback:
                ui_callback("Reminder scheduled.", "aura")
        else:
            if ui_callback:
                ui_callback("Failed to schedule reminder.", "aura")
    except Exception as e:
        print("[Executor] _handle_reminder error:", e)
        if ui_callback:
            ui_callback("Unable to schedule reminder.", "aura")

# ------------------------------
# Handlers for different plan keys
# ------------------------------

# ---- local suggestion engine (best-effort friendly suggestions) ----

def local_suggestion_engine():
    """
    Returns a short list of suggestions given memory (todos + interests).
    """
    suggestions = []
    try:
        # If there are pending todos, prefer that
        todos = [t for t in memory.get("todos", []) if not t.get("done")]
        if todos:
            suggestions.append("You have pending tasks. Would you like me to show your to-do list or set a timer for a focused session?")
            # also offer small breaks if only long tasks
            suggestions.append("Take a 10-minute break and stretch — I can set a quick timer.")
        # Interests-based suggestions
        interests = memory.get("interests", [])
        if interests:
            suggestions.append(f"How about something with {interests[0]}? I can open a related game, video, or article.")
        # If no data, general friendly options
        suggestions.extend([
            "Want me to play some music? Say 'play' and the song name.",
            "I can find a short game or a quiz for you to pass time.",
            "Would you like a quick joke or a short story?"
        ])
    except Exception:
        suggestions = ["Would you like a suggestion? I can play music, tell a joke, or show a game."]
    return suggestions

# ---- Time interpretation helper ----

def interpret_time_request(human_time, reference=None):
    """
    Turn a natural-language time phrase into an ISO-8601 UTC datetime string.
    If parsing fails, returns None.
    """
    try:
        now = reference or datetime.now(tz.tzlocal())
        # prefer dateparser for natural phrases when available
        if DATEPARSER_AVAILABLE:
            dt = dateparser.parse(human_time, settings={"RELATIVE_BASE": now})
            if dt is None:
                return None
            # unify to UTC
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=tz.tzlocal())
            dt_utc = dt.astimezone(tz.UTC)
            return dt_utc.isoformat()
        # fallback: try dateutil.parse with some heuristics
        try:
            dt = du_parser.parse(human_time, default=now)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=tz.tzlocal())
            dt_utc = dt.astimezone(tz.UTC)
            return dt_utc.isoformat()
        except Exception:
            return None
    except Exception as e:
        print("interpret_time_request error:", e)
        return None

def _handle_speak(text, ui_callback=None):
    """Speak text using the shared TTS queue and show it in UI via ui_callback."""
    try:
        if not text:
            return
        # Show in UI first
        if ui_callback:
            try:
                ui_callback(text, "aura")
            except Exception:
                # fallback print
                print("[AURA says]:", text)
        else:
            print("[AURA says]:", text)

        # Use shared speak_async queue (already implemented)
        try:
            speak_async(str(text))
        except Exception as e:
            # last resort: spawn a local TTS (very rare)
            print("[TTS fallback error]:", e)
            try:
                local_engine = pyttsx3.init()
                local_engine.say(str(text))
                local_engine.runAndWait()
            except Exception as e2:
                print("[TTS final fallback error]:", e2)

    except Exception as e:
        print("[_handle_speak] error:", e)

def _handle_apps(value, ui_callback=None):
    """
    value expected: list of app names or a single string.
    We'll try to map to APP_MAP; if not found, ask user for clarification.
    """
    try:
        apps = value if isinstance(value, (list,tuple)) else [value]
        opened = []
        for app in apps:
            key = str(app).lower().strip()
            if key in APP_MAP:
                item = APP_MAP[key]
                if item["type"] == "url":
                    _handle_urls([item["value"]], ui_callback=ui_callback)
                    opened.append(item["value"])
                elif item["type"] == "exe":
                    # try to start exe (non-blocking)
                    try:
                        subprocess.Popen(item["value"], shell=True)
                        opened.append(item["value"])
                    except Exception as e:
                        print("[Executor] failed to open exe:", e)
                        if ui_callback: ui_callback(f"Failed to open {app}.", "aura")
                else:
                    if ui_callback: ui_callback(f"Cannot open {app} (unknown mapping).", "aura")
            else:
                # not in map: try a safe fallback – open google search for "open <app>"
                search_url = f"https://www.google.com/search?q={key.replace(' ','+')}"
                _handle_urls([search_url], ui_callback=ui_callback)
                opened.append(search_url)

        if opened:
            msg = "Opened: " + ", ".join(opened)
            if ui_callback: ui_callback(msg, "aura")
            speak_async(msg)

    except Exception as e:
        print("[Executor] _handle_apps error:", e)
        if ui_callback: ui_callback("I couldn't open the requested app.", "aura")
        speak_async("I couldn't open the requested app.")

def _handle_urls(value, ui_callback=None):
    """
    value: list of URLs or single URL string
    Opens each URL using webbrowser.open (non-blocking).
    """
    try:
        urls = value if isinstance(value, (list,tuple)) else [value]
        opened = []
        for u in urls:
            ustr = str(u).strip()
            if not ustr:
                continue
            # sanitize: if not a full URL, turn into search
            if not (ustr.startswith("http://") or ustr.startswith("https://")):
                ustr = "https://www.google.com/search?q=" + ustr.replace(" ", "+")
            webbrowser.open(ustr, new=2)
            opened.append(ustr)

        if opened:
            msg = "Opening: " + ", ".join(opened)
            if ui_callback: ui_callback(msg, "aura")
            speak_async(msg)
    except Exception as e:
        print("[Executor] Error in _handle_urls:", e)
        if ui_callback: ui_callback("Failed to open the URL(s).", "aura")
        speak_async("Failed to open the URL or search.")

def _handle_close_tabs(value, ui_callback=None):
    """
    Prototype behavior: attempt to close current browser tab using Ctrl+W once per request item.
    value: list of keywords or string
    """
    try:
        items = value if isinstance(value, (list, tuple)) else [value]
        if not items:
            if ui_callback: ui_callback("No tabs specified to close.", "aura")
            speak_async("No tabs specified to close.")
            return
        # Use pyautogui if available; otherwise call generic close_tab
        closed = []
        for it in items:
            # we can't target a specific tab reliably in all browsers without automation
            # so we attempt to close the active tab once per requested keyword (best-effort)
            if 'pyautogui' in globals() and pyautogui:
                try:
                    pyautogui.hotkey('ctrl', 'w')
                    closed.append(str(it))
                    time.sleep(0.15)
                except Exception:
                    pass
            else:
                # fallback: attempt to close an app process (if the keyword is a running process)
                if psutil:
                    for proc in psutil.process_iter(['name']):
                        try:
                            if it.lower() in (proc.info['name'] or "").lower():
                                proc.terminate()
                                closed.append(str(it))
                        except Exception:
                            pass
        if closed:
            msg = "Closed tabs/app(s): " + ", ".join(closed)
            if ui_callback: ui_callback(msg, "aura")
            speak_async(msg)
        else:
            if ui_callback: ui_callback("No matching tabs/apps found to close; attempted a generic close.", "aura")
            speak_async("No matching tabs found; attempted generic close.")
    except Exception as e:
        print("[Executor] _handle_close_tabs error:", e)
        if ui_callback: ui_callback("Failed to close tabs.", "aura")
        speak_async("Failed to close tabs.")


def _handle_memory(value, ui_callback=None):
    global memory
    try:
        memory.setdefault("memory_entries", []).append(value)
        save_memory()
        print("[Executor] Memory saved.")
    except Exception as e:
        print("[Executor] _handle_memory error:", e)

def _handle_facts(facts_data, ui_callback=None):
    global memory
    try:
        memory.setdefault("facts", {}).update(facts_data)
        save_memory()
        print(f"[Executor] Facts saved: {facts_data}")
        if ui_callback:
            ui_callback(f"Got it, I’ve remembered that {list(facts_data.keys())[0]} is {list(facts_data.values())[0]}.", "aura")
            speak_async(f"Got it, I’ve remembered that {list(facts_data.keys())[0]} is {list(facts_data.values())[0]}.")
    except Exception as e:
        print("[Executor] _handle_facts error:", e)


def _handle_images(prompts, ui_callback=None):
    """Generate or show images."""
    try:
        import requests, os
        from PIL import Image
        if not os.path.exists("generated"):
            os.makedirs("generated")
        for prompt in prompts:
            print("[Image Gen]:", prompt)
            img_url = f"https://image.pollinations.ai/prompt/{requests.utils.quote(prompt)}"
            img_data = requests.get(img_url).content
            path = f"generated/{abs(hash(prompt)) % 99999}.jpg"
            with open(path, "wb") as f:
                f.write(img_data)
            print(f"[Executor] Image saved to {path}")
    except Exception as e:
        print("[Image error]:", e)


def _handle_todo(value, ui_callback=None):
    """
    value expected: dict like {"task":"Do X", "when":"..."}
    """
    try:
        task = value.get("task") if isinstance(value, dict) else str(value)
        if task:
            add_todo(task)
            if ui_callback:
                ui_callback(f"Added to-do: {task}", "aura")
            speak_async(f"Added to-do: {task}")
    except Exception as e:
        print("[Executor] _handle_todo error:", e)
        if ui_callback:
            ui_callback("Failed to add to-do item.", "aura")
        speak_async("Failed to add to-do item.")

def _handle_copy(text, ui_callback=None):
    """Show a copyable text box in the chat UI (and copy to clipboard optionally)."""
    try:
        if ui_callback:
            ui_callback(text, "aura")
        else:
            print("[AURA copy]:", text)
        # Put it on the clipboard so user can paste quickly
        try:
            pyperclip.copy(str(text))
        except Exception:
            pass
        # Let the user know in voice
        speak_async("Text copied to clipboard.")
    except Exception as e:
        print("[_handle_copy error]:", e)

def _handle_generate(prompt, ui_callback=None):
    """
    Generate detailed text (report, email, content) using call_gemini_raw wrapper.
    This is safer/compatible across genai SDK versions and when keys are in env.
    """
    print(f"[Executor] Generating content for: {prompt}")
    try:
        key_to_use = CHAT_KEY or PLANNER_KEY
        if not key_to_use:
            _handle_speak("Generator is not configured. Please set CHAT_KEY or PLANNER_KEY in your .env", ui_callback)
            return

        generated = call_gemini_raw(key_to_use, prompt, temperature=0.0, timeout=20)
        if not generated:
            _handle_speak("Sorry, generation failed or returned empty result.", ui_callback)
            return

        generated = str(generated).strip()
        if ui_callback:
            ui_callback(generated, "aura")
        _handle_speak("Here’s the result. You can copy it if you like.", ui_callback)

        # If window supports a copy bubble helper, call it (non-critical)
        if 'window' in globals() and hasattr(window, "_add_copy_bubble"):
            try:
                QtCore.QMetaObject.invokeMethod(
                    window, "_add_copy_bubble",
                    QtCore.Qt.QueuedConnection,
                    QtCore.Q_ARG(str, generated)
                )
            except Exception:
                pass

    except Exception as e:
        print(f"[Generator error]: {e}")
        _handle_speak("Sorry, I couldn’t generate that right now.", ui_callback)

def _handle_apps(value, ui_callback=None):
    """
    Open or close desktop applications or mapped URLs.
    Accepts:
      - a single string app name
      - a list of strings
      - a dict { "action":"open"/"close", "name": <name> }
    """
    try:
        if isinstance(value, dict):
            action = value.get("action", "open")
            names = [value.get("name")] if value.get("name") else value.get("apps", [])
        elif isinstance(value, (list, tuple)):
            action = "open"
            names = value
        else:
            action = "open"
            names = [value]

        opened = []
        for n in names:
            key = str(n).lower().strip()
            if key in APP_MAP:
                item = APP_MAP[key]
                if item["type"] == "url":
                    _handle_urls([item["value"]], ui_callback=ui_callback)
                    opened.append(item["value"])
                elif item["type"] == "exe":
                    try:
                        subprocess.Popen(item["value"], shell=True)
                        opened.append(item["value"])
                    except Exception as e:
                        print("[Executor] failed to open exe:", e)
                        if ui_callback: ui_callback(f"Failed to open {n}.", "aura")
                else:
                    if ui_callback: ui_callback(f"Cannot open {n} (unknown mapping).", "aura")
            else:
                # try to open as executable or URL
                if action == "open":
                    # attempt to open as URL if looks like web
                    if re.match(r"https?://", key):
                        _handle_urls([key], ui_callback=ui_callback)
                        opened.append(key)
                    else:
                        try:
                            if os.name == "nt":
                                subprocess.Popen(key, shell=True)
                            else:
                                subprocess.Popen([key])
                            opened.append(key)
                        except Exception:
                            # fallback: search web
                            search_url = f"https://www.google.com/search?q={key.replace(' ','+')}"
                            _handle_urls([search_url], ui_callback=ui_callback)
                            opened.append(search_url)

                elif action == "close":
                    # best-effort: terminate processes matching name
                    if psutil:
                        for proc in psutil.process_iter(['name']):
                            try:
                                if key in (proc.info['name'] or "").lower():
                                    proc.terminate()
                                    opened.append(key)
                            except Exception:
                                pass
        if opened:
            msg = "Opened/Closed: " + ", ".join(opened)
            if ui_callback: ui_callback(msg, "aura")
            speak_async(msg)
        else:
            if ui_callback: ui_callback("No apps were opened or closed.", "aura")
            speak_async("No apps were opened or closed.")
    except Exception as e:
        print("[Executor] _handle_apps error:", e)
        if ui_callback: ui_callback("I couldn't manage the apps.", "aura")
        speak_async("I couldn't manage the apps.")

def _handle_reminder(reminder_data, ui_callback=None):
    """Schedules a reminder, fixing timezone-aware/naive issues."""
    try:
        import dateutil.parser
        from datetime import datetime, timezone, timedelta
        import threading

        when_str = reminder_data.get("when")
        message = reminder_data.get("text", "You have a reminder!")

        if not when_str:
            print("[Reminder] Invalid reminder data:", reminder_data)
            return

        # Parse ISO timestamp safely
        when_dt = dateutil.parser.isoparse(when_str)
        now_dt = datetime.now(timezone.utc)

        # Compute delay in seconds safely (handles aware vs naive)
        delay = (when_dt - now_dt).total_seconds()
        if delay < 0:
            delay = 1  # immediate trigger if past

        def remind():
            msg = f"🔔 Reminder: {message}"
            print("[Reminder Triggered]:", msg)
            if ui_callback:
                ui_callback(msg, "aura")
            speak_async(msg)

        threading.Timer(delay, remind).start()
        print(f"[Executor] Reminder set for {when_dt} UTC ({delay:.1f}s from now).")

    except Exception as e:
        print("[Executor] _handle_reminder error:", e)

# -------------------- UI Helpers --------------------
def pretty_time(ts_iso):
    try:
        dt = datetime.fromisoformat(ts_iso)
        return dt.strftime("%b %d, %I:%M %p")
    except Exception:
        return ts_iso

class TinyNotification(QtWidgets.QWidget):
    """A small popup notification at bottom-right (auto-closes)."""
    def __init__(self, text, duration=3):
        super().__init__(flags=QtCore.Qt.FramelessWindowHint | QtCore.Qt.Tool)
        self.setAttribute(QtCore.Qt.WA_TranslucentBackground, True)
        self.duration = duration
        self.label = QtWidgets.QLabel(text, self)
        self.label.setStyleSheet("background: rgba(20,20,20,0.95); color: white; padding:10px; border-radius:8px;")
        self.label.adjustSize()
        self.setFixedSize(self.label.size())
        screen = QtWidgets.QApplication.primaryScreen().availableGeometry()
        x = screen.width() - self.width() - 20
        y = screen.height() - self.height() - 120
        self.setGeometry(x, y, self.width(), self.height())
    def show_and_auto_close(self):
        self.show()
        QtCore.QTimer.singleShot(self.duration*1000, self.close)

# -------------------- Chat UI Widgets --------------------
class ChatBubble(QtWidgets.QWidget):
    """A single chat bubble. source: 'user' or 'aura'. aura bubbles have copy button."""
    def __init__(self, text, source="aura"):
        super().__init__()
        self.source = source
        layout = QtWidgets.QHBoxLayout(self)
        layout.setContentsMargins(6,6,6,6)
        if source == "user":
            spacer = QtWidgets.QSpacerItem(40,10, QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Minimum)
            layout.addItem(spacer)
            bubble = QtWidgets.QFrame()
            bubble.setStyleSheet("background:#ffffff; color:#111; border-radius:12px; padding:10px;")
            v = QtWidgets.QVBoxLayout(bubble)
            label = QtWidgets.QLabel(text)
            label.setWordWrap(True)
            v.addWidget(label)
            layout.addWidget(bubble, 0)
        else:
            # aura bubble on right
            bubble = QtWidgets.QFrame()
            bubble.setStyleSheet("background: qlineargradient(spread:pad, x1:0,y1:0,x2:1,y2:1, stop:0 #ff8a65, stop:1 #b71c1c); color:white; border-radius:12px; padding:10px;")
            v = QtWidgets.QVBoxLayout(bubble)
            label = QtWidgets.QLabel(text)
            label.setWordWrap(True)
            v.addWidget(label)
            # copy & speak buttons row
            h = QtWidgets.QHBoxLayout()
            btn_copy = QtWidgets.QPushButton("Copy")
            btn_copy.setFixedSize(60,28)
            btn_copy.setStyleSheet("background:transparent; color:#fff; border:1px solid rgba(255,255,255,0.25); border-radius:6px;")
            btn_copy.clicked.connect(lambda: pyperclip.copy(text))
            h.addWidget(btn_copy)
            btn_speak = QtWidgets.QPushButton("🔊")
            btn_speak.setFixedSize(36,28)
            btn_speak.setStyleSheet("background:transparent; color:#fff; border:none;")
            btn_speak.clicked.connect(lambda: speak_async(text))
            h.addWidget(btn_speak)
            h.addStretch()
            v.addLayout(h)
            layout.addWidget(bubble, 0)
            spacer = QtWidgets.QSpacerItem(40,10, QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Minimum)
            layout.addItem(spacer)
        # small spacing
        self.setMaximumWidth(420)

# -------------------- Mic Thread --------------------
class MicListener(threading.Thread):
    def __init__(self, callback_text):
        super().__init__(daemon=True)
        self._running = False
        self.callback_text = callback_text
        self.recognizer = sr.Recognizer()
        self._stop_event = threading.Event()

    def start_listening(self):
        self._running = True
        if not self.is_alive():
            self.start()

    def stop_listening(self):
        self._running = False

    def run(self):
        try:
            with sr.Microphone() as source:
                self.recognizer.adjust_for_ambient_noise(source, duration=0.4)
                while True:
                    if not self._running:
                        time.sleep(0.15)
                        continue
                    try:
                        audio = self.recognizer.listen(source, timeout=6, phrase_time_limit=8)
                        text = self.recognizer.recognize_google(audio)
                        if text:
                            self.callback_text(text)
                    except sr.WaitTimeoutError:
                        continue
                    except sr.UnknownValueError:
                        continue
                    except Exception as e:
                        print("Mic error:", e)
                        time.sleep(0.3)
        except Exception as e:
            print("MicListener global error:", e)

# -------------------- Main Aura Widget --------------------
# Replace the entire AuraWidget class in your file with this block.

class AuraWidget(QtWidgets.QWidget):
    """
    Unified AuraWidget:
      - Transparent floating UI, pinned circular button
      - Floating chat bubbles (uses ChatBubble if present)
      - Mic transcribes to input (no auto-send)
      - ui callback slot _add_chat_bubble(arg1, arg2) accepts either:
            (message, source) OR (source, message) for compatibility
      - ui_add_bubble(text, source) wrapper for safe UI thread calls
      - Provides on_send -> planner_agent -> execute_plan streaming
    """
    def __init__(self):
        super().__init__()
        # Window flags: frameless, always-on-top, tool window
        self.setWindowFlags(
            QtCore.Qt.FramelessWindowHint |
            QtCore.Qt.WindowStaysOnTopHint |
            QtCore.Qt.Tool
        )
        self.setAttribute(QtCore.Qt.WA_TranslucentBackground, True)

        # Sizes
        self.expanded_size = QtCore.QSize(420, 520)
        self.minimized_size = QtCore.QSize(84, 84)
        self.is_minimized = False

        # state
        self.logo_pix = None
        self._drag_pos = None

        # Build UI
        self._setup_ui()

        # Mic listener (assumes MicListener exists in file)
        try:
            self.mic = MicListener(self._on_mic_text)
        except Exception:
            self.mic = None

        # Load reminders/memory if present (non-fatal)
        try:
            self._load_startup_memory()
        except Exception:
            pass

        # Start minimized shortly after launch
        QtCore.QTimer.singleShot(200, self._ensure_minimized_start)

    def _setup_ui(self):
        # Base geometry & transparent look (bubbles float on screen)
        self.setFixedSize(self.expanded_size)
        self.setWindowTitle("AURA AI")
        self.setStyleSheet("background: transparent;")

        # Main (slightly translucent) frame to host controls when expanded
        self.main_frame = QtWidgets.QFrame(self)
        self.main_frame.setObjectName("main_frame")
        self.main_frame.setGeometry(0, 0, self.expanded_size.width(), self.expanded_size.height())
        self.main_frame.setStyleSheet("""
            #main_frame {
                background-color: rgba(255,255,255,0.02); /* almost transparent */
                border-radius: 20px;
            }
        """)

        layout = QtWidgets.QVBoxLayout(self.main_frame)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        # Header row: small logo + title + spacer + minimize/close
        header = QtWidgets.QHBoxLayout()
        header.setSpacing(6)

        self.logo_label = QtWidgets.QLabel()
        # safe fallback icon
        if 'AURA_LOGO_PATH' in globals() and AURA_LOGO_PATH and os.path.exists(AURA_LOGO_PATH):
            self.logo_pix = QtGui.QPixmap(AURA_LOGO_PATH).scaled(36,36, QtCore.Qt.KeepAspectRatio, QtCore.Qt.SmoothTransformation)
        else:
            # draw simple circle as fallback
            pix = QtGui.QPixmap(36,36)
            pix.fill(QtCore.Qt.transparent)
            p = QtGui.QPainter(pix)
            p.setRenderHint(QtGui.QPainter.Antialiasing)
            p.setBrush(QtGui.QColor("#ff6f61"))
            p.setPen(QtCore.Qt.NoPen)
            p.drawEllipse(0,0,36,36)
            p.end()
            self.logo_pix = pix
        self.logo_label.setPixmap(self.logo_pix)
        header.addWidget(self.logo_label)

        title = QtWidgets.QLabel("AURA")
        title.setStyleSheet("color: #ffffff; font-size:16px; font-weight:600; padding-left:6px;")
        header.addWidget(title)
        header.addStretch()

        btn_min = QtWidgets.QPushButton("—")
        btn_min.setFixedSize(28,24)
        btn_min.setStyleSheet("background:transparent; color:white; border:none;")
        btn_min.clicked.connect(lambda: self.toggle_minimize())
        header.addWidget(btn_min)

        btn_close = QtWidgets.QPushButton("✕")
        btn_close.setFixedSize(28,24)
        btn_close.setStyleSheet("background:transparent; color:white; border:none;")
        btn_close.clicked.connect(self.close)
        header.addWidget(btn_close)

        layout.addLayout(header)

        # Chat area: scrollable container with layout that holds bubbles (floating look)
        self.scroll = QtWidgets.QScrollArea()
        self.scroll.setFrameShape(QtWidgets.QFrame.NoFrame)
        self.scroll.setWidgetResizable(True)
        self.scroll.setStyleSheet("background: transparent; border:none;")

        self.chat_container = QtWidgets.QWidget()
        self.chat_layout = QtWidgets.QVBoxLayout(self.chat_container)
        self.chat_layout.setContentsMargins(6, 6, 6, 6)
        self.chat_layout.setSpacing(12)
        self.chat_layout.addStretch()
        self.scroll.setWidget(self.chat_container)

        layout.addWidget(self.scroll, 1)

        # Input row (transparent pill)
        input_row = QtWidgets.QHBoxLayout()
        input_row.setSpacing(8)

        self.input_edit = QtWidgets.QLineEdit()
        self.input_edit.setPlaceholderText("Ask Aura anything...")
        self.input_edit.setMinimumHeight(44)
        self.input_edit.setStyleSheet("""
            QLineEdit {
                background-color: rgba(255,255,255,0.06);
                border-radius:22px;
                padding: 10px 14px;
                color: #fff;
                font-size:14px;
            }
        """)
        self.input_edit.returnPressed.connect(self.on_send)
        input_row.addWidget(self.input_edit, 1)

        # mic button (toggle)
        self.btn_mic = QtWidgets.QPushButton("🎤")
        self.btn_mic.setFixedSize(44,44)
        self.btn_mic.setStyleSheet("border-radius:22px; background: rgba(255,255,255,0.03); color: #fff;")
        self.btn_mic.clicked.connect(self._mic_toggle)
        input_row.addWidget(self.btn_mic)

        # send
        self.btn_send = QtWidgets.QPushButton("➤")
        self.btn_send.setFixedSize(44,44)
        self.btn_send.setStyleSheet("border-radius:12px; background: qlineargradient(spread:pad,x1:0,y1:0,x2:1,y2:1,stop:0 #ff8a65, stop:1 #b71c1c); color:white;")
        self.btn_send.clicked.connect(self.on_send)
        input_row.addWidget(self.btn_send)

        layout.addLayout(input_row)

        # small footer
        footer = QtWidgets.QLabel("✨ AURA — one assistant for all")
        footer.setStyleSheet("color: rgba(255,215,200,0.7); font-size:10px; padding-top:6px;")
        layout.addWidget(footer)

        # ---------------- minimized circular button (created here but hidden) ----------------
        self.min_button = QtWidgets.QPushButton(self)
        self.min_button.setVisible(False)
        self.min_button.setFixedSize(self.minimized_size)
        self.min_button.setStyleSheet("border-radius:42px; border:none;")
        self.min_button.clicked.connect(lambda: self.toggle_minimize(minimize=False))
        # set icon safely (will be updated in _place_bottom_right)
        self.min_button.setIcon(QtGui.QIcon(self.logo_pix))
        self.min_button.setIconSize(self.minimized_size)

        # mouse drag mapping (frame-level)
        self.main_frame.mousePressEvent = self._on_mouse_press
        self.main_frame.mouseMoveEvent = self._on_mouse_move

        # initial placement
        self._place_bottom_right()

    # ================== flexible slot (handles both arg orders) ==================
    @QtCore.Slot(str, str)
    def _add_chat_bubble(self, a, b):
        """
        Accepts either:
          - _add_chat_bubble(message, source)
          - _add_chat_bubble(source, message)
        Source values: 'user', 'aura', 'aura_copy'
        """
        try:
            # detect which is source: if a equals known source tokens -> treat as (source, message)
            src_tokens = {"user", "aura", "aura_copy", "aura-stream", "aura_copy"}
            if a in src_tokens:
                source = a
                message = b
            else:
                # otherwise assume (message, source) — fallback to 'aura' when unknown
                message = a
                source = b if b in src_tokens else "aura"

            # persist chat history (non-fatal)
            try:
                who = "user" if source == "user" else "aura"
                CHAT_HISTORY.append((who, message, datetime.now().isoformat()))
                if len(CHAT_HISTORY) > MAX_CHAT_HISTORY:
                    CHAT_HISTORY.pop(0)
            except Exception:
                pass

            # If ChatBubble exists in your file, use it. Otherwise build a simple one.
            try:
                bubble = ChatBubble(message, source=("user" if source == "user" else "aura"))
                self.chat_layout.insertWidget(self.chat_layout.count() - 1, bubble)
            except Exception:
                # fallback manual bubble
                frame = QtWidgets.QFrame()
                v = QtWidgets.QVBoxLayout(frame)
                v.setContentsMargins(10,8,10,8)
                lbl = QtWidgets.QLabel(message)
                lbl.setWordWrap(True)
                lbl.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
                lbl.setStyleSheet("font-size:13px;")
                v.addWidget(lbl)

                # Only add copy/speak controls for flagged aura messages
                if source in ("aura_copy", "aura"):
                    add_copy = (source == "aura_copy")
                    if add_copy:
                        h = QtWidgets.QHBoxLayout()
                        btn_copy = QtWidgets.QPushButton("Copy")
                        btn_copy.setFixedSize(64,28)
                        btn_copy.setStyleSheet("background:transparent; color:#fff; border:1px solid rgba(255,255,255,0.12); border-radius:6px;")
                        btn_copy.clicked.connect(lambda _, t=message: self._copy_text(t))
                        h.addWidget(btn_copy)
                        btn_speak = QtWidgets.QPushButton("🔊")
                        btn_speak.setFixedSize(36,28)
                        btn_speak.setStyleSheet("background:transparent; color:#fff; border:none;")
                        btn_speak.clicked.connect(lambda _, t=message: speak_async(t))
                        h.addWidget(btn_speak)
                        h.addStretch()
                        v.addLayout(h)

                if source == "user":
                    frame.setStyleSheet("background: rgba(255,255,255,0.92); color:#111; border-radius:18px;")
                    self.chat_layout.insertWidget(self.chat_layout.count() - 1, frame)
                else:
                    frame.setStyleSheet("background: qlineargradient(spread:pad,x1:0,y1:0,x2:1,y2:1,stop:0 #ff8a65, stop:1 #b71c1c); color:white; border-radius:18px;")
                    self.chat_layout.insertWidget(self.chat_layout.count() - 1, frame)

            # autoscroll
            QtCore.QTimer.singleShot(60, lambda: self.scroll.verticalScrollBar().setValue(self.scroll.verticalScrollBar().maximum()))

        except Exception as e:
            print("[_add_chat_bubble error]:", e)

    # wrapper to safely call from other threads
    def ui_add_bubble(self, text: str, source: str = "aura"):
        QtCore.QTimer.singleShot(0, lambda: self._add_chat_bubble(text, source))

    def _copy_text(self, text: str):
        try:
            import pyperclip
            pyperclip.copy(text)
        except Exception:
            try:
                QtWidgets.QApplication.clipboard().setText(text)
            except Exception:
                pass

    # =================== input / planner chain ===================
    def on_send(self):
        text = self.input_edit.text().strip()
        if not text:
            return
        # show user bubble
        self._add_chat_bubble(text, source="user")
        self.input_edit.clear()
        # run planner in background
        threading.Thread(target=self._plan_and_execute_thread, args=(text,), daemon=True).start()

    def _plan_and_execute_thread(self, text):
        try:
            plan = planner_agent(text)

            if not plan or not isinstance(plan, dict):
                QtCore.QTimer.singleShot(0, lambda: self._add_chat_bubble("Sorry, I couldn't create a plan for that. Try rephrasing.", "aura"))
                try:
                    speak_async("Sorry, I couldn't create a plan for that. Try rephrasing.")
                except Exception:
                    pass
                return

            # Build small summary for UI
            summary = []
            if "speak" in plan and plan.get("speak"):
                summary.append(plan["speak"])
            if "urls" in plan:
                urls = plan["urls"]
                if isinstance(urls, list):
                    summary.append("Opening: " + ", ".join(urls[:3]))
                else:
                    summary.append("Opening: " + str(urls))
            if "apps" in plan:
                summary.append("Launching apps")
            if "reminder" in plan:
                summary.append("Reminder set")
            if "act" in plan:
                summary.append("Action queued")

            if summary:
                short = " • ".join(summary)
                QtCore.QTimer.singleShot(0, lambda: self._add_chat_bubble(short, "aura"))
                try:
                    speak_async(short)
                except Exception:
                    pass

            # Execute plan and permit streaming via ui_callback
            execute_plan(plan, ui_callback=self._ui_callback_from_execute)

        except Exception as e:
            print("plan/execute error:", e)
            QtCore.QTimer.singleShot(0, lambda: self._add_chat_bubble("Sorry, I couldn't process that.", "aura"))
            try:
                speak_async("Sorry, I couldn't process that.")
            except Exception:
                pass

    def _ui_callback_from_execute(self, text, source="aura"):
        """Used by execute_plan to stream partial messages to UI."""
        # many callers expect (text, source) but some QMetaObject invocations pass (source, text).
        # ui_add_bubble will handle flexible ordering.
        QtCore.QTimer.singleShot(0, lambda: self._add_chat_bubble(text, source))
        # speak aura messages optionally
        if source and (source.startswith("aura") or source == "aura"):
            try:
                speak_async(text)
            except Exception:
                pass

    # =================== minimize / drag / placement helpers ===================
    def _on_mouse_press(self, event):
        if event.button() == QtCore.Qt.LeftButton:
            # support both Qt5 and Qt6 APIs
            try:
                gp = event.globalPos()
            except Exception:
                gp = event.globalPosition().toPoint()
            self._drag_pos = gp - self.frameGeometry().topLeft()
            event.accept()

    def _on_mouse_move(self, event):
        try:
            gp = event.globalPos()
        except Exception:
            gp = event.globalPosition().toPoint()
        if event.buttons() & QtCore.Qt.LeftButton and self._drag_pos is not None:
            self.move(gp - self._drag_pos)
            event.accept()
            # keep minimized button in sync
            try:
                self._update_min_button_pos()
            except Exception:
                pass

    def _place_bottom_right(self):
        screen = QtWidgets.QApplication.primaryScreen().availableGeometry()
        x = screen.width() - self.width() - 20
        y = screen.height() - self.height() - 40
        self.move(x, y)
        # make sure min_button icon is set
        try:
            if self.logo_pix:
                self.min_button.setIcon(QtGui.QIcon(self.logo_pix))
                self.min_button.setIconSize(self.minimized_size)
        except Exception:
            pass
        self._update_min_button_pos()

    def _update_min_button_pos(self):
        screen = QtWidgets.QApplication.primaryScreen().availableGeometry()
        w = self.minimized_size.width()
        x = screen.width() - w - 20
        y = screen.height() - w - 40
        try:
            self.min_button.move(x, y)
        except Exception:
            pass

    def _ensure_minimized_start(self):
        self.toggle_minimize(minimize=True)

    def toggle_minimize(self, minimize=None):
        """Minimize to circular button or expand."""
        if minimize is None:
            minimize = not self.is_minimized
        if minimize and not self.is_minimized:
            self.main_frame.hide()
            self.setFixedSize(self.minimized_size)
            self.min_button.setVisible(True)
            self.is_minimized = True
        elif not minimize and self.is_minimized:
            self.min_button.setVisible(False)
            self.setFixedSize(self.expanded_size)
            self.main_frame.show()
            self._place_bottom_right()
            self.is_minimized = False

    # =================== mic helpers ===================
    def _mic_toggle(self):
        if not self.mic:
            return
        if self.btn_mic.text() == "🎤":
            # start listening (mic will callback to _on_mic_text)
            self.btn_mic.setText("■")
            self.btn_mic.setStyleSheet("border-radius:22px; background:#ff4d4d; color:white;")
            # show small transient message in input only (do not create user bubble automatically)
            self.mic.start_listening()
        else:
            self.btn_mic.setText("🎤")
            self.btn_mic.setStyleSheet("border-radius:22px; background: rgba(255,255,255,0.03); color:white;")
            self.mic.stop_listening()

    def _on_mic_text(self, text):
        # mic -> main thread: populate input but don't send automatically
        QtCore.QTimer.singleShot(0, lambda: self.input_edit.setText(text))
        # also optionally show a dim system bubble like "Dictation ready" (comment/uncomment below)
        # QtCore.QTimer.singleShot(0, lambda: self._add_chat_bubble("Dictation ready — press Enter to send.", "aura"))

    # =================== startup memory loader stub (existing in file) ===================
    def _load_startup_memory(self):
        try:
            for rem in memory.get("reminders", []):
                try:
                    schedule_reminder(rem)
                except Exception:
                    pass
        except Exception:
            pass

# -------------------------
# AuraFloatingChatWidget
# -------------------------
from PySide6 import QtWidgets, QtCore, QtGui

class ChatBubble(QtWidgets.QFrame):
    def __init__(self, text: str, sender: str = "aura", parent=None):
        super().__init__(parent)
        self.setObjectName("chat_bubble")
        self.sender = sender
        layout = QtWidgets.QHBoxLayout(self)
        layout.setContentsMargins(8,6,8,6)
        label = QtWidgets.QLabel(text)
        label.setWordWrap(True)
        label.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
        if sender == "user":
            layout.addStretch()
            label.setStyleSheet("background:#ffffff; color:#0b0b0b; padding:8px; border-radius:10px;")
            layout.addWidget(label)
        else:
            # aura bubble
            label.setStyleSheet("background: qlineargradient(x1:0,y1:0,x2:1,y2:1, stop:0 #ff8a65, stop:1 #ff416c); color:white; padding:8px; border-radius:10px;")
            layout.addWidget(label)
            layout.addStretch()
        self.setStyleSheet("QFrame#chat_bubble{background:transparent;}")

class AuraFloatingChatWidget(QtWidgets.QWidget):
    """
    Draggable, floating circular minimized button that expands into a rounded chat window.
    Use add_bubble(text, sender) to inject messages (sender: 'user' | 'aura').
    Provide optional callbacks for actual execution: on_user_send, on_mic_toggle, speak_callback.
    """

    def __init__(self, on_user_send=None, on_mic_toggle=None, speak_callback=None, logo_path=None, parent=None):
        super().__init__(parent)
        self.on_user_send = on_user_send
        self.on_mic_toggle = on_mic_toggle
        self.speak_callback = speak_callback
        self.logo_path = logo_path

        # sizes
        self.expanded_size = QtCore.QSize(400, 520)
        self.minimized_diameter = 84
        self.is_minimized = True
        self._drag_active = False
        self._drag_offset = QtCore.QPoint(0,0)

        # Window flags - always on top, frameless, tool window so it stays above
        self.setWindowFlags(QtCore.Qt.FramelessWindowHint | QtCore.Qt.WindowStaysOnTopHint | QtCore.Qt.Tool)
        self.setAttribute(QtCore.Qt.WA_TranslucentBackground, True)

        # Build UI
        self._build_ui()

        # Start minimized & place bottom-right
        self._apply_minimized_geometry()
        self.show()

    def _build_ui(self):
        # Root layout (empty, we control geometry)
        # Minimized circular button
        self.circle_btn = QtWidgets.QPushButton(self)
        self.circle_btn.setFixedSize(self.minimized_diameter, self.minimized_diameter)
        self.circle_btn.setCursor(QtCore.Qt.PointingHandCursor)
        self.circle_btn.setStyleSheet("""
            QPushButton {
                border-radius: 42px;
                border: none;
                background: qlineargradient(x1:0,y1:0,x2:1,y2:1, stop:0 #ff6f61, stop:1 #b71c1c);
                color: white;
                font-weight: bold;
            }
            QPushButton:hover { transform: translateY(-2px); }
        """)
        # logo/icon
        if self.logo_path:
            pix = QtGui.QPixmap(self.logo_path).scaled(56,56, QtCore.Qt.KeepAspectRatio, QtCore.Qt.SmoothTransformation)
            self.circle_btn.setIcon(QtGui.QIcon(pix))
            self.circle_btn.setIconSize(QtCore.QSize(56,56))
        else:
            self.circle_btn.setText("A")

        self.circle_btn.clicked.connect(self._toggle_expand)

        # Expanded chat frame (hidden initially)
        self.chat_frame = QtWidgets.QFrame(self)
        self.chat_frame.setObjectName("chat_frame")
        self.chat_frame.setStyleSheet("""
            QFrame#chat_frame {
                background: rgba(20,20,20,240);
                border-radius: 16px;
                border: 1px solid rgba(255,80,80,0.08);
            }
        """)
        self.chat_frame.setVisible(False)
        # Layout inside chat_frame
        v = QtWidgets.QVBoxLayout(self.chat_frame)
        v.setContentsMargins(12,12,12,12)
        v.setSpacing(8)

        # Header (title + close/minimize)
        header = QtWidgets.QHBoxLayout()
        header.setSpacing(8)
        lbl_logo = QtWidgets.QLabel()
        if self.logo_path:
            pix = QtGui.QPixmap(self.logo_path).scaled(34,34, QtCore.Qt.KeepAspectRatio, QtCore.Qt.SmoothTransformation)
            lbl_logo.setPixmap(pix)
        else:
            lbl_logo.setFixedSize(34,34)
            lbl_logo.setStyleSheet("background:#ff6f61; border-radius:17px;")
        header.addWidget(lbl_logo)
        title = QtWidgets.QLabel("AURA")
        title.setStyleSheet("color:white; font-weight:700; font-size:16px;")
        header.addWidget(title)
        header.addStretch()
        btn_min = QtWidgets.QPushButton("—")
        btn_min.setFixedSize(26,26)
        btn_min.setStyleSheet("background:transparent; color:white; border:none;")
        btn_min.clicked.connect(lambda: self._toggle_expand(minimize=True))
        header.addWidget(btn_min)
        btn_close = QtWidgets.QPushButton("✕")
        btn_close.setFixedSize(26,26)
        btn_close.setStyleSheet("background:transparent; color:white; border:none;")
        btn_close.clicked.connect(self.close)
        header.addWidget(btn_close)
        v.addLayout(header)

        # Scroll area for chat
        self.scroll = QtWidgets.QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QtWidgets.QFrame.NoFrame)
        self.scroll.setStyleSheet("""
            QScrollArea { background: transparent; }
            QScrollBar:vertical { background: transparent; width:8px; }
            QScrollBar::handle:vertical { background: rgba(255,80,80,0.8); border-radius:4px; }
        """)
        self.chat_container = QtWidgets.QWidget()
        self.chat_layout = QtWidgets.QVBoxLayout(self.chat_container)
        self.chat_layout.setContentsMargins(6,6,6,6)
        self.chat_layout.setSpacing(8)
        self.chat_layout.addStretch()
        self.scroll.setWidget(self.chat_container)
        v.addWidget(self.scroll, 1)

        # Input row
        input_row = QtWidgets.QHBoxLayout()
        self.input_edit = QtWidgets.QLineEdit()
        self.input_edit.setPlaceholderText("Type a message or press mic...")
        self.input_edit.returnPressed.connect(self._on_send_clicked)
        self.input_edit.setStyleSheet("background:#2b2b2b; color:white; padding:10px; border-radius:12px;")
        input_row.addWidget(self.input_edit, 1)
        self.btn_mic = QtWidgets.QPushButton("🎤")
        self.btn_mic.setFixedSize(44,44)
        self.btn_mic.clicked.connect(self._mic_clicked)
        self.btn_mic.setStyleSheet("background: qlineargradient(x1:0,y1:0,x2:1,y2:1, stop:0 #ff8a65, stop:1 #ff416c); border-radius:22px; color:white;")
        input_row.addWidget(self.btn_mic)
        self.btn_send = QtWidgets.QPushButton("➤")
        self.btn_send.setFixedSize(44,44)
        self.btn_send.clicked.connect(self._on_send_clicked)
        self.btn_send.setStyleSheet("background:white; color:#ff4b2b; border-radius:10px;")
        input_row.addWidget(self.btn_send)
        v.addLayout(input_row)

        # keep track of last bubble for auto-scroll
        self._last_bubble = None

        # Make both widgets accept mouse events for drag
        self.circle_btn.setMouseTracking(True)
        self.chat_frame.setMouseTracking(True)
        # allow focus
        self.setFocusPolicy(QtCore.Qt.StrongFocus)

    # -------------------------
    # geometry & show/minimize
    # -------------------------
    def _apply_minimized_geometry(self):
        screen = QtWidgets.QApplication.primaryScreen().availableGeometry()
        w = self.minimized_diameter
        x = screen.width() - w - 20
        y = screen.height() - w - 40
        self.setGeometry(x, y, w, w)
        self.circle_btn.move(0, 0)
        self.circle_btn.setVisible(True)
        self.chat_frame.setVisible(False)
        self.is_minimized = True

    def _apply_expanded_geometry(self):
        screen = QtWidgets.QApplication.primaryScreen().availableGeometry()
        w = self.expanded_size.width()
        h = self.expanded_size.height()
        # expand upwards-left from current circle center
        # keep a margin if near screen edges
        x = max(20, screen.width() - w - 20)
        y = max(20, screen.height() - h - 60)
        self.setGeometry(x, y, w, h)
        self.chat_frame.setGeometry(0, 0, w, h)
        self.circle_btn.setVisible(False)
        self.chat_frame.setVisible(True)
        self.is_minimized = False

    def _toggle_expand(self, minimize=None):
        if minimize is None:
            minimize = not self.is_minimized
        if minimize:
            self._apply_minimized_geometry()
        else:
            self._apply_expanded_geometry()

    # -------------------------
    # Dragging (works in both states)
    # -------------------------
    def mousePressEvent(self, event):
        if event.button() == QtCore.Qt.LeftButton:
            self._drag_active = True
            self._drag_offset = event.globalPos() - self.frameGeometry().topLeft()
            event.accept()

    def mouseMoveEvent(self, event):
        if self._drag_active:
            new_pos = event.globalPos() - self._drag_offset
            screen = QtWidgets.QApplication.primaryScreen().availableGeometry()
            # clamp to screen bounds
            x = max(0, min(new_pos.x(), screen.width() - self.width()))
            y = max(0, min(new_pos.y(), screen.height() - self.height()))
            self.move(x, y)
            event.accept()

    def mouseReleaseEvent(self, event):
        self._drag_active = False

    # -------------------------
    # Bubbles / UI updates
    # -------------------------
    def add_bubble(self, text: str, sender: str = "aura"):
        """
        Thread-safe adding of a bubble. Call from any thread.
        sender: 'user' | 'aura'
        """
        def _add():
            try:
                bubble = ChatBubble(text, sender=sender)
                # insert above stretch
                self.chat_layout.insertWidget(self.chat_layout.count()-1, bubble)
                QtCore.QTimer.singleShot(50, lambda: self.scroll.verticalScrollBar().setValue(self.scroll.verticalScrollBar().maximum()))
            except Exception as e:
                print("add_bubble error:", e)
        QtCore.QTimer.singleShot(0, _add)
        # speak aura messages if callback provided
        if sender == "aura" and self.speak_callback:
            try:
                self.speak_callback(text)
            except Exception:
                pass

    # -------------------------
    # Input / mic
    # -------------------------
    def _on_send_clicked(self):
        text = self.input_edit.text().strip()
        if not text:
            return
        # show user bubble
        self.add_bubble(text, sender="user")
        self.input_edit.clear()
        # call user callback (planner/executor will handle)
        if callable(self.on_user_send):
            try:
                # call in a worker thread to avoid blocking UI
                QtCore.QThreadPool.globalInstance().start(
                    Worker(lambda: self.on_user_send(text))
                )
            except Exception:
                # fallback: call directly
                try:
                    self.on_user_send(text)
                except Exception:
                    pass

    def _mic_clicked(self):
        # toggle start/stop
        if callable(self.on_mic_toggle):
            try:
                self.on_mic_toggle(True)  # let external code control start/stop state
            except Exception:
                pass

# -------------------------
# Tiny Worker helper for non-blocking calls
# -------------------------
class Worker(QtCore.QRunnable):
    def __init__(self, fn):
        super().__init__()
        self.fn = fn
    def run(self):
        try:
            self.fn()
        except Exception as e:
            print("Worker error:", e)


class ToDoPanel(QtWidgets.QWidget):
    """Simple To-Do Panel: view/add/complete items."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self._build_ui()
        self.refresh()

    def _build_ui(self):
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        header = QtWidgets.QHBoxLayout()
        lbl = QtWidgets.QLabel("To-Do")
        lbl.setStyleSheet("font-weight:700; font-size:16px;")
        header.addWidget(lbl)
        header.addStretch()
        self.btn_add = QtWidgets.QPushButton("Add")
        self.btn_add.clicked.connect(self.add_item_dialog)
        header.addWidget(self.btn_add)
        layout.addLayout(header)

        self.listw = QtWidgets.QListWidget()
        self.listw.setSelectionMode(QtWidgets.QAbstractItemView.SingleSelection)
        layout.addWidget(self.listw, 1)

        btn_row = QtWidgets.QHBoxLayout()
        self.btn_done = QtWidgets.QPushButton("Mark Done")
        self.btn_done.clicked.connect(self.mark_done)
        btn_row.addWidget(self.btn_done)
        self.btn_delete = QtWidgets.QPushButton("Delete")
        self.btn_delete.clicked.connect(self.delete_item)
        btn_row.addWidget(self.btn_delete)
        layout.addLayout(btn_row)

    def refresh(self):
        self.listw.clear()
        for idx, item in enumerate(APP_MEMORY.get("todos", [])):
            text = item.get("task") if isinstance(item, dict) else str(item)
            li = QtWidgets.QListWidgetItem(text)
            li.setData(QtCore.Qt.UserRole, idx)
            self.listw.addItem(li)

    def add_item_dialog(self):
        txt, ok = QtWidgets.QInputDialog.getText(self, "Add To-Do", "Task:")
        if ok and txt.strip():
            APP_MEMORY.setdefault("todos", []).append({"task": txt.strip(), "created": datetime.now().isoformat()})
            save_memory()
            self.refresh()

    def mark_done(self):
        sel = self.listw.currentItem()
        if not sel:
            return
        idx = sel.data(QtCore.Qt.UserRole)
        try:
            item = APP_MEMORY["todos"].pop(idx)
            # optionally save to history, for now just remove
            save_memory()
            self.refresh()
        except Exception:
            pass

    def delete_item(self):
        sel = self.listw.currentItem()
        if not sel:
            return
        idx = sel.data(QtCore.Qt.UserRole)
        try:
            APP_MEMORY["todos"].pop(idx)
            save_memory()
            self.refresh()
        except Exception:
            pass

class ProfilePanel(QtWidgets.QWidget):
    """User profile storage (name, age, interests)."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self._build_ui()
        self.load_profile()

    def _build_ui(self):
        layout = QtWidgets.QFormLayout(self)
        self.name_edit = QtWidgets.QLineEdit()
        self.age_spin = QtWidgets.QSpinBox()
        self.age_spin.setRange(0, 120)
        self.interests_edit = QtWidgets.QLineEdit()
        layout.addRow("Name:", self.name_edit)
        layout.addRow("Age:", self.age_spin)
        layout.addRow("Interests (comma):", self.interests_edit)
        btn_row = QtWidgets.QHBoxLayout()
        self.btn_save = QtWidgets.QPushButton("Save")
        self.btn_save.clicked.connect(self.save_profile)
        btn_row.addWidget(self.btn_save)
        btn_row.addStretch()
        layout.addRow(btn_row)

    def load_profile(self):
        facts = APP_MEMORY.get("facts", {})
        self.name_edit.setText(facts.get("user_name", ""))
        try:
            self.age_spin.setValue(int(facts.get("age", 0) or 0))
        except Exception:
            self.age_spin.setValue(0)
        self.interests_edit.setText(",".join(facts.get("interests", [])) if facts.get("interests") else "")

    def save_profile(self):
        facts = APP_MEMORY.setdefault("facts", {})
        facts["user_name"] = self.name_edit.text().strip()
        facts["age"] = int(self.age_spin.value())
        interests = [i.strip() for i in self.interests_edit.text().split(",") if i.strip()]
        facts["interests"] = interests
        save_memory()
        QtWidgets.QMessageBox.information(self, "Profile", "Saved.")

class SettingsPanel(QtWidgets.QWidget):
    """App settings: tone, theme, basic toggles."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self._build_ui()
        self.load_settings()

    def _build_ui(self):
        layout = QtWidgets.QFormLayout(self)
        self.tone_combo = QtWidgets.QComboBox()
        self.tone_combo.addItems(["friendly", "formal", "playful", "concise"])
        layout.addRow("Tone:", self.tone_combo)

        self.theme_combo = QtWidgets.QComboBox()
        self.theme_combo.addItems(["dark", "light", "transparent"])
        layout.addRow("Theme:", self.theme_combo)

        self.parental_ctrl = QtWidgets.QCheckBox("Enable parental control (block adult content)")
        layout.addRow(self.parental_ctrl)

        btn_row = QtWidgets.QHBoxLayout()
        self.btn_save = QtWidgets.QPushButton("Save Settings")
        self.btn_save.clicked.connect(self.save_settings)
        btn_row.addWidget(self.btn_save)
        btn_row.addStretch()
        layout.addRow(btn_row)

    def load_settings(self):
        s = APP_MEMORY.setdefault("settings", {})
        self.tone_combo.setCurrentText(s.get("tone", "friendly"))
        self.theme_combo.setCurrentText(s.get("theme", "dark"))
        self.parental_ctrl.setChecked(s.get("parental_control", False))

    def save_settings(self):
        s = APP_MEMORY.setdefault("settings", {})
        s["tone"] = self.tone_combo.currentText()
        s["theme"] = self.theme_combo.currentText()
        s["parental_control"] = bool(self.parental_ctrl.isChecked())
        save_memory()
        QtWidgets.QMessageBox.information(self, "Settings", "Saved.")

class MainWindow(QtWidgets.QMainWindow):
    """
    Hosts AuraWidget (chat) + ToDo/Settings/Profile panels.
    Exposes a simple UI callback used by execute_plan to show bubbles.
    """
    def __init__(self, aura_widget: QtWidgets.QWidget):
        super().__init__()
        self.setWindowTitle("AURA — Control Center")
        self.resize(980, 680)
        self.aura_widget = aura_widget
        self._build_ui()
        self._connect_hooks()
        # place at bottom-right like before
        self._place_bottom_right()

    def _build_ui(self):
        # central widget
        central = QtWidgets.QWidget()
        self.setCentralWidget(central)
        h = QtWidgets.QHBoxLayout(central)
        h.setContentsMargins(8,8,8,8)
        h.setSpacing(10)

        # Left sidebar (icons)
        sidebar = QtWidgets.QVBoxLayout()
        sidebar.setSpacing(10)
        btn_chat = QtWidgets.QPushButton("Chat")
        btn_todo = QtWidgets.QPushButton("To-Do")
        btn_profile = QtWidgets.QPushButton("Profile")
        btn_settings = QtWidgets.QPushButton("Settings")
        for b in (btn_chat, btn_todo, btn_profile, btn_settings):
            b.setFixedHeight(44)
            sidebar.addWidget(b)
        sidebar.addStretch()
        h.addLayout(sidebar)

        # Stacked pages
        self.stack = QtWidgets.QStackedWidget()
        # ensure aura_widget is a QWidget
        self.stack.addWidget(self.aura_widget)         # index 0
        self.todo_panel = ToDoPanel()
        self.stack.addWidget(self.todo_panel)          # index 1
        self.profile_panel = ProfilePanel()
        self.stack.addWidget(self.profile_panel)       # index 2
        self.settings_panel = SettingsPanel()
        self.stack.addWidget(self.settings_panel)      # index 3
        h.addWidget(self.stack, 1)

        # connect buttons
        btn_chat.clicked.connect(lambda: self.stack.setCurrentIndex(0))
        btn_todo.clicked.connect(lambda: self.stack.setCurrentIndex(1))
        btn_profile.clicked.connect(lambda: self.stack.setCurrentIndex(2))
        btn_settings.clicked.connect(lambda: self.stack.setCurrentIndex(3))

        # small corner minimize button
        self.btn_min = QtWidgets.QPushButton("—")
        self.btn_min.setFixedSize(32,32)
        self.btn_min.clicked.connect(self._minimize_to_tray)
        sidebar.addWidget(self.btn_min)

    def _connect_hooks(self):
        # expose a ui callback that other modules can call: ui_callback(text, source)
        # The aura_widget already has _add_chat_bubble(text, source) — use that.
        pass

    def ui_add_bubble(self, text, source="aura"):
        """Thread-safe wrapper to add a chat bubble into AuraWidget."""
        try:
            QtCore.QTimer.singleShot(0, lambda: self.aura_widget._add_chat_bubble(text, source))
        except Exception:
            # fallback attempt
            try:
                self.aura_widget._add_chat_bubble(text, source)
            except Exception:
                print("[MainWindow] Failed to add bubble.")

    def open_todo(self):
        self.stack.setCurrentWidget(self.todo_panel)

    def _minimize_to_tray(self):
        # simple minimize behaviour
        self.showMinimized()

    def _place_bottom_right(self):
        screen = QtWidgets.QApplication.primaryScreen().availableGeometry()
        w, h = self.width(), self.height()
        x = screen.width() - w - 20
        y = screen.height() - h - 80
        self.move(x, y)

# -------------------- Run App --------------------
def main():
    global MAIN_WINDOW
    app = QtWidgets.QApplication(sys.argv)

    # Create AuraWidget (your chat) exactly as before
    aura = AuraWidget()

    # Create MainWindow that embeds the chat
    MAIN_WINDOW = MainWindow(aura)
    MAIN_WINDOW.show()

    # Pass the ui_callback to executor somewhere if needed:
    # your execute_plan(...) calls should be able to accept ui_callback=ui_callback
    # e.g., execute_plan(plan, ui_callback=ui_callback)

    print("[AURA] UI started successfully.")
    try:
        sys.exit(app.exec_())
    except Exception:
        sys.exit(app.exec())

    # instantiate with callbacks
    def handle_user_send(text):
    # call your planner/executor here
        plan = planner_agent(text)
        execute_plan(plan)   # make sure execute_plan is thread-safe

   # Better approach: pass the object to the function
    def mic_handler(mic_listener, start):
        if start:
            mic_listener.start_listening()
        else:
            mic_listener.stop_listening()

# How to call it:
# mic_handler(my_mic_object, True)

    def speak_fn(text):
        # call your speak or speak_async wrapper
        speak_async(text)

    chat_widget = AuraFloatingChatWidget(on_user_send=handle_user_send, on_mic_toggle=mic_handler, speak_callback=speak_fn, logo_path=AURA_LOGO_PATH)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        speak_async("Shutting down AURA. Goodbye.")
        # Insert this near the top of your aura_project.py file
# or in a place where other functions are defined.

from typing import Tuple

def do_open_app(app_name: str) -> Tuple[bool, str]:
    """Placeholder function to open an application."""
    # Add your logic here (e.g., using subprocess, os, or specific OS libraries)
    print(f"Attempting to open {app_name}...")
    # For now, we'll return a success message
    return True, f"Successfully attempted to open {app_name}."

def do_close_app_by_name(app_name: str) -> Tuple[bool, str]:
    """Placeholder function to close an application by name."""
    # Add your logic here (e.g., using psutil or os-specific process killers)
    print(f"Attempting to close {app_name}...")
    # For now, we'll return a success message
    return True, f"Successfully attempted to close {app_name}."

# The rest of your code block follows below this:
# # if "apps" in plan and plan.get("apps"):a
# # ...y
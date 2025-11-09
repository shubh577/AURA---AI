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

import os, json, re, threading, time
from datetime import datetime
import webbrowser, subprocess, pyperclip
from dateutil import parser as dtparser
from apscheduler.schedulers.background import BackgroundScheduler
import shlex
import threading

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

from PySide6 import QtCore, QtGui, QtWidgets

import queue as _queue

# Optional helpers
try:
    import psutil
except Exception:
    psutil = None

AUTO_EXECUTE = True   # For demo; set False to require confirmations in future


AURA_LOGO_PATH = os.getenv("AURA_LOGO_PATH", "").strip()  # path to circle logo PNG (optional)
MEMORY_FILE = os.path.join(os.path.expanduser("~"), ".aura_memory.json")  # persistent memory

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
    Robust Gemini call wrapper that tries multiple SDK styles.
    Returns plain text (string) or None on failure.
    """
    if genai is None:
        print("[Gemini] SDK not installed.")
        return None
    if not api_key:
        print("[Gemini] No API key provided.")
        return None

    try:
        genai.configure(api_key=api_key)
    except Exception as e:
        print("[Gemini] configure error:", e)
        # continue - some SDK versions still allow calls without configure
    last_exc = None

    # Attempt style 1: new GenerativeModel(...).generate_content(...)
    try:
        ModelCls = getattr(genai, "GenerativeModel", None)
        if ModelCls:
            model = ModelCls(GEMINI_MODEL)
            try:
                # attempt both single-string and list prompt forms
                try:
                    resp = model.generate_content(prompt)
                except Exception:
                    resp = model.generate_content([prompt])
            except Exception as e:
                raise e
            # resp may have .text or .candidates
            if hasattr(resp, "text") and resp.text:
                return resp.text
            if hasattr(resp, "candidates") and resp.candidates:
                try:
                    cand = resp.candidates[0]
                    # candidate content may be a list of content pieces
                    content = getattr(cand, "content", None)
                    if isinstance(content, list):
                        # find first piece with .text
                        for piece in content:
                            if hasattr(piece, "text") and piece.text:
                                return piece.text
                        return str(content)
                    else:
                        return getattr(content, "text", None) or str(content)
                except Exception:
                    pass
    except Exception as e:
        last_exc = e
        #print("[Gemini] style1 failed:", e)

    # Attempt style 2: legacy get_model(...).predict(...)
    try:
        model2 = genai.get_model(GEMINI_MODEL)
        try:
            resp2 = model2.predict(prompt)
        except Exception:
            resp2 = model2.predict([prompt])
        if hasattr(resp2, "text") and resp2.text:
            return resp2.text
        if hasattr(resp2, "candidates") and resp2.candidates:
            try:
                cand = resp2.candidates[0]
                content = getattr(cand, "content", None)
                if isinstance(content, list):
                    for piece in content:
                        if hasattr(piece, "text") and piece.text:
                            return piece.text
                    return str(content)
                else:
                    return getattr(content, "text", None) or str(content)
            except Exception:
                pass
    except Exception as e:
        last_exc = e
        #print("[Gemini] style2 failed:", e)

    # Attempt style 3: older flat API via genai.generate (very old)
    try:
        if hasattr(genai, "generate"):
            resp3 = genai.generate(model=GEMINI_MODEL, prompt=prompt, temperature=temperature)
            if isinstance(resp3, dict):
                # sometimes returns {'candidates':[{'content': '...'}]} or {'text': '...'}
                if "text" in resp3 and resp3["text"]:
                    return resp3["text"]
                if "candidates" in resp3 and resp3["candidates"]:
                    c = resp3["candidates"][0]
                    if isinstance(c, dict) and "content" in c:
                        cont = c["content"]
                        if isinstance(cont, str):
                            return cont
                        if isinstance(cont, list):
                            return cont[0].get("text") if cont and isinstance(cont[0], dict) else str(cont)
    except Exception as e:
        last_exc = e
        #print("[Gemini] style3 failed:", e)

    print("[Gemini] call error:", last_exc)
    return None

def call_gemini_for(role_key, prompt, temperature=0.0, timeout=12):
    """
    role_key: one of PLANNER_KEY, CHAT_KEY, PROMPT_KEY, IMAGE_KEY, ACTION_KEY
    Returns text or None.
    """
    # your call_gemini_raw wrapper should already exist and accept api_key parameter
    return call_gemini_raw(role_key, prompt, temperature=temperature, timeout=timeout)

# -------------------- MEMORY --------------------
def load_memory():
    try:
        if os.path.exists(MEMORY_FILE):
            with open(MEMORY_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception as e:
        print("load_memory error:", e)
    # default structure
    return {"facts": {}, "reminders": [], "meetings": [], "todos": []}

def save_memory(mem):
    try:
        with open(MEMORY_FILE, "w", encoding="utf-8") as f:
            json.dump(mem, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print("save_memory error:", e)

memory = load_memory()

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
    """Smart planner wrapper:
       - Quick local handling for simple open/search/time/close commands (but only if strict fallback returns a plan)
       - If conversational (chit-chat) call the chat agent (CHAT_KEY)
       - Otherwise call PLANNER_KEY (Gemini) with a strict JSON-only prompt
    """
    print("[Planner] in:", user_text)

    # 1) Local quick attempt: only accept strict fallback if it returns a concrete plan.
    if _is_simple_local_cmd(user_text):
        plan = strict_fallback_parse(user_text)
        if plan:
            print("[Planner] local fallback used:", plan)
            return plan
        else:
            # quick local parse was not confident — forward to LLM (avoid returning None here)
            print("[Planner] local quick parse not decisive; forwarding to LLM.")

    # 2) If chit-chat or short Q, prefer chat key for natural reply
    if _is_chitchat(user_text):
        if CHAT_KEY:
            chat_prompt = f"You are AURA, a brief friendly assistant. Reply concisely to: {user_text}"
            chat_res = call_gemini_for(CHAT_KEY, chat_prompt, timeout=5)
            if chat_res:
                return {"speak": chat_res.strip()}
        # fallback to default speak
        return {"speak": f"I heard: {user_text}"}

    # 3) Ask the planner LLM to return strict JSON plan only.
    system_prompt = (
        "You are AURA Planner. **RETURN ONLY A SINGLE JSON OBJECT** that is a plan. "
        "Allowed keys (optional): urls (list), apps (list), close_apps (list), speak (string), act (dict), "
        "reminder (dict: {when:ISO,time zone optional, message:...}), todo (dict), facts (dict), "
        "generate (string prompt), memory (string), images (list), copy (bool). "
        "If the user input is only conversational (greeting / small talk), respond with {\"speak\":\"<short reply>\"}. "
        "Do not include any commentary outside the JSON. Use ISO-8601 datetimes. Example outputs:\n\n"
        '{"urls":["https://www.youtube.com"], "speak":"Opening YouTube for you."}\n\n'
        '{"speak":"I\'m fine, thanks for asking!"}\n\n'
        "User: " + user_text + "\nReturn JSON now:"
    )

    # Prefer local LLM endpoint if configured
    if LLM_URL:
        try:
            res = call_local_llm(LLM_URL, system_prompt, timeout=8)
            if isinstance(res, dict) and "text" in res:
                plan = extract_plan_from_text(res["text"])
                if plan:
                    print("[Planner] got plan from local LLM:", plan)
                    return plan
                return {"speak": res["text"].strip()}
        except Exception as e:
            print("[Planner] local LLM error:", e)

    # Use PLANNER_KEY (Gemini)
    if PLANNER_KEY:
        try:
            res = call_cloud_llm(PLANNER_KEY, system_prompt, timeout=8)
            if isinstance(res, dict) and "text" in res and res["text"]:
                plan = extract_plan_from_text(res["text"])
                if plan:
                    print("[Planner] got plan from LLM:", plan)
                    return plan
                # Non-JSON reply -> try chat fallback
                plain = res["text"].strip()
                if CHAT_KEY:
                    chat_res = call_gemini_for(CHAT_KEY, f"You are AURA. Reply shortly to: {user_text}", timeout=5)
                    if chat_res:
                        return {"speak": chat_res.strip()}
                return {"speak": plain}
        except Exception as e:
            print("[Planner] cloud planner error:", e)

    # last resort: strict fallback only if something matches
    try:
        plan = strict_fallback_parse(user_text)
        if plan:
            print("[Planner] strict fallback used:", plan)
            return plan
    except Exception as e:
        print("[Planner] strict fallback error:", e)

    # Final safety net: explicitly return None to indicate planner couldn't make a plan
    print("[Planner] No valid plan generated; returning None.")
    return None


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

def execute_plan(plan, ui_callback=None):
    """
    Executes the planner's structured plan dictionary.
    Safely dispatches actions to modular handler functions.
    Scalable and concurrency-safe.
    """
    global window

    try:
        # ✅ Validation: handle None or invalid plans
        if not plan or not isinstance(plan, dict):
            print("[Executor] Invalid or empty plan — skipping execution.")
            return

        # ✅ Ensure UI callback is always available
        if ui_callback is None:
            if 'window' in globals() and hasattr(window, "_add_chat_bubble"):
                ui_callback = lambda msg, src="aura": QtCore.QMetaObject.invokeMethod(
                    window, "_add_chat_bubble",
                    QtCore.Qt.QueuedConnection,
                    QtCore.Q_ARG(str, src),
                    QtCore.Q_ARG(str, msg)
                )
            else:
                # fallback safe no-op callback
                ui_callback = lambda msg, src="aura": print(f"[{src.upper()} says]: {msg}")

        print(f"[Executor] Received plan: {plan}")

        # ✅ Execute each key in the plan dictionary
        for key, value in plan.items():
            handler_name = f"_handle_{key}"

            if handler_name in globals():
                handler_func = globals()[handler_name]

                try:
                    # ✅ Use threading for async-safe actions (e.g., speech, browser open)
                    import threading
                    threading.Thread(
                        target=lambda: handler_func(value, ui_callback=ui_callback),
                        daemon=True
                    ).start()

                except Exception as e:
                    print(f"[Executor] Error executing handler '{handler_name}': {e}")

            else:
                print(f"[Executor] ⚠️ No handler for key '{key}'")

    except Exception as e:
        print(f"[Executor] Fatal error in execute_plan: {e}")

# ------------------------------
# Handlers for different plan keys
# ------------------------------

def _handle_speak(text, ui_callback=None):
    """
    Speak text without blocking or causing overlapping speech threads.
    """
    print(f"[AURA says]: {text}")
    
    # Show message in chat bubble
    if ui_callback:
        ui_callback(text, "aura")

    # Run TTS in a dedicated thread safely
    def _tts_worker():
        try:
            import pyttsx3
            engine = pyttsx3.init()
            engine.say(text)
            engine.runAndWait()
        except Exception as e:
            print(f"[TTS error]: {e}")

    threading.Thread(target=_tts_worker, daemon=True).start()

def _handle_urls(url_list, ui_callback=None):
    """Open one or more URLs."""
    try:
        import webbrowser
        for url in url_list:
            if url and isinstance(url, str):
                print(f"[Executor] Opening URL: {url}")
                webbrowser.open(url)
    except Exception as e:
        print("[URL error]:", e)


def _handle_memory(text, ui_callback=None):
    """Save context or reminders to a memory file."""
    try:
        with open("aura_memory.txt", "a", encoding="utf-8") as f:
            f.write(f"{text}\n")
        print("[Executor] Memory saved.")
    except Exception as e:
        print("[Memory error]:", e)


def _handle_facts(facts, ui_callback=None):
    """Store structured facts (like name, preferences)."""
    try:
        import json
        with open("aura_facts.json", "a", encoding="utf-8") as f:
            json.dump(facts, f)
            f.write("\n")
        print("[Executor] Facts saved:", facts)
    except Exception as e:
        print("[Facts error]:", e)


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


def _handle_copy(text, ui_callback=None):
    """Show a copyable text box in the chat UI."""
    if ui_callback:
        ui_callback(text, "aura")
    else:
        if 'window' in globals() and hasattr(window, "_add_chat_bubble"):
            QtCore.QMetaObject.invokeMethod(
                window, "_add_chat_bubble",
                QtCore.Qt.QueuedConnection,
                QtCore.Q_ARG(str, "aura"),
                QtCore.Q_ARG(str, f"📋 {text}\n(click to copy)")
            )

def _handle_generate(prompt, ui_callback=None):
    """
    Generate detailed text (report, email, content) using Gemini Chat model.
    """
    print(f"[Executor] Generating content for: {prompt}")
    try:
        import google.generativeai as genai
        genai.configure(api_key=os.getenv("CHAT_KEY"))
        model = genai.GenerativeModel("gemini-2.0-flash")
        response = model.generate_content(prompt)
        generated = response.text.strip() if hasattr(response, 'text') else "Done!"
        
        if ui_callback:
            ui_callback(generated, "aura")
        _handle_speak("Here’s the result. You can copy it if you like.", ui_callback)
        
        # Optional: Add a copyable chat bubble
        if 'window' in globals() and hasattr(window, "_add_copy_bubble"):
            QtCore.QMetaObject.invokeMethod(
                window, "_add_copy_bubble",
                QtCore.Qt.QueuedConnection,
                QtCore.Q_ARG(str, generated)
            )
    except Exception as e:
        print(f"[Generator error]: {e}")
        _handle_speak("Sorry, I couldn’t generate that right now.", ui_callback)

def _handle_apps(app_data, ui_callback=None):
    """Open or close desktop applications."""
    try:
        import os, subprocess
        if isinstance(app_data, dict):
            action = app_data.get("action", "open")
            app = app_data.get("name", "")
        else:
            action = "open"
            app = app_data

        if action == "open":
            subprocess.Popen(app, shell=True)
            print(f"[Executor] Opened app: {app}")
        elif action == "close":
            os.system(f"taskkill /im {app}.exe /f")
            print(f"[Executor] Closed app: {app}")
    except Exception as e:
        print("[App error]:", e)


def _handle_reminder(data, ui_callback=None):
    """Add a scheduled reminder."""
    try:
        import datetime, threading, time
        msg = data.get("text", "Reminder!")
        when = data.get("time")

        if when:
            def reminder_thread():
                target = datetime.datetime.strptime(when, "%Y-%m-%d %H:%M:%S")
                now = datetime.datetime.now()
                wait = (target - now).total_seconds()
                if wait > 0:
                    time.sleep(wait)
                _handle_speak(f"⏰ Reminder: {msg}")

            threading.Thread(target=reminder_thread, daemon=True).start()
            print(f"[Executor] Reminder set for {when}")

    except Exception as e:
        print("[Reminder error]:", e)

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
class AuraWidget(QtWidgets.QWidget):
    def __init__(self):
        super().__init__()
        # set window flags manually (PySide6 syntax)
        self.setWindowFlags(
            QtCore.Qt.FramelessWindowHint |
            QtCore.Qt.WindowStaysOnTopHint |
            QtCore.Qt.Tool
        )
        self.setAttribute(QtCore.Qt.WA_TranslucentBackground, True)
        self.expanded_size = QtCore.QSize(420, 560)
        self.minimized_size = QtCore.QSize(84, 84)
        self.is_minimized = False

        # signals
        self.signals = QtCore.QObject()
        self._setup_ui()
        self.mic = MicListener(self._on_mic_text)
        self._load_startup_memory()
        # Start minimized after setup
        QtCore.QTimer.singleShot(250, self._ensure_minimized_start)

    def _setup_ui(self):
    # ========= Base window =========
        self.setFixedSize(self.expanded_size)
        self.setWindowTitle("AURA AI")
        self.setStyleSheet("background: transparent;")

    # ========= Main frame (chat box) =========
        self.main_frame = QtWidgets.QFrame(self)
        self.main_frame.setObjectName("main_frame")
        self.main_frame.setGeometry(0, 0, self.expanded_size.width(), self.expanded_size.height())
        self.main_frame.setStyleSheet("""
            #main_frame {
                background-color: rgba(25, 25, 25, 235);
                border-radius: 20px;
                border: 2px solid rgba(255, 100, 100, 0.2);
            }
        """)

    # ========= Layout =========
        layout = QtWidgets.QVBoxLayout(self.main_frame)
        layout.setContentsMargins(16, 16, 16, 12)
        layout.setSpacing(8)

    # ========= Header =========
        header = QtWidgets.QHBoxLayout()
        header.setSpacing(6)

    # AURA Logo (user will supply file path)
        self.logo_label = QtWidgets.QLabel()
        if AURA_LOGO_PATH and os.path.exists(AURA_LOGO_PATH):
            pix = QtGui.QPixmap(AURA_LOGO_PATH).scaled(38, 38, QtCore.Qt.KeepAspectRatio, QtCore.Qt.SmoothTransformation)
        else:
            pix = QtGui.QPixmap(38, 38)
            pix.fill(QtCore.Qt.transparent)
            painter = QtGui.QPainter(pix)
            painter.setRenderHint(QtGui.QPainter.Antialiasing)
            painter.setBrush(QtGui.QColor("#ff4b2b"))
            painter.setPen(QtCore.Qt.NoPen)
            painter.drawEllipse(0, 0, 38, 38)
            painter.end()
        self.logo_label.setPixmap(pix)
        header.addWidget(self.logo_label)

    # Title
        title = QtWidgets.QLabel("AURA AI")
        title.setStyleSheet("color:white; font-size:20px; font-weight:600;")
        header.addWidget(title)
        header.addStretch()

    # Minimize + Close buttons
        self.btn_minimize = QtWidgets.QPushButton("—")
        self.btn_minimize.setFixedSize(30, 24)
        self.btn_minimize.setStyleSheet("background:transparent; color:white; font-weight:bold; border:none;")
        self.btn_minimize.clicked.connect(self.toggle_minimize)
        header.addWidget(self.btn_minimize)

        self.btn_close = QtWidgets.QPushButton("✕")
        self.btn_close.setFixedSize(30, 24)
        self.btn_close.setStyleSheet("background:transparent; color:white; font-weight:bold; border:none;")
        self.btn_close.clicked.connect(self.close)
        header.addWidget(self.btn_close)

        layout.addLayout(header)

    # ========= Chat Area =========
        self.scroll = QtWidgets.QScrollArea()
        self.scroll.setFrameShape(QtWidgets.QFrame.NoFrame)
        self.scroll.setWidgetResizable(True)
        self.scroll.setStyleSheet("""
            QScrollArea {
                background: transparent;
            }
            QScrollBar:vertical {
                background: #1E1E1E;
                width: 8px;
                border-radius: 4px;
            }
            QScrollBar::handle:vertical {
                background: #ff4b2b;
                border-radius: 4px;
            }
        """)

        self.chat_container = QtWidgets.QWidget()
        self.chat_layout = QtWidgets.QVBoxLayout(self.chat_container)
        self.chat_layout.setContentsMargins(6, 6, 6, 6)
        self.chat_layout.setSpacing(10)
        self.chat_layout.addStretch()

        self.scroll.setWidget(self.chat_container)
        layout.addWidget(self.scroll, 1)

    # ========= Input Row =========
        input_row = QtWidgets.QHBoxLayout()
        input_row.setSpacing(6)

        self.input_edit = QtWidgets.QLineEdit()
        self.input_edit.setPlaceholderText("Ask Aura anything...")
        self.input_edit.setStyleSheet("""
            background-color: #2b2b2b;
            color: white;
            padding: 10px;
            border-radius: 12px;
            font-size: 14px;
        """)
        self.input_edit.returnPressed.connect(self.on_send)
        input_row.addWidget(self.input_edit, 1)

    # Microphone Button
        self.btn_mic = QtWidgets.QPushButton("🎤")
        self.btn_mic.setFixedSize(46, 46)
        self.btn_mic.setStyleSheet("""
            background-color: qlineargradient(spread:pad, x1:0, y1:0, x2:1, y2:1,
            stop:0 #ff4b2b, stop:1 #ff416c);
            border-radius: 23px;
            color: white;
            font-size: 20px;
        """)
        self.btn_mic.clicked.connect(self._mic_toggle)
        input_row.addWidget(self.btn_mic)

    # Send Button
        self.btn_send = QtWidgets.QPushButton("➤")
        self.btn_send.setFixedSize(46, 46)
        self.btn_send.setStyleSheet("""
            background-color: white;
            color: #ff4b2b;
            font-size: 18px;
            border-radius: 12px;
            font-weight: bold;
        """)
        self.btn_send.clicked.connect(self.on_send)
        input_row.addWidget(self.btn_send)

        layout.addLayout(input_row)

    # ========= Footer =========
        footer = QtWidgets.QLabel("✨ AURA — Your Personal AI Assistant")
        footer.setStyleSheet("color:#ffd7c4; font-size:10px; padding-top:4px;")
        layout.addWidget(footer)

    # ========= Mouse Drag Support =========
        self._drag_pos = None
        self.main_frame.mousePressEvent = self._on_mouse_press
        self.main_frame.mouseMoveEvent = self._on_mouse_move

    # ========= Position =========
        self._place_bottom_right()

    # ========= Floating (Minimized) Button =========
        # ✅ Create minimized circular overlay button first
        self.min_button = QtWidgets.QPushButton(self)
        self.min_button.setVisible(False)
        self.min_button.setFixedSize(self.minimized_size)
        self.min_button.setStyleSheet("border-radius:42px; border:none;")

        if self.logo_pix:
            self.min_button.setIcon(QtGui.QIcon(self.logo_pix))
            self.min_button.setIconSize(self.minimized_size)
        else:
            self.min_button.setStyleSheet(
                "background: qlineargradient(spread:pad, x1:0,y1:0,x2:1,y2:1, stop:0 #B71C1C, stop:1 #3B0018); border-radius:42px;"
            )

        self.min_button.clicked.connect(self.toggle_minimize)

        # ✅ After the button is created, position it at the bottom-right corner
        #self._place_bottom_right()
        #self._update_min_button_pos()


        def _on_mouse_press(self, ev):
            if ev.button() == QtCore.Qt.LeftButton:
                self._drag_pos = ev.globalPosition().toPoint() - self.pos()

        def _on_mouse_move(self, ev):
            if self._drag_pos:
                self.move(ev.globalPosition().toPoint() - self._drag_pos)
                self._update_min_button_pos()

    @QtCore.Slot(str, str)
    def _add_chat_bubble(self, sender: str, message: str):
        """Adds a chat bubble (either user or AURA) to the chat layout."""
        bubble = QtWidgets.QLabel(message)
        bubble.setWordWrap(True)
        bubble.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
        bubble.setStyleSheet(
            """
            QLabel {
                background-color: %s;
                color: white;
                border-radius: 12px;
                padding: 8px 12px;
                font-size: 14px;
            }
            """ % ("#512DA8" if sender == "user" else "#303030")
        )

        wrapper = QtWidgets.QHBoxLayout()
        if sender == "user":
            wrapper.addStretch()
            wrapper.addWidget(bubble)
        else:
            wrapper.addWidget(bubble)
            wrapper.addStretch()

        self.chat_layout.insertLayout(self.chat_layout.count() - 1, wrapper)
        QtCore.QTimer.singleShot(100, lambda: self.scroll.verticalScrollBar().setValue(
            self.scroll.verticalScrollBar().maximum()
        ))

    # ========== Mouse Drag Support ==========
    def _on_mouse_press(self, event):
        """Store the position of the mouse when clicked for dragging."""
        if event.button() == QtCore.Qt.LeftButton:
            self._drag_pos = event.globalPos() - self.frameGeometry().topLeft()
            event.accept()

    def _on_mouse_move(self, event):
        """Allow the user to drag the floating widget."""
        if event.buttons() == QtCore.Qt.LeftButton and self._drag_pos is not None:
            self.move(event.globalPos() - self._drag_pos)
            event.accept()

    def _place_bottom_right(self):
        """Position the AURA widget and ensure logo is safe to access."""
        screen = QtWidgets.QApplication.primaryScreen().geometry()
        x = screen.width() - self.width() - 20
        y = screen.height() - self.height() - 20
        self.move(x, y)

        # ✅ Ensure min_button exists
        if not hasattr(self, "min_button"):
            self.min_button = QtWidgets.QPushButton(self)
            self.min_button.setVisible(False)
            self.min_button.setFixedSize(self.minimized_size)
            self.min_button.setStyleSheet("border-radius:42px; border:none;")

        # ✅ Ensure logo_pix is defined (fallback to red gradient if missing)
        if not hasattr(self, "logo_pix") or self.logo_pix is None:
            pix = QtGui.QPixmap(42, 42)
            pix.fill(QtCore.Qt.transparent)
            p = QtGui.QPainter(pix)
            p.setRenderHint(QtGui.QPainter.Antialiasing)
            p.setBrush(QtGui.QColor("#FF6F61"))
            p.setPen(QtCore.Qt.NoPen)
            p.drawEllipse(0, 0, 42, 42)
            p.end()
            self.logo_pix = pix

        # ✅ Set icon for minimized button safely
        self.min_button.setIcon(QtGui.QIcon(self.logo_pix))
        self.min_button.setIconSize(self.minimized_size)

        # ✅ Finally, move minimized button
        #self._update_min_button_pos()

    def _update_min_button_pos(self):
        screen = QtWidgets.QApplication.primaryScreen().availableGeometry()
        w = self.minimized_size.width()
        x = screen.width() - w - 20
        y = screen.height() - w - 40
        self.min_button.move(x, y)

    def _ensure_minimized_start(self):
        # minimize at start so only the circular button appears
        self.toggle_minimize(minimize=True)

    def toggle_minimize(self, minimize=None):
        """Minimize to circular button or expand. minimize param forces state."""
        if minimize is None:
            minimize = not self.is_minimized
        if minimize and not self.is_minimized:
            # hide main frame and show circular button
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
        elif minimize and self.is_minimized:
            pass

    def _mic_toggle(self):
        if self.btn_mic.text() == "🎤":
            # start listening
            self.btn_mic.setText("■")
            self.btn_mic.setStyleSheet("background:#ff4d4d; border-radius:22px; color:white;")
            self._append_user_system("System", "Listening...")
            self.mic.start_listening()
        else:
            self.btn_mic.setText("🎤")
            self.btn_mic.setStyleSheet("background:#ff8a65; border-radius:22px; color:white;")
            self._append_user_system("System", "Stopped listening.")
            self.mic.stop_listening()

    def _on_mic_text(self, text):
        # mic thread -> UI thread update via queued singleShot (safe)
        QtCore.QTimer.singleShot(0, lambda: self.input_edit.setText(text))
        QtCore.QTimer.singleShot(0, lambda: self._append_user_system("You (dictation)", text))


    @QtCore.Slot(str, str)
    def _append_user_system(self, who, text):
        self._add_chat_bubble(text, "user" if who.startswith("You") else "AURA", auto_scroll=True)

    def _add_chat_bubble(self, text, source="aura", auto_scroll=True):
        bubble = ChatBubble(text, source="user" if source=="user" else "aura")
        # insert above the stretch
        self.chat_layout.insertWidget(self.chat_layout.count()-1, bubble)
        if auto_scroll:
            QtCore.QTimer.singleShot(80, lambda: self.scroll.verticalScrollBar().setValue(self.scroll.verticalScrollBar().maximum()))

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

        # If planner returns None (no plan), gracefully inform the user and skip execution
            if not plan or not isinstance(plan, dict):
                print("[Planner] No plan returned (None). Skipping execution.")
                QtCore.QTimer.singleShot(0, lambda: self._add_chat_bubble("Sorry, I couldn't create a plan for that. Please try rephrasing.", "aura"))
                speak_async("Sorry, I couldn't create a plan for that. Please try rephrasing.")
                return

        # Build a short summary for UI (non-exhaustive)
            summary = []
            if "speak" in plan and plan.get("speak"):
                summary.append(plan["speak"])
            if "urls" in plan:
                urls = plan["urls"]
                if isinstance(urls, list):
                    summary.append("Opening: " + ", ".join(urls))
                else:
                    summary.append("Opening: " + str(urls))
            if "apps" in plan: summary.append("Launching apps")
            if "reminder" in plan: summary.append("Reminder set")
            if "act" in plan: summary.append("Action queued")

            if summary:
                text_summary = " • ".join(summary)
                QtCore.QTimer.singleShot(0, lambda: self._add_chat_bubble(text_summary, "aura"))
                speak_async(text_summary)

        # Execute plan (with UI callback for long messages)
            execute_plan(plan, ui_callback=self._ui_callback_from_execute)

        except Exception as e:
            print("plan/execute error:", e)
            QtCore.QTimer.singleShot(0, lambda: self._add_chat_bubble("Sorry, I couldn't process that.", "aura"))
            speak_async("Sorry, I couldn't process that.")



    def _ui_callback_from_execute(self, text, source="aura"):
        """Called from execute_plan to show messages in UI; schedule GUI update on main thread."""
        QtCore.QTimer.singleShot(0, lambda: self._add_chat_bubble(text, source))
        # speak as well for aura
        if source == "aura":
            speak_async(text)


    def _load_startup_memory(self):
        # Schedule all reminders saved in memory at startup
        for rem in memory.get("reminders", []):
            try:
                schedule_reminder(rem)
            except Exception:
                pass

# -------------------- Run App --------------------
def main():
    import sys
    from PySide6 import QtWidgets

    try:
        app = QtWidgets.QApplication(sys.argv)
        app.setQuitOnLastWindowClosed(False)

        # ✅ Hold reference
        window = AuraWidget()
        window.show()

        print("[AURA] UI started successfully.")
        sys.exit(app.exec())

    except Exception as e:
        import traceback
        print("[AURA ERROR]", e)
        traceback.print_exc()


if __name__ == "__main__":
    main()

"""
AURA Agentic Assistant (v1)
- Uses VOSK (if available) for continuous wakeword detection and reliable offline STT.
- Falls back to Google SpeechRecognition if VOSK not installed.
- Sends user command to LLM (Gemini via a Flask URL or google-generativeai).
- LLM must respond with a JSON plan or plain text; code will try to extract JSON.
- Executes actions: open_url, search, open_app, close_app, close_tab, tell_time, answer_text.
- Safety: asks for confirmation for destructive actions unless AUTO_EXECUTE=True.
"""

import os
import sys
import time
import json
import queue
import threading
import subprocess
import webbrowser
import re
from datetime import datetime

# TTS
import pyttsx3

# For fallback STT
import speech_recognition as sr

# Optional components
try:
    from vosk import Model, KaldiRecognizer
    import sounddevice as sd
    VOSK_AVAILABLE = True
except Exception:
    VOSK_AVAILABLE = False

# Process management
try:
    import psutil
except Exception:
    psutil = None

# keyboard automation for close_tab
try:
    import pyautogui
except Exception:
    pyautogui = None

# HTTP requests
import requests
from dotenv import load_dotenv
load_dotenv()

# ---------------- Config ----------------
WAKE_WORDS = ["hey aura", "aura", "wake aura"]
MODEL_PATH = os.getenv("VOSK_MODEL_PATH", "models/vosk-model-small-en-us-0.15")  # change if needed
GEMINI_API_URL = os.getenv("GEMINI_API_URL", "")   # If you have a Flask endpoint for Gemini
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")   # For google-generativeai fallback (optional)
AUTO_EXECUTE = False   # If True, will not ask confirmation for destructive actions
SILENCE_TIMEOUT = 1.2  # seconds of silence to end command capture
SAMPLE_RATE = 16000    # vosk expects 16000

# ---------------- TTS init ----------------
tts = pyttsx3.init()
tts.setProperty('rate', 160)
tts.setProperty('volume', 1.0)

def speak(text):
    print("AURA:", text)
    try:
        tts.say(text)
        tts.runAndWait()
    except Exception as e:
        print("TTS error:", e)

# ---------------- Helpers: JSON extraction ----------------
def extract_json_from_text(s):
    s = s or ""
    start = s.find('{')
    end = s.rfind('}')
    if start != -1 and end != -1 and end > start:
        sub = s[start:end+1]
        try:
            return json.loads(sub)
        except Exception:
            pass
    # fallback: try to parse as JSON array
    start = s.find('[')
    end = s.rfind(']')
    if start != -1 and end != -1 and end > start:
        try:
            return json.loads(s[start:end+1])
        except Exception:
            pass
    return None

# ---------------- LLM integration ----------------
def call_llm_for_plan(command_text):
    """
    Preference order:
    1) If GEMINI_API_URL is set, POST {"prompt": "..."} and expect JSON { "plan": [ {...}, ... ] }
    2) Else fallback to plain text response if no JSON returned.
    """
    if GEMINI_API_URL:
        try:
            resp = requests.post(GEMINI_API_URL, json={"prompt": command_text}, timeout=12)
            if resp.status_code == 200:
                # LLM flask may return JSON directly or text containing a JSON block
                try:
                    body = resp.json()
                    # If body contains 'plan' key return it
                    if isinstance(body, dict) and 'plan' in body:
                        return body
                    # else if it's text, try extract JSON
                except Exception:
                    body_text = resp.text
                    j = extract_json_from_text(body_text)
                    if j:
                        return j
                    return {"plan":[{"type":"answer_text","text": body_text}]}
            else:
                return {"plan":[{"type":"answer_text","text": f"Error from LLM endpoint: {resp.status_code}"}]}
        except Exception as e:
            return {"plan":[{"type":"answer_text","text": f"LLM endpoint call failed: {e}"}]}

    # If no GEMINI_API_URL, we fallback to a simple heuristic: try to parse commands locally
    # (This ensures assistant still functions even without LLM)
    return fallback_command_to_plan(command_text)

# ---------------- Fallback simple parser ----------------
def fallback_command_to_plan(cmd):
    c = cmd.lower()
    if "open" in c and "http" in c:
        url = re.search(r"(https?://\S+)", c)
        if url:
            return {"plan":[{"type":"open_url","value": url.group(1), "confirm": False}]}
    if "open chatgpt" in c or "open chat gpt" in c:
        return {"plan":[{"type":"open_url","value":"https://chat.openai.com","confirm": False}]}
    if "youtube" in c:
        return {"plan":[{"type":"open_url","value":"https://www.youtube.com","confirm": False}]}
    if c.startswith("search ") or "search " in c:
        q = re.sub(r"search\s*", "", c, count=1)
        return {"plan":[{"type":"search","value": q.strip(), "confirm": False}]}
    if "time" in c or "what time" in c:
        return {"plan":[{"type":"tell_time","confirm": False}]}
    if "close tab" in c or "close the tab" in c:
        return {"plan":[{"type":"close_tab","confirm": True}]}
    if "close app" in c or "close the application" in c:
        # you could include app name; fallback to asking
        return {"plan":[{"type":"close_app","value":None,"confirm":True}]}
    # default: answer as text (no action)
    return {"plan":[{"type":"answer_text","text": f"I heard: {cmd}", "confirm": False}]}

# ---------------- Action execution ----------------
def open_url(url):
    try:
        webbrowser.open(url, new=2)
        return True, f"Opened {url}"
    except Exception as e:
        return False, f"Failed to open {url}: {e}"

def do_search(query):
    url = f"https://www.google.com/search?q={query.replace(' ', '+')}"
    return open_url(url)

def open_app(path_or_name):
    # Windows: os.startfile with full path or exe name in PATH
    try:
        if sys.platform.startswith("win"):
            if os.path.exists(path_or_name):
                os.startfile(path_or_name)
                return True, f"Launched {path_or_name}"
            else:
                # try run by name (may need full path)
                subprocess.Popen([path_or_name], shell=True)
                return True, f"Attempted to launch {path_or_name}"
        else:
            subprocess.Popen([path_or_name])
            return True, f"Launched {path_or_name}"
    except Exception as e:
        return False, f"Failed to open app {path_or_name}: {e}"

def close_app_by_name(name):
    if not psutil:
        return False, "psutil not installed; cannot close apps automatically."
    killed = []
    for proc in psutil.process_iter(['name', 'pid']):
        try:
            if name.lower() in (proc.info['name'] or "").lower():
                proc.terminate()
                killed.append(proc.info['name'])
        except Exception:
            pass
    if killed:
        return True, f"Closed: {', '.join(killed)}"
    return False, f"No matching running process found for '{name}'"

def close_active_tab():
    # Active window gets Ctrl+W (works on most browsers)
    if not pyautogui:
        return False, "pyautogui not installed; cannot send keystrokes."
    try:
        pyautogui.hotkey('ctrl', 'w')
        return True, "Closed current tab (Ctrl+W sent)."
    except Exception as e:
        return False, f"Failed to send close-tab keys: {e}"

def tell_time():
    t = datetime.now().strftime("%I:%M %p")
    return True, f"The time is {t}"

# Execute plan list
def execute_plan(plan, ask_confirm=True):
    """
    plan: list of action dicts
    Example action dict:
    { "type": "open_url", "value": "https://chat.openai.com", "confirm": False }
    """
    results = []
    for action in plan:
        typ = action.get("type")
        confirm = action.get("confirm", False)
        val = action.get("value") or action.get("text") or action.get("to") or action.get("app") or action.get("name")

        # Ask confirm if required and not auto
        if confirm and ask_confirm and not AUTO_EXECUTE:
            speak(f"I will {typ} {val if val else ''}. Say yes to confirm, no to cancel.")
            ans = capture_simple_yes_no(timeout=6)
            if ans is False:
                results.append((typ, False, "User cancelled"))
                continue

        if typ == "open_url":
            ok, msg = open_url(val)
            speak(msg)
            results.append((typ, ok, msg))

        elif typ == "search":
            ok, msg = do_search(val)
            speak(msg)
            results.append((typ, ok, msg))

        elif typ == "open_app":
            if not val:
                speak("Which application should I open?")
                # you could capture next voice phrase
                results.append((typ, False, "No app specified"))
            else:
                ok, msg = open_app(val)
                speak(msg)
                results.append((typ, ok, msg))

        elif typ == "close_app":
            if not val:
                speak("Which app should I close? say the application name.")
                # optionally capture a follow-up phrase – omitted here
                results.append((typ, False, "No app specified"))
            else:
                ok, msg = close_app_by_name(val)
                speak(msg)
                results.append((typ, ok, msg))

        elif typ == "close_tab":
            ok, msg = close_active_tab()
            speak(msg)
            results.append((typ, ok, msg))

        elif typ == "tell_time":
            ok, msg = tell_time()
            speak(msg)
            results.append((typ, ok, msg))

        elif typ == "answer_text":
            speak(val)
            results.append((typ, True, val))

        else:
            # Unknown action: treat as a spoken reply
            speak(action.get("text") or "I don't know how to perform that action yet.")
            results.append((typ, False, "unknown action"))

    return results

# ---------------- Simple yes/no capture (blocking, brief) ----------------
def capture_simple_yes_no(timeout=6):
    """
    Listen quickly and answer yes/no. returns True if yes, False if no, None if unknown.
    """
    # Use speech_recognition for quick yes/no
    r = sr.Recognizer()
    with sr.Microphone() as source:
        r.adjust_for_ambient_noise(source, duration=0.4)
        try:
            audio = r.listen(source, timeout=timeout, phrase_time_limit=3)
            text = r.recognize_google(audio).lower()
            print("Confirm heard:", text)
            if any(w in text for w in ["yes", "yeah", "yup", "sure", "do it", "confirm"]):
                return True
            if any(w in text for w in ["no", "not now", "don't", "cancel"]):
                return False
            return None
        except Exception as e:
            print("confirm capture failed:", e)
            return None

# ---------------- VOSK-based continuous listener (background thread) ----------------
class VoskListener(threading.Thread):
    # FIX 1: The constructor must be named __init__ (double underscores)
    def __init__(self, model_path, sample_rate=16000):
        super().__init__(daemon=True)
        self.model_path = model_path
        self.sample_rate = sample_rate
        self.queue = queue.Queue()
        self.running = False
        self._rec = None

    def run(self):
        try:
            model = Model(self.model_path)
        except Exception as e:
            print("VOSK model load failed:", e)
            return
        self._rec = KaldiRecognizer(model, self.sample_rate)
        self._rec.SetWords(True)
        self.running = True
        def callback(indata, frames, time_info, status):
            if not self.running: 
                return
                if self._rec.AcceptWaveform(bytes(indata)):                res = self._rec.Result()
                try:
                    j = json.loads(res)
                    text = j.get('text', '')
                    if text:
                        self.queue.put(text)
                except Exception:
                    pass
            else:
                # partial
                part = self._rec.PartialResult()
                try:
                    j = json.loads(part)
                    p = j.get('partial','').strip()
                    # Optionally push partials for wakeword detection (less stable)
                    if p:
                        self.queue.put(p)
                except Exception:
                    pass

        try:
            with sd.RawInputStream(samplerate=self.sample_rate, blocksize = 8000, dtype='int16',
                                   channels=1, callback=callback):
                print("VOSK listener started (press Ctrl+C to stop)...")
                while self.running:
                    time.sleep(0.1)
        except Exception as e:
            print("VOSK sounddevice stream error:", e)
            self.running = False

    def get_text_nonblocking(self, timeout=0.1):
        try:
            return self.queue.get(timeout=timeout)
        except queue.Empty:
            return None

    def stop(self):
        self.running = False

# ---------------- Main orchestration: wakeword detection + command capture ----------------
def run_assistant():
    speak("AURA starting up. Initializing listeners...")

    vosk_thread = None
    if VOSK_AVAILABLE and os.path.exists(MODEL_PATH):
        vosk_thread = VoskListener(MODEL_PATH, SAMPLE_RATE)
        vosk_thread.start()
        use_vosk = True
        print("Using VOSK for continuous STT.")
    else:
        use_vosk = False
        print("VOSK not available or model missing. Falling back to Google Speech (slower, requires internet).")

    # For fallback: use speech_recognition in main thread
    sr_recognizer = sr.Recognizer()

    asleep = True
    last_command_time = time.time()

    try:
        while True:
            text = None
            if use_vosk:
                # fetch partial/full text if available
                text = vosk_thread.get_text_nonblocking(timeout=0.25)
            else:
                # blocking short listen for fallback
                with sr.Microphone() as source:
                    sr_recognizer.adjust_for_ambient_noise(source, duration=0.5)
                    try:
                        audio = sr_recognizer.listen(source, timeout=3, phrase_time_limit=5)
                        text = sr_recognizer.recognize_google(audio).lower()
                    except Exception:
                        text = ""

            if not text:
                # no speech detected; check sleep timeout
                if (time.time() - last_command_time) > 90 and not asleep:
                    speak("No activity detected. Going to sleep. Say Hey Aura to wake me.")
                    asleep = True
                continue

            print("Detected:", text)

            # wakeword detection
            if asleep and any(w in text for w in WAKE_WORDS):
                asleep = False
                speak("Yes, how can I help?")
                # now capture a complete command: gather successive results until silence
                command_parts = []
                command_start = time.time()
                silence_since = time.time()
                while True:
                    # prefer VOSK continuous queue
                    if use_vosk:
                        piece = vosk_thread.get_text_nonblocking(timeout=0.6)
                    else:
                        # use short blocking fallback
                        with sr.Microphone() as source:
                            sr_recognizer.adjust_for_ambient_noise(source, duration=0.3)
                            try:
                                audio = sr_recognizer.listen(source, timeout=3, phrase_time_limit=6)
                                piece = sr_recognizer.recognize_google(audio).lower()
                            except Exception:
                                piece = ""
                    if piece:
                        print("Cmd piece:", piece)
                        command_parts.append(piece)
                        silence_since = time.time()
                    else:
                        # no piece: check silence period
                        if time.time() - silence_since > SILENCE_TIMEOUT:
                            break
                        # else continue waiting
                command_text = " ".join(command_parts).strip()
                if not command_text:
                    speak("I didn't catch a command. Say Hey Aura to try again.")
                    last_command_time = time.time()
                    continue

                # send to LLM / plan generator (non-blocking call)
                speak("Thinking about what to do...")
                plan_obj = call_llm_for_plan(command_text)
                # plan_obj should be a dict with key "plan" containing list of actions
                plan = plan_obj.get("plan") if isinstance(plan_obj, dict) else None
                if not plan:
                    # plan not provided — see if LLM returned text to speak
                    if isinstance(plan_obj, dict) and 'text' in plan_obj:
                        speak(plan_obj['text'])
                        last_command_time = time.time()
                        continue
                    # fallback: simple parse
                    plan = fallback_command_to_plan(command_text).get("plan", [])
                # execute plan (may ask confirmations)
                execute_plan(plan, ask_confirm=True)
                last_command_time = time.time()
                speak("Done. Waiting for next command.")
                continue

            # If awake and hears non-wake command (optional)
            if not asleep:
                # handle direct short commands without explicit wakeword (optional)
                # e.g., user says "open youtube" after being awake
                if any(k in text for k in ["open", "search", "youtube", "google", "time", "close"]):
                    # interpret and execute
                    speak("Heard command, thinking...")
                    plan_obj = call_llm_for_plan(text)
                    plan = plan_obj.get("plan") if isinstance(plan_obj, dict) else None
                    if not plan:
                        plan = fallback_command_to_plan(text).get("plan", [])
                    execute_plan(plan, ask_confirm=True)
                    last_command_time = time.time()
                    speak("Done.")
                    continue

    except KeyboardInterrupt:
        print("Stopping assistant...")
    finally:
        if vosk_thread:
            vosk_thread.stop()
            time.sleep(0.3)
        speak("AURA shutting down. Goodbye.")

# FIX 2: The script entry point must be __name__ == "__main__" (double underscores)
if __name__ == "__main__":
    run_assistant()
# ============================================================
# LMS Automation Script
# Architecture: Core Automation -> HTTP Client -> Browser Driver
# ============================================================

import os
import time
import re
import json
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import base64
from urllib.parse import urlparse
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

# ─── CONFIGURATION ───────────────────────────────────────────
from config_loader import load_config
CONFIG = load_config()
USERNAME = CONFIG.get("USERNAME")
PASSWORD = CONFIG.get("PASSWORD")
GEMINI_API_KEY = CONFIG.get("GEMINI_API_KEY")
BROWSER_EXECUTABLE = CONFIG.get("BROWSER_EXECUTABLE_PATH")
# ─────────────────────────────────────────────────────────────

# ─── PERFORMANCE CONFIGURATION ──────────────────────────────
PERFORMANCE = {
    "headless": True,            # Headless Chromium execution (max speed)
    "block_fonts": True,         # Block web fonts (.woff, .woff2, .ttf)
    "block_media": True,         # Block video/audio elements
    "block_tracking": True,      # Block verified analytics endpoints
    "block_css": False,          # Keep CSS enabled initially for visual/layout stability
    "persistent_auth": True,     # Save/restore login state to auth.json
    "debug_network": False,      # Log HTTP methods, URLs, status, latency
    "benchmark": True,           # Profile and display timing metrics report
}

AUTH_STATE_FILE = "auth.json"
MIN_PASSING_PERCENTAGE = 85.0  # Skip quiz if previously finished with >= 85% mark

# Known analytics and tracker domains (explicit allow/blocklist)
VERIFIED_TRACKERS = [
    "google-analytics.com",
    "googletagmanager.com",
    "clarity.ms",
    "hotjar.com",
    "doubleclick.net",
    "facebook.net",
    "connect.facebook.net",
    "analytics.tiktok.com",
    "mc.yandex.ru"
]

# ─── HTTP CONNECTION POOL (Gemini API) ───────────────────────
http_session = requests.Session()
retries = Retry(
    total=3,
    backoff_factor=0.3,
    status_forcelist=[500, 502, 503, 504],
    raise_on_status=False
)
adapter = HTTPAdapter(pool_connections=10, pool_maxsize=10, max_retries=retries)
http_session.mount("https://", adapter)
http_session.mount("http://", adapter)
http_session.headers.update({
    "Content-Type": "application/json",
    "Connection": "keep-alive"
})

# ─── BENCHMARK & TELEMETRY LAYER ─────────────────────────────
class BenchmarkTracker:
    def __init__(self):
        self.metrics = {}
        self.start_times = {}
        self.total_start = time.perf_counter()

    def start(self, name):
        self.start_times[name] = time.perf_counter()

    def stop(self, name):
        if name in self.start_times:
            elapsed = time.perf_counter() - self.start_times.pop(name)
            self.metrics.setdefault(name, []).append(elapsed)
            return elapsed
        return 0.0

    def print_summary(self):
        total_time = time.perf_counter() - self.total_start
        print("\n" + "═"*60)
        print(" PERFORMANCE BENCHMARK REPORT")
        print("═"*60)
        print(f" {'Stage / Operation':<35} │ {'Count':<6} │ {'Total':<10} │ {'Avg':<8}")
        print("─"*60)
        for stage, durations in self.metrics.items():
            count = len(durations)
            total = sum(durations)
            avg = total / count if count else 0.0
            print(f" {stage:<35} │ {count:<6} │ {total:>8.2f}s │ {avg:>6.2f}s")
        print("─"*60)
        print(f" {'Total Execution Time':<35} │ {'1':<6} │ {total_time:>8.2f}s │ {total_time:>6.2f}s")
        print("═"*60 + "\n")

benchmark = BenchmarkTracker()

# ─── RESOURCE ROUTING & INTERCEPTION ─────────────────────────
def setup_resource_routing(context):
    def route_handler(route):
        request = route.request
        res_type = request.resource_type
        req_url = request.url

        # Check font blocking
        if PERFORMANCE["block_fonts"] and (res_type == "font" or any(ext in req_url for ext in [".woff", ".woff2", ".ttf", ".otf"])):
            if PERFORMANCE["debug_network"]:
                print(f" [BLOCKED FONT] {req_url}")
            route.abort()
            return

        # Check media blocking
        if PERFORMANCE["block_media"] and res_type in ["media", "eventsource", "websocket"]:
            if PERFORMANCE["debug_network"]:
                print(f" [BLOCKED MEDIA] {req_url}")
            route.abort()
            return

        # Check CSS blocking (if explicitly enabled)
        if PERFORMANCE["block_css"] and res_type == "stylesheet":
            if PERFORMANCE["debug_network"]:
                print(f" [BLOCKED CSS] {req_url}")
            route.abort()
            return

        # Check verified analytics/tracker blocking
        if PERFORMANCE["block_tracking"] and any(tracker in req_url for tracker in VERIFIED_TRACKERS):
            if PERFORMANCE["debug_network"]:
                print(f" [BLOCKED TRACKER] {req_url}")
            route.abort()
            return

        # Log active requests if in network debug mode
        if PERFORMANCE["debug_network"]:
            t0 = time.perf_counter()
            response = route.fetch()
            elapsed_ms = (time.perf_counter() - t0) * 1000
            print(f" [{request.method}] {res_type:<10} {request.url[:60]} ({response.status}) - {elapsed_ms:.1f}ms")
            route.fulfill(response=response)
            return

        route.continue_()

    context.route("**/*", route_handler)

# ─── QUIZ AUTOMATION LAYER: PROMPTS & EXTRACTION ─────────────
QUIZ_SYSTEM_PROMPT = (
    "You are a highly intelligent expert professor. Your goal is to get 100% on this quiz. "
    "First, think step-by-step and carefully evaluate each option. Explain why each option is correct or incorrect. "
    "Then, provide the final correct option number at the very end of your response in this exact format: 'FINAL_ANSWER: X' "
    "(where X is the correct option number). "
    "Example: if the answer is option 2, end your response exactly with: FINAL_ANSWER: 2"
)

def get_images_from_question(page):
    images_base64 = []
    for sel in [".qtext", ".questiontext", ".formulation"]:
        el = page.query_selector(sel)
        if el:
            imgs = el.query_selector_all("img")
            for img in imgs:
                try:
                    img_bytes = img.screenshot(type="jpeg")
                    b64 = base64.b64encode(img_bytes).decode("utf-8")
                    images_base64.append(b64)
                except Exception as e:
                    print(f" Could not capture image: {e}")
            break
    return images_base64

def copy_quiz_content(page):
    question = ""
    for sel in [".qtext", ".questiontext", ".formulation"]:
        el = page.query_selector(sel)
        if el:
            question = el.inner_text().strip()
            break

    if not question:
        return None, [], []

    images_base64 = get_images_from_question(page)

    options = []
    for sel in [
        ".answer div[data-region='answer-label']",
        ".answer .flex-wrap label", 
        ".answer label", 
        ".r0 label, .r1 label"
    ]:
        els = page.query_selector_all(sel)
        if els:
            options = [f"{i+1}. {el.inner_text().strip()}" for i, el in enumerate(els)]
            break

    return question, options, images_base64

def send_gemini_request(payload, api_type="Normal Quiz", max_retries=10):
    """
    Executes a POST request to Gemini API.
    If a 429 Too Many Requests (rate limit) is encountered, safely pauses
    with a live countdown timer until the rate limit resets, then resumes.
    """
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-3.5-flash-lite:generateContent?key={GEMINI_API_KEY}"
    
    for attempt in range(1, max_retries + 1):
        try:
            r = http_session.post(url, json=payload, timeout=30)
            
            # Check specifically for rate limiting (429 Too Many Requests)
            if r.status_code == 429:
                wait_time = 60  # default 60s cooldown
                try:
                    retry_after = r.headers.get("Retry-After")
                    if retry_after and retry_after.isdigit():
                        wait_time = max(int(retry_after), 60)
                    else:
                        err_json = r.json()
                        err_msg = err_json.get("error", {}).get("message", "")
                        match = re.search(r'retry in\s*([\d.]+)\s*s', err_msg, re.IGNORECASE)
                        if match:
                            wait_time = max(int(float(match.group(1))) + 5, 30)
                except Exception:
                    pass

                # Progressive backoff if repeated 429s occur
                if attempt > 1:
                    wait_time = max(wait_time, 60 * attempt)

                print(f"\n [429 Too Many Requests] Gemini API rate limit reached (Attempt {attempt}/{max_retries}).")
                print(f"  Pausing execution for {wait_time}s until requests can resume...")
                
                for remaining in range(wait_time, 0, -5):
                    print(f" Waiting for rate limit to reset... {remaining:02d}s remaining", end="\r", flush=True)
                    time.sleep(min(5, remaining))
                print("\n Rate limit reset! Resuming Gemini API requests now...\n")
                continue

            r.raise_for_status()
            res_data = r.json()
            reply = res_data["candidates"][0]["content"]["parts"][0]["text"].strip()
            return reply

        except requests.exceptions.RequestException as e:
            # Check if 429 was raised as HTTPError
            if hasattr(e, "response") and e.response is not None and e.response.status_code == 429:
                wait_time = 60 * attempt
                print(f"\n [429 Too Many Requests] Gemini API rate limit reached (Attempt {attempt}/{max_retries}).")
                print(f"  Pausing execution for {wait_time}s until requests can resume...")
                for remaining in range(wait_time, 0, -5):
                    print(f" Waiting for rate limit to reset... {remaining:02d}s remaining", end="\r", flush=True)
                    time.sleep(min(5, remaining))
                print("\n Rate limit reset! Resuming Gemini API requests now...\n")
                continue
            else:
                print(f" Error asking Gemini API ({api_type}): {e}")
                if attempt < max_retries:
                    time.sleep(3)
                    continue
                return None
        except Exception as e:
            print(f" Unexpected error asking Gemini API ({api_type}): {e}")
            return None

    print(f" Gemini API rate limit could not be resolved after {max_retries} attempts.")
    return None

def ask_gemini_api(question, options, images_base64=None):
    if PERFORMANCE["benchmark"]:
        benchmark.start("Gemini API (MCQ)")

    if options:
        options_block = "\n".join(options)
        message = (
            f"{QUIZ_SYSTEM_PROMPT}\n\n"
            f"Question: {question}\n\n"
            f"Options:\n{options_block}\n\n"
            f"Evaluate step-by-step, then reply with FINAL_ANSWER: X"
        )
    else:
        message = (
            f"You are an expert professor. Your goal is to get 100% on this quiz.\n"
            f"Question: {question}\n\n"
            f"Provide a very short, direct answer. No explanation, just the answer text."
        )

    p = {
        "contents": [{"parts": [{"text": message}]}],
        "generationConfig": {"temperature": 0.0}
    }
    if images_base64:
        for b64 in images_base64:
            p["contents"][0]["parts"].append({
                "inlineData": {
                    "mimeType": "image/jpeg",
                    "data": b64
                }
            })

    print(" Asking Gemini API (Normal Quiz)...")
    reply = send_gemini_request(p, api_type="Normal Quiz")
    if reply:
        print(f" Gemini API said: '{reply}'")
        
    if PERFORMANCE["benchmark"]:
        benchmark.stop("Gemini API (MCQ)")
    return reply

def click_answer(page, reply, options):
    if options:
        match = re.search(r'FINAL_ANSWER:\s*(\d+)', reply, re.IGNORECASE)
        if match:
            index = int(match.group(1)) - 1
        else:
            numbers = re.findall(r'\b(\d+)\b', reply)
            if numbers:
                index = int(numbers[-1]) - 1
            else:
                print(f" Could not find a number in reply: '{reply}'")
                return

        radios = page.query_selector_all(".answer input[type='radio'], .answer input[type='checkbox']")
        if 0 <= index < len(radios):
            radios[index].click()
            print(f" Clicked option {index + 1}: {options[index]}")
        else:
            print(f" Option {index+1} doesn't exist. Only {len(radios)} options found.")
    else:
        inp = page.query_selector(".answer input[type='text'], .answer textarea")
        if inp:
            inp.fill(reply.strip())
            print(f" Typed: {reply.strip()}")

# ─── CODING QUIZ METHODS ─────────────────────────────────────
def get_coding_question(page):
    question = ""
    for sel in [".qtext", ".questiontext", ".formulation"]:
        el = page.query_selector(sel)
        if el:
            question = el.inner_text().strip()
            break

    images_base64 = get_images_from_question(page) if question else []

    existing_code = ""
    try:
        existing_code = page.evaluate("""() => {
            try {
                let el = document.querySelector('.ace_editor');
                if (el && el.env && el.env.editor) return el.env.editor.getValue();
                if (el && typeof ace !== 'undefined') {
                    let editor = ace.edit(el);
                    return editor.getValue();
                }
            } catch(e) {}
            let textarea = document.querySelector('textarea.coderunner-answer');
            if (textarea) return textarea.value;
            return '';
        }""")
    except Exception:
        pass

    return question, existing_code, images_base64

def ask_gemini_coding(question, existing_code, error_feedback=None, previous_code=None, images_base64=None):
    if PERFORMANCE["benchmark"]:
        benchmark.start("Gemini API (Coding)")

    system_prompt = (
        "You are an expert programmer solving a coding assessment.\n"
        "CRITICAL RULES:\n"
        "1. Must code in whatever coding language is shown in the page or question (e.g., SQL, C, Python, Java).\n"
        "2. NO COMMENTS in the code.\n"
        "3. Indentation MUST be PERFECT (use exactly 4 spaces for each indentation level). Never flatten the code.\n"
        "4. Internal spacing (e.g., 'val=0' instead of 'val = 0') can be irregular to look hand-coded.\n"
        "5. Variable names should ideally be 2 to 3 characters long with a descriptive type. However, basic standard single-letter variables like 'i', 'j' in loops, or 'a', 'b' for simple inputs are perfectly permitted.\n"
        "6. Do not leave any blank/empty lines between blocks of code.\n"
        "7. Output ONLY the raw code. Do NOT wrap in markdown blocks like ```java. Just the code."
    )
    
    message = f"Problem Description:\n{question}\n\n"
    if existing_code:
        message += f"Existing/Template Code:\n{existing_code}\n\n"
        
    if error_feedback:
        message += f"Your previous code:\n{previous_code}\n\n"
        message += f"Failed with this error/feedback:\n{error_feedback}\n\n"
        message += "Please fix the code to pass the tests. Output ONLY the raw code without markdown."
    else:
        message += "Please provide the complete working code. Output ONLY the raw code without markdown."

    p = {
        "contents": [{"parts": [{"text": f"{system_prompt}\n\n{message}"}]}],
        "generationConfig": {"temperature": 0.2}
    }
    if images_base64:
        for b64 in images_base64:
            p["contents"][0]["parts"].append({
                "inlineData": {
                    "mimeType": "image/jpeg",
                    "data": b64
                }
            })

    print(" Asking Gemini API for Code...")
    reply = send_gemini_request(p, api_type="Coding Quiz")
    if reply:
        if reply.startswith("```"):
            reply = reply.split("\n", 1)[-1]
            if reply.endswith("```"):
                reply = reply.rsplit("```", 1)[0]
            reply = reply.strip()
            
    if PERFORMANCE["benchmark"]:
        benchmark.stop("Gemini API (Coding)")
    return reply

def paste_code_in_ace(page, code):
    success = page.evaluate(f"""(code) => {{
        try {{
            let el = document.querySelector('.ace_editor');
            if (el && el.env && el.env.editor) {{
                el.env.editor.setValue(code, -1);
                return true;
            }}
            if (el && typeof ace !== 'undefined') {{
                let editor = ace.edit(el);
                editor.setValue(code, -1);
                return true;
            }}
        }} catch(e) {{}}
        
        let textarea = document.querySelector('textarea.coderunner-answer');
        if (textarea) {{
            textarea.value = code;
            textarea.dispatchEvent(new Event('input', {{ bubbles: true }}));
            textarea.dispatchEvent(new Event('change', {{ bubbles: true }}));
            return true;
        }}
        return false;
    }}""", code)
    
    if not success:
        print("⚠️  UI fallback for ACE editor...")
        try:
            page.click(".ace_editor, .ace_text-input, textarea.coderunner-answer", timeout=3000)
            page.keyboard.press("Control+A")
            page.keyboard.press("Backspace")
            page.evaluate("async (code) => await navigator.clipboard.writeText(code)", code)
            page.keyboard.press("Control+V")
        except Exception as e:
            print(f" Failed to paste code: {e}")

def check_result(page):
    check_btn = page.query_selector("input[value='Check'], button:has-text('Check')")
    if not check_btn:
        print("⚠️  No Check button found!")
        return True, "No check button"
        
    check_btn.click()
    
    # Wait for AJAX response to complete and DOM to update
    page.wait_for_timeout(3000)
    
    error_text = ""
    for sel in [".coderunner-test-results", ".feedback", ".alert-danger", ".alert-warning"]:
        el = page.query_selector(sel)
        if el and el.inner_text().strip():
            error_text += el.inner_text().strip() + "\n"
            
    # Check for success indicators
    content = page.content()
    success_indicators = [
        "Passed all tests!",
        "Marks for this submission: 1.00/1.00",
        "Correct",
        "1.00/1.00"
    ]
    
    # Check if a success alert exists
    if page.query_selector(".alert-success"):
        return True, "Passed"
        
    for indicator in success_indicators:
        if indicator in error_text or indicator in content:
            return True, "Passed"
            
    if "Try again." in error_text or "failed" in error_text.lower() or "syntax error" in error_text.lower():
        return False, error_text
        
    if error_text:
        return False, error_text
        
    return False, "Code failed some tests or had syntax errors. Please review the logic."

# ─── NAVIGATION (CONDITION-BASED WAITS) ──────────────────────
def go_next(page):
    finish_selectors = [
        "input#mod_quiz-next-nav",
        "input[value='Finish attempt ...']", 
        "button:has-text('Finish attempt ...')", 
        "input[name='finishattempt']",
        "a:has-text('Finish attempt')",
        "a[href*='summary.php']",
        "a.endtestlink"
    ]
    finish_btn = page.query_selector(", ".join(finish_selectors))
    
    if finish_btn:
        finish_btn.click(force=True)
        try:
            page.locator("button:has-text('Submit all and finish'), input[value='Submit all and finish']").first.wait_for(
                state="visible",
                timeout=10000
            )
        except Exception:
            page.wait_for_load_state("domcontentloaded")
        print(" Finish attempt clicked")
        return "finish"

    next_selectors = [
        "input[name='next']", 
        "input[value='Next page']", 
        "button:has-text('Next page')",
        "a:has-text('Next page')"
    ]
    next_btn = page.query_selector(", ".join(next_selectors))
    
    if next_btn:
        next_btn.click(force=True)
        # Condition-based wait: wait for next question or summary buttons to appear
        try:
            page.locator(".qtext, .questiontext, .formulation, .ace_editor, textarea.coderunner-answer, button:has-text('Submit all and finish')").first.wait_for(
                state="attached",
                timeout=10000
            )
        except Exception:
            page.wait_for_load_state("domcontentloaded")
        print("  Next question\n")
        return "next"

    print(" No navigation button found.")
    return "none"

def handle_final_submission(page):
    """
    Submits the quiz from the summary page and confirms any modal dialogues.
    """
    print("✅  All questions passed! Submitting...")
    try:
        # 1. Locate and click the initial summary page submit button
        submit_btn = page.locator("button:has-text('Submit all and finish'), input[value='Submit all and finish']").first
        submit_btn.wait_for(state="visible", timeout=15000)
        submit_btn.click()
        
        # 2. Wait briefly for confirmation modal dialog to render
        time.sleep(1)
        
        # 3. Target confirmation button specifically in the modal
        modal_confirm_selectors = [
            ".modal.show button:has-text('Submit all and finish')",
            ".modal.show input[value='Submit all and finish']",
            ".modal.show button[data-action='save']",
            ".modal-dialog button:has-text('Submit all and finish')",
            ".modal-dialog input[value='Submit all and finish']",
            ".modal-dialog button.btn-primary",
            ".modal-footer button.btn-primary",
            ".moodle-dialogue-confirm input[type='button']",
            "div[role='dialog'] button:has-text('Submit all and finish')",
            "button:has-text('Submit all and finish')",
            "input[value='Submit all and finish']"
        ]
        
        for sel in modal_confirm_selectors:
            try:
                confirm_btn = page.locator(sel).last
                if confirm_btn.is_visible(timeout=1500):
                    confirm_btn.click()
                    print("✅  Clicked confirmation button in modal!")
                    break
            except Exception:
                continue

        # 4. Wait for submission / redirect to review page
        try:
            page.wait_for_load_state("domcontentloaded", timeout=10000)
        except Exception:
            pass

        print("✅ \n Quiz submitted successfully (confirmed)!")
        return True
    except Exception as e:
        print(f"\n Error during final submission: {e}")
        return False

# ─── AUTHENTICATION LAYER ────────────────────────────────────
def ensure_authenticated(context, page, moodle_url):
    """
    Checks if session is valid; if not, logs in and updates persistent auth.
    """
    if PERFORMANCE["benchmark"]:
        benchmark.start("Authentication / Session")

    # Quick session check
    page.goto(moodle_url + "/my/", wait_until="domcontentloaded")
    
    # Check if already authenticated (look for user profile or logout link)
    is_logged_in = False
    try:
        user_menu = page.locator(".usermenu, a[href*='logout.php'], .userbutton").first
        if user_menu.is_visible(timeout=3000):
            is_logged_in = True
            print(" Authenticated session restored from cache!")
    except Exception:
        pass

    if not is_logged_in:
        print(f" Logging into Moodle at {moodle_url}...")
        page.goto(moodle_url + "/login/index.php", wait_until="domcontentloaded")
        page.locator("input#username, input[name='username']:not([type='hidden'])").first.fill(USERNAME)
        page.locator("input#password, input[name='password']:not([type='hidden'])").first.fill(PASSWORD)
        page.locator("button#loginbtn, input[value='Log In'], input[value='Log in'], input[type='Log In'], button:has-text('Log In'), button:has-text('Log in'), button[type='submit'], input[type='submit']").first.click()
        
        # Wait for redirect to finish
        try:
            page.locator(".usermenu, a[href*='logout.php'], .userbutton").first.wait_for(state="visible", timeout=15000)
            print("✅  Logged in successfully!")
            if PERFORMANCE["persistent_auth"]:
                context.storage_state(path=AUTH_STATE_FILE)
                print(f" Saved session state to {AUTH_STATE_FILE}")
        except Exception as e:
            print(f" Warning during login check: {e}")

    if PERFORMANCE["benchmark"]:
        benchmark.stop("Authentication / Session")

# ─── MAIN ─────────────────────────────────────────────────────
def main():
    course_url = input("Please enter the Course URL: ").strip()
    if not course_url:
        print("❌  No course URL provided. Exiting.")
        return

    with sync_playwright() as p:
        user_data_dir = ""
        
        args = ["--start-maximized"]
        if PERFORMANCE["headless"]:
            args.append("--headless=new")

        launch_args = {
            "user_data_dir": user_data_dir,
            "headless": PERFORMANCE["headless"], 
            "args": args,
            "no_viewport": True,
            "accept_downloads": True,
            "permissions": ["clipboard-read", "clipboard-write"]
        }
        
        if BROWSER_EXECUTABLE and os.path.exists(BROWSER_EXECUTABLE):
            launch_args["executable_path"] = BROWSER_EXECUTABLE
            
        context = p.chromium.launch_persistent_context(**launch_args)

        # Restore saved storage state (cookies) if persistent_auth is enabled
        if PERFORMANCE["persistent_auth"] and os.path.exists(AUTH_STATE_FILE):
            try:
                with open(AUTH_STATE_FILE, "r", encoding="utf-8") as f:
                    state_data = json.load(f)
                    if "cookies" in state_data and state_data["cookies"]:
                        context.add_cookies(state_data["cookies"])
                        print(f" Loaded {len(state_data['cookies'])} session cookie(s) from {AUTH_STATE_FILE}")
            except Exception as e:
                print(f" Failed to restore auth state from {AUTH_STATE_FILE}: {e}")

        # Setup performance route interception
        setup_resource_routing(context)

        parsed_url = urlparse(course_url)
        moodle_url = f"{parsed_url.scheme}://{parsed_url.netloc}"

        quiz_page = context.pages[0] if context.pages else context.new_page()
        quiz_page.set_default_timeout(60000)
        quiz_page.set_default_navigation_timeout(60000)
        quiz_page.on("dialog", lambda dialog: dialog.accept())

        # Ensure authentication state
        ensure_authenticated(context, quiz_page, moodle_url)

        # Scrape quizzes from course page
        if PERFORMANCE["benchmark"]:
            benchmark.start("Scrape Course Quizzes")

        print(f"\n Navigating to Course Page: {course_url}")
        quiz_page.goto(course_url, wait_until="domcontentloaded")
        
        # Wait for activity content to appear
        try:
            quiz_page.locator("a[href*='/mod/quiz/view.php']").first.wait_for(state="attached", timeout=15000)
        except Exception:
            pass

        if "/mod/quiz/view.php" in course_url or "/mod/quiz/attempt.php" in course_url:
            print(" Direct quiz URL detected!")
            quiz_urls = [course_url]
        else:
            print(" Searching for quizzes...")
            quiz_urls = quiz_page.evaluate('''() => {
                const keywords = ['quiz', 'practice quiz', 'mcq', 'skill assessments', 'viva'];
                const links = Array.from(document.querySelectorAll('a[href*="/mod/quiz/view.php"]'));
                const matched = [];
                for (const link of links) {
                    const text = link.innerText.toLowerCase();
                    if (keywords.some(k => text.includes(k))) {
                        matched.push(link.href);
                    }
                }
                return matched;
            }''')
            
            quiz_urls = list(dict.fromkeys(quiz_urls))
        
        if PERFORMANCE["benchmark"]:
            benchmark.stop("Scrape Course Quizzes")

        if not quiz_urls:
            print("❌  No quizzes found matching the criteria.")
            context.close()
            return
            
        print(f" Found {len(quiz_urls)} quizzes to attempt!")
        for url in quiz_urls:
            print(f"  - {url}")

        skipped_seb_quizzes = []

        for current_quiz_url in quiz_urls:
            if PERFORMANCE["benchmark"]:
                benchmark.start(f"Quiz Execution ({current_quiz_url.split('id=')[-1]})")

            print(f"\n Navigating to Quiz: {current_quiz_url}")
            quiz_page.goto(current_quiz_url, wait_until="domcontentloaded")
            
            # Dynamic SEB check
            if "This quiz has been configured so that students may only attempt it using the Safe Exam Browser." in quiz_page.content():
                print("⚠️  Safe Exam Browser required. Skipping this quiz.")
                skipped_seb_quizzes.append(current_quiz_url)
                if PERFORMANCE["benchmark"]:
                    benchmark.stop(f"Quiz Execution ({current_quiz_url.split('id=')[-1]})")
                continue

            # Check if quiz has a finished attempt with score >= MIN_PASSING_PERCENTAGE or if no more attempts are allowed
            status = quiz_page.evaluate(r'''() => {
                let maxPct = null;
                let details = [];
                const bodyText = document.body.innerText;

                const noMoreAttempts = /no more attempts (?:are )?allowed/i.test(bodyText);

                // 1. Check text for Highest grade / Final grade / Overall grade
                const gradePatterns = [
                    /(?:highest grade|final grade|overall grade|grade for this quiz)[:\s]+([\d.]+)\s*(?:\/|out of)\s*([\d.]+)/gi,
                    /(?:highest grade|final grade|overall grade)[:\s]+([\d.]+)%/gi,
                    /your final grade for this quiz is[:\s]+([\d.]+)\s*(?:\/|out of)\s*([\d.]+)/gi
                ];
                
                for (const pat of gradePatterns) {
                    let match;
                    while ((match = pat.exec(bodyText)) !== null) {
                        if (match[2]) {
                            const s = parseFloat(match[1]);
                            const t = parseFloat(match[2]);
                            if (t > 0) {
                                const p = (s / t) * 100;
                                if (maxPct === null || p > maxPct) maxPct = p;
                                details.push(`${s}/${t} (${p.toFixed(1)}%)`);
                            }
                        } else if (match[1]) {
                            const p = parseFloat(match[1]);
                            if (maxPct === null || p > maxPct) maxPct = p;
                            details.push(`${p.toFixed(1)}%`);
                        }
                    }
                }

                // 2. Check the attempt summary table with column headers
                const tables = document.querySelectorAll('table.quizattemptsummary, table.generaltable, .quizattemptsummary');
                for (const table of tables) {
                    // Extract column totals from header cells (e.g. "Marks / 30.00", "Grade / 10.00")
                    const ths = Array.from(table.querySelectorAll('thead th, tr:first-child th'));
                    const colTotals = {};
                    ths.forEach((th, idx) => {
                        const m = th.innerText.match(/(?:marks|grade)[^\d]*([\d.]+)/i);
                        if (m) {
                            colTotals[idx] = parseFloat(m[1]);
                        }
                    });

                    const rows = Array.from(table.querySelectorAll('tbody tr'));
                    for (const row of rows) {
                        const cells = Array.from(row.querySelectorAll('td'));
                        const rowText = row.innerText;
                        if (/finished|submitted/i.test(rowText)) {
                            // Check cells against mapped header column totals
                            cells.forEach((td, idx) => {
                                const total = colTotals[idx];
                                const val = parseFloat(td.innerText.trim());
                                if (total && !isNaN(val) && val <= total) {
                                    const p = (val / total) * 100;
                                    if (maxPct === null || p > maxPct) maxPct = p;
                                    details.push(`Col ${idx}: ${val}/${total} (${p.toFixed(1)}%)`);
                                }
                            });

                            // Also check inline fractions in row text
                            const fracMatches = Array.from(rowText.matchAll(/([\d.]+)\s*(?:\/|out of)\s*([\d.]+)/gi));
                            for (const m of fracMatches) {
                                const s = parseFloat(m[1]);
                                const t = parseFloat(m[2]);
                                if (t > 0 && s <= t) {
                                    const p = (s / t) * 100;
                                    if (maxPct === null || p > maxPct) maxPct = p;
                                    details.push(`Row: ${s}/${t} (${p.toFixed(1)}%)`);
                                }
                            }
                            const pctMatches = Array.from(rowText.matchAll(/([\d.]+)%/g));
                            for (const m of pctMatches) {
                                const p = parseFloat(m[1]);
                                if (maxPct === null || p > maxPct) maxPct = p;
                                details.push(`Row: ${p.toFixed(1)}%`);
                            }
                        }
                    }
                }

                return {
                    has_finished_attempt: maxPct !== null,
                    best_percentage: maxPct !== null ? maxPct : 0,
                    no_more_attempts: noMoreAttempts,
                    details: details
                };
            }''')

            if status.get("has_finished_attempt"):
                best_pct = status.get("best_percentage", 0)
                if best_pct >= MIN_PASSING_PERCENTAGE:
                    print(f" Quiz already finished with score {best_pct:.1f}% (>= {MIN_PASSING_PERCENTAGE}%). Skipping!")
                    if PERFORMANCE["benchmark"]:
                        benchmark.stop(f"Quiz Execution ({current_quiz_url.split('id=')[-1]})")
                    continue
                elif status.get("no_more_attempts"):
                    print(f" No more attempts allowed for this quiz (Best score: {best_pct:.1f}%). Skipping!")
                    if PERFORMANCE["benchmark"]:
                        benchmark.stop(f"Quiz Execution ({current_quiz_url.split('id=')[-1]})")
                    continue
                else:
                    print(f" Previous attempt score: {best_pct:.1f}% (< {MIN_PASSING_PERCENTAGE}%). Re-attempting...")
            elif status.get("no_more_attempts"):
                print("❌  No more attempts allowed for this quiz. Skipping!")
                if PERFORMANCE["benchmark"]:
                    benchmark.stop(f"Quiz Execution ({current_quiz_url.split('id=')[-1]})")
                continue

            attempt_buttons = [
                "button:has-text('Attempt quiz')", "button:has-text('Start attempt')",
                "button:has-text('Continue your attempt')", "button:has-text('Continue the last attempt')",
                "button:has-text('Re-attempt quiz')", "input[value='Attempt quiz now']",
                "input[value='Continue the last attempt']", "input[value='Re-attempt quiz']",
                "input[value='Continue your attempt']", "a:has-text('Attempt quiz')",
                "a:has-text('Continue your attempt')"
            ]
            for sel in attempt_buttons:
                try:
                    loc = quiz_page.locator(sel).first
                    if loc.is_visible(timeout=1500):
                        loc.click()
                        print("✅   Clicked main attempt button!")
                        
                        # Handle confirmation modal if present
                        modal_selectors = [
                            "button#id_submitbutton",
                            "button:has-text('Start attempt')",
                            "input[value='Start attempt']"
                        ]
                        for m_sel in modal_selectors:
                            try:
                                m_loc = quiz_page.locator(m_sel).last
                                if m_loc.is_visible(timeout=1000):
                                    m_loc.click()
                                    print("✅   Clicked modal 'Start attempt'!")
                                    break
                            except Exception:
                                pass
                                
                        break
                except Exception:
                    pass

            # Wait for quiz question formulation or ACE editor to attach
            try:
                quiz_page.locator(".qtext, .questiontext, .formulation, .ace_editor, textarea.coderunner-answer, button:has-text('Submit all and finish')").first.wait_for(
                    state="attached", 
                    timeout=15000
                )
            except Exception:
                pass

            print(" Solving quiz (Unified)... \n")
            q_num = 1
            all_passed = True

            while True:
                print(f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
                print(f" Question #{q_num}")

                quiz_page.bring_to_front()
                
                # Check if ACE editor needs reload
                if "perhaps reload" in quiz_page.content().lower():
                    print(" Page shows 'perhaps reload?'. Reloading to fix ACE editor...")
                    quiz_page.reload(wait_until="domcontentloaded")
                    try:
                        nav_btn = quiz_page.locator(f"a.qnbutton:text-is('{q_num}'), a.qn_btn:text-is('{q_num}')").first
                        if nav_btn.count() > 0 and nav_btn.is_visible(timeout=1000):
                            print(f" Forcing navigation back to Question {q_num}...")
                            nav_btn.click()
                    except Exception as e:
                        print(f" Could not force navigation back to Question {q_num}: {e}")

                # Determine if it's a coding question
                is_coding = quiz_page.query_selector(".ace_editor") is not None or quiz_page.query_selector("textarea.coderunner-answer") is not None
                
                # Check for question content
                question = None
                options = None
                existing_code = None
                images = []
                
                if is_coding:
                    question, existing_code, images = get_coding_question(quiz_page)
                else:
                    question, options, images = copy_quiz_content(quiz_page)

                if not question:
                    submit_btns = quiz_page.locator("button:has-text('Submit all and finish'), input[value='Submit all and finish']")
                    if submit_btns.count() > 0:
                        print(" Reached summary page!")
                        if all_passed:
                            handle_final_submission(quiz_page)
                        else:
                            print("❌  At least 1 question failed. NOT submitting. Browser remains open for you to fix.")
                            print("❗ 1 failed")
                    else:
                        print("No question found. Stopping.")
                    break

                if is_coding:
                    print(f" Coding Problem: {question[:80]}...")
                    attempt = 1
                    passed = False
                    error_text = None
                    previous_code = None

                    while attempt <= 5:
                        print(f"Attempt {attempt}/5...")
                        reply = ask_gemini_coding(question, existing_code, error_text, previous_code, images)

                        if not reply:
                            print(" No reply. Skipping attempt.")
                            attempt += 1
                            continue

                        print(" Pasting code...")
                        paste_code_in_ace(quiz_page, reply)
                        previous_code = reply

                        print(" Checking code...")
                        is_correct, feedback = check_result(quiz_page)

                        if is_correct:
                            print("✅  Code passed!")
                            passed = True
                            break
                        else:
                            print(f" Code failed. Feedback:\n{feedback[:200]}...")
                            error_text = feedback
                            attempt += 1

                    if not passed:
                        print(f" Failed to solve question {q_num} after 5 attempts. Moving to next question.")
                        print("❗ 1 failed")
                        all_passed = False
                else:
                    print(f" Quiz Problem: {question[:80]}...")
                    print(f" {options if options else 'Text input question'}")
                    
                    reply = ask_gemini_api(question, options, images)
                    if not reply:
                        print(" No reply. Skipping question.")
                    else:
                        click_answer(quiz_page, reply, options)

                result = go_next(quiz_page)

                if result == "finish":
                    print(" Summary page reached...")
                    if all_passed:
                        handle_final_submission(quiz_page)
                    else:
                        print("❌  At least 1 question failed. NOT submitting.")
                        print("❗ 1 failed")
                    break

                elif result == "none":
                    print("❌  Can't navigate. Stopping.")
                    if not all_passed:
                        print("❌  At least 1 question failed. NOT submitting.")
                    break

                q_num += 1

            if PERFORMANCE["benchmark"]:
                benchmark.stop(f"Quiz Execution ({current_quiz_url.split('id=')[-1]})")

        print("🎉  All quizzes finished!")
        
        if skipped_seb_quizzes:
            print("\n⚠️  The following SEB quizzes were skipped:")
            for skipped_url in skipped_seb_quizzes:
                print(f"  - {skipped_url}")
        
        # Display performance benchmark telemetry
        if PERFORMANCE["benchmark"]:
            benchmark.print_summary()

        if all_passed:
            context.close()
        else:
            print("❌  Questions failed.")
            if not PERFORMANCE["headless"]:
                input("Press Enter to close browser and exit...")
            context.close()

if __name__ == "__main__":
    main()

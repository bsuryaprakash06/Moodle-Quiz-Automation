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
GEMINI_MODEL = CONFIG.get("GEMINI_MODEL", "gemini-3.8-flash")
GEMINI_THINKING_LEVEL = (CONFIG.get("GEMINI_THINKING_LEVEL") or "HIGH").upper()
_env_fallbacks = [m.strip() for m in (CONFIG.get("GEMINI_FALLBACK_MODELS") or "").split(",") if m.strip()]
GEMINI_FALLBACK_MODELS = list(dict.fromkeys([
    GEMINI_MODEL,
    *_env_fallbacks,
    "gemini-3.6-flash",
    "gemini-3-flash-preview",
    "gemini-3.5-flash-lite",
    "gemini-4-carbon",
    "gemini-4-argon",
]))
_MODEL_COOLDOWN_UNTIL = {}
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
    total=1,
    backoff_factor=0.3,
    status_forcelist=[500],
    raise_on_status=False
)
adapter = HTTPAdapter(pool_connections=10, pool_maxsize=10, max_retries=retries)
http_session.mount("https://", adapter)
http_session.mount("http://", adapter)
http_session.headers.update({
    "Content-Type": "application/json",
    "Connection": "keep-alive",
    "x-goog-api-key": GEMINI_API_KEY or ""
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
    "You are an expert university professor solving a Moodle quiz with 100% accuracy. "
    "Carefully analyze the question, any attached images, formulas, and every option before deciding. "
    "Double-check calculations, edge cases, negations ('NOT', 'EXCEPT', 'FALSE'), and units. "
    "For single-choice questions, select exactly ONE option number. "
    "For multiple-select (checkbox) questions, select ALL correct option numbers."
)

CODING_SYSTEM_PROMPT = (
    "You are an expert programmer solving an automated coding assessment (CodeRunner).\n"
    "CRITICAL RULES:\n"
    "1. Write the solution in the exact programming language required by the page, question, or starter code (e.g., Python, C, C++, Java, SQL).\n"
    "2. Match any starter/template function signatures, class names, input/output formats, and constraints exactly.\n"
    "3. Handle all edge cases (empty inputs, boundaries, negative numbers, whitespace/newlines in output).\n"
    "4. Output ONLY the raw, runnable source code with valid indentation (4 spaces per level). Do NOT include markdown code fences (```) or explanatory text."
)

def build_thinking_config(model_name=None):
    """Returns a valid thinkingConfig dict compatible with the target Gemini model."""
    target = (model_name or GEMINI_MODEL).lower()
    valid_levels = {"MINIMAL", "LOW", "MEDIUM", "HIGH", "XHIGH"}
    level = GEMINI_THINKING_LEVEL if GEMINI_THINKING_LEVEL in valid_levels else "HIGH"
    if level == "XHIGH" and not ("neon" in target or "hydrogen" in target):
        level = "HIGH"
    if level == "MINIMAL" and ("3.8" in target or "gemini-4" in target):
        level = "LOW"
    return {"thinkingLevel": level}

def extract_clean_text_from_dom(page, selectors, exclude_selectors=None):
    """
    Extracts clean text from the first matching selector, preserving TeX math formulas
    and <pre>/<code> formatting while stripping hidden Moodle accessibility/answer nodes.
    """
    exclude_selectors = exclude_selectors or []
    try:
        return page.evaluate(
            """({ selectors, excludeSelectors }) => {
                let target = null;
                for (const sel of selectors) {
                    const el = document.querySelector(sel);
                    if (el) {
                        target = el;
                        break;
                    }
                }
                if (!target) return "";

                const clone = target.cloneNode(true);

                // Replace MathJax/TeX annotations with readable LaTeX
                clone.querySelectorAll('script[type*="math/tex"]').forEach(s => {
                    const tex = (s.textContent || "").trim();
                    if (tex) {
                        const span = document.createElement('span');
                        span.textContent = ` $${tex}$ `;
                        s.replaceWith(span);
                    } else {
                        s.remove();
                    }
                });
                clone.querySelectorAll('mjx-container, .MathJax, .MathJax_Display').forEach(mjx => {
                    const ann = mjx.querySelector('annotation[encoding="application/x-tex"]');
                    if (ann && ann.textContent.trim()) {
                        const span = document.createElement('span');
                        span.textContent = ` $${ann.textContent.trim()}$ `;
                        mjx.replaceWith(span);
                    }
                });
                clone.querySelectorAll('.MathJax_Preview, .MJX_Assistive_MathML').forEach(el => el.remove());

                // Preserve TeX alt text on rendered formula images
                clone.querySelectorAll('img.texrender, img.Wirisformula').forEach(img => {
                    const alt = (img.getAttribute('alt') || img.getAttribute('title') || "").trim();
                    if (alt) {
                        const span = document.createElement('span');
                        span.textContent = ` ${alt} `;
                        img.replaceWith(span);
                    }
                });

                // Remove excluded sub-regions (e.g., answer block, clear choice, hidden labels)
                for (const ex of excludeSelectors) {
                    clone.querySelectorAll(ex).forEach(el => el.remove());
                }
                clone.querySelectorAll('.accesshide, .sr-only').forEach(el => el.remove());

                // Extract innerText in a temporary offscreen container so formatting stays clean
                const host = document.createElement('div');
                host.style.position = 'fixed';
                host.style.left = '-99999px';
                host.style.whiteSpace = 'pre-wrap';
                host.appendChild(clone);
                document.body.appendChild(host);
                const text = (host.innerText || host.textContent || "").trim();
                host.remove();
                return text.replace(/\\n{3,}/g, "\\n\\n").trim();
            }""",
            {"selectors": selectors, "excludeSelectors": exclude_selectors}
        )
    except Exception:
        for sel in selectors:
            el = page.query_selector(sel)
            if el:
                return el.inner_text().strip()
        return ""

def capture_element_images(el, label_prefix="Question"):
    """Captures visible non-spacer images inside a Playwright element as base64 JPEGs."""
    images = []
    if not el:
        return images
    for idx, img in enumerate(el.query_selector_all("img"), 1):
        try:
            # Skip tiny spacer/icon images and pure TeX images if already small
            box = img.bounding_box()
            if not box or box["width"] < 14 or box["height"] < 14:
                continue
            img_bytes = img.screenshot(type="jpeg")
            b64 = base64.b64encode(img_bytes).decode("utf-8")
            images.append({"label": f"{label_prefix} image {idx}", "data": b64})
        except Exception as e:
            print(f" Could not capture {label_prefix} image: {e}")
    return images

def get_images_from_question(page):
    """Captures visible images from the question prompt area (excluding answer options)."""
    for sel in [".qtext", ".questiontext", ".formulation"]:
        el = page.query_selector(sel)
        if el:
            # If falling back to .formulation, avoid capturing option images twice
            imgs = []
            for idx, img in enumerate(el.query_selector_all("img"), 1):
                try:
                    in_answer = img.evaluate("el => !!el.closest('.answer')")
                    if in_answer:
                        continue
                    box = img.bounding_box()
                    if not box or box["width"] < 14 or box["height"] < 14:
                        continue
                    img_bytes = img.screenshot(type="jpeg")
                    imgs.append({"label": f"Question image {idx}", "data": base64.b64encode(img_bytes).decode("utf-8")})
                except Exception as e:
                    print(f" Could not capture image: {e}")
            return imgs
    return []

def copy_quiz_content(page):
    """
    Extracts question text, question images, and paired option metadata
    (choice options with aligned input IDs/selectors, or matching dropdowns).
    """
    question = extract_clean_text_from_dom(
        page,
        [".qtext", ".questiontext", ".formulation"],
        exclude_selectors=[".answer", ".ablock", ".qtype_multichoice_clearchoice"]
    )
    if not question:
        return None, [], []

    images = get_images_from_question(page)

    # Extract radio / checkbox choices paired 1:1 with their DOM input elements
    raw_choices = page.evaluate(
        """() => {
            const inputs = Array.from(document.querySelectorAll(".answer input[type='radio'], .answer input[type='checkbox']"));
            const results = [];

            const extractNodeText = (node) => {
                if (!node) return "";
                const clone = node.cloneNode(true);
                clone.querySelectorAll('script[type*="math/tex"]').forEach(s => {
                    const tex = (s.textContent || "").trim();
                    const span = document.createElement('span');
                    span.textContent = tex ? ` $${tex}$ ` : "";
                    s.replaceWith(span);
                });
                clone.querySelectorAll('mjx-container, .MathJax, .MathJax_Display').forEach(mjx => {
                    const ann = mjx.querySelector('annotation[encoding="application/x-tex"]');
                    if (ann && ann.textContent.trim()) {
                        const span = document.createElement('span');
                        span.textContent = ` $${ann.textContent.trim()}$ `;
                        mjx.replaceWith(span);
                    }
                });
                clone.querySelectorAll('.MathJax_Preview, .MJX_Assistive_MathML, .accesshide, .sr-only, input').forEach(e => e.remove());
                clone.querySelectorAll('img.texrender, img.Wirisformula').forEach(img => {
                    const alt = (img.getAttribute('alt') || img.getAttribute('title') || "").trim();
                    if (alt) {
                        const span = document.createElement('span');
                        span.textContent = ` ${alt} `;
                        img.replaceWith(span);
                    }
                });
                return (clone.innerText || clone.textContent || "").replace(/\\s+/g, " ").trim();
            };

            let validIdx = 0;
            for (let domIdx = 0; domIdx < inputs.length; domIdx++) {
                const inp = inputs[domIdx];
                // Skip Moodle "Clear my choice" or disabled/hidden inputs
                if (inp.value === "-1" || inp.disabled || inp.closest('.qtype_multichoice_clearchoice')) {
                    continue;
                }
                validIdx++;
                let labelEl = null;
                const labelledBy = inp.getAttribute("aria-labelledby");
                if (labelledBy) {
                    labelEl = document.getElementById(labelledBy);
                }
                if (!labelEl && inp.id) {
                    labelEl = document.querySelector(`label[for="${CSS.escape(inp.id)}"]`);
                }
                const row = inp.closest('.r0, .r1, .d-flex, .form-check, div');
                const answerLabel = row ? row.querySelector("div[data-region='answer-label'], .flex-fill, .ml-1, label") : null;
                const targetTextEl = labelEl || answerLabel || row;
                let text = extractNodeText(targetTextEl);
                if (!text && labelEl && labelEl !== targetTextEl) {
                    text = extractNodeText(labelEl);
                }
                if (!text) {
                    text = inp.getAttribute("aria-label") || inp.value || `[Option ${validIdx}]`;
                }

                // Tag the input so Playwright can click/verify the exact paired element
                inp.setAttribute('data-lms-opt-idx', String(validIdx));
                if (targetTextEl) {
                    targetTextEl.setAttribute('data-lms-opt-label-idx', String(validIdx));
                }

                results.push({
                    index: validIdx,
                    text: text,
                    is_checkbox: inp.type === "checkbox",
                    input_id: inp.id || ""
                });
            }
            return results;
        }"""
    )

    if raw_choices:
        options = []
        for item in raw_choices:
            idx = item["index"]
            label_el = page.query_selector(f"[data-lms-opt-label-idx='{idx}']")
            opt_imgs = capture_element_images(label_el, label_prefix=f"Option {idx}")
            images.extend(opt_imgs)
            options.append({
                "index": idx,
                "text": item["text"],
                "display": f"{idx}. {item['text']}",
                "is_checkbox": item["is_checkbox"],
                "selector": f"[data-lms-opt-idx='{idx}']",
                "has_image": len(opt_imgs) > 0
            })
        return question, options, images

    # Check for Moodle Matching (<select> dropdowns inside .answer)
    matching_rows = page.evaluate(
        """() => {
            const selects = Array.from(document.querySelectorAll(".answer select"));
            if (!selects.length) return [];
            const rows = [];
            selects.forEach((sel, i) => {
                const idx = i + 1;
                sel.setAttribute("data-lms-select-idx", String(idx));
                const tr = sel.closest("tr, .r0, .r1, div");
                const promptCell = tr ? tr.querySelector("td.text, .text, label") : null;
                let promptText = promptCell ? (promptCell.innerText || "").replace(/\\s+/g, " ").trim() : `Item ${idx}`;
                const choices = Array.from(sel.options)
                    .filter(o => o.value !== "0" && o.value !== "" && !/choose/i.test(o.text))
                    .map(o => ({ value: o.value, text: o.text.trim() }));
                rows.push({
                    index: idx,
                    prompt: promptText,
                    selector: `[data-lms-select-idx='${idx}']`,
                    choices: choices
                });
            });
            return rows;
        }"""
    )

    if matching_rows:
        return question, {"type": "matching", "rows": matching_rows}, images

    return question, [], images

def send_gemini_request(payload, api_type="Normal Quiz", max_passes=3, model=None):
    """
    Executes a POST request to Gemini API with automatic model fallback on
    503 (high demand), 502/504, 404, or 429 (rate limit) errors.
    Skips temporarily cooled-down models so subsequent questions don't stall,
    and uses the x-goog-api-key header so API keys never appear in URLs.
    """
    base_models = [model] + [m for m in GEMINI_FALLBACK_MODELS if m != model] if model else list(GEMINI_FALLBACK_MODELS)

    for pass_num in range(1, max_passes + 1):
        rate_limited_wait = 0
        now = time.time()
        # Try healthy models first; only retry cooled-down models if all are cooling down
        ready_models = [m for m in base_models if _MODEL_COOLDOWN_UNTIL.get(m, 0) <= now]
        models_to_try = ready_models if ready_models else base_models

        for target_model in models_to_try:
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{target_model}:generateContent"
            req_payload = payload
            if isinstance(payload.get("generationConfig"), dict) and "thinkingConfig" in payload["generationConfig"]:
                req_payload = {
                    **payload,
                    "generationConfig": {
                        **payload["generationConfig"],
                        "thinkingConfig": build_thinking_config(target_model)
                    }
                }
            try:
                r = http_session.post(url, json=req_payload, timeout=(10, 35))

                # Rate limit (429): put model on cooldown and switch to next fallback immediately
                if r.status_code == 429:
                    wait_time = 60
                    try:
                        retry_after = r.headers.get("Retry-After")
                        if retry_after and retry_after.isdigit():
                            wait_time = max(int(retry_after), 30)
                        else:
                            err_msg = r.json().get("error", {}).get("message", "")
                            match = re.search(r'retry in\s*([\d.]+)\s*s', err_msg, re.IGNORECASE)
                            if match:
                                wait_time = max(int(float(match.group(1))) + 5, 15)
                    except Exception:
                        pass
                    _MODEL_COOLDOWN_UNTIL[target_model] = time.time() + wait_time
                    rate_limited_wait = max(rate_limited_wait, wait_time)
                    print(f" [429 Rate Limit] {target_model} rate-limited. Falling back to next model...")
                    continue

                # High demand / transient upstream outage / unavailable model: cool down briefly and fail over
                if r.status_code in (502, 503, 504, 404):
                    cooldown_sec = 3600 if r.status_code == 404 else 120
                    _MODEL_COOLDOWN_UNTIL[target_model] = time.time() + cooldown_sec
                    print(f" [HTTP {r.status_code}] {target_model} unavailable/high demand. Falling back to next model...")
                    continue

                # If an older fallback rejects thinkingConfig with HTTP 400, retry once without thinkingConfig
                if r.status_code == 400 and "thinking" in r.text.lower() and isinstance(req_payload.get("generationConfig"), dict):
                    stripped_gen = {k: v for k, v in req_payload["generationConfig"].items() if k != "thinkingConfig"}
                    r = http_session.post(url, json={**req_payload, "generationConfig": stripped_gen}, timeout=90)

                if r.status_code >= 400:
                    print(f" Gemini API HTTP {r.status_code} ({target_model}). Trying next model...")
                    continue

                res_data = r.json()
                candidates = res_data.get("candidates", [])
                if not candidates:
                    print(f" Empty candidates from {target_model} ({api_type}).")
                    continue

                candidate = candidates[0]
                finish_reason = candidate.get("finishReason")
                if finish_reason and finish_reason not in ("STOP", "MAX_TOKENS"):
                    print(f" Warning: {target_model} finishReason={finish_reason}")

                parts = candidate.get("content", {}).get("parts", [])
                # Exclude internal thought blocks if returned alongside answer text
                text_parts = [
                    part["text"] for part in parts
                    if isinstance(part, dict) and "text" in part and not part.get("thought")
                ]
                if not text_parts:
                    text_parts = [part["text"] for part in parts if isinstance(part, dict) and "text" in part]

                reply = "\n".join(text_parts).strip()
                if reply:
                    _MODEL_COOLDOWN_UNTIL.pop(target_model, None)
                    if target_model != base_models[0]:
                        print(f" [Fallback Success] Answered using {target_model}")
                    return reply

            except requests.exceptions.RequestException as e:
                _MODEL_COOLDOWN_UNTIL[target_model] = time.time() + 120
                print(f" Request error with {target_model} ({api_type}): {type(e).__name__}")
                continue
            except Exception as e:
                print(f" Unexpected error with {target_model} ({api_type}): {type(e).__name__}")
                continue

        # All fallback models failed on this pass
        if pass_num < max_passes:
            cooldown = rate_limited_wait if rate_limited_wait > 0 else 5 * pass_num
            print(f"\n All Gemini models busy/unavailable (Pass {pass_num}/{max_passes}). Cooling down {cooldown}s...")
            for remaining in range(cooldown, 0, -5):
                print(f" Waiting before retry pass... {remaining:02d}s remaining", end="\r", flush=True)
                time.sleep(min(5, remaining))
            print("\n Resuming Gemini API requests...\n")

    print(f" Gemini API request failed across all fallback models after {max_passes} passes.")
    return None

def _append_image_parts(parts, images):
    """Appends labeled image parts to a Gemini REST parts list."""
    if not images:
        return
    for img in images:
        if isinstance(img, dict):
            parts.append({"text": f"[{img.get('label', 'Attached Image')}]:"})
            parts.append({
                "inlineData": {
                    "mimeType": "image/jpeg",
                    "data": img["data"]
                }
            })
        else:
            parts.append({
                "inlineData": {
                    "mimeType": "image/jpeg",
                    "data": img
                }
            })

def ask_gemini_api(question, options, images=None):
    if PERFORMANCE["benchmark"]:
        benchmark.start("Gemini API (MCQ)")

    is_matching = isinstance(options, dict) and options.get("type") == "matching"

    if is_matching:
        rows_desc = []
        for row in options["rows"]:
            choices_str = ", ".join([c["text"] for c in row["choices"]])
            rows_desc.append(f"Row {row['index']}: {row['prompt']} -> Choices: [{choices_str}]")
        prompt_text = (
            f"Question:\n{question}\n\n"
            f"Match each row with the exact correct choice text from its Choices list:\n"
            + "\n".join(rows_desc)
        )
        schema = {
            "type": "object",
            "properties": {
                "reasoning": {"type": "string"},
                "matches": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "row_index": {"type": "integer"},
                            "choice_text": {"type": "string"}
                        },
                        "required": ["row_index", "choice_text"]
                    }
                }
            },
            "required": ["reasoning", "matches"]
        }
    elif options:
        is_multi = any(opt.get("is_checkbox") for opt in options)
        mode_hint = (
            "MULTIPLE-SELECT (checkbox): select ALL correct option numbers."
            if is_multi
            else "SINGLE-CHOICE (radio): select EXACTLY ONE correct option number."
        )
        options_block = "\n".join(opt["display"] for opt in options)
        prompt_text = (
            f"Selection Type: {mode_hint}\n\n"
            f"Question:\n{question}\n\n"
            f"Options:\n{options_block}"
        )
        schema = {
            "type": "object",
            "properties": {
                "reasoning": {
                    "type": "string",
                    "description": "Step-by-step analysis checking calculations, facts, and eliminating wrong options."
                },
                "selected_options": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": "1-based option numbers that are correct."
                }
            },
            "required": ["reasoning", "selected_options"]
        }
    else:
        prompt_text = (
            f"Question:\n{question}\n\n"
            f"Provide the exact short text or numeric answer expected in the Moodle input box."
        )
        schema = {
            "type": "object",
            "properties": {
                "reasoning": {"type": "string"},
                "answer": {
                    "type": "string",
                    "description": "Concise direct answer with no extra explanation or surrounding quotes."
                }
            },
            "required": ["reasoning", "answer"]
        }

    parts = [{"text": prompt_text}]
    _append_image_parts(parts, images)

    payload = {
        "systemInstruction": {"parts": [{"text": QUIZ_SYSTEM_PROMPT}]},
        "contents": [{"role": "user", "parts": parts}],
        "generationConfig": {
            "thinkingConfig": build_thinking_config(),
            "responseMimeType": "application/json",
            "responseJsonSchema": schema
        }
    }

    print(f" Asking Gemini API ({GEMINI_MODEL}, thinking={GEMINI_THINKING_LEVEL})...")
    reply = send_gemini_request(payload, api_type="Normal Quiz")
    if reply:
        print(f" Gemini API said: '{reply[:300]}{'...' if len(reply) > 300 else ''}'")

    if PERFORMANCE["benchmark"]:
        benchmark.stop("Gemini API (MCQ)")
    return reply

def _parse_json_reply(reply):
    """Safely parses JSON structured output, stripping accidental code fences if present."""
    cleaned = reply.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r'^```[a-zA-Z]*\s*', '', cleaned)
        cleaned = re.sub(r'\s*```$', '', cleaned).strip()
    try:
        return json.loads(cleaned)
    except Exception:
        match = re.search(r'\{.*\}', cleaned, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(0))
            except Exception:
                pass
    return None

def click_answer(page, reply, options):
    parsed = _parse_json_reply(reply)

    # 1. Moodle Matching (<select> dropdowns)
    if isinstance(options, dict) and options.get("type") == "matching":
        if not parsed or "matches" not in parsed:
            print(f" Could not parse matching JSON from reply: '{reply}'")
            return
        row_map = {r["index"]: r for r in options["rows"]}
        for m in parsed["matches"]:
            r_idx = m.get("row_index")
            c_text = str(m.get("choice_text", "")).strip()
            row = row_map.get(r_idx)
            if not row:
                continue
            sel_el = page.query_selector(row["selector"])
            if not sel_el:
                continue
            matched_val = None
            for c in row["choices"]:
                if c["text"].lower() == c_text.lower() or c_text.lower() in c["text"].lower():
                    matched_val = c["value"]
                    break
            if matched_val is not None:
                sel_el.select_option(value=matched_val)
                print(f" Selected row {r_idx}: {c_text}")
            else:
                print(f" Could not match choice '{c_text}' for row {r_idx}")
        return

    # 2. Single-choice (radio) or Multi-select (checkbox)
    if options:
        selected_indices = []
        if parsed and isinstance(parsed.get("selected_options"), list):
            selected_indices = [int(x) for x in parsed["selected_options"] if str(x).isdigit()]
        else:
            match = re.search(r'FINAL_ANSWER:\s*(\d+)', reply, re.IGNORECASE)
            if match:
                selected_indices = [int(match.group(1))]
            else:
                print(f" Could not parse structured selection from reply: '{reply}'")
                return

        if not selected_indices:
            print(f" Empty selection returned: '{reply}'")
            return

        is_multi = any(opt.get("is_checkbox") for opt in options)
        if not is_multi:
            selected_indices = [selected_indices[0]]

        opt_map = {opt["index"]: opt for opt in options}

        # For multi-select re-attempts, clear any previously checked boxes not in selection
        if is_multi:
            selected_set = set(selected_indices)
            for opt in options:
                el = page.query_selector(opt["selector"])
                if el and el.is_checked() and opt["index"] not in selected_set:
                    el.uncheck(force=True)

        for opt_num in selected_indices:
            opt = opt_map.get(opt_num)
            if not opt:
                print(f" Option {opt_num} doesn't exist. Only {len(options)} options found.")
                continue
            inp = page.query_selector(opt["selector"])
            if not inp:
                print(f" Could not locate input element for option {opt_num}.")
                continue
            if opt["is_checkbox"]:
                if not inp.is_checked():
                    inp.check(force=True)
            else:
                inp.click(force=True)
            print(f" Clicked option {opt_num}: {opt['text']}")
        return

    # 3. Short-Answer / Numerical input box
    answer_text = str(parsed.get("answer", "")).strip() if parsed and "answer" in parsed else reply.strip()
    inp = page.query_selector(".answer input[type='text'], .answer input[type='number'], .answer textarea")
    if inp:
        inp.fill(answer_text)
        print(f" Typed: {answer_text}")

# ─── CODING QUIZ METHODS ─────────────────────────────────────
def get_coding_question(page):
    question = extract_clean_text_from_dom(
        page,
        [".qtext", ".questiontext", ".formulation"],
        exclude_selectors=[".answer", ".coderunner-test-results"]
    )

    # Also capture visible CodeRunner example tables or language indicators
    extra_context = page.evaluate(
        """() => {
            const parts = [];
            const langSelect = document.querySelector("select[name*='language'], .coderunner-lang");
            if (langSelect) {
                const langText = langSelect.tagName === 'SELECT'
                    ? (langSelect.options[langSelect.selectedIndex]?.text || '')
                    : langSelect.innerText;
                if (langText.trim()) parts.push("Language: " + langText.trim());
            }
            try {
                const el = document.querySelector('.ace_editor');
                const mode = el?.env?.editor?.session?.$modeId || "";
                if (mode) parts.push("Editor Mode: " + mode.split('/').pop());
            } catch (e) {}
            const exTables = document.querySelectorAll('.coderunnerexamples, table.coderunner-examples, .qtype_coderunner_examples');
            exTables.forEach(t => {
                const txt = (t.innerText || "").trim();
                if (txt) parts.push("Examples:\\n" + txt);
            });
            return parts.join("\\n\\n");
        }"""
    )
    if extra_context:
        question = f"{question}\n\n{extra_context}".strip()

    images = get_images_from_question(page) if question else []

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

    return question, existing_code, images

def ask_gemini_coding(question, existing_code, error_feedback=None, previous_code=None, images=None):
    if PERFORMANCE["benchmark"]:
        benchmark.start("Gemini API (Coding)")

    message = f"Problem Description:\n{question}\n\n"
    if existing_code:
        message += f"Existing/Template Code:\n{existing_code}\n\n"
        
    if error_feedback:
        message += f"Your previous code:\n{previous_code}\n\n"
        message += f"Failed with this error/feedback:\n{error_feedback}\n\n"
        message += "Analyze the failure, fix the bug, and output ONLY the complete corrected raw code without markdown."
    else:
        message += "Please provide the complete working code. Output ONLY the raw code without markdown."

    parts = [{"text": message}]
    _append_image_parts(parts, images)

    payload = {
        "systemInstruction": {"parts": [{"text": CODING_SYSTEM_PROMPT}]},
        "contents": [{"role": "user", "parts": parts}],
        "generationConfig": {
            "thinkingConfig": build_thinking_config()
        }
    }

    print(f" Asking Gemini API for Code ({GEMINI_MODEL}, thinking={GEMINI_THINKING_LEVEL})...")
    reply = send_gemini_request(payload, api_type="Coding Quiz")
    if reply:
        if reply.startswith("```"):
            reply = re.sub(r'^```[a-zA-Z0-9_+-]*\s*\n?', '', reply)
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

    # Snapshot previous outcome text so we detect when the new AJAX response lands
    prev_snapshot = page.evaluate("""() => {
        const el = document.querySelector('.coderunner-test-results, .outcome, .feedback');
        return el ? el.innerText.trim() : '';
    }""")

    check_btn.click()

    # Wait for CodeRunner AJAX grading to finish (up to 15s)
    try:
        page.wait_for_function(
            """(prev) => {
                const el = document.querySelector('.coderunner-test-results, .outcome, .feedback, .alert-success, .alert-danger');
                if (!el) return false;
                const txt = el.innerText.trim();
                return txt.length > 0 && txt !== prev;
            }""",
            arg=prev_snapshot,
            timeout=15000
        )
    except Exception:
        page.wait_for_timeout(2000)

    result_info = page.evaluate("""() => {
        const resultEl = document.querySelector('.coderunner-test-results, .outcome, .feedback');
        const successEl = document.querySelector('.outcome .alert-success, .coderunner-test-results .alert-success, .que .alert-success');
        const dangerEl = document.querySelector('.outcome .alert-danger, .coderunner-test-results .alert-danger, .alert-danger, .alert-warning');
        const gradeEl = document.querySelector('.grade, .outcome .grade');

        const resultText = resultEl ? resultEl.innerText.trim() : "";
        const dangerText = dangerEl ? dangerEl.innerText.trim() : "";
        const gradeText = gradeEl ? gradeEl.innerText.trim() : "";
        const hasPassBadge = !!document.querySelector('.coderunner-test-results .fa-check, .coderunner-test-results .pass');
        const hasFailBadge = !!document.querySelector('.coderunner-test-results .fa-times, .coderunner-test-results .fail');

        return {
            resultText,
            dangerText,
            gradeText,
            hasSuccessAlert: !!successEl,
            hasDangerAlert: !!dangerEl,
            hasPassBadge,
            hasFailBadge
        };
    }""")

    combined_text = "\n".join(
        t for t in [result_info["resultText"], result_info["dangerText"], result_info["gradeText"]] if t
    ).strip()

    # Explicit failure signals take precedence
    if result_info["hasFailBadge"] or "try again" in combined_text.lower() or "failed" in combined_text.lower() or "syntax error" in combined_text.lower():
        return False, combined_text or "Code failed tests."

    if result_info["hasSuccessAlert"] or "passed all tests" in combined_text.lower():
        return True, "Passed"

    # Check exact full-mark ratio like "Marks for this submission: 1.00/1.00"
    mark_match = re.search(r'marks for this submission:\s*([\d.]+)\s*/\s*([\d.]+)', combined_text, re.IGNORECASE)
    if mark_match:
        earned, total = float(mark_match.group(1)), float(mark_match.group(2))
        if total > 0 and earned >= total:
            return True, "Passed"
        return False, combined_text

    if result_info["hasPassBadge"] and not result_info["hasDangerAlert"]:
        return True, "Passed"

    if combined_text:
        return False, combined_text

    return False, "Code failed some tests or had syntax errors. Please review the logic."

# ─── NAVIGATION (CONDITION-BASED WAITS) ──────────────────────
def go_next(page):
    next_selectors = [
        "input[value='Next page']", 
        "input[value='Next']", 
        "button:has-text('Next page')",
        "button:has-text('Next')",
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

    finish_selectors = [
        "input[value='Finish attempt ...']", 
        "input[value='Finish attempt']",
        "button:has-text('Finish attempt ...')", 
        "button:has-text('Finish attempt')",
        "input[name='finishattempt']",
        "a:has-text('Finish attempt')",
        "a[href*='summary.php']",
        "a.endtestlink",
        "input#mod_quiz-next-nav",
        "input[name='next']"
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
            quiz_urls = [{"url": course_url, "title": "Direct Quiz"}]
        else:
            print(" Searching for quizzes...")
            quiz_urls = quiz_page.evaluate('''() => {
                const keywords = ['quiz', 'practice quiz', 'mcq', 'skill assessments', 'viva'];
                const excludePatterns = [
                    /(?:^|\\W)cia(?:\\d|\\W|$)/i,
                    /(?:^|\\W)model(?:\\d|\\W|$)/i,
                    /model practical/i,
                    /practical/i,
                    /semester/i,
                    /examination/i,
                    /exam/i,
                    /(?:^|\\W)lab(?:\\d|\\W|$)/i,
                    /password/i
                ];
                const links = Array.from(document.querySelectorAll('a[href*="/mod/quiz/view.php"]'));
                const matched = [];
                const seenUrls = new Set();
                
                for (const link of links) {
                    if (seenUrls.has(link.href)) continue;
                    
                    const text = link.innerText.toLowerCase();
                    const titleText = (link.title || "").toLowerCase();
                    if (!text.trim() && !titleText.trim()) continue;

                    const activityContainer = link.closest('.activity, .activity-item, li.activity, li');
                    const sectionContainer = link.closest('.section, li.section, .course-section');
                    
                    const containerText = activityContainer ? activityContainer.innerText.toLowerCase() : "";
                    const sectionText = sectionContainer ? sectionContainer.innerText.toLowerCase() : "";
                    
                    const fullContext = text + " " + containerText + " " + sectionText + " " + titleText;
                    
                    let title = link.innerText.trim();
                    if (!title && activityContainer) {
                        title = activityContainer.innerText.split('\\n')[0].trim();
                    }
                    if (!title) {
                        title = link.href;
                    }
                    
                    if (keywords.some(k => text.includes(k) || titleText.includes(k))) {
                        let hasExcluded = false;
                        for (const regex of excludePatterns) {
                            if (regex.test(fullContext)) {
                                hasExcluded = true;
                                break;
                            }
                        }
                        if (hasExcluded) {
                            matched.push({url: link.href, title: "⚠️ " + title, is_restricted: true});
                        } else {
                            matched.push({url: link.href, title: title, is_restricted: false});
                        }
                        seenUrls.add(link.href);
                    }
                }
                return matched;
            }''')
        
        if PERFORMANCE["benchmark"]:
            benchmark.stop("Scrape Course Quizzes")

        if not quiz_urls:
            print("❌  No quizzes found matching the criteria.")
            context.close()
            return
            
        print(f" Found {len(quiz_urls)} quizzes to attempt!")
        for q in quiz_urls:
            print(f"  - [{q['title']}] {q['url']}")

        # Let user select which quizzes to attempt via GUI
        try:
            import sys
            sys.path.append(os.path.dirname(os.path.abspath(__file__)))
            from utils.activity_selector import prompt_activity_selection
            
            # The selector will display a Tkinter window with checkboxes
            quiz_urls = prompt_activity_selection(
                quiz_urls, 
                course_name="Moodle Quiz Automation",
                headless=False
            )
            
            if quiz_urls:
                restricted_selected = any(q.get("is_restricted", False) for q in quiz_urls)
                if restricted_selected:
                    try:
                        import tkinter as tk
                        from tkinter import messagebox
                        root = tk.Tk()
                        root.withdraw()
                        root.attributes("-topmost", True)
                        result = messagebox.askyesno(
                            "Warning",
                            "Potential End - Semester Examination Quiz (Included)\nAre you sure you want to continue?",
                            parent=root
                        )
                        root.destroy()
                        if not result:
                            print("❌ Automation cancelled by user due to restricted quiz selection.")
                            context.close()
                            return
                    except ImportError:
                        ans = input("\n⚠️ Potential End - Semester Examination Quiz (Included)\nAre you sure you want to continue? (y/n): ")
                        if ans.lower() not in ['y', 'yes']:
                            print("❌ Automation cancelled by user due to restricted quiz selection.")
                            context.close()
                            return
            if not quiz_urls:
                print("❌  No quizzes selected. Exiting.")
                context.close()
                return
        except ImportError as e:
            print(f"⚠️  Could not load GUI selector ({e}). Proceeding with all {len(quiz_urls)} quizzes.")

        skipped_seb_quizzes = []

        for q in quiz_urls:
            current_quiz_url = q['url']
            quiz_title = q['title']
            
            if PERFORMANCE["benchmark"]:
                benchmark.start(f"Quiz Execution ({current_quiz_url.split('id=')[-1]})")

            print(f"\n Navigating to Quiz: [{quiz_title}] {current_quiz_url}")
            quiz_page.goto(current_quiz_url, wait_until="domcontentloaded")
            
            # Dynamic SEB check
            page_content = quiz_page.content()
            if "This quiz has been configured so that students may only attempt it using the Safe Exam Browser." in page_content:
                print("⚠️  Safe Exam Browser required. Skipping this quiz.")
                skipped_seb_quizzes.append(current_quiz_url)
                if PERFORMANCE["benchmark"]:
                    benchmark.stop(f"Quiz Execution ({current_quiz_url.split('id=')[-1]})")
                continue

            # Strict Password Check
            try:
                page_content_lower = page_content.lower()
                has_password_field = quiz_page.locator("input[type='password']").count() > 0
                if "password" in page_content_lower or "requires password" in page_content_lower or has_password_field:
                    print("⚠️  Quiz requires a password. Skipping this quiz (strict enforcement).")
                    if PERFORMANCE["benchmark"]:
                        benchmark.stop(f"Quiz Execution ({current_quiz_url.split('id=')[-1]})")
                    continue
            except Exception:
                pass

            # Excluded keywords check
            try:
                texts_to_check = quiz_page.locator("h1, h2, h3").all_inner_texts()
                texts_to_check.append(quiz_page.title())
                texts_to_check_lower = " ".join(texts_to_check).lower()
                
                excluded_patterns = [
                    r"(?:^|\W)cia(?:\d|\W|$)", 
                    r"(?:^|\W)model(?:\d|\W|$)", 
                    r"model practical", 
                    r"practical", 
                    r"semester", 
                    r"examination", 
                    r"exam",
                    r"(?:^|\W)lab(?:\d|\W|$)"
                ]
                if any(re.search(p, texts_to_check_lower) for p in excluded_patterns):
                    print("⚠️  Quiz name contains excluded keywords (Model, Practical, Semester, etc). Skipping this quiz.")
                    if PERFORMANCE["benchmark"]:
                        benchmark.stop(f"Quiz Execution ({current_quiz_url.split('id=')[-1]})")
                    continue
            except Exception:
                pass

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
                        
                        # Handle confirmation modal if present (e.g. Time Limit)
                        quiz_page.wait_for_timeout(1500) # Give modal time to animate in
                        
                        modal_selectors = [
                            "input#id_submitbutton",
                            "button#id_submitbutton",
                            ".modal-dialog button:has-text('Start attempt')",
                            ".modal-dialog input[value='Start attempt']",
                            "button:has-text('Start attempt')",
                            "input[value='Start attempt']",
                            ".moodle-dialogue-confirm input[type='button']"
                        ]
                        
                        clicked_modal = False
                        for m_sel in modal_selectors:
                            try:
                                m_loc = quiz_page.locator(m_sel).last
                                if m_loc.is_visible():
                                    m_loc.click()
                                    print(f"✅   Clicked modal 'Start attempt' using selector: {m_sel}")
                                    clicked_modal = True
                                    quiz_page.wait_for_timeout(1000)
                                    break
                            except Exception:
                                pass
                                
                        if not clicked_modal:
                            # Fallback check just in case it took longer
                            try:
                                fallback_loc = quiz_page.locator("button:has-text('Start attempt'), input[value='Start attempt']").last
                                if fallback_loc.is_visible():
                                    fallback_loc.click()
                                    print("✅   Clicked modal 'Start attempt' (fallback)!")
                                    quiz_page.wait_for_timeout(1000)
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
                    if isinstance(options, dict) and options.get("type") == "matching":
                        print(f" Matching question ({len(options['rows'])} rows)")
                    elif options:
                        print(f" {[opt['display'] for opt in options]}")
                    else:
                        print(" Text input question")
                    
                    reply = ask_gemini_api(question, options, images)
                    if not reply:
                        print(" No reply. Skipping question.")
                    else:
                        click_answer(quiz_page, reply, options)

                result = go_next(quiz_page)

                if result == "none":
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

import os
import sys
import io
import json
import getpass
from dotenv import load_dotenv

# Force standard output to handle utf-8 safely, preventing UnicodeEncodeErrors on Windows
if sys.stdout.encoding.lower() != 'utf-8':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

def load_config():
    # 1. Load from .env if present
    load_dotenv()
    
    # 2. Load from config.json if present
    json_config = {}
    if os.path.exists("config.json"):
        try:
            with open("config.json", "r", encoding="utf-8") as f:
                json_config = json.load(f)
        except Exception as e:
            print(f"⚠️ Failed to parse config.json: {e}")
            
    # Resolution priority:
    # 1. config.json
    # 2. .env (via os.environ)
    # 3. Interactive prompt
    
    config = {}
    
    def get_val(key_json, key_env, default="", prompt_text=None, is_secret=False):
        val = json_config.get(key_json)
        if not val:
            val = os.environ.get(key_env, "")
            
        if not val and prompt_text:
            if is_secret:
                val = getpass.getpass(prompt_text).strip()
            else:
                val = input(prompt_text).strip()
                
            # If the user entered a value interactively, persist it to the .env file
            if val:
                from dotenv import set_key
                # Ensure the .env file exists before setting a key
                if not os.path.exists(".env"):
                    open(".env", "a").close()
                set_key(".env", key_env, val)
                
        return val or default

    config["USERNAME"] = get_val("USERNAME", "MOODLE_USERNAME", prompt_text="Enter Moodle Username: ")
    config["PASSWORD"] = get_val("PASSWORD", "MOODLE_PASSWORD", prompt_text="Enter Moodle Password: ", is_secret=True)
    config["GEMINI_API_KEY"] = get_val("GEMINI_API_KEY", "GEMINI_API_KEY", prompt_text="Enter Gemini API Key: ", is_secret=True)
    config["EXTENSION_PATH"] = get_val("EXTENSION_PATH", "EXTENSION_PATH")
    config["BROWSER_EXECUTABLE_PATH"] = get_val("BROWSER_EXECUTABLE_PATH", "BROWSER_EXECUTABLE_PATH")
    
    return config

if __name__ == "__main__":
    print("Testing config loader...")
    cfg = load_config()
    print("Configuration loaded successfully!")

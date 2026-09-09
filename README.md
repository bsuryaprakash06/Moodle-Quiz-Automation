# LMS Quiz Automation

An automated Learning Management System solver utilizing Python, Playwright, and the Google Gemini API.

If you find this project helpful, consider giving it a star on GitHub!

## Prerequisites

Python 3.9 or higher must be installed on your system.

Install the required Python dependencies:
```bash
pip install -r requirements.txt
```

Install the Playwright browser binaries:
```bash
playwright install chromium
```

## Setup and Installation

1. Clone or download the repository to your local environment.
2. Ensure you have your LMS credentials and a valid Google Gemini API Key.
   * *You can obtain a free Gemini API Key from [Google AI Studio](https://aistudio.google.com/api-keys).*
3. Run the primary script:
   ```bash
   python lms_automation.py
   ```
4. During the initial execution, the script will prompt you for the following configuration values:
   * LMS Username
   * LMS Password
   * Gemini API Key
   * Path to your Browser Executable (Optional)
   
   These values are securely written to a local `.env` file and will not be requested on subsequent runs.

## Usage Guide

Execute the main automation script via your terminal:

```bash
python lms_automation.py
```

1. Enter the URL of the LMS Course or a direct Quiz URL when prompted.
2. The script will automatically restore your session, navigate to the target URL, and begin attempting the quizzes.
3. Upon completion, a Performance Benchmark Report is printed to the console, detailing execution metrics and identifying any quizzes that were automatically skipped.

## Notes

* Do not minimize the browser window if running in non-headless mode, as this may disrupt clipboard-based UI fallback mechanisms.

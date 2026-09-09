# Features and Capabilities

This document outlines the core technical implementations, edge cases handled, and optimization strategies integrated into the LMS Automation platform.

## Advanced DOM Parsing and AI Context
* Intelligent Question Extraction: Captures complex HTML structures, including nested elements and inline text formulations.
* Multimodal Context: Extracts embedded image data, converts it to Base64 encoding, and seamlessly injects it into the Gemini API payload to provide comprehensive visual context.

## CodeRunner Environment Integration
* Native ACE Editor Injection: Directly evaluates JavaScript to inject solved code into the ACE editor DOM objects.
* Keyboard Fallback Mechanism: Implements a robust clipboard paste via simulated keyboard inputs if direct DOM injection is blocked or fails.
* Asynchronous Verification: Replaces arbitrary wait states with condition based DOM evaluations, ensuring network requests complete before parsing test results.

## Safe Exam Browser Handling
* Dynamic Detection: Actively scans the DOM for Safe Exam Browser enforcement flags during the initialization phase.
* Automated Skipping: Skips restricted quizzes gracefully to prevent automation halting, logging the bypassed URLs for manual review post execution.

## Dynamic Navigation Protocol
* Abstracted Element Targeting: Utilizes wildcard and array based CSS selectors to identify varying submit, next, and finish buttons across different Moodle environments.
* Anchor Link Support: Capable of navigating both standard form inputs and anchor link based navigation structures.
* Modal Confirmation: Detects and automatically confirms standard Moodle submission dialogs.

## Network and API Stability
* Exponential Backoff: Implements a scaling wait strategy for HTTP 429 Too Many Requests responses from the Gemini API, preserving session integrity during rate limits.
* Telemetry and Benchmarking: Profiles execution times across key stages (authentication, scraping, API latency) and outputs a detailed performance matrix.

## Authentication and Security
* Session Persistence: Caches authenticated cookies to a local JSON file, bypassing the login sequence on subsequent executions.
* Encrypted Credential Loading: Sources credentials exclusively from a local environment file, ensuring sensitive data is isolated from the primary codebase.

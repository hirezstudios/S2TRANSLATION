import os
import csv
import requests
import openai # Added
import google.generativeai as genai # Added
from google.generativeai.types import HarmCategory, HarmBlockThreshold # Added for safety settings
from google.api_core import exceptions as google_exceptions # Added for Gemini exceptions
from dotenv import load_dotenv
import time
import re 
import concurrent.futures
import threading
import random
from tqdm import tqdm # Added for progress bar
import json # Added for audit logging
import logging
import uuid
from datetime import datetime

# Import local modules
from . import config
from . import db_manager
from . import prompt_manager
from . import api_clients # This will trigger client initialization

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(name)s - %(message)s')
logger = logging.getLogger(__name__)

# Load environment variables from .env file
load_dotenv()

# --- Language Configuration --- 
# TODO: Make this configurable, e.g., via command-line argument or .env
LANGUAGE_CODE = "esLA" 
# Simple mapping for language names (expand as needed)
LANGUAGE_NAME_MAP = {
    "esLA": "Latin American Spanish",
    "frFR": "French (France)",
    # Add other languages here
}
TARGET_LANGUAGE_NAME = LANGUAGE_NAME_MAP.get(LANGUAGE_CODE, LANGUAGE_CODE) # Fallback to code if name not found
GLOBAL_RULES_FILE = "system_prompts/global.md" # Added
# --- End Language Configuration ---

# --- API Configuration --- 
# General Settings
ACTIVE_API = os.getenv("ACTIVE_API", "PERPLEXITY").upper()

# Perplexity Settings
PPLX_API_KEY = os.getenv("PERPLEXITY_API_KEY")
PPLX_MODEL = os.getenv("PERPLEXITY_MODEL")
PPLX_API_URL = "https://api.perplexity.ai/chat/completions"

# OpenAI Settings
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL = os.getenv("OPENAI_MODEL")
# Initialize OpenAI client if using OpenAI
openai_client = None
if ACTIVE_API == "OPENAI" and OPENAI_API_KEY:
    openai_client = openai.OpenAI(api_key=OPENAI_API_KEY)

# Gemini Settings
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_MODEL = os.getenv("GEMINI_MODEL")
# Configure Gemini client later if needed

# --- End API Configuration ---

# --- CSV Configuration ---
INPUT_CSV = "input/blake-small.csv"
# Dynamically generate output filename based on API and language
OUTPUT_CSV = f"output/blake-small_{LANGUAGE_CODE}_{ACTIVE_API.lower()}.csv"
SYSTEM_PROMPT_FILE = f"system_prompts/tg_{LANGUAGE_CODE}.md"
SOURCE_COLUMN = "src_enUS"
TARGET_COLUMN = f"tg_{LANGUAGE_CODE}" 
# --- End CSV Configuration ---

# --- Workflow Configuration ---
TRANSLATION_MODE = os.getenv("TRANSLATION_MODE", "ONE_STAGE").upper()
# Determine default API for filename/one-stage mode
DEFAULT_API = os.getenv("ACTIVE_API", "PERPLEXITY").upper() 
AUDIT_LOG_FILE = os.getenv("AUDIT_LOG_FILE", f"output/audit_log_{LANGUAGE_CODE}_{DEFAULT_API.lower()}.jsonl")
# Stage-specific API/Model Settings
STAGE1_API = os.getenv("STAGE1_API", DEFAULT_API).upper()
STAGE2_API = os.getenv("STAGE2_API", DEFAULT_API).upper()
STAGE3_API = os.getenv("STAGE3_API", DEFAULT_API).upper()
STAGE1_MODEL_OVERRIDE = os.getenv("STAGE1_MODEL")
STAGE2_MODEL_OVERRIDE = os.getenv("STAGE2_MODEL")
STAGE3_MODEL_OVERRIDE = os.getenv("STAGE3_MODEL")
# Define system prompt template file paths
STAGE1_PROMPT_FILE = f"system_prompts/tg_{LANGUAGE_CODE}.md" # Specific language rules for stage 1
STAGE2_TEMPLATE_FILE = f"system_prompts/stage2_evaluate_template.md" # Use template
STAGE3_TEMPLATE_FILE = f"system_prompts/stage3_refine_template.md" # Use template
# --- End Workflow Configuration ---

# --- Threading and Retry Configuration ---
# Use environment variables with defaults
MAX_WORKER_THREADS_STR = os.getenv("MAX_WORKER_THREADS", "8")
MAX_WORKER_THREADS = int(MAX_WORKER_THREADS_STR.split('#')[0].strip())

API_MAX_RETRIES_STR = os.getenv("API_MAX_RETRIES", "3")
API_MAX_RETRIES = int(API_MAX_RETRIES_STR.split('#')[0].strip())

# Convert delays to float, also applying strip and split
API_INITIAL_RETRY_DELAY_STR = os.getenv("API_INITIAL_RETRY_DELAY", "5.0")
API_INITIAL_RETRY_DELAY = float(API_INITIAL_RETRY_DELAY_STR.split('#')[0].strip())

API_MAX_RETRY_DELAY_STR = os.getenv("API_MAX_RETRY_DELAY", "60.0")
API_MAX_RETRY_DELAY = float(API_MAX_RETRY_DELAY_STR.split('#')[0].strip())

THREAD_STAGGER_DELAY_STR = os.getenv("THREAD_STAGGER_DELAY", "1.0") 
THREAD_STAGGER_DELAY = float(THREAD_STAGGER_DELAY_STR.split('#')[0].strip())
# --- End Configuration ---

# Thread-safe lock for printing and counters if needed (especially with tqdm)
print_lock = threading.Lock()
translated_counter = 0
error_counter = 0
global_rules_content = None # Added
stage1_language_specific_prompt = None # Renamed for clarity
stage2_template_prompt = None 
stage3_template_prompt = None 

def load_system_prompts():
    """Loads global rules, language-specific intro/rules, and stage templates."""
    global global_rules_content, stage1_language_specific_prompt, stage2_template_prompt, stage3_template_prompt
    
    print(f"Loading Global Rules from {GLOBAL_RULES_FILE}...")
    global_rules_content = load_single_prompt(GLOBAL_RULES_FILE)
    if not global_rules_content:
        print(f"Critical Error: Global rules file {GLOBAL_RULES_FILE} is missing or empty. Exiting.")
        exit(1)
        
    print(f"Loading Language Specific Rules (Stage 1 base) from {STAGE1_PROMPT_FILE}...")
    stage1_language_specific_prompt = load_single_prompt(STAGE1_PROMPT_FILE)
    if not stage1_language_specific_prompt:
         print(f"Critical Error: Language specific file {STAGE1_PROMPT_FILE} is missing or empty. Exiting.")
         exit(1)

    # Stage 1 prompt will be constructed dynamically later

    if TRANSLATION_MODE == "THREE_STAGE":
        print(f"Loading Stage 2 Template from {STAGE2_TEMPLATE_FILE}...")
        stage2_template_prompt = load_single_prompt(STAGE2_TEMPLATE_FILE)
        
        print(f"Loading Stage 3 Template from {STAGE3_TEMPLATE_FILE}...")
        stage3_template_prompt = load_single_prompt(STAGE3_TEMPLATE_FILE)

def load_single_prompt(filepath):
    """Loads a single system prompt from a file."""
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            return f.read()
    except FileNotFoundError:
        # Return None instead of exiting, handle missing file in calling function
        print(f"Warning: Prompt file {filepath} not found.")
        return None 
    except Exception as e:
        print(f"Error reading system prompt file {filepath}: {e}")
        return None # Indicate error

# --- API Call Functions --- 

def call_perplexity_api(system_prompt, user_content, row_identifier="N/A", model_override=None):
    """Calls the Perplexity API for translation with retry logic and parses the output."""
    # Note: API_KEY and MODEL are now PPLX_ specific
    if not PPLX_API_KEY:
        with print_lock:
            print(f"Error [Row {row_identifier}]: PERPLEXITY_API_KEY not found.")
        return None 
    if not PPLX_MODEL:
        with print_lock:
            print(f"Error [Row {row_identifier}]: PERPLEXITY_MODEL not found.")
        return None
        
    target_model = model_override if model_override else PPLX_MODEL
    if not target_model:
        with print_lock:
            print(f"Error [Row {row_identifier}]: No Perplexity model specified (default or override).")
        return None
        
    headers = {
        "Authorization": f"Bearer {PPLX_API_KEY}",
        "Content-Type": "application/json",
        "accept": "application/json"
    }
    payload = {
        "model": target_model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content}
        ],
        "temperature": 0.2 
    }

    current_retry_delay = API_INITIAL_RETRY_DELAY
    for attempt in range(API_MAX_RETRIES + 1):
        try:
            response = requests.post(PPLX_API_URL, json=payload, headers=headers, timeout=60) # Increased timeout
            
            if response.status_code == 429: 
                raise requests.exceptions.RequestException(f"Rate limit exceeded (429)")
            response.raise_for_status() 
            
            data = response.json()
            raw_content = ""
            if data.get("choices") and len(data["choices"]) > 0:
                raw_content = data["choices"][0].get("message", {}).get("content", "")

            if raw_content:
                # Perplexity specific parsing (if needed beyond standard instruction)
                parsed_content = re.sub(r"<think>.*?</think>\s*", "", raw_content, flags=re.DOTALL).strip()
                if parsed_content:
                    return parsed_content 
                else:
                    with print_lock:
                        print(f"Warning [PPLX Row {row_identifier}]: Translation empty after parsing.")
                    return "" 
            else:
                 with print_lock:
                     print(f"Warning [PPLX Row {row_identifier}]: Empty/unexpected API response.")
                 return "" 

        except requests.exceptions.Timeout:
            with print_lock:
                print(f"Attempt {attempt + 1}/{API_MAX_RETRIES + 1} [PPLX Row {row_identifier}]: API call timed out.")
        except requests.exceptions.RequestException as e:
            with print_lock:
                print(f"Attempt {attempt + 1}/{API_MAX_RETRIES + 1} [PPLX Row {row_identifier}]: API Error - {e}")
        except Exception as e:
            with print_lock:
                print(f"Attempt {attempt + 1}/{API_MAX_RETRIES + 1} [PPLX Row {row_identifier}]: Unexpected Error - {e}")

        if attempt < API_MAX_RETRIES:
            wait_time = min(current_retry_delay + random.uniform(0, 1), API_MAX_RETRY_DELAY)
            with print_lock:
                print(f"Retrying [PPLX Row {row_identifier}] in {wait_time:.2f} seconds...")
            time.sleep(wait_time)
            current_retry_delay *= 2 
        else:
            with print_lock:
                print(f"Error [PPLX Row {row_identifier}]: Max retries reached for: '{user_content[:50]}...'")
            return None 
            
    return None 

def call_openai_api(system_prompt, user_content, row_identifier="N/A", model_override=None):
    """Calls the OpenAI Responses API for translation with retry logic and parses the output."""
    if not openai_client:
        with print_lock:
             print(f"Error [Row {row_identifier}]: OpenAI client not initialized (check OPENAI_API_KEY).")
        return None
    if not OPENAI_MODEL:
         with print_lock:
             print(f"Error [Row {row_identifier}]: OPENAI_MODEL not found in .env file.")
         return None

    target_model = model_override if model_override else OPENAI_MODEL
    if not target_model:
         with print_lock:
             print(f"Error [Row {row_identifier}]: No OpenAI model specified (default or override).")
         return None

    current_retry_delay = API_INITIAL_RETRY_DELAY
    for attempt in range(API_MAX_RETRIES + 1):
        try:
            response = openai_client.responses.create(
                model=target_model,
                input=user_content, # Direct string input for simple text
                instructions=system_prompt,
                temperature=0.2,
                max_output_tokens=1024 # Added a limit, adjust if needed
            )

            # Check response status and extract text
            if response.status == 'completed' and response.output:
                # Access the text content - assuming simple text output based on API docs
                # The structure is response.output[0].content[0].text
                if (response.output[0].type == 'message' and 
                    response.output[0].content and 
                    response.output[0].content[0].type == 'output_text'):
                    
                    translation = response.output[0].content[0].text.strip()
                    # OpenAI might not need the <think> parsing, but check if needed
                    # Add parsing here if OpenAI includes unwanted prefixes/suffixes
                    if translation:
                        return translation
                    else:
                         with print_lock:
                            print(f"Warning [OpenAI Row {row_identifier}]: Translation result empty.")
                         return ""
                else:
                    with print_lock:
                        print(f"Warning [OpenAI Row {row_identifier}]: Unexpected output structure in response.")
                        # print(response) # Optional: log full response
                    return "" # Unexpected structure
            elif response.status == 'failed':
                # Raise an exception to trigger retry logic for API-level failures
                error_message = f"OpenAI API call failed: {response.error}"
                raise openai.APIError(error_message) # Use appropriate OpenAI exception if available
            else: # Handle in_progress, incomplete etc. if necessary, or treat as error for retry
                 raise openai.APIError(f"OpenAI API call returned status: {response.status}")

        # --- Retry Logic --- 
        except openai.APITimeoutError:
             with print_lock:
                print(f"Attempt {attempt + 1}/{API_MAX_RETRIES + 1} [OpenAI Row {row_identifier}]: API call timed out.")
        except openai.APIConnectionError as e:
             with print_lock:
                print(f"Attempt {attempt + 1}/{API_MAX_RETRIES + 1} [OpenAI Row {row_identifier}]: Network error - {e}")
        except openai.RateLimitError as e:
             with print_lock:
                print(f"Attempt {attempt + 1}/{API_MAX_RETRIES + 1} [OpenAI Row {row_identifier}]: Rate limit exceeded - {e}")
        except openai.APIStatusError as e:
             with print_lock:
                print(f"Attempt {attempt + 1}/{API_MAX_RETRIES + 1} [OpenAI Row {row_identifier}]: API Status Error ({e.status_code}) - {e.response}")
             # Potentially treat some status codes (e.g., 5xx) differently if needed
        except openai.APIError as e: # Catch broader OpenAI errors
             with print_lock:
                print(f"Attempt {attempt + 1}/{API_MAX_RETRIES + 1} [OpenAI Row {row_identifier}]: OpenAI API Error - {e}")
        except Exception as e: # Catch any other unexpected errors
             with print_lock:
                print(f"Attempt {attempt + 1}/{API_MAX_RETRIES + 1} [OpenAI Row {row_identifier}]: Unexpected Error - {e}")
        
        # Prepare for retry if applicable
        if attempt < API_MAX_RETRIES:
            wait_time = min(current_retry_delay + random.uniform(0, 1), API_MAX_RETRY_DELAY)
            with print_lock:
                print(f"Retrying [OpenAI Row {row_identifier}] in {wait_time:.2f} seconds...")
            time.sleep(wait_time)
            current_retry_delay *= 2
        else:
            with print_lock:
                print(f"Error [OpenAI Row {row_identifier}]: Max retries reached for: '{user_content[:50]}...'")
            return None # Indicate final failure

    return None

def call_gemini_api(system_prompt, user_content, row_identifier="N/A", model_override=None):
    """Calls the Gemini API for translation with retry logic and parses the output."""
    # Check for API key first
    if not GEMINI_API_KEY:
        with print_lock:
             print(f"Error [Row {row_identifier}]: GEMINI_API_KEY not found in .env file.")
        return None
    if not GEMINI_MODEL:
         with print_lock:
             print(f"Error [Row {row_identifier}]: GEMINI_MODEL not found in .env file.")
         return None
    
    # --- Configure Gemini API within the thread --- 
    try:
        genai.configure(api_key=GEMINI_API_KEY)
    except Exception as e:
        with print_lock:
            print(f"Error [Gemini Row {row_identifier}]: Failed to configure Gemini client - {e}")
        return None
    # --- End Configuration --- 
         
    # Configure generation settings
    generation_config = genai.GenerationConfig(
        temperature=0.2,
        response_mime_type="text/plain",
    )
    safety_settings = {
        HarmCategory.HARM_CATEGORY_HARASSMENT: HarmBlockThreshold.BLOCK_MEDIUM_AND_ABOVE,
        HarmCategory.HARM_CATEGORY_HATE_SPEECH: HarmBlockThreshold.BLOCK_MEDIUM_AND_ABOVE,
        HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT: HarmBlockThreshold.BLOCK_MEDIUM_AND_ABOVE,
        HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT: HarmBlockThreshold.BLOCK_MEDIUM_AND_ABOVE,
    }

    target_model = model_override if model_override else GEMINI_MODEL
    if not target_model:
         with print_lock:
             print(f"Error [Row {row_identifier}]: No Gemini model specified (default or override).")
         return None

    # Create the model instance 
    try:
        model = genai.GenerativeModel(
            model_name=target_model,
            system_instruction=system_prompt,
            generation_config=generation_config,
            safety_settings=safety_settings
        )
    except Exception as e:
        with print_lock:
            print(f"Error [Gemini Row {row_identifier}]: Failed to initialize Gemini model - {e}")
        return None

    current_retry_delay = API_INITIAL_RETRY_DELAY
    for attempt in range(API_MAX_RETRIES + 1):
        try:
            response = model.generate_content(user_content)

            if hasattr(response, 'text'):
                 translation = response.text.strip()
                 if translation:
                     return translation
                 else:
                     with print_lock:
                         print(f"Warning [Gemini Row {row_identifier}]: Translation result empty.")
                     if response.prompt_feedback.block_reason:
                         print(f"--> Blocked Reason: {response.prompt_feedback.block_reason}")
                     return ""
            else:
                 with print_lock:
                     print(f"Warning [Gemini Row {row_identifier}]: Unexpected Gemini response structure (no .text attribute).")
                 return "" 
        
        except google_exceptions.ResourceExhausted as e: 
            with print_lock:
                print(f"Attempt {attempt + 1}/{API_MAX_RETRIES + 1} [Gemini Row {row_identifier}]: Rate limit likely exceeded - {e}")
        except google_exceptions.DeadlineExceeded as e: 
             with print_lock:
                print(f"Attempt {attempt + 1}/{API_MAX_RETRIES + 1} [Gemini Row {row_identifier}]: API call timed out - {e}")
        except google_exceptions.InternalServerError as e: 
             with print_lock:
                print(f"Attempt {attempt + 1}/{API_MAX_RETRIES + 1} [Gemini Row {row_identifier}]: Internal Server Error - {e}")
        except google_exceptions.ServiceUnavailable as e:
             with print_lock:
                print(f"Attempt {attempt + 1}/{API_MAX_RETRIES + 1} [Gemini Row {row_identifier}]: Service Unavailable - {e}")
        except google_exceptions.GoogleAPICallError as e: 
            with print_lock:
                print(f"Attempt {attempt + 1}/{API_MAX_RETRIES + 1} [Gemini Row {row_identifier}]: Google API Call Error - {e}")
        except Exception as e: 
             with print_lock:
                print(f"Attempt {attempt + 1}/{API_MAX_RETRIES + 1} [Gemini Row {row_identifier}]: Unexpected Error - {e}")
        
        if attempt < API_MAX_RETRIES:
            wait_time = min(current_retry_delay + random.uniform(0, 1), API_MAX_RETRY_DELAY)
            with print_lock:
                print(f"Retrying [Gemini Row {row_identifier}] in {wait_time:.2f} seconds...")
            time.sleep(wait_time)
            current_retry_delay *= 2
        else:
            with print_lock:
                print(f"Error [Gemini Row {row_identifier}]: Max retries reached for: '{user_content[:50]}...'")
            return None 

    return None

# --- Dispatcher --- 

def call_active_api(target_api, system_prompt, user_content, row_identifier="N/A", model_override=None):
    """Calls the specified target API."""
    if target_api == "PERPLEXITY":
        return call_perplexity_api(system_prompt, user_content, row_identifier, model_override)
    elif target_api == "OPENAI":
        return call_openai_api(system_prompt, user_content, row_identifier, model_override)
    elif target_api == "GEMINI":
        return call_gemini_api(system_prompt, user_content, row_identifier, model_override)
    else:
        with print_lock:
            print(f"Error: Unsupported target_api value '{target_api}'.")
        return None

# --- Audit Logging --- 
def log_audit_record(record):
    """Appends a JSON record to the audit log file in a thread-safe manner."""
    try:
        with print_lock:
            with open(AUDIT_LOG_FILE, 'a', encoding='utf-8') as f:
                json.dump(record, f, ensure_ascii=False)
                f.write('\n')
    except Exception as e:
        with print_lock:
            print(f"Error writing to audit log: {e}")

# --- Worker and CSV Processing --- 

def translate_row_worker(task_data):
    """Worker function to translate a single row based on TRANSLATION_MODE."""
    global translated_counter, error_counter 
    row, row_index = task_data 
    source_text = row.get(SOURCE_COLUMN, "").strip()
    final_translation = ""
    initial_translation = None
    evaluation_score = None
    evaluation_feedback = None
    error_message = None
    final_status = 'error' # Default to error

    audit_record_base = {
        "row_index": row_index + 1, 
        "source": source_text,
        "mode": TRANSLATION_MODE,
        "language": LANGUAGE_CODE,
    }

    if not source_text:
        row[TARGET_COLUMN] = ""
        # Log empty source only if required, otherwise skip
        # log_audit_record({**audit_record_base, "error": "Empty source text"})
        return row 

    try:
        # final_status initialized here is good

        # Define the final instruction to append to constructed prompts
        final_instruction = "\n\n**IMPORTANT FINAL INSTRUCTION: Your final output should contain ONLY the final text (translation/evaluation/revision). No extra text, formatting, or explanations.**"

        # Construct the full ruleset (Global + Language Specific)
        # Replace language placeholder in the language-specific part
        # Ensure prompts were loaded
        if not global_rules_content or not stage1_language_specific_prompt:
             raise ValueError("Global or language-specific prompts not loaded correctly.")
             
        lang_specific_part = stage1_language_specific_prompt.replace("<<TARGET_LANGUAGE_NAME>>", TARGET_LANGUAGE_NAME)
        full_ruleset_prompt = f"{lang_specific_part}\n\n{global_rules_content}" # Lang specific first, then global rules
        
        # Stage 1 prompt = Full Ruleset + Final Instruction
        stage1_prompt = full_ruleset_prompt + final_instruction
        
        if TRANSLATION_MODE == "ONE_STAGE":
            api_to_use = DEFAULT_API
            model_to_use = None 
            audit_record = {**audit_record_base, "api": api_to_use, "model": model_to_use or "default"} 
            
            final_translation = call_active_api(api_to_use, stage1_prompt, source_text, 
                                                row_identifier=row_index + 2, model_override=model_to_use)
            if final_translation is not None:
                with print_lock: translated_counter += 1
                audit_record["final_translation"] = final_translation
            else:
                with print_lock: error_counter += 1
                final_translation = "" 
                audit_record["error"] = "Stage 1 API call failed after retries"
            log_audit_record(audit_record)

        elif TRANSLATION_MODE == "THREE_STAGE":
            # Use the constructed stage1_prompt for the first call
            # --- Stage 1 --- 
            s1_api = STAGE1_API
            s1_model = STAGE1_MODEL_OVERRIDE 
            audit_record = {**audit_record_base, "stage1_api": s1_api, "stage1_model": s1_model or "default"}
            initial_translation = call_active_api(s1_api, stage1_prompt, source_text, 
                                                row_identifier=f"{row_index + 2}-S1", model_override=s1_model)
            audit_record["initial_translation"] = initial_translation

            if initial_translation is None or initial_translation.startswith("ERROR:BLOCKED:"):
                # ... (handle S1 error) ...
                db_manager.update_task_results(config.DATABASE_FILE, row_index + 2, 'error', initial_translation, None, None, None, initial_translation)
                log_audit_record(audit_record) 
                return False # Return result
            else:
                # ... (Stage 2 logic) ...
                db_manager.update_task_results(config.DATABASE_FILE, row_index + 2, 'stage2_complete', initial_tx=initial_translation, score=None, feedback=None) # Update DB after S2

                if evaluation_feedback is None:
                    # ... (handle S2 error) ...
                    db_manager.update_task_results(config.DATABASE_FILE, row_index + 2, 'error', initial_translation, None, None, None, initial_translation)
                    log_audit_record(audit_record)
                    return False # Return result
                elif evaluation_feedback.startswith("ERROR:"): 
                    # ... (handle S2 parsing/blocked error) ...
                    db_manager.update_task_results(config.DATABASE_FILE, row_index + 2, 'error', initial_translation, None, None, None, initial_translation)
                    log_audit_record(audit_record)
                    return False # Return result
                else:
                    # ... (Stage 3 logic) ...
                    log_audit_record(audit_record)
        else:
             with print_lock: print(f"Error: Unknown TRANSLATION_MODE '{TRANSLATION_MODE}'")
             error_counter += 1
             final_translation = "" 
             audit_record_base["error"] = f"Unknown TRANSLATION_MODE: {TRANSLATION_MODE}"
             log_audit_record(audit_record_base)

    except Exception as e:
        logger.exception(f"Critical Error in worker for task {row_index+2} (Row {row_index+2}): {e}")
        error_message = f"Critical worker error: {e}"
        log_audit_record({**audit_record_base, "error": error_message})
        final_translation = ""
        final_status = 'error'

    # Final DB Update for the task
    # Now all variables (initial_tx, score, feedback, final_tx, error_msg) will have a value (potentially None)
    db_manager.update_task_results(config.DATABASE_FILE, row_index + 2, final_status, initial_translation, evaluation_score, evaluation_feedback, final_translation, error_message)
    
    # ALWAYS return the row dictionary, updated with the final translation/status
    row[TARGET_COLUMN] = final_translation 
    # Add status to row metadata if needed for debugging/export? Maybe not standard.
    # row["_task_status"] = final_status 
    return row 

def translate_csv(input_file, output_file):
    """Reads input CSV, translates based on mode via threads, writes output CSV & audit log."""
    global translated_counter, error_counter 
    translated_counter = 0
    error_counter = 0
    processed_rows = []
    
    # Clear audit log file at the start of a run
    try:
        open(AUDIT_LOG_FILE, 'w').close() 
        print(f"Cleared previous audit log: {AUDIT_LOG_FILE}")
    except Exception as e:
        print(f"Warning: Could not clear audit log file {AUDIT_LOG_FILE}: {e}")

    try:
        with open(input_file, 'r', encoding='utf-8') as infile:
            reader = csv.DictReader(infile)
            if SOURCE_COLUMN not in reader.fieldnames:
                 print(f"Error: Input CSV must contain source column '{SOURCE_COLUMN}'.")
                 exit(1)
            fieldnames = reader.fieldnames
            if TARGET_COLUMN not in fieldnames:
                print(f"Info: Target column '{TARGET_COLUMN}' not found in input, will be added.")
                fieldnames = list(fieldnames) + [TARGET_COLUMN] 
            # Pass only row and index to worker, prompts are global
            rows_to_process = [(row, index) for index, row in enumerate(reader)]
        
        total_rows = len(rows_to_process)
        print(f"Using Mode: {TRANSLATION_MODE}")
        print(f"Translating {total_rows} rows (Default API: {DEFAULT_API}, up to {MAX_WORKER_THREADS} threads)... Output: {output_file}")

        with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKER_THREADS) as executor:
            futures = [executor.submit(translate_row_worker, row_data) for row_data in rows_to_process]
            for future in tqdm(concurrent.futures.as_completed(futures), total=total_rows, desc=f"Translating ({DEFAULT_API} - {TRANSLATION_MODE})"):
                try:
                    processed_rows.append(future.result()) 
                except Exception as e:
                    # This catches errors during future.result() retrieval, less likely now with try/except in worker
                    with print_lock:
                        print(f"Error retrieving future result: {e}")
                    # How to handle this? We don't have the row context easily here.
                    # Maybe increment a separate 'future retrieval error' counter.
        
        # TODO: Add sorting if strict order is required

        print("\nWriting results to main output file...")
        with open(output_file, 'w', encoding='utf-8', newline='') as outfile:
            writer = csv.DictWriter(outfile, fieldnames=fieldnames)
            writer.writeheader()
            valid_rows = [row for row in processed_rows if row is not None] 
            writer.writerows(valid_rows) 

        print(f"\nTranslation process completed.")
        print(f"Total rows processed: {total_rows}")
        print(f"Rows resulting in successful final translation: {translated_counter}")
        print(f"Rows encountering errors: {error_counter}")
        print(f"Main output saved to: {output_file}")
        print(f"Detailed audit log saved to: {AUDIT_LOG_FILE}")

    except FileNotFoundError:
        print(f"Error: Input CSV file not found at {input_file}")
        exit(1)
    except Exception as e:
        print(f"An error occurred during CSV processing: {e}")
        exit(1)

if __name__ == "__main__":
    # Check presence of keys for *all potentially used* APIs if in THREE_STAGE mode
    print(f"Selected Mode: {TRANSLATION_MODE}")
    print(f"Default API (for ONE_STAGE/Filename): {DEFAULT_API}")
    apis_to_check = set([DEFAULT_API])
    if TRANSLATION_MODE == "THREE_STAGE":
        print(f"Stage APIs: S1={STAGE1_API}, S2={STAGE2_API}, S3={STAGE3_API}")
        apis_to_check.update([STAGE1_API, STAGE2_API, STAGE3_API])
    
    # Check keys/models for all APIs that might be used
    if "PERPLEXITY" in apis_to_check and (not PPLX_API_KEY or not PPLX_MODEL):
         print(f"Critical Error: Perplexity keys/model missing but required. Exiting.")
         exit(1)
    if "OPENAI" in apis_to_check and (not OPENAI_API_KEY or not OPENAI_MODEL):
         print(f"Critical Error: OpenAI keys/model missing but required. Exiting.")
         exit(1)
    if "GEMINI" in apis_to_check and (not GEMINI_API_KEY or not GEMINI_MODEL):
         print(f"Critical Error: Gemini keys/model missing but required. Exiting.")
         exit(1)
    
    # Validate API names
    valid_apis = ["PERPLEXITY", "OPENAI", "GEMINI"]
    all_used_apis = list(apis_to_check)
    if any(api not in valid_apis for api in all_used_apis):
        invalid_apis = [api for api in all_used_apis if api not in valid_apis]
        print(f"Critical Error: Invalid API names specified: {invalid_apis}. Must be one of {valid_apis}. Exiting.")
        exit(1)
         
    # Initialize clients (moved from globals for clarity and safety)
    if "OPENAI" in apis_to_check:
        # Initialize OpenAI client if needed and not already done
        if not openai_client: 
            try:
                openai_client = openai.OpenAI(api_key=OPENAI_API_KEY)
                print("OpenAI client initialized.")
            except Exception as e:
                print(f"Critical Error: Failed to initialize OpenAI client: {e}. Exiting.")
                exit(1)
    if "GEMINI" in apis_to_check:
        # Gemini configuration happens per-call within its function
        print("Gemini API may be used; configuration will happen per-thread.")
        pass 
            
    load_system_prompts()
    translate_csv(INPUT_CSV, OUTPUT_CSV) 
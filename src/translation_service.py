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
import importlib

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

def call_openai_api(openai_client_obj, system_prompt, user_content, row_identifier="N/A", model_override=None):
    """Calls the OpenAI Responses API for translation with retry logic and parses the output."""
    target_model = model_override if model_override else config.OPENAI_MODEL
    # Use passed client object
    if not openai_client_obj or not target_model:
        with print_lock: logger.error(f"[OpenAI Row {row_identifier}]: Client object not provided or Model missing.")
        return None

    current_retry_delay = API_INITIAL_RETRY_DELAY
    for attempt in range(API_MAX_RETRIES + 1):
        try:
            response = openai_client_obj.responses.create(
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

def call_active_api(target_api, system_prompt, user_content, row_identifier="N/A", model_override=None, openai_client_obj=None):
    """Calls the specified target API, passing necessary client."""
    if target_api == "PERPLEXITY":
        return call_perplexity_api(system_prompt, user_content, row_identifier, model_override)
    elif target_api == "OPENAI":
        return call_openai_api(openai_client_obj, system_prompt, user_content, row_identifier, model_override)
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

# --- Translation Worker (called by thread pool within run_batch_background) --- 
def translate_row_worker(task_data, worker_config):
    """Worker function for a single translation task. 
       Performs translation and UPDATES THE DATABASE directly. 
       Returns True on success, False on error for progress tracking."""
    # Expand task_data dictionary
    task_id = task_data['task_id']
    batch_id = task_data['batch_id'] 
    row_index = task_data['row_index_in_file']
    lang_code = task_data['language_code']
    source_text = task_data['source_text']
    row_identifier = f"{row_index+1}-{lang_code}"
    
    # Get config values passed from process_batch
    db_path = worker_config['db_path']
    translation_mode = worker_config['translation_mode']
    default_api = worker_config['default_api']
    stage1_api_cfg = worker_config['stage1_api']
    stage2_api_cfg = worker_config['stage2_api']
    stage3_api_cfg = worker_config['stage3_api']
    stage1_model_override = worker_config['stage1_model']
    stage2_model_override = worker_config['stage2_model']
    stage3_model_override = worker_config['stage3_model']
    target_column_tpl = worker_config['target_column_tpl']
    openai_client_obj = worker_config.get('openai_client')

    db_manager.update_task_status(db_path, task_id, 'running')
    
    # Initialize local results variables
    final_translation_local = ""
    initial_translation_local = None 
    evaluation_score_local = None
    evaluation_feedback_local = None
    error_message_local = None
    final_status = 'error' 
    
    audit_record_base = {"task_id": task_id, "row_index": task_data['row_index_in_file'] + 1, "language": lang_code, "mode": worker_config['translation_mode']}

    if not source_text:
        logger.warning(f"Task {task_id} (Row {row_identifier}): Source text empty.")
        final_status = 'completed'
        error_message_local = "Empty source text"
        audit_record = {**audit_record_base, "error": error_message_local, "final_translation": ""}
        # Update DB directly
        db_manager.update_task_results(db_path, task_id, final_status, final_tx="", error_msg=error_message_local)
        log_audit_record(audit_record)
        return True # Indicate success (completed handling empty source)
        
    try:
        # final_status initialized inside try block
        # final_status = 'error' # Already initialized outside try
        
        # Define the final instruction
        final_instruction = "\n\n**IMPORTANT FINAL INSTRUCTION: Your final output should contain ONLY the final text (translation/evaluation/revision). No extra text, formatting, or explanations.**"
        
        # Construct the full ruleset (Global + Language Specific)
        lang_specific_part = prompt_manager.stage1_templates.get(lang_code)
        if not lang_specific_part:
             raise ValueError(f"Language specific prompt for {lang_code} not loaded.")
        lang_name = config.LANGUAGE_NAME_MAP.get(lang_code, lang_code)
        lang_specific_part = lang_specific_part.replace("<<TARGET_LANGUAGE_NAME>>", lang_name)
        full_ruleset_prompt = f"{lang_specific_part}\n\n{prompt_manager.global_rules_content}" # Use imported global rules
        
        # Stage 1 prompt = Full Ruleset + Final Instruction
        stage1_prompt = full_ruleset_prompt + final_instruction
        
        if translation_mode == "ONE_STAGE":
            api_to_use = default_api
            model_to_use = None 
            audit_record = {**audit_record_base, "api": api_to_use, "model": model_to_use or "default"} 
            final_translation_local = call_active_api(api_to_use, stage1_prompt, source_text, 
                                                row_identifier=row_identifier, model_override=model_to_use,
                                                openai_client_obj=openai_client_obj)
            if final_translation_local is not None:
                final_status = 'completed'
                audit_record["final_translation"] = final_translation_local
            else:
                error_message_local = "Stage 1 API call failed" 
                audit_record["error"] = error_message_local
                final_status = 'error'
            log_audit_record(audit_record)

        elif translation_mode == "THREE_STAGE":
            # --- Stage 1 --- 
            s1_api = stage1_api_cfg
            s1_model = stage1_model_override 
            audit_record = {**audit_record_base, "stage1_api": s1_api, "stage1_model": s1_model or "default"}
            initial_translation_local = call_active_api(s1_api, stage1_prompt, source_text, 
                                                row_identifier=f"{row_identifier}-S1", model_override=s1_model,
                                                openai_client_obj=openai_client_obj)
            audit_record["initial_translation"] = initial_translation_local
            # Update DB after S1
            db_manager.update_task_results(db_path, task_id, 'stage1_complete', initial_tx=initial_translation_local)

            if initial_translation_local is None or initial_translation_local.startswith("ERROR:BLOCKED:"):
                error_message_local = initial_translation_local or "Stage 1 failed"
                final_translation_local = "" 
                audit_record["error"] = error_message_local
                log_audit_record(audit_record) 
                final_status = 'error' 
            else:
                # --- Stage 2 --- 
                s2_api = stage2_api_cfg
                s2_model = stage2_model_override
                audit_record["stage2_api"] = s2_api
                audit_record["stage2_model"] = s2_model or "default"
                stage2_prompt = prompt_manager.get_full_prompt(stage=2, lang_code=lang_code, source_text=source_text, initial_translation=initial_translation_local)
                eval_user_content = "Evaluate the provided translation based on the rules and context."
                evaluation_raw = call_active_api(s2_api, stage2_prompt, eval_user_content, 
                                           row_identifier=f"{row_identifier}-S2", model_override=s2_model,
                                           openai_client_obj=openai_client_obj)
                
                evaluation_score_local = None
                evaluation_feedback_local = None
                if evaluation_raw and not evaluation_raw.startswith("ERROR:BLOCKED:"):
                    # ... (JSON cleaning and parsing logic) ...
                    cleaned_json_str = evaluation_raw.strip() 
                    if cleaned_json_str.startswith("```json"): cleaned_json_str = cleaned_json_str[7:]
                    elif cleaned_json_str.startswith("```"): cleaned_json_str = cleaned_json_str[3:]
                    if cleaned_json_str.endswith("```"): cleaned_json_str = cleaned_json_str[:-3]
                    cleaned_json_str = cleaned_json_str.strip()
                    try:
                        eval_data = json.loads(cleaned_json_str)
                        score = eval_data.get("score")
                        feedback = eval_data.get("feedback")
                        if isinstance(score, str) and score.isdigit(): score = int(score)
                        if isinstance(score, int) and isinstance(feedback, str) and 1 <= score <= 10:
                            evaluation_score_local = score
                            evaluation_feedback_local = feedback
                        else: raise ValueError("Invalid types/range/format in evaluation JSON")
                    except Exception as e:
                        logger.warning(f"[Row {row_identifier}-S2]: Failed parsing eval JSON ({e}). Cleaned: '{cleaned_json_str[:50]}...'")
                        evaluation_feedback_local = f"ERROR:PARSING:{e}"
                elif evaluation_raw and evaluation_raw.startswith("ERROR:BLOCKED:"):
                    evaluation_feedback_local = evaluation_raw 
                else: 
                    evaluation_feedback_local = None # API call failed

                audit_record["evaluation_score"] = evaluation_score_local
                audit_record["evaluation_feedback"] = evaluation_feedback_local
                # Update DB after S2
                db_manager.update_task_results(db_path, task_id, 'stage2_complete', initial_tx=initial_translation_local, score=evaluation_score_local, feedback=evaluation_feedback_local)

                if evaluation_feedback_local is None or evaluation_feedback_local.startswith("ERROR:"):
                    final_translation_local = initial_translation_local # Fallback
                    error_message_local = evaluation_feedback_local or "Stage 2 failed"
                    audit_record["error"] = error_message_local
                    audit_record["final_translation"] = final_translation_local 
                    log_audit_record(audit_record)
                    final_status = 'completed' # Completed with fallback
                else:
                    # --- Stage 3 --- 
                    s3_api = stage3_api_cfg
                    s3_model = stage3_model_override
                    audit_record["stage3_api"] = s3_api
                    audit_record["stage3_model"] = s3_model or "default"
                    stage3_prompt = prompt_manager.get_full_prompt(stage=3, lang_code=lang_code, source_text=source_text, initial_translation=initial_translation_local, feedback=evaluation_feedback_local)
                    refine_user_content = "Revise the translation based on the provided context and feedback."
                    final_translation_local = call_active_api(s3_api, stage3_prompt, refine_user_content, 
                                                      row_identifier=f"{row_identifier}-S3", model_override=s3_model,
                                                      openai_client_obj=openai_client_obj)
                    audit_record["final_translation"] = final_translation_local
                    
                    if final_translation_local is None or final_translation_local.startswith("ERROR:BLOCKED:"):
                        error_message_local = final_translation_local or "Stage 3 failed"
                        final_translation_local = initial_translation_local # Fallback
                        audit_record["error"] = error_message_local
                        audit_record["final_translation"] = final_translation_local
                        final_status = 'completed' # Completed with fallback
                    else:
                         final_status = 'completed'
                    log_audit_record(audit_record)
        else:
             error_message_local = f"Unknown TRANSLATION_MODE: {translation_mode}"
             logger.error(error_message_local)
             log_audit_record({**audit_record_base, "error": error_message_local})
             final_translation_local = ""
             final_status = 'error'

    except Exception as e:
        logger.exception(f"Critical Error in worker for task {task_id} (Row {row_identifier}): {e}")
        error_message_local = f"Critical worker error: {e}"
        log_audit_record({**audit_record_base, "error": error_message_local})
        final_translation_local = ""
        final_status = 'error' 

    # Final DB Update 
    # Update with all collected local results for this task
    db_manager.update_task_results(db_path, task_id, final_status, initial_translation_local, evaluation_score_local, evaluation_feedback_local, final_translation_local, error_message_local)
    
    # Return simple status for thread pool progress tracking
    return final_status == 'completed' 

# --- Background Batch Processing Function (Target for Flask's Thread) ---
def run_batch_background(batch_id):
    """Processes all pending tasks for a given batch_id using thread pool.
       This function runs in a background thread started by Flask."""
    logger.info(f"BACKGROUND THREAD: Starting processing for batch_id: {batch_id}")
    db_path = config.DATABASE_FILE 
    
    # Retrieve batch config from DB - needed for worker config
    batch_info = db_manager.get_batch_info(db_path, batch_id)
    if not batch_info:
        logger.error(f"BACKGROUND THREAD: Cannot process batch: Batch info not found for {batch_id}")
        return # Exit thread
    try:
        batch_config = json.loads(batch_info['config_details'])
    except Exception as e:
        logger.error(f"BACKGROUND THREAD: Cannot process batch {batch_id}: Failed to parse config_details - {e}")
        db_manager.update_batch_status(db_path, batch_id, 'failed')
        return # Exit thread
        
    tasks_to_process = db_manager.get_pending_tasks(db_path, batch_id)
    total_tasks = len(tasks_to_process)
    if total_tasks == 0:
        logger.info(f"BACKGROUND THREAD: No pending tasks found for batch_id: {batch_id}")
        db_manager.update_batch_status(db_path, batch_id, 'completed_empty')
        return # Exit thread

    logger.info(f"BACKGROUND THREAD: Processing {total_tasks} tasks for batch {batch_id} (Mode: {batch_config.get('mode')}) using up to {config.MAX_WORKER_THREADS} threads...")
    # Update DB status to processing (already done by prepare_batch, but can confirm)
    db_manager.update_batch_status(db_path, batch_id, 'processing') 
    
    success_count = 0
    error_count = 0
    
    # Prepare worker_config based on the stored batch config
    # Retrieve initialized client if needed
    openai_client_thread = api_clients.get_openai_client() # Get client instance for this thread
    worker_config = {
        'db_path': db_path,
        'translation_mode': batch_config.get('mode', 'ONE_STAGE'),
        'default_api': batch_config.get('default_api'),
        'stage1_api': batch_config.get('s1_api'),
        'stage2_api': batch_config.get('s2_api'),
        'stage3_api': batch_config.get('s3_api'),
        'stage1_model': batch_config.get('s1_model'),
        'stage2_model': batch_config.get('s2_model'),
        'stage3_model': batch_config.get('s3_model'),
        'target_column_tpl': config.TARGET_COLUMN_TPL,
        'openai_client': openai_client_thread, # Pass client object
    }
    
    processed_task_results = {} 

    # Use ThreadPoolExecutor for row-level parallelism within this background thread
    with concurrent.futures.ThreadPoolExecutor(max_workers=config.MAX_WORKER_THREADS) as executor:
        future_to_task_id = {executor.submit(translate_row_worker, dict(task), worker_config): task['task_id'] for task in tasks_to_process}
        
        for future in tqdm(concurrent.futures.as_completed(future_to_task_id), total=total_tasks, desc=f"Translating Batch {batch_id}"):
            task_id = future_to_task_id[future]
            try:
                # Worker now returns True for success (incl. fallback), False for error
                completed_successfully = future.result() 
                if completed_successfully:
                    success_count += 1
                else:
                    error_count += 1 # Worker handled logging/DB for specific error
            except Exception as exc:
                # Find original task info for logging
                original_task = next((t for t in tasks_to_process if t['task_id'] == task_id), None)
                row_idx = original_task['row_index_in_file']+1 if original_task else '?'
                lang = original_task['language_code'] if original_task else '??'
                logger.exception(f"Task Future (id={task_id}, row={row_idx}-{lang}) generated exception: {exc}")
                error_count += 1
                try: 
                    db_manager.update_task_status(db_path, task_id, 'error', f"Future execution error: {exc}")
                except Exception as db_exc:
                    logger.error(f"Failed to update task status to error after future exception: {db_exc}")

    logger.info(f"BACKGROUND THREAD: Batch {batch_id} processing finished in thread.")
    logger.info(f"BACKGROUND THREAD: Successfully completed tasks: {success_count}")
    logger.info(f"BACKGROUND THREAD: Tasks ending in error state: {error_count}")
    
    # Determine final batch status based on *task errors*
    final_batch_status = 'completed' if error_count == 0 else 'completed_with_errors' 
    db_manager.update_batch_status(db_path, batch_id, final_batch_status)
    logger.info(f"BACKGROUND THREAD: Final batch status {final_batch_status} updated in DB for {batch_id}.")
    # This function doesn't need to return data, it just updates the DB

# --- Input Processing --- 
def prepare_batch(input_file_path, original_filename, selected_languages, mode_config):
    """Parses input CSV, stores header/filename, creates DB entries."""
    db_path = config.DATABASE_FILE 
    batch_id = str(uuid.uuid4())
    # filename = os.path.basename(input_file_path) # Use original_filename for DB record
    logger.info(f"Preparing batch {batch_id} from file: {original_filename}")
    
    original_header = []
    tasks_to_create = []
    target_columns_in_file = set()

    try:
        with open(input_file_path, 'r', encoding='utf-8') as infile:
            reader = csv.DictReader(infile)
            original_header = reader.fieldnames or []
            if config.SOURCE_COLUMN not in original_header:
                logger.error(f"Source column '{config.SOURCE_COLUMN}' not found in {original_filename}")
                # Cannot proceed without source column
                return None 
            
            target_columns_in_file = {f for f in original_header if f.startswith("tg_")}
            metadata_columns = [f for f in original_header if not f.startswith("tg_") and f != config.SOURCE_COLUMN]

            for i, row in enumerate(reader):
                source_text = row.get(config.SOURCE_COLUMN, "").strip()
                metadata = {col: row.get(col, "") for col in metadata_columns} 
                metadata_json = json.dumps(metadata)
                
                for lang_code in selected_languages:
                    target_col_name = config.TARGET_COLUMN_TPL.format(lang_code=lang_code)
                    # Check language availability (prompt and column)
                    if lang_code in prompt_manager.stage1_templates and target_col_name in target_columns_in_file:
                         # Add task details to a list for bulk insertion later?
                         # For now, add one by one
                         db_manager.add_translation_task(db_path, batch_id, i, lang_code, source_text, metadata_json)
                         # tasks_to_create.append((batch_id, i, lang_code, source_text, metadata_json))
                    elif lang_code not in prompt_manager.stage1_templates:
                         logger.warning(f"Skipping language {lang_code} for row {i}: Lang code not available (missing prompt file?).")
    except Exception as e:
        logger.exception(f"Error processing input file {original_filename} for batch {batch_id}: {e}")
        # Don't create batch record if file processing fails
        return None

    # Check if any tasks were actually created
    tasks_added = db_manager.count_tasks_for_batch(db_path, batch_id) # Need this new DB function
    if tasks_added == 0:
        logger.warning(f"No valid tasks could be created for batch {batch_id}. Check file content, selected languages, columns, prompt files.")
        return None 
        
    # Store config snapshot *including the original header*
    config_snapshot_dict = {**mode_config, "original_header": original_header}
    config_snapshot_json = json.dumps(config_snapshot_dict)
    # Use original_filename when adding batch record
    db_manager.add_batch(db_path, batch_id, original_filename, config_snapshot_json)
        
    logger.info(f"Added {tasks_added} tasks for batch {batch_id}.")
    db_manager.update_batch_status(db_path, batch_id, 'pending') # Mark as ready for processing
    return batch_id

# --- Export --- #
def generate_export(batch_id, output_file_path):
    """Generates the output CSV file by fetching results from DB."""
    logger.info(f"Generating export for batch {batch_id} to {output_file_path}")
    db_path = config.DATABASE_FILE
    
    # Get original header from batch config
    batch_info = db_manager.get_batch_info(db_path, batch_id)
    original_header = []
    if batch_info and batch_info['config_details']:
        try:
            batch_config = json.loads(batch_info['config_details'])
            original_header = batch_config.get("original_header", [])
        except json.JSONDecodeError:
            logger.warning(f"Could not decode config details for batch {batch_id} to get header.")
    
    if not original_header:
         logger.error(f"Could not determine original header for batch {batch_id}. Cannot generate export.")
         return False
         
    # Fetch completed task data required for export
    tasks = db_manager.get_completed_tasks_for_export(db_path, batch_id)
    if not tasks:
        logger.warning(f"No task data found or retrieved for export for batch {batch_id}")
        # Proceed to write header anyway? Or return False?
        # Let's write an empty file with header.
        tasks = [] 

    # Reconstruct the data row by row, preserving original row order
    output_data_dict = {}

    for task_row in tasks:
        row_index = task_row['row_index_in_file']
        lang_code = task_row['language_code']
        translation = task_row['final_translation']
        metadata_json = task_row['metadata_json']
        
        try: metadata = json.loads(metadata_json or '{}')
        except: metadata = {}
            
        target_col = config.TARGET_COLUMN_TPL.format(lang_code=lang_code)

        if row_index not in output_data_dict:
            # Get source text from the first task encountered for this row
            # This assumes source text is consistent across tasks for the same row index
            first_task_for_row = db_manager.get_task_by_row_index(db_path, batch_id, row_index) 
            source = first_task_for_row['source_text'] if first_task_for_row else "SOURCE_NOT_FOUND"
            output_data_dict[row_index] = {config.SOURCE_COLUMN: source, **metadata}
        
        # Add the translation for the specific language
        output_data_dict[row_index][target_col] = translation

    # Convert dict to list ordered by row index
    output_data_list = [output_data_dict[idx] for idx in sorted(output_data_dict.keys())]
    
    try:
        os.makedirs(os.path.dirname(output_file_path), exist_ok=True)
        with open(output_file_path, 'w', encoding='utf-8', newline='') as outfile:
            # Use the original header order retrieved from the batch config
            writer = csv.DictWriter(outfile, fieldnames=original_header, extrasaction='ignore')
            writer.writeheader()
            # Write rows using original header order
            for row_dict in output_data_list:
                # Ensure all header columns exist, fill missing with ""
                row_to_write = {hdr: row_dict.get(hdr, "") for hdr in original_header}
                writer.writerow(row_to_write)
        logger.info(f"Export generated successfully: {output_file_path}")
        return True
    except Exception as e:
        logger.error(f"Error writing export file {output_file_path}: {e}")
        return False

def generate_stages_report(batch_id, output_file_path):
    """Generates a detailed CSV report showing results from each stage for THREE_STAGE mode."""
    logger.info(f"Generating detailed stages report for batch {batch_id} to {output_file_path}")
    db_path = config.DATABASE_FILE
    
    # Check if batch was actually run in three-stage mode?
    batch_info = db_manager.get_batch_info(db_path, batch_id)
    if not batch_info or not batch_info['config_details']:
        logger.error(f"Cannot generate stages report: Batch info not found for {batch_id}")
        return False
    try:
        batch_config = json.loads(batch_info['config_details'])
        if batch_config.get('mode') != 'THREE_STAGE':
            logger.warning(f"Skipping stages report for batch {batch_id}: Was not run in THREE_STAGE mode.")
            # Or should we generate it anyway? Let's skip for now.
            return False 
    except Exception as e:
         logger.error(f"Cannot generate stages report: Error reading batch config for {batch_id} - {e}")
         return False

    # Fetch all task data for the batch
    conn = db_manager.get_db_connection(db_path)
    try:
        cursor = conn.execute("""
            SELECT task_id, row_index_in_file, language_code, source_text, 
                   initial_translation, evaluation_score, evaluation_feedback, 
                   final_translation, status, error_message
            FROM TranslationTasks 
            WHERE batch_id = ? 
            ORDER BY row_index_in_file ASC, language_code ASC
            """, (batch_id,))
        tasks = cursor.fetchall()
    except Exception as e:
        logger.error(f"Failed to fetch tasks for stages report (batch {batch_id}): {e}")
        tasks = []
    finally:
        conn.close()

    if not tasks:
        logger.warning(f"No tasks found for stages report (batch {batch_id})")
        # Write empty file with header?

    # Define header for the report
    report_header = [
        'task_id', 'row_index', 'language', 'source', 
        'initial_translation', 'eval_score', 'eval_feedback', 
        'final_translation', 'final_status', 'error'
    ]

    try:
        os.makedirs(os.path.dirname(output_file_path), exist_ok=True)
        with open(output_file_path, 'w', encoding='utf-8', newline='') as outfile:
            writer = csv.DictWriter(outfile, fieldnames=report_header, extrasaction='ignore')
            writer.writeheader()
            for task_row in tasks:
                # Map DB columns to report header names
                row_dict = {
                    'task_id': task_row['task_id'],
                    'row_index': task_row['row_index_in_file'],
                    'language': task_row['language_code'],
                    'source': task_row['source_text'],
                    'initial_translation': task_row['initial_translation'],
                    'eval_score': task_row['evaluation_score'],
                    'eval_feedback': task_row['evaluation_feedback'],
                    'final_translation': task_row['final_translation'],
                    'final_status': task_row['status'],
                    'error': task_row['error_message']
                }
                writer.writerow(row_dict)
        logger.info(f"Stages report generated successfully: {output_file_path}")
        return True
    except Exception as e:
        logger.error(f"Error writing stages report file {output_file_path}: {e}")
        return False

# --- REMOVED translate_csv and __main__ block ---

# def process_batch(...): ... # Remove this old function
# if __name__ == "__main__": ... # Remove this block 
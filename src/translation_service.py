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

# --- Translation Worker --- 
def translate_row_worker(task_data, worker_config):
    """Worker function for a single translation task. Fetches data, translates, updates DB."""
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

    db_manager.update_task_status(db_path, task_id, 'running')
    
    # Initialize results variables
    final_translation = ""
    initial_translation = task_data.get('initial_translation') 
    evaluation_score = task_data.get('evaluation_score')
    evaluation_feedback = task_data.get('evaluation_feedback')
    error_message = None
    final_status = 'error' # Default to error
    
    audit_record_base = {"task_id": task_id, "row_index": row_index + 1, "language": lang_code, "mode": translation_mode}

    if not source_text:
        logger.warning(f"Task {task_id} (Row {row_identifier}): Source text empty.")
        final_status = 'completed'
        error_message = "Empty source text"
        audit_record = {**audit_record_base, "error": error_message, "final_translation": ""}
        db_manager.update_task_results(db_path, task_id, final_status, final_tx="", error_msg=error_message)
        log_audit_record(audit_record)
        # Return the original task data updated for merging
        task_data['final_translation'] = ""
        task_data['final_status'] = final_status
        return task_data 
        
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
            final_translation = call_active_api(api_to_use, stage1_prompt, source_text, 
                                                row_identifier=row_identifier, model_override=model_to_use)
            if final_translation is not None:
                final_status = 'completed'
                audit_record["final_translation"] = final_translation
            else:
                error_message = "Stage 1 API call failed after retries"
                final_translation = "" 
                audit_record["error"] = error_message
                final_status = 'error' # Ensure status is error
            log_audit_record(audit_record)

        elif translation_mode == "THREE_STAGE":
            # --- Stage 1 --- 
            s1_api = stage1_api_cfg
            s1_model = stage1_model_override 
            audit_record = {**audit_record_base, "stage1_api": s1_api, "stage1_model": s1_model or "default"}
            initial_translation = call_active_api(s1_api, stage1_prompt, source_text, 
                                                row_identifier=f"{row_identifier}-S1", model_override=s1_model)
            audit_record["initial_translation"] = initial_translation
            db_manager.update_task_results(db_path, task_id, 'stage1_complete', initial_tx=initial_translation) 

            if initial_translation is None or initial_translation.startswith("ERROR:BLOCKED:"):
                error_message = initial_translation if initial_translation else "Stage 1 API call failed after retries"
                final_translation = "" 
                audit_record["error"] = error_message
                log_audit_record(audit_record) 
                final_status = 'error' # Ensure status is error
            else:
                # --- Stage 2 --- 
                s2_api = stage2_api_cfg
                s2_model = stage2_model_override
                audit_record["stage2_api"] = s2_api
                audit_record["stage2_model"] = s2_model or "default"
                stage2_prompt = prompt_manager.get_full_prompt(stage=2, lang_code=lang_code, source_text=source_text, initial_translation=initial_translation)
                eval_user_content = "Evaluate the provided translation based on the rules and context."
                evaluation_raw = call_active_api(s2_api, stage2_prompt, eval_user_content, 
                                           row_identifier=f"{row_identifier}-S2", model_override=s2_model)
                
                evaluation_score = None
                evaluation_feedback = None
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
                            evaluation_score = score
                            evaluation_feedback = feedback
                        else: raise ValueError("Invalid types/range/format in evaluation JSON")
                    except Exception as e:
                        logger.warning(f"[Row {row_identifier}-S2]: Failed parsing eval JSON ({e}). Cleaned: '{cleaned_json_str[:50]}...'")
                        evaluation_feedback = f"ERROR:PARSING:{e}"
                elif evaluation_raw and evaluation_raw.startswith("ERROR:BLOCKED:"):
                    evaluation_feedback = evaluation_raw 
                else: 
                    evaluation_feedback = None # API call failed

                audit_record["evaluation_score"] = evaluation_score
                audit_record["evaluation_feedback"] = evaluation_feedback
                db_manager.update_task_results(db_path, task_id, 'stage2_complete', initial_tx=initial_translation, score=evaluation_score, feedback=evaluation_feedback) 

                if evaluation_feedback is None:
                    final_translation = initial_translation 
                    error_message = "Stage 2 API call failed, using Stage 1 result"
                    audit_record["error"] = error_message
                    audit_record["final_translation"] = final_translation 
                    log_audit_record(audit_record)
                    final_status = 'completed' # Completed with fallback
                elif evaluation_feedback.startswith("ERROR:"): 
                    final_translation = initial_translation 
                    error_message = f"Stage 2 failed ({evaluation_feedback}), using Stage 1 result"
                    audit_record["error"] = error_message
                    audit_record["final_translation"] = final_translation
                    log_audit_record(audit_record)
                    final_status = 'completed' # Completed with fallback 
                else:
                    # --- Stage 3 --- 
                    s3_api = stage3_api_cfg
                    s3_model = stage3_model_override
                    audit_record["stage3_api"] = s3_api
                    audit_record["stage3_model"] = s3_model or "default"
                    stage3_prompt = prompt_manager.get_full_prompt(stage=3, lang_code=lang_code, source_text=source_text, initial_translation=initial_translation, feedback=evaluation_feedback)
                    refine_user_content = "Revise the translation based on the provided context and feedback."
                    final_translation = call_active_api(s3_api, stage3_prompt, refine_user_content, 
                                                      row_identifier=f"{row_identifier}-S3", model_override=s3_model)
                    audit_record["final_translation"] = final_translation
                    
                    if final_translation is None or final_translation.startswith("ERROR:BLOCKED:"):
                        error_message = final_translation if final_translation else "Stage 3 API call failed, using Stage 1 result"
                        final_translation = initial_translation 
                        audit_record["error"] = error_message
                        audit_record["final_translation"] = final_translation
                        final_status = 'completed' # Completed with fallback
                    else:
                         final_status = 'completed'
                    log_audit_record(audit_record)
        else:
             error_message = f"Unknown TRANSLATION_MODE: {translation_mode}"
             logger.error(error_message)
             log_audit_record({**audit_record_base, "error": error_message})
             final_translation = ""
             final_status = 'error'

    except Exception as e:
        logger.exception(f"Critical Error in worker for task {task_id} (Row {row_identifier}): {e}")
        error_message = f"Critical worker error: {e}"
        log_audit_record({**audit_record_base, "error": error_message})
        final_translation = ""
        final_status = 'error' 

    # Final DB Update 
    db_manager.update_task_results(db_path, task_id, final_status, initial_translation, evaluation_score, evaluation_feedback, final_translation, error_message)
    
    # Return the updated task data dictionary for process_batch to handle
    task_data_copy = dict(task_data) 
    task_data_copy['final_translation'] = final_translation
    task_data_copy['final_status'] = final_status
    return task_data_copy 

# --- Batch Processing --- 
def process_batch(batch_id):
    """Processes all pending tasks for a given batch_id using thread pool."""
    logger.info(f"Starting processing for batch_id: {batch_id}")
    db_path = config.DATABASE_FILE # Get DB path once
    tasks_to_process = db_manager.get_pending_tasks(db_path, batch_id)
    total_tasks = len(tasks_to_process)
    if total_tasks == 0:
        # ... (handle no tasks) ...
        return [], True

    logger.info(f"Processing {total_tasks} tasks for batch {batch_id} using up to {config.MAX_WORKER_THREADS} threads...")
    db_manager.update_batch_status(db_path, batch_id, 'processing')
    
    success_count = 0
    error_count = 0
    
    # Prepare config dict to pass to workers
    worker_config = {
        'db_path': db_path,
        'translation_mode': config.TRANSLATION_MODE,
        'default_api': config.DEFAULT_API,
        'stage1_api': config.STAGE1_API,
        'stage2_api': config.STAGE2_API,
        'stage3_api': config.STAGE3_API,
        'stage1_model': config.STAGE1_MODEL_OVERRIDE,
        'stage2_model': config.STAGE2_MODEL_OVERRIDE,
        'stage3_model': config.STAGE3_MODEL_OVERRIDE,
        'target_column_tpl': config.TARGET_COLUMN_TPL
    }
    
    processed_task_results = {} # Store results keyed by task_id

    with concurrent.futures.ThreadPoolExecutor(max_workers=config.MAX_WORKER_THREADS) as executor:
        # Pass task data and worker_config
        future_to_task_id = {executor.submit(translate_row_worker, dict(task), worker_config): task['task_id'] for task in tasks_to_process}
        
        for future in tqdm(concurrent.futures.as_completed(future_to_task_id), total=total_tasks, desc=f"Translating Batch {batch_id}"):
            task_id = future_to_task_id[future]
            try:
                # Worker returns the updated task dictionary or None if skipped
                updated_task_data = future.result()
                if updated_task_data:
                    processed_task_results[task_id] = updated_task_data
                    if updated_task_data.get('final_status') == 'completed':
                        success_count += 1
                    else:
                        error_count += 1
                # else: worker returned None (e.g., skipped empty source), count neither?

            except Exception as exc:
                # Find original task info if needed for logging
                original_task = next((t for t in tasks_to_process if t['task_id'] == task_id), None)
                row_idx = original_task['row_index_in_file']+1 if original_task else '?'
                lang = original_task['language_code'] if original_task else '??'
                logger.exception(f"Task Future (id={task_id}, row={row_idx}-{lang}) generated exception: {exc}")
                error_count += 1
                try: 
                    db_manager.update_task_status(db_path, task_id, 'error', f"Future execution error: {exc}")
                except Exception as db_exc:
                    logger.error(f"Failed to update task status to error after future exception: {db_exc}")

    # ... (Logging results - unchanged) ...
    
    final_batch_status = 'completed' if error_count == 0 and success_count == total_tasks else 'completed_with_errors' # Adjust logic slightly
    db_manager.update_batch_status(db_path, batch_id, final_batch_status)
    
    # Reconstruct data for export/return using updated task results
    reconstructed_data_dict = {}
    all_metadata_keys = set()
    all_target_cols = set()
    original_tasks_dict = {t['task_id']: dict(t) for t in tasks_to_process}

    for task_id, result_data in processed_task_results.items():
        original_task = original_tasks_dict.get(task_id)
        if not original_task: continue # Should not happen

        row_index = original_task['row_index_in_file']
        lang_code = original_task['language_code']
        final_translation = result_data.get('final_translation', '') # Use result from worker
        metadata_json = original_task['metadata_json']
        
        try: metadata = json.loads(metadata_json or '{}')
        except: metadata = {}
        all_metadata_keys.update(metadata.keys())
        target_col = config.TARGET_COLUMN_TPL.format(lang_code=lang_code)
        all_target_cols.add(target_col)

        if row_index not in reconstructed_data_dict:
            reconstructed_data_dict[row_index] = {config.SOURCE_COLUMN: original_task['source_text'], **metadata}
        
        reconstructed_data_dict[row_index][target_col] = final_translation

    reconstructed_data_list = [reconstructed_data_dict[idx] for idx in sorted(reconstructed_data_dict.keys())]
    return reconstructed_data_list, error_count == 0

# --- Input Processing --- 
def prepare_batch(input_file_path, selected_languages):
    """Parses input CSV, creates DB entries for a new batch and tasks."""
    db_path = config.DATABASE_FILE # Get DB path
    batch_id = str(uuid.uuid4())
    filename = os.path.basename(input_file_path)
    logger.info(f"Preparing batch {batch_id} from file: {filename}")
    
    # Store config snapshot 
    config_snapshot = json.dumps({
        "mode": config.TRANSLATION_MODE,
        "languages": selected_languages,
        "default_api": config.DEFAULT_API,
        "s1_api": config.STAGE1_API, "s1_model": config.STAGE1_MODEL_OVERRIDE,
        "s2_api": config.STAGE2_API, "s2_model": config.STAGE2_MODEL_OVERRIDE,
        "s3_api": config.STAGE3_API, "s3_model": config.STAGE3_MODEL_OVERRIDE,
    })
    db_manager.add_batch(db_path, batch_id, filename, config_snapshot)

    tasks_added = 0
    target_columns_in_file = set() 
    try:
        with open(input_file_path, 'r', encoding='utf-8') as infile:
            reader = csv.DictReader(infile)
            fieldnames = reader.fieldnames or []
            if config.SOURCE_COLUMN not in fieldnames:
                logger.error(f"Source column '{config.SOURCE_COLUMN}' not found in {filename}")
                db_manager.update_batch_status(db_path, batch_id, 'failed')
                return None
            
            target_columns_in_file = {f for f in fieldnames if f.startswith("tg_")}
            metadata_columns = [f for f in fieldnames if not f.startswith("tg_") and f != config.SOURCE_COLUMN]

            for i, row in enumerate(reader):
                source_text = row.get(config.SOURCE_COLUMN, "").strip()
                metadata = {col: row.get(col, "") for col in metadata_columns} 
                metadata_json = json.dumps(metadata)
                
                for lang_code in selected_languages:
                    target_col_name = config.TARGET_COLUMN_TPL.format(lang_code=lang_code)
                    # Check if language is generally available AND target column exists
                    if lang_code in prompt_manager.stage1_templates and target_col_name in target_columns_in_file:
                         db_manager.add_translation_task(db_path, batch_id, i, lang_code, source_text, metadata_json)
                         tasks_added += 1
                    elif lang_code not in prompt_manager.stage1_templates:
                         logger.warning(f"Skipping language {lang_code} for row {i}: Lang code not available (missing prompt file?).")
                    # else: column not present, skip silently or log?
                        
    except Exception as e:
        logger.exception(f"Error processing input file {filename} for batch {batch_id}: {e}")
        db_manager.update_batch_status(db_path, batch_id, 'failed')
        return None

    if tasks_added == 0:
        logger.warning(f"No tasks added for batch {batch_id}. Check selected languages and input file columns/prompt files.")
        db_manager.update_batch_status(db_path, batch_id, 'completed_empty')
        return None 
        
    logger.info(f"Added {tasks_added} tasks for batch {batch_id}.")
    db_manager.update_batch_status(db_path, batch_id, 'pending') # Mark as pending, ready for processing
    return batch_id

# --- Export --- #
def generate_export(batch_id, output_path, processed_data):
    """DEPRECATED / Example: Generates export file from processed data."""
    # This function structure is more suitable for direct script execution.
    # For Streamlit, app.py will call prepare_batch and process_batch separately.
    logger.warning("generate_export function is deprecated for direct use, use prepare_batch and process_batch.")
    
    logger.info("Running export generation directly (Example Usage - Phase 1)")
    
    # 1. Generate Export (if successful)
    if processed_data: # Check if we got data back
         # Construct output path based on input filename and config
         output_filename = f"output_{os.path.splitext(os.path.basename(output_path))[0]}_{config.DEFAULT_API.lower()}_batch_{batch_id[:8]}.csv"
         output_path = os.path.join(config.OUTPUT_DIR, output_filename)
         with open(output_path, 'w', encoding='utf-8') as outfile:
             writer = csv.DictWriter(outfile, fieldnames=processed_data[0].keys())
             writer.writeheader()
             writer.writerows(processed_data)
         print(f"Export generation complete. Check DB ({config.DATABASE_FILE}) and audit log ({config.AUDIT_LOG_FILE})")
         print(f"Main output attempt saved to: {output_path}")
    else:
         print(f"Export generation completed but no data returned for export. Errors may have occurred for batch {batch_id}. Check logs and DB.")

def translate_csv(input_file, output_file):
    """DEPRECATED / Example: Reads input CSV, prepares batch, processes, exports."""
    # This function structure is more suitable for direct script execution.
    # For Streamlit, app.py will call prepare_batch and process_batch separately.
    logger.warning("translate_csv function is deprecated for direct use, use prepare_batch and process_batch.")
    
    logger.info("Running translation service directly (Example Usage - Phase 1)")
    
    # Determine available languages based on config AND loaded prompts
    available_and_prompted_langs = [lang for lang in config.AVAILABLE_LANGUAGES if lang in prompt_manager.stage1_templates]
    
    if not available_and_prompted_langs:
        logger.error("No languages available to translate (check AVAILABLE_LANGUAGES in .env and prompt file existence).")
        return # Exit function
        
    logger.info(f"Selected languages for processing: {available_and_prompted_langs}")

    # 1. Prepare Batch (Parse input, populate DB)
    batch_id = prepare_batch(input_file, available_and_prompted_langs)

    if batch_id:
        # 2. Process Batch (Run translations)
        processed_data, success = process_batch(batch_id)
        
        # 3. Generate Export (if successful)
        if processed_data: # Check if we got data back
             # Construct output path based on input filename and config
             output_filename = f"output_{os.path.splitext(os.path.basename(input_file))[0]}_{config.DEFAULT_API.lower()}_batch_{batch_id[:8]}.csv"
             output_path = os.path.join(config.OUTPUT_DIR, output_filename)
             generate_export(batch_id, output_path, processed_data=processed_data) 
             print(f"Processing complete. Check DB ({config.DATABASE_FILE}) and audit log ({config.AUDIT_LOG_FILE})")
             print(f"Main output attempt saved to: {output_path}")
        else:
             print(f"Processing completed but no data returned for export. Errors may have occurred for batch {batch_id}. Check logs and DB.")
    else:
        print("Batch preparation failed.")

# --- Main execution (Example CLI usage) --- #
if __name__ == "__main__":
    # Initialize DB explicitly if running standalone
    db_manager.initialize_database(config.DATABASE_FILE)
    
    logger.info("Running translation service CLI entry point...")
    
    # Example: Process a specific file with specific languages
    # In real app, these would come from UI or arguments
    input_filename = "blake-small.csv" # Example input filename
    input_filepath = os.path.join(config.INPUT_CSV_DIR, input_filename) 

    if not os.path.exists(input_filepath):
        logger.error(f"Input file not found: {input_filepath}")
        exit(1)

    # Call the (now example) translate_csv function
    # In the future, we might call prepare_batch and process_batch directly here
    # based on command-line arguments.
    translate_csv(input_filepath, None) # Output path is now determined inside generate_export 
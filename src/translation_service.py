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
import pandas as pd # <<< Ensure pandas is imported >>>
import io           # <<< Ensure io is imported >>>

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

def call_openai_api(openai_client_obj, system_instructions, source_text, row_identifier="N/A", model_to_use=None, openai_vs_id=None, temperature=None):
    """Calls the OpenAI Responses API for translation. Assumes model_to_use is valid.
       Optionally uses vector store assistance if openai_vs_id is provided.
       Allows specifying temperature.
    """
    
    if not openai_client_obj or not model_to_use:
        logger.error(f"[OpenAI Row {row_identifier}]: Client object or Model name missing in call_openai_api.")
        return None
    if not system_instructions or not source_text:
        logger.error(f"[OpenAI Row {row_identifier}]: System instructions or source text missing in call_openai_api.")
        return None # Need instructions and source
        
    # Construct the input prompt for the Responses API
    # Combining system instructions and source text, similar to the sample
    input_prompt = (
        f"{system_instructions}\n\n" 
        f"--- TASK ---\n"
        f"Translate the following English text based *only* on the rules and context provided above. "
        f"If relevant documents are found via file search, use them to ensure consistency. " # Added mention of file search
        f"Output ONLY the final translated string, with no extra commentary or formatting.\n\n"
        f"English Text:\n{source_text}"
    )
    logger.debug(f"[OpenAI Row {row_identifier}] Input Prompt for Responses API:\n{input_prompt[:200]}...") # Log truncated prompt

    # --- Prepare tools if Vector Store ID is provided ---
    api_tools = None
    if openai_vs_id:
        api_tools = [
            {
                "type": "file_search",
                "vector_store_ids": [openai_vs_id],
                "max_num_results": 7 # Limit results returned to the LLM
            }
        ]
        logger.info(f"[OpenAI Row {row_identifier}]: Enabling File Search tool with VS ID: {openai_vs_id}, max_results=5")
    # -----------------------------------------------------

    current_retry_delay = config.API_INITIAL_RETRY_DELAY 
    for attempt in range(config.API_MAX_RETRIES + 1):
        try:
            # Use the client.responses.create endpoint
            call_args = {
                "model": model_to_use,
                "input": input_prompt, 
            }
            if api_tools:
                call_args["tools"] = api_tools
            if temperature is not None:
                call_args["temperature"] = temperature
                
            response = openai_client_obj.responses.create(**call_args)
            # logger.debug(f"[OpenAI Row {row_identifier}] Full API Response: {response}") # DEBUG

            # Parse the Responses API output structure (as per sample)
            translation = None
            # Responses API doesn't have finish_reason in the same way as Chat Completions

            if response.output:
                for output_item in response.output:
                    if output_item.type == 'message' and output_item.role == 'assistant':
                        if output_item.content:
                            # Find the text content within the message
                            for content_block in output_item.content:
                                if content_block.type == 'output_text':
                                    translation = content_block.text.strip()
                                    break # Found the text, exit inner loop
                        if translation:
                            break # Found the message text, exit outer loop

            if translation is not None: # Check for None explicitly, allows empty string
                # Clean up potential quotes added by the model
                if translation.startswith('"') and translation.endswith('"'):
                    translation = translation[1:-1]
                if translation.startswith("'") and translation.endswith("'"):
                    translation = translation[1:-1]
                logger.info(f"[OpenAI Row {row_identifier}] Responses API Call successful. Translation: {translation[:50]}...")
                return translation
            else:
                 logger.warning(f"[OpenAI Row {row_identifier}]: Could not extract translation text from Responses API output.")
                 # Log the structure for debugging if extraction fails
                 try: logger.warning(f"[OpenAI Row {row_identifier}] Raw Output: {response.output}")
                 except: pass
                 return "" # Return empty string if extraction fails but API call succeeded

        # --- Retry Logic (Adapted for potential Responses API errors) ---
        except openai.APITimeoutError:
             logger.error(f"Attempt {attempt + 1}/{config.API_MAX_RETRIES + 1} [OpenAI Row {row_identifier}]: Responses API call timed out.")
        except openai.APIConnectionError as e:
             logger.error(f"Attempt {attempt + 1}/{config.API_MAX_RETRIES + 1} [OpenAI Row {row_identifier}]: Network error - {e}")
        except openai.RateLimitError as e:
             logger.error(f"Attempt {attempt + 1}/{config.API_MAX_RETRIES + 1} [OpenAI Row {row_identifier}]: Rate limit exceeded - {e.status_code} {e.response}")
             current_retry_delay *= 1.5 
        except openai.APIStatusError as e:
             logger.error(f"Attempt {attempt + 1}/{config.API_MAX_RETRIES + 1} [OpenAI Row {row_identifier}]: API Status Error ({e.status_code}) - {e.response}")
             if e.status_code == 401:
                  logger.error(f"[OpenAI Row {row_identifier}]: Authentication error (401), stopping retries. Check API Key.")
                  return f"ERROR:API:Authentication Error"
             if e.status_code == 429:
                 logger.error(f"Attempt {attempt + 1}/{config.API_MAX_RETRIES + 1} [OpenAI Row {row_identifier}]: Rate limit exceeded (429 status) - {e.response}")
                 current_retry_delay *= 1.5 
             # Check for specific Responses API errors (e.g., model incompatibility)
             if e.status_code == 400 and e.body:
                  if 'does not support the Responses API' in str(e.body):
                       logger.error(f"[OpenAI Row {row_identifier}]: Model '{model_to_use}' does not support the Responses API. Error: {e.body}")
                       return f"ERROR:API:Model does not support Responses API"
                  elif 'vector_store_not_found' in str(e.body) or 'Invalid vector_store_id' in str(e.body):
                       logger.error(f"[OpenAI Row {row_identifier}]: Invalid Vector Store ID '{openai_vs_id}' provided. Error: {e.body}")
                       return f"ERROR:API:Invalid Vector Store ID"
                  elif 'does not support the file_search tool' in str(e.body):
                      logger.error(f"[OpenAI Row {row_identifier}]: Model '{model_to_use}' does not support file_search tool with Responses API. Error: {e.body}")
                      return f"ERROR:API:Model/API combo does not support File Search"
                 
        except openai.APIError as e: # Catch broader OpenAI errors
             logger.error(f"Attempt {attempt + 1}/{config.API_MAX_RETRIES + 1} [OpenAI Row {row_identifier}]: OpenAI API Error - {e}")
        except Exception as e: # Catch any other unexpected errors
             logger.exception(f"Attempt {attempt + 1}/{config.API_MAX_RETRIES + 1} [OpenAI Row {row_identifier}]: Unexpected Error during OpenAI Responses API call - {e}")
        
        # Prepare for retry if applicable
        if attempt < config.API_MAX_RETRIES:
            wait_time = min(current_retry_delay + random.uniform(0, 1), config.API_MAX_RETRY_DELAY)
            logger.info(f"Retrying [OpenAI Row {row_identifier}] Responses API call in {wait_time:.2f} seconds...")
            time.sleep(wait_time)
            current_retry_delay *= 2 
        else:
            logger.error(f"Error [OpenAI Row {row_identifier}]: Max retries reached for Responses API call.")
            return None # Indicate final failure

    return None # Should not be reached if loop completes, but safety return

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

def call_active_api(target_api, system_prompt, user_content, row_identifier="N/A", model_override=None, openai_vs_id=None, temperature=None):
    """Calls the specified target API, fetching client and determining model internally.
       Passes openai_vs_id and temperature to OpenAI calls if provided.
    """
    
    # Ensure api_clients module is loaded
    # (Assumed to be loaded correctly at module import)

    if target_api == "PERPLEXITY":
        # Perplexity function handles its own model default logic
        model_to_use = model_override # Pass override, let function handle default if None
        return call_perplexity_api(system_prompt, user_content, row_identifier, model_to_use)
        
    elif target_api == "OPENAI":
        openai_client_obj = api_clients.get_openai_client()
        if not openai_client_obj:
            logger.error(f"[Row {row_identifier}] Failed to get OpenAI client.")
            return f"ERROR:INTERNAL:OpenAI Client Failed"
        # Determine model, ensuring we have a valid one
        model_to_use = model_override if model_override else config.OPENAI_MODEL 
        if not model_to_use:
            logger.error(f"[Row {row_identifier}] No OpenAI model specified (override or default in config). Cannot proceed.")
            return f"ERROR:CONFIG:OpenAI Model Missing"
            
        # Call the updated OpenAI API function, passing the VS ID and temperature
        return call_openai_api(
            openai_client_obj=openai_client_obj,
            system_instructions=system_prompt, 
            source_text=user_content,
            row_identifier=row_identifier, 
            model_to_use=model_to_use,
            openai_vs_id=openai_vs_id,
            temperature=temperature
        )
        
    elif target_api == "GEMINI":
        # Gemini function handles its own model default logic
        model_to_use = model_override # Pass override, let function handle default if None
        # Gemini API also takes system_instruction and user_content (model.generate_content)
        return call_gemini_api(system_prompt, user_content, row_identifier, model_to_use)
        
    else:
        logger.error(f"Error: Unsupported target_api value '{target_api}' in call_active_api.")
        return f"ERROR:INTERNAL:Unsupported API"

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
def translate_row_worker(task_id, batch_id, row_index, lang_code, source_text, worker_config, progress_callback=None):
    """Worker function for a single translation task. 
       Performs translation and UPDATES THE DATABASE directly. 
       Returns True on success, False on error for progress tracking."""
    row_identifier = f"{row_index+1}-{lang_code}"
    logger.debug(f"WORKER [{task_id} / {row_identifier}]: Starting processing.")

    # --- Retrieve Config --- 
    db_path = worker_config['db_path']
    # <<< GET new flags >>>
    use_stage0 = worker_config['use_stage0']
    use_evaluate_refine = worker_config['use_evaluate_refine']
    
    # <<< Get ALL stage configs regardless of flags >>>
    stage1_api_cfg = worker_config['stage1_api']
    stage2_api_cfg = worker_config['stage2_api']
    stage3_api_cfg = worker_config['stage3_api']
    stage1_model_override = worker_config['stage1_model']
    stage2_model_override = worker_config['stage2_model']
    stage3_model_override = worker_config['stage3_model']
    s0_model = worker_config['s0_model']
    
    batch_prompt = worker_config['batch_prompt']
    use_vs = worker_config['use_vs']
    openai_client_obj = worker_config['openai_client']
    update_strategy = worker_config['update_strategy']

    # --- Initialize State --- 
    db_manager.update_task_status(db_path, task_id, 'running')
    logger.debug(f"WORKER [{task_id} / {row_identifier}]: Status updated.")
    final_translation_local = ""
    initial_translation_local = None 
    evaluation_score_local = None
    evaluation_feedback_local = None
    error_message_local = None
    approved_translation_local = None 
    review_status_local = 'pending_review'
    final_status = 'error' 
    generated_glossary = None
    stage0_raw = None
    stage0_status = None
    target_openai_vs_id = None

    # --- Vector Store Setup --- 
    if use_vs:
        logger.info(f"WORKER [{task_id} / {row_identifier}]: Vector Store Assistance enabled for this batch.")
        try:
            active_vs_map = db_manager.get_active_vector_store_map(db_path)
            if active_vs_map:
                target_openai_vs_id = active_vs_map.get(lang_code)
                if target_openai_vs_id:
                    logger.info(f"WORKER [{task_id} / {row_identifier}]: Found active VS ID '{target_openai_vs_id}' for {lang_code}.")
                else:
                    logger.warning(f"WORKER [{task_id} / {row_identifier}]: Active VS Set found, but no store ID for {lang_code}.")
            else:
                logger.warning(f"WORKER [{task_id} / {row_identifier}]: VS enabled, but no active set found in DB.")
        except Exception as vs_e:
            logger.exception(f"WORKER [{task_id} / {row_identifier}]: Error retrieving active VS map: {vs_e}")
            target_openai_vs_id = None 
    # --- End Vector Store Setup ---
            
    # --- Get necessary data from DB for this specific task ---
    task_details = None
    try:
        conn_task = db_manager.get_db_connection(db_path)
        cursor_task = conn_task.cursor()
        cursor_task.execute("SELECT initial_target_text FROM TranslationTasks WHERE task_id = ?", (task_id,))
        task_details = cursor_task.fetchone()
        conn_task.close()
    except Exception as db_err:
        logger.error(f"WORKER [{task_id} / {row_identifier}]: Failed to retrieve task details from DB: {db_err}", exc_info=True)
        db_manager.update_task_results(db_path, task_id, 'failed', error_msg=f"DB Error: {db_err}")
        return False, None

    if not task_details:
        logger.error(f"WORKER [{task_id} / {row_identifier}]: Task details not found in DB (task_id: {task_id})")
        db_manager.update_task_results(db_path, task_id, 'failed', error_msg="Task details not found")
        return False, None

    initial_target_text_db = task_details['initial_target_text']
    
    # --- Determine if Minimal Change Mode should be applied (MOVED) --- 
    apply_minimal_change = False
    minimal_change_context = None
    if update_strategy == 'update_existing' and initial_target_text_db and initial_target_text_db.strip():
        apply_minimal_change = True
        minimal_change_context = {
            "existing_target_text": initial_target_text_db,
            "new_english_source": source_text
        }
        logger.info(f"WORKER [{task_id} / {row_identifier}]: Applying MINIMAL CHANGE strategy.")
    else:
        if update_strategy == 'update_existing':
             logger.info(f"WORKER [{task_id} / {row_identifier}]: Minimal change strategy selected, but no existing target text found. Using retranslate.")
    # --- End Mode Determination (MOVED) ---
            
    audit_record_base = {
        "task_id": task_id, 
        "row_index": row_index + 1,
        "language": lang_code, 
        "use_stage0": use_stage0,
        "use_evaluate_refine": use_evaluate_refine,
        "update_strategy_applied": "minimal_change" if apply_minimal_change else "retranslate",
        "vs_used_overall": bool(target_openai_vs_id)
    }

    # --- Handle Empty Source Text --- #
    if not source_text:
        logger.warning(f"WORKER [{task_id} / {row_identifier}]: Source text empty. Completing task.")
        final_status = 'completed'
        error_message_local = "Empty source text"
        approved_translation_local = ""
        review_status_local = 'approved_original'
        audit_record = {**audit_record_base, "error": error_message_local, "final_translation": "", "approved_translation": approved_translation_local, "review_status": review_status_local}
        db_manager.update_task_results(db_path, task_id, final_status, 
                                       initial_tx=None, score=None, feedback=None, 
                                       final_tx="", approved_tx=approved_translation_local, 
                                       review_sts=review_status_local, error_msg=error_message_local)
        log_audit_record(audit_record)
        logger.debug(f"WORKER [{task_id} / {row_identifier}]: Finished processing empty source.")
        return True, ""
        
    # --- Main Translation Logic (Refactored) --- 
    try:
        generated_glossary = None
        stage0_raw = None
        stage0_status = None
        initial_translation_local = None
        evaluation_score_local = None
        evaluation_feedback_local = None
        final_translation_local = "" # Default to empty string
        error_message_local = None

        # === Stage 0 (Optional) ===
        if use_stage0:
            logger.info(f"WORKER [{task_id} / {row_identifier}]: Starting Stage 0 (Glossary Generation)")
            s0_api_to_use = 'OPENAI' 
            s0_model_to_use = s0_model
            if target_openai_vs_id: 
                try:
                    stage0_combined_prompt = prompt_manager.get_full_prompt(
                        prompt_type="0", language_code=lang_code,
                        batch_prompt=batch_prompt, minimal_change_context=None
                    )
                    if not stage0_combined_prompt: raise ValueError("Could not generate Stage 0 prompt.")
                    logger.debug(f"WORKER [{task_id} / {row_identifier}]: Calling API (Stage 0) - Model: {s0_model_to_use}, VS_ID: {target_openai_vs_id}")
                    stage0_raw = call_active_api(
                        target_api=s0_api_to_use, system_prompt=stage0_combined_prompt, 
                        user_content=source_text, row_identifier=f"{row_identifier}-S0", 
                        model_override=s0_model_to_use, openai_vs_id=target_openai_vs_id,
                        temperature=0.2
                    )
                    if stage0_raw and not stage0_raw.startswith("ERROR:"):
                        generated_glossary = stage0_raw.strip()
                        logger.info(f"WORKER [{task_id} / {row_identifier}]: Stage 0 generated glossary (length: {len(generated_glossary)}).")
                        stage0_status = 'completed'
                    else:
                        logger.warning(f"WORKER [{task_id} / {row_identifier}]: Stage 0 API call failed or returned empty/error: {stage0_raw}. Proceeding without glossary.")
                        generated_glossary = None 
                        stage0_status = 'failed'
                        error_message_local = f"Stage 0 Error: {stage0_raw}" # Log first error
                except Exception as s0_err:
                    logger.error(f"WORKER [{task_id} / {row_identifier}]: Error during Stage 0 execution: {s0_err}", exc_info=True)
                    generated_glossary = None 
                    stage0_raw = str(s0_err)[:500] 
                    stage0_status = 'failed'
                    if not error_message_local: error_message_local = f"Stage 0 Exception: {stage0_raw}" # Log first error
            else:
                 logger.warning(f"WORKER [{task_id} / {row_identifier}]: Skipping Stage 0: No active VS ID for {lang_code}.")
                 stage0_status = 'skipped_no_vs'
            # Update DB with S0 results immediately
            db_manager.update_task_results(db_path, task_id, 'stage0_complete', 
                                       stage0_glossary=generated_glossary, 
                                       stage0_raw_output=stage0_raw, 
                                       stage0_status=stage0_status)
            logger.debug(f"WORKER [{task_id} / {row_identifier}]: DB updated after Stage 0.")

        # === Stage 1 (Always Runs) ===
        logger.info(f"WORKER [{task_id} / {row_identifier}]: Starting Stage 1 (Base Translation)")
        stage1_prompt = prompt_manager.get_full_prompt(
            prompt_type="1", language_code=lang_code, batch_prompt=batch_prompt,
            generated_glossary=generated_glossary, # Use if generated
            minimal_change_context=minimal_change_context # Use if applicable
        )
        if not stage1_prompt: raise ValueError(f"Could not generate Stage 1 prompt for {lang_code}")

        s1_api_to_use = stage1_api_cfg
        s1_model_to_use = stage1_model_override
        vs_id_for_s1 = target_openai_vs_id if use_vs and s1_api_to_use == "OPENAI" else None
        logger.debug(f"WORKER [{task_id} / {row_identifier}]: Calling API (Stage 1) - API: {s1_api_to_use}, Model: {s1_model_to_use or 'default'}, VS_ID: {vs_id_for_s1}, MinimalChange: {apply_minimal_change}") 
        initial_translation_local = call_active_api(
            s1_api_to_use, stage1_prompt, source_text, 
            row_identifier=f"{row_identifier}-S1", 
            model_override=s1_model_to_use, openai_vs_id=vs_id_for_s1
        )
        logger.debug(f"WORKER [{task_id} / {row_identifier}]: API call returned (Stage 1).")
        audit_record = {**audit_record_base, "stage1_api": s1_api_to_use, "stage1_model": s1_model_to_use or "default", "initial_translation": initial_translation_local} 
        # Log minimal change context only once if applied
        if apply_minimal_change and minimal_change_context:
             audit_record["existing_target_preview"] = minimal_change_context['existing_target_text'][:50] + "..." 

        # Check S1 result before proceeding or deciding final state
        s1_failed = (initial_translation_local is None or initial_translation_local.startswith("ERROR:"))
        if s1_failed:
            if not error_message_local: error_message_local = initial_translation_local or "Stage 1 failed"
            audit_record["error"] = error_message_local
            final_status = 'failed' # S1 failure means overall failure
        else:
            # S1 succeeded, update DB
            db_manager.update_task_results(db_path, task_id, 'stage1_complete', initial_tx=initial_translation_local)
            logger.debug(f"WORKER [{task_id} / {row_identifier}]: DB updated after Stage 1 success.") 
            final_translation_local = initial_translation_local # Default final result is S1 if S2/S3 don't run or fail
            final_status = 'completed' # Initial assumption if S1 succeeds

        # === Stages 2 & 3 (Optional) ===
        if use_evaluate_refine and not s1_failed:
            logger.info(f"WORKER [{task_id} / {row_identifier}]: Starting Stage 2 (Evaluation)")
            s2_api_to_use = stage2_api_cfg
            s2_model_to_use = stage2_model_override
            vs_id_for_s2 = None # S2 doesn't typically use VS
            audit_record["stage2_api"] = s2_api_to_use
            audit_record["stage2_model"] = s2_model_to_use or "default"
            stage2_prompt = prompt_manager.get_full_prompt(
                prompt_type="2", language_code=lang_code, batch_prompt=batch_prompt,
                base_variables={"source_text": source_text, "initial_translation": initial_translation_local},
                generated_glossary=generated_glossary, minimal_change_context=minimal_change_context
            )
            if not stage2_prompt: raise ValueError(f"Could not generate Stage 2 prompt for {lang_code}")
            logger.debug(f"WORKER [{task_id} / {row_identifier}]: Calling API (Stage 2) - API: {s2_api_to_use}, Model: {s2_model_to_use or 'default'}") 
            evaluation_result = call_active_api(
                s2_api_to_use, stage2_prompt, 
                "Evaluate the translation provided above based on the rules.",
                row_identifier=f"{row_identifier}-S2", model_override=s2_model_to_use, openai_vs_id=vs_id_for_s2
            )
            logger.debug(f"WORKER [{task_id} / {row_identifier}]: API call returned (Stage 2).") 
            evaluation_score_local, evaluation_feedback_local = prompt_manager.parse_evaluation(evaluation_result)
            audit_record["evaluation_raw"] = evaluation_result
            audit_record["evaluation_score"] = evaluation_score_local
            audit_record["evaluation_feedback"] = evaluation_feedback_local
            db_manager.update_task_results(db_path, task_id, 'stage2_complete', score=evaluation_score_local, feedback=evaluation_feedback_local)
            logger.debug(f"WORKER [{task_id} / {row_identifier}]: DB updated after Stage 2.") 

            s2_failed = (evaluation_result is None or evaluation_result.startswith("ERROR:"))
            if not s2_failed:
                logger.info(f"WORKER [{task_id} / {row_identifier}]: Starting Stage 3 (Refinement)")
                s3_api_to_use = stage3_api_cfg
                s3_model_to_use = stage3_model_override
                vs_id_for_s3 = target_openai_vs_id if use_vs and s3_api_to_use == "OPENAI" else None
                audit_record["stage3_api"] = s3_api_to_use
                audit_record["stage3_model"] = s3_model_to_use or "default"
                stage3_prompt = prompt_manager.get_full_prompt(
                    prompt_type="3", language_code=lang_code, batch_prompt=batch_prompt,
                    base_variables={"source_text": source_text, "initial_translation": initial_translation_local},
                    stage_variables={"feedback": evaluation_feedback_local},
                    generated_glossary=generated_glossary, minimal_change_context=minimal_change_context
                )
                if not stage3_prompt: raise ValueError(f"Could not generate Stage 3 prompt for {lang_code}")
                logger.debug(f"WORKER [{task_id} / {row_identifier}]: Calling API (Stage 3) - API: {s3_api_to_use}, Model: {s3_model_to_use or 'default'}, VS_ID: {vs_id_for_s3}") 
                s3_final_translation = call_active_api(
                    s3_api_to_use, stage3_prompt, 
                    "Revise the initial translation based *only* on the evaluation feedback provided.",
                    row_identifier=f"{row_identifier}-S3", model_override=s3_model_to_use, openai_vs_id=vs_id_for_s3
                )
                logger.debug(f"WORKER [{task_id} / {row_identifier}]: API call returned (Stage 3).") 
                audit_record["final_translation"] = s3_final_translation # Log final LLM output

                s3_failed = (s3_final_translation is None or s3_final_translation.startswith("ERROR:"))
                if not s3_failed:
                    final_translation_local = s3_final_translation # Update final result with S3 output
                    # final_status remains 'completed'
                else:
                    # S3 failed, keep S1 result as final, but log S3 error
                    if not error_message_local: error_message_local = s3_final_translation or "Stage 3 failed"
                    audit_record["error"] = error_message_local
                    final_status = 'completed_with_errors' # S1 ok, S3 failed
            else: 
                # S2 failed, keep S1 result as final, log S2 error
                if not error_message_local: error_message_local = evaluation_result or "Stage 2 failed"
                audit_record["error"] = error_message_local
                final_status = 'completed_with_errors' # S1 ok, S2 failed
        
        # === Determine Final Approved/Review Status ===
        if final_status == 'completed':
            approved_translation_local = final_translation_local
            review_status_local = 'approved_original'
        elif final_status == 'completed_with_errors': 
            # If S1 worked but S2/S3 failed, should we auto-approve S1? Or leave pending?
            # Let's leave pending review if any stage after S1 failed.
            approved_translation_local = None # No auto-approval
            review_status_local = 'pending_review'
        else: # failed or error
            approved_translation_local = None
            review_status_local = 'pending_review'
        
        # Add final approved/review status to audit
        audit_record["approved_translation"] = approved_translation_local
        audit_record["review_status"] = review_status_local
        log_audit_record(audit_record) # Log full audit record once at the end

    except Exception as e:
        logger.exception(f"WORKER [{task_id} / {row_identifier}]: Unhandled exception in worker: {e}", exc_info=True)
        error_message_local = str(e)[:500] # Truncate long errors
        final_status = 'failed'
        # Ensure results are nullified
        initial_translation_local = None; final_translation_local = ""; evaluation_score_local = None; evaluation_feedback_local = None; approved_translation_local = None; review_status_local = 'pending_review'; generated_glossary = None; stage0_raw = None; # stage0_status might be set
        # Log minimal audit on major failure
        try: log_audit_record({**audit_record_base, "error": error_message_local}) 
        except: pass
        
    # Final DB update outside the main try block
    try:
        logger.debug(f"WORKER [{task_id} / {row_identifier}]: Performing final DB update. Status: {final_status}")
        db_manager.update_task_results(db_path, task_id, final_status, 
                                   initial_tx=initial_translation_local, 
                                   score=evaluation_score_local, 
                                   feedback=evaluation_feedback_local, 
                                   final_tx=final_translation_local,
                                   approved_tx=approved_translation_local, 
                                   review_sts=review_status_local,
                                   error_msg=error_message_local,
                                   stage0_glossary=generated_glossary,
                                   stage0_raw_output=stage0_raw,
                                   stage0_status=stage0_status)
        logger.debug(f"WORKER [{task_id} / {row_identifier}]: Final DB update complete.")
        return final_status == 'completed', final_translation_local
    except Exception as final_db_e:
        logger.exception(f"WORKER [{task_id} / {row_identifier}]: CRITICAL - Failed during final DB update: {final_db_e}")
        return False, None

# --- Background Batch Processing Function (Target for Flask's Thread) ---
def run_batch_background(batch_id):
    """Processes all pending tasks for a given batch_id using thread pool.
       This function runs in a background thread started by Flask."""
    logger.info(f"BACKGROUND THREAD: Starting processing for batch_id: {batch_id}")
    db_path = config.DATABASE_FILE 
    
    # Retrieve batch config from DB
    batch_info = db_manager.get_batch_info(db_path, batch_id)
    if not batch_info:
        logger.error(f"BACKGROUND THREAD [{batch_id}]: Cannot process batch: Batch info not found")
        return
    try:
        batch_config = json.loads(batch_info['config_details'] or '{}')
        # <<< Add Backward Compatibility Check >>>
        if 'use_stage0' not in batch_config or 'use_evaluate_refine' not in batch_config:
            logger.warning(f"BACKGROUND THREAD [{batch_id}]: Old config format detected. Inferring settings from 'mode'.")
            old_mode = batch_config.get('mode', 'ONE_STAGE') # Default to ONE_STAGE if mode is missing too
            use_stage0_inferred = (old_mode == 'FOUR_STAGE')
            use_evaluate_refine_inferred = (old_mode in ['THREE_STAGE', 'FOUR_STAGE'])
            # Add inferred flags to the config we use later
            batch_config['use_stage0'] = use_stage0_inferred
            batch_config['use_evaluate_refine'] = use_evaluate_refine_inferred
            logger.info(f"BACKGROUND THREAD [{batch_id}]: Inferred: use_stage0={use_stage0_inferred}, use_evaluate_refine={use_evaluate_refine_inferred} from mode='{old_mode}'")
        # <<< End Backward Compatibility >>>

        batch_prompt = batch_config.get('batch_prompt', '')
    except Exception as e:
        logger.error(f"BACKGROUND THREAD [{batch_id}]: Cannot process batch: Failed to parse config_details - {e}")
        db_manager.update_batch_status(db_path, batch_id, 'failed')
        return
        
    tasks_to_process = db_manager.get_pending_tasks(db_path, batch_id)
    total_tasks = len(tasks_to_process)
    if total_tasks == 0:
        logger.info(f"BACKGROUND THREAD: No pending tasks found for batch_id: {batch_id}")
        db_manager.update_batch_status(db_path, batch_id, 'completed_empty')
        return # Exit thread

    logger.info(f"BACKGROUND THREAD [{batch_id}]: Processing {total_tasks} tasks (UseS0: {batch_config.get('use_stage0')}, UseEvalRefine: {batch_config.get('use_evaluate_refine')}) using up to {config.MAX_WORKER_THREADS} threads...")
    db_manager.update_batch_status(db_path, batch_id, 'processing') 
    
    success_count = 0
    error_count = 0
    cancelled = False # Flag to track if job was cancelled
    processed_tasks = 0
    tasks_to_cancel = list(tasks_to_process) # Keep track of tasks not yet processed

    # Create a configuration dictionary to pass to each worker
    worker_config = {
        'db_path': db_path,
        'use_stage0': batch_config.get('use_stage0', False),
        'use_evaluate_refine': batch_config.get('use_evaluate_refine', False),
        'stage1_api': batch_config.get('s1_api'),
        'stage2_api': batch_config.get('s2_api'),
        'stage3_api': batch_config.get('s3_api'),
        'stage1_model': batch_config.get('s1_model'),
        'stage2_model': batch_config.get('s2_model'),
        'stage3_model': batch_config.get('s3_model'),
        's0_model': batch_config.get('s0_model'),
        'batch_prompt': batch_config.get('batch_prompt', ''),
        'use_vs': batch_config.get('use_vs', False),
        'openai_client': api_clients.get_openai_client() or None, 
        'update_strategy': batch_config.get('update_strategy', 'retranslate')
    }
    
    processed_task_results = {} 

    # Use ThreadPoolExecutor for row-level parallelism within this background thread
    with concurrent.futures.ThreadPoolExecutor(max_workers=config.MAX_WORKER_THREADS) as executor:
        future_to_task_id = {
            executor.submit(
                translate_row_worker, 
                task['task_id'], task['batch_id'], task['row_index_in_file'], 
                task['language_code'], task['source_text'], worker_config, None
            ): task['task_id'] 
            for task in tasks_to_process
        }
        
        # Wrap the as_completed iterator with tqdm for progress
        progress_bar = tqdm(concurrent.futures.as_completed(future_to_task_id), total=total_tasks, desc=f"Translating Batch {batch_id}")

        for i, future in enumerate(progress_bar):
            # <<< Check for cancellation periodically (e.g., every 5 tasks) >>>
            if i % 5 == 0: 
                current_status_info = db_manager.get_batch_info(db_path, batch_id)
                if current_status_info and current_status_info['status'] == 'cancelling':
                    logger.warning(f"BACKGROUND THREAD [{batch_id}]: Cancellation requested. Stopping task submission and waiting for running tasks.")
                    cancelled = True
                    # Cancel pending futures (tasks not yet started)
                    # Note: This might not be perfectly reliable depending on thread state
                    # It tells the executor not to run them if they haven't started.
                    for f in future_to_task_id:
                        if not f.done() and not f.running():
                            f.cancel()
                    break # Exit the loop submitting/waiting for tasks

            task_id = future_to_task_id[future]
            logger.debug(f"BACKGROUND THREAD [{batch_id}]: Waiting for result from task {task_id}...") 
            try:
                completed_successfully, final_translation = future.result()
                logger.debug(f"BACKGROUND THREAD [{batch_id}]: Result received for task {task_id}. Success={completed_successfully}") 
                if completed_successfully:
                    success_count += 1
                else:
                    error_count += 1 
                processed_tasks += 1
            except concurrent.futures.CancelledError:
                 logger.info(f"BACKGROUND THREAD [{batch_id}]: Task {task_id} was cancelled before completion.")
                 error_count += 1 # Count cancelled as error for status purposes?
                 processed_tasks += 1
            except Exception as exc:
                logger.error(f'BACKGROUND THREAD [{batch_id}]: Task {task_id} generated an exception: {exc}', exc_info=True) 
                error_count += 1 
                processed_tasks += 1
                try:
                    # Use db_manager to update task status on direct exception
                    db_manager.update_task_results(
                         db_path=db_path,
                         task_id=task_id, 
                         status='failed', 
                         error_msg=f'Unhandled worker exception: {exc}'
                    )
                except Exception as db_exc:
                     logger.error(f"BACKGROUND THREAD [{batch_id}]: Failed to update task {task_id} status after exception: {db_exc}")

        # After loop finishes or is broken
        progress_bar.close() # Close the tqdm bar
        logger.info(f"BACKGROUND THREAD [{batch_id}]: Worker loop finished. Processed: {processed_tasks}, Cancelled flag: {cancelled}")

    # Determine final status
    if cancelled:
        final_batch_status = 'failed' # Treat cancelled jobs as failed for simplicity
        logger.warning(f"BACKGROUND THREAD [{batch_id}]: Job was cancelled. Setting final status to {final_batch_status}.")
        # Optionally: Update remaining unprocessed tasks in DB to 'cancelled' or 'failed'
        # This requires knowing which tasks *didn't* get processed.
        # This is complex with as_completed; easier might be to fetch all non-completed tasks after loop.
        try:
            remaining_tasks = db_manager.get_pending_tasks(db_path, batch_id) # Get tasks still pending
            for task in remaining_tasks:
                 db_manager.update_task_status(db_path, task['task_id'], 'failed', 'Job cancelled')
            logger.info(f"BACKGROUND THREAD [{batch_id}]: Marked {len(remaining_tasks)} remaining pending tasks as failed due to cancellation.")
        except Exception as cancel_update_e:
             logger.error(f"BACKGROUND THREAD [{batch_id}]: Error updating remaining tasks after cancellation: {cancel_update_e}")
    elif error_count > 0:
        final_batch_status = 'completed_with_errors'
    else:
        final_batch_status = 'completed'
    
    logger.info(f"BACKGROUND THREAD [{batch_id}]: Finalizing batch. Success: {success_count}, Errors/Cancelled: {error_count}, Final Status: {final_batch_status}") 
    db_manager.update_batch_status(db_path, batch_id, final_batch_status)
    logger.info(f"BACKGROUND THREAD [{batch_id}]: Final batch status {final_batch_status} updated in DB.")

# --- Input Processing --- 
def prepare_batch(input_file_path, original_filename, selected_languages, mode_config):
    """Reads input CSV using pandas, validates columns, and creates batch/task entries in the DB."""
    logger.info(f"Preparing batch {original_filename} from {input_file_path} for languages: {selected_languages}")
    db_path = config.DATABASE_FILE
    batch_id = str(uuid.uuid4())
    batch_start_time = datetime.now()
    update_strategy = mode_config.get('update_strategy', 'retranslate')
    input_type = mode_config.get('input_type', 'csv')

    try:
        # 1. Read the CSV file using pandas
        df = pd.read_csv(input_file_path)
        logger.info(f"Read {len(df)} rows from {input_file_path}")

        if df.empty:
            logger.warning(f"Input file {original_filename} is empty. Creating batch with 0 tasks.")
            config_details_json = json.dumps(mode_config)
            success = db_manager.add_batch(db_path, batch_id, original_filename, config_details_json)
            if success:
                db_manager.update_batch_status(db_path, batch_id, 'completed_empty')
                logger.info(f"Created empty batch {batch_id} with status 'completed_empty'.")
                return None # Indicate no processing needed
            else:
                logger.error(f"Failed to add empty batch record {batch_id} to database.")
                return None

        # 2. Validate required columns
        # <<< RELAX VALIDATION: Only require Source and Record ID >>>
        required_cols = {config.SOURCE_COLUMN, "Record ID"}
        logger.info(f"Validating presence of core columns: {required_cols}")
        
        # Still check for target columns if strategy requires reading existing text
        if update_strategy != 'retranslate':
            target_cols_to_check = {f"tg_{lang}" for lang in selected_languages}
            # Only add target cols to validation if they are *needed* for the strategy
            required_cols.update(target_cols_to_check)
            logger.info(f"Update strategy is '{update_strategy}', *also* checking for target columns: {target_cols_to_check}")
        # else:
        #     logger.info(f"Update strategy is 'retranslate', skipping target column check for {input_type} input.")
        # <<< END RELAXED VALIDATION >>>

        missing_cols = required_cols - set(df.columns)
        if missing_cols:
            err_msg = f"Missing required columns in {original_filename}: {missing_cols}. "
            if update_strategy != 'retranslate' and any(col.startswith('tg_') for col in missing_cols):
                err_msg += f"Target columns ({target_cols_to_check}) are required for '{update_strategy}' strategy." 
            else:
                 # Adjust error message for core columns
                 err_msg += f"Core columns ({config.SOURCE_COLUMN}, Record ID) are required."
            logger.error(err_msg)
            return None

        # 3. Add Batch entry to DB
        # <<< Construct the FINAL intended header >>>
        final_header = df.columns.tolist()
        target_cols_to_add = [f"tg_{lang}" for lang in selected_languages if f"tg_{lang}" not in final_header]
        final_header.extend(target_cols_to_add)
        logger.info(f"Constructed final header for saving: {final_header}")
        mode_config['original_header'] = final_header # Add final header to config
        
        config_details_json = json.dumps(mode_config)
        db_manager.add_batch(db_path, batch_id, original_filename, config_details_json)
        # No need to check return value if add_batch raises exceptions

        # 4. Add Task entries to DB
        tasks_added = 0
        for index, row in df.iterrows():
            source_text = str(row.get(config.SOURCE_COLUMN, "")) # Ensure string
            metadata = {
                "Record ID": str(row.get("Record ID", "")),
                "Context": str(row.get("Context", "")),
                "DeveloperNotes": str(row.get("DeveloperNotes", ""))
            }
            metadata_json = json.dumps(metadata)

            for lang_code in selected_languages:
                initial_target_text = None
                if update_strategy != 'retranslate':
                    target_col_name = f"tg_{lang_code}"
                    # Column presence already validated if needed
                    initial_target_text = row.get(target_col_name)
                    if pd.isna(initial_target_text):
                        initial_target_text = None
                    else:
                        initial_target_text = str(initial_target_text)
                
                # Check if the prompt file actually exists for this language
                if lang_code not in prompt_manager.stage1_templates: # Use loaded prompt keys
                     logger.warning(f"Skipping task for lang {lang_code}, row {index}: Prompt template not loaded.")
                     continue # Skip this language for this row

                db_manager.add_translation_task(
                    db_path, # Pass db_path positionally
                    batch_id=batch_id,
                    row_index=index,
                    lang_code=lang_code,
                    source_text=source_text,
                    metadata_json=metadata_json,
                    initial_target_text=initial_target_text
                )
                tasks_added += 1

        if tasks_added == 0:
            logger.warning(f"No tasks were successfully added for batch {batch_id}. Check logs for errors or language availability.")
            db_manager.update_batch_status(db_path, batch_id, 'failed') 
            return None

        logger.info(f"Successfully added {tasks_added} tasks for batch {batch_id}")
        db_manager.update_batch_status(db_path, batch_id, 'pending')
        return batch_id

    except pd.errors.EmptyDataError:
        logger.error(f"Input file is empty: {input_file_path}")
        return None
    except FileNotFoundError:
        logger.error(f"Input file not found: {input_file_path}")
        return None
    except Exception as e:
        logger.exception(f"Error during batch preparation for {input_file_path}: {e}")
        if 'batch_id' in locals() and batch_id:
            try:
                logger.info(f"Marking batch {batch_id} as failed due to preparation error: {e}")
                db_manager.update_batch_status(db_path, batch_id, 'failed') 
            except Exception as cleanup_e:
                logger.error(f"Failed to update batch status to failed for {batch_id} after error: {cleanup_e}")
        return None # Original line

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
         
    # Fetch task data including approved translation
    tasks = db_manager.get_completed_tasks_for_export(db_path, batch_id)
    if not tasks:
        logger.warning(f"No task data found or retrieved for export for batch {batch_id}")
        # Proceed to write header anyway? Or return False?
        # Let's write an empty file with header.
        tasks = [] 

    # Reconstruct the data row by row
    output_data_dict = {}

    for task_row in tasks:
        row_index = task_row['row_index_in_file']
        lang_code = task_row['language_code']
        approved_tx = task_row['approved_translation']
        review_status = task_row['review_status']
        task_status = task_row['status'] # Get task status
        final_tx = task_row['final_translation'] # Get LLM final
        
        # Determine translation to export based on review and task status
        translation_to_export = "" # Default to empty
        if review_status in ['approved_original', 'approved_edited'] and approved_tx is not None:
            # Prioritize explicitly approved text
            translation_to_export = approved_tx
        elif task_status == 'completed' and review_status == 'pending_review' and final_tx is not None:
            # If task completed BUT is pending review, export the LLM's final translation 
            # This covers the S0+S1 case where auto-approval might not have set approved_translation yet
            # or if user manually set back to pending.
            translation_to_export = final_tx
        # Otherwise (e.g., status=failed, review=denied, review=pending with no final_tx), leave as empty string
        
        metadata_json = task_row['metadata_json']
        try: metadata = json.loads(metadata_json or '{}')
        except: metadata = {}
        target_col = config.TARGET_COLUMN_TPL.format(lang_code=lang_code)

        if row_index not in output_data_dict:
            source = task_row['source_text'] or "SOURCE_NOT_FOUND" # Get source directly
            output_data_dict[row_index] = {config.SOURCE_COLUMN: source, **metadata}
        
        # Add the translation to export for the specific language
        output_data_dict[row_index][target_col] = translation_to_export

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
    
    # Check if batch was run in a mode that might have stage data (3 or 4)
    batch_info = db_manager.get_batch_info(db_path, batch_id)
    mode = 'UNKNOWN'
    if batch_info and batch_info['config_details']:
        try:
            batch_config = json.loads(batch_info['config_details'])
            mode = batch_config.get('mode')
            # Only generate if 3 or 4 stage?
            # if mode not in ['THREE_STAGE', 'FOUR_STAGE']:
            #     logger.warning(f"Skipping stages report: Batch {batch_id} was mode {mode}.")
            #     return False # Or maybe generate with fewer columns?
        except Exception as e:
            logger.error(f"Cannot generate stages report: Error reading config for {batch_id} - {e}")
            return False
    else:
         logger.error(f"Cannot generate stages report: Batch info not found for {batch_id}")
         return False

    # Fetch task data including stage 0 columns
    tasks = db_manager.get_tasks_for_stages_report(db_path, batch_id)
    if not tasks:
        logger.warning(f"No tasks found for stages report (batch {batch_id})")
        # Write empty file with header?

    # Define header for the report - include Stage 0
    report_header = [
        'task_id', 'row_index', 'language', 'source', 
        'stage0_glossary', 'stage0_raw_output', # <<< ADD S0 HEADERS >>>
        'initial_translation', 'eval_score', 'eval_feedback', 
        'final_llm_translation', 
        'approved_translation', 'review_status', 
        'task_status', 'error'
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
                    'stage0_glossary': task_row['stage0_glossary'],     # <<< MAP S0 DATA >>>
                    'stage0_raw_output': task_row['stage0_raw_output'], # <<< MAP S0 DATA >>>
                    'initial_translation': task_row['initial_translation'],
                    'eval_score': task_row['evaluation_score'],
                    'eval_feedback': task_row['evaluation_feedback'],
                    'final_llm_translation': task_row['final_translation'],
                    'approved_translation': task_row['approved_translation'],
                    'review_status': task_row['review_status'],
                    'task_status': task_row['task_status'],
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
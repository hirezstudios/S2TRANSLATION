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

# Load environment variables from .env file
load_dotenv()

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
LANGUAGE_CODE = "esLA" # TODO: Make this dynamic if supporting multiple languages
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
# Define system prompt file paths
STAGE1_PROMPT_FILE = f"system_prompts/tg_{LANGUAGE_CODE}.md"
STAGE2_PROMPT_FILE = f"system_prompts/stage2_evaluate_{LANGUAGE_CODE}.md"
STAGE3_PROMPT_FILE = f"system_prompts/stage3_refine_{LANGUAGE_CODE}.md"
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
stage1_system_prompt = None # Load prompts globally once
stage2_system_prompt = None
stage3_system_prompt = None

def load_system_prompts():
    """Loads all necessary system prompts."""
    global stage1_system_prompt, stage2_system_prompt, stage3_system_prompt
    
    print(f"Loading Stage 1 prompt from {STAGE1_PROMPT_FILE}...")
    stage1_system_prompt = load_single_prompt(STAGE1_PROMPT_FILE)
    # Append the crucial final instruction directly to the loaded prompt
    final_instruction = "\n\n**IMPORTANT FINAL INSTRUCTION: Your final output should contain ONLY the translated text (or evaluation, depending on the stage) for the given input string. Do not include any other information, explanations, thinking processes (like <think> blocks), or formatting.**"
    stage1_system_prompt += final_instruction

    if TRANSLATION_MODE == "THREE_STAGE":
        print(f"Loading Stage 2 prompt from {STAGE2_PROMPT_FILE}...")
        stage2_system_prompt = load_single_prompt(STAGE2_PROMPT_FILE)
        stage2_system_prompt += final_instruction # Also add instruction here
        
        print(f"Loading Stage 3 prompt from {STAGE3_PROMPT_FILE}...")
        stage3_system_prompt = load_single_prompt(STAGE3_PROMPT_FILE)
        stage3_system_prompt += final_instruction # And here
        

def load_single_prompt(filepath):
    """Loads a single system prompt from a file."""
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            return f.read()
    except FileNotFoundError:
        print(f"Error: System prompt file {filepath} not found.")
        # Exit if essential stage 1 prompt is missing, maybe allow missing stage 2/3?
        exit(1) 
    except Exception as e:
        print(f"Error reading system prompt file {filepath}: {e}")
        exit(1)

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

def translate_row_worker(row_data):
    """Worker function to translate a single row based on TRANSLATION_MODE."""
    global translated_counter, error_counter 
    row, row_index = row_data 
    source_text = row.get(SOURCE_COLUMN, "").strip()
    final_translation = "" 
    audit_record_base = {
        "row_index": row_index + 1, 
        "source": source_text,
        "mode": TRANSLATION_MODE,
    }

    if not source_text:
        row[TARGET_COLUMN] = ""
        # Log empty source only if required, otherwise skip
        # log_audit_record({**audit_record_base, "error": "Empty source text"})
        return row 

    try:
        if TRANSLATION_MODE == "ONE_STAGE":
            # Use DEFAULT_API and its default model (no override needed here)
            api_to_use = DEFAULT_API
            model_to_use = None 
            audit_record = {**audit_record_base, "api": api_to_use, "model": model_to_use or "default"} 
            
            final_translation = call_active_api(api_to_use, stage1_system_prompt, source_text, 
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
            # --- Stage 1: Initial Translation --- 
            s1_api = STAGE1_API
            s1_model = STAGE1_MODEL_OVERRIDE 
            audit_record = {**audit_record_base, "stage1_api": s1_api, "stage1_model": s1_model or "default"}
            initial_translation = call_active_api(s1_api, stage1_system_prompt, source_text, 
                                                row_identifier=f"{row_index + 2}-S1", model_override=s1_model)
            audit_record["initial_translation"] = initial_translation

            if initial_translation is None:
                with print_lock: error_counter += 1
                final_translation = "" 
                audit_record["error"] = "Stage 1 API call failed after retries"
                log_audit_record(audit_record)
            else:
                # --- Stage 2: Evaluation --- 
                s2_api = STAGE2_API
                s2_model = STAGE2_MODEL_OVERRIDE
                audit_record["stage2_api"] = s2_api
                audit_record["stage2_model"] = s2_model or "default"
                
                # Construct Stage 2 Prompt 
                # Ensure stage2_system_prompt is loaded
                if not stage2_system_prompt:
                    raise ValueError("Stage 2 system prompt not loaded.")
                # Inject the base rules (stage1 prompt without final instruction)
                base_rules = stage1_system_prompt.split("**IMPORTANT FINAL INSTRUCTION:")[0].strip()
                eval_system_prompt = stage2_system_prompt.replace("<<RULES>>", base_rules) 
                # Create user content for evaluation stage
                eval_user_content = f"SOURCE_TEXT:\n{source_text}\n\nINITIAL_TRANSLATION:\n{initial_translation}"
                
                evaluation_raw = call_active_api(s2_api, eval_system_prompt, eval_user_content, 
                                           row_identifier=f"{row_index + 2}-S2", model_override=s2_model)
                
                # Attempt to parse the JSON evaluation
                evaluation_score = None
                evaluation_feedback = None
                if evaluation_raw:
                    # --- Start JSON Cleaning --- 
                    cleaned_json_str = evaluation_raw.strip()
                    # Remove potential markdown fences
                    if cleaned_json_str.startswith("```json"):
                        cleaned_json_str = cleaned_json_str[7:] # Remove ```json
                    elif cleaned_json_str.startswith("```"):
                         cleaned_json_str = cleaned_json_str[3:] # Remove ```
                    if cleaned_json_str.endswith("```"):
                        cleaned_json_str = cleaned_json_str[:-3] # Remove ```
                    cleaned_json_str = cleaned_json_str.strip() # Strip again after removing fences
                    # --- End JSON Cleaning --- 
                    
                    try:
                        # Use the cleaned string for parsing
                        eval_data = json.loads(cleaned_json_str) 
                        evaluation_score = eval_data.get("score")
                        evaluation_feedback = eval_data.get("feedback")
                        # Basic type validation
                        if not isinstance(evaluation_score, int) or not isinstance(evaluation_feedback, str):
                             # If score is convertible string, try converting
                            if isinstance(evaluation_score, str) and evaluation_score.isdigit():
                                evaluation_score = int(evaluation_score)
                            else:
                                raise ValueError(f"Invalid types in evaluation JSON. Score: {type(evaluation_score)}, Feedback: {type(evaluation_feedback)}")
                        # Add score range validation
                        if not 1 <= evaluation_score <= 10:
                            raise ValueError(f"Score {evaluation_score} out of range (1-10).")
                            
                    except (json.JSONDecodeError, ValueError, TypeError) as e:
                        with print_lock:
                            print(f"Warning [Row {row_index + 2}-S2]: Failed to parse cleaned evaluation JSON ({e}). Cleaned: '{cleaned_json_str[:100]}...' Raw: '{evaluation_raw[:100]}...'")
                        evaluation_feedback = f"Evaluation parsing failed. Raw output: {evaluation_raw}" 
                        evaluation_score = None 
                
                audit_record["evaluation_score"] = evaluation_score
                audit_record["evaluation_feedback"] = evaluation_feedback

                if evaluation_feedback is None: # Check if the API call itself failed
                    final_translation = initial_translation
                    with print_lock: translated_counter += 1 
                    audit_record["error"] = "Stage 2 API call failed, using Stage 1 result"
                    audit_record["final_translation"] = final_translation 
                    log_audit_record(audit_record)
                # Decide if refinement is needed (e.g., score < 10 or specific feedback exists) 
                # Let's proceed to stage 3 regardless for now, unless stage 2 failed entirely.
                else: 
                    # --- Stage 3: Refinement --- 
                    s3_api = STAGE3_API
                    s3_model = STAGE3_MODEL_OVERRIDE
                    audit_record["stage3_api"] = s3_api
                    audit_record["stage3_model"] = s3_model or "default"
                    
                    # Construct Stage 3 Prompt
                    if not stage3_system_prompt:
                        raise ValueError("Stage 3 system prompt not loaded.")
                    # Inject base rules again
                    refine_system_prompt_template = stage3_system_prompt.replace("<<RULES>>", base_rules)
                    # Inject dynamic content
                    refine_system_prompt = refine_system_prompt_template.replace("<<SOURCE_TEXT>>", source_text)\
                                                                   .replace("<<INITIAL_TRANSLATION>>", initial_translation)\
                                                                   .replace("<<FEEDBACK>>", evaluation_feedback or "No specific feedback provided.")
                                                                   
                    # User content for stage 3 can be simple, as context is in system prompt
                    refine_user_content = "Revise the translation based on the provided context and feedback."
                    
                    final_translation = call_active_api(s3_api, refine_system_prompt, refine_user_content, 
                                                      row_identifier=f"{row_index + 2}-S3", model_override=s3_model)
                    audit_record["final_translation"] = final_translation

                    if final_translation is None:
                        final_translation = initial_translation 
                        with print_lock: translated_counter += 1
                        audit_record["error"] = "Stage 3 API call failed, using Stage 1 result"
                        audit_record["final_translation"] = final_translation
                    else:
                         with print_lock: translated_counter += 1 
                    log_audit_record(audit_record)
        else:
             with print_lock: print(f"Error: Unknown TRANSLATION_MODE '{TRANSLATION_MODE}'")
             error_counter += 1
             final_translation = "" 
             audit_record_base["error"] = f"Unknown TRANSLATION_MODE: {TRANSLATION_MODE}"
             log_audit_record(audit_record_base)

    except Exception as e:
        # Catch unexpected errors within the worker's logic
        with print_lock:
            print(f"Critical Error in worker for row {row_index+2}: {e}")
        error_counter += 1
        final_translation = "" # Ensure it's empty on critical worker error
        audit_record_base["error"] = f"Critical worker error: {e}"
        log_audit_record(audit_record_base)

    # --- Update the row for the main CSV output --- 
    row[TARGET_COLUMN] = final_translation 
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
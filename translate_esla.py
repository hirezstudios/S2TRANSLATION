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
AUDIT_LOG_FILE = os.getenv("AUDIT_LOG_FILE", f"output/audit_log_{LANGUAGE_CODE}_{ACTIVE_API.lower()}.jsonl")
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

def call_perplexity_api(system_prompt, user_content, row_identifier="N/A"):
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
        
    headers = {
        "Authorization": f"Bearer {PPLX_API_KEY}",
        "Content-Type": "application/json",
        "accept": "application/json"
    }
    payload = {
        "model": PPLX_MODEL,
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

def call_openai_api(system_prompt, user_content, row_identifier="N/A"):
    """Calls the OpenAI Responses API for translation with retry logic and parses the output."""
    if not openai_client:
        with print_lock:
             print(f"Error [Row {row_identifier}]: OpenAI client not initialized (check OPENAI_API_KEY).")
        return None
    if not OPENAI_MODEL:
         with print_lock:
             print(f"Error [Row {row_identifier}]: OPENAI_MODEL not found in .env file.")
         return None

    current_retry_delay = API_INITIAL_RETRY_DELAY
    for attempt in range(API_MAX_RETRIES + 1):
        try:
            response = openai_client.responses.create(
                model=OPENAI_MODEL,
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

def call_gemini_api(system_prompt, user_content, row_identifier="N/A"):
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

    # Create the model instance 
    try:
        model = genai.GenerativeModel(
            model_name=GEMINI_MODEL,
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

def call_active_api(system_prompt, user_content, row_identifier="N/A"):
    """Calls the currently active API based on ACTIVE_API setting."""
    if ACTIVE_API == "PERPLEXITY":
        return call_perplexity_api(system_prompt, user_content, row_identifier)
    elif ACTIVE_API == "OPENAI":
        return call_openai_api(system_prompt, user_content, row_identifier)
    elif ACTIVE_API == "GEMINI":
        return call_gemini_api(system_prompt, user_content, row_identifier)
    else:
        with print_lock:
            print(f"Error: Unsupported ACTIVE_API value '{ACTIVE_API}' in .env file.")
        return None # Or raise an error

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
    row, row_index = row_data # Unpack data (system prompts are global now)
    source_text = row.get(SOURCE_COLUMN, "").strip()
    final_translation = "" # Initialize
    audit_record = {
        "row_index": row_index + 1, # 1-based index for readability
        "source": source_text,
        "api": ACTIVE_API,
        "mode": TRANSLATION_MODE,
        "initial_translation": None,
        "evaluation": None,
        "final_translation": None,
        "error": None
    }

    if not source_text:
        row[TARGET_COLUMN] = ""
        # Optionally log empty source rows to audit?
        # log_audit_record({**audit_record, "error": "Empty source text"})
        return row # Return unmodified row if source is empty

    try:
        if TRANSLATION_MODE == "ONE_STAGE":
            # --- One Stage Workflow --- 
            final_translation = call_active_api(stage1_system_prompt, source_text, row_identifier=row_index + 2)
            if final_translation is not None:
                with print_lock: translated_counter += 1
                audit_record["final_translation"] = final_translation
            else:
                with print_lock: error_counter += 1
                final_translation = "" # Use empty string on error
                audit_record["error"] = "Stage 1 API call failed after retries"
            
            # Log one-stage result to audit file
            log_audit_record(audit_record)

        elif TRANSLATION_MODE == "THREE_STAGE":
            # --- Three Stage Workflow --- 
            # Stage 1: Initial Translation
            initial_translation = call_active_api(stage1_system_prompt, source_text, row_identifier=f"{row_index + 2}-S1")
            audit_record["initial_translation"] = initial_translation

            if initial_translation is None:
                # If stage 1 fails, cannot proceed. Log error and use empty final translation.
                with print_lock: error_counter += 1
                final_translation = ""
                audit_record["error"] = "Stage 1 API call failed after retries"
                log_audit_record(audit_record)
            else:
                # Stage 2: Evaluation
                # Prepare evaluation prompt (needs proper templating)
                # Basic example, replace placeholders correctly later
                eval_prompt = stage2_system_prompt.replace("<<RULES>>", "[Rules Not Inserted Yet]") # Placeholder
                eval_input = f"Source: {source_text}\nTranslation: {initial_translation}"
                evaluation = call_active_api(eval_prompt, eval_input, row_identifier=f"{row_index + 2}-S2")
                audit_record["evaluation"] = evaluation

                if evaluation is None:
                    # If stage 2 fails, use Stage 1 translation as final
                    final_translation = initial_translation
                    with print_lock: translated_counter += 1 # Count Stage 1 as success in this case
                    audit_record["error"] = "Stage 2 API call failed after retries, using Stage 1 result"
                    audit_record["final_translation"] = final_translation # Log stage 1 result as final
                    log_audit_record(audit_record)
                else:
                    # Stage 3: Refinement
                    # Prepare refinement prompt (needs proper templating)
                    refine_prompt = stage3_system_prompt.replace("<<SOURCE>>", source_text)\
                                                        .replace("<<INITIAL>>", initial_translation)\
                                                        .replace("<<FEEDBACK>>", evaluation)
                    # The user content for refine is often just the instruction to refine based on context
                    # Or sometimes the refine prompt itself contains all context. Let's assume the latter for now.
                    final_translation = call_active_api(refine_prompt, "Refine the translation based on the provided feedback.", row_identifier=f"{row_index + 2}-S3")
                    audit_record["final_translation"] = final_translation

                    if final_translation is None:
                        # If stage 3 fails, use Stage 1 translation as final
                        final_translation = initial_translation 
                        with print_lock: translated_counter += 1 # Count Stage 1 as success
                        audit_record["error"] = "Stage 3 API call failed after retries, using Stage 1 result"
                        audit_record["final_translation"] = final_translation # Log stage 1 result as final again
                    else:
                         with print_lock: translated_counter += 1 # Count final stage 3 as success
                    
                    log_audit_record(audit_record)
        else:
             with print_lock: print(f"Error: Unknown TRANSLATION_MODE '{TRANSLATION_MODE}'")
             error_counter += 1
             final_translation = "" 
             audit_record["error"] = f"Unknown TRANSLATION_MODE: {TRANSLATION_MODE}"
             log_audit_record(audit_record)

    except Exception as e:
        # Catch unexpected errors within the worker's logic
        with print_lock:
            print(f"Critical Error in worker for row {row_index+2}: {e}")
        error_counter += 1
        final_translation = "" # Ensure it's empty on critical worker error
        audit_record["error"] = f"Critical worker error: {e}"
        log_audit_record(audit_record)

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
        print(f"Translating {total_rows} rows using {ACTIVE_API} (up to {MAX_WORKER_THREADS} threads)... Output: {output_file}")

        with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKER_THREADS) as executor:
            futures = [executor.submit(translate_row_worker, row_data) for row_data in rows_to_process]
            for future in tqdm(concurrent.futures.as_completed(futures), total=total_rows, desc=f"Translating ({ACTIVE_API} - {TRANSLATION_MODE})"):
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
    # Check presence of keys for the *active* API
    print(f"Selected API: {ACTIVE_API}")
    if ACTIVE_API == "PERPLEXITY" and (not PPLX_API_KEY or not PPLX_MODEL):
        print(f"Critical Error: PERPLEXITY_API_KEY or PERPLEXITY_MODEL not found in .env file for active API. Exiting.")
        exit(1)
    elif ACTIVE_API == "OPENAI" and (not OPENAI_API_KEY or not OPENAI_MODEL):
        print(f"Critical Error: OPENAI_API_KEY or OPENAI_MODEL not found in .env file for active API. Exiting.")
        exit(1)
    elif ACTIVE_API == "GEMINI" and (not GEMINI_API_KEY or not GEMINI_MODEL):
        print(f"Critical Error: GEMINI_API_KEY or GEMINI_MODEL not found in .env file for active API. Exiting.")
        exit(1)
    elif ACTIVE_API not in ["PERPLEXITY", "OPENAI", "GEMINI"]:
         print(f"Critical Error: Invalid ACTIVE_API value '{ACTIVE_API}'. Choose 'PERPLEXITY', 'OPENAI', or 'GEMINI'. Exiting.")
         exit(1)
         
    # Initialize OpenAI client if needed (moved from global scope for clarity)
    if ACTIVE_API == "OPENAI" and not openai_client:
        try:
            openai_client = openai.OpenAI(api_key=OPENAI_API_KEY)
            print("OpenAI client initialized.")
        except Exception as e:
            print(f"Critical Error: Failed to initialize OpenAI client: {e}. Exiting.")
            exit(1)
            
    # Load system prompts based on mode
    load_system_prompts()
        
    # Run the main translation process (prompts are now global)
    translate_csv(INPUT_CSV, OUTPUT_CSV) 
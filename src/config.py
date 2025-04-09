import os
from dotenv import load_dotenv

load_dotenv()

# --- Language Configuration ---
LANGUAGE_CODE = "esLA" # Default, will be overridden by selections
AVAILABLE_LANGUAGES = [lang.strip() for lang in os.getenv("AVAILABLE_LANGUAGES", "esLA").split('#')[0].strip().strip('"').strip("'").split(',') if lang.strip()] # Ensure stripping after split
LANGUAGE_NAME_MAP = {
    "esLA": "Latin American Spanish",
    "frFR": "French (France)",
    # Add other languages here
}
GLOBAL_RULES_FILE = "system_prompts/global.md"

# --- API Configuration ---
ACTIVE_API = os.getenv("ACTIVE_API", "PERPLEXITY").split('#')[0].strip().strip('"').strip("'").upper()
PPLX_API_KEY = os.getenv("PERPLEXITY_API_KEY")
PPLX_MODEL = os.getenv("PERPLEXITY_MODEL")
PPLX_API_URL = "https://api.perplexity.ai/chat/completions"
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL = os.getenv("OPENAI_MODEL")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_MODEL = os.getenv("GEMINI_MODEL")

# --- Celery / Redis --- #
CELERY_BROKER_URL = os.getenv("CELERY_BROKER_URL", "redis://localhost:6379/0").split('#')[0].strip().strip('"').strip("'")
CELERY_RESULT_BACKEND = os.getenv("CELERY_RESULT_BACKEND", "redis://localhost:6379/0").split('#')[0].strip().strip('"').strip("'")

# --- Workflow Configuration ---
TRANSLATION_MODE = os.getenv("TRANSLATION_MODE", "ONE_STAGE").split('#')[0].strip().strip('"').strip("'").upper()
DEFAULT_API = os.getenv("ACTIVE_API", "PERPLEXITY").split('#')[0].strip().strip('"').strip("'").upper()
AUDIT_LOG_FILE = os.getenv("AUDIT_LOG_FILE", f"output/audit_log_{LANGUAGE_CODE}_{DEFAULT_API.lower()}.jsonl").split('#')[0].strip().strip('"').strip("'")
STAGE1_API = os.getenv("STAGE1_API", DEFAULT_API).split('#')[0].strip().strip('"').strip("'").upper()
STAGE2_API = os.getenv("STAGE2_API", DEFAULT_API).split('#')[0].strip().strip('"').strip("'").upper()
STAGE3_API = os.getenv("STAGE3_API", DEFAULT_API).split('#')[0].strip().strip('"').strip("'").upper()
STAGE1_MODEL_OVERRIDE = os.getenv("STAGE1_MODEL", "").split('#')[0].strip().strip('"').strip("'") or None
STAGE2_MODEL_OVERRIDE = os.getenv("STAGE2_MODEL", "").split('#')[0].strip().strip('"').strip("'") or None
STAGE3_MODEL_OVERRIDE = os.getenv("STAGE3_MODEL", "").split('#')[0].strip().strip('"').strip("'") or None
STAGE1_PROMPT_FILE_TPL = "system_prompts/tg_{lang_code}.md" # Template path
STAGE2_TEMPLATE_FILE = "system_prompts/stage2_evaluate_template.md" 
STAGE3_TEMPLATE_FILE = "system_prompts/stage3_refine_template.md" 

# --- CSV/DB Configuration ---
INPUT_CSV_DIR = "input/" # Base directory for inputs
OUTPUT_DIR = "output/" # Base directory for outputs
DATABASE_FILE = os.getenv("DATABASE_FILE", "output/translation_jobs.db").split('#')[0].strip().strip('"').strip("'")
SOURCE_COLUMN = "src_enUS"
TARGET_COLUMN_TPL = "tg_{lang_code}"

# --- Threading and Retry Configuration ---
MAX_WORKER_THREADS_STR = os.getenv("MAX_WORKER_THREADS", "8")
MAX_WORKER_THREADS = int(MAX_WORKER_THREADS_STR.split('#')[0].strip())
API_MAX_RETRIES_STR = os.getenv("API_MAX_RETRIES", "3")
API_MAX_RETRIES = int(API_MAX_RETRIES_STR.split('#')[0].strip())
API_INITIAL_RETRY_DELAY_STR = os.getenv("API_INITIAL_RETRY_DELAY", "5.0")
API_INITIAL_RETRY_DELAY = float(API_INITIAL_RETRY_DELAY_STR.split('#')[0].strip())
API_MAX_RETRY_DELAY_STR = os.getenv("API_MAX_RETRY_DELAY", "60.0")
API_MAX_RETRY_DELAY = float(API_MAX_RETRY_DELAY_STR.split('#')[0].strip())
THREAD_STAGGER_DELAY_STR = os.getenv("THREAD_STAGGER_DELAY", "1.0") 
THREAD_STAGGER_DELAY = float(THREAD_STAGGER_DELAY_STR.split('#')[0].strip())

# --- Rules Archiving ---
ARCHIVE_DIR = os.getenv("ARCHIVE_DIR", "system_prompts/archive/").split('#')[0].strip().strip('"').strip("'")

# --- Validation ---
VALID_APIS = ["PERPLEXITY", "OPENAI", "GEMINI"]
VALID_TRANSLATION_MODES = ["ONE_STAGE", "THREE_STAGE"]

# Perform validation *after* stripping quotes/comments
if TRANSLATION_MODE not in VALID_TRANSLATION_MODES:
    print(f"Warning: Invalid TRANSLATION_MODE '{TRANSLATION_MODE}'. Defaulting to ONE_STAGE.")
    TRANSLATION_MODE = "ONE_STAGE"

all_apis = [DEFAULT_API, STAGE1_API, STAGE2_API, STAGE3_API]
for api in all_apis:
    # Check only non-empty API names
    if api and api not in VALID_APIS:
        print(f"Warning: Invalid API name '{api}' found in config. Check .env comments/values.")

# Ensure output/archive directories exist
os.makedirs(OUTPUT_DIR, exist_ok=True)
os.makedirs(os.path.dirname(DATABASE_FILE), exist_ok=True) # Ensure DB directory exists
os.makedirs(ARCHIVE_DIR, exist_ok=True)

print("Config loaded.") 
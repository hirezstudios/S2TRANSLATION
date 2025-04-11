# SMITE 2 Translation Helper (Flask Version)

This tool facilitates the translation of game text strings for SMITE 2 using various large language models (LLMs), providing a web interface for managing the translation workflow.

**Current Status:** Web UI allows uploading CSV files, configuring and running **ONE_STAGE**, **THREE_STAGE**, or **FOUR_STAGE** translation jobs using different LLMs (Perplexity, OpenAI, Gemini) via a Flask backend. It supports tracking progress, viewing batch history, viewing/editing translation results, viewing/editing prompt rules, managing OpenAI Vector Stores, and exporting results. Supports optional Vector Store assistance for OpenAI and optional batch-specific prompt instructions.

## Architecture

This application uses a Flask + Python Threading + Frontend Polling architecture:

1.  **Flask Web Server (`app.py`):** Handles HTTP requests, serves the HTML frontend, manages background job initiation, provides status, results, rules editing, vector store admin, and export endpoints.
2.  **Frontend (`templates/`):** HTML interface using Bootstrap. JavaScript handles file uploads, dynamic language selection, form submission via Fetch API, polling the status endpoint, and interactive tables/editors (Tabulator, CodeMirror).
3.  **Backend Logic (`src/`):** Core Python modules for configuration, database management, prompt construction, API interactions, background task execution, and vector store management.
4.  **SQLite Database (`instance/translation_jobs.db`):** Stores batch information, individual translation task details (including Stage 0 glossary output), status, intermediate results, final translations, rule edit history, and vector store mappings.
5.  **Configuration (`.env`):** Stores API keys, model names, language mappings, database path, etc. **Must be created manually and is not tracked by Git.**
6.  **Prompts (`system_prompts/`, `user_prompts/`):** Contains rule files and templates used for LLM prompts, including the Stage 0 glossary generation template. User overrides can be placed in `user_prompts/` (ignored by Git).
7.  **OpenAI Vector Stores:** Optionally created and managed via the Admin UI to provide context for OpenAI translations.

## Features

*   **CSV Upload:** Upload input files containing source text (`src_enUS`) and target language columns (`tg_XXXX`).
*   **Dynamic Language Detection:** Identifies potentially translatable languages from input file columns and prompt files.
*   **Mode Selection:** Choose between:
    *   `ONE_STAGE`: Direct translation.
    *   `THREE_STAGE`: Translate -> Evaluate -> Refine.
    *   `FOUR_STAGE`: **Glossary Generation (S0)** -> Translate (S1) -> Evaluate (S2) -> Refine (S3).
*   **API/Model Configuration:** Select API providers and optionally override default models for each stage (S0 must be OpenAI).
*   **Batch-Specific Prompt:** Optionally provide additional instructions for an entire batch on the Home page.
*   **Vector Store Assistance (Optional/Mandatory):** 
    *   Uses OpenAI's File Search tool with managed vector stores.
    *   Optional for S1/S3 in ONE/THREE stage modes (if OpenAI API is selected).
    *   Mandatory for S0 in FOUR_STAGE mode (checkbox is auto-selected and disabled).
*   **Stage 0 Glossary Generation (Four Stage Mode):** 
    *   Uses an OpenAI model (via Responses API) with File Search against the active vector store for the target language.
    *   Analyzes the source text and consults the VS + project rules to create a concise English-to-Target glossary for key terms.
    *   This glossary is prepended to the context for Stages 1, 2, and 3.
*   **Background Processing:** Translation jobs run in background threads.
*   **Database State:** Job progress and results stored persistently in SQLite.
*   **Status Monitoring:** UI polling updates status and progress bar on the Home page.
*   **Batch History:** View a table of past translation jobs.
*   **View/Edit Results:** Review and edit translations in an interactive table (Tabulator) with status tracking and re-translation capability.
*   **View/Edit Prompt Rules:** List, view, and edit prompt rule files, with automatic archiving and user override support.
*   **Vector Store Admin:** UI to prepare and activate OpenAI vector store sets from CSV files.
*   **Results Export:** 
    *   Download final approved translations (CSV).
    *   Download detailed stage reports (for THREE_STAGE and FOUR_STAGE modes) including Stage 0 glossary/raw output (CSV).

## Setup and Running

1.  **Prerequisites:** Python 3.9+, Git.
2.  **Clone Repository:** `git clone <repo_url> && cd <repo_dir>`
3.  **Create Virtual Environment:** `python3 -m venv .venv && source .venv/bin/activate` (or `.venv\Scripts\activate` on Windows)
4.  **Install Dependencies:** `pip install -r requirements.txt`
5.  **Configure Environment (`.env`):** 
    *   Copy `.env.example` to `.env`.
    *   Fill in API keys (Perplexity, OpenAI, Gemini).
    *   Set `S0_MODEL` (default `gpt-4o`), `STAGE1_MODEL`, `STAGE2_MODEL`, `STAGE3_MODEL` if desired.
    *   Ensure OpenAI models support the Responses API if using Vector Stores/Four Stage mode (e.g., `gpt-4o`, `gpt-4o-mini`).
    *   Review other settings (paths, defaults).
6.  **Initialize Database:** The database (`instance/translation_jobs.db`) should be created/updated automatically on the first run. Manual init/reset: `python -c "from src import db_manager; db_manager.init_db()"`.
7.  **Run Flask Server:** `flask run`.
8.  Access at `http://127.0.0.1:5000`.

## Usage

1.  **Admin Page (First Use/Update VS):** Prepare and activate an OpenAI Vector Store set if using VS features.
2.  **Home Page:** Upload CSV, select languages, (optionally) add batch prompt, choose mode (defaults to Four Stage), configure APIs/Models (S0-S3 as applicable), ensure "Use Vector Store" is checked if needed (auto for Four Stage), start job.
3.  **Monitor:** Track progress on the Home page.
4.  **Batch History:** View past jobs.
5.  **Results Page:** Access via links. Review/edit translations. Download reports.
6.  **Rules Page:** View/edit rule files.

## Project Structure

```
├── app.py                  # Main Flask application
├── requirements.txt        # Python dependencies
├── .env.example            # Example environment file
├── .env                    # Environment variables (**DO NOT COMMIT**)
├── .gitignore              # Files/directories ignored by git
├── README.md               # This file
├── src/
│   ├── __init__.py
│   ├── api_clients.py        # Initializes API clients
│   ├── config.py             # Loads configuration from .env
│   ├── db_manager.py         # Database interaction logic
│   ├── prompt_manager.py     # Loads and constructs prompts
│   ├── translation_service.py # Core translation and background task logic
│   └── vector_store_manager.py # OpenAI Vector Store management
├── system_prompts/         # Default prompt rule files
│   ├── archive/            # Archived versions of edited rules
│   ├── stage0_glossary_template.md # New
│   └── ... (global.md, tg_*.md, stage*.md)
├── templates/              # HTML templates for Flask
│   ├── includes/           # Reusable template fragments (e.g., _vs_status_modal.html)
│   ├── admin.html
│   ├── base.html
│   ├── history.html
│   ├── index.html
│   ├── results.html
│   ├── rules_edit.html
│   ├── rules_list.html
│   └── rules_view.html
├── user_prompts/           # User overrides for prompt files (**DO NOT COMMIT**)
├── instance/               # SQLite database file location (**DO NOT COMMIT**)
├── output/                 # Default location for export files (**DO NOT COMMIT**)
├── uploads/                # Temporary storage for uploads (**DO NOT COMMIT**)
└── .cursor/                # Agent notes and configuration
``` 
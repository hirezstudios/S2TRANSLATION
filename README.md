# SMITE 2 Translation Helper (Flask Version)

This tool facilitates the translation of game text strings for SMITE 2 using various large language models (LLMs), providing a web interface for managing the process.

**Current Status:** Phase 2 Complete. Basic UI allows uploading CSV files, configuring and running ONE_STAGE or THREE_STAGE translation jobs using different LLMs (Perplexity, OpenAI, Gemini) via a Flask backend, tracking progress via database polling, and exporting results.

## Architecture

This application uses a Flask + Python Threading + Frontend Polling architecture:

1.  **Flask Web Server (`app.py`):** Handles HTTP requests, serves the basic HTML frontend, manages background job initiation, provides status/export endpoints.
2.  **Frontend (`templates/index.html`):** Basic HTML interface using Bootstrap for layout. JavaScript handles file uploads, dynamic language selection, form submission via Fetch API, and polling the status endpoint to update the UI.
3.  **Backend Logic (`src/`):**
    *   `translation_service.py`: Orchestrates batch preparation, starts background processing threads, handles export generation. Contains the `run_batch_background` function (which uses a `ThreadPoolExecutor` for row-level parallelism) and the `translate_row_worker` function (implementing ONE/THREE stage logic).
    *   `config.py`: Loads and manages configuration from the `.env` file.
    *   `db_manager.py`: Handles all interactions with the SQLite database.
    *   `prompt_manager.py`: Loads global rules, language-specific rules, and stage templates; constructs prompts dynamically.
    *   `api_clients.py`: Initializes API clients (currently OpenAI).
4.  **SQLite Database (`output/translation_jobs.db`):** Stores batch information (including configuration used), individual translation task details, status, intermediate results (for 3-stage), and final translations.
5.  **Configuration (`.env`):** Stores API keys, model names, language lists, database path, translation mode defaults, etc.
6.  **Prompts (`system_prompts/`):** Contains `global.md` for common rules, `tg_*.md` files for language-specific rules (Stage 1), and templates for Stage 2/3.

## Features (Current)

*   **CSV Upload:** Upload input files containing source text (`src_enUS`) and target language columns (`tg_XXXX`).
*   **Dynamic Language Detection:** Identifies available languages based on input file columns, `.env` configuration (`AVAILABLE_LANGUAGES`), and existence of corresponding `system_prompts/tg_XXXX.md` files.
*   **Mode Selection:** Choose between:
    *   `ONE_STAGE`: Direct translation using a selected API/model.
    *   `THREE_STAGE`: Translate (Stage 1) -> Evaluate (Stage 2) -> Refine (Stage 3).
*   **API/Model Configuration:** Select the API provider (Perplexity, OpenAI, Gemini) and optionally override the default model for `ONE_STAGE` or each stage in `THREE_STAGE` mode. Defaults are read from `.env`.
*   **Background Processing:** Translation jobs run in a background thread managed by Flask to avoid blocking the UI.
*   **Database State:** Job progress and results are stored persistently in an SQLite database.
*   **Status Monitoring:** Basic UI polling updates status messages and a progress bar.
*   **Audit Logging:** Detailed JSONL file (`output/translation_audit_log.jsonl`) records the steps and results for each task, especially useful for `THREE_STAGE`.
*   **Results Export:** Download final translations in a CSV format matching the input structure (based on stored header).
*   **Stages Report Export:** For `THREE_STAGE` jobs, download a detailed CSV report showing results from all stages.

## Setup and Running

1.  **Prerequisites:**
    *   Python 3.9+
    *   Git

2.  **Clone Repository:**
    ```bash
    git clone https://github.com/hirezstudios/S2TRANSLATION.git
    cd S2TRANSLATION
    ```

3.  **Create Virtual Environment:**
    ```bash
    python3 -m venv .venv
    source .venv/bin/activate  # On Windows use `.venv\Scripts\activate`
    ```

4.  **Install Dependencies:**
    ```bash
    pip install -r requirements.txt
    ```

5.  **Configure Environment (`.env`):**
    *   Create a `.env` file in the project root (or copy `.env.example` if one exists).
    *   Add your API keys for Perplexity, OpenAI, and/or Gemini:
        ```dotenv
        PERPLEXITY_API_KEY="pplx-..."
        OPENAI_API_KEY="sk-..."
        GEMINI_API_KEY="AIza..."
        ```
    *   Review and adjust other settings (default models, available languages, API choices for stages, etc.) as needed. Ensure `DATABASE_FILE` path is correct.

6.  **Initialize Database:** (Should happen automatically when Flask starts, but can be run manually)
    ```bash
    python -m src.db_manager 
    ```

7.  **Run Flask Development Server:**
    ```bash
    python app.py
    ```
    The application should be accessible at `http://127.0.0.1:5000` (or the address shown in the terminal).

## Usage

1.  Navigate to the application URL in your browser.
2.  Upload your input CSV file using the "Choose File" button.
3.  Wait for the "Languages to Translate" section to populate based on your file and configuration. Select the languages you want to process (defaults to all valid languages found).
4.  Choose the "Translation Mode" (One Stage or Three Stage).
5.  Configure the API provider(s) and optional model overrides for the selected mode.
6.  Click "Start Translation Job".
7.  Monitor the status area and progress bar. Check the Flask terminal logs for detailed background activity.
8.  Once the status shows "completed" or "completed_with_errors", download links for the output CSV(s) will appear in the "Export Results" section.
9.  The SQLite database (`output/translation_jobs.db`) and the audit log (`output/translation_audit_log.jsonl`) contain detailed records of the process.

## Project Structure

```
├── app.py                  # Main Flask application
├── requirements.txt        # Python dependencies
├── .env                    # Environment variables (API keys, config - **DO NOT COMMIT**)
├── .gitignore              # Files/directories ignored by git
├── pytest.ini              # Pytest configuration
├── celery_app.py           # REMOVED
├── input/
│   └── blake-small.csv     # Example input file
├── output/
│   ├── translation_jobs.db # SQLite database
│   └── *.csv               # Generated output files
│   └── *.jsonl             # Generated audit logs
├── src/
│   ├── __init__.py
│   ├── config.py           # Loads .env configuration
│   ├── db_manager.py       # Database interaction logic
│   ├── prompt_manager.py   # Loads and constructs prompts
│   ├── api_clients.py      # Initializes API clients
│   └── translation_service.py # Core translation logic, background task
├── system_prompts/
│   ├── global.md           # Global translation rules
│   ├── tg_esLA.md          # Language-specific rules (Spanish)
│   ├── tg_frFR.md          # Language-specific rules (French)
│   ├── stage2_evaluate_template.md # Template for Stage 2
│   ├── stage3_refine_template.md   # Template for Stage 3
│   └── archive/            # Directory for archived rule files (future)
├── templates/
│   ├── base.html           # Base HTML template
│   └── index.html          # Main page template
├── tests/
│   ├── __init__.py
│   └── test_app.py         # Basic Streamlit/UI tests (Outdated)
│   └── test_*.py           # TODO: Add backend unit tests
└── temp_uploads/           # Temporary storage for uploaded files (ignored)
``` 
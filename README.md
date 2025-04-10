# SMITE 2 Translation Helper (Flask Version)

This tool facilitates the translation of game text strings for SMITE 2 using various large language models (LLMs), providing a web interface for managing the process.

**Current Status:** Web UI allows uploading CSV files, configuring and running ONE_STAGE or THREE_STAGE translation jobs using different LLMs (Perplexity, OpenAI, Gemini) via a Flask backend, tracking progress via database polling, viewing batch history, viewing/editing results, and exporting results.

## Architecture

This application uses a Flask + Python Threading + Frontend Polling architecture:

1.  **Flask Web Server (`app.py`):** Handles HTTP requests, serves the HTML frontend, manages background job initiation, provides status/results/export endpoints.
2.  **Frontend (`templates/`):** HTML interface using Bootstrap. JavaScript handles file uploads, dynamic language selection, form submission via Fetch API, and polling the status endpoint to update the UI. Key templates:
    *   `base.html`: Base structure and navigation.
    *   `index.html`: Home page for upload, configuration, and job start.
    *   `history.html`: Displays past batch jobs.
    *   `results.html`: Displays and allows editing of translation results for a specific batch.
3.  **Backend Logic (`src/`):**
    *   `translation_service.py`: Orchestrates batch preparation, starts background processing threads, handles export generation. Contains the `run_batch_background` function (which uses a `ThreadPoolExecutor` for row-level parallelism) and the `translate_row_worker` function (implementing ONE/THREE stage logic).
    *   `config.py`: Loads and manages configuration from the `.env` file (API keys, default models, paths, etc.).
    *   `db_manager.py`: Handles all interactions with the SQLite database.
    *   `prompt_manager.py`: Loads global rules, language-specific rules, and stage templates; constructs prompts dynamically; discovers available languages based on CSV headers and configuration.
    *   `api_clients.py`: Initializes API clients (Perplexity, OpenAI, Gemini).
4.  **SQLite Database (`output/translation_jobs.db`):** Stores batch information (including configuration used), individual translation task details, status, intermediate results (for 3-stage), and final translations.
5.  **Configuration (`.env`):** Stores API keys, model names, language mappings, database path, translation mode defaults, etc. **Must be created manually and is not tracked by Git.**
6.  **Prompts (`system_prompts/`):** Contains `global.md` for common rules, `tg_*.md` files for language-specific rules (Stage 1), and templates for Stage 2/3.

## Features (Current)

*   **CSV Upload:** Upload input files containing source text (`src_enUS`) and target language columns (`tg_XXXX`).
*   **Dynamic Language Detection:** Identifies potentially translatable languages based on input file columns matching the pattern `tg_XXXX` and checks against configured language mappings and the existence of corresponding `system_prompts/tg_XXXX.md` files.
*   **Mode Selection:** Choose between:
    *   `ONE_STAGE`: Direct translation using a selected API/model.
    *   `THREE_STAGE`: Translate (Stage 1) -> Evaluate (Stage 2) -> Refine (Stage 3).
*   **API/Model Configuration:** Select the API provider (Perplexity, OpenAI, Gemini) and optionally override the default model for `ONE_STAGE` or each stage in `THREE_STAGE` mode. Defaults are read from `.env`.
*   **Background Processing:** Translation jobs run in background threads managed by Flask to avoid blocking the UI.
*   **Database State:** Job progress and results are stored persistently in an SQLite database.
*   **Status Monitoring:** UI polling updates status messages and a progress bar on the Home page.
*   **Batch History:** View a table of past translation jobs with their status and configurations on the "Batch History" page.
*   **View/Edit Results:** Access a dedicated page (`/results/<batch_id>`) to view and edit the translations for a completed batch.
*   **Audit Logging:** Detailed JSONL file (`output/translation_audit_log.jsonl`) records the steps and results for each task, especially useful for `THREE_STAGE`.
*   **Results Export:** Download final translations in a CSV format matching the input structure (based on stored header).
*   **Stages Report Export:** For `THREE_STAGE` jobs, download a detailed CSV report showing results from all stages.

## Setup and Running

1.  **Prerequisites:**
    *   Python 3.9+
    *   Git

2.  **Clone Repository:**
    ```bash
    git clone https://github.com/hirezstudios/S2TRANSLATION.git # Or your fork
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
    *   Create a `.env` file in the project root (you can copy `.env.example` and rename it).
    *   Add your API keys for Perplexity, OpenAI, and/or Gemini:
        ```dotenv
        PERPLEXITY_API_KEY="pplx-..."
        OPENAI_API_KEY="sk-..."
        GEMINI_API_KEY="AIza..."
        ```
    *   Review and adjust other settings (default models, language name mappings, API choices for stages, etc.) as needed. Ensure `DATABASE_FILE` path is correct (defaults to `output/translation_jobs.db`).

6.  **Initialize Database:** (The application attempts this automatically on startup if the file doesn't exist, but you can run it manually to be sure)
    ```bash
    python -m src.db_manager
    ```

7.  **Run Flask Development Server:**
    ```bash
    flask run
    # Or: python app.py
    ```
    The application should be accessible at `http://127.0.0.1:5000` (or the address shown in the terminal).

## Usage

1.  Navigate to the application URL (typically `http://127.0.0.1:5000`) in your browser. You'll land on the "Home" page.
2.  Upload your input CSV file using the "Choose File" button.
3.  Wait for the "Languages to Translate" section to populate. Select/deselect languages using the checkboxes or the "Select All"/"Unselect All" buttons.
4.  Choose the "Translation Mode" (One Stage or Three Stage).
5.  Optionally, expand "Advanced API/Model Settings" to configure the API provider(s) and model overrides for the selected mode.
6.  Click "Start Translation Job".
7.  Monitor the status area and progress bar on the Home page. Check the Flask terminal logs for detailed background activity.
8.  Navigate to the "Batch History" page to see a summary of this and previous jobs.
9.  Once a job status shows "completed" or "completed_with_errors", click the "View/Edit Results" link on the Home page status area or find the corresponding link on the Batch History page.
10. On the Results page, review translations and make edits as needed. Click "Save Changes" to update the database.
11. Download links for the output CSV(s) are available on the Results page.
12. The SQLite database (`output/translation_jobs.db`) and the audit log (`output/translation_audit_log.jsonl`) contain detailed records of the process.

## Project Structure

```
├── app.py                  # Main Flask application
├── requirements.txt        # Python dependencies
├── .env.example            # Example environment file
├── .env                    # Environment variables (API keys, config - **DO NOT COMMIT**)
├── .gitignore              # Files/directories ignored by git
├── README.md               # This file
├── pytest.ini              # Pytest configuration
├── input/                  # Directory for sample input files (Optional)
│   └── example.csv
├── output/                 # Directory for generated files (Created automatically, ignored by git)
│   ├── translation_jobs.db # SQLite database
│   └── *.csv               # Generated output files
│   └── *.jsonl             # Generated audit logs
├── src/
│   ├── __init__.py
│   ├── config.py           # Loads .env configuration
│   ├── db_manager.py       # Database interaction logic
│   ├── prompt_manager.py   # Loads/constructs prompts, language discovery
│   ├── api_clients.py      # Initializes API clients
│   └── translation_service.py # Core translation logic, background task manager
├── system_prompts/         # Directory for prompt files
│   ├── global.md           # Global translation rules
│   ├── tg_*.md             # Language-specific rules (e.g., tg_frFR.md, tg_esLA.md)
│   ├── stage2_evaluate_template.md # Template for Stage 2
│   └── stage3_refine_template.md   # Template for Stage 3
├── templates/              # HTML templates for Flask
│   ├── base.html           # Base HTML template (navigation, structure)
│   ├── index.html          # Home page template
│   ├── history.html        # Batch history page template
│   └── results.html        # Results viewing/editing page template
├── tests/                  # Directory for test files (Currently minimal)
│   ├── __init__.py
│   └── test_*.py           # Placeholder for future tests
└── temp_uploads/           # Temporary storage for uploaded files (Created automatically, ignored by git)
``` 
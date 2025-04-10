import os
import glob
from . import config
import logging

logger = logging.getLogger(__name__)

global_rules_content = None
stage1_templates = {}
stage2_template_prompt = None
stage3_template_prompt = None
available_languages = []

def discover_available_languages():
    """Discovers available languages by scanning for stage 1 prompt files."""
    global available_languages
    available_languages = []
    pattern = config.STAGE1_PROMPT_FILE_TPL.format(lang_code='*')
    logger.info(f"Scanning for language prompts using pattern: {pattern}")
    try:
        prompt_files = glob.glob(pattern)
        for filepath in prompt_files:
            # Extract lang_code from filename like 'system_prompts/tg_enGB.md'
            filename = os.path.basename(filepath)
            parts = filename.split('_')
            # Corrected check for 'tg_' prefix and '.md' suffix
            if len(parts) == 2 and parts[0] == 'tg' and parts[1].endswith('.md'):
                lang_code = parts[1][:-3] # Remove .md extension
                if lang_code not in available_languages:
                     available_languages.append(lang_code)
                     logger.info(f"Discovered language: {lang_code} from {filepath}")
            else:
                 logger.warning(f"Skipping file with unexpected format: {filepath}")
        if not available_languages:
             logger.warning("No language-specific Stage 1 prompt files found.")
    except Exception as e:
         logger.error(f"Error scanning for language prompts: {e}")
    logger.info(f"Available languages discovered: {available_languages}")
    return available_languages

def load_single_prompt_file(filepath):
    """Loads content from a single prompt file."""
    if not os.path.exists(filepath):
        logger.warning(f"Prompt file not found: {filepath}")
        return None
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            return f.read()
    except Exception as e:
        logger.error(f"Error reading prompt file {filepath}: {e}")
        return None

def load_prompts():
    """Loads global rules, language-specific stage 1 bases, and stage 2/3 templates."""
    global global_rules_content, stage1_templates, stage2_template_prompt, stage3_template_prompt, available_languages

    logger.info("Discovering available languages...")
    discover_available_languages()

    logger.info(f"Loading Global Rules from {config.GLOBAL_RULES_FILE}...")
    global_rules_content = load_single_prompt_file(config.GLOBAL_RULES_FILE)
    if not global_rules_content:
        logger.error(f"Critical Error: Global rules file {config.GLOBAL_RULES_FILE} is missing or empty. Exiting.")
        exit(1)

    logger.info("Loading language-specific Stage 1 prompts...")
    stage1_templates = {}
    for lang_code in available_languages:
        filepath = config.STAGE1_PROMPT_FILE_TPL.format(lang_code=lang_code)
        content = load_single_prompt_file(filepath)
        if content:
            stage1_templates[lang_code] = content
            logger.info(f"Loaded Stage 1 prompt for: {lang_code}")
        else:
            logger.warning(f"Missing Stage 1 prompt file for language: {lang_code} ({filepath}) - this language will not be available.")
            
    if config.TRANSLATION_MODE == "THREE_STAGE":
        logger.info(f"Loading Stage 2 Template from {config.STAGE2_TEMPLATE_FILE}...")
        stage2_template_prompt = load_single_prompt_file(config.STAGE2_TEMPLATE_FILE)
        if not stage2_template_prompt:
             logger.error(f"Critical Error: Stage 2 template {config.STAGE2_TEMPLATE_FILE} missing for THREE_STAGE mode. Exiting.")
             exit(1)
             
        logger.info(f"Loading Stage 3 Template from {config.STAGE3_TEMPLATE_FILE}...")
        stage3_template_prompt = load_single_prompt_file(config.STAGE3_TEMPLATE_FILE)
        if not stage3_template_prompt:
             logger.error(f"Critical Error: Stage 3 template {config.STAGE3_TEMPLATE_FILE} missing for THREE_STAGE mode. Exiting.")
             exit(1)
    logger.info("Prompt loading complete.")

def get_full_prompt(stage, lang_code, source_text=None, initial_translation=None, feedback=None):
    """Constructs the full system prompt for a given stage and language."""
    
    # Final instruction (applied to all stages)
    final_instruction = "\n\n**IMPORTANT FINAL INSTRUCTION: Your final output should contain ONLY the final text (translation/evaluation/revision). No extra text, formatting, or explanations.**"
    
    # Get base language rules
    lang_specific_part = stage1_templates.get(lang_code)
    if not lang_specific_part:
        raise ValueError(f"Language specific prompt for {lang_code} not loaded.")
    lang_name = config.LANGUAGE_NAME_MAP.get(lang_code, lang_code)
    lang_specific_part = lang_specific_part.replace("<<TARGET_LANGUAGE_NAME>>", lang_name)
        
    full_ruleset = f"{lang_specific_part}\n\n{global_rules_content}"

    if stage == 1:
        return full_ruleset + final_instruction
    
    elif stage == 2:
        if not stage2_template_prompt:
            raise ValueError("Stage 2 template not loaded.")
        prompt = stage2_template_prompt.replace("<<TARGET_LANGUAGE_NAME>>", lang_name)\
                                       .replace("<<RULES>>", full_ruleset)\
                                       .replace("<<SOURCE_TEXT>>", source_text or "")\
                                       .replace("<<INITIAL_TRANSLATION>>", initial_translation or "")
        return prompt + final_instruction
        
    elif stage == 3:
        if not stage3_template_prompt:
            raise ValueError("Stage 3 template not loaded.")
        prompt = stage3_template_prompt.replace("<<TARGET_LANGUAGE_NAME>>", lang_name)\
                                       .replace("<<RULES>>", full_ruleset)\
                                       .replace("<<SOURCE_TEXT>>", source_text or "")\
                                       .replace("<<INITIAL_TRANSLATION>>", initial_translation or "")\
                                       .replace("<<FEEDBACK>>", feedback or "")
        return prompt + final_instruction
        
    else:
        raise ValueError(f"Invalid stage number: {stage}")

# Load prompts when module is imported
load_prompts() 
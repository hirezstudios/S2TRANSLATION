import openai
# import google.generativeai as genai # No global config needed now
import logging
from . import config

logger = logging.getLogger(__name__)

# Store initialized clients globally within this module after first init
_openai_client = None
_gemini_key_present = None # Flag if key exists

def get_openai_client():
    """Initializes and returns the OpenAI client singleton if configured."""
    global _openai_client
    if _openai_client is not None:
        return _openai_client
        
    if config.OPENAI_API_KEY:
        try:
            _openai_client = openai.OpenAI(api_key=config.OPENAI_API_KEY)
            logger.info("OpenAI client initialized.")
            return _openai_client
        except Exception as e:
            logger.error(f"Failed to initialize OpenAI client: {e}")
            return None
    else:
        logger.warning("OpenAI API key not found.")
        return None

def check_gemini_readiness():
    """Checks if Gemini API key is present."""
    global _gemini_key_present
    if _gemini_key_present is not None:
        return _gemini_key_present
        
    if config.GEMINI_API_KEY:
        logger.info("Gemini API key found. Configuration will happen per-thread.")
        _gemini_key_present = True
        return True
    else:
        logger.warning("Gemini API key not found.")
        _gemini_key_present = False
        return False

# Perform checks/initialization on first import
get_openai_client() 
check_gemini_readiness() 
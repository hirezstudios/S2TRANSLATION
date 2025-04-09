from celery import Celery
# Assume config.py is in src/
from src import config 
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

logger.info(f"Initializing Celery app with broker: {config.CELERY_BROKER_URL}")

# Create Celery instance
# The first argument is the name of the current module, important for tasks
# The include argument tells Celery where to find task modules
celery = Celery(
    'translation_tasks', 
    broker=config.CELERY_BROKER_URL,
    backend=config.CELERY_RESULT_BACKEND, # Optional: for storing task results
    include=['src.tasks'] # Point to the tasks module within the src package
)

# Optional Celery configuration (can also be done via CELERY_ settings in config)
celery.conf.update(
    result_expires=3600, # Expire results after 1 hour
    task_serializer='json',
    accept_content=['json'],  # Ensure tasks use JSON
    result_serializer='json',
    timezone='UTC', # Use UTC timezone
    enable_utc=True,
)

if __name__ == '__main__':
    # This allows running the worker directly using:
    # celery -A celery_app worker --loglevel=info
    logger.warning("Starting Celery worker directly from celery_app.py is possible but usually done via CLI.")
    celery.start() 
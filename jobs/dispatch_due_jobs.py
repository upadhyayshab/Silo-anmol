import requests
import json
import logging
import argparse
import sys

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("dispatch_due_jobs")

BASE_URL = "http://localhost:8000"

def dispatch_due_jobs(base_url, limit=100):
    endpoint = f"{base_url}/api/v1/smartping/jobs/dispatch-due"
    
    params = {}
    if limit:
        params["limit"] = limit

    logger.info(f"Making POST request to {endpoint} with params: {params}")
    
    try:
        response = requests.post(endpoint, params=params, headers={"Content-Type": "application/json"})
        
        logger.info(f"Status Code: {response.status_code}")
        
        if response.status_code in (200, 201):
            logger.info("Successfully dispatched due jobs.")
            try:
                logger.info(f"Response: {json.dumps(response.json(), indent=2)}")
            except json.JSONDecodeError:
                logger.info(f"Raw Response: {response.text}")
        else:
            logger.error(f"Failed to dispatch jobs. Status Code: {response.status_code}")
            try:
                logger.error(f"Error details: {json.dumps(response.json(), indent=2)}")
            except json.JSONDecodeError:
                logger.error(f"Raw Error: {response.text}")
            sys.exit(1)
            
    except requests.exceptions.ConnectionError:
        logger.error(f"Connection Error: Is your FastAPI server running on {base_url}?")
        sys.exit(1)
    except Exception as e:
        logger.error(f"An unexpected error occurred: {e}")
        sys.exit(1)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Dispatch due SmartPing jobs.")
    parser.add_argument("--url", type=str, default=BASE_URL, help="Base URL of the API server")
    parser.add_argument("--limit", type=int, default=100, help="Maximum number of jobs to dispatch")
    
    args = parser.parse_args()
    
    dispatch_due_jobs(base_url=args.url, limit=args.limit)

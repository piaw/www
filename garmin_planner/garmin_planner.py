import os
import json
import logging
from datetime import datetime, timedelta
from dotenv import load_dotenv

# Garmin Connect
from garminconnect import (
    Garmin,
    GarminConnectAuthenticationError,
    GarminConnectConnectionError,
    GarminConnectTooManyRequestsError,
)

# Gemini API
from google import genai

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

def get_garmin_api():
    """Authenticates and returns the Garmin Connect API client."""
    email = os.getenv("GARMIN_EMAIL")
    password = os.getenv("GARMIN_PASSWORD")

    if not email or not password:
        logger.error("Missing GARMIN_EMAIL or GARMIN_PASSWORD environment variables.")
        exit(1)

    logger.info("Initializing Garmin Connect Client...")
    try:
        # Note: If MFA is required, garminconnect will handle it via interactive prompts
        # when running directly in the terminal for the first time.
        api = Garmin(email, password, is_mfa_enabled=True)
        api.login()
        logger.info("Successfully logged in to Garmin Connect.")
        return api
    except (
        GarminConnectAuthenticationError,
        GarminConnectConnectionError,
        GarminConnectTooManyRequestsError,
    ) as err:
        logger.error("Error occurred during Garmin Connect Client login: %s", err)
        exit(1)

def extract_garmin_data(api):
    """Fetches stats and activities from Garmin Connect."""
    today = datetime.now().date()
    yesterday = today - timedelta(days=1)
    
    logger.info("Fetching Garmin stats for today and yesterday...")
    
    try:
        # User Summary & Stats
        stats_today = api.get_stats(today.isoformat())
        summary_today = api.get_user_summary(today.isoformat())
        
        # Training status if available
        training_status = api.get_training_status(today.isoformat())

        # Recent activities (last 5)
        logger.info("Fetching recent activities...")
        recent_activities = api.get_activities(0, 5)

        return {
            "query_date": str(today),
            "daily_stats": stats_today,
            "user_summary": summary_today,
            "training_status": training_status,
            "recent_activities": recent_activities
        }

    except Exception as e:
        logger.error("Failed to extract data: %s", e)
        return None

def read_reference_template():
    """Reads the reference HTML file (claude/training2026.html)."""
    # Assuming this script is run from garmin_planner/ relative to the www dir
    template_path = os.path.join(os.path.dirname(__file__), "..", "claude", "training2026.html")
    logger.info("Reading reference template from %s", template_path)
    
    try:
        with open(template_path, "r", encoding="utf-8") as f:
            return f.read()
    except Exception as e:
        logger.error("Could not read reference template: %s", e)
        exit(1)

def generate_plan(garmin_data, reference_html):
    """Hits the Gemini API to generate the updated plan."""
    gemini_key = os.getenv("GEMINI_API_KEY")
    if not gemini_key:
        logger.error("Missing GEMINI_API_KEY environment variable.")
        exit(1)

    logger.info("Initializing Gemini API...")
    client = genai.Client(api_key=gemini_key)

    prompt = f"""
I want you to act as an expert cycling coach. I have extracted my most recent health and activity data from Garmin Connect. I want you to generate a personalized training plan HTML file based on my current statistics.

Here is my latest Garmin Connect Data (JSON):
```json
{json.dumps(garmin_data, indent=2)}
```

Here is a reference training plan HTML file that I like. I want you to completely match this design, layout, color scheme, and aesthetic. You must output a fully self-contained HTML file exactly like this one, but updated with my new stats and generating a new 12-week plan dynamically.

Reference HTML:
```html
{reference_html}
```

Instructions:
1. Extract my current metrics from the Garmin JSON (e.g. resting HR, weight, recent volume, VO2max/FTP if available in the training status).
2. Use this data to generate a realistic cycling training plan.
3. Keep the visual design, CSS variables, typography, and tabbed structure IDENTICAL to the reference HTML.
4. Output ONLY the raw HTML code. Do NOT wrap it in ```html markdown blocks. Output exactly text that I can save directly to an .html file.
    """

    logger.info("Sending request to Gemini model (gemini-2.5-flash)... this may take a minute.")
    
    try:
        response = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=prompt,
            config=genai.types.GenerateContentConfig(
                temperature=0.4 # Slightly lower temp for predictable HTML generation
            ),
        )
        return response.text
    except Exception as e:
        logger.error("Error generating content via Gemini: %s", e)
        exit(1)

def save_output(content):
    """Saves the generated HTML to the output file."""
    # Clean output just in case Gemini wrapped it in markdown
    if content.startswith("```html"):
        content = content[len("```html"):]
    if content.endswith("```"):
        content = content[:-len("```")]

    output_path = os.path.join(os.path.dirname(__file__), "..", "claude", "generated_plan.html")
    logger.info("Saving updated plan to: %s", output_path)
    try:
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(content.strip())
        logger.info("Success! New training plan saved.")
    except Exception as e:
        logger.error("Failed to save output file: %s", e)

def main():
    load_dotenv()
    
    api = get_garmin_api()
    garmin_data = extract_garmin_data(api)
    
    if not garmin_data:
        logger.warning("No garmin data retrieved. Exiting.")
        return

    reference_html = read_reference_template()
    generated_html = generate_plan(garmin_data, reference_html)
    
    save_output(generated_html)

if __name__ == "__main__":
    main()

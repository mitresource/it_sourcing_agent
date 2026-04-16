from firecrawl import Firecrawl
import json
from datetime import datetime
from pathlib import Path

FIRECRAWL_API_KEY = "fc-f19ced7d38364783aa268f59c2c0b725"

SEARCH_URL = "https://www.dice.com/jobs?filters.postedDate=THREE&radius=30&q=python+developer&radiusUnit=mi"

DATA_FOLDER = Path("data/raw_jobs")
DATA_FOLDER.mkdir(parents=True, exist_ok=True)

print("🚀 Testing Firecrawl on Dice...\n")

app = Firecrawl(api_key=FIRECRAWL_API_KEY)

try:
    result = app.scrape(
        url=SEARCH_URL,
        formats=[
            "markdown",
            {
                "type": "json",
                "prompt": "Extract all visible job listings from this Dice search results page. Return a list of jobs with: title, company, location, posted_date, job_url"
            }
        ]
    )

    print("✅ Scrape completed successfully!\n")

    # Convert Document object to dictionary so we can save and print
    result_dict = result.model_dump() if hasattr(result, "model_dump") else vars(result)

    # Show what we got
    print("Available attributes:", [attr for attr in dir(result) if not attr.startswith("_")])

    # Save the raw result properly
    timestamp = datetime.now().strftime("%Y%m%d_%H%M")
    raw_file = DATA_FOLDER / f"dice_search_raw_{timestamp}.json"
    
    with open(raw_file, "w", encoding="utf-8") as f:
        json.dump(result_dict, f, indent=2, ensure_ascii=False)

    print(f"✅ Full result saved to: {raw_file}\n")

    # Summary
    if hasattr(result, "markdown") and result.markdown:
        md = result.markdown
        print(f"Markdown length: {len(md)} characters")
        print(f"First 700 characters:\n{md[:700]}...\n")
        
        if "python developer" in md.lower() or "senior" in md.lower() or "job" in md.lower():
            print("✅ Firecrawl successfully fetched Dice job listings!")
            print("The markdown contains real job data from Dice.")
        else:
            print("⚠️ Markdown may be empty or blocked - check the saved file.")
    else:
        print("No markdown found.")

    if hasattr(result, "json") and result.json:
        print("Structured JSON was also returned!")

except Exception as e:
    print(f"❌ Error: {type(e).__name__}: {e}")

print("\nTest finished. Please open the latest file in data/raw_jobs/ folder and tell me:")
print("1. Does the markdown contain job titles like 'Python Developer', company names, locations?")
print("2. Any other output you see.")
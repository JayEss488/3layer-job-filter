import os
import json
import requests
import time
from bs4 import BeautifulSoup
from openai import OpenAI
from email.message import EmailMessage

# 1. Initialize the AI client
client = OpenAI()

# 2. Define the basic company list with their specific project URLs
# 2. Define the comprehensive company list with their relevant context URLs
companies = {
    # Original & First Expansion
    "Durham Wildlife Trust": "https://www.durhamwt.com/what-we-do/projects",
    "Sweco UK": "https://www.sweco.co.uk/",
    "The Climate Coalition": "https://www.theclimatecoalition.org/about-us",
    "Arup": "https://www.arup.com/",
    "RSPB": "https://www.rspb.org.uk/",
    "The Wildlife Trusts": "https://www.wildlifetrusts.org/about-us/what-we-do",
    "Woodland Trust": "https://www.woodlandtrust.org.uk/",
    "Wildfowl & Wetlands Trust": "https://www.wwt.org.uk/our-work/",
    "Earthwatch Europe": "https://earthwatch.org.uk/",
    "Friends of the Earth UK": "https://friendsoftheearth.uk/",
    "Greenpeace UK": "https://www.greenpeace.org.uk/",
    "Carbon Trust": "https://www.carbontrust.com/",
    "Anthesis Group": "https://www.anthesisgroup.com/",
    "Ricardo Energy & Environment": "https://www.ricardo.com/",
    "Eunomia Research & Consulting": "https://eunomia.eco/",
    "Bioregional": "https://www.bioregional.com/about-us",
    "ERM (Environmental Resources Management)": "https://www.erm.com/",
    "WSP UK": "https://www.wsp.com/en-gb",
    "Mott MacDonald": "https://www.mottmac.com/",
    "Buro Happold": "https://www.burohappold.com/projects/",
    
    # Campaign & Conservation NGOs
    "ClientEarth": "https://www.clientearth.org/",
    "Sustrans": "https://www.sustrans.org.uk/",
    "Keep Britain Tidy": "https://www.keepbritaintidy.org/",
    "Marine Conservation Society": "https://www.mcsuk.org/",
    "Rewilding Britain": "https://www.rewildingbritain.org.uk/",
    "Soil Association": "https://www.soilassociation.org/",
    "Possible": "https://www.wearepossible.org/",
    "CPRE The Countryside Charity": "https://www.cpre.org.uk/",
    
    # Sustainability Consultancies & Engineering
    "AECOM": "https://aecom.com/",
    "AtkinsRéalis": "https://www.atkinsrealis.com/",
    "Jacobs": "https://www.jacobs.com/",
    "Ramboll": "https://www.ramboll.com/",
    "Cundall": "https://www.cundall.com/projects",
    "Hoare Lea": "https://hoarelea.com/",
    
    # Climate Think Tanks & Foundations
    "Green Alliance": "https://green-alliance.org.uk/",
    "Forum for the Future": "https://www.forumforthefuture.org/",
    "E3G": "https://www.e3g.org/about/",
    "Ashden": "https://ashden.org/about-us/"
}

# 3. Your core background context to feed to the AI
MY_CONTEXT = """
- Education: Physics student at Durham University, just finished final exams[cite: 15].
- Core Interest: start career e.g. in environment, sustainability, and public engagement roles.
- Name: James Stephens
"""

def scrape_projects(url):
    """Scrapes text content from a company's project page."""
    print(f"-> Scraping recent projects from: {url}")
    try:
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status() 
        
        soup = BeautifulSoup(response.text, 'html.parser')
        text_elements = soup.find_all(['h1', 'h2', 'h3', 'p'])
        scraped_text = "\n".join([el.get_text(strip=True) for el in text_elements if el.get_text(strip=True)])
        
        return scraped_text[:3000]
    except Exception as e:
        print(f"-> Error scraping {url}: {e}")
        return "No real-time project information could be retrieved."

def generate_outreach(company_name, scraped_context):
    # Notice the prompt now explicitly asks for JSON output
    prompt = f"""
    You are an assistant helping automate cold outreach for career opportunities.
    
    Target Company: {company_name}
    
    Recent Company Projects (Scraped from their website):
    {scraped_context}
    
    Candidate Background:
    {MY_CONTEXT}
    
    Task:
    1. Find an email address for the company (generic, department or a specific person and their contact details)
    2. Write a brief, personalized cold email asking for a conversation. Reference a specific project, campaign, or initiative this specific company is known to do, pulling from the "Recent Company Projects" context provided above. Keep the tone professional, enthusiastic, and direct.
    
    You must respond ONLY with a valid JSON object containing exactly these three keys:
    "to_email" : The suggested email address to send to.
    "subject" : The email subject line.
    "body" : The main body text of the email.

    Email Structure & Constraints:
    - Keep the tone professional, enthusiastic, and direct.
    - Be exceptionally concise: The email must be a maximum of 3 short paragraphs (under 120 words total). Get straight to the point.
    - DO NOT include placeholder brackets at the end like '[Your LinkedIn Profile]', '[Contact Information]', or '[Phone Number]'. 
    - Sign off cleanly using only the candidate's name provided in the context (e.g., 'Best regards,\nJames Stephens').
    """

    response = client.chat.completions.create(
        model="gpt-4o-mini",
        response_format={ "type": "json_object" }, # Forces the AI to output valid JSON
        messages=[{"role": "user", "content": prompt}],
        temperature=0.7
    )
    
    # Parse the JSON string returned by the AI into a Python dictionary
    return json.loads(response.choices[0].message.content)

# Ensure the drafts directory exists
DRAFTS_DIR = "drafts"
os.makedirs(DRAFTS_DIR, exist_ok=True)

# 4. Loop through each company, scrape context, and save the .eml file
for company, url in companies.items():
    print(f"GENERATING DRAFT FOR: {company}")
    time.sleep(1)
    
    try:
        scraped_text = scrape_projects(url)
        
        # draft_data is now a dictionary containing to_email, subject, and body
        draft_data = generate_outreach(company, scraped_text)
        
        # --- CREATE NATIVE .EML FILE ---
        msg = EmailMessage()
        msg['To'] = draft_data.get('to_email', 'contact@example.com')
        msg['From'] = "jstephens650@gmail.com"
        msg['Subject'] = draft_data.get('subject', 'Introduction')
        msg['X-Unsent'] = '1'  # <--- ADD THIS LINE
        msg.set_content(draft_data.get('body', ''), cte='8bit')
        
        # Save as .eml instead of .txt
        safe_filename = "".join([c if c.isalnum() else "_" for c in company]).strip("_") + ".eml"
        filepath = os.path.join(DRAFTS_DIR, safe_filename)
        
        with open(filepath, "wb") as f:
            f.write(bytes(msg))
            
        print(f"-> Draft saved to: {filepath}\n")
        
    except Exception as e:
        print(f"Error generating draft for {company}: {e}\n")

print("All tasks completed.")

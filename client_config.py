"""Candidate profiles, target roles, paths, settings and filter configuration.

Everything candidate-specific lives here (not in the scraper). To add a candidate see
`CLIENTS` / `CANDIDATE_ROLES` below. PERSONAL DATA BELOW IS PLACEHOLDER - REPLACE IT.
"""
import os
import re
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------- paths
BASE_DIR = Path(__file__).resolve().parent
CLIENTS_DIR = BASE_DIR / "clients"
SCRATCH_DIR = BASE_DIR / "scratch"
SESSION_FILE = BASE_DIR / "session.json"
RECRUITERS_CSV = BASE_DIR / "recruiters.csv"
HISTORY_CSV = BASE_DIR / "email_sent_history.csv"
ANALYTICS_HTML = BASE_DIR / "analytics_report.html"
STATS_JSON = BASE_DIR / "scrape_stats.json"  # scrape counters used by analytics
for _d in (CLIENTS_DIR, SCRATCH_DIR):
    _d.mkdir(exist_ok=True)

# ---------------------------------------------------------------- settings
EMAIL_DELAY_SECONDS = float(os.getenv("EMAIL_DELAY_SECONDS", "3"))
FOLLOWUP_DAYS = int(os.getenv("FOLLOWUP_DAYS", "7"))
IMAP_FALLBACK_DAYS = int(os.getenv("IMAP_FALLBACK_DAYS", "30"))
SCROLL_COUNT = 6
US_GEO_URN = "103644278"          # LinkedIn geoUrn for United States
REQUIRE_EXPLICIT_US = False       # True = reject posts with no explicit US indicator
TEST_MODE_MAX_EMAILS = 3          # test mode sends to yourself, capped
LEAD_EMAIL = os.getenv("LEAD_EMAIL", "").strip()    # CC
ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", "").strip()  # BCC

# ---------------------------------------------------------------- candidates
DEFAULT_EXCLUSION_KEYWORDS = ["hotlist", "fulltime", "available consultants", "bench"]

CLIENTS = {
    "safi": {
        "name": "Safi Ahmed",                       # PLACEHOLDER
        "email": "safi@example.com",
        "phone": "555-010-0001",
        "linkedin": "https://www.linkedin.com/in/safi-example",
        "location": "Dallas, TX",
        "relocation": "Yes",
        "work_auth": "H1B",
        "availability": "Immediate",
        "experience": "8+ Years",
        "resume_candidates": ["clients/safi_resume.docx"],
        "skills": ["Java", "Spring Boot", "Microservices", "Kafka", "PostgreSQL", "AWS", "Docker", "REST APIs"],
        "customizer": "safi_customizer",             # dedicated resume layout handler
    },
    "tanuja": {
        "name": "Tanuja Rao",                       # PLACEHOLDER
        "email": "tanuja@example.com",
        "phone": "555-010-0002",
        "linkedin": "https://www.linkedin.com/in/tanuja-example",
        "location": "Austin, TX",
        "relocation": "Yes",
        "work_auth": "GC-EAD",
        "availability": "2 weeks",
        "experience": "6+ Years",
        "resume_candidates": ["clients/tanuja_resume.pdf"],
        "skills": ["React", "TypeScript", "JavaScript", "Redux", "Node.js", "GraphQL", "AWS"],
        "customizer": "tanuja_customizer",
    },
}

CANDIDATE_ROLES = {
    "safi": {
        "primary_role": "Java Developer",
        "search_roles": ["Java Developer", "Backend Developer", "Software Engineer"],
        "exclusion_keywords": DEFAULT_EXCLUSION_KEYWORDS,
        # optional: "allowed_domains": ["java", "software"],
    },
    "tanuja": {
        "primary_role": "React Developer",
        "search_roles": ["React Developer", "Frontend Engineer", "UI Developer"],
        "exclusion_keywords": DEFAULT_EXCLUSION_KEYWORDS,
    },
}

# Keywords that only reject a post when it does NOT explicitly say C2C
SOFT_EXCLUSIONS = {"fulltime", "full time", "full-time", "fte"}


def list_clients():
    return list(CLIENTS.keys())


def get_client(key: str) -> dict:
    """Merged profile + role config for a candidate."""
    if key not in CLIENTS:
        raise KeyError(f"Unknown candidate '{key}'")
    merged = dict(CLIENTS[key])
    merged.update(CANDIDATE_ROLES.get(key, {}))
    merged["key"] = key
    merged.setdefault("exclusion_keywords", DEFAULT_EXCLUSION_KEYWORDS)
    merged.setdefault("search_roles", [merged.get("primary_role", "Software Engineer")])
    return merged


def existing_resumes(key: str):
    out = []
    for rel in get_client(key).get("resume_candidates", []):
        p = Path(rel)
        p = p if p.is_absolute() else BASE_DIR / p
        if p.exists():
            out.append(p)
    return out


def resolve_resume(key: str, role: str = ""):
    """Pick the best resume for a role: filename hint match first, else first existing file."""
    files = existing_resumes(key)
    if not files:
        return None
    words = [w for w in re.split(r"\W+", role.lower()) if len(w) > 2 and w not in ("developer", "engineer")]
    for f in files:
        if any(w in f.stem.lower() for w in words):
            return f
    return files[0]


def validate_client(key: str):
    """Return a list of problems (empty = OK)."""
    problems = []
    try:
        c = get_client(key)
    except KeyError as e:
        return [str(e)]
    for f in ("name", "email", "primary_role", "search_roles", "resume_candidates"):
        if not c.get(f):
            problems.append(f"missing '{f}'")
    if c.get("email") and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[A-Za-z]{2,}", c["email"]):
        problems.append("invalid email")
    if not existing_resumes(key):
        problems.append("no resume file found in resume_candidates")
    return problems


# ---------------------------------------------------------------- filter configuration
NON_USA_INDICATORS = [
    "Pune", "Noida", "India", "Canada", "Bangalore", "Bengaluru", "Hyderabad", "Chennai", "Mumbai",
    "Gurgaon", "Gurugram", "Delhi", "Kolkata", "Toronto", "Vancouver", "Mississauga", "Calgary",
    "United Kingdom", "UK", "Pakistan", "Philippines", "Dubai", "UAE", "Australia", "Germany",
    "Singapore", "Mexico", "Brazil",
]
US_STATE_ABBR = (
    "AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM "
    "NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY DC"
).split()
US_STATE_NAMES = [
    "Alabama", "Alaska", "Arizona", "Arkansas", "California", "Colorado", "Connecticut", "Delaware",
    "Florida", "Georgia", "Hawaii", "Idaho", "Illinois", "Indiana", "Iowa", "Kansas", "Kentucky",
    "Louisiana", "Maine", "Maryland", "Massachusetts", "Michigan", "Minnesota", "Mississippi",
    "Missouri", "Montana", "Nebraska", "Nevada", "New Hampshire", "New Jersey", "New Mexico",
    "New York", "North Carolina", "North Dakota", "Ohio", "Oklahoma", "Oregon", "Pennsylvania",
    "Rhode Island", "South Carolina", "South Dakota", "Tennessee", "Texas", "Utah", "Vermont",
    "Virginia", "Washington", "West Virginia", "Wisconsin", "Wyoming",
]
US_CITIES = [
    "Dallas", "Houston", "Austin", "San Antonio", "New York", "NYC", "Chicago", "Atlanta", "Seattle",
    "San Francisco", "San Jose", "Los Angeles", "Boston", "Denver", "Phoenix", "Charlotte", "Raleigh",
    "Plano", "Irving", "Frisco", "Jersey City", "Newark", "Philadelphia", "Miami", "Tampa", "Columbus",
    "Detroit", "Minneapolis", "Salt Lake City", "Portland", "San Diego", "Pittsburgh", "Nashville",
]
US_TERMS = ["United States", "USA", "U.S.A", "U.S.", "US only", "US based", "US-based", "USA only"]

BENCH_PHRASES = [
    "on my bench", "my bench", "candidate available", "candidates available", "marketing my consultant",
    "marketing my consultants", "hotlist", "hot list", "available consultant", "available consultants",
    "consultant available", "consultants available", "available for c2c", "open to c2c",
    "please find the profile", "profile of my consultant", "my consultant", "our consultant",
    "looking for c2c opportunities", "looking for c2c roles",
]

DOMAIN_KEYWORDS = {
    "qa": r"\b(qa|sdet|tester|quality assurance|test automation|manual testing|selenium)\b",
    "devops": r"\b(devops|sre|site reliability|release engineer|cloud engineer|platform engineer|infrastructure engineer)\b",
    "salesforce": r"\b(salesforce|sfdc|servicenow|workday|sap|peoplesoft|dynamics 365|oracle ebs)\b",
    "data": r"\b(data engineer|data scientist|data analyst|etl|informatica|databricks|machine learning|ml engineer|ai engineer|bi developer|power bi|tableau)\b",
    "java": r"\b(java|spring|spring boot|j2ee)\b",
    "dotnet": r"(\.net\b|\bdotnet\b|\bc#)",
    "python": r"\b(python|django|flask|fastapi)\b",
    "javascript": r"\b(react|reactjs|angular|vue|frontend|front-end|front end|ui developer|ui engineer|javascript|typescript|node|nodejs|node\.js|mern|mean)\b",
    "mobile": r"\b(ios|android|mobile|flutter|react native|swift|kotlin)\b",
    "security": r"\b(security|cyber|soc analyst|pentest|iam)\b",
    "network": r"\b(network engineer|network administrator|cisco|firewall)\b",
    "business_analyst": r"\b(business analyst|scrum master|project manager|product owner)\b",
    "software": r"\b(software (?:engineer|developer)|backend|back-end|full[- ]?stack|developer|engineer)\b",
}
GENERIC_DOMAINS = {"software"}
PROGRAMMING_DOMAINS = {"java", "dotnet", "python", "javascript"}

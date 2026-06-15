"""
Text normalization for keyword matching.

Goals:
  - Tokenize and stem so wash/washing/washes all match
  - Strip hyphens so "drive-thru" == "drive thru"
  - Handle Canadian spelling (centre/center)
  - Avoid substring traps (washington != washing)
"""

import re
import unicodedata

# Lightweight suffix rules (no external NLP library required)
_STEMMING_MAP = {
    "washing": "wash",
    "washes":  "wash",
    "cleaned": "clean",
    "cleaning": "clean",
    "cleans":  "clean",
    "washings": "wash",
    "degreasing": "degrease",
    "degreased": "degrease",
    "degreases": "degrease",
    "centres":  "center",
    "centre":   "center",
    "sanitizing": "sanitize",
    "sanitized":  "sanitize",
    "sanitizes":  "sanitize",
    "disinfecting": "disinfect",
    "disinfected":  "disinfect",
    "disinfects":   "disinfect",
    "services": "service",
    "agreements": "agreement",
    "contracts":  "contract",
    "facilities": "facility",
    "washers":    "washer",
    "structures": "structure",
}

# Canadian → standard English spelling
_CANADIAN = {
    "centre": "center",
    "colour": "color",
    "favour": "favor",
    "behaviour": "behavior",
    "neighbourhood": "neighborhood",
}


def normalize(text: str) -> str:
    """
    Return a normalized, lowercased, stemmed version of text suitable for
    token-level matching.

    The output is a space-joined token string — compare normalized keyword
    against normalized tender text using substring search on token boundaries.
    """
    if not text:
        return ""

    # Unicode normalize (NFD → ASCII where possible)
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()

    text = text.lower()

    # Replace hyphens and slashes with space so "drive-thru" → "drive thru"
    text = re.sub(r"[-/]", " ", text)

    # Strip remaining punctuation except apostrophes (keep "don't" intact)
    text = re.sub(r"[^\w\s']", " ", text)

    # Tokenize
    tokens = text.split()

    # Apply Canadian spelling and stemming
    normalized = []
    for tok in tokens:
        tok = _CANADIAN.get(tok, tok)
        tok = _STEMMING_MAP.get(tok, tok)
        normalized.append(tok)

    return " ".join(normalized)


def keyword_pattern(keyword: str) -> re.Pattern:
    """
    Build a word-boundary regex for a normalized keyword phrase so that
    'wash' does not match 'washington'.
    """
    norm = normalize(keyword)
    escaped = re.escape(norm)
    # Replace escaped spaces with a flexible whitespace matcher
    escaped = escaped.replace(r"\ ", r"\s+")
    return re.compile(r"\b" + escaped + r"\b", re.IGNORECASE)

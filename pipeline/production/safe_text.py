"""Family-friendly text for titles, thumbnails and captions (owner, 2026-10-23).

YouTube's advertiser-friendly guidelines limit ads on videos whose title or thumbnail
carries profanity or self-harm wording - and a model-written hook said "Made K'Sante Kill
HIMSELF". Gaming violence is fine ("pentakill", "deletes the enemy team"), so only
self-harm phrasing and swearing are masked. Pure functions; the prompts also ask for clean
text, this is the guarantee.
"""
import re

# stems: any word containing them ("fucking", "motherfucker", "bullshit")
CURSE_STEMS = ("fuck", "shit", "cunt", "bitch")
# whole words only (a stem like "ass" would hit "pass"/"class")
CURSE_WORDS = ("ass", "asshole", "assholes", "dick", "dicks", "pussy", "bastard",
               "bastards", "wtf", "stfu", "kys")
SELF_HARM = re.compile(
    r"\b(kill(?:s|ed|ing)?|end(?:s|ed|ing)?)(\s+(?:him|her|them|my|your|it)sel(?:f|ves))\b",
    re.I)
SELF_HARM_WORDS = re.compile(r"\b(suicid\w*)\b", re.I)


def _stars(word: str, style: str) -> str:
    """'****' (owner's pick for captions) or first letter + stars (F***)."""
    if style == "stars":
        return "****"
    return word[0] + "*" * max(len(word) - 1, 3)


def mask(text: str, extra: list | tuple = (), style: str = "initial") -> str:
    """Swearing, slurs (`extra`) and self-harm wording masked. Pure."""
    if not text:
        return text
    out = SELF_HARM.sub(lambda m: m.group(1)[0] + "*" + m.group(1)[2:] + m.group(2), text)
    out = SELF_HARM_WORDS.sub(lambda m: m.group(1)[0] + "*" * (len(m.group(1)) - 1), out)
    for stem in CURSE_STEMS:
        out = re.sub(rf"\b\w*{stem}\w*\b", lambda m: _stars(m.group(0), style), out, flags=re.I)
    for w in (*CURSE_WORDS, *(extra or ())):
        out = re.sub(rf"\b{re.escape(w)}(?:s|es)?\b", lambda m: _stars(m.group(0), style), out,
                     flags=re.I)
    return out


PROMPT_RULE = ("- Family friendly (YouTube ad guidelines): no swearing, no slurs, no "
               "self-harm or suicide wording (never \"kill himself\" - say \"outplayed "
               "himself\" or \"threw\"). Game violence is fine: pentakill, deletes, destroys.")

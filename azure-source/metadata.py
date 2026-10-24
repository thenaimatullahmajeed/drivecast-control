"""Deterministic metadata preparation. No AI calls or invented video facts."""
import re
from pathlib import PurePath


def text_sidecar(text):
    lines = text.strip().splitlines()
    if not lines:
        raise ValueError("TXT sidecar is empty. Put the video title on its first line.")
    title = re.sub(r"(?i)^title\s*:\s*", "", lines[0]).strip()
    rest = "\n".join(lines[1:]).strip()
    rest = re.sub(r"(?i)^description\s*:\s*", "", rest)
    return {"title": title, "description": rest}


def prepare(title, description="", tags=None, category="22", privacy="private", basic_seo=True):
    if not isinstance(title, str) or not title.strip():
        raise ValueError("Video title missing. Use a TXT/JSON sidecar with the same base filename as the Drive video.")
    if not isinstance(description, str):
        raise ValueError("Description must be text.")
    if tags is not None and (not isinstance(tags, list) or not all(isinstance(t, str) for t in tags)):
        raise ValueError("Tags must be a list of text values.")
    if privacy not in ("private", "unlisted", "public"):
        raise ValueError("Invalid YouTube privacy.")
    title = re.sub(r"\s+", " ", title.replace("<", "").replace(">", "")).strip()[:100]
    if not title:
        raise ValueError("Title has no usable characters.")
    description = description.replace("\x00", "").replace("<", "").replace(">", "").strip()
    tags = list(tags or [])
    if basic_seo:
        if not description:
            description = title
        tags += re.findall(r"#([^\W_][\w]*)", title + " " + description, flags=re.UNICODE)
        stop = {"the", "and", "for", "with", "this", "that", "from", "your", "are", "you", "video", "shorts"}
        tags += [w.lower() for w in re.findall(r"[^\W_][\w'-]{2,}", title, flags=re.UNICODE) if w.lower() not in stop][:8]
    clean, used, seen = [], 0, set()
    for tag in tags:
        tag = re.sub(r"\s+", " ", tag.replace("<", "").replace(">", "")).strip().lstrip("#")[:60]
        cost = len(tag) + (2 if " " in tag else 0) + (1 if clean else 0)
        if tag and tag.casefold() not in seen and used + cost <= 450:
            clean.append(tag)
            seen.add(tag.casefold())
            used += cost
    snippet = {"title": title, "description": description[:5000], "categoryId": str(category)}
    if clean:
        snippet["tags"] = clean
    return {"snippet": snippet, "status": {"privacyStatus": privacy, "selfDeclaredMadeForKids": False}}



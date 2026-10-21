"""The About page behind each profile: file uploads digested into facts (the facts themselves live in
facts.py / notes/about.md).

    profiles/<name>/notes/about.md          one "- [w]" bullet per fact; the ranking and the LLM hooks use it
    profiles/<name>/notes/uploads/<file>.md full extracted text of each uploaded file (reference only)
"""
from __future__ import annotations

import base64
import io
import re
import zipfile
from pathlib import Path
from typing import Optional
from xml.etree import ElementTree

from .. import llm

ABOUT_FILE = "about.md"
UPLOAD_DIR = "uploads"
MAX_UPLOAD_BYTES = 8 * 1024 * 1024

_FACT_HINT_RE = re.compile(
    r"\b(19|20)\d{2}\b|\b(b\.?s\.?|m\.?s\.?|m\.?a\.?|ph\.?d|bachelor|master|degree|university|college)\b|"
    r"\b(engineer|scientist|analyst|statistician|manager|lead|senior|director|founder|intern)\b|"
    r"\b(python|sql|r|sas|java|c\+\+|pytorch|tensorflow|aws|kubernetes|spark|tableau|excel|stata)\b|"
    r"\b(led|built|designed|delivered|managed|developed|launched|published|awarded|commend)\w*\b", re.I)
_NOISE_RE = re.compile(r"^(page \d+|references?|contact|phone|email|linkedin|\W*)$", re.I)


# ----------------------------------------------------------------------------- facts (see facts.py)

from ..classify import facts as factsmod  # noqa: E402

about_path = factsmod.about_path


def read_bullets(profile_dir: Path) -> list[str]:
    return [f.text for f in factsmod.read_facts(profile_dir)]


def _bullets_from_markdown(text: str) -> list[str]:
    return [f.text for f in factsmod.parse_facts(text)]


# ----------------------------------------------------------------------------- uploads

def extract_text(name: str, data: bytes) -> str:
    """Plain text from a PDF, DOCX, TXT or MD upload."""
    ext = Path(name).suffix.lower()
    if ext == ".pdf":
        try:
            from pypdf import PdfReader
        except ImportError as e:  # pragma: no cover
            raise RuntimeError("pypdf is not installed; run `uv sync`") from e
        reader = PdfReader(io.BytesIO(data))
        return "\n".join((page.extract_text() or "") for page in reader.pages)
    if ext == ".docx":
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            xml = z.read("word/document.xml")
        root = ElementTree.fromstring(xml)
        ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
        paras = []
        for para in root.iter("{%s}p" % ns["w"]):
            texts = [t.text or "" for t in para.iter("{%s}t" % ns["w"])]
            if texts:
                paras.append("".join(texts))
        return "\n".join(paras)
    if ext in (".txt", ".md", ".markdown", ".text", ""):
        return data.decode("utf-8", "replace")
    raise ValueError(f"unsupported file type {ext!r} (use PDF, DOCX, TXT or MD)")


def heuristic_digest(text: str, max_items: int = 15) -> list[str]:
    """Fact-like lines: dates, degrees, titles, tools, achievement verbs. No model needed."""
    out: list[str] = []
    for raw in re.split(r"[\n\r]+|(?<=[.!?])\s{2,}", text):
        s = re.sub(r"\s+", " ", raw).strip(" -•*·\t")
        if len(s) < 12 or len(s) > 220 or _NOISE_RE.match(s):
            continue
        if _FACT_HINT_RE.search(s) and s not in out:
            out.append(s)
        if len(out) >= max_items:
            break
    return out


def llm_digest(llm_cfg: dict, text: str, source_name: str, first_name: str = "") -> Optional[list[str]]:
    """8-15 first-person facts from the text, or None when the LLM is off / fails."""
    if (llm_cfg.get("backend") or "none").lower() == "none":
        return None
    who = first_name or "the applicant"
    prompt = (
        f"Below is the text of a file ({source_name}) that {who} uploaded to describe themselves for a "
        "job search. Extract 8 to 15 facts that would help answer application questions: roles with employer "
        "and years, education, notable projects with numbers, tools and skills, awards, location and work "
        "preferences. Write each fact as one complete sentence in the FIRST person, as if the applicant wrote "
        "it (\"I ...\"), one per line, each line starting with '- ', no headings, no commentary, and no facts "
        "that are not in the text.\n\n"
        f"{text[:12000]}"
    )
    out = llm._call(llm_cfg, prompt, max_tokens=700)
    if not out:
        return None
    bullets = _bullets_from_markdown(out)
    return bullets or None


def ingest_upload(profile_dir: Path, name: str, content_b64: str, llm_cfg: dict, first_name: str = "") -> dict:
    """Save the extracted text under notes/uploads/, digest it, append the new facts to about.md."""
    data = base64.b64decode(content_b64 or "")
    if not data:
        raise ValueError("empty upload")
    if len(data) > MAX_UPLOAD_BYTES:
        raise ValueError("file is larger than 8MB")
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(name).name) or "upload"
    text = extract_text(safe, data).strip()
    if not text:
        raise ValueError("no text could be extracted from the file")
    up = Path(profile_dir) / "notes" / UPLOAD_DIR
    up.mkdir(parents=True, exist_ok=True)
    saved = up / (Path(safe).stem + ".md")
    saved.write_text(f"# Uploaded: {safe}\n\n{text}\n", encoding="utf-8")
    used_llm = False
    bullets = llm_digest(llm_cfg, text, safe, first_name)
    if bullets:
        used_llm = True
    else:
        bullets = heuristic_digest(text)
    existing = factsmod.read_facts(profile_dir)
    have = {f.text for f in existing}
    added = [f"{b} (from {safe})" for b in bullets if b not in have and f"{b} (from {safe})" not in have]
    factsmod.write_facts(profile_dir, existing + [factsmod.Fact(a, False) for a in added])
    return {"saved_as": str(saved), "chars": len(text), "used_llm": used_llm, "added": added}


def list_uploads(profile_dir: Path) -> list[str]:
    up = Path(profile_dir) / "notes" / UPLOAD_DIR
    return sorted(p.name for p in up.glob("*.md")) if up.is_dir() else []

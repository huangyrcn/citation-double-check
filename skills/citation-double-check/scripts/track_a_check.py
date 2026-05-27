#!/usr/bin/env python3
"""Track A — Entry-health checks (deterministic, no LLM, no network).

Reads the three artefacts from earlier stages and emits track_a_findings.json:

  bib_entries.json       (Stage 0a)
  citations.json         (Stage 0a)
  evidence_pack.json     (Stage 0b — multi-source data + native BibTeX)

Codes per SKILL.md (adapted for multi-source schema):
  A1_undefined_key       critical   \\cite{X} but X missing from bib
  A2_unused_entry        cleanup    bib has entry never cited
  A3_duplicate_entry     warning    two keys, title-sim ≥ 0.95, same year
  A4_unresolved          critical*  all 7 sources missed       (* downgraded to
                                    warning when bib venue looks non-traditional:
                                    blog, GitHub, tech report, dataset, etc.)
  A5_metadata_mismatch   warning    title/year/first-author mismatch.
                                    A5 compares bib entry vs external sources in
                                    priority order. Native BibTeX sources (DBLP >
                                    Crossref > arXiv > DataCite) take precedence
                                    over raw JSON metadata. S2/OpenAlex are used
                                    only as last-resort fallback.
  A6_field_missing       cleanup    required fields absent for the entry type
  A7_venue_mismatch      cleanup    bib venue ≠ DBLP BibTeX booktitle (genuinely
                                    different venues, not just abbreviation vs full).
  A8_venue_style         cleanup    bib venue and DBLP are equivalent but written
                                    differently (e.g. abbreviation vs full name);
                                    suggests format unification.
                                    Only fired when DBLP BibTeX is available
                                    (DBLP's booktitle is the authoritative venue
                                    for CS conferences; DBLP JSON alone is NOT
                                    sufficient because it often gives "CoRR" for
                                    arXiv-only papers).

Multisource priority ladder (A5 / A7):
  BibTeX tier 1 (native, authoritative):
    dblp.bib  >  crossref.bib  >  arxiv.bib  >  datacite.bib
    ↑ parsed via bibtexparser, used for title/year/author/venue checks

  BibTeX tier 2 (rendered, lower confidence):
    gscholar.bib
    ↑ only if tier-1 sources are all absent; flagged confidence: "low"

  Metadata tier (JSON/Atom, used only as last-resort fallback):
    dblp.data > crossref.data > semantic_scholar.data > openalex.data
    ↑ flagged confidence: "low"; S2/OpenAlex year is NOT used in year checks
      (it's often the arXiv-upload year, off by 1 from the published year)

Usage:
  python track_a_check.py <paper_dir> [--out DIR]
"""

from __future__ import annotations

import argparse
import json
import re
import unicodedata
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

try:
    import bibtexparser
    from bibtexparser.bparser import BibTexParser
    HAS_BIBPARSER = True
except Exception:
    HAS_BIBPARSER = False


# ---------------------------------------------------------------------------
# Constants

A3_TITLE_THRESHOLD = 0.95
A5_TITLE_THRESHOLD = 0.85

# BibTeX type → required / recommended fields.
# "/" means any alternative is OK (e.g. "author/editor" → either suffices).
REQUIRED_FIELDS: dict[str, list[str]] = {
    "article":       ["author", "title", "journal", "year"],
    "inproceedings": ["author", "title", "booktitle", "year"],
    "conference":    ["author", "title", "booktitle", "year"],
    "incollection":  ["author", "title", "booktitle", "year"],
    "book":          ["author/editor", "title", "publisher", "year"],
    "inbook":        ["author/editor", "title", "publisher", "year"],
    "phdthesis":     ["author", "title", "school", "year"],
    "mastersthesis": ["author", "title", "school", "year"],
    "techreport":    ["author", "title", "institution", "year"],
    "manual":        ["title"],
    "misc":          ["author", "title", "year"],
    "online":        ["author", "title", "year"],
    "unpublished":   ["author", "title", "note"],
    "proceedings":   ["title", "year"],
}

RECOMMENDED_FIELDS: dict[str, list[str]] = {
    "article":       ["volume", "number", "pages"],
    "inproceedings": ["pages"],
    "conference":    ["pages"],
    "incollection":  ["publisher", "pages"],
    "misc":          ["url/eprint/howpublished"],
    "online":        ["url"],
}

NON_TRADITIONAL_VENUE_PATTERNS = [
    r"\bblog\b", r"\bgithub\b", r"\bhugging[\s\-]?face\b", r"\bhuggingface\b",
    r"\btech(nical)?\s*report\b", r"\bwhite\s*paper\b", r"\bonline\b",
    r"\bdataset\b", r"\bdatasets?\b", r"\bcorpus\b", r"\bsoftware\b",
    r"\brepo(sitory)?\b", r"\bsystem\s*card\b", r"\bmodel\s*card\b",
    r"\b(?:open\s*ai|openai)\b.*\b(card|blog|report)\b",
]

# BibTeX tier-1 sources — used for A5 (title/year/author) and A7 (venue)
T1_BIB_SOURCES = ("dblp", "crossref", "arxiv", "datacite")
# BibTeX tier-2 sources (rendered, lower confidence)
T2_BIB_SOURCES = ("gscholar",)
# JSON tier — used as last-resort fallback for A5 title only
JSON_TIER = ("dblp", "crossref", "semantic_scholar", "openalex")

# DataCite types that should NOT participate in author/year/venue comparisons
SKIP_DATACITE_TYPES = {"audiovisual", "image", "software", "dataset"}


# ---------------------------------------------------------------------------
# Utilities

_PUNCT_RE = re.compile(r"[^\w\s]")
_WS_RE = re.compile(r"\s+")


def norm_title(s: str) -> str:
    s = (s or "").lower()
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = _PUNCT_RE.sub(" ", s)
    return _WS_RE.sub(" ", s).strip()


def title_similarity(a: str, b: str) -> float:
    na, nb = norm_title(a), norm_title(b)
    if not na or not nb:
        return 0.0
    seq = SequenceMatcher(None, na, nb).ratio()
    ta, tb = set(na.split()), set(nb.split())
    jacc = len(ta & tb) / max(1, len(ta | tb))
    return 0.5 * (seq + jacc)


def norm_token(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r"[^\w]", "", s).lower()
    return s


def venue_norm(s: str) -> str:
    """Normalize venue strings to a canonical key for equivalence comparison.

    Maps well-known abbreviations and full names to the same key so that
    e.g. "TMLR" and "Trans. Mach. Learn. Res." compare equal.
    Unrecognised strings are lowercased with periods/whitespace normalised.
    """
    if not s:
        return ""
    s = " ".join(s.split()).strip()
    s_norm = re.sub(r"\s+", " ", s.lower().replace(".", " ").strip())
    aliases = {
        # Conferences
        "international conference on learning representations": "iclr",
        "international conference on machine learning": "icml",
        "advances in neural information processing systems": "neurips",
        "neural information processing systems": "neurips",
        "conference on empirical methods in natural language processing": "emnlp",
        "association for computational linguistics": "acl",
        "annual meeting of the association for computational linguistics": "acl",
        "computer vision and pattern recognition": "cvpr",
        "proceedings of the aaai conference on artificial intelligence": "aaai",
        # Journals — abbreviation ↔ full name
        "trans mach learn res": "tmlr",
        "transactions on machine learning research": "tmlr",
        "ieee trans pattern anal mach intell": "ieeetpami",
        "ieee transactions on pattern analysis and machine intelligence": "ieeetpami",
        "ieee tpami": "ieeetpami",
        "nat mac intell": "natmi",
        "nat mach intell": "natmi",
        "nature machine intelligence": "natmi",
        # arXiv / CoRR
        "corr": "arxiv",
        "corr abs": "arxiv",
        "arxiv": "arxiv",
        "arxiv preprint": "arxiv",
    }
    if s_norm in aliases:
        return aliases[s_norm]
    return s_norm


def venue_lax_match(bib_venue: str, dblp_venue: str) -> bool:
    """Check if bib_venue and dblp_venue refer to the same venue.

    Strategies:
    1. Exact venue_norm match (handles known aliases/abbreviations).
    2. Bib venue acronym appears as a token in the DBLP string.
    3. Normalized substring containment.
    """
    vb = venue_norm(bib_venue)
    vd = venue_norm(dblp_venue)
    if vb == vd:
        return True
    if len(vb) >= 3 and vb in vd:
        return True
    if len(vd) >= 3 and vd in vb:
        return True
    # Case-insensitive substring of originals (handles acronyms like "ACL")
    b_low = bib_venue.strip().lower()
    d_low = dblp_venue.strip().lower()
    if len(b_low) >= 3 and b_low in d_low:
        return True
    if len(d_low) >= 3 and d_low in b_low:
        return True
    return False


def venue_style_differs(bib_venue: str, dblp_venue: str) -> bool:
    """True when both venues are equivalent but written differently.

    This means venue_lax_match returns True, but the raw strings are not
    trivially identical (after case/space normalization). Used to flag
    A8_venue_style — a cleanup suggestion to unify the format.
    """
    if not venue_lax_match(bib_venue, dblp_venue):
        return False
    b = " ".join(bib_venue.lower().split())
    d = " ".join(dblp_venue.lower().split())
    return b != d


def is_arxiv_venue(s: str) -> bool:
    text = (s or "").lower()
    return venue_norm(text) == "arxiv" or bool(re.search(r"(^|[^a-z0-9])(arxiv|corr)([^a-z0-9]|$)", text))


def is_pure_arxiv_venue(s: str) -> bool:
    text = (s or "").lower()
    if re.fullmatch(r"\s*(?:\\url\{?)?https?://(?:www\.)?arxiv\.org/(?:abs|pdf)/\d{4}\.\d{4,5}(?:v\d+)?(?:\.pdf)?\}?\s*", text):
        return True
    if re.fullmatch(r"\s*(?:\\url\{?)?https?://(?:www\.)?arxiv\.org/(?:abs|pdf)/[a-z\-]+/\d{7}(?:v\d+)?(?:\.pdf)?\}?\s*", text):
        return True
    if not is_arxiv_venue(text):
        return False
    cleaned = re.sub(r"\be\s*-?\s*prints?\b", " ", text)
    cleaned = re.sub(r"\b(?:arxiv|corr|abs|preprint)\b", " ", cleaned)
    cleaned = re.sub(r"\b\d{4}\.\d{4,5}(?:v\d+)?\b", " ", cleaned)
    cleaned = re.sub(r"\b[a-z\-]+/\d{7}(?:v\d+)?\b", " ", cleaned)
    cleaned = re.sub(r"[\W_]+", " ", cleaned)
    return not cleaned.strip()


def parse_year(s: Any) -> int | None:
    if s is None:
        return None
    if isinstance(s, int):
        return s
    m = re.search(r"\b(19|20)\d{2}\b", str(s))
    return int(m.group(0)) if m else None


def first_author_surname(authors: list[str] | None) -> str:
    if not authors:
        return ""
    a = (authors[0] or "").strip()
    if not a:
        return ""
    # "LastName, FirstName" format
    if "," in a:
        parts = a.split(",")
        surname = parts[0].strip()
    else:
        # "FirstName LastName" format → last token is surname
        parts = a.split()
        surname = parts[-1] if parts else ""
    # Clean DBLP disambiguation suffix: "LastName 0001" → "LastName"
    surname = re.sub(r"\s+\d{4}$", "", surname)
    return norm_token(surname)


def surnames_match(a: str, b: str) -> bool:
    """Lenient surname comparison. Handles 'de Lange' vs 'Lange' and
    other name-particle prefix variants by substring containment
    (only when the shorter name is ≥ 4 chars to avoid accidental matches)."""
    if a == b:
        return True
    if len(a) >= 4 and len(b) >= 4 and (a in b or b in a):
        return True
    return False


_PUBLISHER_RE = re.compile(r"\b(ieee|acm|springer|elsevier|wiley|mit press|oxford)\b", re.I)


def is_non_traditional(entry: dict) -> bool:
    formal_venue = " ".join(str(entry.get(k, "")) for k in ("booktitle", "journal"))
    if formal_venue.strip():
        return False

    source_text = " ".join(str(entry.get(k, "")) for k in ("venue", "howpublished"))
    source_text = source_text.lower()
    if not _PUBLISHER_RE.search(source_text):
        for pat in NON_TRADITIONAL_VENUE_PATTERNS:
            if re.search(pat, source_text):
                return True

    note = str(entry.get("note", "")).lower()
    for pat in NON_TRADITIONAL_VENUE_PATTERNS:
        if pat in (r"\bonline\b", r"\bsoftware\b"):
            continue
        if re.search(pat, note):
            return True

    url = str(entry.get("url", "")).lower()
    return bool(re.search(r"\b(github|huggingface|dataset|datasets|software|repo)\b", url))


def required_present(entry: dict, fields: list[str]) -> list[str]:
    missing = []
    for f in fields:
        if "/" in f:
            alts = f.split("/")
            if not any(entry.get(a) for a in alts):
                missing.append(f)
        else:
            if not entry.get(f):
                missing.append(f)
    return missing


# ---------------------------------------------------------------------------
# BibTeX field extraction

def parse_bibtex_string(text: str) -> dict | None:
    """Parse a single native BibTeX string into {title, authors[], year, venue, doi}.
    venue is taken from booktitle > journal > howpublished > publisher.
    Returns None on parse failure.
    """
    if not text or not HAS_BIBPARSER:
        # fallback: regex-based rough extraction from .bib string
        return _parse_bibtex_fallback(text)
    try:
        parser = BibTexParser(common_strings=True)
        parser.ignore_nonstandard_types = False
        bib = bibtexparser.loads(text, parser=parser)
        if not bib.entries:
            return None
        e = bib.entries[0]
    except Exception:
        return _parse_bibtex_fallback(text)

    authors = [a.strip() for a in re.split(r"\s+and\s+",
              (e.get("author") or ""), flags=re.IGNORECASE) if a.strip()]
    venue = (e.get("booktitle") or e.get("journal")
             or e.get("howpublished") or e.get("publisher") or "")
    year = e.get("year") or e.get("pub_year") or None
    if year and str(year).isdigit():
        year = int(year)
    else:
        year = None
    return {
        "title": e.get("title", ""),
        "authors": authors,
        "year": year,
        "venue": re.sub(r"\s+", " ", venue.replace("{","").replace("}","")).strip(),
        "doi": e.get("doi") or e.get("DOI") or "",
    }


_BRACE_RE = re.compile(r"\{|\}")

def _parse_bibtex_fallback(text: str) -> dict | None:
    """Regex-based BibTeX extraction for when bibtexparser is unavailable."""
    if not text:
        return None
    t = re.search(r'title\s*=\s*[\{"]([^}"]+)', text, re.I)
    a = re.findall(r'author\s*=\s*[\{"]?(.+?)[}"]?\s*[,}]', text, flags=re.DOTALL|re.I)
    y = re.search(r'(?:pub_)?year\s*=\s*[\{"]?(\d{4})\b', text, re.I)
    bt = re.search(r'booktitle\s*=\s*[\{"]?([^}"]+)', text, re.I)
    jn = re.search(r'journal\s*=\s*[\{"]?([^}"]+)', text, re.I)
    hp = re.search(r'howpublished\s*=\s*[\{"]?([^}"]+)', text, re.I)
    pb = re.search(r'publisher\s*=\s*[\{"]?([^}"]+)', text, re.I)
    di = re.search(r'DOI\s*=\s*[\{"]?(\S+)', text, re.I) or re.search(r'doi\s*=\s*[\{"]?(\S+)', text, re.I)

    authors = []
    if a:
        authors_raw = re.split(r'\s+and\s+', _BRACE_RE.sub("", a[0]), flags=re.I)
        authors = [x.strip() for x in authors_raw if x.strip()]
    return {
        "title": (_BRACE_RE.sub("", t.group(1)).strip() if t else ""),
        "authors": authors,
        "year": int(y.group(1)) if y else None,
        "venue": _BRACE_RE.sub("", (bt or jn or hp or pb or "").group(1)).strip() if (bt or jn or hp or pb) else "",
        "doi": (di.group(1) if di else ""),
    }


def strip_bibtex_braces(s: str) -> str:
    return s.replace("{", "").replace("}", "").strip() if s else s


# ---------------------------------------------------------------------------
# Source-priority extraction utilities

def _pick_from_bibtex(bibtex: dict[str, str], field: str, *,
                      skip_datacite_av: bool = False,
                      skip_gscholar: bool = False,
                      skip_arxiv: bool = False) -> tuple:
    """Walk T1_BIB_SOURCES → T2_BIB_SOURCES, return (value, source_name)."""
    sources = list(T1_BIB_SOURCES)
    if skip_arxiv:
        sources = [s for s in sources if s != "arxiv"]
    if not skip_gscholar:
        sources = sources + list(T2_BIB_SOURCES)

    # Collect all values from BibTeX sources
    values = []
    for src in sources:
        b = parse_bibtex_string(bibtex.get(src, ""))
        if not b:
            continue
        if skip_datacite_av and src == "datacite":
            continue
        v = b.get(field)
        if field == "venue":
            v = strip_bibtex_braces(v or "")
        if v:
            values.append((v, src))

    if not values:
        return None, None

    # For non-year fields, return first match (priority order)
    if field != "year":
        return values[0]

    return values[0]


def _has_published_venue(bibtex: dict[str, str], data: dict[str, dict]) -> bool:
    """True if any authoritative source indicates a formal publication venue.

    DBLP/Crossref BibTeX with a non-arXiv venue, or DBLP/Crossref JSON data
    with a venue that isn't just "arXiv"/"CoRR", means the paper has been
    published. In that case arXiv BibTeX year should not be trusted.
    """
    # Check BibTeX sources for a published venue
    for src in ("dblp", "crossref"):
        bt = bibtex.get(src, "")
        if not bt:
            continue
        parsed = parse_bibtex_string(bt)
        if not parsed:
            continue
        venue = (parsed.get("venue") or "").strip()
        if venue and venue_norm(venue) != "arxiv":
            return True
    # Check JSON data for a published venue
    for src in ("dblp", "crossref"):
        d = data.get(src)
        if not d:
            continue
        venue = (d.get("venue") or d.get("container_title") or "").strip()
        if venue and venue_norm(venue) != "arxiv":
            return True
    return False


def _pick_from_data(data: dict[str, dict], field: str) -> tuple:
    """Walk JSON_TIER, return (value, source_name)."""
    for src in JSON_TIER:
        d = data.get(src)
        if not d:
            continue
        v = d.get(field)
        if v:
            return v, src
    return None, None


def _pick_best_authors(bibtex: dict[str, str]) -> tuple:
    """Return (author_list, source_name) from best BibTeX source.

    Priority: arxiv > dblp > crossref > datacite > gscholar.
    Crossref is demoted because it alphabetizes authors by last name for
    many publisher-indexed papers, so the first author in Crossref BibTeX
    is often not the paper's actual first author.
    """
    # arXiv first — preserves original author order
    for src in ("arxiv", "dblp", "crossref"):
        b = parse_bibtex_string(bibtex.get(src, ""))
        if b and b.get("authors"):
            return b["authors"], src
    # datacite last in T1 (often org names as authors)
    b = parse_bibtex_string(bibtex.get("datacite", ""))
    if b and b.get("authors"):
        return b["authors"], "datacite"
    # T2 fallback
    for src in T2_BIB_SOURCES:
        b = parse_bibtex_string(bibtex.get(src, ""))
        if b and b.get("authors"):
            return b["authors"], f"gscholar (rendered)"
    return None, None


# ---------------------------------------------------------------------------
# Findings factory

def make_finding(code: str, severity: str, key: str | None,
                 message: str, **details) -> dict:
    f = {"code": code, "severity": severity, "key": key, "message": message}
    if details:
        f["details"] = details
    return f


# ---------------------------------------------------------------------------
# Track A checks

def check_a1(citations: list[dict], bib: dict) -> list[dict]:
    findings: list[dict] = []
    sites_by_key: dict[str, list[dict]] = {}
    for c in citations:
        k = c["key"]
        if k in bib:
            continue
        sites_by_key.setdefault(k, []).append(
            {"file": c.get("file"), "line": c.get("line"),
             "sentence": c.get("sentence", "")[:200]})
    for k, sites in sorted(sites_by_key.items()):
        findings.append(make_finding(
            "A1_undefined_key", "critical", k,
            f"引用键 `{k}` 在 bib 中未定义",
            sites=sites, n_sites=len(sites),
            suggested_fix="补全对应 bib 条目，或检查 \\cite 拼写"))
    return findings


def check_a2(citations: list[dict], bib: dict) -> list[dict]:
    findings: list[dict] = []
    cited = {c["key"] for c in citations}
    for k in sorted(bib.keys() - cited):
        findings.append(make_finding(
            "A2_unused_entry", "cleanup", k,
            f"bib 条目 `{k}` 从未在正文 \\cite 中使用",
            title=bib[k].get("title", ""),
            suggested_fix="确认是否需要保留，否则可删除"))
    return findings


def check_a3(bib: dict) -> list[dict]:
    findings: list[dict] = []
    keys = list(bib.keys())
    seen: set[tuple[str, str]] = set()
    for i in range(len(keys)):
        for j in range(i + 1, len(keys)):
            k1, k2 = keys[i], keys[j]
            e1, e2 = bib[k1], bib[k2]
            t1, t2 = e1.get("title", ""), e2.get("title", "")
            if not t1 or not t2:
                continue
            sim = title_similarity(t1, t2)
            if sim < A3_TITLE_THRESHOLD:
                continue
            y1, y2 = parse_year(e1.get("year")), parse_year(e2.get("year"))
            if y1 is None or y2 is None or y1 != y2:
                continue
            pair = tuple(sorted([k1, k2]))
            if pair in seen:
                continue
            seen.add(pair)
            findings.append(make_finding(
                "A3_duplicate_entry", "warning", None,
                f"bib 中存在疑似重复条目: `{k1}` 与 `{k2}`",
                keys=list(pair), titles=[t1, t2], year=y1,
                similarity=round(sim, 3),
                suggested_fix="保留一条，删除另一条；统一所有 \\cite 引用键"))
    return findings


def check_a4(cited_keys: set[str], bib: dict, evidence: dict) -> list[dict]:
    findings: list[dict] = []
    for k in sorted(cited_keys):
        ev = evidence.get(k)
        if ev and ev.get("resolved"):
            continue
        entry = bib.get(k, {})
        non_trad = is_non_traditional(entry)
        sev = "warning" if non_trad else "critical"
        msg = (
            f"`{k}` 在全部外部源中均未命中"
            + ("；bib venue 看起来是非传统来源（博客 / GitHub / 系统卡 / 数据集等），已降级为 warning"
               if non_trad else "")
        )
        findings.append(make_finding(
            "A4_unresolved", sev, k, msg,
            bib_title=entry.get("title", ""),
            bib_venue=entry.get("venue") or entry.get("booktitle") or entry.get("journal", ""),
            bib_year=entry.get("year", ""),
            non_traditional_venue=non_trad,
            suggested_fix=(
                "确认 venue / howpublished 字段写明了来源（如 GitHub URL / 系统卡链接）"
                if non_trad else
                "核对 title 是否拼写正确；若引用对象本身不存在，替换为真实参考文献")
        ))
    return findings


def check_a5(cited_keys: set[str], bib: dict, evidence: dict) -> list[dict]:
    """Bib metadata vs external sources, with multisource priority ladder."""
    findings: list[dict] = []
    for k in sorted(cited_keys):
        ev = evidence.get(k)
        if not ev or not ev.get("resolved"):
            continue
        entry = bib.get(k, {})
        bibtex = ev.get("bibtex") or {}
        data = ev.get("data") or {}

        problems = []
        details: dict[str, Any] = {}

        # ---- Title ----
        bib_title = entry.get("title", "")
        ext_title, title_src = _pick_from_bibtex(bibtex, "title")
        title_confidence = "high"
        if not ext_title:
            ext_title, title_src = _pick_from_data(data, "title")
            title_confidence = "low"
        if bib_title and ext_title:
            sim = title_similarity(bib_title, ext_title)
            details["title_similarity"] = round(sim, 3)
            details["title_source"] = (title_src or "none")
            details["title_confidence"] = title_confidence
            if sim < A5_TITLE_THRESHOLD:
                problems.append(
                    f"title 相似度 {sim:.2f} < {A5_TITLE_THRESHOLD} "
                    f"(外部源: {title_src}, 置信度: {title_confidence})")

        # ---- Year ----
        # Only use BibTeX year from DBLP/Crossref/DataCite.
        # Ignore arXiv BibTeX year (often returns latest revision, not original).
        # Priority: DBLP > Crossref > DataCite.
        ext_year, year_src = _pick_from_bibtex(bibtex, "year",
                                               skip_gscholar=True,
                                               skip_arxiv=True)
        year_confidence = "high" if ext_year else "low"

        bib_year = parse_year(entry.get("year"))
        if bib_year is not None and ext_year is not None:
            details["bib_year"] = bib_year
            details["ext_year"] = ext_year
            details["year_source"] = (year_src or "none")
            details["year_confidence"] = year_confidence
            if abs(bib_year - ext_year) >= 1:
                problems.append(
                    f"年份不一致：bib={bib_year} vs {year_src}={ext_year}")

        # ---- First-author surname (BibTeX only; skip DataCite) ----
        bib_surname = ""
        bib_authors = entry.get("authors") or []
        if bib_authors:
            bib_surname = first_author_surname(bib_authors)
        ext_authors, auth_src = _pick_best_authors(bibtex)
        ext_surname = first_author_surname(ext_authors) if ext_authors else ""
        if bib_surname and ext_surname and not surnames_match(bib_surname, ext_surname):
            details["bib_first_author"] = bib_surname
            details["ext_first_author"] = ext_surname
            details["author_source"] = (auth_src or "none")
            details["ext_authors_all"] = ext_authors or []
            problems.append(
                f"第一作者姓不一致：bib=`{bib_surname}` vs {auth_src}=`{ext_surname}`")

        if not problems:
            continue
        findings.append(make_finding(
            "A5_metadata_mismatch", "warning", k,
            f"`{k}` 的元数据与外部源不一致：" + "；".join(problems),
            bib_title=bib_title,
            suggested_fix="比对 DOI 后修正 bib 中拼写错误的 title / 年份 / 作者",
            **details))
    return findings


def _field_in_evidence(field: str, bibtex: dict[str, str]) -> bool:
    """Check if any external BibTeX source has a non-empty value for *field*."""
    for src in list(T1_BIB_SOURCES) + list(T2_BIB_SOURCES):
        bt = bibtex.get(src, "")
        if not bt:
            continue
        if "/" in field:
            for a in field.split("/"):
                if _bibtex_field_value(a, bt):
                    return True
        else:
            if _bibtex_field_value(field, bt):
                return True
    return False


def _bibtex_field_value(field: str, bibtex_str: str) -> str | None:
    """Extract a field value from a raw BibTeX string. Returns None if absent/empty.

    Uses bibtexparser when available (handles nested braces correctly).
    Falls back to regex otherwise (may fail on nested braces).
    """
    if not bibtex_str:
        return None
    if HAS_BIBPARSER:
        try:
            parser = BibTexParser(common_strings=True)
            parser.ignore_nonstandard_types = False
            bib = bibtexparser.loads(bibtex_str, parser=parser)
            if bib.entries:
                v = bib.entries[0].get(field, "")
                if v:
                    return v.strip()
        except Exception:
            pass
    # Fallback: balanced-brace parser
    pat = re.compile(rf"\b{field}\s*=\s*", re.I)
    m = pat.search(bibtex_str)
    if not m:
        return None
    pos = m.end()
    while pos < len(bibtex_str) and bibtex_str[pos] in " \t\r\n":
        pos += 1
    if pos >= len(bibtex_str):
        return None
    ch = bibtex_str[pos]
    if ch in ('"', '{'):
        close = '"' if ch == '"' else '}'
        depth, start = (0 if ch == '"' else 1), pos + 1
        i = start
        while i < len(bibtex_str):
            c = bibtex_str[i]
            if c == '{':
                depth += 1
            elif c == '}':
                depth -= 1
                if depth == 0:
                    break
            elif c == '"' and ch == '"':
                break
            i += 1
        v = bibtex_str[start:i].strip()
        return v if v else None
    # Bare value (no braces/quotes)
    end = pos
    while end < len(bibtex_str) and bibtex_str[end] not in (',', '\n', '\r'):
        end += 1
    v = bibtex_str[pos:end].strip()
    return v if v else None


def check_a6(bib: dict, evidence: dict) -> list[dict]:
    findings: list[dict] = []
    for k in sorted(bib.keys()):
        e = bib[k]
        etype = (e.get("type") or "").lower()

        check_entry = dict(e)
        if e.get("authors"):
            check_entry["author"] = "x"

        # Required fields — always report
        req = REQUIRED_FIELDS.get(etype)
        if req is not None:
            missing = required_present(check_entry, req)
            if missing:
                findings.append(make_finding(
                    "A6_field_missing", "cleanup", k,
                    f"bib 条目 `{k}` (类型 @{etype}) 缺少必填字段：" + ", ".join(missing),
                    entry_type=etype, missing_fields=missing, tier="required",
                    suggested_fix="补全缺失字段或更换合适的 BibTeX 类型"))

        # Recommended fields — only report when external source has the field
        rec = RECOMMENDED_FIELDS.get(etype)
        if rec is not None:
            ev = evidence.get(k)
            bibtex = (ev.get("bibtex") or {}) if ev and ev.get("resolved") else {}
            missing_rec = required_present(check_entry, rec)
            missing_with_evidence = []
            for f in missing_rec:
                if _field_in_evidence(f, bibtex):
                    missing_with_evidence.append(f)
            if missing_with_evidence:
                findings.append(make_finding(
                    "A6_field_missing", "cleanup", k,
                    f"bib 条目 `{k}` (类型 @{etype}) 缺少推荐字段（外部源有数据）："
                    + ", ".join(missing_with_evidence),
                    entry_type=etype, missing_fields=missing_with_evidence,
                    tier="recommended",
                    suggested_fix="从外部源补全缺失字段"))
    return findings


def check_a7(cited_keys: set[str], bib: dict, evidence: dict) -> list[dict]:
    """Venue checks — only when DBLP BibTeX is available.

    A7_venue_mismatch (cleanup): bib venue and DBLP refer to different venues.
    A8_venue_style   (cleanup): same venue but written differently (e.g. abbreviation
                                vs full name), suggesting format unification.
    """
    findings: list[dict] = []
    for k in sorted(cited_keys):
        ev = evidence.get(k)
        if not ev or not ev.get("resolved"):
            continue
        bibtex = ev.get("bibtex") or {}
        dblp_bib = bibtex.get("dblp")
        if not dblp_bib:
            continue
        parsed = parse_bibtex_string(dblp_bib)
        if not parsed or not parsed.get("venue"):
            continue
        dblp_venue = parsed["venue"]
        entry = bib.get(k, {})
        bib_venue = (entry.get("booktitle") or entry.get("venue")
                     or entry.get("journal") or entry.get("howpublished") or "")
        if not bib_venue:
            continue
        if is_pure_arxiv_venue(bib_venue):
            continue
        if is_arxiv_venue(bib_venue) and is_arxiv_venue(dblp_venue):
            if not (entry.get("booktitle") or entry.get("journal")):
                continue
        if venue_lax_match(bib_venue, dblp_venue):
            if venue_style_differs(bib_venue, dblp_venue):
                findings.append(make_finding(
                    "A8_venue_style", "cleanup", k,
                    f"`{k}` 的 venue 与 DBLP 等价但写法不同："
                    f"bib=`{bib_venue}` vs DBLP=`{dblp_venue}`",
                    bib_venue=bib_venue, dblp_venue=dblp_venue,
                    suggested_fix=f"建议统一为 DBLP 格式：{dblp_venue}"))
            continue
        # Genuine mismatch
        findings.append(make_finding(
            "A7_venue_mismatch", "cleanup", k,
            f"`{k}` 的 venue 与 DBLP BibTeX 不一致："
            f"bib=`{bib_venue}` vs DBLP=`{dblp_venue}`",
            bib_venue=bib_venue, dblp_venue=dblp_venue,
            suggested_fix="将 bib 中 venue / booktitle 字段更新为正式发表会议或期刊名"))
    return findings


def _diagnosis_items(unresolved_diagnosis: Any) -> list[dict]:
    if isinstance(unresolved_diagnosis, list):
        return [x for x in unresolved_diagnosis if isinstance(x, dict)]
    if isinstance(unresolved_diagnosis, dict):
        for key in ("diagnoses", "items", "findings"):
            value = unresolved_diagnosis.get(key)
            if isinstance(value, list):
                return [x for x in value if isinstance(x, dict)]
        if "diagnosis" in unresolved_diagnosis:
            return [unresolved_diagnosis]
        return [x for x in unresolved_diagnosis.values() if isinstance(x, dict)]
    return []


def check_a9(unresolved_diagnosis: Any, current_keys: set[str] | None = None) -> list[dict]:
    findings: list[dict] = []
    for item in _diagnosis_items(unresolved_diagnosis):
        if item.get("diagnosis") != "bib_error":
            continue
        key = item.get("key")
        if current_keys is not None and key not in current_keys:
            continue
        findings.append(make_finding(
            "A9_bib_entry_error", "critical", key,
            f"Stage 1 诊断发现 `{key}` 的 bib 条目存在错误：{item.get('issue', '原因未说明')}",
            issue=item.get("issue", ""),
            evidence=item.get("evidence", ""),
            correct_info=item.get("correct_info") or {},
            suggested_fix="修正 bib 条目的 arXiv ID、DOI、标题或作者信息"))
    return findings


# ---------------------------------------------------------------------------
# Main

def main():
    ap = argparse.ArgumentParser(description="Track A entry-health checks.")
    ap.add_argument("paper_dir", type=Path)
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args()

    paper_dir = args.paper_dir.resolve()
    out_dir = args.out or (paper_dir / "runs" / "citation-check")
    out_dir.mkdir(parents=True, exist_ok=True)

    def need(name: str) -> dict | list:
        p = out_dir / name
        if not p.exists():
            raise SystemExit(f"[track_a] missing {p} — run earlier stages first")
        return json.loads(p.read_text(encoding="utf-8"))

    bib: dict = need("bib_entries.json")
    citations: list = need("citations.json")
    evidence: dict = need("evidence_pack.json")
    diagnosis_path = out_dir / "unresolved_diagnosis.json"
    unresolved_diagnosis: list = (
        json.loads(diagnosis_path.read_text(encoding="utf-8"))
        if diagnosis_path.exists() else []
    )

    cited_keys = {c["key"] for c in citations} & set(bib.keys())

    findings: list[dict] = []
    findings += check_a1(citations, bib)
    findings += check_a2(citations, bib)
    findings += check_a3(bib)
    findings += check_a4(cited_keys, bib, evidence)
    findings += check_a5(cited_keys, bib, evidence)
    findings += check_a6(bib, evidence)
    findings += check_a7(cited_keys, bib, evidence)
    findings += check_a9(unresolved_diagnosis, current_keys=cited_keys)

    counts: dict[str, int] = {"critical": 0, "warning": 0, "cleanup": 0}
    by_code: dict[str, int] = {}
    for f in findings:
        counts[f["severity"]] = counts.get(f["severity"], 0) + 1
        by_code[f["code"]] = by_code.get(f["code"], 0) + 1

    summary = {
        "bib_entries": len(bib),
        "citation_sites": len(citations),
        "unique_cited_keys": len({c["key"] for c in citations}),
        "evaluated_keys": len(cited_keys),
        "counts_by_severity": counts,
        "counts_by_code": by_code,
    }

    out = {"summary": summary, "findings": findings}
    out_path = out_dir / "track_a_findings.json"
    out_path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"[track_a] entries={len(bib)}  cite_sites={len(citations)}  "
          f"evaluated={len(cited_keys)}")
    print(f"[track_a] findings: critical={counts['critical']}  "
          f"warning={counts['warning']}  cleanup={counts['cleanup']}")
    for code in sorted(by_code):
        print(f"           {code:<24} {by_code[code]}")
    print(f"[track_a] -> {out_path}")


if __name__ == "__main__":
    main()

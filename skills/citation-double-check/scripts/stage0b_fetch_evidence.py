#!/usr/bin/env python3
"""Stage 0b — Per-source: fetch metadata, then fetch native BibTeX if available.

For each unique cited bib key, query every source by title:
  - Semantic Scholar : JSON only             (no BibTeX endpoint)
  - OpenAlex         : JSON only             (no BibTeX endpoint)
  - DBLP             : JSON  + native BibTeX (https://dblp.org/rec/<id>.bib?param=1)
  - Crossref         : JSON  + native BibTeX (doi.org content negotiation)
  - arXiv            : Atom  + native BibTeX (https://arxiv.org/bibtex/<id>)
  - PubMed           : XML   only            (no BibTeX endpoint)
  - DataCite         : JSON  + native BibTeX (doi.org content negotiation)
  - Google Scholar   : scraped + scholarly.bibtex() (rate-limited, optional)

Result lands in <out_dir>/evidence_pack.json with shape:
  {
    "<key>": {
      "resolved": bool,
      "sources_used": ["..."],
      "data":   {<source>: {title, authors, year, venue, doi, ..., match_score}},
      "bibtex": {<source>: "@... { ... }"},   # only sources that returned BibTeX
      "abstract": str, "tldr": str, "abstract_source": str
    }
  }

All HTTP responses cached under <out_dir>/.cache/<source>/{data,bibtex}/<sha1>.json.
Title match threshold: 0.85 (SequenceMatcher + Jaccard average).

Usage:
  python stage0b_fetch_evidence.py <paper_dir> [--out DIR] [--mailto EMAIL]
                                   [--no-cache] [--only KEY,KEY,...]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
import unicodedata
import urllib.parse
import xml.etree.ElementTree as ET
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

import requests

try:
    import bibtexparser
    from bibtexparser.bparser import BibTexParser
except Exception:
    bibtexparser = None  # we degrade gracefully; raw BibTeX still saved

try:
    from scholarly import scholarly
    HAS_SCHOLARLY = True
except Exception:
    HAS_SCHOLARLY = False


TITLE_SIM_THRESHOLD = 0.85
DEFAULT_MAILTO = "noreply@example.com"
USER_AGENT = "citation-double-check/0.2 (Stage 0b evidence fetcher)"


# ---------------------------------------------------------------------------
# Title normalization + similarity

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


def first_author_surname(authors: list[str]) -> str:
    if not authors:
        return ""
    a = authors[0]
    surname = a.split(",")[0].strip() if "," in a else a.split()[-1]
    surname = re.sub(r"\s+\d{4}$", "", surname)
    return surname


# ---------------------------------------------------------------------------
# Cached HTTP

class CachedHTTP:
    def __init__(self, cache_dir: Path, use_cache: bool = True):
        self.cache_dir = cache_dir
        self.use_cache = use_cache
        self._last_call: dict[str, float] = {}

    def _cache_path(self, kind: str, source: str, key: str) -> Path:
        h = hashlib.sha1(key.encode("utf-8")).hexdigest()
        d = self.cache_dir / source / kind
        d.mkdir(parents=True, exist_ok=True)
        return d / f"{h}.json"

    def _throttle(self, source: str, min_interval: float):
        if min_interval <= 0:
            return
        now = time.time()
        last = self._last_call.get(source, 0.0)
        wait = min_interval - (now - last)
        if wait > 0:
            time.sleep(wait)
        self._last_call[source] = time.time()

    def get(self, source: str, kind: str, url: str, *,
            params: dict | None = None, headers: dict | None = None,
            min_interval: float = 0.0, parse: str = "json") -> dict | None:
        cache_key = url + ("?" + urllib.parse.urlencode(sorted((params or {}).items())) if params else "")
        cpath = self._cache_path(kind, source, cache_key)
        if self.use_cache and cpath.exists():
            try:
                return json.loads(cpath.read_text(encoding="utf-8"))
            except Exception:
                pass

        self._throttle(source, min_interval)
        h = {"User-Agent": USER_AGENT, **(headers or {})}
        try:
            r = requests.get(url, params=params, headers=h, timeout=30)
        except requests.RequestException as e:
            print(f"[0b] {source}/{kind} network error: {e}", file=sys.stderr)
            return None

        if r.status_code == 429:
            time.sleep(5.0)
            try:
                r = requests.get(url, params=params, headers=h, timeout=30)
            except requests.RequestException as e:
                print(f"[0b] {source}/{kind} retry network error: {e}", file=sys.stderr)
                return None
        if r.status_code != 200:
            print(f"[0b] {source}/{kind} HTTP {r.status_code} for {r.url}", file=sys.stderr)
            return None

        if parse == "json":
            try:
                data = r.json()
            except ValueError:
                print(f"[0b] {source}/{kind} non-JSON body", file=sys.stderr)
                return None
        else:
            data = {"_text": r.text}

        if self.use_cache:
            try:
                cpath.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            except Exception as e:
                print(f"[0b] cache write failed: {e}", file=sys.stderr)
        return data


# ---------------------------------------------------------------------------
# Step 1: per-source metadata (title -> structured fields)

def s2_data(http: CachedHTTP, title: str, api_key: str | None) -> dict | None:
    if not title:
        return None
    headers = {"x-api-key": api_key} if api_key else {}
    interval = 0.05 if api_key else 1.1
    data = http.get(
        "semantic_scholar", "data",
        "https://api.semanticscholar.org/graph/v1/paper/search",
        params={"query": title, "limit": 5,
                "fields": "title,authors,year,venue,externalIds,abstract,tldr"},
        headers=headers, min_interval=interval,
    )
    if not data or not data.get("data"):
        return None
    best, score = None, 0.0
    for h in data["data"]:
        s = title_similarity(title, h.get("title", ""))
        if s > score: score, best = s, h
    if not best or score < TITLE_SIM_THRESHOLD:
        return None
    return {
        "title": best.get("title", ""),
        "authors": [a.get("name", "") for a in (best.get("authors") or [])],
        "year": best.get("year"),
        "venue": best.get("venue") or "",
        "doi": (best.get("externalIds") or {}).get("DOI"),
        "arxiv": (best.get("externalIds") or {}).get("ArXiv"),
        "abstract": best.get("abstract") or "",
        "tldr": ((best.get("tldr") or {}).get("text")) or "",
        "match_score": round(score, 3),
    }


def openalex_data(http: CachedHTTP, title: str, mailto: str) -> dict | None:
    if not title:
        return None
    data = http.get(
        "openalex", "data", "https://api.openalex.org/works",
        params={"search": title, "per-page": 5, "mailto": mailto},
        min_interval=0.1,
    )
    if not data or not data.get("results"):
        return None
    best, score = None, 0.0
    for h in data["results"]:
        s = title_similarity(title, h.get("title", "") or h.get("display_name", ""))
        if s > score: score, best = s, h
    if not best or score < TITLE_SIM_THRESHOLD:
        return None
    abstract = ""
    inv = best.get("abstract_inverted_index")
    if inv:
        n = max((p for ps in inv.values() for p in ps), default=-1) + 1
        words = [""] * n
        for w, ps in inv.items():
            for p in ps:
                if 0 <= p < n: words[p] = w
        abstract = " ".join(w for w in words if w)
    venue = ""
    host = best.get("host_venue") or {}
    if host.get("display_name"):
        venue = host["display_name"]
    else:
        venue = ((best.get("primary_location") or {}).get("source") or {}).get("display_name", "")
    return {
        "title": best.get("title") or best.get("display_name") or "",
        "authors": [(a.get("author") or {}).get("display_name", "")
                    for a in (best.get("authorships") or [])],
        "year": best.get("publication_year"),
        "venue": venue,
        "doi": (best.get("doi") or "").replace("https://doi.org/", "") or None,
        "abstract": abstract,
        "match_score": round(score, 3),
    }


def dblp_data(http: CachedHTTP, title: str, surname: str = "") -> dict | None:
    if not title:
        return None
    # Try title search first
    data = http.get(
        "dblp", "data", "https://dblp.org/search/publ/api",
        params={"q": title, "format": "json", "h": 30},
        min_interval=0.5,
    )
    hits = (((data or {}).get("result") or {}).get("hits") or {}).get("hit") or []
    best, score = None, 0.0
    for hit in hits:
        info = hit.get("info") or {}
        s = title_similarity(title, info.get("title", ""))
        if s > score: score, best = s, hit
    # If title search fails and we have a surname, try author + keywords
    if (not best or score < TITLE_SIM_THRESHOLD) and surname:
        # Extract key words from title (first 3-4 words)
        words = title.split()[:4]
        keywords = " ".join(words)
        author_data = http.get(
            "dblp", "data", "https://dblp.org/search/publ/api",
            params={"q": f"{surname} {keywords}", "format": "json", "h": 10},
            min_interval=0.5,
        )
        author_hits = (((author_data or {}).get("result") or {}).get("hits") or {}).get("hit") or []
        for hit in author_hits:
            info = hit.get("info") or {}
            s = title_similarity(title, info.get("title", ""))
            if s > score:
                score, best = s, hit
    if not best or score < TITLE_SIM_THRESHOLD:
        return None
    info = best.get("info") or {}
    record_key = (best.get("@id") or "").rsplit("/", 1)[-1] or info.get("key", "")
    # Prefer URL-derived key (like "conf/acl/DongYLLXL00ZZ24")
    url = info.get("url", "")
    if url:
        m = re.search(r"dblp\.org/rec/([^/?#]+(?:/[^/?#]+)*)", url)
        if m:
            record_key = m.group(1)
    a_list = (info.get("authors") or {}).get("author") or []
    if isinstance(a_list, dict): a_list = [a_list]
    return {
        "title": info.get("title", ""),
        "authors": [(a.get("text") if isinstance(a, dict) else str(a)) for a in a_list],
        "year": info.get("year"),
        "venue": info.get("venue", ""),
        "doi": info.get("doi"),
        "type": info.get("type", ""),
        "record_key": record_key,
        "match_score": round(score, 3),
    }


def crossref_data(http: CachedHTTP, title: str, surname: str) -> dict | None:
    if not title:
        return None
    params = {"query.title": title, "rows": 5}
    if surname: params["query.author"] = surname
    data = http.get("crossref", "data", "https://api.crossref.org/works",
                    params=params, min_interval=0.1)
    if not data:
        return None
    items = ((data.get("message") or {}).get("items")) or []
    best, score = None, 0.0
    for it in items:
        t = (it.get("title") or [""])[0]
        s = title_similarity(title, t)
        if s > score: score, best = s, it
    if not best or score < TITLE_SIM_THRESHOLD:
        return None
    venue = ""
    if best.get("container-title"): venue = best["container-title"][0]
    elif best.get("event"): venue = (best["event"] or {}).get("name", "")
    year = None
    for k in ("published-print", "published-online", "issued", "created"):
        dp = ((best.get(k) or {}).get("date-parts") or [[]])[0]
        if dp: year = dp[0]; break
    # Crossref 偶尔在 'abstract' 字段返回 JATS XML 包裹的摘要
    abstract = best.get("abstract") or ""
    if abstract:
        abstract = re.sub(r"<[^>]+>", "", abstract).strip()
    doi = best.get("DOI")
    # Quality check: skip if venue is empty (likely data pollution)
    # or DOI format is suspicious (e.g., 10.65215/xxx is often fake)
    if not venue and doi:
        # Check if DOI prefix is known to be problematic
        prefix = doi.split("/")[0] if "/" in doi else ""
        if prefix in ("10.65215",):
            return None  # Known problematic prefix
    return {
        "title": (best.get("title") or [""])[0],
        "authors": [f"{a.get('given','').strip()} {a.get('family','').strip()}".strip()
                    for a in (best.get("author") or [])],
        "year": year, "venue": venue,
        "doi": best.get("DOI"), "type": best.get("type", ""),
        "abstract": abstract,
        "match_score": round(score, 3),
    }


def arxiv_data(http: CachedHTTP, title: str) -> dict | None:
    if not title:
        return None
    data = http.get(
        "arxiv", "data", "http://export.arxiv.org/api/query",
        params={"search_query": f'ti:"{title}"', "max_results": 5},
        min_interval=3.0, parse="text",
    )
    if not data or "_text" not in data:
        return None
    try:
        root = ET.fromstring(data["_text"])
    except ET.ParseError:
        return None
    ns = {"atom": "http://www.w3.org/2005/Atom"}
    best, score = None, 0.0
    for entry in root.findall("atom:entry", ns):
        t = (entry.findtext("atom:title", default="", namespaces=ns) or "").strip()
        s = title_similarity(title, t)
        if s > score: score, best = s, entry
    if best is None or score < TITLE_SIM_THRESHOLD:
        return None
    arxiv_id = (best.findtext("atom:id", default="", namespaces=ns) or "").rsplit("/", 1)[-1]
    arxiv_id = re.sub(r"v\d+$", "", arxiv_id)
    authors = [(a.findtext("atom:name", default="", namespaces=ns) or "").strip()
               for a in best.findall("atom:author", ns)]
    published = best.findtext("atom:published", default="", namespaces=ns) or ""
    year = published[:4] if published else None
    abstract = (best.findtext("atom:summary", default="", namespaces=ns) or "").strip()
    return {
        "title": (best.findtext("atom:title", default="", namespaces=ns) or "").strip(),
        "authors": authors,
        "year": int(year) if year and year.isdigit() else None,
        "venue": "arXiv", "doi": None, "arxiv": arxiv_id,
        "abstract": abstract,
        "match_score": round(score, 3),
    }


def pubmed_data(http: CachedHTTP, title: str, mailto: str) -> dict | None:
    if not title:
        return None
    api_key = os.environ.get("NCBI_API_KEY")
    base_params = {"db": "pubmed", "term": title, "retmode": "json", "retmax": 3}
    if mailto: base_params["email"] = mailto
    if api_key: base_params["api_key"] = api_key
    res = http.get("pubmed", "data",
                   "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi",
                   params=base_params, min_interval=0.4)
    if not res:
        return None
    ids = (res.get("esearchresult") or {}).get("idlist") or []
    if not ids:
        return None
    fetch_params = {"db": "pubmed", "id": ",".join(ids[:3]),
                    "retmode": "xml", "rettype": "abstract"}
    if mailto: fetch_params["email"] = mailto
    if api_key: fetch_params["api_key"] = api_key
    xml = http.get("pubmed", "fetch",
                   "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi",
                   params=fetch_params, min_interval=0.4, parse="text")
    if not xml or "_text" not in xml:
        return None
    try:
        root = ET.fromstring(xml["_text"])
    except ET.ParseError:
        return None
    best, score = None, 0.0
    for art in root.findall(".//PubmedArticle"):
        t = (art.findtext(".//Article/ArticleTitle", default="") or "").strip()
        s = title_similarity(title, t)
        if s > score: score, best = s, art
    if best is None or score < TITLE_SIM_THRESHOLD:
        return None
    a = best.find(".//Article") or best
    pmid = best.findtext(".//PMID", default="") or ""
    doi = ""
    for aid in best.findall(".//ArticleId"):
        if aid.get("IdType") == "doi": doi = (aid.text or "").strip(); break
    journal = a.findtext(".//Journal/Title", default="") or ""
    year = a.findtext(".//Journal/JournalIssue/PubDate/Year", default="") or ""
    if not year:
        md = a.findtext(".//Journal/JournalIssue/PubDate/MedlineDate", default="") or ""
        m = re.search(r"\d{4}", md)
        if m: year = m.group(0)
    authors = []
    for au in a.findall(".//Author"):
        ln = au.findtext("LastName", default="") or ""
        fn = au.findtext("ForeName", default="") or ""
        if ln: authors.append(f"{ln}, {fn}".strip().rstrip(","))
    abstract_parts = [t.text or "" for t in a.findall(".//Abstract/AbstractText")]
    return {
        "title": (a.findtext(".//ArticleTitle", default="") or "").strip(),
        "authors": authors,
        "year": int(year) if year.isdigit() else None,
        "venue": journal,
        "doi": doi or None,
        "pmid": pmid,
        "abstract": " ".join(p for p in abstract_parts if p),
        "match_score": round(score, 3),
    }


def datacite_data(http: CachedHTTP, title: str) -> dict | None:
    if not title:
        return None
    res = http.get("datacite", "data", "https://api.datacite.org/dois",
                   params={"query": title, "page[size]": 5},
                   headers={"Accept": "application/json"},
                   min_interval=0.2)
    if not res or not res.get("data"):
        return None
    best, score = None, 0.0
    for it in res["data"]:
        attr = it.get("attributes") or {}
        ts = (attr.get("titles") or [])
        t = (ts[0].get("title") if ts else "") or ""
        s = title_similarity(title, t)
        if s > score: score, best = s, it
    if best is None or score < TITLE_SIM_THRESHOLD:
        return None
    attr = best.get("attributes") or {}
    creators = attr.get("creators") or []
    authors = []
    for c in creators:
        n = c.get("name") or ""
        if n: authors.append(n)
    pub_year = attr.get("publicationYear")
    year = int(pub_year) if isinstance(pub_year, int) or (isinstance(pub_year, str) and pub_year.isdigit()) else None
    venue = (attr.get("publisher") or "")
    if isinstance(venue, dict): venue = venue.get("name", "")
    return {
        "title": ((attr.get("titles") or [{}])[0].get("title", "")),
        "authors": authors,
        "year": year,
        "venue": venue,
        "doi": (attr.get("doi") or "").lower() or None,
        "type": (attr.get("types") or {}).get("resourceTypeGeneral", ""),
        "match_score": round(score, 3),
    }


# Google Scholar: scholarly library handles HTTP; we cache the parsed dict ourselves.

def gscholar_data(http: CachedHTTP, title: str, min_interval: float = 25.0) -> dict | None:
    """Google Scholar via scholarly. Heavy rate-limit: GS captcha-blocks aggressive
    callers within a few requests. Default 25s/request (community consensus).
    Cache key is the title; once cached, subsequent runs skip GS entirely."""
    if not title or not HAS_SCHOLARLY:
        return None
    cpath = http._cache_path("data", "gscholar", title)
    cached_bib_path = http._cache_path("bibtex", "gscholar", title)
    if http.use_cache and cpath.exists():
        try:
            return json.loads(cpath.read_text(encoding="utf-8"))
        except Exception:
            pass

    http._throttle("gscholar", min_interval)
    try:
        gen = scholarly.search_pubs(title)
        hit = next(gen, None)
    except Exception as e:
        print(f"[0b] gscholar/data error: {e}", file=sys.stderr)
        return None
    if hit is None:
        return None
    bib = hit.get("bib") or {}
    t = bib.get("title", "") or ""
    s = title_similarity(title, t)
    if s < TITLE_SIM_THRESHOLD:
        return None

    # Try to capture native BibTeX while we still have the hit object
    bibtex_text = None
    try:
        http._throttle("gscholar", min_interval)
        bibtex_text = scholarly.bibtex(hit)
    except Exception as e:
        print(f"[0b] gscholar/bibtex error: {e}", file=sys.stderr)

    auth = bib.get("author") or []
    if isinstance(auth, str): auth = [auth]
    out = {
        "title": t,
        "authors": auth,
        "year": int(bib["pub_year"]) if str(bib.get("pub_year","")).isdigit() else None,
        "venue": bib.get("venue") or bib.get("journal") or "",
        "doi": None,
        "abstract": bib.get("abstract", "") or "",
        "num_citations": hit.get("num_citations"),
        "pub_url": hit.get("pub_url"),
        "match_score": round(s, 3),
    }

    if http.use_cache:
        try:
            cpath.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
            if bibtex_text:
                cached_bib_path.write_text(json.dumps({"_text": bibtex_text}), encoding="utf-8")
        except Exception as e:
            print(f"[0b] gscholar cache write failed: {e}", file=sys.stderr)
    elif bibtex_text:
        # carry through in-process if cache off
        out["_bibtex_text"] = bibtex_text
    return out


def gscholar_bibtex_from_cache(http: CachedHTTP, title: str, data_record: dict | None) -> str | None:
    if not title:
        return None
    # In-process carry-through (--no-cache path)
    if data_record and "_bibtex_text" in data_record:
        return data_record["_bibtex_text"]
    cpath = http._cache_path("bibtex", "gscholar", title)
    if cpath.exists():
        try:
            return json.loads(cpath.read_text(encoding="utf-8")).get("_text")
        except Exception:
            return None
    return None


# ---------------------------------------------------------------------------
# Step 2: per-source native BibTeX (only sources that publish a BibTeX endpoint)

def doi_to_bibtex(http: CachedHTTP, source: str, doi: str) -> str | None:
    """doi.org content negotiation. Used by Crossref + DataCite (same endpoint)."""
    if not doi:
        return None
    doi = doi.strip()
    for prefix in ("https://doi.org/", "http://doi.org/", "doi:"):
        if doi.lower().startswith(prefix):
            doi = doi[len(prefix):]; break
    res = http.get(
        source, "bibtex", f"https://doi.org/{urllib.parse.quote(doi, safe='/')}",
        headers={"Accept": "application/x-bibtex"},
        min_interval=0.1, parse="text",
    )
    if not res or "_text" not in res:
        return None
    text = (res["_text"] or "").strip()
    if not text or not text.startswith("@"):
        return None
    if text.startswith("@data{"):
        text = text.replace("@data{", "@misc{", 1)
    return text


def dblp_bibtex(http: CachedHTTP, record_key: str) -> str | None:
    if not record_key:
        return None
    res = http.get(
        "dblp", "bibtex", f"https://dblp.org/rec/{record_key}.bib",
        params={"param": "1"}, min_interval=0.5, parse="text",
    )
    if not res or "_text" not in res:
        return None
    text = (res["_text"] or "").strip()
    return text if text.startswith("@") else None


def arxiv_bibtex(http: CachedHTTP, arxiv_id: str) -> str | None:
    if not arxiv_id:
        return None
    res = http.get(
        "arxiv", "bibtex", f"https://arxiv.org/bibtex/{arxiv_id}",
        min_interval=3.0, parse="text",
    )
    if not res or "_text" not in res:
        return None
    text = (res["_text"] or "").strip()
    return text if text.startswith("@") else None


# ---------------------------------------------------------------------------
# Per-key resolution

def resolve_key(http: CachedHTTP, key: str, entry: dict, mailto: str,
                s2_key: str | None, gs_interval: float = 25.0,
                enable_gs: bool = True) -> dict:
    title = entry.get("title", "")
    surname = first_author_surname(entry.get("authors") or [])

    data: dict[str, dict] = {}
    bibtex: dict[str, str] = {}

    # Step 1: every source — fetch metadata
    if (d := s2_data(http, title, s2_key)):       data["semantic_scholar"] = d
    if (d := openalex_data(http, title, mailto)): data["openalex"]         = d
    if (d := dblp_data(http, title, surname)):    data["dblp"]             = d
    if (d := crossref_data(http, title, surname)):data["crossref"]         = d
    if (d := arxiv_data(http, title)):            data["arxiv"]            = d
    if (d := pubmed_data(http, title, mailto)):   data["pubmed"]           = d
    if (d := datacite_data(http, title)):         data["datacite"]         = d
    if enable_gs and (d := gscholar_data(http, title, gs_interval)):
        data["gscholar"] = d

    # Step 2: sources with a native BibTeX endpoint.
    # Note: S2 returns a `citationStyles.bibtex` field but it is rendered server-side
    # from S2's own JSON (truncated authors like "A. Andonian", non-canonical journal),
    # so it does not give us anything beyond `data.semantic_scholar`. We do NOT store it.
    if "dblp" in data:
        b = dblp_bibtex(http, data["dblp"].get("record_key", ""))
        if b: bibtex["dblp"] = b
    if "crossref" in data and data["crossref"].get("doi"):
        b = doi_to_bibtex(http, "crossref", data["crossref"]["doi"])
        if b: bibtex["crossref"] = b
    if "arxiv" in data and data["arxiv"].get("arxiv"):
        b = arxiv_bibtex(http, data["arxiv"]["arxiv"])
        if b: bibtex["arxiv"] = b
    if "datacite" in data and data["datacite"].get("doi"):
        b = doi_to_bibtex(http, "datacite", data["datacite"]["doi"])
        if b: bibtex["datacite"] = b
    if "gscholar" in data:
        b = gscholar_bibtex_from_cache(http, title, data["gscholar"])
        if b: bibtex["gscholar"] = b

    sources_used = list(data.keys())

    abstract, abstract_source = "", None
    for src in ("semantic_scholar", "openalex", "arxiv", "pubmed", "gscholar"):
        if data.get(src, {}).get("abstract"):
            abstract = data[src]["abstract"]; abstract_source = src; break
    tldr = (data.get("semantic_scholar") or {}).get("tldr", "")

    return {
        "resolved": bool(sources_used),
        "sources_used": sources_used,
        "data": data,
        "bibtex": bibtex,
        "abstract": abstract,
        "tldr": tldr,
        "abstract_source": abstract_source,
    }


# ---------------------------------------------------------------------------
# Main

def main():
    ap = argparse.ArgumentParser(description="Stage 0b evidence fetcher.")
    ap.add_argument("paper_dir", type=Path)
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--mailto", type=str, default=DEFAULT_MAILTO,
                    help="Email for OpenAlex/PubMed polite pool.")
    ap.add_argument("--no-cache", action="store_true")
    ap.add_argument("--only", type=str, default=None,
                    help="Comma-separated bib keys (default: all cited).")
    ap.add_argument("--gs-interval", type=float, default=25.0,
                    help="Min seconds between Google Scholar requests (default 25).")
    ap.add_argument("--enable-gs", action="store_true",
                    help="Enable Google Scholar (default OFF — GS aggressively "
                         "captcha-blocks; useful only when other sources miss).")
    args = ap.parse_args()

    paper_dir = args.paper_dir.resolve()
    out_dir = args.out or (paper_dir / "runs" / "citation-check")
    out_dir.mkdir(parents=True, exist_ok=True)

    bib_path = out_dir / "bib_entries.json"
    cites_path = out_dir / "citations.json"
    if not bib_path.exists():
        raise SystemExit(f"[0b] missing {bib_path} — run stage0a_parse.py first")

    bib: dict[str, dict] = json.loads(bib_path.read_text(encoding="utf-8"))
    cited: set[str] = set()
    if cites_path.exists():
        for c in json.loads(cites_path.read_text(encoding="utf-8")):
            cited.add(c["key"])
    else:
        cited = set(bib.keys())

    keys = sorted(k for k in cited if k in bib)
    if args.only:
        wanted = {k.strip() for k in args.only.split(",") if k.strip()}
        keys = [k for k in keys if k in wanted]

    s2_key = os.environ.get("SEMANTIC_SCHOLAR_API_KEY") or None
    enable_gs = args.enable_gs and HAS_SCHOLARLY
    print(f"[0b] keys to fetch: {len(keys)}")
    print(f"[0b] semantic scholar key: {'present' if s2_key else 'MISSING (1 req/s)'}")
    print(f"[0b] mailto:               {args.mailto}")
    print(f"[0b] gscholar:             {'OFF' if not enable_gs else f'ON (≥{args.gs_interval}s/req)'}"
          + ("" if HAS_SCHOLARLY else "  [scholarly not installed]"))
    print(f"[0b] cache:                {'OFF' if args.no_cache else 'ON'} ({out_dir / '.cache'})")

    http = CachedHTTP(out_dir / ".cache", use_cache=not args.no_cache)

    out_path = out_dir / "evidence_pack.json"
    pack: dict[str, dict] = {}
    if args.only and out_path.exists():
        pack = {k: v for k, v in json.loads(out_path.read_text(encoding="utf-8")).items() if k in cited}
    n_resolved = 0
    for i, k in enumerate(keys, 1):
        rec = resolve_key(http, k, bib[k], args.mailto, s2_key,
                          gs_interval=args.gs_interval, enable_gs=enable_gs)
        pack[k] = rec
        if rec["resolved"]:
            n_resolved += 1
            srcs = ",".join(rec["sources_used"])
            n_bib = len(rec["bibtex"])
            ab = "Y" if rec["abstract"] else "-"
            print(f"  [{i:>2}/{len(keys)}] {k:<32} ✓ data={srcs} bibtex={n_bib} abs={ab}")
        else:
            print(f"  [{i:>2}/{len(keys)}] {k:<32} ✗ unresolved")

    out_path.write_text(json.dumps(pack, ensure_ascii=False, indent=2), encoding="utf-8")
    print()
    print(f"[0b] resolved {n_resolved}/{len(keys)} keys")
    print(f"[0b] -> {out_path}")


if __name__ == "__main__":
    main()

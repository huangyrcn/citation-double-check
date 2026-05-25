#!/usr/bin/env python3
"""Stage 0a parser for the citation-double-check skill.

Discovers the LaTeX entry .tex (root file with \\documentclass and
\\begin{document}), follows \\input/\\include recursively, locates the .bib via
\\bibliography{} or \\addbibresource{}, then writes two JSONs into
<paper_dir>/runs/citation-check/:

  - bib_entries.json : one entry per bib key
  - citations.json   : one record per (cite-site, key) with location only

Sentence context is NOT extracted here — the downstream agent reads the
source .tex files directly, which gives it full, accurate context.

Usage:
  python stage0a_parse.py <paper_dir> [--entry FILE.tex] [--out DIR]

Dependency: bibtexparser (pip install bibtexparser).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Iterable

import bibtexparser
from bibtexparser.bparser import BibTexParser


# ---------------------------------------------------------------------------
# Patterns

CITE_CMDS = [
    "cite", "citep", "citet", "citeauthor", "citeyear", "citeyearpar",
    "citealp", "citealt", "citenum", "citeNP",
    "Cite", "Citep", "Citet", "Citeauthor",
    "parencite", "textcite", "autocite", "footcite", "smartcite",
    "Parencite", "Textcite", "Autocite", "Footcite",
    "fullcite", "citetitle", "nocite",
]
CITE_RE = re.compile(
    r"\\(" + "|".join(CITE_CMDS) + r")"
    r"(?:\*)?"
    r"(?:\s*\[[^\]]*\])?"
    r"(?:\s*\[[^\]]*\])?"
    r"\s*\{([^}]*)\}"
)

INPUT_RE = re.compile(r"\\(?:input|include|subfile)\s*\{([^}]+)\}")
BIB_RE = re.compile(r"\\bibliography\s*\{([^}]+)\}")
BIBRES_RE = re.compile(r"\\addbibresource\s*\{([^}]+)\}")


# ---------------------------------------------------------------------------
# Length-preserving comment stripping

def strip_comments(text: str) -> str:
    out = []
    for line in text.splitlines(keepends=True):
        has_nl = line.endswith("\n")
        body = line[:-1] if has_nl else line
        i, esc, cut = 0, False, len(body)
        while i < len(body):
            c = body[i]
            if c == "\\" and not esc:
                esc = True
                i += 1
                continue
            if c == "%" and not esc:
                cut = i
                break
            esc = False
            i += 1
        kept = body[:cut] + " " * (len(body) - cut)
        out.append(kept + ("\n" if has_nl else ""))
    return "".join(out)


# ---------------------------------------------------------------------------
# Entry detection + recursive \input walk

def detect_entry(paper_dir: Path) -> Path:
    """Find the LaTeX root: a *.tex containing both \\documentclass and \\begin{document}.

    If multiple match, prefer one that is not \\input'd by another candidate.
    """
    candidates: list[Path] = []
    for tex in sorted(paper_dir.glob("*.tex")):
        text = strip_comments(tex.read_text(encoding="utf-8", errors="replace"))
        if r"\documentclass" in text and r"\begin{document}" in text:
            candidates.append(tex)
    if not candidates:
        raise SystemExit(
            f"[stage0a] No LaTeX entry under {paper_dir} "
            r"(need a *.tex with both \documentclass and \begin{document})."
        )
    if len(candidates) == 1:
        return candidates[0]

    included: set[Path] = set()
    for c in candidates:
        text = strip_comments(c.read_text(encoding="utf-8", errors="replace"))
        for m in INPUT_RE.finditer(text):
            ref = m.group(1).strip()
            if not ref.endswith(".tex"):
                ref += ".tex"
            included.add((paper_dir / ref).resolve())
    top = [c for c in candidates if c.resolve() not in included]
    if len(top) == 1:
        return top[0]
    raise SystemExit(
        "[stage0a] Multiple LaTeX entries detected: "
        + ", ".join(c.name for c in (top or candidates))
        + ". Re-run with --entry <file>.tex."
    )


def walk_inputs(entry: Path, paper_dir: Path) -> list[tuple[Path, str]]:
    seen: set[Path] = set()
    files: list[tuple[Path, str]] = []

    def visit(p: Path):
        if not p.exists():
            print(f"[stage0a] warning: \\input target not found: {p}", file=sys.stderr)
            return
        rp = p.resolve()
        if rp in seen:
            return
        seen.add(rp)
        clean = strip_comments(p.read_text(encoding="utf-8", errors="replace"))
        files.append((p, clean))
        for m in INPUT_RE.finditer(clean):
            ref = m.group(1).strip()
            if not ref.endswith(".tex"):
                ref += ".tex"
            visit(paper_dir / ref)

    visit(entry)
    return files


def find_bib_paths(files: list[tuple[Path, str]], paper_dir: Path) -> list[Path]:
    paths: list[Path] = []
    seen: set[Path] = set()
    for _, text in files:
        for m in BIB_RE.finditer(text):
            for ref in m.group(1).split(","):
                ref = ref.strip()
                if not ref:
                    continue
                if not ref.endswith(".bib"):
                    ref += ".bib"
                p = (paper_dir / ref).resolve()
                if p.exists() and p not in seen:
                    seen.add(p); paths.append(p)
        for m in BIBRES_RE.finditer(text):
            ref = m.group(1).strip()
            if not ref.endswith(".bib"):
                ref += ".bib"
            p = (paper_dir / ref).resolve()
            if p.exists() and p not in seen:
                seen.add(p); paths.append(p)
    return paths


# ---------------------------------------------------------------------------
# Bib parsing

_BRACE_RE = re.compile(r"[{}]")
_WS_RE = re.compile(r"\s+")


def clean_field(s: str) -> str:
    s = _BRACE_RE.sub("", s or "")
    s = _WS_RE.sub(" ", s).strip()
    return s


def split_authors(s: str) -> list[str]:
    if not s:
        return []
    raw = re.split(r"\s+and\s+", s, flags=re.IGNORECASE)
    return [clean_field(a) for a in raw if a.strip()]


def parse_bibs(paths: Iterable[Path]) -> dict:
    entries: dict[str, dict] = {}
    for bp in paths:
        text = bp.read_text(encoding="utf-8", errors="replace")
        parser = BibTexParser(common_strings=True)
        parser.ignore_nonstandard_types = False
        bib = bibtexparser.loads(text, parser=parser)
        for e in bib.entries:
            key = e.get("ID")
            if not key:
                continue
            venue = (
                e.get("booktitle")
                or e.get("journal")
                or e.get("howpublished")
                or e.get("publisher")
                or ""
            )
            entries[key] = {
                "type": e.get("ENTRYTYPE", ""),
                "title": clean_field(e.get("title", "")),
                "authors": split_authors(e.get("author", "")),
                "venue": clean_field(venue),
                "year": clean_field(e.get("year", "")),
                "doi": clean_field(e.get("doi", "")),
                "url": (e.get("url", "") or "").strip(),
                "pages": clean_field(e.get("pages", "")),
                "journal": clean_field(e.get("journal", "")),
                "booktitle": clean_field(e.get("booktitle", "")),
                "howpublished": clean_field(e.get("howpublished", "")),
                "publisher": clean_field(e.get("publisher", "")),
                "school": clean_field(e.get("school", "")),
                "institution": clean_field(e.get("institution", "")),
                "note": clean_field(e.get("note", "")),
                "editor": clean_field(e.get("editor", "")),
                "volume": clean_field(e.get("volume", "")),
                "number": clean_field(e.get("number", "")),
                "eprint": clean_field(e.get("eprint", "")),
                "source_file": bp.name,
            }
    return entries


# ---------------------------------------------------------------------------
# Citation extraction

def extract_citations(files: list[tuple[Path, str]], paper_dir: Path) -> list[dict]:
    records: list[dict] = []
    for path, clean in files:
        for m in CITE_RE.finditer(clean):
            cmd = m.group(1)
            keys = [k.strip() for k in m.group(2).split(",") if k.strip()]
            if not keys:
                continue
            pos = m.start()
            line = clean.count("\n", 0, pos) + 1
            for key in keys:
                records.append({
                    "key": key,
                    "command": cmd,
                    "file": str(path.relative_to(paper_dir)),
                    "line": line,
                    "char_offset": pos,
                })
    return records


# ---------------------------------------------------------------------------
# Main

def main():
    ap = argparse.ArgumentParser(description="Stage 0a parser for citation-double-check.")
    ap.add_argument("paper_dir", type=Path)
    ap.add_argument("--entry", type=str, default=None,
                    help="Override LaTeX root file (relative to paper_dir).")
    ap.add_argument("--out", type=Path, default=None,
                    help="Output dir (default: <paper_dir>/runs/citation-check/).")
    args = ap.parse_args()

    paper_dir = args.paper_dir.resolve()
    if not paper_dir.is_dir():
        raise SystemExit(f"[stage0a] not a directory: {paper_dir}")

    entry = (paper_dir / args.entry) if args.entry else detect_entry(paper_dir)
    if not entry.exists():
        raise SystemExit(f"[stage0a] entry not found: {entry}")

    files = walk_inputs(entry, paper_dir)
    bib_paths = find_bib_paths(files, paper_dir)
    if not bib_paths:
        bib_paths = sorted(paper_dir.glob("*.bib"))

    out_dir = args.out or (paper_dir / "runs" / "citation-check")
    out_dir.mkdir(parents=True, exist_ok=True)

    bib_entries = parse_bibs(bib_paths)
    citations = extract_citations(files, paper_dir)

    (out_dir / "bib_entries.json").write_text(
        json.dumps(bib_entries, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (out_dir / "citations.json").write_text(
        json.dumps(citations, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    cited = {c["key"] for c in citations}
    print(f"[stage0a] entry:        {entry.relative_to(paper_dir)}")
    print(f"[stage0a] tex files:    {len(files)} ({', '.join(p.name for p, _ in files)})")
    print(f"[stage0a] bib files:    {len(bib_paths)} ({', '.join(p.name for p in bib_paths)})")
    print(f"[stage0a] bib entries:  {len(bib_entries)}")
    print(f"[stage0a] cite sites:   {len(citations)} ({len(cited)} unique keys)")
    print(f"[stage0a] -> {out_dir / 'bib_entries.json'}")
    print(f"[stage0a] -> {out_dir / 'citations.json'}")


if __name__ == "__main__":
    main()

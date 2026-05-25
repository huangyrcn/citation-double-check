# Citation Double-Check

A Claude Code skill that automatically vets LaTeX paper citations before submission or camera-ready. It catches two categories of citation problems that hurt papers most:

1. **Hard errors (Track A)** — the bib entry itself is wrong: undefined keys, unresolved references, metadata mismatches, missing fields, venue errors, duplicates.
2. **Soft errors (Track B)** — the bib entry is real but the citation is misplaced: the surrounding sentence's claim does not match the cited paper's abstract.

## Quick Start

```bash
# From a Claude Code session with a LaTeX paper directory:
> check my citations in ~/papers/my-paper
```

Or trigger with phrases like: "check citations", "vet the bib", "verify references", "camera-ready bib check".

## Pipeline

| Stage | What it does | Network? | LLM? |
|-------|-------------|----------|------|
| Stage 0a | Parse `.tex` and `.bib` files | No | No |
| Stage 0b | Fetch evidence from 8 academic sources | Yes | No |
| Stage 1 | Diagnose unresolved entries | Yes | Yes |
| Track A | Deterministic bib-health checks (A1-A9) | No | No |
| Track B | Claim-vs-abstract semantic checks | No | Yes |
| Stage 4 | Generate `report.md` + `report.json` | No | No |

## Track A Checks (A1-A9)

| Code | Severity | What it checks |
|------|----------|---------------|
| A1_undefined_key | Critical | `\cite{X}` where X is missing from bib |
| A2_unused_entry | Cleanup | Bib entry never cited |
| A3_duplicate_entry | Warning | Two keys with fuzzy title similarity ≥ 0.95, same year |
| A4_unresolved | Critical/Warning | All sources missed (downgraded for non-traditional sources) |
| A5_metadata_mismatch | Warning | Title/year/first-author mismatch vs external sources |
| A6_field_missing | Cleanup | Required or recommended BibTeX fields absent |
| A7_venue_mismatch | Cleanup | Bib venue differs from DBLP venue |
| A8_venue_style | Cleanup | Same venue but different formatting |
| A9_bib_entry_error | Critical | Stage 1 detected arXiv ID/DOI/title errors |

## Track B Verdicts

| Verdict | Severity | Meaning |
|---------|----------|---------|
| STRONG_SUPPORT | — | Abstract directly supports the claim (not reported) |
| WEAK_SUPPORT | Cleanup | Topically related but abstract lacks specifics |
| OFF_TOPIC | Critical | Abstract topic is completely unrelated |
| WRONG_DIRECTION | Critical | Abstract contradicts the claim |
| INSUFFICIENT_DATA | Warning | Abstract missing or too short |

## Output

All output goes to `<paper-dir>/runs/citation-check/`:

- `report.md` — human-readable, severity-grouped (Chinese descriptions)
- `report.json` — machine-readable flat list
- Intermediate artifacts: `bib_entries.json`, `citations.json`, `evidence_pack.json`, `track_a_findings.json`, `track_b_findings.json`

## Configuration

- **Semantic Scholar API key**: set `SEMANTIC_SCHOLAR_API_KEY` env var (optional, increases rate limit from 1/s to 100/s)
- **NCBI API key**: set `NCBI_API_KEY` env var (optional, for PubMed)

## Dependencies

```bash
pip install bibtexparser requests
```

## Running Tests

```bash
python3 -m unittest discover -s tests -v
```

## License

MIT

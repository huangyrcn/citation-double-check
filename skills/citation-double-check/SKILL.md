---
name: citation-double-check
description: >-
  Use when a LaTeX paper with a .bib file needs its citations vetted before
  submission or camera-ready -- runs two parallel checks: entry health (every
  \cite key resolves to a real, well-described bib entry) and claim support
  (each in-text citation's surrounding sentence is consistent with the cited
  paper's abstract). Trigger on phrases like "check citations", "vet the bib",
  "引用核对", "引用条目检查", "verify references", "camera-ready bib check". Do NOT
  use for bib formatting or style conversion -- those belong to
  citation-management or ars-citation-check.
---

# citation-double-check

A workflow for catching the two citation problems that hurt papers most: **fake/wrong bib entries** and **citations that don't actually support the surrounding claim**. This skill describes what to produce and how to judge; you decide *how* to implement each step using the tools you have (Python + bibtexparser, requests, an LLM, etc.).

## Why two tracks

Camera-ready papers fail review for two different reasons that need different evidence:

1. **The bib entry itself is wrong** — `\cite{xxx2024foo}` key missing, or bib has wrong title/year/venue, or the work doesn't exist (typo, hallucinated). Fix needs entry-vs-reality comparison.
2. **The bib entry is real but the citation is misplaced** — "We adopt the FOO algorithm \cite{bar2023}" but `bar2023` is a survey on graph neural networks. Fix needs claim-vs-abstract comparison.

These are independent: a perfectly cited paper can have a fake bib entry, and a real bib entry can be cited for the wrong claim. Run both tracks; merge findings at the end.

## What you produce

All output goes to `<paper-dir>/runs/citation-check/`. The pipeline never modifies `.bib` or `.tex`.

**重要：所有报告和中间产物必须使用中文**。包括 report.md 的标题、说明、建议修复文案，以及所有 finding 的 description/message/rationale/suggested_fix 字段。只有技术字段名（如 `A4_unresolved`、`OFF_TOPIC`、`STRONG_SUPPORT` 等 verdict code）和 JSON schema key 保持英文。除非用户明确指定了其他语言。

| Artifact | Purpose | Stage |
|---|---|---|
| `bib_entries.json` | Parsed bib (what the author claims each work is) | 0a |
| `citations.json` | One record per `\cite` site: `{key, command, file, line, char_offset}` | 0a |
| `evidence_pack.json` | Per-key external evidence (abstract, tldr, venue, year, source data) | 0b |
| `unresolved_diagnosis.json` | Diagnosis of unresolved entries (bib errors vs not indexed) | 1 |
| `track_a_findings.json` | Track A intermediate results (deterministic bib-health checks) | 3 |
| `track_b_findings.json` | Track B intermediate results (LLM claim-support checks) | 3 |
| `report.md` | Human-readable, prioritised by severity | 4 |
| `report.json` | Machine-readable, flat list of findings | 4 |
| `evidence_pack.json` | Per-key external evidence (DOI, abstract, venue, year, source provenance) | 0b |
| `report.md` | Human-readable, prioritised by severity | 4 |
| `report.json` | Machine-readable, flat list of findings | 4 |

Cache API responses under `runs/citation-check/.cache/` so re-runs only fetch missing entries.

## Pipeline

### Stage 0a — Parse (deterministic, no network)

Run the bundled extractor:

```bash
python3 .claude/skills/citation-double-check/scripts/stage0a_parse.py <paper-dir>
# override the root if auto-detection picks the wrong file:
python3 .claude/skills/citation-double-check/scripts/stage0a_parse.py <paper-dir> --entry main.tex
```

It writes `bib_entries.json` and `citations.json` into `<paper-dir>/runs/citation-check/`. Requires `bibtexparser`.

**Entry-file detection.** The script picks the LaTeX root by scanning every `*.tex` for both `\documentclass` and `\begin{document}`. If multiple files match, it keeps the one(s) not `\input`/`\include`'d by another candidate; if that's still ambiguous, it errors out and asks for `--entry`. From the root, it follows `\input{...}` / `\include{...}` recursively (LaTeX comments are stripped first so commented-out `\input` lines like `%\input{theory}` are ignored). Bib paths come from `\bibliography{...}` / `\addbibresource{...}` in the included tree; if none are found it falls back to every `*.bib` in the paper dir.

**bib_entries.json** — keyed by citation key. Fields: `type`, `title`, `authors` (list), `venue` (booktitle/journal/howpublished/publisher fallback), `year`, plus optional `doi`, `url`, `pages`, `journal`, `booktitle`, and `source_file`.

**citations.json** — list of records. The extractor recognises `\cite`, `\citep`, `\citet`, `\citeauthor`, `\citeyear`, `\parencite`, `\textcite`, `\autocite`, `\footcite`, `\nocite`, and starred/capitalised variants, and emits one record **per key** (so `\cite{a,b,c}` becomes three records). Each record has:

- `key`, `command` (the cite macro used), `file` (relative to paper dir), `line`, `char_offset`

Track B sub-agents read `.tex` files directly to get surrounding context.

### Stage 0b — Fetch evidence (network, cached)

For each unique bib key, query sources in this order. Each source has a different strength; you may need to combine results from multiple sources to fill both metadata and abstract.

#### Source priority

Query order matches `stage0b_fetch_evidence.py`. BibTeX sources (used by Track A for metadata checks) are marked with ★.

| # | Source | Strength | BibTeX? | Auth |
|---|---|---|---|---|
| 1 | **Semantic Scholar** | abstract + tldr + venue + citations | — | `SEMANTIC_SCHOLAR_API_KEY` env var, pass as `x-api-key` header. **Always use the key** — rate limit 1/s → 100/s. |
| 2 | **OpenAlex** | broadest coverage + inverted-index abstract | — | None (mailto for polite pool) |
| 3 | **DBLP** ★ | authoritative venue + author for CS papers | Yes | None |
| 4 | **Crossref** ★ | DOI-authoritative metadata | Yes | None |
| 5 | **arXiv** ★ | CS preprint abstracts | Yes | None |
| 6 | **PubMed** | biomedical papers | — | None |
| 7 | **DataCite** ★ | datasets, software, non-traditional works | Yes | None |
| 8 | **Google Scholar** | broadest fallback (disabled by default, rate-limited) | Yes (rendered) | None |

#### Query strategy

For each bib key:

1. **Semantic Scholar first** — best single source: has abstract, tldr (1-sentence summary, very useful for Track B), venue, year, authors, DOI. If S2 returns a hit with abstract, you may not need other sources.
2. **If S2 misses or has no abstract**, try **OpenAlex** next (broadest coverage, abstract via inverted index).
3. **If S2 venue is ambiguous** (e.g. returns just "arXiv" for a paper that was published at ICLR), query **DBLP** to confirm the formal publication venue. DBLP is the authority for CS conference/journal names — it won't confuse arXiv preprints with their published versions.
4. **Crossref** and **arXiv** as final fallbacks.

Match a returned record to a bib entry only if title-similarity ≥ 0.85 (use `difflib.SequenceMatcher` or token Jaccard). Be conservative: a wrong match poisons every downstream check.

#### evidence_pack.json per key

```json
{
  "resolved": true,
  "sources_used": ["semantic_scholar", "dblp", "crossref", "arxiv"],
  "data": {
    "semantic_scholar": {"title": "...", "authors": [...], "year": 2022, "venue": "...", "doi": "...", "abstract": "...", "tldr": "...", "match_score": 0.95},
    "dblp": {"title": "...", "venue": "...", ...}
  },
  "bibtex": {
    "dblp": "@inproceedings{...}",
    "crossref": "@article{...}",
    "arxiv": "@article{...}"
  },
  "abstract": "...",
  "tldr": "...",
  "abstract_source": "semantic_scholar"
}
```

- `data` — structured metadata per source (JSON/Atom responses)
- `bibtex` — raw BibTeX strings per source (used by Track A for authoritative metadata checks)
- `abstract` / `tldr` — best available abstract and one-line summary (from any source)
- Track A uses `bibtex` (DBLP > Crossref > arXiv > DataCite) for title/year/author/venue checks, falling back to `data` only as last resort

If all sources fail: `{"resolved": false, "sources_used": [], "data": {}, "bibtex": {}, "abstract": null, "tldr": null}`.

### Stage 1 — Diagnose unresolved entries (sub-agent, LLM)

After Stage 0b, check if there are any unresolved entries (`resolved: false`). If so, spawn a sub-agent to diagnose why they failed.

**Key insight:** Stage 0b already queried all sources. The sub-agent's job is NOT to re-query, but to:
1. Check if the bib entry itself has errors (wrong arXiv ID, wrong DOI, title typos)
2. Use LLM knowledge to identify the correct paper
3. Cross-check arXiv ID vs title (e.g., arXiv:2309.06256 is "Mitigating the Alignment Tax of RLHF", not what the bib claims)

#### Sub-agent prompt

```
你是一位学术文献诊断专家。这些 bib 条目在 Stage 0b 中未能解析。请诊断原因。

## 诊断策略

1. **arXiv ID 交叉验证** — 如果 bib 有 arXiv ID，用 WebFetch 查询 arxiv.org/abs/<id>，验证标题是否匹配
2. **DOI 交叉验证** — 如果 bib 有 DOI，用 WebFetch 查询 doi.org/<doi>，验证标题是否匹配
3. **LLM 知识判断** — 如果是知名论文（如 GPT-5、LLaMA），用你的知识判断 bib 信息是否正确
4. **标题拼写检查** — 检查标题是否有明显错误（常见：连字符、大小写、复数、冠词）

## 输入

读取：
- `{paper_dir}/runs/citation-check/bib_entries.json` — 获取 unresolved 条目
- `{paper_dir}/runs/citation-check/evidence_pack.json` — 找出 resolved: false 的 key

## 输出

```json
[
  {
    "key": "xxx",
    "diagnosis": "bib_error|not_indexed|unknown",
    "issue": "arXiv ID 不匹配标题|DOI 错误|标题拼写错误|论文未被索引|原因不明",
    "evidence": "arXiv:2309.06256 的实际标题是 'Mitigating the Alignment Tax of RLHF'",
    "correct_info": {"title": "...", "arxiv_id": "...", "doi": "..."}
  }
]
```

将结果保存到 `{paper_dir}/runs/citation-check/unresolved_diagnosis.json`。
```

如果 sub-agent 发现 bib 条目有错误（`bib_error`），在 Track A 中生成 `A9_bib_entry_error` finding（severity: critical）。

### Track A — Entry health (deterministic, no LLM)

Compare `bib_entries.json` vs `evidence_pack.json` and emit findings:

| Code | Severity | Trigger |
|---|---|---|
| `A1_undefined_key` | critical | `\cite{X}` but X missing from bib |
| `A2_unused_entry` | cleanup | bib has entry never cited |
| `A3_duplicate_entry` | warning | two keys, fuzzy title-similarity ≥ 0.95, same year |
| `A4_unresolved` | critical | All sources missed — entry may be fabricated or has severe typos. **Exception:** if the bib entry's venue is a non-traditional source (Blog, GitHub, tech report, etc.), downgrade to warning — these are expected to be absent from academic APIs. |
| `A5_metadata_mismatch` | warning | bib title vs canonical similarity < 0.85, OR `|year_bib − year_canonical| ≥ 1`, OR first-author surname mismatch |
| `A6_field_missing` | cleanup | required or recommended fields absent — see BibTeX field reference below |
| `A7_venue_mismatch` | cleanup | bib venue ≠ canonical venue **and** the canonical venue comes from DBLP (which distinguishes published vs preprint). If the mismatch is only "arXiv vs published conference" from a non-DBLP source, suppress it — this is normal dual-publication, not an error. |
| `A8_venue_style` | cleanup | bib venue and DBLP are equivalent but written differently (abbreviation vs full name, e.g. `TMLR` vs `Trans. Mach. Learn. Res.`). Not an error, but suggests format unification. |
| `A9_bib_entry_error` | critical | Stage 1 sub-agent detected a bib entry error: arXiv ID doesn't match title, DOI is wrong, or title has severe typos. |

A1, A4 (for traditional venues), A5, and A9 remain the camera-ready killers. Surface them first.

#### BibTeX field reference (A6 check standard)

A well-formed bib entry should carry all the fields listed below. **Required** fields missing → cleanup finding. **Recommended** fields missing → cleanup, only reported when external source has the field data.

| Type | Required | Recommended |
|---|---|---|
| `@article` | `title`, `author`, `journal`, `year` | `volume`, `number`, `pages` |
| `@inproceedings` | `title`, `author`, `booktitle`, `year` | `pages` |
| `@misc` / `@online` | `title`, `author`, `year` | `url` or `eprint` |
| `@book` | `author`/`editor`, `title`, `publisher`, `year` | — |
| `@incollection` | `title`, `author`, `booktitle`, `year` | `publisher`, `pages` |
| `@phdthesis` / `@mastersthesis` | `title`, `author`, `school`, `year` | — |
| `@techreport` | `title`, `author`, `institution`, `year` | — |

Notes:
- `author/editor`: either suffices for `@book` / `@proceedings`.
- `howpublished` can substitute for `url` in `@misc`.
- Additional types in code: `@conference` (= `@inproceedings`), `@inbook`, `@manual`, `@unpublished`, `@proceedings`.

### Track B — Claim support (LLM, abstract-only)

2 个子 agent 并行，各处理一半 cite sites（按 .tex 文件分组），各自读全文批量检查。
第 3 个 summary agent 合并两份结果，生成最终 `track_b_findings.json`。

#### 主 agent 准备

1. 读 `citations.json`，按 `.tex` 文件分组，平均分成两批（尽量让同一文件的 cite sites 在同一批）。
2. 读 `evidence_pack.json`，提取每个 key 的 `title`、`venue`、`year`、`abstract`、`tldr`（来自 `data.semantic_scholar` 或 `data.openalex`）。
3. 跳过 `evidence_pack` 中 `resolved: false` 的 key（无摘要可查）。
4. 将两批 cite sites + 对应 evidence 打包，分别传给两个子 agent 并行启动。

#### 子 agent 输入格式

```
paper_dir: /path/to/paper
cite_sites:
  - key: xxx
    file: introduction.tex
    line: 42
    cited_title: ...
    cited_abstract: ...
    cited_tldr: ...
    cited_venue: ...
    cited_year: 2023
  - key: yyy
    ...
```

#### 子 agent prompt

```
你是一位学术论文引用复核专家。请对以下引文列表逐条检查，判断每条引文是否正确地支持了其周围的论述。

## 任务

1. 按文件分组，用 Read 工具读取 paper_dir 下的每个 .tex 文件（关注 cite sites 对应的行附近，前后各 15 行）。
2. 对每个 cite site 执行 B1→B2→B3 流程。
3. 将所有结果以 JSON 数组形式返回（每个 cite site 一个 JSON 对象）。

## 引文列表

{cite_sites 列表，格式见上方}

## B1 — 分类引文类型

根据引文所在句子的语境，将该引文归为以下类型之一：

- `methodological` — 引用某方法/算法/框架作为本文的基础或对比基准
  （例："we adopt the FOO algorithm \cite{Y}", "following \cite{Y}, we ..."）
- `empirical` — 引用某实验结果/性能数据
  （例："X achieves Z on T \cite{Y}"）
- `definitional` — 引用某概念/术语的定义来源
  （例："X is defined as ... \cite{Y}"）
- `acknowledgment` — 在相关工作中的泛泛列举，没有对被引论文做具体声明
  （例："recent work \cite{A,B,C} has explored ..."）

## B2 — acknowledgment 快捷路径

如果 B1 分类为 `acknowledgment`，且被引论文标题的主题词与引文所在段落的主题有明显重叠，
直接判定为 `WEAK_SUPPORT`，跳过 B3。

## B3 — 逐条比对

将引文所在句子的声明与被引论文摘要（及 TL;DR）进行比对，判定：

| Verdict | 条件 | Severity |
|---|---|---|
| `STRONG_SUPPORT` | 摘要明确包含所声明的内容 | （不列入报告） |
| `WEAK_SUPPORT` | 主题相关，但摘要中看不到具体声明的内容 | cleanup |
| `OFF_TOPIC` | 摘要主题与声明完全无关 | critical |
| `WRONG_DIRECTION` | 摘要内容与声明的立场相矛盾 | critical |
| `INSUFFICIENT_DATA` | 摘要过短或缺失，无法判断 | warning |

**知识回退**：当摘要缺失但被引论文是知名工作时，使用参数知识判断并标记 `"knowledge_based": true`。
仅当论文确实冷门且你对其不了解时才使用 `INSUFFICIENT_DATA`。

## 输出格式

严格返回 JSON 数组，每个 cite site 一个对象，不要输出任何其他内容：

```json
[
  {
    "key": "xxx",
    "claim_type": "methodological|empirical|definitional|acknowledgment",
    "claim_text": "引文所在句子的核心声明（中文概括）",
    "verdict": "STRONG_SUPPORT|WEAK_SUPPORT|OFF_TOPIC|WRONG_DIRECTION|INSUFFICIENT_DATA",
    "knowledge_based": false,
    "rationale": "判断依据（中文，1-2 句）",
    "suggested_fix": "仅对 OFF_TOPIC 和 WRONG_DIRECTION 给出修复建议，其他为 null"
  }
]
```

## 注意事项

- 摘要级别的检查**无法证明** STRONG_SUPPORT，只能可靠地发现 OFF_TOPIC 和 WRONG_DIRECTION。
- 不要因为摘要缺少细节就判 OFF_TOPIC——如果主题一致，应判 WEAK_SUPPORT。
- acknowledgment 类型的引文不需要精细检查，B2 快捷路径即可。
- 对每个 cite site 独立判断，不要因为多条 cite 共享同一段落而互相影响。
```

#### Summary agent

主 agent 收集两个子 agent 的 JSON 数组结果后，派 summary agent 合并：

```
请将以下两个 Track B 检查结果合并为一个 JSON 文件。

## 输入

batch_1: {子 agent 1 返回的 JSON 数组}
batch_2: {子 agent 2 返回的 JSON 数组}

## 输出格式

```json
{
  "summary": {
    "total_cite_sites": N,
    "STRONG_SUPPORT": N,
    "WEAK_SUPPORT": N,
    "OFF_TOPIC": N,
    "WRONG_DIRECTION": N,
    "INSUFFICIENT_DATA": N,
    "knowledge_based": N
  },
  "findings": [
    {
      "code": "B_OFF_TOPIC",
      "severity": "critical",
      "key": "xxx",
      "claim_type": "...",
      "claim_text": "...",
      "verdict": "OFF_TOPIC",
      "knowledge_based": false,
      "rationale": "...",
      "suggested_fix": "..."
    }
  ]
}
```

规则：
- STRONG_SUPPORT 的条目不列入 findings。
- 其余条目全部列入，按 severity 排序：critical > warning > cleanup。
- severity 映射：OFF_TOPIC/WRONG_DIRECTION → critical，INSUFFICIENT_DATA → warning，
  WEAK_SUPPORT → cleanup，WEAK_SUPPORT + knowledge_based → warning。
```

summary agent 将合并结果写入 `{paper_dir}/runs/citation-check/track_b_findings.json`。
| WEAK_SUPPORT + knowledge_based | warning | 🟠（注明"基于模型知识"） |

**Honesty constraint.** Abstract-level checks **cannot prove** STRONG_SUPPORT — only the negative cases (OFF_TOPIC, WRONG_DIRECTION) are reliable signals. Put this disclaimer at the top of every `report.md`. Users who want strong claims need full-text retrieval, which is out of scope for this skill.

### Stage 4 — Report

Merge Track A + Track B into a sorted, severity-grouped report.

`report.md` structure:

```markdown
# Citation Check Report — <paper title or dir name>

> **Honesty disclaimer**: All "support" verdicts are based on abstracts (or LLM knowledge where abstracts are missing).
> A `STRONG_SUPPORT` verdict cannot guarantee the cited paper actually contains
> the claim — only the negative verdicts (OFF_TOPIC, WRONG_DIRECTION) are
> reliable. Verdicts marked `knowledge_based` use the LLM's parametric knowledge
> rather than the paper's own abstract. For deeper checks, retrieve full text manually.

## Summary
- N bib entries, M citation sites
- Track A: <c> critical, <w> warnings, <l> cleanup
- Track B: <off> OFF_TOPIC, <wrong> WRONG_DIRECTION, <ins> INSUFFICIENT_DATA, <kb> knowledge_based

## 🔴 Critical
### [A1_undefined_key] `xxx2024foo`
  cited at: method.tex:42 — "we adopt the FOO algorithm \cite{xxx2024foo}"
  suggested fix: add the missing bib entry

### [A4_unresolved] `yyy2024bar`
  bib title: "..."
  evidence: not found in Semantic Scholar / DBLP / OpenAlex / Crossref / arXiv
  suggested fix: check key spelling; if intentional, replace with a real reference

### [B_OFF_TOPIC] `bar2023` cited at theory.tex:88
  claim: "X converges in O(n log n) \cite{bar2023}"
  cited paper: "A survey of GNN applications in biology" (NeurIPS 2023)
  rationale: the abstract describes biology applications, not convergence rates
  suggested fix: locate the actual reference for the convergence claim, or remove the cite

## 🟠 Warnings
...

## 🟡 Cleanup
...
```

`report.json` is the same content but flat:

```json
{
  "summary": {"entries": 60, "citation_sites": 134, "critical": 3, "warning": 5, "cleanup": 8},
  "findings": [
    {"code": "A4_unresolved", "severity": "critical", "key": "...", "details": {...}},
    ...
  ]
}
```

## Decision rubric

When in doubt during implementation:

- **Title-similarity threshold for matching**: 0.85 token-set ratio. Lower → false matches; higher → false negatives. Tune only if eval shows a problem.
- **What counts as "the sentence" for a cite**: the period-delimited sentence containing the `\cite{}`, plus the immediately preceding and following sentences for context.
- **What to do when an arXiv preprint exists with the same title as a published version**: prefer DBLP or the published venue for canonical metadata, but use either source's abstract.
- **Whether to LLM-judge `acknowledgment` cites**: don't. Skip B3, emit `WEAK_SUPPORT`. They produce false positives because abstracts of broad citations rarely match individual sub-claims.
- **Non-traditional venues (Blog, GitHub, tech report)**: A4_unresolved should be warning, not critical — these sources are expected to be absent from academic APIs.
- **arXiv vs published venue mismatch**: A7 should only fire when DBLP confirms a genuine venue error (e.g. bib claims ICLR but DBLP shows it was only a workshop paper). Mere "arXiv vs NeurIPS" dual-publication is not an error — suppress it.
- **What model to use**: Claude Sonnet 4.6 by default. Track B is the only LLM-bound step; the rest is deterministic.
- **Semantic Scholar API key**: read from `$SEMANTIC_SCHOLAR_API_KEY`. Always pass it as `x-api-key` header when available. Without it you're limited to 1 req/s; with it, 100 req/s.

## When NOT to use this skill

- Bib **formatting** or style conversion → `citation-management` / `ars-citation-check`
- Suggesting **missing references** to add → out of scope, this skill only checks existing cites
- **Full-text** verification → out of scope; abstract-level only

## Quick start

```bash
PAPER=~/path/to/paper-with-tex-and-bib

# the skill provides a recipe, not a script — implement each stage as you go
# minimum end state: PAPER/runs/citation-check/report.md exists and follows the schema above
```

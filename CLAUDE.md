# Citation Double-Check

开发一个 Claude Code skill：给定一个 LaTeX 论文项目，自动检测两类引用错误——硬错误（bib 条目本身有问题）和软错误（引文与周围论述不匹配），在提交前拦截最伤论文的引用问题。

## 已完成

- [x] Stage 0a: 解析 .tex 和 .bib
- [x] Stage 0b: 多源证据获取（Semantic Scholar, OpenAlex, DBLP, Crossref, arXiv, PubMed, DataCite）
- [x] Stage 1: 诊断 unresolved 条目（arXiv ID 交叉验证、LLM 知识判断）
- [x] Track A: 硬错误检查（A1-A9）
- [x] Track B: 软错误检查（2 并行子 agent + 1 summary agent）
- [x] Stage 4: 报告生成（report.md + report.json）
- [x] 修复 arXiv 年份比较逻辑（使用 atom:published 而非 BibTeX year）
- [x] 修复年份冲突逻辑（只用 DBLP/Crossref/DataCite BibTeX year，忽略 arXiv）
- [x] Crossref 质量检查（venue 为空 + DOI 前缀 10.65215 → 忽略）
- [x] DBLP 搜索优化（标题搜索失败时，用 surname + 关键词搜索）

## 待完成

- [ ] 更多测试用例（A3/A6/A7/A8 边界情况）
- [ ] 完善错误处理和边界情况
- [ ] 性能优化（缓存、并行查询）

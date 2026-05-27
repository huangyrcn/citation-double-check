import importlib.util
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class CitationCheckRepairTests(unittest.TestCase):
    def run_stage0a(self, paper_dir: Path):
        subprocess.run(
            [sys.executable, str(ROOT / "skills/citation-double-check/scripts/stage0a_parse.py"), str(paper_dir)],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

    def run_track_a(self, paper_dir: Path):
        subprocess.run(
            [sys.executable, str(ROOT / "skills/citation-double-check/scripts/track_a_check.py"), str(paper_dir)],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        with (paper_dir / "runs/citation-check/track_a_findings.json").open(encoding="utf-8") as f:
            return json.load(f)

    def write_minimal_paper(self, paper_dir: Path, bib: str, cite_key: str = "paper"):
        (paper_dir / "main.tex").write_text(
            "\\documentclass{article}\n"
            "\\begin{document}\n"
            f"Claim \\cite{{{cite_key}}}.\n"
            "\\bibliography{refs}\n"
            "\\end{document}\n",
            encoding="utf-8",
        )
        (paper_dir / "refs.bib").write_text(bib, encoding="utf-8")

    def test_stage0a_preserves_fields_needed_by_a6(self):
        with tempfile.TemporaryDirectory() as td:
            paper_dir = Path(td)
            self.write_minimal_paper(
                paper_dir,
                """
@book{paper,
  title={A Book},
  author={Doe, Jane},
  editor={Smith, John},
  publisher={Example Press},
  year={2020},
  volume={4},
  number={2},
  eprint={2001.00001},
  howpublished={Online},
  note={Accepted}
}
""",
            )

            self.run_stage0a(paper_dir)

            with (paper_dir / "runs/citation-check/bib_entries.json").open(encoding="utf-8") as f:
                entry = json.load(f)["paper"]
            for field in ["editor", "publisher", "volume", "number", "eprint", "howpublished", "note"]:
                self.assertIn(field, entry)
                self.assertTrue(entry[field])

    def test_a4_reports_key_missing_from_evidence_pack(self):
        track_a = load_module("track_a_check", ROOT / "skills/citation-double-check/scripts/track_a_check.py")

        findings = track_a.check_a4(
            {"paper"},
            {"paper": {"title": "A Real Paper", "venue": "Journal", "year": "2024"}},
            {},
        )

        self.assertEqual(1, len(findings))
        self.assertEqual("A4_unresolved", findings[0]["code"])
        self.assertEqual("critical", findings[0]["severity"])

    def test_stage0b_only_preserves_existing_evidence_pack_entries(self):
        stage0b = load_module("stage0b_fetch_evidence", ROOT / "skills/citation-double-check/scripts/stage0b_fetch_evidence.py")
        with tempfile.TemporaryDirectory() as td:
            paper_dir = Path(td)
            out_dir = paper_dir / "runs/citation-check"
            out_dir.mkdir(parents=True)
            (out_dir / "bib_entries.json").write_text(
                json.dumps({
                    "refresh": {"title": "Refresh Me"},
                    "keep": {"title": "Keep Me"},
                }),
                encoding="utf-8",
            )
            (out_dir / "citations.json").write_text(
                json.dumps([{"key": "refresh"}, {"key": "keep"}]),
                encoding="utf-8",
            )
            (out_dir / "evidence_pack.json").write_text(
                json.dumps({"keep": {"resolved": True, "sentinel": "old"}}),
                encoding="utf-8",
            )
            fresh = {"resolved": True, "sources_used": [], "data": {}, "bibtex": {}, "abstract": "", "tldr": ""}

            with mock.patch.object(stage0b, "resolve_key", return_value=fresh), \
                 mock.patch.object(sys, "argv", ["stage0b_fetch_evidence.py", str(paper_dir), "--only", "refresh"]), \
                 mock.patch("sys.stdout", new=io.StringIO()):
                stage0b.main()

            with (out_dir / "evidence_pack.json").open(encoding="utf-8") as f:
                pack = json.load(f)
            self.assertEqual({"refresh", "keep"}, set(pack))
            self.assertEqual("old", pack["keep"]["sentinel"])

    def test_stage0b_only_removes_stale_keys(self):
        stage0b = load_module("stage0b_fetch_evidence", ROOT / "skills/citation-double-check/scripts/stage0b_fetch_evidence.py")
        with tempfile.TemporaryDirectory() as td:
            paper_dir = Path(td)
            out_dir = paper_dir / "runs/citation-check"
            out_dir.mkdir(parents=True)
            (out_dir / "bib_entries.json").write_text(
                json.dumps({"current": {"title": "Current Paper"}}),
                encoding="utf-8",
            )
            (out_dir / "citations.json").write_text(
                json.dumps([{"key": "current"}]),
                encoding="utf-8",
            )
            (out_dir / "evidence_pack.json").write_text(
                json.dumps({
                    "current": {"resolved": True, "sentinel": "old"},
                    "stale_removed": {"resolved": True, "sentinel": "stale"},
                }),
                encoding="utf-8",
            )
            fresh = {"resolved": True, "sources_used": [], "data": {}, "bibtex": {}, "abstract": "", "tldr": ""}

            with mock.patch.object(stage0b, "resolve_key", return_value=fresh), \
                 mock.patch.object(sys, "argv", ["stage0b_fetch_evidence.py", str(paper_dir), "--only", "current"]), \
                 mock.patch("sys.stdout", new=io.StringIO()):
                stage0b.main()

            with (out_dir / "evidence_pack.json").open(encoding="utf-8") as f:
                pack = json.load(f)
            self.assertEqual({"current"}, set(pack))
            self.assertNotIn("stale_removed", pack)

    def test_a4_downgrades_url_only_non_traditional_sources(self):
        track_a = load_module("track_a_check", ROOT / "skills/citation-double-check/scripts/track_a_check.py")

        findings = track_a.check_a4(
            {"repo"},
            {
                "repo": {
                    "type": "misc",
                    "title": "Project Repository",
                    "url": "https://github.com/example/project",
                    "year": "2024",
                }
            },
            {"repo": {"resolved": False, "sources_used": [], "data": {}, "bibtex": {}}},
        )

        self.assertEqual(1, len(findings))
        self.assertEqual("warning", findings[0]["severity"])
        self.assertTrue(findings[0]["details"]["non_traditional_venue"])

    def test_a4_keeps_traditional_paper_with_repository_url_critical(self):
        track_a = load_module("track_a_check", ROOT / "skills/citation-double-check/scripts/track_a_check.py")

        findings = track_a.check_a4(
            {"paper"},
            {
                "paper": {
                    "type": "inproceedings",
                    "title": "Misspelled Conference Paper",
                    "booktitle": "International Conference on Learning Representations",
                    "venue": "International Conference on Learning Representations",
                    "url": "https://github.com/example/project",
                    "year": "2024",
                }
            },
            {"paper": {"resolved": False, "sources_used": [], "data": {}, "bibtex": {}}},
        )

        self.assertEqual(1, len(findings))
        self.assertEqual("critical", findings[0]["severity"])
        self.assertFalse(findings[0]["details"]["non_traditional_venue"])

    def test_a4_keeps_traditional_paper_with_howpublished_repository_url_critical(self):
        track_a = load_module("track_a_check", ROOT / "skills/citation-double-check/scripts/track_a_check.py")

        findings = track_a.check_a4(
            {"paper"},
            {
                "paper": {
                    "type": "inproceedings",
                    "title": "Misspelled Conference Paper",
                    "booktitle": "International Conference on Learning Representations",
                    "venue": "International Conference on Learning Representations",
                    "howpublished": "\\url{https://github.com/example/project}",
                    "year": "2024",
                }
            },
            {"paper": {"resolved": False, "sources_used": [], "data": {}, "bibtex": {}}},
        )

        self.assertEqual(1, len(findings))
        self.assertEqual("critical", findings[0]["severity"])
        self.assertFalse(findings[0]["details"]["non_traditional_venue"])

    def test_a4_keeps_article_note_online_first_critical(self):
        track_a = load_module("track_a_check", ROOT / "skills/citation-double-check/scripts/track_a_check.py")

        findings = track_a.check_a4(
            {"paper"},
            {
                "paper": {
                    "type": "article",
                    "title": "Ordinary Journal Paper",
                    "journal": "Journal of Normal Research",
                    "venue": "Journal of Normal Research",
                    "note": "Online first",
                    "year": "2024",
                }
            },
            {"paper": {"resolved": False, "sources_used": [], "data": {}, "bibtex": {}}},
        )

        self.assertEqual(1, len(findings))
        self.assertEqual("critical", findings[0]["severity"])
        self.assertFalse(findings[0]["details"]["non_traditional_venue"])

    def test_a4_keeps_traditional_software_venue_critical(self):
        track_a = load_module("track_a_check", ROOT / "skills/citation-double-check/scripts/track_a_check.py")

        for venue in ["International Conference on Software Engineering", "IEEE Software"]:
            with self.subTest(venue=venue):
                findings = track_a.check_a4(
                    {"paper"},
                    {
                        "paper": {
                            "type": "inproceedings",
                            "title": "Misspelled Traditional Paper",
                            "booktitle": venue,
                            "venue": venue,
                            "year": "2024",
                        }
                    },
                    {"paper": {"resolved": False, "sources_used": [], "data": {}, "bibtex": {}}},
                )

                self.assertEqual(1, len(findings))
                self.assertEqual("critical", findings[0]["severity"])
                self.assertFalse(findings[0]["details"]["non_traditional_venue"])

    def test_a4_downgrades_note_only_model_card(self):
        track_a = load_module("track_a_check", ROOT / "skills/citation-double-check/scripts/track_a_check.py")

        findings = track_a.check_a4(
            {"model_card"},
            {
                "model_card": {
                    "type": "misc",
                    "title": "Example Model Card",
                    "note": "Model card",
                    "year": "2024",
                }
            },
            {"model_card": {"resolved": False, "sources_used": [], "data": {}, "bibtex": {}}},
        )

        self.assertEqual(1, len(findings))
        self.assertEqual("warning", findings[0]["severity"])
        self.assertTrue(findings[0]["details"]["non_traditional_venue"])

    def test_a4_downgrades_note_blog(self):
        track_a = load_module("track_a_check", ROOT / "skills/citation-double-check/scripts/track_a_check.py")

        findings = track_a.check_a4(
            {"blog"},
            {
                "blog": {
                    "type": "misc",
                    "title": "Blog Post About Method",
                    "note": "Blog post describing the method",
                    "year": "2024",
                }
            },
            {"blog": {"resolved": False, "sources_used": [], "data": {}, "bibtex": {}}},
        )

        self.assertEqual(1, len(findings))
        self.assertEqual("warning", findings[0]["severity"])
        self.assertTrue(findings[0]["details"]["non_traditional_venue"])

    def test_a7_suppresses_arxiv_bib_when_dblp_has_published_venue(self):
        track_a = load_module("track_a_check", ROOT / "skills/citation-double-check/scripts/track_a_check.py")

        findings = track_a.check_a7(
            {"gcn"},
            {
                "gcn": {
                    "title": "Semi-Supervised Classification with Graph Convolutional Networks",
                    "venue": "arXiv preprint arXiv:1609.02907",
                    "journal": "arXiv preprint arXiv:1609.02907",
                }
            },
            {
                "gcn": {
                    "resolved": True,
                    "bibtex": {
                        "dblp": "@inproceedings{gcn,title={Semi-Supervised Classification with Graph Convolutional Networks},author={Kipf, Thomas N. and Welling, Max},booktitle={International Conference on Learning Representations},year={2017}}"
                    },
                    "data": {},
                }
            },
        )

        self.assertEqual([], findings)

    def test_a7_suppresses_equivalent_arxiv_corr_variants(self):
        track_a = load_module("track_a_check", ROOT / "skills/citation-double-check/scripts/track_a_check.py")

        findings = track_a.check_a7(
            {"paper"},
            {"paper": {"venue": "arXiv preprint arXiv:2401.00001"}},
            {
                "paper": {
                    "resolved": True,
                    "bibtex": {
                        "dblp": "@article{paper,title={T},author={Doe, Jane},journal={CoRR abs/2401.00001},year={2024}}"
                    },
                    "data": {},
                }
            },
        )

        self.assertEqual([], findings)

    def test_a7_suppresses_arxiv_eprints_as_pure_preprint(self):
        track_a = load_module("track_a_check", ROOT / "skills/citation-double-check/scripts/track_a_check.py")

        findings = track_a.check_a7(
            {"paper"},
            {"paper": {"journal": "arXiv e-prints"}},
            {
                "paper": {
                    "resolved": True,
                    "bibtex": {
                        "dblp": "@inproceedings{paper,title={T},author={Doe, Jane},booktitle={International Conference on Learning Representations},year={2024}}"
                    },
                    "data": {},
                }
            },
        )

        self.assertEqual([], findings)

    def test_a7_suppresses_arxiv_url_as_pure_preprint(self):
        track_a = load_module("track_a_check", ROOT / "skills/citation-double-check/scripts/track_a_check.py")

        for value in ["\\url{https://arxiv.org/abs/2401.00001}", "\\urlhttps://arxiv.org/abs/2401.00001"]:
            with self.subTest(value=value):
                findings = track_a.check_a7(
                    {"paper"},
                    {"paper": {"howpublished": value}},
                    {
                        "paper": {
                            "resolved": True,
                            "bibtex": {
                                "dblp": "@inproceedings{paper,title={T},author={Doe, Jane},booktitle={International Conference on Learning Representations},year={2024}}"
                            },
                            "data": {},
                        }
                    },
                )

                self.assertEqual([], findings)

    def test_a7_reports_mixed_arxiv_formal_venue_against_corr(self):
        track_a = load_module("track_a_check", ROOT / "skills/citation-double-check/scripts/track_a_check.py")

        findings = track_a.check_a7(
            {"paper"},
            {"paper": {"booktitle": "arXiv preprint; Proceedings of KDD"}},
            {
                "paper": {
                    "resolved": True,
                    "bibtex": {
                        "dblp": "@article{paper,title={T},author={Doe, Jane},journal={CoRR abs/2401.00001},year={2024}}"
                    },
                    "data": {},
                }
            },
        )

        self.assertEqual(1, len(findings))
        self.assertEqual("A7_venue_mismatch", findings[0]["code"])

    def test_a7_reports_mixed_arxiv_formal_venue_mismatch(self):
        track_a = load_module("track_a_check", ROOT / "skills/citation-double-check/scripts/track_a_check.py")

        findings = track_a.check_a7(
            {"paper"},
            {"paper": {"booktitle": "arXiv preprint; Proceedings of KDD"}},
            {
                "paper": {
                    "resolved": True,
                    "bibtex": {
                        "dblp": "@inproceedings{paper,title={T},author={Doe, Jane},booktitle={International Conference on Learning Representations},year={2024}}"
                    },
                    "data": {},
                }
            },
        )

        self.assertEqual(1, len(findings))
        self.assertEqual("A7_venue_mismatch", findings[0]["code"])

    def test_a7_suppresses_old_format_arxiv_id(self):
        track_a = load_module("track_a_check", ROOT / "skills/citation-double-check/scripts/track_a_check.py")

        findings = track_a.check_a7(
            {"paper"},
            {"paper": {"venue": "arXiv:hep-ph/0601001"}},
            {
                "paper": {
                    "resolved": True,
                    "bibtex": {
                        "dblp": "@article{paper,title={T},author={Doe, Jane},journal={Phys. Rev. D},year={2006}}"
                    },
                    "data": {},
                }
            },
        )

        self.assertEqual([], findings)

    def test_a9_reports_stage1_bib_errors(self):
        track_a = load_module("track_a_check", ROOT / "skills/citation-double-check/scripts/track_a_check.py")

        findings = track_a.check_a9(
            [
                {
                    "key": "wrong_arxiv",
                    "diagnosis": "bib_error",
                    "issue": "arXiv ID 不匹配标题",
                    "evidence": "arXiv:2309.06256 的实际标题是 Mitigating the Alignment Tax of RLHF",
                    "correct_info": {"title": "Mitigating the Alignment Tax of RLHF"},
                },
                {"key": "not_indexed", "diagnosis": "not_indexed", "issue": "论文未被索引"},
            ],
            current_keys={"wrong_arxiv", "not_indexed"},
        )

        self.assertEqual(1, len(findings))
        self.assertEqual("A9_bib_entry_error", findings[0]["code"])
        self.assertEqual("critical", findings[0]["severity"])
        self.assertEqual("wrong_arxiv", findings[0]["key"])

    def test_a9_ignores_stale_diagnosis_keys(self):
        track_a = load_module("track_a_check", ROOT / "skills/citation-double-check/scripts/track_a_check.py")

        findings = track_a.check_a9(
            [{"key": "old_key", "diagnosis": "bib_error", "issue": "旧诊断"}],
            current_keys={"current_key"},
        )

        self.assertEqual([], findings)

    def test_a9_accepts_object_wrapped_diagnosis_file(self):
        track_a = load_module("track_a_check", ROOT / "skills/citation-double-check/scripts/track_a_check.py")

        findings = track_a.check_a9(
            {"diagnoses": [{"key": "paper", "diagnosis": "bib_error", "issue": "DOI 错误"}]},
            current_keys={"paper"},
        )

        self.assertEqual(1, len(findings))
        self.assertEqual("paper", findings[0]["key"])

    def test_track_a_main_includes_a9_from_unresolved_diagnosis_file(self):
        with tempfile.TemporaryDirectory() as td:
            paper_dir = Path(td)
            self.write_minimal_paper(
                paper_dir,
                """
@article{paper,
  title={Wrong Identity},
  author={Doe, Jane},
  journal={Journal},
  year={2024}
}
""",
            )
            self.run_stage0a(paper_dir)
            out_dir = paper_dir / "runs/citation-check"
            (out_dir / "evidence_pack.json").write_text(
                json.dumps({"paper": {"resolved": False, "sources_used": [], "data": {}, "bibtex": {}}}),
                encoding="utf-8",
            )
            (out_dir / "unresolved_diagnosis.json").write_text(
                json.dumps([
                    {
                        "key": "paper",
                        "diagnosis": "bib_error",
                        "issue": "DOI 指向另一篇论文",
                        "evidence": "DOI 返回的标题不同",
                        "correct_info": {"title": "Correct Paper"},
                    }
                ], ensure_ascii=False),
                encoding="utf-8",
            )

            result = self.run_track_a(paper_dir)

            codes = [f["code"] for f in result["findings"]]
            self.assertIn("A9_bib_entry_error", codes)

    def test_track_a_main_ignores_stale_a9_from_unresolved_diagnosis_file(self):
        with tempfile.TemporaryDirectory() as td:
            paper_dir = Path(td)
            self.write_minimal_paper(
                paper_dir,
                """
@article{paper,
  title={Current Paper},
  author={Doe, Jane},
  journal={Journal},
  year={2024}
}
""",
            )
            self.run_stage0a(paper_dir)
            out_dir = paper_dir / "runs/citation-check"
            (out_dir / "evidence_pack.json").write_text(
                json.dumps({"paper": {"resolved": False, "sources_used": [], "data": {}, "bibtex": {}}}),
                encoding="utf-8",
            )
            (out_dir / "unresolved_diagnosis.json").write_text(
                json.dumps([{"key": "old_key", "diagnosis": "bib_error", "issue": "旧诊断"}], ensure_ascii=False),
                encoding="utf-8",
            )

            result = self.run_track_a(paper_dir)

            self.assertNotIn("A9_bib_entry_error", [f["code"] for f in result["findings"]])

    def test_misc_howpublished_satisfies_url_recommendation(self):
        track_a = load_module("track_a_check", ROOT / "skills/citation-double-check/scripts/track_a_check.py")

        findings = track_a.check_a6(
            {
                "manual": {
                    "type": "misc",
                    "title": "Manual",
                    "authors": ["Doe, Jane"],
                    "author": "x",
                    "year": "2024",
                    "howpublished": "\\url{https://example.com}",
                }
            },
            {
                "manual": {
                    "resolved": True,
                    "bibtex": {
                        "crossref": "@misc{manual,title={Manual},author={Doe, Jane},year={2024},url={https://example.com}}"
                    },
                }
            },
        )

        self.assertEqual([], [f for f in findings if f["code"] == "A6_field_missing"])

    def test_stage0b_and_track_a_title_normalization_match(self):
        track_a = load_module("track_a_check", ROOT / "skills/citation-double-check/scripts/track_a_check.py")
        stage0b = load_module("stage0b_fetch_evidence", ROOT / "skills/citation-double-check/scripts/stage0b_fetch_evidence.py")

        self.assertEqual(track_a.norm_title("Beyoncé’s Spatial-Omics"), stage0b.norm_title("Beyoncé’s Spatial-Omics"))

    def test_subprocess_helpers_use_current_interpreter(self):
        with mock.patch("subprocess.run", return_value=subprocess.CompletedProcess([], 0)) as run:
            self.run_stage0a(Path("/tmp/example-paper"))
        self.assertEqual(sys.executable, run.call_args.args[0][0])

        with tempfile.TemporaryDirectory() as td:
            paper_dir = Path(td)
            out_dir = paper_dir / "runs/citation-check"
            out_dir.mkdir(parents=True)
            (out_dir / "track_a_findings.json").write_text("{}", encoding="utf-8")
            with mock.patch("subprocess.run", return_value=subprocess.CompletedProcess([], 0)) as run:
                self.run_track_a(paper_dir)
            self.assertEqual(sys.executable, run.call_args.args[0][0])

    def test_gitignore_keeps_root_kdd_fixture_ignored(self):
        gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
        self.assertIn("KDD_TRACE_Camera/", gitignore)


if __name__ == "__main__":
    unittest.main()

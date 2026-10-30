from __future__ import annotations

from evals.audit_adversarial import run_all_27_adversarial_probes
from evals.demo_real_file_roundtrip import run_real_file_to_file_mlr_round


def test_all_27_adversarial_audit_probes_pass() -> None:
    results = run_all_27_adversarial_probes()
    assert len(results) == 27
    for probe_key, outcome in results.items():
        assert outcome["passed"] is True, f"Adversarial probe failed: {probe_key} -> {outcome}"


def test_real_file_to_file_docx_pdf_redline_zip_roundtrip() -> None:
    summary = run_real_file_to_file_mlr_round()
    assert summary["passed"] is True
    assert summary["annotated_pdf"]["annotation_count"] == 2
    assert summary["redline_docx"]["openxml_has_w_del"] is True
    assert summary["redline_docx"]["openxml_has_w_ins"] is True
    assert len(summary["resubmission_zip"]["manifest"]["files"]) == 8

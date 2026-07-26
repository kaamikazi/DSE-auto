from __future__ import annotations

import io
import zipfile
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.orm import Session

from app.models import AuthoritativeEvidence
from app.services.governed_research_data import register_dataset
from app.services.public_source_collection import (
    SourceAttempt,
    compare_csv_sources,
    extract_rule_candidates,
    inspect_csv_stream,
    record_schema_preview,
    register_under_review_claims,
    safe_download,
    validate_archive_bytes,
    validate_public_url,
    write_attempt_manifest,
)

CSV = b"symbol,date,open,high,low,close,volume\nGP,2026-01-01,10,12,9,11,100\nACI,2026-01-02,20,22,19,21,200\n"


class _Headers:
    def __init__(self, content_type: str, length: int) -> None:
        self._content_type = content_type
        self._length = length

    def get(self, key: str, default: str | None = None) -> str | None:
        return str(self._length) if key == "Content-Length" else default

    def get_content_type(self) -> str:
        return self._content_type


class _Response(io.BytesIO):
    def __init__(self, raw: bytes, url: str, content_type: str) -> None:
        super().__init__(raw)
        self._url = url
        self.headers = _Headers(content_type, len(raw))

    def geturl(self) -> str:
        return self._url


class _Opener:
    def __init__(self, response: _Response) -> None:
        self.response = response

    def open(self, request: Any, timeout: int) -> _Response:
        assert timeout > 0
        return self.response


def test_safe_download_rejects_unapproved_redirect_and_active_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(ValueError, match="not approved"):
        validate_public_url("https://example.com/data.csv")
    response = _Response(CSV, "https://evil.example/data.csv", "text/csv")
    monkeypatch.setattr(
        "app.services.public_source_collection.urllib.request.build_opener",
        lambda *args: _Opener(response),
    )
    with pytest.raises(ValueError, match="not approved"):
        safe_download("https://dsestocks.com/data.csv", tmp_path / "data.csv")
    response = _Response(
        b"<html><script>x</script></html>", "https://dsestocks.com/x.csv", "text/csv"
    )
    monkeypatch.setattr(
        "app.services.public_source_collection.urllib.request.build_opener",
        lambda *args: _Opener(response),
    )
    with pytest.raises(ValueError, match="active content"):
        safe_download("https://dsestocks.com/x.csv", tmp_path / "x.csv")
    assert not (tmp_path / "x.csv").exists()


def test_safe_download_hash_and_immutable_collision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / "data.csv"
    response = _Response(CSV, "https://dsestocks.com/data.csv", "text/csv")
    monkeypatch.setattr(
        "app.services.public_source_collection.urllib.request.build_opener",
        lambda *args: _Opener(response),
    )
    result = safe_download("https://dsestocks.com/data.csv", destination)
    assert len(result["sha256"]) == 64
    assert destination.read_bytes() == CSV
    response = _Response(CSV + b"extra", "https://dsestocks.com/data.csv", "text/csv")
    with pytest.raises(ValueError, match="collision"):
        safe_download("https://dsestocks.com/data.csv", destination)


def test_archive_manifest_accepts_nested_csv_and_rejects_traversal() -> None:
    safe = io.BytesIO()
    with zipfile.ZipFile(safe, "w") as archive:
        archive.writestr("adjusted/GP.csv", CSV)
        archive.writestr("unadjusted/GP.csv", CSV)
    assert validate_archive_bytes(safe.getvalue())["member_count"] == 2
    unsafe = io.BytesIO()
    with zipfile.ZipFile(unsafe, "w") as archive:
        archive.writestr("../escape.csv", CSV)
    with pytest.raises(ValueError, match="path traversal"):
        validate_archive_bytes(unsafe.getvalue())


def test_schema_preview_is_review_only_and_registry_linked(db: Session, tmp_path: Path) -> None:
    source = tmp_path / "source.csv"
    source.write_bytes(CSV)
    inspection = inspect_csv_stream(source)
    assert inspection["row_count"] == 2
    assert inspection["normalized_mapping"]["symbol"] == "symbol"
    dataset = register_dataset(
        db,
        filename="source.csv",
        raw=CSV,
        raw_dir=tmp_path / "raw",
        source_category="mendeley",
        source_name="public research fixture",
        source_reference="https://data.mendeley.com/datasets/example/1",
        publisher="Fixture publisher",
        license_note="Test fixture only; redistribution prohibited",
        operator="test-collector",
        timestamp_trust="unknown",
        source_trust="third_party_research",
    )
    run = record_schema_preview(db, dataset, inspection)
    assert run.state == "review_required"
    assert run.preview["activated"] is False
    assert dataset.review_status == "registered"


def test_duplicate_detection_and_attempt_manifest(tmp_path: Path) -> None:
    attempt = SourceAttempt(
        source_url="https://dsestocks.com/data.csv",
        publisher="DSE Stocks",
        title="Archive",
        result="downloaded",
        accessed_at="2026-07-26T12:00:00+00:00",
        license_note="Research use only; no redistribution",
        source_trust="third_party_research",
        timestamp_trust="unknown",
        local_path="raw/data.csv",
        file_size=len(CSV),
        mime_type="text/csv",
        sha256="a" * 64,
    )
    path = tmp_path / "manifest.json"
    assert len(write_attempt_manifest([attempt], path)) == 64
    with pytest.raises(ValueError, match="exactly once"):
        write_attempt_manifest([attempt, attempt], path)
    with pytest.raises(ValueError, match="complete file metadata"):
        SourceAttempt(
            source_url="https://dsestocks.com/data.csv",
            publisher="DSE Stocks",
            title="Archive",
            result="downloaded",
            accessed_at="2026-07-26T12:00:00+00:00",
            license_note="Research use only",
            source_trust="third_party_research",
            timestamp_trust="unknown",
        )


def test_cross_source_report_never_averages_values(tmp_path: Path) -> None:
    left = tmp_path / "left.csv"
    right = tmp_path / "right.csv"
    left.write_bytes(CSV)
    right.write_bytes(CSV.replace(b"GP,2026-01-01,10,12,9,11,100", b"GP,2026-01-01,10,12,9,10,100"))
    report = compare_csv_sources(
        left,
        right,
        left_label="left",
        right_label="right",
        output_dir=tmp_path / "reports",
    )
    assert report["overlap_rows"] == 2
    assert report["counts"] == {"material_conflict": 1, "exact_match": 1}
    assert report["prices_averaged"] is False
    assert set(report["output_paths"]) == {"json", "csv", "markdown"}


def test_rule_claims_remain_under_review_and_link_source(db: Session) -> None:
    evidence = AuthoritativeEvidence(
        category="market_rules",
        title="Settlement rules",
        source_organization="Official publisher",
        source_type="official_document",
        source_reference="https://sec.gov.bd/rules.pdf",
        collected_by="test-collector",
        verification_status="submitted",
        original_filename="rules.pdf",
        extraction={"human_verified": False},
    )
    db.add(evidence)
    db.commit()
    candidates = extract_rule_candidates(
        [
            "Settlement shall follow the applicable T+2 business day cycle. Short selling is regulated."
        ]
    )
    claims = register_under_review_claims(
        db,
        evidence,
        candidates,
        source_url=evidence.source_reference,
        actor="test-collector",
    )
    assert {"settlement", "short_selling"} <= {claim.claim_type for claim in claims}
    assert all(claim.reviewer_status == "under_review" for claim in claims)
    assert all(
        claim.normalized_interpretation["source_url"] == evidence.source_reference
        for claim in claims
    )
    assert evidence.verification_status == "under_review"

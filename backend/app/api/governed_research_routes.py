from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.security import require_api_key
from app.models import DatasetImportRun, GovernedDataset
from app.services.governed_research_data import (
    BROKER_QUESTIONS,
    VENDOR_QUESTIONS,
    activate_for_research,
    compare_sources,
    eod_research_workflow,
    preview_import,
    register_dataset,
    rollback_import,
    workspace_summary,
)

router = APIRouter(prefix="/research-data", tags=["research-data"])
Db = Annotated[Session, Depends(get_db)]


@router.get("/summary")
def summary(db: Db) -> dict[str, Any]:
    return workspace_summary(db)


@router.get("/workflow/eod")
def eod_workflow() -> dict[str, Any]:
    return eod_research_workflow()


@router.get("/questionnaires")
def questionnaires() -> dict[str, list[str]]:
    return {"data_vendor": VENDOR_QUESTIONS, "broker": BROKER_QUESTIONS}


@router.post("/datasets", dependencies=[Depends(require_api_key)])
async def create_dataset(
    db: Db,
    file: Annotated[UploadFile, File()],
    source_category: Annotated[str, Form()],
    source_name: Annotated[str, Form()],
    source_reference: Annotated[str, Form()],
    publisher: Annotated[str, Form()],
    license_note: Annotated[str, Form()],
    operator: Annotated[str, Form()],
    timestamp_trust: Annotated[str, Form()],
    source_trust: Annotated[str, Form()],
    adjustment_status: Annotated[str, Form()] = "unknown",
    publication_date: Annotated[str | None, Form()] = None,
    stated_date_coverage: Annotated[str, Form()] = "",
    stated_symbol_coverage: Annotated[str, Form()] = "",
    notes: Annotated[str, Form()] = "",
) -> dict[str, Any]:
    try:
        item = register_dataset(
            db,
            filename=file.filename or "dataset.bin",
            raw=await file.read(),
            raw_dir=Path("../data/research_datasets/raw"),
            source_category=source_category,
            source_name=source_name,
            source_reference=source_reference,
            publisher=publisher,
            license_note=license_note,
            operator=operator,
            timestamp_trust=timestamp_trust,
            source_trust=source_trust,
            adjustment_status=adjustment_status,
            publication_date=date.fromisoformat(publication_date) if publication_date else None,
            stated_date_coverage=stated_date_coverage,
            stated_symbol_coverage=[
                s.strip() for s in stated_symbol_coverage.split(",") if s.strip()
            ],
            notes=notes,
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return {"dataset_id": item.id, "sha256": item.raw_sha256, "activated": False}


@router.post("/datasets/{dataset_id}/preview", dependencies=[Depends(require_api_key)])
def preview(dataset_id: str, payload: dict[str, Any], db: Db) -> dict[str, Any]:
    dataset = db.get(GovernedDataset, dataset_id)
    if dataset is None:
        raise HTTPException(404, "Dataset not found")
    try:
        mapping = payload.get("column_mapping", payload)
        if isinstance(mapping, str):
            mapping = json.loads(mapping)
        run = preview_import(db, dataset, column_mapping=dict(mapping))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(422, str(exc)) from exc
    return {
        "import_run_id": run.id,
        "state": run.state,
        "preview": run.preview,
        "errors": run.errors,
    }


@router.post("/imports/{run_id}/activate-research", dependencies=[Depends(require_api_key)])
def activate(run_id: str, payload: dict[str, str], db: Db) -> dict[str, Any]:
    run = db.get(DatasetImportRun, run_id)
    if run is None:
        raise HTTPException(404, "Import run not found")
    try:
        activate_for_research(
            db,
            run,
            operator=payload.get("operator", "operator"),
            normalized_dir=Path("../data/research_datasets/normalized"),
        )
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"id": run.id, "state": run.state, "research_only": True, "campaign_eligible": False}


@router.post("/imports/{run_id}/rollback", dependencies=[Depends(require_api_key)])
def rollback(run_id: str, payload: dict[str, str], db: Db) -> dict[str, Any]:
    run = db.get(DatasetImportRun, run_id)
    if run is None:
        raise HTTPException(404, "Import run not found")
    try:
        rollback_import(db, run, operator=payload.get("operator", "operator"))
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"id": run.id, "state": run.state}


@router.post("/compare", dependencies=[Depends(require_api_key)])
def compare(payload: dict[str, str], db: Db) -> dict[str, Any]:
    try:
        run = compare_sources(
            db,
            payload["primary_dataset_id"],
            payload["secondary_dataset_id"],
            output_dir=Path("../reports/research_data/cross_source"),
        )
    except (KeyError, ValueError) as exc:
        raise HTTPException(422, str(exc)) from exc
    return {"id": run.id, "report": run.report, "paths": run.output_paths}

from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, File, HTTPException, Request, Response, UploadFile, status
from fastapi.responses import JSONResponse

from app.api.schemas import (
    BatchAccepted,
    BatchResult,
    CsvInvalid,
    CsvIssue,
    CsvReport,
    HospitalResult,
    Problem,
)
from app.config import Settings
from app.domain.csv_validation import ValidationResult, validate_csv
from app.domain.models import BatchStatus, RowStatus
from app.persistence.repository import Repository, ResumeClaim
from app.runner import RunnerTasks

router = APIRouter(prefix="/hospitals/bulk", tags=["bulk"])


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_repo(request: Request) -> Repository:
    return request.app.state.repo


def get_tasks(request: Request) -> RunnerTasks:
    return request.app.state.tasks


async def _read_upload(file: UploadFile, settings: Settings) -> bytes:
    # read at most one byte past the limit: enough to know it's too big, never the whole thing
    data = await file.read(settings.max_upload_bytes + 1)
    if len(data) > settings.max_upload_bytes:
        raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, f"file exceeds {settings.max_upload_bytes} bytes")
    return data


def _issues(result: ValidationResult) -> list[CsvIssue]:
    return [CsvIssue(message=e.message, row=e.row, line=e.line, column=e.column) for e in result.errors]


def _accepted(response: Response, batch_id: UUID, batch_status: BatchStatus, total: int) -> BatchAccepted:
    url = f"/hospitals/bulk/{batch_id}"
    response.headers["Location"] = url
    return BatchAccepted(batch_id=batch_id, status=batch_status, total_hospitals=total, status_url=url)


@router.post(
    "",
    status_code=status.HTTP_202_ACCEPTED,
    responses={400: {"model": CsvInvalid}, 413: {"model": Problem}},
)
async def create_bulk(
    response: Response,
    file: UploadFile = File(..., description="CSV with header exactly `name,address,phone`"),
    settings: Settings = Depends(get_settings),
    repo: Repository = Depends(get_repo),
    tasks: RunnerTasks = Depends(get_tasks),
) -> BatchAccepted:
    result = validate_csv(await _read_upload(file, settings), settings.max_rows)
    if not result.ok:
        return JSONResponse(status_code=400, content=CsvInvalid(errors=_issues(result)).model_dump())

    batch_id = uuid4()
    await repo.create_batch(batch_id, result.rows)
    tasks.schedule(batch_id)
    return _accepted(response, batch_id, BatchStatus.PROCESSING, len(result.rows))


@router.post("/validate")
async def validate_bulk(
    file: UploadFile = File(...),
    settings: Settings = Depends(get_settings),
) -> CsvReport:
    result = validate_csv(await _read_upload(file, settings), settings.max_rows)
    return CsvReport(valid=result.ok, total_hospitals=len(result.rows), errors=_issues(result))


@router.get("/{batch_id}", responses={404: {"model": Problem}})
async def get_bulk(batch_id: UUID, repo: Repository = Depends(get_repo)) -> BatchResult:
    batch = await repo.get_batch(batch_id)
    if batch is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "batch not found")
    rows = await repo.get_rows(batch_id)

    activated = batch.status is BatchStatus.COMPLETED
    failed = (RowStatus.REJECTED, RowStatus.RETRY_EXHAUSTED)

    def row_status(s: RowStatus) -> str:
        return "created_and_activated" if s is RowStatus.CREATED and activated else str(s)

    return BatchResult(
        batch_id=batch.batch_id,
        status=batch.status,
        total_hospitals=batch.total_rows,
        processed_hospitals=sum(r.status is RowStatus.CREATED for r in rows),
        failed_hospitals=sum(r.status in failed for r in rows),
        pending_hospitals=sum(r.status not in (RowStatus.CREATED, *failed) for r in rows),
        processing_time_seconds=round(batch.processing_seconds, 2),
        batch_activated=activated,
        error=batch.last_error,
        hospitals=[
            HospitalResult(
                row=r.row_no,
                hospital_id=r.upstream_hospital_id,
                name=r.name,
                status=row_status(r.status),
                error=None if r.status is RowStatus.CREATED else r.last_error,
            )
            for r in rows
        ],
    )


@router.post(
    "/{batch_id}/resume",
    status_code=status.HTTP_202_ACCEPTED,
    responses={404: {"model": Problem}, 409: {"model": Problem}},
)
async def resume_bulk(
    batch_id: UUID,
    response: Response,
    repo: Repository = Depends(get_repo),
    tasks: RunnerTasks = Depends(get_tasks),
) -> BatchAccepted:
    claim, batch_status = await repo.claim_for_resume(batch_id)
    if claim is ResumeClaim.NOT_FOUND:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "batch not found")
    if claim is ResumeClaim.HAS_REJECTED_ROWS:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "batch has rows the upstream rejected; resuming cannot fix them. Correct the data and upload a new file",
        )
    if claim is ResumeClaim.NOT_RESUMABLE:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"batch is {batch_status}; only failed, interrupted or activation_failed batches can be resumed",
        )

    tasks.schedule(batch_id)
    batch = await repo.get_batch(batch_id)
    return _accepted(response, batch_id, batch_status, batch.total_rows)

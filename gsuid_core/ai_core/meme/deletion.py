"""Safe, persistent whole-library meme deletion operations."""

import asyncio
from typing import Any, Optional, Sequence
from pathlib import Path
from datetime import datetime, timezone, timedelta

from sqlmodel import col, delete, select, update

from gsuid_core.i18n import t
from gsuid_core.logger import logger
from gsuid_core.ai_core.meme.database_model import (
    AiMemeRecord,
    AiMemeDeleteTarget,
    AiMemeDeleteOperation,
)

DELETE_PREVIEW_TTL = timedelta(minutes=15)
DELETE_BATCH_SIZE = 100
_ACTIVE_STATES = ("queued", "running")

_operation_lock = asyncio.Lock()
_meme_index_lock = asyncio.Lock()
_delete_queue: asyncio.Queue[str] = asyncio.Queue()
_delete_worker_task: Optional[asyncio.Task[None]] = None
_queued_ids: set[str] = set()


def meme_index_lock() -> asyncio.Lock:
    """Serialize vector upserts with destructive SQL/vector batches."""
    return _meme_index_lock


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def safe_meme_path(base_path: Path, relative_path: str) -> Path:
    """Resolve a stored relative path and reject traversal or symlink escape."""
    if not relative_path or Path(relative_path).is_absolute():
        raise ValueError("表情文件路径不是安全的相对路径")
    base = base_path.resolve()
    candidate = (base / relative_path).resolve(strict=False)
    try:
        candidate.relative_to(base)
    except ValueError as exc:
        raise ValueError("表情文件路径越出资源目录") from exc
    return candidate


def operation_to_dict(operation: AiMemeDeleteOperation, targets: Sequence[AiMemeDeleteTarget]) -> dict[str, Any]:
    failed = [
        {"meme_id": target.meme_id, "reason": target.error_message, "attempts": target.attempts}
        for target in targets
        if target.state == "failed"
    ]
    return {
        "operation_id": operation.operation_id,
        "owner_email": operation.owner_email,
        "filters": {
            "folder": operation.folder_filter,
            "persona_hint": operation.persona_filter,
            "status": operation.status_filter,
        },
        "state": operation.state,
        "total_count": operation.total_count,
        "processed_count": operation.processed_count,
        "deleted_count": operation.deleted_count,
        "failed_count": operation.failed_count,
        "failed": failed,
        "created_at": operation.created_at.isoformat(),
        "expires_at": operation.expires_at.isoformat(),
        "confirmed_at": operation.confirmed_at.isoformat() if operation.confirmed_at else None,
        "started_at": operation.started_at.isoformat() if operation.started_at else None,
        "finished_at": operation.finished_at.isoformat() if operation.finished_at else None,
    }


async def create_delete_preview(
    owner_email: str,
    *,
    meme_ids: Optional[list[str]] = None,
    exclude_ids: Optional[list[str]] = None,
    folder: Optional[str] = None,
    persona_hint: Optional[str] = None,
    status: Optional[str] = None,
) -> tuple[AiMemeDeleteOperation, list[AiMemeDeleteTarget]]:
    """Persist one transactionally consistent operation and target snapshot."""
    from gsuid_core.utils.database.base_models import async_maker

    now = _now()
    operation = AiMemeDeleteOperation(
        owner_email=owner_email,
        folder_filter=folder,
        persona_filter=persona_hint,
        status_filter=status,
        expires_at=now + DELETE_PREVIEW_TTL,
    )
    async with async_maker() as session:
        stmt = select(AiMemeRecord).order_by(col(AiMemeRecord.meme_id))
        if meme_ids is not None:
            stmt = stmt.where(col(AiMemeRecord.meme_id).in_(list(dict.fromkeys(meme_ids))))
        if exclude_ids:
            stmt = stmt.where(col(AiMemeRecord.meme_id).not_in(list(dict.fromkeys(exclude_ids))))
        if folder is not None:
            stmt = stmt.where(AiMemeRecord.folder == folder)
        if status is not None:
            stmt = stmt.where(AiMemeRecord.status == status)
        records = list((await session.execute(stmt)).scalars().all())
        targets = [
            AiMemeDeleteTarget(
                operation_id=operation.operation_id,
                meme_id=record.meme_id,
                file_path=record.file_path,
                file_size=record.file_size,
                folder=record.folder,
                persona_hint=record.persona_hint,
                meme_status=record.status,
            )
            for record in records
        ]
        operation.total_count = len(targets)
        session.add(operation)
        session.add_all(targets)
        await session.commit()
    return operation, targets


async def get_delete_operation(operation_id: str) -> tuple[Optional[AiMemeDeleteOperation], list[AiMemeDeleteTarget]]:
    from gsuid_core.utils.database.base_models import async_maker

    async with async_maker() as session:
        operation = await session.get(AiMemeDeleteOperation, operation_id)
        if operation is None:
            return None, []
        stmt = (
            select(AiMemeDeleteTarget)
            .where(AiMemeDeleteTarget.operation_id == operation_id)
            .order_by(col(AiMemeDeleteTarget.meme_id))
        )
        targets = list((await session.execute(stmt)).scalars().all())
        return operation, targets


async def confirm_delete_operation(operation_id: str, owner_email: str, confirmation: str) -> AiMemeDeleteOperation:
    """Owner-confirm an unexpired preview and acquire the global operation slot."""
    from gsuid_core.utils.database.base_models import async_maker

    async with _operation_lock:
        async with async_maker() as session:
            operation = await session.get(AiMemeDeleteOperation, operation_id)
            if operation is None:
                raise ValueError("删除操作不存在")
            if operation.owner_email != owner_email:
                raise PermissionError("只能确认自己创建的删除操作")
            if operation.state != "preview":
                raise ValueError(f"删除操作当前状态不可确认: {operation.state}")
            if _as_utc(operation.expires_at) <= _now():
                operation.state = "expired"
                operation.finished_at = _now()
                session.add(operation)
                await session.commit()
                raise ValueError("删除预览已过期，请重新生成")
            expected = f"DELETE {operation.total_count}"
            if confirmation != expected:
                raise ValueError(f"确认文本不匹配，应为: {expected}")
            active = await session.execute(
                select(AiMemeDeleteOperation.operation_id).where(
                    col(AiMemeDeleteOperation.state).in_(_ACTIVE_STATES),
                    AiMemeDeleteOperation.operation_id != operation_id,
                )
            )
            if active.first() is not None:
                raise RuntimeError("已有删除操作正在执行")
            operation.state = "queued"
            operation.confirmed_at = _now()
            operation.error_message = ""
            session.add(operation)
            await session.commit()

    start_delete_worker()
    await enqueue_delete_operation(operation_id)
    return operation


async def retry_delete_operation(operation_id: str, owner_email: str) -> AiMemeDeleteOperation:
    """Requeue only failed targets from the original immutable snapshot."""
    from gsuid_core.utils.database.base_models import async_maker

    async with _operation_lock:
        async with async_maker() as session:
            operation = await session.get(AiMemeDeleteOperation, operation_id)
            if operation is None:
                raise ValueError("删除操作不存在")
            if operation.owner_email != owner_email:
                raise PermissionError("只能重试自己创建的删除操作")
            retryable = await session.execute(
                select(AiMemeDeleteTarget.meme_id).where(
                    AiMemeDeleteTarget.operation_id == operation_id,
                    col(AiMemeDeleteTarget.state).in_(("pending", "failed")),
                )
            )
            if operation.state not in ("failed", "partial", "interrupted", "completed") or retryable.first() is None:
                raise ValueError("删除操作没有可重试的失败项")
            active = await session.execute(
                select(AiMemeDeleteOperation.operation_id).where(
                    col(AiMemeDeleteOperation.state).in_(_ACTIVE_STATES),
                    AiMemeDeleteOperation.operation_id != operation_id,
                )
            )
            if active.first() is not None:
                raise RuntimeError("已有删除操作正在执行")
            await session.execute(
                update(AiMemeDeleteTarget)
                .where(
                    AiMemeDeleteTarget.operation_id == operation_id,
                    col(AiMemeDeleteTarget.state).in_(("pending", "failed")),
                )
                .values(state="pending", error_message="", updated_at=_now())
            )
            operation.state = "queued"
            operation.processed_count = operation.deleted_count
            operation.failed_count = 0
            operation.error_message = ""
            operation.finished_at = None
            session.add(operation)
            await session.commit()

    start_delete_worker()
    await enqueue_delete_operation(operation_id)
    return operation


async def enqueue_delete_operation(operation_id: str) -> None:
    if operation_id in _queued_ids:
        return
    _queued_ids.add(operation_id)
    await _delete_queue.put(operation_id)


def start_delete_worker() -> None:
    global _delete_worker_task
    if _delete_worker_task is None or _delete_worker_task.done():
        _delete_worker_task = asyncio.create_task(_delete_worker_loop(), name="meme_delete_worker")


async def recover_delete_operations() -> None:
    """Mark active operations interrupted; destructive work requires manual retry."""
    from gsuid_core.utils.database.base_models import async_maker

    async with async_maker() as session:
        operations = list(
            (
                await session.execute(
                    select(AiMemeDeleteOperation).where(col(AiMemeDeleteOperation.state).in_(_ACTIVE_STATES))
                )
            )
            .scalars()
            .all()
        )
        for operation in operations:
            operation.state = "interrupted"
            operation.error_message = "服务重启中断，需管理员手动重试"
            operation.finished_at = _now()
            session.add(operation)
        await session.commit()
    start_delete_worker()


async def stop_delete_worker() -> None:
    global _delete_worker_task
    if _delete_worker_task is not None and not _delete_worker_task.done():
        _delete_worker_task.cancel()
        try:
            await _delete_worker_task
        except asyncio.CancelledError:
            pass
    _delete_worker_task = None
    _queued_ids.clear()


async def _delete_worker_loop() -> None:
    while True:
        operation_id = await _delete_queue.get()
        _queued_ids.discard(operation_id)
        try:
            await execute_delete_operation(operation_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception(t("[Meme] 删除操作执行异常: {exc}", exc=exc))
            await _mark_operation_crashed(operation_id, str(exc))
        finally:
            _delete_queue.task_done()


async def _mark_operation_crashed(operation_id: str, reason: str) -> None:
    from gsuid_core.utils.database.base_models import async_maker

    async with async_maker() as session:
        operation = await session.get(AiMemeDeleteOperation, operation_id)
        if operation is not None:
            operation.state = "failed"
            operation.error_message = reason[:2000]
            operation.finished_at = _now()
            session.add(operation)
            await session.commit()


async def execute_delete_operation(operation_id: str) -> None:
    """Execute pending targets in bounded batches and persist progress."""
    from gsuid_core.utils.database.base_models import async_maker

    async with async_maker() as session:
        operation = await session.get(AiMemeDeleteOperation, operation_id)
        if operation is None or operation.state not in _ACTIVE_STATES:
            return
        operation.state = "running"
        operation.started_at = operation.started_at or _now()
        session.add(operation)
        await session.commit()

    while True:
        async with async_maker() as session:
            stmt = (
                select(AiMemeDeleteTarget)
                .where(
                    AiMemeDeleteTarget.operation_id == operation_id,
                    AiMemeDeleteTarget.state == "pending",
                )
                .order_by(col(AiMemeDeleteTarget.meme_id))
                .limit(DELETE_BATCH_SIZE)
            )
            batch = list((await session.execute(stmt)).scalars().all())
        if not batch:
            break
        await _execute_target_batch(operation_id, batch)

    await _refresh_operation_progress(operation_id, finished=True)


async def _execute_target_batch(operation_id: str, targets: list[AiMemeDeleteTarget]) -> None:
    from gsuid_core.ai_core.meme.library import get_memes_base_path, _remove_many_from_qdrant
    from gsuid_core.utils.database.base_models import async_maker

    base_path = get_memes_base_path()
    ids = [target.meme_id for target in targets]
    async with async_maker() as session:
        records = list(
            (await session.execute(select(AiMemeRecord).where(col(AiMemeRecord.meme_id).in_(ids))))
            .scalars()
            .all()
        )
    record_map = {record.meme_id: record for record in records}
    candidates: list[AiMemeDeleteTarget] = []
    resolved_paths: dict[str, Path] = {}
    failures: dict[str, str] = {}

    for target in targets:
        record = record_map.get(target.meme_id)
        if record is not None and (
            record.file_path != target.file_path
            or record.folder != target.folder
            or record.persona_hint != target.persona_hint
            or record.status != target.meme_status
        ):
            failures[target.meme_id] = "记录在预览后发生变化，拒绝删除"
            continue
        try:
            file_path = safe_meme_path(base_path, target.file_path)
            if file_path.exists() and not file_path.is_file():
                raise ValueError("目标路径不是普通文件")
            resolved_paths[target.meme_id] = file_path
            candidates.append(target)
        except Exception as exc:
            failures[target.meme_id] = str(exc)

    database_deleted_ids: set[str] = set()
    if candidates:
        candidate_ids = [target.meme_id for target in candidates]
        try:
            async with _meme_index_lock:
                await _remove_many_from_qdrant(candidate_ids)
                async with async_maker() as session:
                    await session.execute(delete(AiMemeRecord).where(col(AiMemeRecord.meme_id).in_(candidate_ids)))
                    await session.commit()
            database_deleted_ids.update(candidate_ids)
        except Exception as exc:
            for meme_id in candidate_ids:
                failures[meme_id] = str(exc)

    deleted_ids: set[str] = set()
    for target in candidates:
        if target.meme_id not in database_deleted_ids:
            continue
        try:
            file_path = resolved_paths[target.meme_id]
            if file_path.exists():
                file_path.unlink()
            deleted_ids.add(target.meme_id)
        except Exception as exc:
            failures[target.meme_id] = str(exc)
    async with async_maker() as session:
        for target in targets:
            values: dict[str, Any] = {
                "attempts": target.attempts + 1,
                "updated_at": _now(),
            }
            if target.meme_id in deleted_ids:
                values.update(state="deleted", error_message="")
            else:
                values.update(state="failed", error_message=failures.get(target.meme_id, "删除失败")[:2000])
            await session.execute(
                update(AiMemeDeleteTarget)
                .where(
                    AiMemeDeleteTarget.operation_id == operation_id,
                    AiMemeDeleteTarget.meme_id == target.meme_id,
                )
                .values(**values)
            )
        await session.commit()
    await _refresh_operation_progress(operation_id, finished=False)


async def _refresh_operation_progress(operation_id: str, *, finished: bool) -> None:
    from sqlalchemy import func

    from gsuid_core.utils.database.base_models import async_maker

    async with async_maker() as session:
        counts = dict(
            (
                await session.execute(
                    select(AiMemeDeleteTarget.state, func.count())
                    .where(AiMemeDeleteTarget.operation_id == operation_id)
                    .group_by(AiMemeDeleteTarget.state)
                )
            ).all()
        )
        operation = await session.get(AiMemeDeleteOperation, operation_id)
        if operation is None:
            return
        operation.deleted_count = int(counts.get("deleted", 0))
        operation.failed_count = int(counts.get("failed", 0))
        operation.processed_count = operation.deleted_count + operation.failed_count
        if finished:
            if operation.failed_count == 0:
                operation.state = "completed"
            elif operation.deleted_count:
                operation.state = "partial"
            else:
                operation.state = "failed"
            operation.finished_at = _now()
        session.add(operation)
        await session.commit()

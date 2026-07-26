import asyncio
from types import SimpleNamespace
from pathlib import Path
from datetime import timedelta

import pytest
from fastapi import HTTPException
from sqlmodel import SQLModel
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from gsuid_core.webconsole import web_api, meme_api
from gsuid_core.ai_core.meme import library, deletion
from gsuid_core.ai_core.meme.database_model import (
    AiMemeRecord,
    AiMemeDeleteTarget,
    AiMemeDeleteOperation,
)


async def _database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from gsuid_core.utils.database import base_models

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'meme-delete.db'}")
    maker = async_sessionmaker(engine, expire_on_commit=False)
    tables = [
        AiMemeRecord.__table__,
        AiMemeDeleteOperation.__table__,
        AiMemeDeleteTarget.__table__,
    ]
    async with engine.begin() as connection:
        await connection.run_sync(SQLModel.metadata.create_all, tables=tables)
    monkeypatch.setattr(base_models, "async_maker", maker)
    return engine, maker


def _record(meme_id: str, file_path: str, *, folder: str = "common", status: str = "tagged") -> AiMemeRecord:
    return AiMemeRecord(
        meme_id=meme_id,
        file_path=file_path,
        folder=folder,
        persona_hint="common",
        status=status,
    )


def test_require_admin_reads_current_database_role(monkeypatch: pytest.MonkeyPatch) -> None:
    session = {"email": "owner@example.com", "user": {"email": "owner@example.com", "role": "admin"}}
    monkeypatch.setattr(web_api, "verify_token", lambda *_args, **_kwargs: session)

    async def demoted_user(**_kwargs):
        return SimpleNamespace(role="user")

    from gsuid_core.utils.database.auth_models import WebUser

    monkeypatch.setattr(WebUser, "get_user_by_email", demoted_user)

    async def run() -> None:
        with pytest.raises(HTTPException) as exc:
            await web_api.require_admin(authorization="Bearer token")
        assert exc.value.status_code == 403

    asyncio.run(run())


def test_preview_is_exact_snapshot_and_confirmation_guards(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def run() -> None:
        engine, maker = await _database(tmp_path, monkeypatch)
        try:
            async with maker() as session:
                session.add_all(
                    [
                        _record("a" * 16, "common/a.png"),
                        _record("b" * 16, "other/b.png", folder="other"),
                    ]
                )
                await session.commit()

            ids_operation, ids_targets = await deletion.create_delete_preview(
                "owner@example.com", meme_ids=["b" * 16, "f" * 16, "b" * 16]
            )
            assert [target.meme_id for target in ids_targets] == ["b" * 16]
            assert ids_operation.total_count == 1

            operation, targets = await deletion.create_delete_preview(
                "owner@example.com", folder="common", persona_hint="common", status="tagged"
            )
            assert [target.meme_id for target in targets] == ["a" * 16]
            assert targets[0].file_path == "common/a.png"

            async with maker() as session:
                record = await session.get(AiMemeRecord, "a" * 16)
                assert record is not None
                record.file_path = "common/changed.png"
                session.add(record)
                await session.commit()

            _, persisted = await deletion.get_delete_operation(operation.operation_id)
            assert persisted[0].file_path == "common/a.png"

            with pytest.raises(PermissionError):
                await deletion.confirm_delete_operation(operation.operation_id, "other@example.com", "DELETE 1")
            with pytest.raises(ValueError, match="DELETE 1"):
                await deletion.confirm_delete_operation(operation.operation_id, "owner@example.com", "DELETE 2")

            async with maker() as session:
                stored = await session.get(AiMemeDeleteOperation, operation.operation_id)
                assert stored is not None
                stored.expires_at = deletion._now() - timedelta(seconds=1)
                session.add(stored)
                await session.commit()
            with pytest.raises(ValueError, match="过期"):
                await deletion.confirm_delete_operation(operation.operation_id, "owner@example.com", "DELETE 1")
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_batch_delete_retries_file_after_sql_success(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def run() -> None:
        engine, maker = await _database(tmp_path, monkeypatch)
        meme_root = tmp_path / "memes"
        meme_file = meme_root / "common" / "a.png"
        meme_file.parent.mkdir(parents=True)
        meme_file.write_bytes(b"image")
        qdrant_batches: list[list[str]] = []

        async def fake_remove(meme_ids: list[str]) -> None:
            qdrant_batches.append(meme_ids)

        monkeypatch.setattr(library, "get_memes_base_path", lambda: meme_root)
        monkeypatch.setattr(library, "_remove_many_from_qdrant", fake_remove)
        monkeypatch.setattr(deletion, "start_delete_worker", lambda: None)

        async def no_enqueue(_operation_id: str) -> None:
            return None

        monkeypatch.setattr(deletion, "enqueue_delete_operation", no_enqueue)
        try:
            async with maker() as session:
                session.add(_record("a" * 16, "common/a.png"))
                await session.commit()
            operation, _ = await deletion.create_delete_preview("owner@example.com")
            operation = await deletion.confirm_delete_operation(
                operation.operation_id, "owner@example.com", "DELETE 1"
            )

            original_unlink = Path.unlink
            failed_once = False

            def fail_first_unlink(path: Path, *args, **kwargs):
                nonlocal failed_once
                if path == meme_file and not failed_once:
                    failed_once = True
                    raise OSError("busy")
                return original_unlink(path, *args, **kwargs)

            monkeypatch.setattr(Path, "unlink", fail_first_unlink)
            await deletion.execute_delete_operation(operation.operation_id)
            stored, targets = await deletion.get_delete_operation(operation.operation_id)
            assert stored is not None and stored.state == "failed"
            assert targets[0].state == "failed"
            assert meme_file.exists()
            async with maker() as session:
                assert await session.get(AiMemeRecord, "a" * 16) is None

            monkeypatch.setattr(Path, "unlink", original_unlink)
            await deletion.retry_delete_operation(operation.operation_id, "owner@example.com")
            await deletion.execute_delete_operation(operation.operation_id)
            stored, targets = await deletion.get_delete_operation(operation.operation_id)
            assert stored is not None and stored.state == "completed"
            assert stored.deleted_count == 1 and stored.failed_count == 0
            assert targets[0].attempts == 2
            assert not meme_file.exists()
            assert qdrant_batches == [["a" * 16], ["a" * 16]]
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_recovery_marks_active_operations_interrupted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def run() -> None:
        engine, maker = await _database(tmp_path, monkeypatch)
        monkeypatch.setattr(deletion, "start_delete_worker", lambda: None)
        try:
            operation = AiMemeDeleteOperation(
                owner_email="owner@example.com",
                state="running",
                total_count=1,
                expires_at=deletion._now() + timedelta(minutes=10),
            )
            async with maker() as session:
                session.add(operation)
                await session.commit()
            await deletion.recover_delete_operations()
            async with maker() as session:
                stored = await session.get(AiMemeDeleteOperation, operation.operation_id)
                assert stored is not None
                assert stored.state == "interrupted"
                assert "手动重试" in stored.error_message
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_partial_terminal_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def run() -> None:
        engine, maker = await _database(tmp_path, monkeypatch)
        try:
            operation = AiMemeDeleteOperation(
                owner_email="owner@example.com",
                state="running",
                total_count=2,
                expires_at=deletion._now() + timedelta(minutes=10),
            )
            async with maker() as session:
                session.add(operation)
                session.add_all(
                    [
                        AiMemeDeleteTarget(
                            operation_id=operation.operation_id,
                            meme_id="a" * 16,
                            file_path="common/a.png",
                            folder="common",
                            persona_hint="common",
                            meme_status="tagged",
                            state="deleted",
                        ),
                        AiMemeDeleteTarget(
                            operation_id=operation.operation_id,
                            meme_id="b" * 16,
                            file_path="common/b.png",
                            folder="common",
                            persona_hint="common",
                            meme_status="tagged",
                            state="failed",
                        ),
                    ]
                )
                await session.commit()
            await deletion._refresh_operation_progress(operation.operation_id, finished=True)
            async with maker() as session:
                stored = await session.get(AiMemeDeleteOperation, operation.operation_id)
                assert stored is not None and stored.state == "partial"
                assert stored.deleted_count == 1 and stored.failed_count == 1
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_frontend_api_contract(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def run() -> None:
        engine, maker = await _database(tmp_path, monkeypatch)
        monkeypatch.setattr(deletion, "start_delete_worker", lambda: None)

        async def no_enqueue(_operation_id: str) -> None:
            return None

        monkeypatch.setattr(deletion, "enqueue_delete_operation", no_enqueue)
        admin = {"email": "owner@example.com", "user": {"email": "owner@example.com"}}
        try:
            async with maker() as session:
                session.add_all(
                    [
                        _record("a" * 16, "common/a.png"),
                        _record("b" * 16, "common/b.png"),
                    ]
                )
                for record in session.new:
                    record.file_size = 10
                await session.commit()

            request = meme_api.MemeDeletePreviewRequest.model_validate(
                {
                    "selection": {
                        "mode": "filter",
                        "filter": {"folder": "common", "status": "tagged"},
                        "exclude_ids": ["b" * 16],
                    },
                    "action": "delete",
                }
            )
            response = await meme_api.preview_meme_delete(request, admin)
            data = response["data"]
            assert set(data) == {
                "preview_id",
                "matched_count",
                "status_counts",
                "file_bytes",
                "expires_at",
                "requires_confirmation",
                "confirmation_phrase",
                "sample_ids",
            }
            assert data["matched_count"] == 1
            assert data["status_counts"] == {"tagged": 1}
            assert data["file_bytes"] == 10
            assert data["sample_ids"] == ["a" * 16]

            confirmation = meme_api.MemeDeleteConfirmRequest(
                preview_id=data["preview_id"], confirmation=data["confirmation_phrase"]
            )
            execute = await meme_api.confirm_meme_delete_contract(confirmation, admin)
            assert execute["data"]["status"] == "queued"
            assert execute["data"]["matched_count"] == 1

            status = await meme_api.get_meme_delete_status(data["preview_id"], admin)
            assert set(status["data"]) == {
                "operation_id",
                "status",
                "matched",
                "processed",
                "succeeded",
                "failed",
                "progress",
                "failures",
                "error_summary",
                "created_at",
                "started_at",
                "finished_at",
            }
            assert status["data"]["matched"] == 1

            operation, _targets = await deletion.get_delete_operation(data["preview_id"])
            assert operation is not None
            operation.state = "completed"
            operation.processed_count = 1
            operation.deleted_count = 1
            async with maker() as session:
                session.add(operation)
                await session.commit()
            completed = await meme_api.get_meme_delete_status(data["preview_id"], admin)
            assert completed["data"]["status"] == "succeeded"
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_list_selection_metadata_and_persona_folder_semantics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def run() -> None:
        engine, maker = await _database(tmp_path, monkeypatch)
        try:
            legacy = _record("a" * 16, "common/a.png")
            legacy.persona_hint = ""
            async with maker() as session:
                session.add(legacy)
                await session.commit()

            request = meme_api.MemeDeletePreviewRequest.model_validate(
                {
                    "selection": {
                        "mode": "filter",
                        "filter": {"persona_hint": "common", "status": "tagged"},
                    },
                    "action": "delete",
                }
            )
            preview = await meme_api.preview_meme_delete(
                request, {"email": "owner@example.com", "user": {"email": "owner@example.com"}}
            )
            assert preview["data"]["matched_count"] == 1

            async def fake_search(*_args, **_kwargs):
                return []

            monkeypatch.setattr(library.MemeLibrary, "search_by_text", fake_search)
            result = await meme_api.get_meme_list(
                folder=None,
                persona_hint="common",
                status="tagged",
                q="semantic",
                page=1,
                page_size=20,
                sort="created_at_desc",
                _={},
            )
            assert result["data"]["canonical_filter"] == {"folder": "common", "status": "tagged"}
            assert result["data"]["select_all_supported"] is False
            assert result["data"]["reason"] == "semantic_search_not_exhaustive"
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_compat_single_delete_unlinks_after_vector_and_sql(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def run() -> None:
        engine, maker = await _database(tmp_path, monkeypatch)
        meme_root = tmp_path / "memes"
        meme_file = meme_root / "common" / "a.png"
        meme_file.parent.mkdir(parents=True)
        meme_file.write_bytes(b"image")

        async def fake_remove(_meme_id: str) -> None:
            assert meme_file.exists()
            async with maker() as session:
                assert await session.get(AiMemeRecord, "a" * 16) is not None

        monkeypatch.setattr(library, "get_memes_base_path", lambda: meme_root)
        monkeypatch.setattr(library, "_remove_from_qdrant", fake_remove)
        try:
            async with maker() as session:
                session.add(_record("a" * 16, "common/a.png"))
                await session.commit()
            assert await library.MemeLibrary.delete_meme("a" * 16)
            assert not meme_file.exists()
            async with maker() as session:
                assert await session.get(AiMemeRecord, "a" * 16) is None
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_safe_path_and_qdrant_match_any(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValueError):
        deletion.safe_meme_path(tmp_path, "../outside.png")
    with pytest.raises(ValueError):
        deletion.safe_meme_path(tmp_path, str(tmp_path / "absolute.png"))

    class FakeClient:
        def __init__(self) -> None:
            self.selector = None

        async def delete(self, *, collection_name, points_selector):
            assert collection_name == library.MEME_COLLECTION_NAME
            self.selector = points_selector

    async def run() -> None:
        from gsuid_core.ai_core.rag import base

        client = FakeClient()
        monkeypatch.setattr(base, "client", client)
        await library._remove_many_from_qdrant(["a" * 16, "b" * 16, "a" * 16])
        assert client.selector.must[0].match.any == ["a" * 16, "b" * 16]

    asyncio.run(run())

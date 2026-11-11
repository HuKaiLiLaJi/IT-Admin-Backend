"""Bounded multipart intake and transactional attachment binding."""

import re
import sqlite3

from fastapi import HTTPException, Request
from starlette.datastructures import UploadFile
from starlette.formparsers import MultiPartException, MultiPartParser


MAX_FILES = 5
MAX_FILE_SIZE = 10 * 1024 * 1024
MAX_REQUEST_SIZE = MAX_FILES * MAX_FILE_SIZE + 64 * 1024
FILE_COLUMNS = "id AS file_id, file_name, size, content_type, created_at"


def attachment_error(message: str, status: int = 422) -> HTTPException:
    return HTTPException(status_code=status, detail={
        "code": "invalid_attachment", "message": message,
    })


async def read_uploads(request: Request) -> list[tuple[str, str, bytes]]:
    if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "multipart/form-data":
        raise attachment_error("请用 multipart/form-data 的 files 字段上传文件")

    async def limited_stream():
        received = 0
        async for chunk in request.stream():
            received += len(chunk)
            if received > MAX_REQUEST_SIZE:
                raise MultiPartException("上传请求超过大小限制")
            yield chunk

    try:
        form = await MultiPartParser(request.headers, limited_stream(), max_files=MAX_FILES, max_fields=0).parse()
    except MultiPartException as error:
        raise attachment_error("上传格式无效或超过限制，每次最多5个文件，单文件最多10 MiB", 413) from error
    try:
        entries = form.multi_items()
        if not entries or any(key != "files" or not isinstance(value, UploadFile) for key, value in entries):
            raise attachment_error("请使用 files 字段上传至少一个文件")
        uploads = []
        for _, file in entries:
            name = re.sub(r"[\x00-\x1f\x7f]", "", (file.filename or "").replace("\\", "/").rsplit("/", 1)[-1]).strip()
            if not name or name in (".", "..") or len(name) > 255:
                raise attachment_error("文件名须为1～255字符")
            content = await file.read(MAX_FILE_SIZE + 1)
            if len(content) > MAX_FILE_SIZE:
                raise attachment_error("单文件最多10 MiB", 413)
            if not content:
                raise attachment_error("文件内容不能为空")
            content_type = file.content_type or "application/octet-stream"
            if len(content_type) > 255 or not re.fullmatch(r"[\w!#$&^.+-]+/[\w!#$&^.+-]+", content_type, re.ASCII):
                content_type = "application/octet-stream"
            uploads.append((name, content_type, content))
        return uploads
    finally:
        await form.close()


def ticket_files(connection: sqlite3.Connection, ticket_id: str) -> list[dict]:
    return [dict(row) for row in connection.execute(
        f"SELECT {FILE_COLUMNS} FROM attachments WHERE ticket_id = ? ORDER BY created_at, id", (ticket_id,),
    )]


def bind_files(connection: sqlite3.Connection, file_ids: list[str], owner_id: str, ticket_id: str) -> int:
    if len(set(file_ids)) != len(file_ids):
        raise attachment_error("文件编号不能重复")
    added = 0
    for file_id in file_ids:
        row = connection.execute(
            "SELECT ticket_id FROM attachments WHERE id = ? AND owner_id = ?", (file_id, owner_id),
        ).fetchone()
        if row is None or row["ticket_id"] not in (None, ticket_id):
            raise attachment_error("文件不存在、不可使用或已绑定其他工单")
        if row["ticket_id"] is None:
            connection.execute("UPDATE attachments SET ticket_id = ? WHERE id = ?", (ticket_id, file_id))
            added += 1
    return added

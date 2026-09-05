from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterator

from fastapi import FastAPI, File, HTTPException, Request, UploadFile, status, Query, Path as ApiPath
from fastapi.exceptions import RequestValidationError
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse, Response, StreamingResponse

from .config import Settings
from .django_auth import AuthenticationBackend, DjangoAuthentication
from .models import ApproveRequest, CreateEditRequest, LoginRequest, RegisterRequest, ReviseRequest, EditResponse, PlanResponse, ResultResponse, UserResponse, VideoResponse
from .repository import ConflictError, NotFoundError, PostgresRepository, new_id
from .storage import Storage, build_storage


def create_app(
    settings: Settings | None = None,
    repository: PostgresRepository | None = None,
    storage: Storage | None = None,
    blocking_runner: Callable[..., Awaitable[Any]] | None = None,
    authentication: AuthenticationBackend | None = None,
) -> FastAPI:
    selected = settings or Settings.from_env()
    selected.validate()
    repo = repository or PostgresRepository(selected.database_url)
    # The real service delegates accounts, password hashing, and sessions
    # entirely to Django. Tests can inject a narrow authentication double.
    auth = authentication or DjangoAuthentication(selected.database_url, selected.django_secret_key)
    objects = storage or build_storage(selected)
    application = FastAPI(title="Video Editing Service", version="1.0.0")
    web_root = Path(__file__).with_name("web").resolve()

    async def run_blocking(function: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        if blocking_runner is not None:
            return await blocking_runner(function, *args, **kwargs)
        return await asyncio.to_thread(function, *args, **kwargs)

    @application.get("/", include_in_schema=False)
    async def website() -> Response:
        """Serve the Phase 2 local product UI without changing the API boundary."""
        return await web_asset("index.html")

    @application.get("/web/{asset_path:path}", include_in_schema=False)
    async def web_asset(asset_path: str) -> Response:
        candidate = (web_root / asset_path).resolve()
        if web_root not in candidate.parents or not candidate.is_file():
            raise HTTPException(status_code=404, detail={"code": "not_found", "message": "web asset not found", "retryable": False})
        media_types = {".css": "text/css; charset=utf-8", ".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8"}
        content = await run_blocking(candidate.read_bytes)
        return Response(
            content=content,
            media_type=media_types.get(candidate.suffix, "application/octet-stream"),
            headers={"Cache-Control": "no-store"},
        )

    @application.exception_handler(HTTPException)
    async def http_error(_request: Request, exc: HTTPException) -> JSONResponse:
        detail = exc.detail if isinstance(exc.detail, dict) else {"code": "request_error", "message": str(exc.detail)}
        error = {
            "code": detail.get("code", "request_error"),
            "stage": detail.get("stage", "api"),
            "message": detail.get("message", "request failed"),
            "retryable": bool(detail.get("retryable", False)),
            "details": detail.get("details", {}),
            "request_id": new_id("req"),
        }
        return JSONResponse(status_code=exc.status_code, content={"error": error}, headers=exc.headers)

    @application.exception_handler(RequestValidationError)
    async def validation_error(_request: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(status_code=422, content=jsonable_encoder({"error": {
            "code": "invalid_request", "stage": "api", "message": "request validation failed",
            "retryable": False, "details": {"issues": exc.errors()}, "request_id": new_id("req"),
        }}))

    def missing_or_conflict(exc: Exception) -> HTTPException:
        if isinstance(exc, NotFoundError):
            return HTTPException(status_code=404, detail={"code": "not_found", "message": str(exc), "retryable": False})
        return HTTPException(status_code=409, detail={"code": "invalid_state", "message": str(exc), "retryable": False})

    async def authenticated_user(request: Request) -> dict[str, Any]:
        token = request.cookies.get("video_edit_session")
        if not token:
            raise HTTPException(status_code=401, detail={"code": "authentication_required", "message": "sign in is required", "retryable": False})
        try:
            return await run_blocking(auth.get_session_user, token)
        except NotFoundError as exc:
            raise HTTPException(status_code=401, detail={"code": "authentication_required", "message": "session is invalid or expired", "retryable": False}) from exc

    def session_response(user: dict[str, Any], token: str) -> JSONResponse:
        response = JSONResponse(status_code=status.HTTP_201_CREATED, content=UserResponse(id=user["id"], email=user["email"]).model_dump(mode="json"))
        response.set_cookie("video_edit_session", token, max_age=14 * 24 * 60 * 60, httponly=True, secure=selected.session_cookie_secure, samesite="lax", path="/")
        return response

    @application.post("/v1/auth/register", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
    async def register(request: RegisterRequest) -> JSONResponse:
        try:
            user = await run_blocking(auth.register, email=request.email, password=request.password)
            user, token = await run_blocking(auth.create_session, email=request.email, password=request.password)
        except ConflictError as exc:
            raise missing_or_conflict(exc) from exc
        return session_response(user, token)

    @application.post("/v1/auth/login", response_model=UserResponse)
    async def login(request: LoginRequest) -> JSONResponse:
        try:
            user, token = await run_blocking(auth.create_session, email=request.email, password=request.password)
        except NotFoundError as exc:
            raise HTTPException(status_code=401, detail={"code": "invalid_credentials", "message": str(exc), "retryable": False}) from exc
        response = session_response(user, token)
        response.status_code = status.HTTP_200_OK
        return response

    @application.post("/v1/auth/logout", status_code=status.HTTP_204_NO_CONTENT)
    async def logout(request: Request) -> Response:
        token = request.cookies.get("video_edit_session")
        if token:
            await run_blocking(auth.delete_session, token)
        response = Response(status_code=status.HTTP_204_NO_CONTENT)
        response.delete_cookie("video_edit_session", path="/")
        return response

    @application.get("/v1/auth/me", response_model=UserResponse)
    async def me(request: Request) -> UserResponse:
        user = await authenticated_user(request)
        return UserResponse(id=user["id"], email=user["email"])

    def edit_response(row: dict[str, Any]) -> EditResponse:
        iteration = row.get("current_iteration", 1)
        return EditResponse(id=row["id"], state=row["state"], created_at=row["created_at"],
                            progress=row.get("progress"), error=row.get("failure"), iteration=iteration,
                            active_iteration=row.get("active_iteration"), approved_iteration=row.get("approved_iteration"),
                            preview_status=row.get("preview_status", "queued"), render_status=row.get("render_status"),
                            plan_url=f"/v1/edits/{row['id']}/iterations/{iteration}/plan", jobs=row.get("jobs", []))

    @application.post("/v1/edits/{edit_id}/revise", response_model=EditResponse, status_code=202)
    async def revise(edit_id: str, request: ReviseRequest, http_request: Request) -> EditResponse:
        try:
            user = await authenticated_user(http_request)
            row = await run_blocking(repo.revise, edit_id, request.instruction, user["id"])
        except (NotFoundError, ConflictError) as exc:
            raise missing_or_conflict(exc) from exc
        return edit_response(row)

    @application.get("/v1/edits/{edit_id}/iterations")
    async def iterations(edit_id: str, request: Request) -> dict[str, Any]:
        try:
            user = await authenticated_user(request)
            rows = await run_blocking(repo.get_iterations, edit_id, user["id"])
        except (NotFoundError, ConflictError) as exc:
            raise missing_or_conflict(exc) from exc
        for row in rows:
            number = row["iteration"]
            row["plan_url"] = f"/v1/edits/{edit_id}/iterations/{number}/plan" if row.get("plan_id") else None
            row["preview_url"] = f"/v1/edits/{edit_id}/iterations/{number}/preview" if row["preview_status"] == "succeeded" else None
        return {"edit_id": edit_id, "iterations": rows}

    @application.post("/v1/videos", response_model=VideoResponse, status_code=status.HTTP_201_CREATED)
    async def upload_video(request: Request, file: UploadFile = File(...)) -> VideoResponse:
        user = await authenticated_user(request)
        filename = Path(file.filename or "upload.media").name
        if not filename or filename in {".", ".."}:
            raise HTTPException(status_code=422, detail={"code": "invalid_filename", "message": "filename is required", "retryable": False})
        video_id = new_id("vid")
        suffix = Path(filename).suffix.lower()
        if not suffix or len(suffix) > 11 or not suffix[1:].isalnum():
            suffix = ".media"
        key = f"videos/{video_id}/source{suffix}"
        try:
            stored = await run_blocking(objects.put, key, file.file, max_bytes=selected.max_upload_bytes)
        except ValueError as exc:
            raise HTTPException(status_code=413, detail={"code": "upload_too_large", "message": str(exc), "retryable": False}) from exc
        row = await run_blocking(repo.create_video,
            video_id=video_id, filename=filename, content_type=file.content_type,
            storage_key=stored.key, size=stored.size, sha256=stored.sha256, user_id=user["id"],
        )
        return VideoResponse(id=row["id"], state=row["state"], filename=row["filename"])

    @application.post("/v1/edits", response_model=EditResponse, status_code=status.HTTP_202_ACCEPTED)
    async def create_edit(request: CreateEditRequest, http_request: Request) -> EditResponse:
        try:
            user = await authenticated_user(http_request)
            row = await run_blocking(repo.create_edit, video_id=request.video_id, instruction=request.instruction, user_id=user["id"])
        except (NotFoundError, ConflictError) as exc:
            raise missing_or_conflict(exc) from exc
        return edit_response(row)

    @application.get("/v1/edits/{edit_id}", response_model=EditResponse)
    async def get_edit(edit_id: str, request: Request) -> EditResponse:
        try:
            user = await authenticated_user(request)
            row = await run_blocking(repo.get_edit, edit_id, user["id"])
        except (NotFoundError, ConflictError) as exc:
            raise missing_or_conflict(exc) from exc
        return edit_response(row)

    @application.get("/v1/edits/{edit_id}/plan", response_model=PlanResponse)
    async def get_plan(edit_id: str, request: Request, iteration: int | None = Query(default=None, ge=1)) -> PlanResponse:
        try:
            user = await authenticated_user(request)
            row = await run_blocking(repo.get_plan, edit_id, iteration, user["id"])
        except (NotFoundError, ConflictError) as exc:
            raise missing_or_conflict(exc) from exc
        return PlanResponse(
            edit_id=edit_id, plan_id=row["id"], status=row["status"], summary=row["summary"],
            warnings=row["warnings"], edit_plan=row["document"],
            iteration=row.get("iteration", 1), parent_iteration=row.get("parent_iteration"), instruction=row.get("instruction", ""),
            plan_status=row.get("plan_status", "awaiting_approval"), preview_status=row.get("preview_status", "queued"),
            render_status=row.get("render_status"), decision_log=row.get("decision_log", {}), error=row.get("failure"),
            video_url=f"/v1/edits/{edit_id}/iterations/{row['iteration']}/video" if row.get("render_status") == "succeeded" else None,
            **({f"{kind}_url": f"/v1/edits/{edit_id}/iterations/{row['iteration']}/{kind}" for kind in ("preview", "poster", "inspection")}
               if row.get("preview_status") == "succeeded" else {}),
        )

    @application.get("/v1/edits/{edit_id}/iterations/{iteration}/plan", response_model=PlanResponse)
    async def iteration_plan(edit_id: str, request: Request, iteration: int = ApiPath(ge=1)) -> PlanResponse:
        return await get_plan(edit_id, request, iteration)

    @application.post("/v1/edits/{edit_id}/approve", response_model=EditResponse)
    async def approve(edit_id: str, request: ApproveRequest, http_request: Request) -> EditResponse:
        try:
            user = await authenticated_user(http_request)
            row = await run_blocking(repo.approve, edit_id, request.plan_id, user["id"])
        except (NotFoundError, ConflictError) as exc:
            raise missing_or_conflict(exc) from exc
        return edit_response(row)

    @application.post("/v1/edits/{edit_id}/render", response_model=EditResponse, status_code=status.HTTP_202_ACCEPTED)
    async def queue_render(edit_id: str, request: Request) -> EditResponse:
        try:
            user = await authenticated_user(request)
            row = await run_blocking(repo.queue_render, edit_id, user["id"])
        except (NotFoundError, ConflictError) as exc:
            raise missing_or_conflict(exc) from exc
        return edit_response(row)

    @application.get("/v1/edits/{edit_id}/result", response_model=ResultResponse)
    async def get_result(edit_id: str, request: Request) -> ResultResponse:
        try:
            user = await authenticated_user(request)
            edit, artifacts = await run_blocking(repo.get_result, edit_id, user["id"])
        except (NotFoundError, ConflictError) as exc:
            raise missing_or_conflict(exc) from exc
        urls: dict[str, str] = {}
        for artifact in artifacts:
            url = await run_blocking(objects.url, artifact["storage_key"])
            urls[f"{artifact['kind']}_url"] = url or f"/v1/edits/{edit_id}/result/{artifact['kind']}"
        return ResultResponse(
            edit_id=edit_id, state=edit["state"], video_url=urls.pop("video_url"), artifacts=urls,
        )

    @application.get("/v1/edits/{edit_id}/result/{kind}", include_in_schema=False)
    async def download_result(edit_id: str, kind: str, request: Request) -> StreamingResponse:
        return await download_artifact(edit_id, kind, request)

    @application.get("/v1/edits/{edit_id}/iterations/{iteration}/{kind}")
    async def download_iteration(edit_id: str, kind: str, request: Request, iteration: int = ApiPath(ge=1)) -> StreamingResponse:
        return await download_artifact(edit_id, kind, request, iteration)

    async def download_artifact(edit_id: str, kind: str, request: Request, iteration: int | None = None) -> StreamingResponse:
        try:
            user = await authenticated_user(request)
            artifact = await run_blocking(repo.get_artifact, edit_id, kind, iteration, user["id"])
        except (NotFoundError, ConflictError) as exc:
            raise missing_or_conflict(exc) from exc
        size = artifact.get("size_bytes", artifact.get("size"))
        start, end = 0, size - 1 if size is not None else None
        partial = request.headers.get("range")
        if partial and size is not None:
            match = re.fullmatch(r"bytes=(\d*)-(\d*)", partial)
            if not match or not any(match.groups()):
                raise HTTPException(416, "unsupported byte range", headers={"Content-Range": f"bytes */{size}"})
            first, last = match.groups()
            if first:
                start, end = int(first), min(int(last), size - 1) if last else size - 1
            else:
                start, end = max(0, size - int(last)), size - 1
            if start > end or start >= size:
                raise HTTPException(416, "byte range outside artifact", headers={"Content-Range": f"bytes */{size}"})
        stream = await run_blocking(objects.open, artifact["storage_key"])

        async def chunks() -> Iterator[bytes]:
            try:
                # Storage bodies need not support seek (e.g. S3 StreamingBody).
                skip = start
                while skip:
                    skipped = await run_blocking(stream.read, min(skip, 1024 * 1024))
                    if not skipped:
                        return
                    skip -= len(skipped)
                remaining = end - start + 1 if end is not None else None
                while remaining is None or remaining > 0:
                    chunk = await run_blocking(stream.read, min(remaining, 1024 * 1024) if remaining is not None else 1024 * 1024)
                    if not chunk:
                        break
                    yield chunk
                    if remaining is not None:
                        remaining -= len(chunk)
            finally:
                await run_blocking(stream.close)

        media_type = "video/mp4" if kind in {"video", "preview"} else "image/png" if kind == "poster" else "application/json" if kind in {"inspection", "final_inspection", "decisions", "manifest", "edit_plan"} else "application/octet-stream"
        headers = {
            "Accept-Ranges": "bytes", "X-Content-Type-Options": "nosniff",
            # Iteration artifacts are immutable, but a browser can otherwise
            # reuse an incomplete media response after an interrupted preview.
            "Cache-Control": "no-store",
            "Content-Disposition": f'inline; filename="{kind}{artifact["storage_key"].rsplit("/", 1)[-1][-8:]}"',
        }
        if size is not None:
            headers["Content-Length"] = str(end - start + 1)
        if partial and size is not None:
            headers["Content-Range"] = f"bytes {start}-{end}/{size}"
        if artifact.get("sha256"):
            headers["ETag"] = f'"{artifact["sha256"]}"'
        return StreamingResponse(chunks(), media_type=media_type, status_code=206 if partial and size is not None else 200, headers=headers)

    return application


def main() -> int:
    try:
        import uvicorn
    except ImportError as exc:
        raise RuntimeError("API serving requires the 'service' project extra") from exc
    # Pass the created app directly. This avoids a second module import that can
    # resolve to a stale installed copy when the CLI is launched during local
    # editable development.
    uvicorn.run(create_app(), host="127.0.0.1", port=8000)
    return 0

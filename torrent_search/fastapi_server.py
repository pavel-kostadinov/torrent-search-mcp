import json
from os import getenv
from pathlib import Path as PathLib
from typing import Literal

from fastapi import FastAPI, HTTPException, Path, Response
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.staticfiles import StaticFiles
from minify_html import minify
from pydantic import BaseModel

from .library import Library, LibraryError, LibraryItem
from .synology import (
    DownloadTask,
    SynologyConfig,
    SynologyDownloadStation,
    SynologyError,
)
from .wrapper import Torrent, TorrentSearchApi

app = FastAPI(
    title="Torrent Search FastAPI",
    description="FastAPI server for Torrent Search API.",
)
api_client = TorrentSearchApi()
app.add_middleware(GZipMiddleware, minimum_size=1024)
_STATIC_DIR = PathLib(__file__).parent / "static"
_TELEGRAM_BOT_HANDLE = getenv("TELEGRAM_BOT_HANDLE") or None
_TELEGRAM_BOT_HANDLE_JSON = json.dumps(_TELEGRAM_BOT_HANDLE).replace("<", "\\u003c")
_synology_config = SynologyConfig.from_env()
synology = SynologyDownloadStation(_synology_config) if _synology_config else None
library = Library.from_env()
_WEBUI_HTML = minify(
    (_STATIC_DIR / "index.html")
    .read_text(encoding="utf-8")
    .replace("__TELEGRAM_BOT_HANDLE__", _TELEGRAM_BOT_HANDLE_JSON)
    .replace("__SYNOLOGY_ENABLED__", json.dumps(synology is not None))
    .replace("__LIBRARY_ENABLED__", json.dumps(library is not None)),
    minify_css=True,
    minify_js=True,
)
app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")


@app.get(
    "/",
    summary="Web UI",
    tags=["Webui"],
)
async def webui() -> Response:
    """
    Serve the bundled dark-mode web UI (minified).
    """
    return Response(content=_WEBUI_HTML, media_type="text/html")


@app.get("/sources", summary="List Sources", tags=["General"], response_model=list[str])
async def list_sources() -> list[str]:
    """
    Endpoint listing the available torrent sources.
    """
    return api_client.available_sources()


@app.get(
    "/torrent/popular",
    summary="Popular Torrents",
    tags=["Torrents"],
    response_model=list[Torrent],
)
async def popular_torrents(response: Response, per_source: int = 10) -> list[Torrent]:
    """
    Get the most popular torrents across sources with a top listing,
    keeping up to `per_source` results from each. Cached server-side for
    2 minutes; the response is marked no-store so browsers always re-fetch.
    Corresponds to `TorrentSearchApi.popular_torrents()`.
    """
    response.headers.update(
        {
            "Cache-Control": "no-cache, no-store",
            "Pragma": "no-cache",
            "Expires": "0",
        }
    )
    torrents: list[Torrent] = await api_client.popular_torrents(per_source)
    return torrents


@app.post(
    "/torrent/search",
    summary="Search Torrents",
    tags=["Torrents"],
    response_model=list[Torrent],
)
async def search_torrents(
    response: Response,
    query: str,
    max_items: int = 20,
) -> list[Torrent]:
    """
    Search for torrents across all enabled sources.
    Corresponds to `TorrentSearchApi.search_torrents()`.
    """
    response.headers.update(
        {
            "Cache-Control": "no-cache, no-store",
            "Pragma": "no-cache",
            "Expires": "0",
        }
    )
    torrents: list[Torrent] = await api_client.search_torrents(query, max_items)
    return torrents


@app.get(
    "/torrent/{torrent_id}",
    summary="Get Magnet Link",
    tags=["Torrents"],
    response_model=str,
)
async def get_torrent(
    torrent_id: str = Path(..., description="The ID of the torrent."),
) -> str:
    """
    Get the magnet link for a specific torrent by id.
    Corresponds to `TorrentSearchApi.get_torrent()`.
    """
    result: str | None = await api_client.get_torrent(torrent_id)
    if not result:
        raise HTTPException(
            status_code=404,
            detail="Magnet link not found.",
        )
    return result


def _require_nas() -> SynologyDownloadStation:
    if synology is None:
        raise HTTPException(status_code=404, detail="NAS downloads are not configured.")
    return synology


def _require_library() -> Library:
    if library is None:
        raise HTTPException(
            status_code=404,
            detail="The download folder is not configured (LIBRARY_DIR).",
        )
    return library


class NasDownloadRequest(BaseModel):
    magnet: str


@app.get("/nas/status", summary="NAS Connection Status", tags=["NAS"])
async def nas_status() -> dict[str, object]:
    """
    Report whether the NAS is configured and the credentials work.
    """
    if synology is None:
        return {"configured": False, "connected": False, "library": library is not None}
    try:
        await synology.check()
    except SynologyError as exc:
        return {
            "configured": True,
            "connected": False,
            "error": str(exc),
            "library": library is not None,
        }
    return {"configured": True, "connected": True, "library": library is not None}


@app.post("/nas/download", summary="Send to Synology Download Station", tags=["NAS"])
async def nas_download(request: NasDownloadRequest) -> dict[str, bool]:
    """
    Add a magnet link as a task in Synology Download Station.
    Requires SYNOLOGY_URL, SYNOLOGY_USERNAME and SYNOLOGY_PASSWORD.
    """
    try:
        await _require_nas().add_magnet(request.magnet)
    except SynologyError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {"ok": True}


@app.get(
    "/nas/tasks",
    summary="List Download Station Tasks",
    tags=["NAS"],
    response_model=list[DownloadTask],
)
async def nas_tasks(response: Response) -> list[DownloadTask]:
    """
    List the app account's Download Station tasks with progress and speed.
    """
    response.headers["Cache-Control"] = "no-store"
    try:
        return await _require_nas().list_tasks()
    except SynologyError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.post(
    "/nas/tasks/{task_id}/{action}", summary="Pause or Resume a Task", tags=["NAS"]
)
async def nas_task_action(
    task_id: str, action: Literal["pause", "resume"]
) -> dict[str, bool]:
    """
    Pause or resume a Download Station task.
    """
    nas = _require_nas()
    try:
        await (nas.pause if action == "pause" else nas.resume)(task_id)
    except SynologyError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {"ok": True}


@app.delete("/nas/tasks/{task_id}", summary="Remove a Task", tags=["NAS"])
async def nas_task_delete(task_id: str, delete_files: bool = False) -> dict[str, bool]:
    """
    Remove a Download Station task. With `delete_files=true`, also permanently
    delete its downloaded file or folder from the download folder.
    """
    nas = _require_nas()
    lib = _require_library() if delete_files else None
    try:
        task = await nas.get_task(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="Task not found.")
        if lib is not None and lib.is_protected(task.title):
            raise HTTPException(
                status_code=409,
                detail=f"“{task.title}” is a protected folder. Remove the download without deleting files.",
            )
        if lib is not None:
            wanted = (nas.config.destination or "").strip("/")
            if wanted and task.destination.strip("/") != wanted:
                raise HTTPException(
                    status_code=409,
                    detail=f"This download was saved to “{task.destination}”, outside the managed folder. Remove it without deleting files.",
                )
        await nas.delete(task_id)
    except SynologyError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    files_deleted = False
    if lib is not None and lib.exists(task.title):
        try:
            await lib.delete(task.title)
            files_deleted = True
        except (LibraryError, OSError) as exc:
            raise HTTPException(
                status_code=500,
                detail=f"Task removed, but deleting files failed: {exc}",
            ) from exc
    return {"ok": True, "files_deleted": files_deleted}


@app.get(
    "/library",
    summary="List Downloaded Files",
    tags=["Library"],
    response_model=list[LibraryItem],
)
async def library_list(response: Response) -> list[LibraryItem]:
    """
    List the top-level files and folders in the download folder.
    """
    response.headers["Cache-Control"] = "no-store"
    try:
        return await _require_library().list()
    except LibraryError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.delete("/library/{name}", summary="Delete a Downloaded File", tags=["Library"])
async def library_delete(name: str) -> dict[str, bool]:
    """
    Permanently delete a top-level file or folder from the download folder.
    """
    try:
        await _require_library().delete(name)
    except LibraryError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except OSError as exc:
        raise HTTPException(
            status_code=500, detail=f"Delete failed: {exc.strerror or exc}"
        ) from exc
    return {"ok": True}

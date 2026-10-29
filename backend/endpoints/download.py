"""Endpoint helpers for the Download feature (remote ROM sources)."""

from typing import Any

import httpx
from fastapi import BackgroundTasks, HTTPException, Request, status
from pydantic import BaseModel

from decorators.auth import protected_route
from handler.auth.constants import Scope
from handler.metadata import meta_sgdb_handler
from logger.logger import log
from utils.router import APIRouter

MINERVA_BASE_URL = "https://minerva-archive.org"

router = APIRouter(
    prefix="/download",
    tags=["download"],
)


@protected_route(
    router.get,
    "/minerva/filters",
    [Scope.ROMS_READ],
)
async def minerva_filters(request: Request) -> dict[str, Any]:
    """Proxy Minerva's filter lists so the frontend can populate pickers."""
    async with httpx.AsyncClient(base_url=MINERVA_BASE_URL, timeout=120.0) as client:
        response = await client.get("/v1/api/rom/filters")
    if response.status_code != 200:
        log.error("Minerva filters request failed: %s", response.status_code)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Minerva archive unavailable",
        )
    return response.json()


@protected_route(
    router.get,
    "/minerva/search",
    [Scope.ROMS_READ],
)
async def minerva_search(
    request: Request,
    query: str,
    group: str | None = None,
    console: str | None = None,
    region: str | None = None,
    manufacturer: str | None = None,
    min_size: int | None = None,
    max_size: int | None = None,
    tokenised: bool = False,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """Proxy a Minerva search, returning ROM metadata."""
    params: dict[str, Any] = {"query": query.lower(), "offset": offset}
    if tokenised:
        params["tokenised"] = "true"
    if min_size:
        params["min_size"] = min_size
    if max_size:
        params["max_size"] = max_size
    if group:
        params["group"] = group
    if console:
        params["console"] = console
    if region:
        params["region"] = region
    if manufacturer:
        params["manufacturer"] = manufacturer

    try:
        async with httpx.AsyncClient(
            base_url=MINERVA_BASE_URL, timeout=120.0
        ) as client:
            response = await client.get("/v1/api/rom/search", params=params)
    except httpx.TimeoutException as exc:
        log.error("Minerva search timed out: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail="Minerva archive timed out",
        ) from exc
    except httpx.HTTPError as exc:
        log.error("Minerva search request failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Minerva archive unavailable",
        ) from exc
    if response.status_code != 200:
        log.error("Minerva search request failed: %s", response.status_code)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Minerva archive unavailable",
        )
    rows = response.json()
    if not isinstance(rows, list):
        return rows
    # Annotate entries that already exist in the RomM library so the UI
    # can gray them out and prevent re-downloading.
    try:
        from pathlib import Path

        from handler.database import db_rom_handler as db_roms_handler

        candidates: list[str] = []
        for r in rows:
            name = r.get("file_name") or str(r.get("full_path", "")).rsplit("/", 1)[-1]
            candidates.append(Path(str(name)).stem)
        existing_roms = db_roms_handler.get_roms_by_fs_names_no_ext(candidates)
        existing_names = {
            str(getattr(rom, "fs_name_no_ext", "") or "").lower()
            for rom in (existing_roms or [])
        }
        for r in rows:
            name = r.get("file_name") or str(r.get("full_path", "")).rsplit("/", 1)[-1]
            r["exists"] = Path(str(name)).stem.lower() in existing_names
    except Exception as exc:  # noqa: BLE001
        log.warning("Could not annotate existing ROMs: %s", exc)
        for r in rows:
            r.setdefault("exists", False)
    return rows


class MinervaDownloadItem(BaseModel):
    id: int | None = None
    full_path: str
    file_name: str | None = None
    magnet: str
    so_id: int
    size: int | None = None
    sha256: str | None = None
    cover_url: str | None = None
    console: str | None = None


class MinervaDownloadRequest(BaseModel):
    platform_fs_slug: str
    items: list[MinervaDownloadItem]


def _magnet_hash(magnet: str) -> str:
    import re

    match = re.search(r"btih:([0-9a-fA-F]+)", magnet)
    if not match:
        raise ValueError("No btih in magnet link")
    return match.group(1).lower()


_CANCELLED_DOWNLOAD_KEYS: set[str] = set()

_USER_DOWNLOAD_QUEUES: dict[int, list[dict[str, Any]]] = {}
_USER_DOWNLOAD_PROGRESS: dict[int, dict[str, dict[str, Any]]] = {}


def _user_download_queue(user_id: int) -> list[dict[str, Any]]:
    return _USER_DOWNLOAD_QUEUES.setdefault(user_id, [])


def _user_download_progress(user_id: int) -> dict[str, dict[str, Any]]:
    return _USER_DOWNLOAD_PROGRESS.setdefault(user_id, {})


async def _run_minerva_batch(
    items: list[MinervaDownloadItem], platform_fs_slug: str, user_id: int
) -> None:
    from config.config_manager import config_manager as cm

    cfg = cm.get_config().DOWNLOAD

    import asyncio
    import os
    import re
    import shutil
    from pathlib import Path

    import httpx

    base = cfg.qbittorrent_url or os.getenv(
        "ROMM_QBITTORRENT_URL", "http://127.0.0.1:8080"
    )
    username = cfg.qbittorrent_username or os.getenv(
        "ROMM_QBITTORRENT_USERNAME", "admin"
    )
    password = cfg.qbittorrent_password or os.getenv(
        "ROMM_QBITTORRENT_PASSWORD", "adminadmin"
    )
    base_path = os.getenv("ROMM_BASE_PATH", "/romm")
    library_path = Path(
        os.getenv("ROMM_LIBRARY_PATH", str(Path(base_path) / "library"))
    )
    staging_root = library_path / "_minerva_staging"
    staging_root.mkdir(parents=True, exist_ok=True)

    async with httpx.AsyncClient(
        base_url=base,
        timeout=120.0,
        headers={
            "Referer": f"{base}/",
            "Origin": base,
            "X-Requested-With": "XMLHttpRequest",
        },
    ) as client:
        login = await client.post(
            "/api/v2/auth/login",
            data={"username": username, "password": password},
        )
        if login.status_code not in (200, 204):
            log.error("qBittorrent login failed: %s", login.status_code)
            return

        from handler.socket_handler import socket_handler

        for _qitem in items:
            await socket_handler.emit_to_user(
                user_id,
                "download:progress",
                {
                    "id": _qitem.id,
                    "file_name": _qitem.file_name,
                    "progress": 0.0,
                    "status": "queued",
                },
            )

        for item in items:
            job_dir = staging_root / re.sub(r"[^A-Za-z0-9_.-]", "_", item.file_name)
            job_dir.mkdir(parents=True, exist_ok=True)
            try:
                if {str(item.id), str(item.file_name)} & _CANCELLED_DOWNLOAD_KEYS:
                    await socket_handler.emit_to_user(
                        user_id,
                        "download:progress",
                        {
                            "id": item.id,
                            "file_name": item.file_name,
                            "progress": 0.0,
                            "status": "cancelled",
                        },
                    )
                    continue
                await socket_handler.emit_to_user(
                    user_id,
                    "download:progress",
                    {
                        "id": item.id,
                        "file_name": item.file_name,
                        "progress": 0.0,
                        "status": "downloading",
                    },
                )
                torrent_hash = _magnet_hash(item.magnet)
                magnet = item.magnet
                if "so=" not in magnet:
                    magnet = f"{magnet}&so={item.so_id}"

                add = await client.post(
                    "/api/v2/torrents/add",
                    data={
                        "urls": magnet,
                        "savepath": str(job_dir),
                        "paused": "true",
                        "autoTMM": "false",
                    },
                )
                if add.status_code != 200:
                    log.error("qBittorrent add failed: %s", add.status_code)
                    await socket_handler.emit_to_user(
                        user_id,
                        "download:progress",
                        {
                            "id": item.id,
                            "file_name": item.file_name,
                            "progress": 0.0,
                            "status": "error",
                        },
                    )
                    continue

                # Wait for torrent metadata to become available before selecting
                # files. qBittorrent can sit in "Downloading metadata" until
                # peers are found; without this wait the file list is empty and
                # the download stalls.
                selected_index = None
                torrent_files: list[dict[str, Any]] = []
                for _ in range(max(1, cfg.metadata_timeout_seconds // 2)):
                    files_resp = await client.get(
                        "/api/v2/torrents/files", params={"hash": torrent_hash}
                    )
                    if files_resp.status_code == 200:
                        torrent_files = files_resp.json() or []
                        for f in torrent_files:
                            if str(f.get("name", "")).endswith(item.file_name):
                                selected_index = f.get("index")
                                break
                        if selected_index is not None:
                            break
                    await asyncio.sleep(2)
                if selected_index is None:
                    log.error(
                        "Could not locate file in torrent metadata: %s",
                        item.file_name,
                    )
                    await socket_handler.emit_to_user(
                        user_id,
                        "download:progress",
                        {
                            "id": item.id,
                            "file_name": item.file_name,
                            "progress": 0.0,
                            "status": "error",
                        },
                    )
                    continue

                # Select only the requested file to avoid downloading the whole torrent.
                other_ids = [
                    str(f.get("index"))
                    for f in torrent_files
                    if f.get("index") != selected_index
                ]
                if other_ids:
                    await client.post(
                        "/api/v2/torrents/filePrio",
                        data={
                            "hash": torrent_hash,
                            "id": "|".join(other_ids),
                            "priority": "0",
                        },
                    )
                await client.post(
                    "/api/v2/torrents/filePrio",
                    data={
                        "hash": torrent_hash,
                        "id": str(selected_index),
                        "priority": "1",
                    },
                )
                resume = await client.post(
                    "/api/v2/torrents/resume", data={"hashes": torrent_hash}
                )
                if resume.status_code == 404:
                    await client.post(
                        "/api/v2/torrents/start", data={"hashes": torrent_hash}
                    )

                cancelled = False
                for _ in range(720):
                    await asyncio.sleep(5)
                    if {str(item.id), str(item.file_name)} & _CANCELLED_DOWNLOAD_KEYS:
                        cancelled = True
                        break
                    selected_progress = 0.0
                    files_info = await client.get(
                        "/api/v2/torrents/files", params={"hash": torrent_hash}
                    )
                    if files_info.status_code == 200:
                        for f in files_info.json():
                            if str(f.get("name", "")).endswith(item.file_name):
                                selected_progress = float(f.get("progress", 0))
                                log.info(
                                    "qBittorrent selected progress: %.2f for %s",
                                    selected_progress,
                                    item.file_name,
                                )
                                break
                    await socket_handler.emit_to_user(
                        user_id,
                        "download:progress",
                        {
                            "id": item.id,
                            "file_name": item.file_name,
                            "progress": selected_progress,
                            "status": "downloading",
                        },
                    )
                    if selected_progress >= 1:
                        break
                    info = await client.get(
                        "/api/v2/torrents/info", params={"hashes": torrent_hash}
                    )
                    data = info.json() if info.status_code == 200 else []
                    if data and "error" in str(data[0].get("state", "")).lower():
                        break
                if cancelled:
                    try:
                        await client.post(
                            "/api/v2/torrents/delete",
                            data={"hashes": torrent_hash, "deleteFiles": "true"},
                        )
                    except Exception:  # noqa: BLE001
                        pass
                    await socket_handler.emit_to_user(
                        user_id,
                        "download:progress",
                        {
                            "id": item.id,
                            "file_name": item.file_name,
                            "progress": 0.0,
                            "status": "cancelled",
                        },
                    )
                    continue
                if not cancelled and selected_progress < 1:
                    log.error("qBittorrent download timed out for %s", item.file_name)
                    await socket_handler.emit_to_user(
                        user_id,
                        "download:progress",
                        {
                            "id": item.id,
                            "file_name": item.file_name,
                            "progress": 0.0,
                            "status": "error",
                        },
                    )
                    continue

                # Wait for the selected file to be fully flushed to disk.
                found = None
                for _ in range(60):
                    for path in job_dir.rglob(item.file_name):
                        try:
                            size = path.stat().st_size
                            if item.size is None or size >= item.size or size > 0:
                                found = path
                                break
                        except OSError:
                            pass
                    if found:
                        break
                    await asyncio.sleep(1)
                if found is None:
                    log.error(
                        "Selected file did not finish writing: %s", item.file_name
                    )
                    await socket_handler.emit_to_user(
                        user_id,
                        "download:progress",
                        {
                            "id": item.id,
                            "file_name": item.file_name,
                            "progress": 0.0,
                            "status": "error",
                        },
                    )
                    continue
                if found is None:
                    log.error("Downloaded file not found: %s", item.file_name)
                    await socket_handler.emit_to_user(
                        user_id,
                        "download:progress",
                        {
                            "id": item.id,
                            "file_name": item.file_name,
                            "progress": 0.0,
                            "status": "error",
                        },
                    )
                    continue

                target_dir = library_path / "roms" / platform_fs_slug
                target_dir.mkdir(parents=True, exist_ok=True)
                shutil.move(str(found), str(target_dir / item.file_name))
                await client.post(
                    "/api/v2/torrents/delete",
                    data={"hashes": torrent_hash, "deleteFiles": "true"},
                )
                shutil.rmtree(job_dir, ignore_errors=True)
                await socket_handler.emit_to_user(
                    user_id,
                    "download:progress",
                    {
                        "id": item.id,
                        "file_name": item.file_name,
                        "progress": 1.0,
                        "status": "done",
                    },
                )
                log.info("Downloaded %s -> %s", item.file_name, target_dir)
            except Exception as exc:  # noqa: BLE001
                log.exception("Minerva download failed: %s", exc)
                await socket_handler.emit_to_user(
                    user_id,
                    "download:progress",
                    {
                        "id": item.id,
                        "file_name": item.file_name,
                        "progress": 0.0,
                        "status": "error",
                    },
                )

    if cfg.scan_after_download:
        try:
            from endpoints.sockets.scan import ScanPayload, queue_scan

            await queue_scan(
                ScanPayload(platform_fs_slugs=[platform_fs_slug]),
                started_by_user_id=user_id,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("Automatic scan could not be queued: %s", exc)


@protected_route(
    router.post,
    "/minerva/download",
    [Scope.ROMS_WRITE],
    status_code=202,
)
async def minerva_download(
    request: Request,
    payload: MinervaDownloadRequest,
    background_tasks: BackgroundTasks,
) -> dict[str, int]:
    from config.config_manager import config_manager as cm

    if not cm.get_config().DOWNLOAD.enabled:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Downloads are disabled",
        )
    for item in payload.items:
        if not item.file_name:
            item.file_name = item.full_path.rsplit("/", 1)[-1]
    user_id = request.user.id
    queue = _user_download_queue(user_id)
    progress = _user_download_progress(user_id)
    for item in payload.items:
        fn = item.file_name or item.full_path.rsplit("/", 1)[-1]
        if not any(q.get("id") == item.id and q.get("file_name") == fn for q in queue):
            queue.append(
                {
                    "id": item.id,
                    "file_name": fn,
                    "full_path": item.full_path,
                    "size": item.size,
                    "cover_url": item.cover_url,
                    "console": item.console,
                }
            )
        progress[str(fn)] = {"progress": 0.0, "status": "queued"}
    background_tasks.add_task(
        _run_minerva_batch, payload.items, payload.platform_fs_slug, user_id
    )
    return {"scheduled": len(payload.items)}


class MinervaCancelRequest(BaseModel):
    ids: list[int] = []
    file_names: list[str] = []


class MinervaConnectionTestPayload(BaseModel):
    qbittorrent_url: str | None = None
    qbittorrent_username: str | None = None
    qbittorrent_password: str | None = None


@protected_route(
    router.post,
    "/minerva/cancel",
    [Scope.ROMS_WRITE],
)
async def minerva_cancel(
    request: Request, payload: MinervaCancelRequest
) -> dict[str, int]:
    user_id = request.user.id
    cancelled = 0
    queue = _user_download_queue(user_id)
    progress = _user_download_progress(user_id)
    keys = {str(i) for i in (payload.ids or []) if i is not None}
    keys.update(n for n in (payload.file_names or []) if n)
    for key in keys:
        _CANCELLED_DOWNLOAD_KEYS.add(key)
        cancelled += 1
    # Drop cancelled entries from the persisted queue immediately.
    queue[:] = [
        q
        for q in queue
        if str(q.get("id")) not in keys and str(q.get("file_name")) not in keys
    ]
    for key in keys:
        progress.pop(key, None)
    return {"cancelled": cancelled}


@protected_route(
    router.get,
    "/minerva/queue",
    [Scope.ROMS_READ],
)
async def minerva_queue(request: Request) -> dict[str, Any]:
    user_id = request.user.id
    progress = _user_download_progress(user_id)
    queue = [
        q
        for q in _user_download_queue(user_id)
        if progress.get(str(q.get("file_name")), {}).get("status") != "done"
    ]
    return {"queue": queue, "progress": progress}


@protected_route(
    router.post,
    "/minerva/test-connection",
    [Scope.ROMS_WRITE],
)
async def minerva_test_connection(
    request: Request, payload: MinervaConnectionTestPayload
) -> dict[str, bool]:
    from config.config_manager import config_manager as cm

    cfg = cm.get_config().DOWNLOAD
    base = payload.qbittorrent_url or cfg.qbittorrent_url or "http://127.0.0.1:8080"
    username = payload.qbittorrent_username or cfg.qbittorrent_username or "admin"
    password = payload.qbittorrent_password or cfg.qbittorrent_password or "adminadmin"
    async with httpx.AsyncClient(
        base_url=base,
        timeout=15.0,
        headers={
            "Referer": f"{base}/",
            "Origin": base,
            "X-Requested-With": "XMLHttpRequest",
        },
    ) as client:
        login = await client.post(
            "/api/v2/auth/login",
            data={"username": username, "password": password},
        )
        if login.status_code not in (200, 204):
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="qBittorrent login failed",
            )
    return {"ok": True}


@protected_route(
    router.get,
    "/minerva/rom",
    [Scope.ROMS_READ],
)
async def minerva_rom(request: Request, id: int) -> dict[str, Any]:
    """Fetch a single Minerva ROM page and extract its JSON payload."""
    import json
    import re

    async with httpx.AsyncClient(base_url=MINERVA_BASE_URL, timeout=120.0) as client:
        response = await client.get("/rom", params={"id": id})
    if response.status_code != 200:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Minerva archive unavailable",
        )
    match = re.search(r"window\.rom\s*=\s*(\{.*?\});", response.text, re.S)
    if not match:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Could not parse Minerva ROM",
        )
    payload = json.loads(match.group(1))
    try:
        title = payload.get("file_name") or payload.get("full_path", "")
        if "." in title:
            title = title.rsplit(".", 1)[0]
        title = title.replace("_", " ")
        # Strip parenthetical regions/tags SteamGridDB won't match on.
        import re

        title = re.sub(r"\s*\([^)]*\)", "", title).strip()
        log.info("SteamGridDB lookup for Minerva ROM: %s", title)
        if title and meta_sgdb_handler.is_enabled():
            cover_results = await meta_sgdb_handler.get_details(title)
            cover_url = None
            for result in cover_results:
                for resource in result.get("resources", []):
                    if resource.get("url"):
                        cover_url = resource["url"]
                        break
                if cover_url:
                    break
            log.info("SteamGridDB cover result: %s", cover_url)
            if cover_url:
                payload["cover_url"] = cover_url
    except Exception as exc:  # noqa: BLE001
        log.warning("SteamGridDB cover lookup failed: %s", exc)
    return payload

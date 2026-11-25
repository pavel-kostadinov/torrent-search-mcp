"""Minimal Synology Download Station client.

Adds magnet links as Download Station tasks through the DSM Web API, so the
web UI can hand a torrent to the NAS in one click. Configured from env vars;
when SYNOLOGY_URL is unset the feature is disabled.
"""

import json
import logging
from asyncio import Lock
from dataclasses import dataclass
from os import getenv
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# Session-related error codes shared by every DSM API: the sid expired or was
# invalidated, so log in again and retry once.
_SESSION_ERRORS = {105, 106, 107, 119}

_AUTH_ERRORS = {
    400: "wrong NAS username or password",
    401: "NAS account is disabled",
    402: "NAS account lacks permission",
    403: "NAS account requires 2-factor authentication (use a dedicated account without 2FA)",
    404: "2-factor authentication code failed",
    406: "NAS account must enable 2-factor authentication",
    407: "this IP is blocked by the NAS (too many failed logins)",
    408: "NAS password expired",
    409: "NAS password expired",
    410: "NAS password must be changed",
}

_TASK_ERRORS = {
    400: "Download Station rejected the task",
    401: "Download Station has too many tasks",
    402: "no permission for the destination folder",
    403: "destination folder does not exist",
    406: "no default destination set in Download Station settings",
    407: "could not set the destination folder",
}

_COMMON_ERRORS = {
    100: "unknown NAS error",
    102: "Download Station API not found - is Download Station installed?",
    103: "Download Station method not found",
    104: "Download Station API version not supported",
    105: "insufficient user privilege for Download Station",
}


@dataclass(frozen=True)
class DownloadTask:
    id: str
    title: str
    status: str
    size: int
    downloaded: int
    uploaded: int
    speed_down: int
    speed_up: int
    destination: str
    created: int

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> "DownloadTask":
        extra = raw.get("additional") or {}
        detail = extra.get("detail") or {}
        transfer = extra.get("transfer") or {}

        def num(value: Any) -> int:
            try:
                return int(value or 0)
            except (TypeError, ValueError):
                return 0

        return cls(
            id=str(raw.get("id", "")),
            title=str(raw.get("title", "")),
            status=str(raw.get("status", "")),
            size=num(raw.get("size")),
            downloaded=num(transfer.get("size_downloaded")),
            uploaded=num(transfer.get("size_uploaded")),
            speed_down=num(transfer.get("speed_download")),
            speed_up=num(transfer.get("speed_upload")),
            destination=str(detail.get("destination", "")),
            created=num(detail.get("create_time")),
        )


class SynologyError(Exception):
    """Raised when the NAS rejects a request; the message is user-facing."""


@dataclass(frozen=True)
class SynologyConfig:
    url: str
    username: str
    password: str
    destination: str | None
    verify_ssl: bool

    @classmethod
    def from_env(cls) -> "SynologyConfig | None":
        url = (getenv("SYNOLOGY_URL") or "").strip().rstrip("/")
        if not url:
            return None
        username = getenv("SYNOLOGY_USERNAME") or ""
        password = getenv("SYNOLOGY_PASSWORD") or ""
        if not username or not password:
            logger.warning(
                "SYNOLOGY_URL is set but SYNOLOGY_USERNAME/SYNOLOGY_PASSWORD are missing; NAS downloads disabled."
            )
            return None
        destination = (getenv("SYNOLOGY_DESTINATION") or "").strip().strip("/") or None
        verify_ssl = (getenv("SYNOLOGY_VERIFY_SSL") or "true").strip().lower() not in {
            "0",
            "false",
            "no",
        }
        return cls(url, username, password, destination, verify_ssl)


class SynologyDownloadStation:
    def __init__(
        self, config: SynologyConfig, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self.config = config
        self._transport = transport
        self._sid: str | None = None
        self._paths: dict[str, str] = {}
        self._lock = Lock()

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=f"{self.config.url}/webapi/",
            verify=self.config.verify_ssl,
            timeout=20.0,
            transport=self._transport,
        )

    async def _call(
        self,
        client: httpx.AsyncClient,
        path: str,
        params: dict[str, Any],
        post: bool = False,
        files: dict[str, tuple[str, bytes, str]] | None = None,
    ) -> dict[str, Any]:
        try:
            if post:
                # Multipart when uploading: DSM wants the file part last,
                # which httpx does (form fields first, then files).
                res = await client.post(path, data=params, files=files)
            else:
                res = await client.get(path, params=params)
            res.raise_for_status()
            body = res.json()
        except httpx.HTTPError as exc:
            raise SynologyError(
                f"cannot reach the NAS at {self.config.url} ({exc.__class__.__name__})"
            ) from exc
        except ValueError as exc:
            raise SynologyError("unexpected response from the NAS") from exc
        if not isinstance(body, dict):
            raise SynologyError("unexpected response from the NAS")
        return body

    async def _discover(self, client: httpx.AsyncClient) -> None:
        if self._paths:
            return
        body = await self._call(
            client,
            "query.cgi",
            {
                "api": "SYNO.API.Info",
                "version": "1",
                "method": "query",
                "query": "SYNO.API.Auth,SYNO.DownloadStation.Task,SYNO.DownloadStation2.Task",
            },
        )
        data = body.get("data") or {}
        if "SYNO.DownloadStation.Task" not in data:
            raise SynologyError(_COMMON_ERRORS[102])
        self._paths = {api: info["path"] for api, info in data.items()}

    async def _login(self, client: httpx.AsyncClient) -> str:
        body = await self._call(
            client,
            self._paths.get("SYNO.API.Auth", "auth.cgi"),
            {
                "api": "SYNO.API.Auth",
                "version": "3",
                "method": "login",
                "account": self.config.username,
                "passwd": self.config.password,
                "session": "DownloadStation",
                "format": "sid",
            },
            post=True,
        )
        if not body.get("success"):
            code = (body.get("error") or {}).get("code")
            raise SynologyError(
                _AUTH_ERRORS.get(code)
                or _COMMON_ERRORS.get(code)
                or f"NAS login failed (code {code})"
            )
        return body["data"]["sid"]

    async def _task_api(
        self,
        method: str,
        files: dict[str, tuple[str, bytes, str]] | None = None,
        api: str = "SYNO.DownloadStation.Task",
        version: str = "1",
        **params: str,
    ) -> dict[str, Any]:
        """Call a Download Station task API, logging in (again) as needed."""
        async with self._lock, self._client() as client:
            await self._discover(client)
            if not self._sid:
                self._sid = await self._login(client)
            if api not in self._paths:
                raise SynologyError(
                    f"{api} is not available on this Download Station version"
                )
            path = self._paths[api]
            base = {"api": api, "version": version, "method": method}
            body = await self._call(
                client,
                path,
                {**base, **params, "_sid": self._sid},
                post=True,
                files=files,
            )
            code = (body.get("error") or {}).get("code")
            if not body.get("success") and code in _SESSION_ERRORS:
                self._sid = await self._login(client)
                body = await self._call(
                    client,
                    path,
                    {**base, **params, "_sid": self._sid},
                    post=True,
                    files=files,
                )
                code = (body.get("error") or {}).get("code")
            if not body.get("success"):
                raise SynologyError(
                    _TASK_ERRORS.get(code)
                    or _COMMON_ERRORS.get(code)
                    or f"Download Station error (code {code})"
                )
            data = body.get("data")
            return data if isinstance(data, dict) else {}

    async def check(self) -> None:
        """Verify the NAS is reachable and the credentials work."""
        await self._task_api("list", offset="0", limit="1")

    async def add_magnet(self, magnet: str) -> None:
        """Create a Download Station task for the magnet link."""
        if not magnet.startswith("magnet:?"):
            raise SynologyError("only magnet links can be sent to the NAS")
        await self._task_api(
            "create",
            uri=magnet,
            **(
                {"destination": self.config.destination}
                if self.config.destination
                else {}
            ),
        )

    async def add_torrent_file(self, filename: str, data: bytes) -> None:
        """Create a task by uploading a .torrent file.

        Download Station 4.x rejects uploads on the v1 API (error 101), so
        this uses the (undocumented) SYNO.DownloadStation2.Task v2 API, which
        takes JSON-encoded form values and the file in a "torrent" part.
        """
        if not filename.endswith(".torrent"):
            filename += ".torrent"  # Download Station rejects other extensions
        await self._task_api(
            "create",
            files={"torrent": (filename, data, "application/x-bittorrent")},
            api="SYNO.DownloadStation2.Task",
            version="2",
            type=json.dumps("file"),
            file=json.dumps(["torrent"]),
            destination=json.dumps(self.config.destination or ""),
            create_list="false",
        )

    async def list_tasks(self) -> list[DownloadTask]:
        """List this account's Download Station tasks."""
        data = await self._task_api(
            "list", offset="0", limit="-1", additional="detail,transfer"
        )
        return [
            DownloadTask.from_api(t)
            for t in data.get("tasks") or []
            if isinstance(t, dict)
        ]

    async def get_task(self, task_id: str) -> DownloadTask | None:
        return next((t for t in await self.list_tasks() if t.id == task_id), None)

    async def pause(self, task_id: str) -> None:
        await self._task_api("pause", id=task_id)

    async def resume(self, task_id: str) -> None:
        await self._task_api("resume", id=task_id)

    async def delete(self, task_id: str) -> None:
        """Remove the task from Download Station (downloaded files stay on disk)."""
        await self._task_api("delete", id=task_id, force_complete="false")

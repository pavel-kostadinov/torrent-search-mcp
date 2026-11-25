"""Synology Download Station client, library and /nas + /library endpoint tests."""

from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest
from fastapi.testclient import TestClient

from torrent_search import fastapi_server
from torrent_search.library import Library, LibraryError
from torrent_search.synology import (
    DownloadTask,
    SynologyConfig,
    SynologyDownloadStation,
    SynologyError,
)

MAGNET = f"magnet:?xt=urn:btih:{'a' * 40}&dn=x"


class FakeNas:
    """Stateful stand-in for the DSM Web API."""

    def __init__(
        self, login_error: int | None = None, task_errors: list[int] | None = None
    ) -> None:
        self.login_error = login_error
        self.task_errors = list(task_errors or [])
        self.logins = 0
        self.tasks: list[dict[str, str]] = []
        self.calls: list[dict[str, str]] = []
        self.uploads: list[bytes] = []
        self.listing: list[dict[str, Any]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        params = (
            {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
            if request.method == "POST"
            else dict(request.url.params)
        )
        if path == "/webapi/query.cgi":
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "data": {
                        "SYNO.API.Auth": {"path": "entry.cgi", "maxVersion": 7},
                        "SYNO.DownloadStation.Task": {
                            "path": "DownloadStation/task.cgi",
                            "maxVersion": 3,
                        },
                        "SYNO.DownloadStation2.Task": {
                            "path": "entry.cgi",
                            "maxVersion": 2,
                        },
                    },
                },
            )
        if b"SYNO.DownloadStation2.Task" in request.content:  # multipart upload
            self.uploads.append(request.content)
            return httpx.Response(
                200, json={"success": True, "data": {"task_id": ["dbid_9"]}}
            )
        if path == "/webapi/entry.cgi" and params.get("method") == "login":
            self.logins += 1
            if self.login_error:
                return httpx.Response(
                    200, json={"success": False, "error": {"code": self.login_error}}
                )
            return httpx.Response(
                200, json={"success": True, "data": {"sid": f"sid{self.logins}"}}
            )
        if path == "/webapi/DownloadStation/task.cgi":
            if self.task_errors:
                return httpx.Response(
                    200,
                    json={"success": False, "error": {"code": self.task_errors.pop(0)}},
                )
            self.calls.append(params)
            if params.get("method") == "list":
                return httpx.Response(
                    200,
                    json={
                        "success": True,
                        "data": {"total": len(self.listing), "tasks": self.listing},
                    },
                )
            if params.get("method") == "create":
                self.tasks.append(params)
            return httpx.Response(200, json={"success": True})
        return httpx.Response(404)


def _station(nas: FakeNas, destination: str | None = None) -> SynologyDownloadStation:
    config = SynologyConfig("http://nas.test:5000", "user", "pw", destination, True)
    return SynologyDownloadStation(config, transport=httpx.MockTransport(nas.handler))


async def test_add_magnet_creates_task() -> None:
    nas = FakeNas()
    await _station(nas, destination="downloads/tv").add_magnet(MAGNET)
    assert nas.logins == 1
    assert nas.tasks == [
        {
            "api": "SYNO.DownloadStation.Task",
            "version": "1",
            "method": "create",
            "uri": MAGNET,
            "_sid": "sid1",
            "destination": "downloads/tv",
        }
    ]


async def test_session_reused_and_refreshed_on_expiry() -> None:
    nas = FakeNas()
    station = _station(nas)
    await station.add_magnet(MAGNET)
    await station.add_magnet(MAGNET)
    assert nas.logins == 1
    nas.task_errors = [106]  # session timeout
    await station.add_magnet(MAGNET)
    assert nas.logins == 2
    assert len(nas.tasks) == 3
    assert nas.tasks[-1]["_sid"] == "sid2"


async def test_login_failure_is_reported() -> None:
    with pytest.raises(SynologyError, match="wrong NAS username or password"):
        await _station(FakeNas(login_error=400)).add_magnet(MAGNET)


async def test_task_failure_is_reported() -> None:
    with pytest.raises(SynologyError, match="destination folder does not exist"):
        await _station(FakeNas(task_errors=[403])).add_magnet(MAGNET)


async def test_rejects_non_magnet() -> None:
    nas = FakeNas()
    with pytest.raises(SynologyError, match="only magnet links"):
        await _station(nas).add_magnet("http://evil.test/file.torrent")
    assert nas.logins == 0


def test_config_from_env(monkeypatch: Any) -> None:
    for key in (
        "SYNOLOGY_URL",
        "SYNOLOGY_USERNAME",
        "SYNOLOGY_PASSWORD",
        "SYNOLOGY_DESTINATION",
        "SYNOLOGY_VERIFY_SSL",
    ):
        monkeypatch.delenv(key, raising=False)
    assert SynologyConfig.from_env() is None
    monkeypatch.setenv("SYNOLOGY_URL", "https://nas:5001/")
    assert SynologyConfig.from_env() is None  # credentials missing
    monkeypatch.setenv("SYNOLOGY_USERNAME", "u")
    monkeypatch.setenv("SYNOLOGY_PASSWORD", "p")
    monkeypatch.setenv("SYNOLOGY_DESTINATION", "/downloads/")
    monkeypatch.setenv("SYNOLOGY_VERIFY_SSL", "false")
    assert SynologyConfig.from_env() == SynologyConfig(
        "https://nas:5001", "u", "p", "downloads", False
    )


def test_endpoint_not_configured(monkeypatch: Any) -> None:
    monkeypatch.setattr(fastapi_server, "synology", None)
    response = TestClient(fastapi_server.app).post(
        "/nas/download", json={"magnet": MAGNET}
    )
    assert response.status_code == 404


def test_endpoint_success_and_error(monkeypatch: Any) -> None:
    nas = FakeNas()
    monkeypatch.setattr(fastapi_server, "synology", _station(nas))
    client = TestClient(fastapi_server.app)
    response = client.post("/nas/download", json={"magnet": MAGNET})
    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert len(nas.tasks) == 1
    nas.task_errors = [401]
    response = client.post("/nas/download", json={"magnet": MAGNET})
    assert response.status_code == 502
    assert response.json()["detail"] == "Download Station has too many tasks"


RAW_TASK = {
    "id": "dbid_1",
    "title": "Some.Show.S01E01.1080p",
    "status": "downloading",
    "size": "1000",
    "additional": {
        "detail": {"destination": "downloads/tv", "create_time": "1700000000"},
        "transfer": {
            "size_downloaded": "250",
            "size_uploaded": "10",
            "speed_download": "50",
            "speed_upload": "5",
        },
    },
}


async def test_list_tasks_parses_progress() -> None:
    nas = FakeNas()
    nas.listing = [RAW_TASK, {"id": "dbid_2", "title": "bare", "status": "waiting"}]
    tasks = await _station(nas).list_tasks()
    assert tasks[0] == DownloadTask(
        "dbid_1",
        "Some.Show.S01E01.1080p",
        "downloading",
        1000,
        250,
        10,
        50,
        5,
        "downloads/tv",
        1700000000,
    )
    assert tasks[1].size == 0 and tasks[1].destination == ""
    assert nas.calls[-1]["additional"] == "detail,transfer"


async def test_pause_resume_delete_send_task_id() -> None:
    nas = FakeNas()
    station = _station(nas)
    await station.pause("dbid_1")
    await station.resume("dbid_1")
    await station.delete("dbid_1")
    assert [(c["method"], c["id"]) for c in nas.calls] == [
        ("pause", "dbid_1"),
        ("resume", "dbid_1"),
        ("delete", "dbid_1"),
    ]
    assert nas.calls[-1]["force_complete"] == "false"


# ---- Library ---------------------------------------------------------------


def test_library_lists_and_deletes(tmp_path: Any) -> None:
    import asyncio

    (tmp_path / "Movie (2026)").mkdir()
    (tmp_path / "Movie (2026)" / "movie.mkv").write_bytes(b"x" * 100)
    (tmp_path / "single.mkv").write_bytes(b"y" * 10)
    (tmp_path / "@eaDir").mkdir()
    (tmp_path / "#recycle").mkdir()
    lib = Library(tmp_path)
    items = {i.name: i for i in asyncio.run(lib.list())}
    assert set(items) == {"Movie (2026)", "single.mkv"}
    assert items["Movie (2026)"].is_dir and items["Movie (2026)"].size == 100
    asyncio.run(lib.delete("Movie (2026)"))
    asyncio.run(lib.delete("single.mkv"))
    assert sorted(p.name for p in tmp_path.iterdir()) == ["#recycle", "@eaDir"]


@pytest.mark.parametrize(
    "name", ["..", ".", "", "a/b", "../x", "@eaDir", "#recycle", ".hidden"]
)
def test_library_refuses_escape(tmp_path: Any, name: str) -> None:
    import asyncio

    root = tmp_path / "root"
    root.mkdir()
    (tmp_path / "x").write_text("outside")
    with pytest.raises(LibraryError):
        asyncio.run(Library(root).delete(name))
    assert (tmp_path / "x").exists()


def test_library_symlink_only_unlinks(tmp_path: Any) -> None:
    import asyncio

    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep")
    (root / "link").symlink_to(outside)
    asyncio.run(Library(root).delete("link"))
    assert (outside / "keep.txt").exists()
    assert not (root / "link").exists()


def test_library_unmounted(tmp_path: Any) -> None:
    import asyncio

    with pytest.raises(LibraryError, match="not mounted"):
        asyncio.run(Library(tmp_path / "missing").list())


# ---- Task + library endpoints ---------------------------------------------


@pytest.fixture
def nas_client(monkeypatch: Any, tmp_path: Any) -> tuple[TestClient, FakeNas, Any]:
    nas = FakeNas()
    nas.listing = [RAW_TASK]
    monkeypatch.setattr(
        fastapi_server, "synology", _station(nas, destination="downloads/tv")
    )
    monkeypatch.setattr(fastapi_server, "library", Library(tmp_path))
    return TestClient(fastapi_server.app), nas, tmp_path


def test_status_endpoint(nas_client: Any, monkeypatch: Any) -> None:
    client, nas, _ = nas_client
    assert client.get("/nas/status").json() == {
        "configured": True,
        "connected": True,
        "library": True,
    }
    nas.login_error = 400
    fastapi_server.synology._sid = None
    body = client.get("/nas/status").json()
    assert body["connected"] is False and "password" in body["error"]
    monkeypatch.setattr(fastapi_server, "synology", None)
    assert client.get("/nas/status").json()["configured"] is False


def test_tasks_endpoints(nas_client: Any) -> None:
    client, nas, _ = nas_client
    body = client.get("/nas/tasks").json()
    assert body[0]["id"] == "dbid_1" and body[0]["downloaded"] == 250
    assert client.post("/nas/tasks/dbid_1/pause").json() == {"ok": True}
    assert client.post("/nas/tasks/dbid_1/resume").status_code == 200
    assert client.post("/nas/tasks/dbid_1/explode").status_code == 422
    assert nas.calls[-1]["method"] == "resume"


def test_delete_task_keeps_files_by_default(nas_client: Any) -> None:
    client, nas, root = nas_client
    (root / RAW_TASK["title"]).mkdir()
    assert client.delete("/nas/tasks/dbid_1").json() == {
        "ok": True,
        "files_deleted": False,
    }
    assert nas.calls[-1]["method"] == "delete"
    assert (root / RAW_TASK["title"]).exists()


def test_delete_task_with_files(nas_client: Any) -> None:
    client, _, root = nas_client
    (root / RAW_TASK["title"]).mkdir()
    (root / RAW_TASK["title"] / "ep.mkv").write_text("x")
    assert client.delete("/nas/tasks/dbid_1?delete_files=true").json() == {
        "ok": True,
        "files_deleted": True,
    }
    assert not (root / RAW_TASK["title"]).exists()


def test_delete_task_files_outside_managed_folder(nas_client: Any) -> None:
    client, nas, root = nas_client
    nas.listing = [
        {**RAW_TASK, "additional": {"detail": {"destination": "other/place"}}}
    ]
    (root / RAW_TASK["title"]).mkdir()
    response = client.delete("/nas/tasks/dbid_1?delete_files=true")
    assert response.status_code == 409
    assert (root / RAW_TASK["title"]).exists()
    assert all(c["method"] != "delete" for c in nas.calls)


def test_delete_unknown_task(nas_client: Any) -> None:
    client, _, _ = nas_client
    assert client.delete("/nas/tasks/nope").status_code == 404


def test_library_endpoints(nas_client: Any, monkeypatch: Any) -> None:
    client, _, root = nas_client
    (root / "film.mkv").write_bytes(b"z" * 7)
    assert [i["name"] for i in client.get("/library").json()] == ["film.mkv"]
    assert client.delete("/library/film.mkv").json() == {"ok": True}
    assert client.delete("/library/film.mkv").status_code == 400
    assert client.delete("/library/%2E%2E").status_code != 200
    monkeypatch.setattr(fastapi_server, "library", None)
    assert client.get("/library").status_code == 404


def test_library_protected_names(tmp_path: Any) -> None:
    import asyncio

    (tmp_path / "Movies").mkdir()
    (tmp_path / "series").mkdir()
    (tmp_path / "new.mkv").write_text("x")
    lib = Library(tmp_path, frozenset({"movies", "SERIES"}))
    assert [i.name for i in asyncio.run(lib.list())] == ["new.mkv"]
    for name in ("Movies", "movies", "series"):
        with pytest.raises(LibraryError, match="protected"):
            asyncio.run(lib.delete(name))
    assert (tmp_path / "Movies").exists() and (tmp_path / "series").exists()


def test_library_protect_from_env(monkeypatch: Any, tmp_path: Any) -> None:
    monkeypatch.setenv("LIBRARY_DIR", str(tmp_path))
    monkeypatch.setenv("LIBRARY_PROTECT", " movies, series ,")
    lib = Library.from_env()
    assert (
        lib is not None
        and lib.is_protected("Movies")
        and lib.is_protected("series")
        and not lib.is_protected("x")
    )


def test_delete_task_refuses_protected_folder(nas_client: Any) -> None:
    client, nas, root = nas_client
    nas.listing = [{**RAW_TASK, "title": "movies"}]
    fastapi_server.library.protected = frozenset({"movies"})
    (root / "movies").mkdir()
    assert client.delete("/nas/tasks/dbid_1?delete_files=true").status_code == 409
    assert (root / "movies").exists()
    assert all(c["method"] != "delete" for c in nas.calls)


async def test_add_torrent_file_uses_ds2_multipart() -> None:
    nas = FakeNas()
    await _station(nas, destination="video").add_torrent_file(
        "debian", b"d4:infod4:name1:xee"
    )
    (body,) = nas.uploads
    assert b'name="api"' in body and b"SYNO.DownloadStation2.Task" in body
    assert b'name="type"\r\n\r\n"file"' in body
    assert b'name="destination"\r\n\r\n"video"' in body
    assert b'name="torrent"; filename="debian.torrent"' in body
    assert body.index(b'name="torrent"') > body.index(b'name="_sid"')  # file part last

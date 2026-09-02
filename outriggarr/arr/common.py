"""HTTP plumbing and behaviour shared by the Sonarr and Radarr clients."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

import httpx

from outriggarr.arr.base import (
    ArrError,
    CommandStatus,
    ExtraFilesConfig,
    ImportCandidate,
    ImportFile,
    Language,
    QualityDefinition,
    SystemStatus,
    Target,
)

log = logging.getLogger(__name__)

PAGE_SIZE = 200
IMPORT_MODE = "move"


class ArrHttp:
    def __init__(self, base_url: str, api_key: str, http: httpx.AsyncClient) -> None:
        self._base = base_url.rstrip("/")
        self._headers = {"X-Api-Key": api_key}
        self._http = http

    # -- transport -------------------------------------------------------------------

    async def put(self, path: str, body: Any) -> Any:
        return await self._request("PUT", path, json=body)

    async def _request(self, method: str, path: str, **kw: Any) -> Any:
        url = f"{self._base}/api/v3/{path.lstrip('/')}"
        try:
            r = await self._http.request(method, url, headers=self._headers, **kw)
        except httpx.HTTPError as exc:
            raise ArrError(f"{method} {url}: {exc}") from exc
        if r.status_code >= 400:
            raise ArrError(f"{method} {url} -> HTTP {r.status_code}: {r.text}")
        try:
            return r.json()
        except ValueError as exc:
            raise ArrError(f"{method} {url}: non-JSON response: {r.text[:500]}") from exc

    async def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        return await self._request("GET", path, params=params)

    async def post(self, path: str, body: Any) -> Any:
        return await self._request("POST", path, json=body)

    async def get_all_pages(self, path: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        page = 1
        while True:
            data = await self.get(path, {**params, "page": page, "pageSize": PAGE_SIZE})
            batch = data.get("records", [])
            records.extend(batch)
            if not batch or len(records) >= data.get("totalRecords", 0):
                return records
            page += 1

    # -- shared endpoints ------------------------------------------------------------

    async def status(self) -> SystemStatus:
        data = await self.get("system/status")
        return SystemStatus(
            app_name=str(data.get("appName", "")), version=str(data.get("version", ""))
        )

    async def quality_definitions(self) -> list[QualityDefinition]:
        data = await self.get("qualitydefinition")
        return [
            QualityDefinition(
                id=int(d["id"]),
                quality_id=int(d["quality"]["id"]),
                name=str(d["quality"]["name"]),
                title=str(d.get("title") or d["quality"]["name"]),
                weight=int(d.get("weight", 0)),
            )
            for d in data
        ]

    async def path_visible(self, path: str) -> bool:
        # /filesystem returns an empty listing for a missing directory, which is
        # indistinguishable from an empty (e.g. fresh staging) directory. So list the
        # PARENT and look for the directory itself.
        target = path.rstrip("/")
        if not target:
            return False
        parent = target.rsplit("/", 1)[0] + "/"
        data = await self.get(
            "filesystem",
            {"path": parent, "includeFiles": "false", "allowFoldersWithoutTrailingSlashes": "true"},
        )
        return any(
            str(d.get("path", "")).rstrip("/") == target for d in data.get("directories", [])
        )

    async def extra_files_config(self) -> ExtraFilesConfig:
        data = await self.get("config/mediamanagement")
        exts = str(data.get("extraFileExtensions") or "")
        return ExtraFilesConfig(
            import_extra_files=bool(data.get("importExtraFiles")),
            extensions=tuple(e.strip().lower().lstrip(".") for e in exts.split(",") if e.strip()),
        )

    async def ensure_tag(self, label: str) -> int:
        for t in await self.get("tag"):
            if str(t.get("label", "")).lower() == label.lower():
                return int(t["id"])
        created = await self.post("tag", {"label": label})
        return int(created["id"])

    async def command(self, command_id: int) -> CommandStatus:
        data = await self.get(f"command/{command_id}")
        return CommandStatus(
            id=int(data["id"]),
            name=str(data.get("name", "")),
            status=str(data.get("status", "")),
            message=data.get("message"),
        )

    async def manual_import_candidates(self, folder: str) -> list[ImportCandidate]:
        # Proven live 2026-09-01: adding seriesId (Sonarr) / movieId (Radarr) makes the
        # *arr list the existing series/movie folder and ignore `folder` entirely.
        data = await self.get("manualimport", {"folder": folder, "filterExistingFiles": "true"})
        return [_candidate(d) for d in data]

    async def manual_import(self, files: list[ImportFile]) -> int:
        definitions = await self.quality_definitions()
        by_name = {q.name: q for q in definitions}
        payload = []
        for f in files:
            q = by_name.get(f.quality_name)
            if q is None:
                raise ArrError(
                    f"quality {f.quality_name!r} is not defined on this server "
                    f"(known: {sorted(by_name)})"
                )
            entry: dict[str, Any] = {
                "path": f.path,
                "quality": {
                    "quality": {"id": q.quality_id, "name": q.name},
                    "revision": {"version": 1, "real": 0, "isRepack": False},
                },
                "languages": [{"id": lang.id, "name": lang.name} for lang in f.languages],
            }
            entry.update(self._import_ids(f.target))
            payload.append(entry)
        data = await self.post(
            "command", {"name": "ManualImport", "files": payload, "importMode": IMPORT_MODE}
        )
        return int(data["id"])

    # -- per-kind hooks ---------------------------------------------------------------

    def _import_ids(self, target: Target) -> dict[str, Any]:  # pragma: no cover
        raise NotImplementedError


def _candidate(d: dict[str, Any]) -> ImportCandidate:
    return ImportCandidate(
        path=str(d.get("path", "")),
        relative_path=str(d.get("relativePath", "")),
        name=str(d.get("name", "")),
        size=int(d.get("size", 0) or 0),
        rejections=tuple(str(r.get("reason", "")) for r in d.get("rejections", []) or []),
        languages=tuple(
            Language(int(lang["id"]), str(lang.get("name", "")))
            for lang in d.get("languages", []) or []
        ),
    )


def parse_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        log.warning("unparseable datetime from *arr: %r", value)
        return None

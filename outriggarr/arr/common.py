"""HTTP plumbing shared by the Sonarr and Radarr clients."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

import httpx

from outriggarr.arr.base import ArrError, QualityDefinition, SystemStatus

log = logging.getLogger(__name__)

PAGE_SIZE = 200


class ArrHttp:
    def __init__(self, base_url: str, api_key: str, http: httpx.AsyncClient) -> None:
        self._base = base_url.rstrip("/")
        self._headers = {"X-Api-Key": api_key}
        self._http = http

    async def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        url = f"{self._base}/api/v3/{path.lstrip('/')}"
        try:
            r = await self._http.get(url, params=params, headers=self._headers)
        except httpx.HTTPError as exc:
            raise ArrError(f"GET {url}: {exc}") from exc
        if r.status_code >= 400:
            raise ArrError(f"GET {url} -> HTTP {r.status_code}: {r.text}")
        try:
            return r.json()
        except ValueError as exc:
            raise ArrError(f"GET {url}: non-JSON response: {r.text[:500]}") from exc

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


def parse_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        log.warning("unparseable datetime from *arr: %r", value)
        return None

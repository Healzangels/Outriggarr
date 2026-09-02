from __future__ import annotations

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import func, select

from outriggarr.api.deps import ArrFactoryDep, DbSession
from outriggarr.arr.base import ArrError
from outriggarr.db.models import Connection, ConnectionKind, Job
from outriggarr.settings import get_setting

router = APIRouter(prefix="/api/connections", tags=["connections"])


class ConnectionIn(BaseModel):
    kind: ConnectionKind
    name: str = Field(min_length=1, max_length=100)
    url: str = Field(min_length=1, max_length=500)
    api_key: str = Field(min_length=1, max_length=200)
    staging_path_remote: str = Field(min_length=1, max_length=500)
    enabled: bool = True

    @field_validator("url")
    @classmethod
    def _url(cls, v: str) -> str:
        v = v.strip().rstrip("/")
        if not (v.startswith("http://") or v.startswith("https://")):
            raise ValueError("url must start with http:// or https://")
        return v

    @field_validator("staging_path_remote")
    @classmethod
    def _path(cls, v: str) -> str:
        v = v.strip()
        if not v.startswith("/"):
            raise ValueError("staging_path_remote must be an absolute path as the *arr sees it")
        return v.rstrip("/") or "/"


class ConnectionOut(ConnectionIn):
    model_config = ConfigDict(from_attributes=True)
    id: int


class ConnectionTestResult(BaseModel):
    ok: bool
    app_name: str | None = None
    version: str | None = None
    staging_visible: bool | None = None
    error: str | None = None
    warning: str | None = None  # non-fatal: e.g. subtitles would not be imported


def _get_or_404(session: DbSession, connection_id: int) -> Connection:
    conn = session.get(Connection, connection_id)
    if conn is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"connection {connection_id} not found")
    return conn


@router.get("", response_model=list[ConnectionOut])
def list_connections(session: DbSession) -> list[Connection]:
    return list(session.scalars(select(Connection).order_by(Connection.id)))


@router.post("", response_model=ConnectionOut, status_code=status.HTTP_201_CREATED)
def create_connection(body: ConnectionIn, session: DbSession) -> Connection:
    conn = Connection(**body.model_dump())
    session.add(conn)
    session.commit()
    return conn


@router.get("/{connection_id}", response_model=ConnectionOut)
def get_connection(connection_id: int, session: DbSession) -> Connection:
    return _get_or_404(session, connection_id)


@router.put("/{connection_id}", response_model=ConnectionOut)
def update_connection(connection_id: int, body: ConnectionIn, session: DbSession) -> Connection:
    conn = _get_or_404(session, connection_id)
    for k, v in body.model_dump().items():
        setattr(conn, k, v)
    session.commit()
    return conn


@router.delete("/{connection_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_connection(connection_id: int, session: DbSession) -> None:
    conn = _get_or_404(session, connection_id)
    n_jobs = session.scalar(select(func.count()).where(Job.connection_id == connection_id)) or 0
    if n_jobs:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"connection {connection_id} has {n_jobs} job(s); delete them first",
        )
    session.delete(conn)
    session.commit()


@router.post("/{connection_id}/test", response_model=ConnectionTestResult)
async def test_connection(
    connection_id: int, session: DbSession, arr_factory: ArrFactoryDep
) -> ConnectionTestResult:
    conn = _get_or_404(session, connection_id)
    client = arr_factory(conn)
    try:
        st = await client.status()
    except ArrError as exc:
        return ConnectionTestResult(ok=False, error=str(exc))
    if st.app_name.lower() != conn.kind.value:
        return ConnectionTestResult(
            ok=False,
            app_name=st.app_name,
            version=st.version,
            error=f"connection kind is {conn.kind.value} but the server reports {st.app_name!r}",
        )
    try:
        visible = await client.path_visible(conn.staging_path_remote)
    except ArrError as exc:
        return ConnectionTestResult(
            ok=False, app_name=st.app_name, version=st.version, error=str(exc)
        )
    warning = None
    if visible and get_setting(session, "subtitles_langs"):
        try:
            extras = await client.extra_files_config()
            if not extras.imports("srt"):
                warning = (
                    f"{st.app_name} will not import subtitle sidecars: enable "
                    "Settings → Media Management → Import Extra Files with 'srt'"
                )
        except ArrError as exc:
            warning = f"could not read {st.app_name}'s media management settings: {exc}"
    return ConnectionTestResult(
        ok=visible,
        app_name=st.app_name,
        version=st.version,
        staging_visible=visible,
        error=None
        if visible
        else f"{st.app_name} cannot see staging path {conn.staging_path_remote!r}",
        warning=warning,
    )

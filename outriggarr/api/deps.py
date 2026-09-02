from collections.abc import Iterator
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.orm import Session

from outriggarr.arr import ArrFactory
from outriggarr.source import VideoSource


def get_session(request: Request) -> Iterator[Session]:
    with request.app.state.session_factory() as session:
        yield session


def get_arr_factory(request: Request) -> ArrFactory:
    return request.app.state.arr_factory


def get_source(request: Request) -> VideoSource:
    return request.app.state.source


def get_runner_deps(request: Request):
    return request.app.state.runner_deps


DbSession = Annotated[Session, Depends(get_session)]
SourceDep = Annotated[VideoSource, Depends(get_source)]
RunnerDepsDep = Annotated[object, Depends(get_runner_deps)]
ArrFactoryDep = Annotated[ArrFactory, Depends(get_arr_factory)]

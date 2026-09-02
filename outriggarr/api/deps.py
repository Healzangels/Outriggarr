from collections.abc import Iterator
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.orm import Session

from outriggarr.arr import ArrFactory


def get_session(request: Request) -> Iterator[Session]:
    with request.app.state.session_factory() as session:
        yield session


def get_arr_factory(request: Request) -> ArrFactory:
    return request.app.state.arr_factory


DbSession = Annotated[Session, Depends(get_session)]
ArrFactoryDep = Annotated[ArrFactory, Depends(get_arr_factory)]

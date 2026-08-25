"""
Saved-project endpoints.

A render leaves its output on disk but not the settings that produced it, so
"make another one like that, but shorter" meant retyping the whole form. These
routes persist a form as a named project and hand it back for re-editing.

Path building lives entirely in ``app.services.project``: an id that does not
match the generated pattern never reaches the filesystem.
"""

from fastapi import Depends, Path, Request

from app.controllers import base
from app.controllers.v1.base import new_router
from app.models.exception import HttpException
from app.models.schema import ProjectSaveRequest, ProjectUpdateRequest
from app.services import project as project_service
from app.utils import utils

router = new_router(dependencies=[Depends(base.verify_token)])


def _require_valid_id(project_id: str, request_id: str) -> str:
    if not project_service.is_valid_project_id(project_id):
        raise HttpException(
            task_id=request_id,
            status_code=400,
            message=f"{request_id}: invalid project id",
        )
    return project_id


@router.post("/projects", summary="Save the current settings as a project")
def create_project(request: Request, body: ProjectSaveRequest):
    request_id = base.get_task_id(request)
    try:
        record = project_service.save_project(
            title=body.title,
            params=body.params,
            task_id=body.task_id,
            materials=body.materials,
        )
    except ValueError as exc:
        # A params payload the render endpoints would reject is not worth
        # storing: it would only fail later, at load time, with no context.
        raise HttpException(
            task_id=request_id,
            status_code=400,
            message=f"{request_id}: {str(exc)}",
        ) from exc
    return utils.get_response(200, {"project": record})


@router.get("/projects", summary="List saved projects")
def list_projects(request: Request):
    # An empty list still has to reach the client as `projects: []` - the
    # envelope drops falsy data, so the key must sit inside a dict.
    return utils.get_response(200, {"projects": project_service.list_projects()})


@router.get("/projects/{project_id}", summary="Load a saved project")
def get_project(
    request: Request,
    project_id: str = Path(..., description="Project ID"),
):
    request_id = base.get_task_id(request)
    _require_valid_id(project_id, request_id)

    record = project_service.load_project(project_id)
    if record is None:
        raise HttpException(
            task_id=request_id,
            status_code=404,
            message=f"{request_id}: project not found",
        )
    return utils.get_response(200, {"project": record})


@router.put("/projects/{project_id}", summary="Rename or re-save a project")
def update_project(
    request: Request,
    body: ProjectUpdateRequest,
    project_id: str = Path(..., description="Project ID"),
):
    request_id = base.get_task_id(request)
    _require_valid_id(project_id, request_id)

    if (
        body.title is None
        and body.params is None
        and body.task_id is None
        and body.materials is None
    ):
        raise HttpException(
            task_id=request_id,
            status_code=400,
            message=f"{request_id}: nothing to update",
        )

    try:
        record = project_service.update_project(
            project_id,
            title=body.title,
            params=body.params,
            task_id=body.task_id,
            materials=body.materials,
        )
    except ValueError as exc:
        raise HttpException(
            task_id=request_id,
            status_code=400,
            message=f"{request_id}: {str(exc)}",
        ) from exc

    if record is None:
        raise HttpException(
            task_id=request_id,
            status_code=404,
            message=f"{request_id}: project not found",
        )
    return utils.get_response(200, {"project": record})


@router.delete("/projects/{project_id}", summary="Delete a saved project")
def delete_project(
    request: Request,
    project_id: str = Path(..., description="Project ID"),
):
    request_id = base.get_task_id(request)
    _require_valid_id(project_id, request_id)

    if not project_service.delete_project(project_id):
        raise HttpException(
            task_id=request_id,
            status_code=404,
            message=f"{request_id}: project not found",
        )
    return utils.get_response(200, {"deleted": project_id})

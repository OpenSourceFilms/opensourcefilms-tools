import re
from typing import Optional

from fastapi import APIRouter, HTTPException, Header
from pydantic import BaseModel
from sqlalchemy import select

from db.base import AsyncSessionLocal
from db.models import Project

router = APIRouter()


class ProjectCreate(BaseModel):
    name: str
    slug: Optional[str] = None


def _slugify(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


@router.post("", status_code=201)
async def create_project(body: ProjectCreate):
    slug = body.slug or _slugify(body.name)
    async with AsyncSessionLocal() as db:
        existing = await db.execute(select(Project).where(Project.slug == slug))
        if existing.scalar_one_or_none():
            raise HTTPException(409, f"slug '{slug}' already exists")
        project = Project(name=body.name, slug=slug)
        db.add(project)
        await db.commit()
        await db.refresh(project)
        return {"id": project.id, "name": project.name, "slug": project.slug,
                "created_at": project.created_at}


@router.get("")
async def list_projects():
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(Project).order_by(Project.created_at.desc()))
        projects = result.scalars().all()
        return [{"id": p.id, "name": p.name, "slug": p.slug,
                 "created_at": p.created_at} for p in projects]

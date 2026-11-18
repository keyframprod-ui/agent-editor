"""`ae` command line."""
from __future__ import annotations

import json
from typing import Optional

import typer

from . import analyze, export as exp, render as rnd, review as rev, timeline as T
from .core import Project

app = typer.Typer(help="Agent Editor — agentic video & film editing", no_args_is_help=True)


@app.command()
def ingest(project: str, sources: list[str], tag: list[str] = typer.Option(None)):
    """Add media (files, folders, URLs) to a project."""
    from . import mcp_server as M
    M.open_project(project)
    print(M.ingest(sources, tag))


@app.command("analyze")
def analyze_cmd(project: str, media: Optional[list[str]] = typer.Argument(None), faces: bool = False,
                no_transcribe: bool = False):
    """Shots, audio, transcript (all media if none given)."""
    p = Project.open(project)
    for m in media or list(p.data["media"]):
        print(json.dumps(analyze.analyze_all(p, m, not no_transcribe, faces), indent=1))


@app.command("bin")
def bin_cmd(project: str):
    print(analyze.overview(Project.open(project)))


@app.command()
def show(project: str, timeline: str = "main"):
    p = Project.open(project)
    print(T.describe(p, T.load(p, timeline)))


@app.command()
def render(project: str, timeline: str = "main", quality: str = "preview"):
    p = Project.open(project)
    print(json.dumps(rnd.render(p, T.load(p, timeline), quality), indent=1))


@app.command()
def review(project: str, render_path: str, timeline: str = "main"):
    p = Project.open(project)
    tl = T.load(p, timeline)
    print(rev.review_page(p, tl, render_path))
    print(json.dumps({"qc": rev.qc(render_path, tl.duration, tl.loudness_target), "pacing": rev.pacing(p, tl)}, indent=1))


@app.command("export")
def export_cmd(project: str, timeline: str = "main", fmt: list[str] = typer.Option(None)):
    p = Project.open(project)
    print(json.dumps(exp.export(p, T.load(p, timeline), fmt), indent=1))


@app.command()
def agent(project: str, brief: str, model: Optional[str] = None, effort: str = "high", max_iterations: int = 120):
    """Run the autonomous editing agent on a brief (needs ANTHROPIC_API_KEY)."""
    from . import agent as A
    print(A.run(project, brief, model, effort, max_iterations))


@app.command()
def mcp():
    """Run the MCP server (stdio)."""
    from .mcp_server import main
    main()


if __name__ == "__main__":
    app()

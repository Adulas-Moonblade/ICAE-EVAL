"""Render task prompts for agents running on the host or in the container."""
from . import config as C
from .docker_env import WORKDIR, PRD_NAME


def render_task_prompt(alias: str, *, docker_id: str, lang: str,
                       in_container: bool = False) -> str:
    source = (C.TASK_TEMPLATE if in_container else
              C.HARNESS / "prompt_templates" / "task_host_docker.md")
    template = source.read_text(encoding="utf-8")
    return template.format(
        docker_id=docker_id,
        workdir=WORKDIR,
        prd_name=PRD_NAME,
        lang=lang or "(unspecified)",
    )

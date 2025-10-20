"""Taskwarrior consumer that turns capture notes into tasks."""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Set, Tuple

from ..config import AutomationConfig, ConsumerConfig
from ..emitter import NoteState
from ..notes import NotePayload
from ..store import AutomationStore
from . import register
from .base import Consumer, ConsumerResult

LOG = logging.getLogger("automation.taskwarrior")


def _normalise_tag(value: str) -> str:
    return value.strip().replace(" ", "_").lower()


def _task_timestamp(source: Optional[str] = None) -> str:
    if source:
        try:
            dt = datetime.fromisoformat(source.replace("Z", "+00:00"))
            return dt.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        except ValueError:
            pass
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


class TaskCommandError(RuntimeError):
    """Raised when a Taskwarrior invocation fails."""

    def __init__(self, message: str, stdout: str = "", stderr: str = "") -> None:
        super().__init__(message)
        self.stdout = stdout
        self.stderr = stderr


@register("taskwarrior")
class TaskWarriorConsumer(Consumer):
    """Consumer that mirrors capture notes into Taskwarrior."""

    def __init__(self, config: ConsumerConfig, global_config: AutomationConfig) -> None:
        super().__init__(config, global_config)
        opts = dict(config.options)

        self.marker_tag = str(opts.get("marker_tag", "todo")).strip()
        self.strip_tags = {tag.lower() for tag in opts.get("strip_tags", [])}
        if self.marker_tag:
            self.strip_tags.add(self.marker_tag.lower())

        self.remove_unknown_tags = bool(opts.get("remove_unknown_tags", True))
        self.project_tag_prefix = str(opts.get("project_tag_prefix", "project:"))
        self.default_project = opts.get("default_project")
        self.additional_tags = [
            _normalise_tag(str(tag))
            for tag in opts.get("additional_tags", [])
            if str(tag).strip()
        ]
        self.annotation_template = opts.get("annotation_template")
        review_tag_raw = opts.get("review_tag", "not_reviewed")
        review_tag = _normalise_tag(str(review_tag_raw)) if review_tag_raw else ""
        self.review_tag = review_tag or None

        limit_raw = opts.get("max_new_tasks_per_run")
        if limit_raw is None or str(limit_raw).strip() == "":
            self.max_new_tasks_per_run: Optional[int] = None
        else:
            try:
                parsed_limit = int(limit_raw)
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    f"Invalid max_new_tasks_per_run value: {limit_raw!r}",
                ) from exc
            if parsed_limit < 0:
                raise ValueError(
                    f"max_new_tasks_per_run must be non-negative, got {parsed_limit}",
                )
            self.max_new_tasks_per_run = parsed_limit
        self._tasks_added = 0

        backup_opts = opts.get("backup", {})
        backup_dir_raw = backup_opts.get("directory", "backups/taskwarrior")
        self.backup_enabled = bool(backup_opts.get("enabled", True))
        if backup_dir_raw:
            backup_path = Path(backup_dir_raw)
            if not backup_path.is_absolute():
                backup_path = global_config.state_dir / backup_path
        else:
            backup_path = global_config.state_dir / "backups/taskwarrior"
        backup_path.mkdir(parents=True, exist_ok=True)
        self.backup_dir = backup_path

        self.taskrc_path = self._resolve_taskrc_path(opts)
        self.data_directory = self._resolve_data_directory(opts)
        if not self.data_directory.exists():
            raise FileNotFoundError(
                f"Taskwarrior data directory not found: {self.data_directory}",
            )

        self._backed_up = False
        self._existing_tags = self._load_existing_tags()
        if self.review_tag:
            self._existing_tags.add(self.review_tag.lower())
        self._existing_task_keys, self._existing_summary_keys = self._load_existing_task_keys()

    # ------------------------------------------------------------------ lifecycle

    def matches(self, state: NoteState) -> bool:
        if not self.marker_tag:
            return True
        return state.note.has_tag(self.marker_tag)

    def handle(self, state: NoteState, store: AutomationStore) -> ConsumerResult:
        note = state.note
        if not self.matches(state):
            return ConsumerResult(
                status="skip",
                note_path=note.path,
                message="marker tag missing",
            )
        try:
            result = self._process_note(note, store)
            if result.status in {"success", "skip"}:
                store.mark_emitted(
                    consumer=self.name,
                    note_path=note.path,
                    note_hash=note.note_hash,
                    status=result.status,
                    metadata=result.metadata,
                )
            return result
        except TaskCommandError as exc:
            LOG.error(
                "[%s] Taskwarrior command failed for %s: %s\nSTDOUT: %s\nSTDERR: %s",
                self.name,
                note.path,
                exc,
                exc.stdout,
                exc.stderr,
            )
            raise

    # ------------------------------------------------------------------ helpers

    def _process_note(self, note: NotePayload, store: AutomationStore) -> ConsumerResult:
        if self.max_new_tasks_per_run is not None and self._tasks_added >= self.max_new_tasks_per_run:
            return ConsumerResult(
                status="limit",
                note_path=note.path,
                message="task limit reached",
                metadata={
                    "limit": self.max_new_tasks_per_run,
                    "note": str(note.path),
                },
            )
        description = self._build_description(note)
        if not description:
            return ConsumerResult(
                status="skip",
                note_path=note.path,
                message="empty description",
            )

        tags, project = self._prepare_tags(note)
        summary_key = self._summary_key(description, project)
        if summary_key in self._existing_summary_keys:
            return ConsumerResult(
                status="skip",
                note_path=note.path,
                message="duplicate task (description/project)",
                metadata={
                    "description": description,
                    "project": project,
                },
            )
        key = self._task_key(description, tags, project)
        if key in self._existing_task_keys:
            return ConsumerResult(
                status="skip",
                note_path=note.path,
                message="duplicate task",
                metadata={"description": description, "tags": tags, "project": project},
            )

        if self.backup_enabled:
            self._ensure_backup()

        annotation = self._format_annotation(note)
        task_payload = self._build_task_payload(
            note=note,
            description=description,
            tags=tags,
            project=project,
            annotation=annotation,
        )
        self._import_task(task_payload)
        self._tasks_added += 1
        self._existing_task_keys.add(key)
        self._existing_summary_keys.add(summary_key)
        for tag in tags:
            self._existing_tags.add(tag.lower())

        return ConsumerResult(
            status="success",
            note_path=note.path,
            message="task added",
            metadata={
                "description": description,
                "tags": tags,
                "project": project,
            },
        )

    def _ensure_backup(self) -> None:
        if self._backed_up or not self.backup_enabled:
            return
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup_path = self.backup_dir / f"{timestamp}"
        # Guarantee unique target by appending counter if needed.
        counter = 1
        candidate = backup_path
        while candidate.exists():
            counter += 1
            candidate = self.backup_dir / f"{timestamp}-{counter}"
        shutil.copytree(self.data_directory, candidate)
        self._backed_up = True
        LOG.info("[%s] Created Taskwarrior backup at %s", self.name, candidate)

    def _task_key(self, description: str, tags: Sequence[str], project: Optional[str]) -> Tuple[str, Tuple[str, ...], str]:
        canonical_tags = tuple(sorted(tag.lower() for tag in tags))
        project_key = (project or "").lower()
        return (description.strip().lower(), canonical_tags, project_key)

    @staticmethod
    def _summary_key(description: str, project: Optional[str]) -> Tuple[str, str]:
        return (description.strip().lower(), (project or "").strip().lower())

    # ------------------------------- description / tags

    def _build_description(self, note: NotePayload) -> str:
        title = str(note.frontmatter.get("title", "")).strip() if note.frontmatter.get("title") else ""
        lines = [line.strip() for line in note.content.splitlines()]
        filtered: List[str] = []
        for line in lines:
            if not line:
                continue
            if line.startswith("#"):
                # Skip Markdown headings such as '## Content'
                continue
            filtered.append(line)
        if not filtered and title:
            candidate = title
        elif filtered:
            candidate = filtered[0]
            if len(filtered) > 1:
                candidate += " " + " ".join(filtered[1:])
        else:
            candidate = note.path.stem
        candidate = " ".join(candidate.split())
        if len(candidate) > 512:
            candidate = candidate[:509] + "..."
        return candidate.strip()

    def _prepare_tags(self, note: NotePayload) -> Tuple[List[str], Optional[str]]:
        tags = {_normalise_tag(tag) for tag in note.tags}
        tags = {tag for tag in tags if tag}

        # Remove marker and explicit strip tags.
        tags = {
            tag
            for tag in tags
            if tag.lower() not in self.strip_tags
        }

        # Extract project from tag prefix if present.
        project: Optional[str] = None
        prefix = self.project_tag_prefix.lower()
        to_remove: Set[str] = set()
        for tag in tags:
            if prefix and tag.lower().startswith(prefix):
                project = tag[len(self.project_tag_prefix) :].strip()
                to_remove.add(tag)
                break
        tags -= to_remove
        if not project and self.default_project:
            project = str(self.default_project)

        # Additional tags from config.
        tags.update(self.additional_tags)
        if self.review_tag:
            tags.add(self.review_tag)

        final_tags = sorted({tag for tag in tags if tag})

        if self.remove_unknown_tags:
            final_tags = [
                tag for tag in final_tags if tag.lower() in self._existing_tags
            ]

        return final_tags, project

    def _format_annotation(self, note: NotePayload) -> Optional[str]:
        template = self.annotation_template
        if not template:
            return None
        relative_path: Path
        try:
            relative_path = note.path.relative_to(self.global_config.vault_root)
        except ValueError:
            relative_path = note.path
        context = {
            "path": str(note.path),
            "relative_path": str(relative_path),
            "id": note.frontmatter.get("id") or "",
            "capture_id": note.frontmatter.get("capture_id") or "",
        }
        try:
            return template.format(**context)
        except KeyError:
            return template

    def _build_task_payload(
        self,
        note: NotePayload,
        description: str,
        tags: Sequence[str],
        project: Optional[str],
        annotation: Optional[str],
    ) -> Dict[str, object]:
        entry_ts = _task_timestamp(
            str(note.frontmatter.get("timestamp") or note.frontmatter.get("created_date") or ""),
        )
        payload: Dict[str, object] = {
            "description": description,
            "entry": entry_ts,
        }
        if tags:
            payload["tags"] = list(tags)
        if project:
            payload["project"] = project
        if annotation:
            payload["annotations"] = [
                {"entry": _task_timestamp(), "description": annotation},
            ]
        return payload

    # ------------------------------- Taskwarrior integration

    def _run_task(self, args: Sequence[str], input_text: Optional[str] = None) -> subprocess.CompletedProcess:
        cmd = [
            "task",
            f"rc.data.location={self.data_directory}",
            "rc.confirmation=no",
            "rc.hooks=off",
        ] + list(args)
        env = os.environ.copy()
        if self.taskrc_path:
            env["TASKRC"] = str(self.taskrc_path)
        proc = subprocess.run(
            cmd,
            input=input_text.encode("utf-8") if input_text else None,
            capture_output=True,
            text=True,
            env=env,
        )
        if proc.returncode != 0:
            raise TaskCommandError(
                f"Taskwarrior command failed: {' '.join(cmd)}",
                stdout=proc.stdout,
                stderr=proc.stderr,
            )
        return proc

    def _resolve_taskrc_path(self, opts: Dict[str, object]) -> Optional[Path]:
        taskrc = opts.get("taskrc_path")
        if taskrc:
            path = Path(str(taskrc)).expanduser().resolve(strict=False)
            return path
        env_taskrc = os.environ.get("TASKRC")
        if env_taskrc:
            return Path(env_taskrc).expanduser().resolve(strict=False)
        default = Path("~/.taskrc").expanduser().resolve(strict=False)
        if default.exists():
            return default
        return None

    def _resolve_data_directory(self, opts: Dict[str, object]) -> Path:
        explicit = opts.get("data_directory")
        if explicit:
            path = Path(str(explicit)).expanduser().resolve(strict=False)
            if path.exists():
                return path
        taskrc_dir = self._parse_taskrc_for_data_location()
        if taskrc_dir:
            return taskrc_dir
        return Path("~/.task").expanduser().resolve(strict=False)

    def _parse_taskrc_for_data_location(self) -> Optional[Path]:
        path = self.taskrc_path
        if not path or not path.exists():
            return None
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return None
        for line in lines:
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if "=" not in stripped:
                continue
            key, value = stripped.split("=", 1)
            key = key.strip()
            if key.lower() == "data.location":
                value = value.strip()
                if value:
                    if (value.startswith('"') and value.endswith('"')) or (
                        value.startswith("'") and value.endswith("'")
                    ):
                        value = value[1:-1]
                    return Path(value).expanduser().resolve(strict=False)
        return None

    def _load_existing_tags(self) -> Set[str]:
        try:
            proc = self._run_task(["_tags"])
        except TaskCommandError as exc:
            raise TaskCommandError(
                "Failed to load existing Taskwarrior tags",
                stdout=exc.stdout,
                stderr=exc.stderr,
            ) from exc
        tags = {line.strip().lower() for line in proc.stdout.splitlines() if line.strip()}
        return tags

    def _load_existing_task_keys(self) -> Tuple[Set[Tuple[str, Tuple[str, ...], str]], Set[Tuple[str, str]]]:
        try:
            proc = self._run_task(["rc.json.array=1", "export"])
        except TaskCommandError as exc:
            raise TaskCommandError(
                "Failed to export existing Taskwarrior tasks",
                stdout=exc.stdout,
                stderr=exc.stderr,
            ) from exc
        try:
            tasks = json.loads(proc.stdout)
        except json.JSONDecodeError as exc:
            raise TaskCommandError("Failed to parse Taskwarrior export", proc.stdout, proc.stderr) from exc
        keys: Set[Tuple[str, Tuple[str, ...], str]] = set()
        summary_keys: Set[Tuple[str, str]] = set()
        for task in tasks:
            description = str(task.get("description", "")).strip()
            tags = tuple(sorted(str(tag) for tag in task.get("tags", []) if tag))
            project = str(task.get("project", "")).strip()
            keys.add(self._task_key(description, tags, project or None))
            summary_keys.add(self._summary_key(description, project or None))
        return keys, summary_keys

    def _import_task(self, payload: Dict[str, object]) -> None:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False, suffix=".json") as handle:
            json.dump([payload], handle)
            handle.flush()
            tmp_path = Path(handle.name)
        try:
            self._run_task(["import", str(tmp_path)])
        finally:
            try:
                tmp_path.unlink()
            except FileNotFoundError:
                pass

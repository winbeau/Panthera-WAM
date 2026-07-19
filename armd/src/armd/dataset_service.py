"""独立 DatasetService 与按需隔离运行的 LeRobot v3 导出作业。"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import shlex
import shutil
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

import grpc
from panthera_arm import dataset_pb2, dataset_pb2_grpc

from .teach import TeachStore, load_raw_frames

DEFAULT_DATASET_DIR = Path("~/.local/share/panthera/datasets")


@dataclass(slots=True)
class DatasetJob:
    job_id: str
    trajectory: Path
    output: Path
    repo_id: str
    task_name: str
    overwrite: bool
    state: int = dataset_pb2.DATASET_JOB_STATE_QUEUED
    progress: float = 0.0
    frame_count: int = 0
    error_message: str = ""
    task: asyncio.Task[None] | None = None
    process: asyncio.subprocess.Process | None = None

    @property
    def terminal(self) -> bool:
        return self.state in {
            dataset_pb2.DATASET_JOB_STATE_DONE,
            dataset_pb2.DATASET_JOB_STATE_FAILED,
            dataset_pb2.DATASET_JOB_STATE_CANCELLED,
        }


class DatasetJobManager:
    def __init__(
        self,
        *,
        teach_store: TeachStore | None = None,
        root: str | Path | None = None,
    ) -> None:
        configured = root or os.environ.get("PANTHERA_DATASET_DIR") or DEFAULT_DATASET_DIR
        self.root = Path(configured).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.teach_store = teach_store or TeachStore()
        self._jobs: dict[str, DatasetJob] = {}

    def start(
        self,
        *,
        trajectory_path: str,
        output_dir: str,
        repo_id: str,
        task_name: str,
        overwrite: bool,
    ) -> DatasetJob:
        trajectory = self.teach_store.existing_path(trajectory_path)
        output = self._output_path(output_dir)
        if output.exists() and not overwrite:
            raise FileExistsError(f"数据集输出目录已存在: {output}")
        selected_repo = repo_id.strip() or "local/panthera-wam"
        if "/" not in selected_repo or selected_repo.startswith("/") or selected_repo.endswith("/"):
            raise ValueError("repo_id 必须形如 owner/dataset")
        selected_task = task_name.strip() or "Panthera demonstration"
        job = DatasetJob(
            job_id=uuid.uuid4().hex,
            trajectory=trajectory,
            output=output,
            repo_id=selected_repo,
            task_name=selected_task,
            overwrite=overwrite,
        )
        self._jobs[job.job_id] = job
        job.task = asyncio.create_task(self._run(job), name=f"panthera-dataset-{job.job_id}")
        return job

    def get(self, job_id: str) -> DatasetJob | None:
        return self._jobs.get(job_id)

    def cancel(self, job_id: str) -> bool:
        job = self._jobs.get(job_id)
        if job is None or job.terminal or job.task is None:
            return False
        job.task.cancel()
        return True

    async def close(self) -> None:
        tasks = [job.task for job in self._jobs.values() if job.task is not None and not job.terminal]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _run(self, job: DatasetJob) -> None:
        try:
            if job.output.exists():
                await asyncio.to_thread(shutil.rmtree, job.output)
            # 先在主服务环境校验输入，避免为坏 JSONL 启动隔离依赖环境。
            frames = await asyncio.to_thread(load_raw_frames, job.trajectory)
            job.frame_count = len(frames)
            job.state = dataset_pb2.DATASET_JOB_STATE_RUNNING
            command = self._runner_command()
            process = await asyncio.create_subprocess_exec(
                *command,
                "--trajectory",
                str(job.trajectory),
                "--output",
                str(job.output),
                "--repo-id",
                job.repo_id,
                "--task",
                job.task_name,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            job.process = process
            assert process.stdout is not None and process.stderr is not None
            stderr_task = asyncio.create_task(process.stderr.read())
            async for raw_line in process.stdout:
                try:
                    update = json.loads(raw_line.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    continue
                job.progress = max(job.progress, float(update.get("progress", 0.0)))
                job.frame_count = int(update.get("frame_count", job.frame_count))
            return_code = await process.wait()
            stderr = (await stderr_task).decode("utf-8", errors="replace").strip()
            job.process = None
            if return_code != 0:
                raise RuntimeError(stderr or f"LeRobot worker 退出码 {return_code}")
            job.progress = 1.0
            job.state = dataset_pb2.DATASET_JOB_STATE_DONE
        except asyncio.CancelledError:
            process = job.process
            if process is not None and process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=2.0)
                except asyncio.TimeoutError:
                    process.kill()
                    await process.wait()
            job.process = None
            job.state = dataset_pb2.DATASET_JOB_STATE_CANCELLED
        except BaseException as exc:
            job.error_message = str(exc)
            job.state = dataset_pb2.DATASET_JOB_STATE_FAILED

    def _output_path(self, requested: str) -> Path:
        name = requested.strip() or time.strftime("panthera_%Y%m%d_%H%M%S")
        raw = Path(name).expanduser()
        candidate = (raw if raw.is_absolute() else self.root / raw).resolve()
        if not candidate.is_relative_to(self.root):
            raise ValueError(f"数据集输出必须位于目录内: {self.root}")
        return candidate

    @staticmethod
    def _runner_command() -> list[str]:
        override = os.environ.get("PANTHERA_LEROBOT_RUNNER", "").strip()
        if override:
            return [*shlex.split(override), "-m", "armd.dataset_worker"]
        if importlib.util.find_spec("lerobot") is not None:
            return [sys.executable, "-m", "armd.dataset_worker"]
        uv = shutil.which("uv")
        project = Path(__file__).resolve().parents[2]
        if uv is None or not (project / "pyproject.toml").is_file():
            raise RuntimeError(
                "未安装 LeRobot；请安装 lerobot>=0.4,<0.5，或配置 PANTHERA_LEROBOT_RUNNER"
            )
        return [
            uv,
            "run",
            "--project",
            str(project),
            "--with",
            "lerobot>=0.4,<0.5",
            "python",
            "-m",
            "armd.dataset_worker",
        ]


class DatasetService(dataset_pb2_grpc.DatasetServiceServicer):
    def __init__(self, jobs: DatasetJobManager) -> None:
        self._jobs = jobs

    async def ExportLeRobot(self, request, context):
        try:
            job = self._jobs.start(
                trajectory_path=request.trajectory_path,
                output_dir=request.output_dir,
                repo_id=request.repo_id,
                task_name=request.task,
                overwrite=request.overwrite,
            )
        except FileNotFoundError as exc:
            await context.abort(grpc.StatusCode.NOT_FOUND, str(exc))
        except FileExistsError as exc:
            await context.abort(grpc.StatusCode.ALREADY_EXISTS, str(exc))
        except ValueError as exc:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, str(exc))
        return dataset_pb2.DatasetJobAccepted(job_id=job.job_id)

    async def GetJob(self, request, context):
        job = await self._require_job(request.job_id, context)
        return job_message(job)

    async def WatchJob(self, request, context):
        job = await self._require_job(request.job_id, context)
        while True:
            yield job_message(job)
            if job.terminal:
                return
            await asyncio.sleep(0.2)

    async def CancelJob(self, request, context):
        del context
        return dataset_pb2.DatasetCancelResponse(cancelled=self._jobs.cancel(request.job_id))

    async def GetMapping(self, request, context):
        del request, context
        response = dataset_pb2.DatasetMappingResponse(format_version="LeRobotDataset v3.0")
        for source, target in (
            ("pos + gripper_pos", "observation.state"),
            ("vel + gripper_vel", "observation.velocity"),
            ("pos + gripper_pos", "action"),
            ("vel + gripper_vel", "action.velocity"),
            ("t", "panthera.timestamp"),
        ):
            response.fields.add(source=source, target=target, dtype="float32", shape=[7 if target != "panthera.timestamp" else 1])
        return response

    async def _require_job(self, job_id: str, context) -> DatasetJob:
        job = self._jobs.get(job_id)
        if job is None:
            await context.abort(grpc.StatusCode.NOT_FOUND, "dataset job_id 不存在")
        return job


def job_message(job: DatasetJob) -> dataset_pb2.DatasetJobStatus:
    return dataset_pb2.DatasetJobStatus(
        job_id=job.job_id,
        state=job.state,
        progress=job.progress,
        output_dir=str(job.output),
        frame_count=job.frame_count,
        error_message=job.error_message,
    )

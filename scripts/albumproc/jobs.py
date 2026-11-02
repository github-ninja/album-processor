"""Durable import records and private workspaces; no audio is edited in downloads."""
import hashlib
import json
import os
import logging
import shutil
import time
from pathlib import Path

from . import config


def directory() -> Path:
    return config.state_dir() / 'jobs'


def save(job: dict) -> None:
    directory().mkdir(parents=True, exist_ok=True)
    path = directory() / (job['id'] + '.json')
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(job, ensure_ascii=False, indent=1), encoding='utf-8')
    os.replace(temporary, path)


def manifest(path: Path) -> list:
    return [[str(f.relative_to(path)), f.stat().st_size, f.stat().st_mtime_ns]
            for f in sorted(path.rglob('*')) if f.is_file()]


def pending() -> list[dict]:
    entries = []
    for path in sorted(directory().glob('*.json')):
        try:
            job = json.loads(path.read_text(encoding='utf-8'))
            if (job.get('status') == 'waiting' and isinstance(job.get('deferred'), dict)
                    and isinstance(job['deferred'].get('path'), str)):
                entries.append(job['deferred'])
        except (OSError, ValueError, AttributeError):
            logging.getLogger(__name__).warning('Cannot read import job: %s', path)
    return entries


def recoverable_sources() -> list[Path]:
    sources = []
    for path in directory().glob('*.json'):
        try:
            job = json.loads(path.read_text(encoding='utf-8'))
            if (job.get('status') == 'committing' and Path(job['destination']).is_dir()
                    and not Path(job['work']).exists()):
                sources.append(Path(job['source']))
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            continue
    return sources


def identity_for(audio: list[str]) -> dict | None:
    if not audio:
        return None
    for path in sorted(directory().glob('*.json')):
        try:
            job = json.loads(path.read_text(encoding='utf-8'))
            if job.get('status') == 'done' and job.get('measured', {}).get('audio') == audio:
                return job.get('metadata')
        except (OSError, ValueError, AttributeError):
            continue
    return None


def open_job(source: Path) -> dict:
    source = source.resolve()
    identifier = hashlib.sha256(str(source).encode()).hexdigest()[:24]
    path = directory() / (identifier + '.json')
    if path.exists():
        job = json.loads(path.read_text(encoding='utf-8'))
        if job['source'] != str(source):
            raise ValueError('import job identity conflict')
        if job.get('status') != 'done' or not source.is_dir():
            return job
        # A later download may reuse this path; keep the previous album's identity.
        path.rename(path.with_name(f'{identifier}-done-{time.time_ns()}.json'))
    job = {'id': identifier, 'source': str(source), 'status': 'new',
           'work': str(config.state_dir() / 'work' / identifier / source.name)}
    save(job)
    return job


def prepare(job: dict) -> Path:
    source, work = Path(job['source']), Path(job['work'])
    if not work.resolve().is_relative_to((config.state_dir() / 'work').resolve()):
        raise ValueError('workspace escapes the private work directory')
    current = manifest(source)
    if work.is_dir():
        shutil.rmtree(work)
    job.update(status='copying', source_manifest=current)
    save(job)
    work.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, work)
    if manifest(source) != current or manifest(work) != current:
        raise RuntimeError('download changed while copying; retry when it is complete')
    job.update(status='processing')
    save(job)
    return work


def discard(job: dict, status: str) -> None:
    work = Path(job['work'])
    if not work.resolve().is_relative_to((config.state_dir() / 'work').resolve()):
        raise ValueError('workspace escapes the private work directory')
    if work.exists():
        shutil.rmtree(work)
    job.update(status=status, deferred=None)
    save(job)


def finish(job: dict) -> None:
    source = Path(job['source'])
    if source.is_dir():
        if manifest(source) != job['source_manifest']:
            raise RuntimeError('source changed after preparation; imported copy retained, source not deleted')
        shutil.rmtree(source)
    job.update(status='done', deferred=None)
    save(job)
    parent = Path(job['work']).parent
    if parent.is_dir() and not any(parent.iterdir()):
        parent.rmdir()

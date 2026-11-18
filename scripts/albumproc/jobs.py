"""Durable import records and private workspaces; no audio is edited in downloads."""
import hashlib
import json
import logging
import shutil
import time
import uuid
from pathlib import Path

from . import config
from .files import safe_rename, write_json
from .names import name_key


def directory() -> Path:
    return config.state_dir() / 'jobs'


def save(job: dict) -> None:
    write_json(directory() / (job['id'] + '.json'), job)


def manifest(path: Path) -> list:
    snapshot = []
    for file in sorted(path.rglob("*")):
        if file.is_file():
            info = file.stat()
            snapshot.append([str(file.relative_to(path)), info.st_size, info.st_mtime_ns])
    return snapshot


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
            if (job.get('status') in {'committing', 'deleting'}):
                sources.append(Path(job['source']))
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            continue
    return sources


def identity_for(audio: list[str], *, artist: str | None = None, title: str | None = None) -> dict | None:
    if not audio:
        return None
    for path in sorted(directory().glob('*.json')):
        try:
            job = json.loads(path.read_text(encoding='utf-8'))
            if job.get('status') == 'done' and job.get('measured', {}).get('audio') == audio:
                metadata = job.get("metadata") or {}
                if artist and name_key(metadata.get("artist", "")) != name_key(artist):
                    continue
                if title and name_key(metadata.get("album", "")) != name_key(title):
                    continue
                if metadata:
                    return metadata
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
        if job.get('status') not in {'done', 'deleted'} or not source.is_dir():
            return job
        # A later download may reuse this path; keep the previous album's identity.
        path.rename(path.with_name(f'{identifier}-done-{time.time_ns()}.json'))
    job = {'id': identifier, 'source': str(source), 'status': 'new',
           'work': str(config.state_dir() / 'work' / identifier / source.name)}
    save(job)
    return job


def check_decision(job: dict, entry: dict) -> None:
    source = Path(job['source'])
    if (job.get('status') != 'waiting' or job.get('deferred') != entry
            or not source.is_dir() or source.is_symlink()
            or manifest(source) != entry.get('source_manifest')):
        raise ValueError('Загрузка или запрос изменились — запустите /newalbum ещё раз.')


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
    shutil.copytree(source, work, symlinks=True)
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


def finish(job: dict, status: str = 'done') -> None:
    source = Path(job['source'])
    if source.is_symlink():
        raise ValueError('source became a symbolic link')
    if 'cleanup' not in job and source.is_dir():
        job['cleanup'] = {'name': f".albumproc-cleanup-{uuid.uuid4().hex}"}
        save(job)
    if cleanup := job.get('cleanup'):
        name = cleanup['name']
        if Path(name).name != name or not name.startswith('.albumproc-cleanup-'):
            raise ValueError('invalid source cleanup path')
        trash = source.with_name(name)
        if trash.is_symlink():
            raise ValueError('source cleanup path is a symbolic link')
        if not cleanup.get('verified'):
            if not trash.exists() and source.is_dir():
                if manifest(source) != job['source_manifest']:
                    raise RuntimeError('source changed after preparation; imported copy retained, source not deleted')
                safe_rename(source, trash)
            if trash.exists() and manifest(trash) != job['source_manifest']:
                if not source.exists():
                    safe_rename(trash, source)
                raise RuntimeError('source changed during cleanup; source retained')
            cleanup['verified'] = True
            save(job)
        if trash.exists():
            shutil.rmtree(trash)
    job.update(status=status, deferred=None)
    save(job)
    parent = Path(job['work']).parent
    if parent.is_dir() and not any(parent.iterdir()):
        parent.rmdir()

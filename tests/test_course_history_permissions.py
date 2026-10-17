"""Check SQLite readiness without changing history or issuing paid requests."""

import errno
import os
import re
import sqlite3
from pathlib import Path

import pytest

from app.services.course_history import CourseHistory


@pytest.mark.asyncio
async def test_history_preflight_creates_database_and_preserves_history(tmp_path):
    """A successful probe must allow later writes without recording a dummy course."""
    path = tmp_path / 'data' / 'history.sqlite3'
    history = CourseHistory(path=path)
    await history.ensure_available()
    with sqlite3.connect(path) as connection:
        assert connection.execute('SELECT COUNT(*) FROM courses').fetchone() == (0,)

    assert await history.record_if_novel('real-user', {'places/real'})
    with sqlite3.connect(path) as connection:
        connection.execute('UPDATE courses SET created = 0')
        before = connection.execute('SELECT * FROM courses').fetchall()

    await history.ensure_available()
    with sqlite3.connect(path) as connection:
        assert connection.execute('SELECT * FROM courses').fetchall() == before


@pytest.mark.asyncio
async def test_history_preflight_propagates_denied_directory(tmp_path, mocker):
    """Reproduce the deployed mkdir PermissionError before the graph is opened."""
    denied = PermissionError(errno.EACCES, 'Permission denied', '.data')
    mocker.patch.object(Path, 'mkdir', side_effect=denied)
    with pytest.raises(PermissionError) as failure:
        await CourseHistory(path=tmp_path / '.data' / 'history.sqlite3').ensure_available()
    assert failure.value.errno == errno.EACCES
    assert failure.value.filename == '.data'


@pytest.mark.asyncio
async def test_history_preflight_checks_real_sqlite_write_not_readability(tmp_path, mocker):
    """An existing readable database opened in read-only mode cannot pass readiness."""
    path = tmp_path / 'history.sqlite3'
    history = CourseHistory(path=path)
    assert await history.record_if_novel('user', {'places/real'})

    def readonly_connection():
        return sqlite3.connect(f'{path.as_uri()}?mode=ro', uri=True)

    mocker.patch.object(history, '_connect', side_effect=readonly_connection)
    with pytest.raises(sqlite3.Error):
        await history.ensure_available()
    with sqlite3.connect(path) as connection:
        assert connection.execute('SELECT COUNT(*) FROM courses').fetchone() == (1,)


@pytest.mark.asyncio
async def test_history_is_writable_in_precreated_child_of_readonly_workdir(tmp_path):
    """A writable owned data directory works even when the app working directory is read-only."""
    if os.geteuid() == 0:
        pytest.skip('Root bypasses directory permission checks')
    workdir = tmp_path / 'app'
    data = workdir / '.data'
    data.mkdir(parents=True)
    data.chmod(0o700)
    workdir.chmod(0o555)
    try:
        with pytest.raises(PermissionError):
            (workdir / 'unexpected-directory').mkdir()
        history = CourseHistory(path=data / 'history.sqlite3')
        await history.ensure_available()
        assert await history.record_if_novel('user', {'places/real'})
        assert await history.recent('user') == [{'places/real'}]
    finally:
        workdir.chmod(0o755)


def test_runtime_image_prepares_owned_history_directory_before_nonroot_user():
    """The deployed runtime must provision the writable path before changing UID."""
    dockerfile = (Path(__file__).resolve().parents[1] / 'Dockerfile').read_text()
    runtime = dockerfile.split(' AS runner', 1)[1]
    before_user = runtime.split('USER appuser', 1)[0]
    assert re.search(r'COURSE_HISTORY_PATH=["\']?/app/\.data/course_history\.sqlite3', before_user)
    assert re.search(r'mkdir\s+(?:-p\s+)?/app/\.data', before_user)
    assert re.search(r'chown\s+(?:-R\s+)?(?:appuser:appuser|10001:10001)\s+/app/\.data', before_user)


@pytest.mark.asyncio
@pytest.mark.parametrize(('ids', 'metadata'), [
    ('{broken-json', None),
    ('{"not": "ids"}', None),
    ('["bad", 17]', None),
    ('["bad"]', '{broken-json'),
    ('["bad"]', '["not metadata"]'),
    ('["bad"]', '{"attraction_ids": null, "areas": null, "experiences": null}'),
])
async def test_damaged_history_rows_do_not_block_reads_or_duplicate_claims(tmp_path, ids, metadata):
    """Keep valid ID history even when an adjacent row or metadata is damaged."""
    import json
    import time

    history = CourseHistory(tmp_path / 'history.sqlite3')
    assert await history.record_if_novel('user', {'good'}, metadata={'attraction_ids': ['good']})
    with sqlite3.connect(history.path) as db:
        db.execute('INSERT INTO courses VALUES (?, ?, ?, ?)', ('user', 'damaged', ids, time.time()))
        if metadata is not None:
            db.execute('INSERT INTO course_plans VALUES (?, ?, ?)', ('user', 'damaged', metadata))
    entries = await history.recent_profiles('user')
    expected = [{'bad'}, {'good'}] if metadata is not None else [{'good'}]
    assert [set(entry['place_ids']) for entry in entries] == expected
    assert await history.recent('user') == expected
    assert not await history.record_if_novel('user', {'good', 'new'}, metadata={'attraction_ids': ['good']}, max_overlap=.4)
    assert await history.record_if_novel('user', {'fresh'}, metadata={'attraction_ids': ['fresh']}, max_overlap=.4)
    assert 'fresh' in json.dumps(await history.recent_profiles('user'))


@pytest.mark.asyncio
async def test_history_metadata_cannot_override_verified_place_ids(tmp_path):
    import json
    import time

    history = CourseHistory(tmp_path / 'history.sqlite3')
    await history.ensure_available()
    with sqlite3.connect(history.path) as db:
        db.execute('INSERT INTO courses VALUES (?, ?, ?, ?)', ('user', 'record', '["verified"]', time.time()))
        db.execute('INSERT INTO course_plans VALUES (?, ?, ?)', ('user', 'record', json.dumps({'place_ids': ['spoofed'], 'attraction_ids': ['verified', 'spoofed'], 'areas': ['  Seoul  '], 'experiences': ['culture', 'invented']})))
    assert await history.recent_profiles('user') == [{'place_ids': ['verified'], 'attraction_ids': ['verified'], 'areas': ['seoul'], 'experiences': ['culture']}]

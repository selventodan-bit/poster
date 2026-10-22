import yaml
import pytest

from poster.models import RecognitionInfo
from poster.project import Project, mark_ready
from poster.storage.local import Registry
from poster.recognition.service import RecognitionSession
from poster.fixtures_recognition import serve_recognition_fixture


def test_hot_registry_accepts_only_verified_new_projects(tmp_path, make_project):
    legacy = make_project("https://safe.test")
    site = legacy.site.model_copy(
        update={"recognition": RecognitionInfo(state="HTTP_CANDIDATE", browser_verified=True)}
    )
    candidate = Project(site, legacy.workflow, "candidate")
    folder = tmp_path / "classic"
    folder.mkdir()
    (folder / "site.yaml").write_text(yaml.safe_dump(site.model_dump()))
    (folder / "workflow.yaml").write_text(yaml.safe_dump(candidate.workflow.model_dump(by_alias=True)))
    mark_ready(folder)
    registry = Registry(tmp_path)
    registry.poll()
    assert not registry.projects
    verified = site.model_copy(
        update={
            "recognition": RecognitionInfo(state="HTTP_VERIFIED", browser_verified=True, http_verified=True)
        }
    )
    (folder / "site.yaml").write_text(yaml.safe_dump(verified.model_dump()))
    mark_ready(folder)
    assert len(registry.poll()) == 1 and "classic" in registry.projects


async def test_invalid_key_blocks_before_first_mutation(tmp_path, monkeypatch):
    monkeypatch.setenv("POSTER_SESSION_KEY", "invalid-key")
    with serve_recognition_fixture() as (server, base):
        session = RecognitionSession(base + "/recognize/ajax", tmp_path / "profile", headless=True)
        try:
            await session.start()
            await session.recognize()
            with pytest.raises(ValueError, match="SESSION_KEY_INVALID"):
                await session.test_publish("Title", "Body", "test", confirmed=True)
            assert not server.posts and not server.write_counts
        finally:
            await session.close()

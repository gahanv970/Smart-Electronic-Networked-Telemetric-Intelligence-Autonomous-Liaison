"""
Unit tests for Memory and Personalization Service:
Context storage, sensitive secret rejection, preferences, context injection, and restart persistence.
"""

from pathlib import Path
import tempfile
import pytest

from memory_service import MemoryService


@pytest.fixture
def temp_memory_service():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "memories_test.db"
        prefs_path = Path(tmpdir) / "prefs_test.json"
        svc = MemoryService(db_path=db_path, prefs_path=prefs_path)
        yield svc, db_path, prefs_path


def test_memory_crud_operations(temp_memory_service):
    svc, _, _ = temp_memory_service

    mem = svc.add_memory("User is a senior software engineer", category="fact")
    assert mem.id is not None
    assert mem.category == "fact"
    assert "senior software engineer" in mem.content

    retrieved = svc.get_memory(mem.id)
    assert retrieved is not None
    assert retrieved.content == mem.content

    updated = svc.update_memory(mem.id, "User is a principal software engineer", category="fact")
    assert "principal software engineer" in updated.content

    deleted = svc.delete_memory(mem.id)
    assert deleted is True
    assert svc.get_memory(mem.id) is None


def test_security_secret_blocking(temp_memory_service):
    svc, _, _ = temp_memory_service

    # Passwords
    with pytest.raises(ValueError) as exc:
        svc.add_memory("My database password is SuperSecretPassword123!")
    assert "Security violation" in str(exc.value)

    # API keys
    with pytest.raises(ValueError) as exc:
        svc.add_memory("Store my api key: AIzaSyD9483928492849284928492849284928")
    assert "Security violation" in str(exc.value)

    # Bearer tokens
    with pytest.raises(ValueError) as exc:
        svc.add_memory("Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.xyz12345678901234567890")
    assert "Security violation" in str(exc.value)


def test_user_preferences_and_context_injection(temp_memory_service):
    svc, _, _ = temp_memory_service

    svc.save_preferences({
        "user_name": "Tony Stark",
        "preferred_tone": "Brilliant and charismatic",
        "code_language_preference": "Python and C++",
    })

    svc.add_memory("Prefers dark glass UI themes", category="preference")
    svc.add_memory("Building iron suit operating system", category="project")

    context = svc.build_context_for_prompt()
    assert context is not None
    assert "Tony Stark" in context
    assert "Brilliant and charismatic" in context
    assert "dark glass UI themes" in context


def test_disabled_memory_behavior(temp_memory_service):
    svc, _, _ = temp_memory_service
    svc.add_memory("Test memory", category="fact")

    svc.set_memory_enabled(False)
    assert svc.build_context_for_prompt() is None

    svc.set_memory_enabled(True)
    assert svc.build_context_for_prompt() is not None


def test_clear_all_memories(temp_memory_service):
    svc, _, _ = temp_memory_service
    svc.add_memory("Mem 1")
    svc.add_memory("Mem 2")
    assert len(svc.get_all_memories()) == 2

    count = svc.clear_all_memories()
    assert count == 2
    assert len(svc.get_all_memories()) == 0


def test_memory_restart_persistence(temp_memory_service):
    svc1, db_path, prefs_path = temp_memory_service
    svc1.add_memory("Persistent fact 1", category="fact")
    svc1.save_preferences({"user_name": "Bruce Wayne"})

    # Application restart simulation
    svc2 = MemoryService(db_path=db_path, prefs_path=prefs_path)
    all_mem = svc2.get_all_memories()
    assert len(all_mem) == 1
    assert all_mem[0].content == "Persistent fact 1"
    assert svc2.get_preferences()["user_name"] == "Bruce Wayne"

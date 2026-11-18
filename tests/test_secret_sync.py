from pathlib import Path


def test_sync_reads_only_openai_from_env_without_returning_it(tmp_path):
    from scripts.sync_secrets import sync_openai
    env = tmp_path / '.env.local'
    env.write_text('UNRELATED_SECRET=leave-alone\nOPENAI_API_KEY="test-project-key"\n')
    destination = tmp_path / 'secrets' / 'openai_key'
    assert sync_openai(env, destination) == 'configured'
    assert destination.read_text() == 'test-project-key'
    assert not (destination.parent / 'UNRELATED_SECRET').exists()


def test_missing_env_never_erases_existing_key(tmp_path):
    from scripts.sync_secrets import sync_openai
    destination = tmp_path / 'openai_key'
    destination.write_text('existing-key')
    assert sync_openai(tmp_path / 'absent', destination) == 'unchanged'
    assert destination.read_text() == 'existing-key'

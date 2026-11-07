import importlib.util
from pathlib import Path


def scanner():
    path = Path(__file__).resolve().parents[2] / 'tools/check_release.py'
    spec = importlib.util.spec_from_file_location('release_guard', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_guard_rejects_credential_and_private_state_without_printing_values(tmp_path, capsys):
    module = scanner()
    token = 'ghp_' + 'x' * 36
    (tmp_path / 'accidental.txt').write_text(token)
    private = tmp_path / 'private'
    private.mkdir()
    (private / 'state.json').write_text('{}')
    count, findings = module.scan(tmp_path)
    assert count == 2
    assert {x[2] for x in findings} == {'github-token', 'runtime-or-credential-path'}
    assert token not in str(findings)
    assert capsys.readouterr().out == ''


def test_guard_accepts_synthetic_configuration_and_fixture_paths(tmp_path):
    module = scanner()
    (tmp_path / 'config.example.toml').write_text('bot_token = "123456:replace-me"\n')
    (tmp_path / 'fixture.txt').write_text('/Users/example/Project/Exports\n')
    count, findings = module.scan(tmp_path)
    assert count == 2 and findings == []

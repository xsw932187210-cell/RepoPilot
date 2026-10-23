"""Evaluator-only symptom check: the failing hook must actually execute."""
from cookiecutter import hooks


def test_public_hook_symptom(tmp_path):
    script = tmp_path / 'pre_gen_project.py'
    script.write_text(
        "from pathlib import Path\n"
        "Path('hook-ran').write_text('yes')\n"
        "raise SystemExit(1)\n"
    )
    rejected = False
    try:
        hooks.run_script(str(script), str(tmp_path))
    except Exception:
        rejected = True
    assert (tmp_path / 'hook-ran').read_text() == 'yes'
    assert rejected, 'Failed pre-generation hook returned normally'

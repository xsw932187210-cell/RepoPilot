"""Evaluator-only check of the public symptom, independent of upstream mocks."""
import click
from click.testing import CliRunner
from cookiecutter.prompt import read_user_choice


def test_public_prompt_symptom():
    @click.command()
    def command():
        assert read_user_choice('my_choice', ['a', 'b']) == 'a'

    result = CliRunner().invoke(command, input='1\n')
    assert result.exit_code == 0
    assert result.output.count('1, 2') == 1

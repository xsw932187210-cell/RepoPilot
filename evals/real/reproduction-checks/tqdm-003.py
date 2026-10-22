"""Evaluator-only direct check of the public open-file boolean symptom."""
from tqdm import tqdm


def test_public_file_symptom(tmp_path):
    source = tmp_path / 'payload.bin'
    source.write_bytes(b'abc')
    with source.open('rb') as stream:
        wrapped = tqdm(stream)
        try:
            assert bool(wrapped)
        finally:
            wrapped.close()

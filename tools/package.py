"""Build distributable archives using an explicit file list, without local data."""
import ast
import hashlib
import json
from pathlib import Path
import re
import zipfile

ROOT = Path(__file__).resolve().parents[1]
NAME = 'astrbot_plugin_mahjongsoul'


def package():
    metadata = (ROOT / 'metadata.yaml').read_text('utf-8')
    version = re.search(r'^version: (v[0-9.]+)$', metadata, re.MULTILINE).group(1)
    files = [ROOT / name for name in (
        '__init__.py', '_common.py', '_conf_schema.json', 'config.py',
        'config.reference.json', 'errors.py', 'main.py', 'metadata.yaml',
        'requirements.txt', 'README.md', 'LICENSE', 'NOTICE', 'UPSTREAM.md',
        'CHANGELOG.md', 'CONTRIBUTING.md', 'requirements-dev.txt',
        'pytest.ini', '.gitignore', '.gitattributes',
    )]
    for directory in ('network', 'paifuya', 'utils', 'tests', 'tools'):
        files.extend((ROOT / directory).rglob('*.py'))
    files.extend((ROOT / 'docs').glob('*.md'))
    files.extend((ROOT / 'paifuya' / 'assets').rglob('*.png'))
    files.extend([ROOT / 'licenses/LICENSE', ROOT / 'licenses/Apache-2.0.txt',
                  ROOT / 'paifuya/NotoSansCJK-Regular.otf'])
    files = sorted(set(files))
    for file in files:
        assert file.is_file() and not file.is_symlink(), file
        assert file.resolve().is_relative_to(ROOT.resolve()), file
        if file.suffix == '.py':
            tree = ast.parse(file.read_text('utf-8'), filename=str(file))
            for node in ast.walk(tree):
                modules = ([a.name for a in node.names] if isinstance(node, ast.Import)
                           else [node.module or ''] if isinstance(node, ast.ImportFrom) else [])
                assert not any(m.startswith('nonebot') for m in modules), file
    schema = json.loads((ROOT / '_conf_schema.json').read_text('utf-8'))
    for key in ('majsoul_ai_api_key', 'majsoul_paifuya_api_key'):
        assert schema[key]['default'] == '', key
    out = ROOT / 'dist'
    out.mkdir(exist_ok=True)
    archive = out / f'{NAME}_{version}.zip'
    with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for file in files:
            z.write(file, f'{NAME}/{file.relative_to(ROOT).as_posix()}')
    with zipfile.ZipFile(archive) as z:
        assert z.testzip() is None
        assert not any(n.startswith(f'{NAME}/data/') or '/.git/' in n or '__pycache__' in n for n in z.namelist())
    checksum = hashlib.sha256(archive.read_bytes()).hexdigest()
    (out / 'SHA256SUMS.txt').write_text(f'{checksum}  {archive.name}\n', 'utf-8')
    print(f'Packaged {len(files)} files: {archive.name}')
    print(f'SHA256: {checksum}')


if __name__ == '__main__':
    package()

"""Create a reproducible release ZIP from an already-built production extension."""
import base64
import hashlib
import json
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

root = Path(__file__).resolve().parents[1]
build = root / '.output/chrome-mv3'
manifest = json.loads((build / 'manifest.json').read_text())
version = json.loads((root / 'package.json').read_text())['version']
assert manifest['version'] == version, 'Build version differs from package version'
digest = hashlib.sha256(base64.b64decode(manifest['key'])).hexdigest()[:32]
extension_id = ''.join(chr(ord('a') + int(char, 16)) for char in digest)
assert extension_id == 'ifednlgahhhflcbaojjhfphiockifodk', 'Published extension identity changed'
assert not any(origin.startswith('http:') or origin == '<all_urls>' for origin in manifest['host_permissions']), 'Release requires production endpoints'
assert set(manifest['permissions']) == {'activeTab', 'tabCapture', 'offscreen', 'storage', 'sidePanel', 'scripting'}, 'Review changed permissions before packaging'

entries = {path.relative_to(build).as_posix(): path.read_bytes() for path in build.rglob('*') if path.is_file()}
for name in entries:
    assert not any(part.startswith('.') or part in {'tests', 'node_modules'} for part in Path(name).parts), f'Unexpected release file: {name}'
    assert not name.endswith(('.map', '.pem', '.key', '.log', '.wav', '.m4a')), f'Unexpected release file: {name}'
for name in ('LICENSE', 'NOTICE'):
    entries[name] = (root.parent / name).read_bytes()

archive = root / f'.output/meeting-intelligence-{version}.zip'
with ZipFile(archive, 'w', compression=ZIP_DEFLATED, compresslevel=9) as output:
    for name, content in sorted(entries.items()):
        entry = ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
        entry.compress_type = ZIP_DEFLATED
        entry.external_attr = 0o100644 << 16
        output.writestr(entry, content)
with ZipFile(archive) as output:
    assert output.testzip() is None, 'Release ZIP integrity check failed'
checksum = hashlib.sha256(archive.read_bytes()).hexdigest()
archive.with_suffix('.zip.sha256').write_text(f'{checksum}  {archive.name}\n')
print(f'Packaged {archive.name} ({archive.stat().st_size:,} bytes), ID {extension_id}')
print(f'SHA-256: {checksum}')

from base64 import b64encode
from hashlib import sha256
import json
import re
import struct

import pytest

from app.meetings.avatar import _CSP, _FILES, _WEB


@pytest.mark.parametrize(
    ("file", "digest"),
    [
        ("sophia_formal.glb", "329af2fbc680355ac09488a4eedae15650ad0a6b48e71aef304129aed2e170a3"),
        ("daniel_formal.glb", "11cd9aba59f694ddd09e32e838b61df70c408c5a8ddf69faa038efeeeebee869"),
    ],
)
def test_camera_models_keep_the_approved_binary_and_animation_controls(file, digest):
    filename, content_type = _FILES[f"/models/{file}"]
    model = (_WEB / filename).read_bytes()
    assert content_type == "model/gltf-binary"
    assert sha256(model).hexdigest() == digest

    magic, version, _, json_size, kind = struct.unpack_from("<4sIIII", model)
    assert (magic, version, kind) == (b"glTF", 2, 0x4E4F534A)
    data = json.loads(model[20:20 + json_size])
    assert data["skins"]
    morphs = {name for mesh in data["meshes"] for name in mesh.get("extras", {}).get("targetNames", [])}
    assert {"jawOpen", "eyeBlinkLeft", "eyeBlinkRight", "mouthSmileLeft", "browInnerUp", "eyeWideLeft"} <= morphs
    assert {"Idle", "Think", "Talk"} <= {clip["name"] for clip in data["animations"]}


def test_camera_csp_allows_the_pinned_three_import_map():
    html = (_WEB / "index.html").read_text()
    import_map = re.search(r'<script type="importmap">(.*?)</script>', html).group(1)
    digest = b64encode(sha256(import_map.encode()).digest()).decode()
    assert f"'sha256-{digest}'" in _CSP

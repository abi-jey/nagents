"""Image placeholders refer to private real files, with no server-owned names."""

import base64
import os
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import url2pathname

from nagents.tools.output import ToolOutput
from nagents.types import ImageContent


def test_image_file_link(tmp_path: Path) -> None:
    data = b"\x89PNG\r\n\x1a\nfixture"
    image = ImageContent(base64_data=base64.b64encode(data).decode(), media_type="image/png", filename="../../evil")
    output = ToolOutput(text="Screenshot", images=(image,))
    text = output.save_for_display(tmp_path / "folder with spaces")
    uri = text.splitlines()[-1].split("] ", 1)[1]
    assert uri.startswith("file:///")
    assert "%20" in uri
    path = Path(url2pathname(urlsplit(uri).path))
    assert path.parent == tmp_path / "folder with spaces"
    assert path.read_bytes() == data
    if os.name == "posix":
        assert path.stat().st_mode & 0o777 == 0o600
    assert output.save_for_display(path.parent) != text


def test_saving_failure_keeps_placeholder(tmp_path: Path) -> None:
    blocked = tmp_path / "not-directory"
    blocked.write_text("x")
    output = ToolOutput(images=(ImageContent(base64_data="YWJj", media_type="image/png"),))
    assert "local file unavailable" in output.save_for_display(blocked)

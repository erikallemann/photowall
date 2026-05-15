import io
import zipfile

import pytest
from PIL import Image

import photowall


@pytest.fixture()
def client(tmp_path, monkeypatch):
    upload_dir = tmp_path / "uploads"
    upload_dir.mkdir()
    metadb_path = tmp_path / "metadata_index.json"

    monkeypatch.setattr(photowall, "UPLOAD_DIR", upload_dir)
    monkeypatch.setattr(photowall, "PHOTO_DIR", upload_dir)
    monkeypatch.setattr(photowall, "PHOTO_READONLY", False)
    monkeypatch.setattr(photowall, "PHOTO_RECURSIVE", False)
    monkeypatch.setattr(photowall, "PHOTO_SCAN_TTL", 0)
    monkeypatch.setattr(photowall, "ALLOW_UPLOAD", False)
    monkeypatch.setattr(photowall, "ALLOW_UPLOAD_EFFECTIVE", False)
    monkeypatch.setattr(photowall, "UPLOAD_PIN", "")
    monkeypatch.setattr(photowall, "VIEW_PIN", "")
    monkeypatch.setattr(photowall, "METADB_PATH", metadb_path)
    monkeypatch.setattr(photowall, "_metadb", {})
    monkeypatch.setattr(photowall, "_scan_cache", {})

    photowall.app.config.update(TESTING=True)
    with photowall.app.test_client() as test_client:
        yield test_client


def _small_jpeg_bytes() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (2, 2), color=(255, 0, 0)).save(buf, format="JPEG")
    return buf.getvalue()


def _seed_image(name: str, data: bytes | None = None) -> None:
    (photowall.UPLOAD_DIR / name).write_bytes(data or _small_jpeg_bytes())


def test_home_wall_and_slideshow_render(client):
    assert client.get("/").status_code == 200
    assert client.get("/wall").status_code == 200
    assert client.get("/slideshow").status_code == 200


@pytest.mark.parametrize(
    ("path", "title", "css_path"),
    [
        ("/", "Upload Photos", "/static/upload_disabled.css"),
        ("/wall", "Photo Wall", "/static/wall.css"),
        ("/slideshow", "Slideshow", "/static/slideshow.css"),
        ("/admin", "Admin Photowall", "/static/admin.css"),
    ],
)
def test_pages_render_expected_html_and_css_links(client, path, title, css_path):
    response = client.get(path)

    assert response.status_code == 200
    assert response.mimetype == "text/html"
    html = response.get_data(as_text=True)
    assert f"<title>{title}</title>" in html
    assert f'href="{css_path}"' in html


@pytest.mark.parametrize(
    "css_path",
    [
        "/static/wall.css",
        "/static/upload_form.css",
        "/static/admin.css",
        "/static/slideshow.css",
        "/static/upload_disabled.css",
        "/static/locked.css",
    ],
)
def test_extracted_css_files_are_served(client, css_path):
    response = client.get(css_path)

    assert response.status_code == 200
    assert response.mimetype == "text/css"
    assert response.get_data(as_text=True).strip()


def test_upload_enabled_home_page_links_upload_form_css(client, monkeypatch):
    monkeypatch.setattr(photowall, "ALLOW_UPLOAD", True)
    monkeypatch.setattr(photowall, "ALLOW_UPLOAD_EFFECTIVE", True)

    response = client.get("/")

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert '<form id="uploadForm">' in html
    assert 'href="/static/upload_form.css"' in html
    assert 'src="/static/upload_form.js"' in html


def test_admin_page_links_admin_javascript(client):
    response = client.get("/admin")

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert 'src="/static/admin.js"' in html


def test_slideshow_page_links_slideshow_javascript(client):
    response = client.get("/slideshow")

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert 'src="/static/slideshow.js"' in html


def test_upload_form_javascript_is_served(client):
    response = client.get("/static/upload_form.js")

    assert response.status_code == 200
    assert response.mimetype in {"application/javascript", "text/javascript"}
    js = response.get_data(as_text=True)
    assert "fetch('/upload'" in js
    assert "X-Upload-Pin" in js
    assert "Upload successful!" in js


def test_admin_javascript_is_served(client):
    response = client.get("/static/admin.js")

    assert response.status_code == 200
    assert response.mimetype in {"application/javascript", "text/javascript"}
    js = response.get_data(as_text=True)
    assert "/list" in js
    assert "/delete" in js
    assert "X-Admin-Pin" in js
    assert "localStorage" in js


def test_slideshow_javascript_is_served(client):
    response = client.get("/static/slideshow.js")

    assert response.status_code == 200
    assert response.mimetype in {"application/javascript", "text/javascript"}
    js = response.get_data(as_text=True)
    assert "/list" in js
    assert "setInterval" in js
    assert "next" in js
    assert "prev" in js


def test_locked_view_page_links_locked_css(client, monkeypatch):
    monkeypatch.setattr(photowall, "VIEW_PIN", "secret")

    response = client.get("/wall")

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert "The wall is locked." in html
    assert 'href="/static/locked.css"' in html


def test_list_returns_json_items(client):
    _seed_image("1700000000000-abcdef-party.jpg")

    response = client.get("/list")

    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    data = response.get_json()
    assert data["items"] == [
        {
            "name": "1700000000000-abcdef-party.jpg",
            "url": "/uploads/1700000000000-abcdef-party.jpg",
            "ts": 1700000000000,
            "tk": None,
            "cap": "",
        }
    ]


def test_uploads_are_disabled_by_default(client):
    response = client.post(
        "/upload",
        data={"file": (io.BytesIO(_small_jpeg_bytes()), "party.jpg")},
        content_type="multipart/form-data",
    )

    assert response.status_code == 403
    assert list(photowall.UPLOAD_DIR.iterdir()) == []


def test_upload_can_be_enabled_and_accepts_small_valid_image(client, monkeypatch):
    monkeypatch.setattr(photowall, "ALLOW_UPLOAD", True)
    monkeypatch.setattr(photowall, "ALLOW_UPLOAD_EFFECTIVE", True)

    response = client.post(
        "/upload",
        data={
            "caption": "hello there",
            "file": (io.BytesIO(_small_jpeg_bytes()), "party.jpg"),
        },
        content_type="multipart/form-data",
    )

    assert response.status_code == 201
    saved = list(photowall.UPLOAD_DIR.iterdir())
    assert len(saved) == 1
    assert saved[0].suffix == ".jpg"
    assert "__hello_there" in saved[0].stem


def test_oversized_upload_is_rejected(client, monkeypatch):
    monkeypatch.setattr(photowall, "ALLOW_UPLOAD", True)
    monkeypatch.setattr(photowall, "ALLOW_UPLOAD_EFFECTIVE", True)

    response = client.post(
        "/upload",
        data={"file": (io.BytesIO(b"x" * (photowall.MAX_BYTES + 1)), "too-big.jpg")},
        content_type="multipart/form-data",
    )

    assert response.status_code == 413
    assert list(photowall.UPLOAD_DIR.iterdir()) == []


def test_download_returns_zip_for_default_uploads_folder(client):
    _seed_image("1700000000000-abcdef-party.jpg")

    response = client.get("/download")

    assert response.status_code == 200
    assert response.mimetype == "application/zip"
    assert response.headers["Content-Disposition"].startswith("attachment;")

    with zipfile.ZipFile(io.BytesIO(response.data)) as zf:
        assert zf.namelist() == ["1700000000000-abcdef-party.jpg"]

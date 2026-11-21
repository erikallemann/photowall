import io
import time
from html.parser import HTMLParser
from types import SimpleNamespace
from urllib.parse import unquote
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
    monkeypatch.setattr(photowall, "EVENT_EXPIRES_AT", None)
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


def test_public_photo_keeps_immutable_cache(client):
    _seed_image("public.jpg")
    response = client.get("/uploads/public.jpg")
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "public, max-age=604800, immutable"


@pytest.mark.parametrize("library_mode", [False, True])
def test_private_photo_access_including_range_and_revalidation(client, monkeypatch, tmp_path, library_mode):
    monkeypatch.setattr(photowall, "VIEW_PIN", "viewer")
    photo = _small_jpeg_bytes()
    if library_mode:
        library = tmp_path / "library"
        folder = library / "folder"
        folder.mkdir(parents=True)
        (folder / "private.jpg").write_bytes(photo)
        monkeypatch.setattr(photowall, "PHOTO_DIR", library)
        monkeypatch.setattr(photowall, "PHOTO_READONLY", True)
        url = "/uploads/folder/private.jpg"
    else:
        _seed_image("private.jpg", photo)
        url = "/uploads/private.jpg"

    view_header = {"X-View-Pin": "viewer"}
    original = client.get(url, headers=view_header)
    assert original.status_code == 200
    assert original.data == photo
    assert original.headers["Cache-Control"] == "private, no-store"
    assert {"Cookie", "X-View-Pin"} <= set(original.vary)

    cache_headers = {"If-None-Match": original.headers["ETag"]}
    range_headers = {"Range": "bytes=0-3"}
    for headers in ({}, {"X-View-Pin": "wrong"}, cache_headers, range_headers):
        denied = client.get(url, headers=headers)
        assert denied.status_code == 403
        assert denied.headers["Cache-Control"] == "private, no-store"
        assert denied.data == b"Forbidden"
    assert client.head(url).status_code == 403

    assert client.post("/enter", data={"pin": "viewer"}).status_code == 303
    assert client.get(url + "?v=123").data == photo
    for headers, status, expected_bytes in ((cache_headers, 304, b""), (range_headers, 206, photo[:4])):
        response = client.get(url, headers=headers)
        assert response.status_code == status
        assert response.headers["Cache-Control"] == "private, no-store"
        assert response.data == expected_bytes


def test_viewer_access_ends_at_event_deadline(client, monkeypatch):
    monkeypatch.setattr(photowall, "VIEW_PIN", "viewer")
    deadline = time.time() + 60
    now = deadline - 1
    monkeypatch.setattr(photowall, "EVENT_EXPIRES_AT", deadline)
    monkeypatch.setattr(photowall, "time", SimpleNamespace(time=lambda: now, strftime=time.strftime))
    _seed_image("event.jpg")

    denied_zip = client.get("/download")
    assert denied_zip.status_code == 403
    assert denied_zip.headers["Cache-Control"] == "private, no-store"
    assert client.post("/enter", data={"pin": "viewer"}).status_code == 303
    for page, script in (("/wall", "wall.js"), ("/slideshow", "slideshow.js")):
        response = client.get(page)
        assert response.status_code == 200
        assert f"/static/{script}" in response.get_data(as_text=True)
    listing = client.get("/list")
    assert listing.headers["Cache-Control"] == "no-store"
    item = listing.json["items"][0]
    original = client.get(item["url"])
    tile_url = f"{item['url']}?v={item['ts']}"
    assert original.status_code == 200
    assert client.get(tile_url).data == original.data
    archive = client.get("/download")
    assert archive.status_code == 200
    assert archive.headers["Cache-Control"] == "private, no-store"

    now = deadline
    with photowall.app.test_client() as guest:
        for viewer in (client, guest):
            for path in ("/", "/wall", "/slideshow", "/dirs", "/list", "/download", item["url"], tile_url):
                expired = viewer.get(path)
                assert expired.status_code == 410
                assert expired.cache_control.no_store
                assert expired.data == b"Event has expired."
        for headers in (
            {"X-View-Pin": "viewer", "If-None-Match": original.headers["ETag"]},
            {"X-View-Pin": "viewer", "Range": "bytes=0-3"},
        ):
            assert guest.get(tile_url, headers=headers).status_code == 410
        assert guest.post("/enter", data={"pin": "viewer"}).status_code == 410
    assert (photowall.UPLOAD_DIR / "event.jpg").exists()


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


def test_wall_page_links_wall_javascript(client):
    response = client.get("/wall")

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert 'src="/static/wall.js"' in html


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


def test_wall_javascript_is_served(client):
    response = client.get("/static/wall.js")

    assert response.status_code == 200
    assert response.mimetype in {"application/javascript", "text/javascript"}
    js = response.get_data(as_text=True)
    assert "/list" in js
    assert "/dirs" in js
    assert "localStorage" in js
    assert "refresh" in js
    assert "viewer" in js
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
        assert zf.namelist() == ["1700000000000-abcdef-party.jpg", "index.html"]
        assert zf.read("1700000000000-abcdef-party.jpg") == _small_jpeg_bytes()


def test_download_gallery_has_local_links_and_escaped_captions(client):
    names = [
        '1700000000000-abcdef-party__Hello_&_"friends".jpg',
        'special #?% café.png',
        'photo__<img_src=x_onerror=alert(1)>.gif',
        'javascript:photo.webp',
    ]
    for name in names:
        _seed_image(name)
    (photowall.UPLOAD_DIR / "ignored.txt").write_text("not a photo")
    (photowall.UPLOAD_DIR / "nested").mkdir()

    class GalleryParser(HTMLParser):
        def __init__(self):
            super().__init__()
            self.links = []
            self.images = []
            self.scripts = []

        def handle_starttag(self, tag, attrs):
            attrs = dict(attrs)
            if tag == "a" and attrs.get("class") == "photo-link":
                self.links.append(attrs["href"])
            if tag == "img" and "src" in attrs:
                self.images.append(attrs)
            if tag == "script":
                self.scripts.append(attrs)

    response = client.get("/download")
    with zipfile.ZipFile(io.BytesIO(response.data)) as zf:
        assert set(zf.namelist()) == {*names, "index.html"}
        html = zf.read("index.html").decode("utf-8")
        parser = GalleryParser()
        parser.feed(html)
        assert [unquote(link[2:]) for link in parser.links] == sorted(names)
        assert all(link.startswith("./") for link in parser.links)
        assert [img["src"] for img in parser.images] == parser.links
        assert all("onerror" not in img for img in parser.images)
        assert parser.scripts == [{}]
        assert '<img src=x onerror=alert(1)>' not in html
        assert 'Hello &amp; &#34;friends&#34;' in html
        assert 'fetch(' not in html
        for name in names:
            assert zf.read(name) == _small_jpeg_bytes()


def test_download_empty_gallery(client):
    response = client.get("/download")
    with zipfile.ZipFile(io.BytesIO(response.data)) as zf:
        assert zf.namelist() == ["index.html"]
        assert "No photos in this export." in zf.read("index.html").decode("utf-8")


def test_download_still_requires_view_access(client, monkeypatch):
    monkeypatch.setattr(photowall, "VIEW_PIN", "private")
    assert client.get("/download").status_code == 403


def test_download_still_rejects_external_photo_root(client, monkeypatch, tmp_path):
    monkeypatch.setattr(photowall, "PHOTO_DIR", tmp_path / "external")
    assert client.get("/download").status_code == 409

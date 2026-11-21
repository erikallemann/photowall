import io
import struct
import time
import zlib
from html.parser import HTMLParser
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import unquote
import zipfile

import pytest
from PIL import Image
from werkzeug.datastructures import FileStorage

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


@pytest.mark.parametrize("pin_payload", [{"data": {"pin": "viewer"}}, {"json": {"pin": "viewer"}}])
@pytest.mark.parametrize(
    "target",
    [
        "https://example.invalid/",
        "slideshow",
        "//example.invalid/",
        r"/\example.invalid/",
        "/%2Fexample.invalid/",
        "/%5Cexample.invalid/",
        "/%252Fexample.invalid/",
        "/\t/slideshow",
    ],
)
def test_enter_pin_uses_wall_for_unsafe_redirect(client, monkeypatch, target, pin_payload):
    monkeypatch.setattr(photowall, "VIEW_PIN", "viewer")

    response = client.post("/enter", query_string={"next": target}, **pin_payload)

    assert response.status_code == 303
    assert response.headers["Location"] == "/wall"
    with client.session_transaction() as session:
        assert session["view_ok"] is True


@pytest.mark.parametrize(
    ("target", "location"),
    [
        (None, "/wall"),
        ("/slideshow?dir=party%2Fday%201#photos", "/slideshow?dir=party%2Fday%201#photos"),
        ("/photos/caf%C3%A9", "/photos/caf%C3%A9"),
    ],
)
def test_enter_pin_preserves_local_redirect(client, monkeypatch, target, location):
    monkeypatch.setattr(photowall, "VIEW_PIN", "viewer")
    query = {"next": target} if target is not None else {}

    response = client.post("/enter", query_string=query, data={"pin": "viewer"})

    assert response.status_code == 303
    assert response.headers["Location"] == location


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


@pytest.fixture()
def upload_client(client, monkeypatch):
    monkeypatch.setattr(photowall, "ALLOW_UPLOAD", True)
    return client


def _upload_photo(client, data, name="party.jpg", *, content_type=None, headers=None, caption="hello there"):
    file = (io.BytesIO(data), name)
    if content_type is not None:
        file += (content_type,)
    return client.post("/upload", data={"file": file, "caption": caption}, headers=headers)


def _image_bytes(image_format, size=(2, 2), frame_count=1):
    buf = io.BytesIO()
    first = Image.new("RGB", size, color="red")
    options = {}
    if frame_count > 1:
        colors = ("red", "blue", "green")
        frames = [Image.new("RGB", size, color=colors[frame % len(colors)]) for frame in range(1, frame_count)]
        options = {
            "save_all": True,
            "append_images": frames,
            "duration": 100,
        }
    first.save(buf, format=image_format, **options)
    return buf.getvalue()


def _assert_no_upload(client):
    assert list(photowall.UPLOAD_DIR.iterdir()) == []
    assert client.get("/list").get_json()["items"] == []
    assert not photowall.METADB_PATH.exists()


def test_uploads_are_disabled_by_default(client):
    assert _upload_photo(client, _small_jpeg_bytes()).status_code == 403
    _assert_no_upload(client)


@pytest.mark.parametrize("external_library", [False, True])
def test_uploads_require_a_writable_local_library(upload_client, monkeypatch, tmp_path, external_library):
    if external_library:
        library_dir = tmp_path / "library"
        library_dir.mkdir()
        monkeypatch.setattr(photowall, "PHOTO_DIR", library_dir)
    else:
        monkeypatch.setattr(photowall, "PHOTO_READONLY", True)

    assert _upload_photo(upload_client, _small_jpeg_bytes()).status_code == 403
    _assert_no_upload(upload_client)
    assert list(photowall.PHOTO_DIR.iterdir()) == []


def test_upload_pin_is_required_only_when_configured(upload_client, monkeypatch):
    monkeypatch.setattr(photowall, "UPLOAD_PIN", "uploader")
    photo = _small_jpeg_bytes()

    assert _upload_photo(upload_client, photo).status_code == 403
    assert _upload_photo(upload_client, photo, headers={"X-Upload-Pin": "wrong"}).status_code == 403
    _assert_no_upload(upload_client)
    assert _upload_photo(upload_client, photo, headers={"X-Upload-Pin": "uploader"}).status_code == 201


@pytest.mark.parametrize(
    ("image_format", "ext"),
    [("JPEG", ".jpg"), ("JPEG", ".jpeg"), ("PNG", ".png"), ("GIF", ".gif"), ("WEBP", ".webp")],
)
def test_upload_accepts_supported_formats(upload_client, image_format, ext):
    photo = _image_bytes(image_format)
    response = _upload_photo(upload_client, photo, "party" + ext)

    assert response.status_code == 201
    assert response.data == b"OK"
    saved, = photowall.UPLOAD_DIR.iterdir()
    assert saved.suffix == ext
    assert saved.stem.endswith("party__hello_there")
    assert saved.read_bytes() == photo
    assert upload_client.get(f"/uploads/{saved.name}").data == photo


def _png_with_size(size):
    png = _image_bytes("PNG")
    # Add a private ancillary chunk with a valid CRC before IEND.
    data = b"paDd" + bytes(size - len(png) - 12)
    chunk = struct.pack(">I", len(data) - 4) + data + struct.pack(">I", zlib.crc32(data))
    return png[:-12] + chunk + png[-12:]


@pytest.mark.parametrize(("extra_bytes", "status"), [(0, 201), (1, 413)])
def test_upload_file_size_boundary(upload_client, extra_bytes, status):
    photo = _png_with_size(10 * 1024 * 1024 + extra_bytes)
    response = _upload_photo(upload_client, photo, "party.png", caption="x" * 40)

    assert response.status_code == status
    if status == 201:
        saved, = photowall.UPLOAD_DIR.iterdir()
        assert saved.read_bytes() == photo
    else:
        _assert_no_upload(upload_client)


def test_oversized_request_is_rejected_before_multipart_is_read(upload_client):
    class UnreadableBody(io.BytesIO):
        def read(self, size=-1):
            pytest.fail("Oversized request body was read")

        def readinto(self, buffer):
            pytest.fail("Oversized request body was read")

    response = upload_client.post(
        "/upload",
        environ_overrides={
            "CONTENT_LENGTH": str(photowall.MAX_UPLOAD_REQUEST_BYTES + 1),
            "CONTENT_TYPE": "multipart/form-data; boundary=example",
            "wsgi.input": UnreadableBody(),
        },
    )

    assert response.status_code == 413
    _assert_no_upload(upload_client)


def test_streamed_request_is_bounded_without_content_length(upload_client):
    boundary = "example"
    body = (
        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="party.jpg"\r\n'
        "Content-Type: image/jpeg\r\n\r\n"
    ).encode() + bytes(photowall.MAX_UPLOAD_REQUEST_BYTES) + f"\r\n--{boundary}--\r\n".encode()
    stream = io.BytesIO(body)
    response = upload_client.post(
        "/upload",
        environ_overrides={
            "CONTENT_LENGTH": None,
            "CONTENT_TYPE": f"multipart/form-data; boundary={boundary}",
            "HTTP_TRANSFER_ENCODING": "chunked",
            "wsgi.input": stream,
            "wsgi.input_terminated": True,
        },
    )

    assert response.status_code == 413
    assert stream.tell() == photowall.MAX_UPLOAD_REQUEST_BYTES
    _assert_no_upload(upload_client)


@pytest.mark.parametrize(
    ("image_format", "ext", "trim"),
    [("JPEG", ".jpg", 10), ("PNG", ".png", 4), ("GIF", ".gif", 1), ("WEBP", ".webp", 4)],
)
def test_upload_rejects_truncated_supported_images(upload_client, image_format, ext, trim):
    photo = _image_bytes(image_format)[:-trim]
    assert _upload_photo(upload_client, photo, "party" + ext).status_code == 400
    _assert_no_upload(upload_client)


@pytest.mark.parametrize(
    ("image_format", "filename", "content_type"),
    [
        ("PNG", "party.jpg", None),
        ("JPEG", "party.jpg", "image/png"),
        ("JPEG", "party.txt", None),
        ("BMP", "party.jpg", None),
        (None, "party.png", None),
    ],
)
def test_upload_rejects_mislabeled_or_unsupported_images(upload_client, image_format, filename, content_type):
    photo = _image_bytes(image_format) if image_format else b"broken image"
    assert _upload_photo(upload_client, photo, filename, content_type=content_type).status_code == 400
    _assert_no_upload(upload_client)


def test_upload_allows_generic_content_type(upload_client):
    response = _upload_photo(upload_client, _small_jpeg_bytes(), content_type="application/octet-stream")
    assert response.status_code == 201


def test_upload_rejects_excess_pixels_in_one_frame(upload_client):
    photo = _image_bytes("PNG", size=(5001, 5000))
    assert _upload_photo(upload_client, photo, "party.png").status_code == 413
    _assert_no_upload(upload_client)


@pytest.mark.parametrize(("image_format", "ext"), [("GIF", ".gif"), ("PNG", ".png"), ("WEBP", ".webp")])
def test_animated_upload_frame_limit(upload_client, image_format, ext):
    over_limit = _image_bytes(image_format, frame_count=photowall.MAX_FRAMES + 1)
    assert _upload_photo(upload_client, over_limit, "party" + ext).status_code == 413
    _assert_no_upload(upload_client)

    at_limit = _image_bytes(image_format, frame_count=photowall.MAX_FRAMES)
    assert _upload_photo(upload_client, at_limit, "party" + ext).status_code == 201
    saved, = photowall.UPLOAD_DIR.iterdir()
    assert saved.read_bytes() == at_limit


def test_upload_rejects_excess_pixels_across_frames(upload_client, monkeypatch):
    monkeypatch.setattr(photowall, "MAX_TOTAL_PIXELS", 7)
    photo = _image_bytes("GIF", frame_count=2)
    assert _upload_photo(upload_client, photo, "party.gif").status_code == 413
    _assert_no_upload(upload_client)


def test_upload_rejects_apng_with_undeclared_frames(upload_client):
    photo = bytearray(_image_bytes("PNG", frame_count=2))
    control = photo.index(b"acTL")
    struct.pack_into(">I", photo, control + 4, 1)
    struct.pack_into(">I", photo, control + 12, zlib.crc32(photo[control:control + 12]))

    assert _upload_photo(upload_client, photo, "party.png").status_code == 400
    _assert_no_upload(upload_client)


def test_upload_rejects_gif_frames_outside_the_canvas(upload_client):
    photo = bytearray(_image_bytes("GIF", frame_count=2))
    descriptor = photo.rindex(b"\x21\xf9\x04") + 8
    assert photo[descriptor] == ord(",")
    struct.pack_into("<H", photo, descriptor + 5, 3)  # widen the second frame beyond the canvas

    assert _upload_photo(upload_client, photo, "party.gif").status_code == 400
    _assert_no_upload(upload_client)


def test_upload_disconnect_during_multipart(upload_client):
    body = (
        b'--example\r\nContent-Disposition: form-data; name="file"; filename="party.jpg"\r\n'
        b"Content-Type: image/jpeg\r\n\r\n" + _small_jpeg_bytes()
    )
    response = upload_client.post(
        "/upload",
        environ_overrides={
            "CONTENT_LENGTH": str(len(body) + 20),
            "CONTENT_TYPE": "multipart/form-data; boundary=example",
            "wsgi.input": io.BytesIO(body),
        },
    )

    assert response.status_code == 400
    _assert_no_upload(upload_client)


def test_upload_cleans_up_after_interrupted_staging(upload_client, monkeypatch):
    def interrupted_save(self, destination):
        destination.write(b"partial image")
        destination.flush()
        stage = Path(destination.name)
        assert stage.parent == photowall.UPLOAD_DIR
        assert stage.exists()
        reader = photowall.app.test_client()
        assert reader.get("/list").get_json()["items"] == []
        assert reader.get(f"/uploads/{stage.name}").status_code == 404
        raise OSError("staging interrupted")

    monkeypatch.setattr(FileStorage, "save", interrupted_save)
    with pytest.raises(OSError, match="staging interrupted"):
        _upload_photo(upload_client, _small_jpeg_bytes())
    _assert_no_upload(upload_client)


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

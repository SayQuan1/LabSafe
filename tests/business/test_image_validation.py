"""Real Pillow and real SDK stubs; no live MinIO/IAM acceptance claim."""

import hashlib
import multiprocessing
import time
from io import BytesIO
from threading import Event, Timer
from unittest.mock import Mock, patch
from uuid import uuid4

import pytest
from botocore.stub import Stubber
from PIL import Image, PngImagePlugin

from apps.worker.image_validation import BoundedImagePrepare
from packages.application.image_validation import dimensions, normalize, prepare_image
from packages.domain.image_validation import RejectedImage, ValidatedImage, image_keys
from packages.domain.job_execution import ImageInput, LeaseLost
from packages.domain.security import ServiceError
from packages.storage.s3 import BUCKET, S3Settings, S3Storage
from tests.business.test_s3_storage import settings


def encoded(format="PNG", mode="RGB", size=(7, 5), **options):
    with Image.new(mode, size, 1) as source:
        output = BytesIO()
        source.save(output, format=format, **options)
        return output.getvalue()


def image_input(payload, mime="image/png"):
    tenant, upload = str(uuid4()), str(uuid4())
    return tenant, ImageInput(
        str(uuid4()),
        upload,
        str(uuid4()),
        "inspection_item",
        str(uuid4()),
        f"staging/{tenant}/{upload}",
        "pinned",
        hashlib.sha256(payload).hexdigest(),
        len(payload),
        mime,
        1,
        2,
    )


@pytest.mark.parametrize(
    "format,mime", [("JPEG", "image/jpeg"), ("PNG", "image/png"), ("WEBP", "image/webp")]
)
def test_real_formats_normalize_without_resize(format, mime):
    payload, width, height = normalize(encoded(format), mime)
    with Image.open(BytesIO(payload)) as result:
        assert (result.format, result.mode, result.size) == ("PNG", "RGB", (7, 5))
        assert not result.info and not result.getexif()
        result.load()
    assert (width, height) == (7, 5)


@pytest.mark.parametrize("mode", ["RGBA", "P", "L", "LA", "I;16"])
def test_png_modes_become_rgb(mode):
    output, _, _ = normalize(encoded(mode=mode), "image/png")
    with Image.open(BytesIO(output)) as result:
        assert result.mode == "RGB"


def test_exif_orientation_once_and_strip_gps_icc_text():
    exif = Image.Exif()
    exif[274], exif[270] = 6, "synthetic-private-description"
    pnginfo = PngImagePlugin.PngInfo()
    pnginfo.add_text("GPS", "synthetic-position")
    source = encoded(exif=exif, pnginfo=pnginfo, icc_profile=b"synthetic-profile")
    output, width, height = normalize(source, "image/png")
    assert (width, height) == (5, 7)
    with Image.open(BytesIO(output)) as image:
        assert image.info == {} and len(image.getexif()) == 0
    repeated, w2, h2 = normalize(output, "image/png")
    assert (w2, h2) == (5, 7) and repeated == output


@pytest.mark.parametrize(
    "payload,mime,code",
    [
        (b"<svg/>", "image/png", "IMAGE_INVALID"),
        (b"not an image", "image/png", "IMAGE_INVALID"),
        (encoded(), "image/jpeg", "UNSUPPORTED_MEDIA_TYPE"),
        (encoded()[:40], "image/png", "IMAGE_INVALID"),
        (encoded("BMP"), "image/png", "UNSUPPORTED_MEDIA_TYPE"),
    ],
)
def test_invalid_content_rejected(payload, mime, code):
    with pytest.raises(ServiceError) as caught:
        normalize(payload, mime)
    assert caught.value.code == code


@pytest.mark.parametrize("format,mime", [("PNG", "image/png"), ("WEBP", "image/webp")])
def test_animation_rejected(format, mime):
    with Image.new("RGB", (3, 3), "red") as first, Image.new("RGB", (3, 3), "blue") as second:
        stream = BytesIO()
        first.save(stream, format=format, save_all=True, append_images=[second], duration=100)
        with pytest.raises(ServiceError) as caught:
            normalize(stream.getvalue(), mime)
    assert caught.value.code == "IMAGE_INVALID"


@pytest.mark.parametrize(
    "size,allowed",
    [
        ((10000, 4000), True),
        ((10000, 4001), False),
        ((10001, 1), False),
        ((1, 10001), False),
        ((0, 1), False),
    ],
)
def test_dimension_boundaries(size, allowed):
    if allowed:
        dimensions(*size)
    else:
        with pytest.raises(ServiceError, match="dimensions"):
            dimensions(*size)


def test_oversized_real_image_header_rejected_before_full_decode():
    with pytest.raises(ServiceError) as caught:
        normalize(encoded(size=(10001, 1)), "image/png")
    assert caught.value.code == "IMAGE_TOO_LARGE"


def test_prepare_sdk_exact_get_copy_and_png_put_versions():
    original = encoded()
    tenant, input = image_input(original)
    analysis, w, h = normalize(original, input.mime_type)
    sha = hashlib.sha256(analysis).hexdigest()
    original_key, analysis_key = image_keys(tenant, input, sha)
    storage = S3Storage(settings())
    body = BytesIO(original)
    try:
        with Stubber(storage.internal) as stub:
            stub.add_response(
                "get_object",
                {
                    "Body": body,
                    "VersionId": "pinned",
                    "ContentLength": len(original),
                    "ContentType": "image/png",
                },
                {"Bucket": BUCKET, "Key": input.key, "VersionId": "pinned"},
            )
            stub.add_client_error(
                "head_object", "NoSuchKey", expected_params={"Bucket": BUCKET, "Key": original_key}
            )
            stub.add_response(
                "copy_object",
                {"VersionId": "original-v1"},
                {
                    "Bucket": BUCKET,
                    "Key": original_key,
                    "CopySource": {"Bucket": BUCKET, "Key": input.key, "VersionId": "pinned"},
                    "MetadataDirective": "REPLACE",
                    "ContentType": "image/png",
                },
            )
            stub.add_client_error(
                "head_object", "NoSuchKey", expected_params={"Bucket": BUCKET, "Key": analysis_key}
            )
            stub.add_response(
                "put_object",
                {"VersionId": "analysis-v1"},
                {
                    "Bucket": BUCKET,
                    "Key": analysis_key,
                    "Body": analysis,
                    "ContentType": "image/png",
                },
            )
            assert prepare_image(storage, tenant, input) == ValidatedImage(
                original_key, "original-v1", analysis_key, "analysis-v1", sha, w, h
            )
            stub.assert_no_pending_responses()
    finally:
        storage.close()
    assert body.closed


@pytest.mark.parametrize(
    "code",
    [
        "IMAGE_INVALID",
        "HASH_MISMATCH",
        "OBJECT_NOT_FOUND",
        "IMAGE_TOO_LARGE",
        "UNSUPPORTED_MEDIA_TYPE",
    ],
)
def test_content_error_never_writes_objects(code):
    storage = Mock()
    storage.read_staging.side_effect = ServiceError(code, 422, "safe")
    tenant, input = image_input(encoded())
    assert prepare_image(storage, tenant, input) == RejectedImage(code)
    storage.preserve_original.assert_not_called()
    storage.put_analysis.assert_not_called()


def test_decode_rejects_after_verified_read_before_writes():
    storage = Mock()
    storage.read_staging.return_value = b"not an image"
    tenant, input = image_input(b"not an image")
    assert prepare_image(storage, tenant, input) == RejectedImage("IMAGE_INVALID")
    storage.preserve_original.assert_not_called()


def test_storage_unavailable_after_original_write_never_returns_ready():
    storage = Mock()
    storage.read_staging.return_value = encoded()
    storage.preserve_original.return_value = "orphan-version"
    storage.put_analysis.side_effect = ServiceError("DEPENDENCY_UNAVAILABLE", 503, "safe")
    tenant, input = image_input(encoded())
    with pytest.raises(ServiceError) as caught:
        prepare_image(storage, tenant, input)
    assert caught.value.code == "DEPENDENCY_UNAVAILABLE"
    storage.delete_object.assert_not_called()


@pytest.mark.parametrize("version", [None, "", "null", "x" * 201, "with space"])
def test_written_objects_require_precise_version(version):
    with pytest.raises(ServiceError):
        S3Storage.result_version({"VersionId": version})


@pytest.mark.parametrize("variant", ["original", "analysis"])
@pytest.mark.parametrize("conflict", [False, True])
def test_existing_destination_reused_only_after_actual_hash(variant, conflict):
    payload = encoded()
    tenant, input = image_input(payload)
    sha = hashlib.sha256(payload).hexdigest()
    original, analysis = image_keys(tenant, input, sha)
    key = original if variant == "original" else analysis
    body = BytesIO(b"x" * len(payload) if conflict else payload)
    storage = S3Storage(settings())
    try:
        with Stubber(storage.internal) as stub:
            stub.add_response(
                "head_object",
                {
                    "VersionId": "existing-v1",
                    "ContentLength": len(payload),
                    "ContentType": "image/png",
                    "Metadata": {"sha256": sha},
                },
                {"Bucket": BUCKET, "Key": key},
            )
            stub.add_response(
                "get_object",
                {
                    "Body": body,
                    "VersionId": "existing-v1",
                    "ContentLength": len(payload),
                    "ContentType": "image/png",
                },
                {"Bucket": BUCKET, "Key": key, "VersionId": "existing-v1"},
            )

            def write():
                if variant == "original":
                    from packages.storage.s3 import ObjectVersion

                    return storage.preserve_original(
                        ObjectVersion(input.key, "pinned", len(payload), "image/png"), key, sha
                    )
                return storage.put_analysis(key, payload)

            if conflict:
                with pytest.raises(ServiceError) as caught:
                    write()
                assert caught.value.code == "INTERNAL_ERROR"
            else:
                assert write() == "existing-v1"
            stub.assert_no_pending_responses()  # No COPY/PUT on reuse or conflict.
    finally:
        storage.close()
    assert body.closed


@pytest.mark.parametrize("provider", ["AccessDenied", "SlowDown", "NoSuchVersion"])
def test_existing_destination_dependency_failure_never_overwrites(provider):
    payload = encoded()
    storage = S3Storage(settings())
    try:
        with Stubber(storage.internal) as stub:
            if provider == "NoSuchVersion":
                stub.add_response(
                    "head_object",
                    {
                        "VersionId": "existing-v1",
                        "ContentLength": len(payload),
                        "ContentType": "image/png",
                    },
                    {"Bucket": BUCKET, "Key": "controlled-analysis-key"},
                )
                stub.add_client_error("get_object", provider)
            else:
                stub.add_client_error("head_object", provider)
            with pytest.raises(ServiceError) as caught:
                storage.put_analysis("controlled-analysis-key", payload)
            assert caught.value.code == "DEPENDENCY_UNAVAILABLE"
            stub.assert_no_pending_responses()
    finally:
        storage.close()


def stalled_prepare(*args):
    time.sleep(60)


def synthetic_storage_decode_child(channel, _settings, tenant, input):
    # Real spawned process + Pillow, with clearly synthetic storage version IDs.
    payload, width, height = normalize(encoded(), input.mime_type)
    sha = hashlib.sha256(payload).hexdigest()
    original, analysis = image_keys(tenant, input, sha)
    channel.send(("result", ValidatedImage(original, "o-v1", analysis, "a-v1", sha, width, height)))
    channel.close()


def test_spawned_child_returns_real_decode_result(monkeypatch):
    monkeypatch.setattr(
        "apps.worker.image_validation.prepare_child", synthetic_storage_decode_child
    )
    tenant, input = image_input(encoded())
    result = BoundedImagePrepare(settings(), tenant)(input, Event())
    assert (result.width, result.height) == (7, 5)
    assert (
        result.analysis_sha256 == hashlib.sha256(normalize(encoded(), "image/png")[0]).hexdigest()
    )


@pytest.mark.parametrize("cancel", [False, True])
def test_real_stalled_child_killed_on_timeout_or_lease_loss(monkeypatch, cancel):
    monkeypatch.setattr("apps.worker.image_validation.prepare_child", stalled_prepare)
    monkeypatch.setattr("apps.worker.image_validation.PREPARE_SECONDS", 0.4 if not cancel else 5)
    tenant, input = image_input(encoded())
    cancelled = Event()
    timer = Timer(0.2, cancelled.set) if cancel else None
    before = {p.pid for p in multiprocessing.active_children()}
    if timer:
        timer.start()
    try:
        with pytest.raises(LeaseLost if cancel else ServiceError) as caught:
            BoundedImagePrepare(settings(), tenant)(input, cancelled)
        if not cancel:
            assert caught.value.code == "STAGE_TIMEOUT"
    finally:
        if timer:
            timer.cancel()
            timer.join()
    assert {p.pid for p in multiprocessing.active_children()} == before


def test_worker_secrets_have_no_api_fallback(monkeypatch):
    monkeypatch.delenv("S3_WORKER_ACCESS_KEY_FILE", raising=False)
    monkeypatch.delenv("S3_WORKER_SECRET_KEY_FILE", raising=False)
    with pytest.raises(ValueError):
        S3Settings.from_environment("https://labsafe.test", worker=True)


@pytest.mark.parametrize("enabled", [False, True, False])
def test_consumer_opt_in_and_sanitized_errors(monkeypatch, enabled, caplog):
    from apps.worker.app.main import create_celery_app

    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("WORKER_IMAGE_VALIDATION_ENABLED", "1" if enabled else "0")
    monkeypatch.setenv("PUBLIC_ORIGIN", "https://labsafe.test")
    with patch.object(S3Settings, "from_environment", return_value=settings()):
        app = create_celery_app()
    try:
        assert ("labsafe.tasks.dispatch" in app.tasks) == enabled
        if enabled:
            with patch(
                "apps.worker.image_validation.consume_image", side_effect=RuntimeError("secret")
            ):
                app.tasks["labsafe.tasks.dispatch"].run({"untrusted": "private"})
            assert "secret" not in caplog.text and "private" not in caplog.text
            assert "image_dispatch_failed" in caplog.text
    finally:
        app.close()

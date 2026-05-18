"""이미지 업로드 API 단위 테스트.

GCS 클라이언트는 unittest.mock으로 대체. 실제 GCS 호출 없이 로직만 검증.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from src.api.upload import upload_image

_USER_ID = 1
_FAKE_URL = "https://storage.googleapis.com/bucket/uploads/test.jpg?X-Goog-Signature=abc"


def _make_file(content_type: str, data: bytes) -> MagicMock:
    """청크 읽기 흐름을 시뮬레이션하는 UploadFile mock."""
    fake = MagicMock()
    fake.content_type = content_type
    # read()를 두 번 호출: 첫 번째 → data, 두 번째 → b"" (EOF)
    fake.read = AsyncMock(side_effect=[data, b""])
    return fake


@pytest.mark.asyncio
async def test_upload_image_success() -> None:
    """정상 업로드 — GCS signed URL 반환."""
    from src.config import Settings

    with (
        patch("src.api.upload.get_settings", return_value=Settings(gcs_bucket_name="test-bucket")),
        patch("src.api.upload.asyncio.to_thread", new=AsyncMock(return_value=_FAKE_URL)),
    ):
        result = await upload_image(file=_make_file("image/jpeg", b"fake-image-data"), user_id=_USER_ID)

    assert result.image_url == _FAKE_URL
    assert result.expires_in_seconds == 3600


@pytest.mark.asyncio
async def test_upload_image_invalid_content_type() -> None:
    """지원하지 않는 확장자 — 422."""
    fake_file = _make_file("image/gif", b"fake-gif-data")

    with pytest.raises(HTTPException) as exc:
        await upload_image(file=fake_file, user_id=_USER_ID)

    assert exc.value.status_code == 422  # HTTP_422_UNPROCESSABLE_ENTITY


@pytest.mark.asyncio
async def test_upload_image_too_large() -> None:
    """10MB 초과 파일 — 413 (청크 읽기 중 감지)."""
    # 첫 청크 자체가 10MB+1B → 즉시 413
    oversized = b"x" * (10 * 1024 * 1024 + 1)
    fake_file = _make_file("image/jpeg", oversized)

    with pytest.raises(HTTPException) as exc:
        await upload_image(file=fake_file, user_id=_USER_ID)

    assert exc.value.status_code == 413  # HTTP_413_REQUEST_ENTITY_TOO_LARGE


@pytest.mark.asyncio
async def test_upload_image_gcs_failure() -> None:
    """GCS 업로드 실패 — 503."""
    with patch(
        "src.api.upload.asyncio.to_thread",
        new=AsyncMock(side_effect=HTTPException(status_code=503, detail="GCS 실패")),
    ):
        with pytest.raises(HTTPException) as exc:
            await upload_image(file=_make_file("image/png", b"fake-png-data"), user_id=_USER_ID)

    assert exc.value.status_code == 503


@pytest.mark.asyncio
async def test_upload_image_webp_allowed() -> None:
    """webp 형식 허용 확인."""
    from src.config import Settings

    webp_url = "https://storage.googleapis.com/bucket/uploads/test.webp?sig=abc"
    with (
        patch("src.api.upload.get_settings", return_value=Settings(gcs_bucket_name="test-bucket")),
        patch("src.api.upload.asyncio.to_thread", new=AsyncMock(return_value=webp_url)),
    ):
        result = await upload_image(file=_make_file("image/webp", b"fake-webp-data"), user_id=_USER_ID)

    assert result.image_url == webp_url


@pytest.mark.asyncio
async def test_upload_image_no_bucket_configured() -> None:
    """GCS_BUCKET_NAME 미설정 — 503."""
    from src.config import Settings

    with patch("src.api.upload.get_settings", return_value=Settings(gcs_bucket_name="")):
        with pytest.raises(HTTPException) as exc:
            await upload_image(file=_make_file("image/jpeg", b"data"), user_id=_USER_ID)

    assert exc.value.status_code == 503

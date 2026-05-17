"""이미지 업로드 API 단위 테스트.

GCS 클라이언트는 unittest.mock으로 대체. 실제 GCS 호출 없이 로직만 검증.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from src.main import app

_USER_ID = 1


def _auth_header() -> dict[str, str]:
    return {"Authorization": "Bearer test-token"}


def _make_client() -> TestClient:
    return TestClient(app, raise_server_exceptions=True)


# ---------------------------------------------------------------------------
# POST /api/v1/upload/image
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_upload_image_success() -> None:
    """정상 업로드 — GCS signed URL 반환."""
    from src.api.upload import upload_image

    fake_url = "https://storage.googleapis.com/bucket/uploads/test.jpg?X-Goog-Signature=abc"

    with patch("src.api.upload._upload_to_gcs", new=AsyncMock(return_value=fake_url)):
        fake_file = MagicMock()
        fake_file.content_type = "image/jpeg"
        fake_file.read = AsyncMock(return_value=b"fake-image-data")

        result = await upload_image(file=fake_file, user_id=_USER_ID)

    assert result.image_url == fake_url
    assert result.expires_in_seconds == 3600


@pytest.mark.asyncio
async def test_upload_image_invalid_content_type() -> None:
    """지원하지 않는 확장자 — 422."""
    from src.api.upload import upload_image

    fake_file = MagicMock()
    fake_file.content_type = "image/gif"
    fake_file.read = AsyncMock(return_value=b"fake-gif-data")

    with pytest.raises(HTTPException) as exc:
        await upload_image(file=fake_file, user_id=_USER_ID)

    assert exc.value.status_code == 422


@pytest.mark.asyncio
async def test_upload_image_too_large() -> None:
    """10MB 초과 파일 — 413."""
    from src.api.upload import upload_image

    fake_file = MagicMock()
    fake_file.content_type = "image/jpeg"
    fake_file.read = AsyncMock(return_value=b"x" * (10 * 1024 * 1024 + 1))

    with pytest.raises(HTTPException) as exc:
        await upload_image(file=fake_file, user_id=_USER_ID)

    assert exc.value.status_code == 413


@pytest.mark.asyncio
async def test_upload_image_gcs_failure() -> None:
    """GCS 업로드 실패 — 503."""
    from src.api.upload import upload_image

    with patch(
        "src.api.upload._upload_to_gcs",
        new=AsyncMock(side_effect=HTTPException(status_code=503, detail="GCS 실패")),
    ):
        fake_file = MagicMock()
        fake_file.content_type = "image/png"
        fake_file.read = AsyncMock(return_value=b"fake-png-data")

        with pytest.raises(HTTPException) as exc:
            await upload_image(file=fake_file, user_id=_USER_ID)

    assert exc.value.status_code == 503


@pytest.mark.asyncio
async def test_upload_image_webp_allowed() -> None:
    """webp 형식 허용 확인."""
    from src.api.upload import upload_image

    fake_url = "https://storage.googleapis.com/bucket/uploads/test.webp?sig=abc"

    with patch("src.api.upload._upload_to_gcs", new=AsyncMock(return_value=fake_url)):
        fake_file = MagicMock()
        fake_file.content_type = "image/webp"
        fake_file.read = AsyncMock(return_value=b"fake-webp-data")

        result = await upload_image(file=fake_file, user_id=_USER_ID)

    assert result.image_url == fake_url

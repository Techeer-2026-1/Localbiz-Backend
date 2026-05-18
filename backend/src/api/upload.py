"""이미지 업로드 API — GCS 임시 저장 후 URL 반환 (feat/#103, Phase 1).

흐름:
  FE → POST /api/v1/upload/image (multipart)
  → 파일 크기/확장자 검증 (청크 읽기)
  → GCS uploads/ 폴더에 저장 (asyncio.to_thread)
  → 1시간짜리 signed URL 반환 (Workload Identity 호환)
  → FE가 URL을 image_search_node에 넘김 (기존 흐름 그대로)

GCS 버킷 설정 필요:
  - 버킷명: GCS_BUCKET_NAME 환경변수
  - GCE 서비스 계정에 roles/storage.objectAdmin + roles/iam.serviceAccountTokenCreator 권한
  - 버킷 lifecycle 규칙: uploads/ 폴더 24시간 후 자동 삭제 권장
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, UploadFile, status
from pydantic import BaseModel

from src.api.deps import get_current_user_id  # pyright: ignore[reportMissingImports]
from src.config import get_settings  # pyright: ignore[reportMissingImports]

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/upload", tags=["upload"])

_ALLOWED_CONTENT_TYPES = {"image/jpeg", "image/png", "image/webp"}
_CONTENT_TYPE_TO_EXT = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}
_MAX_BYTES = 10 * 1024 * 1024  # 10MB
_CHUNK_SIZE = 64 * 1024  # 64KB
_SIGNED_URL_HOURS = 1


class ImageUploadResponse(BaseModel):
    image_url: str
    expires_in_seconds: int


@router.post("/image", response_model=ImageUploadResponse, status_code=status.HTTP_200_OK)
async def upload_image(
    file: UploadFile,
    user_id: int = Depends(get_current_user_id),
) -> ImageUploadResponse:
    """사진 파일을 GCS에 업로드하고 1시간짜리 URL을 반환한다."""
    content_type = file.content_type or ""
    if content_type not in _ALLOWED_CONTENT_TYPES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="지원하지 않는 파일 형식입니다. jpg, png, webp만 가능합니다.",
        )

    # 청크 단위 읽기 — 전체 로드 전에 크기 초과 감지
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await file.read(_CHUNK_SIZE)
        if not chunk:
            break
        total += len(chunk)
        if total > _MAX_BYTES:
            raise HTTPException(
                status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                detail="파일 크기가 10MB를 초과합니다.",
            )
        chunks.append(chunk)
    contents = b"".join(chunks)

    settings = get_settings()
    if not settings.gcs_bucket_name:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="이미지 업로드 서비스가 설정되지 않았습니다.",
        )

    signed_url = await asyncio.to_thread(
        _sync_upload_to_gcs,
        contents,
        content_type,
        settings.gcs_bucket_name,
        user_id,
    )

    return ImageUploadResponse(
        image_url=signed_url,
        expires_in_seconds=_SIGNED_URL_HOURS * 3600,
    )


def _sync_upload_to_gcs(
    data: bytes,
    content_type: str,
    bucket_name: str,
    user_id: Optional[int] = None,
) -> str:
    """GCS에 업로드 후 signed URL 반환. asyncio.to_thread로 호출해야 함."""
    try:
        import google.auth  # pyright: ignore[reportMissingImports]
        import google.auth.transport.requests  # pyright: ignore[reportMissingImports]
        from google.cloud import storage  # pyright: ignore[reportMissingImports,reportAttributeAccessIssue]
    except ImportError as e:
        logger.error("google-cloud-storage 미설치: %s", e)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="이미지 업로드 서비스를 사용할 수 없습니다.",
        ) from e

    ext = _CONTENT_TYPE_TO_EXT[content_type]
    blob_name = f"uploads/{uuid.uuid4()}.{ext}"

    try:
        client = storage.Client()
        bucket = client.bucket(bucket_name)
        blob = bucket.blob(blob_name)
        blob.upload_from_string(data, content_type=content_type)

        # GCE Workload Identity: private key 없이 IAM signBlob API로 서명
        credentials, _ = google.auth.default()
        auth_req = google.auth.transport.requests.Request()
        credentials.refresh(auth_req)

        signed_url: str = blob.generate_signed_url(
            expiration=timedelta(hours=_SIGNED_URL_HOURS),
            method="GET",
            version="v4",
            service_account_email=credentials.service_account_email,  # pyright: ignore[reportAttributeAccessIssue]
            access_token=credentials.token,  # pyright: ignore[reportAttributeAccessIssue]
        )
        logger.info("upload: user_id=%s blob=%s", user_id, blob_name)
        return signed_url

    except Exception as e:
        logger.exception("upload: GCS 업로드 실패 blob=%s", blob_name)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="이미지 업로드에 실패했습니다. 잠시 후 다시 시도해주세요.",
        ) from e

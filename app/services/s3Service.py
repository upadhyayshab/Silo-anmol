import asyncio
import io
import logging
import os
import uuid
from typing import Optional, Union

import boto3
import botocore.exceptions as boto_exc
from fastapi import HTTPException, UploadFile

from config import settings

logger = logging.getLogger(__name__)


class S3Service:
    def __init__(
        self,
        aws_access_key_id: Optional[str] = None,
        aws_secret_access_key: Optional[str] = None,
        region_name: str = settings.AWS_REGION,
        bucket_name: str = settings.S3_BUCKET_PUBLIC,
    ):
        access_key = aws_access_key_id or settings.AWS_ACCESS_KEY_ID
        secret_key = aws_secret_access_key or settings.AWS_SECRET_ACCESS_KEY
        self.region_name = region_name or "ap-south-1"

        # 1. If explicit keys are provided (local dev), use them.
        # 2. Otherwise use default boto3 chain (IAM role in AWS/Docker/ECS).
        self.s3_client = None
        if access_key and secret_key:
            try:
                self.s3_client = boto3.client(
                    "s3",
                    region_name=self.region_name,
                    aws_access_key_id=access_key,
                    aws_secret_access_key=secret_key,
                )
            except Exception as e:
                logger.warning(f"Failed to initialize S3 client with explicit keys: {e}")

        if self.s3_client is None:
            try:
                self.s3_client = boto3.client("s3", region_name=self.region_name)
            except Exception as e:
                logger.error(f"Failed to initialize default S3 client: {e}")

        # Handle bucket names with path prefixes like 'gausampurna-public/cow-images/'
        raw_bucket = (bucket_name or "").strip().rstrip("/")
        if "/" in raw_bucket:
            parts = raw_bucket.split("/", 1)
            self.bucket_name = parts[0]
            self.default_prefix = parts[1].strip("/")
        else:
            self.bucket_name = raw_bucket
            self.default_prefix = "images"

    async def upload_file(
        self,
        file: Union[UploadFile, bytes],
        filename: Optional[str] = None,
        content_type: Optional[str] = None,
        prefix: Optional[str] = None,
    ) -> str:
        """Upload an UploadFile or raw bytes non-blockingly and return the public S3 URL."""
        if isinstance(file, UploadFile):
            name = filename or file.filename or "file.jpg"
            file_content = await file.read()
            c_type = content_type or file.content_type or "image/jpeg"
        else:
            name = filename or "file.jpg"
            file_content = file
            c_type = content_type or "image/jpeg"

        if "." in name:
            extension = name.rsplit(".", 1)[-1]
        else:
            extension = "jpg"

        unique_filename = f"{uuid.uuid4().hex[:12]}.{extension}"
        folder_prefix = prefix if prefix is not None else self.default_prefix
        object_key = f"{folder_prefix.strip('/')}/{unique_filename}" if folder_prefix else unique_filename

        if not self.s3_client:
            raise HTTPException(status_code=500, detail="S3 client is not initialized or credentials missing")

        try:
            await asyncio.to_thread(
                self.s3_client.put_object,
                Bucket=self.bucket_name,
                Key=object_key,
                Body=file_content,
                ContentType=c_type,
            )
        except boto_exc.NoCredentialsError:
            # Fallback for local development if default boto3 chain had no credentials
            access_key = settings.AWS_ACCESS_KEY_ID
            secret_key = settings.AWS_SECRET_ACCESS_KEY
            if access_key and secret_key:
                try:
                    fallback_client = boto3.client(
                        "s3",
                        region_name=self.region_name,
                        aws_access_key_id=access_key,
                        aws_secret_access_key=secret_key,
                    )
                    await asyncio.to_thread(
                        fallback_client.put_object,
                        Bucket=self.bucket_name,
                        Key=object_key,
                        Body=file_content,
                        ContentType=c_type,
                    )
                    self.s3_client = fallback_client
                except Exception as fallback_err:
                    raise HTTPException(
                        status_code=500,
                        detail=f"S3 upload failed with fallback credentials: {str(fallback_err)}",
                    )
            else:
                raise HTTPException(status_code=500, detail="AWS credentials not found for S3 upload")
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to upload to S3: {str(e)}")

        return f"https://{self.bucket_name}.s3.{self.region_name}.amazonaws.com/{object_key}"


# Instantiate services for Public and Private buckets
s3_service_public = S3Service(bucket_name=settings.S3_BUCKET_PUBLIC)
s3_service_private = S3Service(bucket_name=settings.S3_BUCKET_PRIVATE)

__all__ = ["S3Service", "s3_service_public", "s3_service_private"]

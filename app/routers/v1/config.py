from fastapi import APIRouter, HTTPException, Depends, status, UploadFile, File
from typing import Optional
from datetime import datetime, timezone
import os
import uuid
from pathlib import Path

from config import get_settings, get_engine
from managers import SystemConfigurationManager, SystemConfigurationSchema, AppSettingManager
from models import SystemConfigurationUpdateRequest, SystemConfigurationResponse, StatusResponse
from utils.auth import require_permission, AuthContext
from utils.permissions import Permission
from utils.constants import (
    SETTING_AUTO_REVERT_ENABLED, SETTING_AUTO_REVERT_FLUSH, SETTING_AUTO_REVERT_BASELINE_AT,
)

settings = get_settings()
engine = get_engine(settings.name)
config_manager = SystemConfigurationManager(engine)
app_setting_manager = AppSettingManager(engine)

router = APIRouter(prefix="/config", tags=["System Configuration"])

# Create uploads directory if it doesn't exist
UPLOAD_DIR = Path("app/assets/uploads")
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

_AUTO_REVERT_KEYS = [SETTING_AUTO_REVERT_ENABLED, SETTING_AUTO_REVERT_FLUSH,
                     SETTING_AUTO_REVERT_BASELINE_AT]


async def _config_response(config) -> SystemConfigurationResponse:
    """Company/GST fields from the system_configuration row, merged with the
    auto-revert toggles which live as app_settings rows (not columns)."""
    resp = SystemConfigurationResponse.model_validate(config)
    kv = await app_setting_manager.get_map(_AUTO_REVERT_KEYS)
    resp.auto_revert_enabled = bool(kv.get(SETTING_AUTO_REVERT_ENABLED, False))
    resp.auto_revert_flush_existing = bool(kv.get(SETTING_AUTO_REVERT_FLUSH, False))
    baseline = kv.get(SETTING_AUTO_REVERT_BASELINE_AT)
    resp.auto_revert_baseline_at = datetime.fromisoformat(baseline) if baseline else None
    return resp


@router.get("", response_model=SystemConfigurationResponse)
async def get_system_configuration(
    _: AuthContext = Depends(require_permission(Permission.CONFIG_READ))
):
    """
    Get current system configuration
    Used for company details, GST settings, invoice templates
    """
    try:
        # Try to get existing configuration
        configs = await config_manager.fetch_all(limit=1)
        
        if not configs.items:
            # Create default configuration if none exists
            default_config = SystemConfigurationSchema(
                company_name="Your Company Name",
                company_address="Your Company Address",
                company_gstin="00AAAAA0000A0Z0",
                company_pan="AAAAA0000A",
                company_logo_url=None,
                invoice_terms="Terms and conditions apply",
                invoice_footer="Thank you for your business",
                cgst_default_rate=9.00,
                sgst_default_rate=9.00,
                igst_default_rate=18.00,
                price_includes_tax=False,
            )

            created_config = await config_manager.create(default_config)
            config = created_config
        else:
            config = configs.items[0]

        return await _config_response(config)

    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to fetch configuration: {str(e)}"
        )


@router.put("", response_model=SystemConfigurationResponse)
async def update_system_configuration(
    payload: SystemConfigurationUpdateRequest,
    _: AuthContext = Depends(require_permission(Permission.CONFIG_WRITE))
):
    """
    Update system configuration
    Only super_admin and admin can modify system settings
    """
    try:
        # Get existing configuration
        configs = await config_manager.fetch_all(limit=1)
        
        if not configs.items:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="System configuration not found. Please initialize first."
            )
        
        config = configs.items[0]

        updates = payload.dict(exclude_unset=True)
        if not updates:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="No fields to update"
            )

        # Split the auto-revert toggles (stored as app_settings rows) out of the
        # company/GST column updates.
        new_enabled = updates.pop("auto_revert_enabled", None)
        new_flush = updates.pop("auto_revert_flush_existing", None)

        if updates:
            config = await config_manager.update(config.uid, updates)

        kv = await app_setting_manager.get_map(_AUTO_REVERT_KEYS)
        if new_enabled is not None:
            await app_setting_manager.set(SETTING_AUTO_REVERT_ENABLED, bool(new_enabled))
        if new_flush is not None:
            await app_setting_manager.set(SETTING_AUTO_REVERT_FLUSH, bool(new_flush))

        # Stamp the grace anchor the first time auto-revert ends up enabled without
        # flush, so the pre-existing backlog gets 24h before it reverts.
        result_enabled = bool(new_enabled) if new_enabled is not None else bool(kv.get(SETTING_AUTO_REVERT_ENABLED, False))
        result_flush = bool(new_flush) if new_flush is not None else bool(kv.get(SETTING_AUTO_REVERT_FLUSH, False))
        if result_enabled and not result_flush and not kv.get(SETTING_AUTO_REVERT_BASELINE_AT):
            await app_setting_manager.set(SETTING_AUTO_REVERT_BASELINE_AT,
                                          datetime.now(timezone.utc).isoformat())

        return await _config_response(config)

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to update configuration: {str(e)}"
        )


@router.post("/logo", response_model=StatusResponse)
async def upload_company_logo(
    file: UploadFile = File(...),
    _: AuthContext = Depends(require_permission(Permission.CONFIG_WRITE))
):
    """
    Upload company logo for invoices
    Accepts image files (PNG, JPG, JPEG)
    """
    try:
        # Validate file type
        allowed_types = ["image/png", "image/jpeg", "image/jpg"]
        if file.content_type not in allowed_types:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Only PNG and JPEG images are allowed"
            )
        
        # Validate file size (max 5MB)
        max_size = 5 * 1024 * 1024  # 5MB
        file_content = await file.read()
        if len(file_content) > max_size:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="File size must be less than 5MB"
            )
        
        # Generate unique filename
        file_extension = file.filename.split('.')[-1].lower()
        unique_filename = f"logo_{uuid.uuid4().hex}.{file_extension}"
        file_path = UPLOAD_DIR / unique_filename
        
        # Save file
        with open(file_path, "wb") as buffer:
            buffer.write(file_content)
        
        # Update configuration with logo URL
        logo_url = f"/assets/uploads/{unique_filename}"
        
        configs = await config_manager.fetch_all(limit=1)
        if configs.items:
            await config_manager.update(configs.items[0].uid, {"company_logo_url": logo_url})
        
        return StatusResponse(
            status="ok",
            message=f"Logo uploaded successfully: {logo_url}"
        )
    
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to upload logo: {str(e)}"
        )


@router.delete("/logo", response_model=StatusResponse)
async def remove_company_logo(
    _: AuthContext = Depends(require_permission(Permission.CONFIG_WRITE))
):
    """
    Remove company logo
    """
    try:
        configs = await config_manager.fetch_all(limit=1)
        
        if not configs.items:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="System configuration not found"
            )
        
        config = configs.items[0]
        
        # Remove logo file if exists
        if config.company_logo_url:
            logo_filename = config.company_logo_url.split('/')[-1]
            logo_path = UPLOAD_DIR / logo_filename
            
            if logo_path.exists():
                logo_path.unlink()
        
        # Update configuration
        await config_manager.update(config.uid, {"company_logo_url": None})
        
        return StatusResponse(
            status="ok",
            message="Logo removed successfully"
        )
    
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to remove logo: {str(e)}"
        )
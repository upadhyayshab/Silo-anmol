from fastapi import APIRouter, HTTPException, Depends, status, UploadFile, File
from typing import Optional
import os
import uuid
from pathlib import Path

from config import get_settings, get_engine
from managers import SystemConfigurationManager, SystemConfigurationSchema
from models import SystemConfigurationUpdateRequest, SystemConfigurationResponse, StatusResponse
from utils.auth import require_permission, AuthContext
from utils.permissions import Permission

settings = get_settings()
engine = get_engine(settings.name)
config_manager = SystemConfigurationManager(engine)

router = APIRouter(prefix="/config", tags=["System Configuration"])

# Create uploads directory if it doesn't exist
UPLOAD_DIR = Path("app/assets/uploads")
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)


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
                price_includes_tax=False
            )
            
            created_config = await config_manager.create(default_config)
            config = created_config
        else:
            config = configs.items[0]
        
        return SystemConfigurationResponse(
            uid=config.uid,
            company_name=config.company_name,
            company_address=config.company_address,
            company_gstin=config.company_gstin,
            company_pan=config.company_pan,
            company_logo_url=config.company_logo_url,
            invoice_terms=config.invoice_terms,
            invoice_footer=config.invoice_footer,
            cgst_default_rate=config.cgst_default_rate,
            sgst_default_rate=config.sgst_default_rate,
            igst_default_rate=config.igst_default_rate,
            price_includes_tax=config.price_includes_tax,
            created_at=config.created_at,
            updated_at=config.updated_at
        )
    
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
        
        # Update only provided fields
        updates = payload.dict(exclude_unset=True)
        
        if not updates:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="No fields to update"
            )
        
        updated_config = await config_manager.update(config.uid, updates)
        
        return SystemConfigurationResponse(
            uid=updated_config.uid,
            company_name=updated_config.company_name,
            company_address=updated_config.company_address,
            company_gstin=updated_config.company_gstin,
            company_pan=updated_config.company_pan,
            company_logo_url=updated_config.company_logo_url,
            invoice_terms=updated_config.invoice_terms,
            invoice_footer=updated_config.invoice_footer,
            cgst_default_rate=updated_config.cgst_default_rate,
            sgst_default_rate=updated_config.sgst_default_rate,
            igst_default_rate=updated_config.igst_default_rate,
            price_includes_tax=updated_config.price_includes_tax,
            created_at=updated_config.created_at,
            updated_at=updated_config.updated_at
        )
    
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
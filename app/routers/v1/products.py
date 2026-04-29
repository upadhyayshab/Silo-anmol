from fastapi import APIRouter, HTTPException, Depends, status
from typing import Optional, List

from config import get_settings, get_engine
from managers import ProductManager, ProductCategoryManager
from models import (
    ProductCreateRequest, ProductUpdateRequest, ProductResponse,
    ProductCategoryCreateRequest, ProductCategoryResponse,
    ListResponse, StatusResponse
)
from utils.auth import require_roles
from utils.constants import UserRole

settings = get_settings()
engine = get_engine(settings.name)
product_manager = ProductManager(engine)
category_manager = ProductCategoryManager(engine)

router = APIRouter(tags=["Product Management"])


# ============================================================================
# PRODUCT CATEGORIES
# ============================================================================

@router.get("/categories", response_model=ListResponse[ProductCategoryResponse])
async def list_categories(
    is_active: bool = None,
    limit: int = 100,
    offset: int = 0,
    _: str = Depends(require_roles(
        UserRole.SUPER_ADMIN, UserRole.ADMIN, UserRole.WAREHOUSE_MANAGER,
        UserRole.OUTLET_MANAGER, UserRole.TELECALLER
    ))
):
    """
    List all product categories
    """
    try:
        filters = {}
        if is_active is not None:
            filters["is_active"] = is_active
        
        categories = await category_manager.fetch_all(
            limit=limit,
            offset=offset,
            filters=filters if filters else None
        )
        
        category_responses = [
            ProductCategoryResponse(
                uid=cat.uid,
                category_name=cat.category_name,
                description=cat.description,
                is_active=cat.is_active,
                created_at=cat.created_at
            )
            for cat in categories.items
        ]
        
        return ListResponse(items=category_responses, count=len(category_responses))
    
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to fetch categories: {str(e)}"
        )


@router.post("/categories", response_model=ProductCategoryResponse, status_code=status.HTTP_201_CREATED)
async def create_category(
    payload: ProductCategoryCreateRequest,
    _: str = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.ADMIN, UserRole.WAREHOUSE_MANAGER))
):
    """
    Create new product category
    Requires: super_admin, admin, or warehouse_manager role
    """
    try:
        # Check if category name already exists
        existing = await category_manager.fetch_all(filters={"category_name": payload.category_name})
        if existing.items:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Category name already exists"
            )
        
        from managers import ProductCategorySchema
        category = ProductCategorySchema(
            category_name=payload.category_name,
            description=payload.description,
            is_active=True
        )
        
        created_category = await category_manager.create(category)
        
        return ProductCategoryResponse(
            uid=created_category.uid,
            category_name=created_category.category_name,
            description=created_category.description,
            is_active=created_category.is_active,
            created_at=created_category.created_at
        )
    
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to create category: {str(e)}"
        )


# ============================================================================
# PRODUCTS
# ============================================================================

@router.get("/products", response_model=ListResponse[ProductResponse])
async def list_products(
    category_id: str = None,
    is_active: bool = None,
    search: str = None,
    limit: int = 50,
    offset: int = 0,
    _: str = Depends(require_roles(
        UserRole.SUPER_ADMIN, UserRole.ADMIN, UserRole.WAREHOUSE_MANAGER,
        UserRole.OUTLET_MANAGER, UserRole.TELECALLER
    ))
):
    """
    List all products with optional filters
    """
    try:
        filters = {}
        if category_id:
            filters["category_id"] = category_id
        if is_active is not None:
            filters["is_active"] = is_active
        
        # TODO: Implement search functionality
        
        products = await product_manager.fetch_all(
            limit=limit,
            offset=offset,
            filters=filters if filters else None
        )
        
        product_responses = [
            ProductResponse(
                uid=prod.uid,
                sku=prod.sku,
                product_name=prod.product_name,
                description=prod.description,
                category_id=prod.category_id,
                hsn_code=prod.hsn_code,
                tax_rate=prod.tax_rate,
                unit_price=prod.unit_price,
                cost_price=prod.cost_price,
                unit_of_measure=prod.unit_of_measure,
                barcode=prod.barcode,
                image_url=prod.image_url,
                min_stock_level=prod.min_stock_level,
                lsq_display_name=prod.lsq_display_name,
                commission=prod.commission,
                discount=prod.discount,
                margin=prod.margin,
                is_active=prod.is_active,
                created_at=prod.created_at
            )
            for prod in products.items
        ]
        
        return ListResponse(items=product_responses, count=len(product_responses))
    
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to fetch products: {str(e)}"
        )


@router.get("/products/barcode/{barcode}", response_model=ProductResponse)
async def get_product_by_barcode(
    barcode: str,
    _: str = Depends(require_roles(
        UserRole.SUPER_ADMIN, UserRole.ADMIN, UserRole.WAREHOUSE_MANAGER,
        UserRole.OUTLET_MANAGER, UserRole.TELECALLER
    ))
):
    """
    Get product by barcode
    """
    try:
        products = await product_manager.fetch_all(filters={"barcode": barcode})
        
        if not products.items:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Product not found"
            )
        
        prod = products.items[0]
        
        return ProductResponse(
            uid=prod.uid,
            sku=prod.sku,
            product_name=prod.product_name,
            description=prod.description,
            category_id=prod.category_id,
            hsn_code=prod.hsn_code,
            tax_rate=prod.tax_rate,
            unit_price=prod.unit_price,
            cost_price=prod.cost_price,
            unit_of_measure=prod.unit_of_measure,
            barcode=prod.barcode,
            image_url=prod.image_url,
            min_stock_level=prod.min_stock_level,
            lsq_display_name=prod.lsq_display_name,
            commission=prod.commission,
            discount=prod.discount,
            margin=prod.margin,
            is_active=prod.is_active,
            created_at=prod.created_at
        )
    
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to fetch product: {str(e)}"
        )


@router.get("/products/{product_id}", response_model=ProductResponse)
async def get_product(
    product_id: str,
    _: str = Depends(require_roles(
        UserRole.SUPER_ADMIN, UserRole.ADMIN, UserRole.WAREHOUSE_MANAGER,
        UserRole.OUTLET_MANAGER, UserRole.TELECALLER
    ))
):
    """
    Get product by ID
    """
    try:
        prod = await product_manager.fetch(product_id)
        
        return ProductResponse(
            uid=prod.uid,
            sku=prod.sku,
            product_name=prod.product_name,
            description=prod.description,
            category_id=prod.category_id,
            hsn_code=prod.hsn_code,
            tax_rate=prod.tax_rate,
            unit_price=prod.unit_price,
            cost_price=prod.cost_price,
            unit_of_measure=prod.unit_of_measure,
            barcode=prod.barcode,
            image_url=prod.image_url,
            min_stock_level=prod.min_stock_level,
            lsq_display_name=prod.lsq_display_name,
            commission=prod.commission,
            discount=prod.discount,
            margin=prod.margin,
            is_active=prod.is_active,
            created_at=prod.created_at
        )
    
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Product not found: {str(e)}"
        )


@router.post("/products", response_model=ProductResponse, status_code=status.HTTP_201_CREATED)
async def create_product(
    payload: ProductCreateRequest,
    _: str = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.ADMIN, UserRole.WAREHOUSE_MANAGER))
):
    """
    Create new product
    Requires: super_admin, admin, or warehouse_manager role
    """
    try:
        # Check if SKU already exists
        existing = await product_manager.fetch_all(filters={"sku": payload.sku})
        if existing.items:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="SKU already exists"
            )
        
        # Check if barcode already exists (if provided)
        if payload.barcode:
            existing_barcode = await product_manager.fetch_all(filters={"barcode": payload.barcode})
            if existing_barcode.items:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Barcode already exists"
                )
        
        from managers import ProductSchema
        product = ProductSchema(
            sku=payload.sku,
            product_name=payload.product_name,
            description=payload.description,
            category_id=payload.category_id,
            hsn_code=payload.hsn_code,
            tax_rate=payload.tax_rate,
            unit_price=payload.unit_price,
            cost_price=payload.cost_price,
            unit_of_measure=payload.unit_of_measure,
            barcode=payload.barcode,
            image_url=payload.image_url,
            min_stock_level=payload.min_stock_level,
            lsq_display_name=payload.lsq_display_name,
            commission=payload.commission,
            discount=payload.discount,
            margin=payload.margin,
            is_active=True
        )
        
        created_product = await product_manager.create(product)
        
        return ProductResponse(
            uid=created_product.uid,
            sku=created_product.sku,
            product_name=created_product.product_name,
            description=created_product.description,
            category_id=created_product.category_id,
            hsn_code=created_product.hsn_code,
            tax_rate=created_product.tax_rate,
            unit_price=created_product.unit_price,
            cost_price=created_product.cost_price,
            unit_of_measure=created_product.unit_of_measure,
            barcode=created_product.barcode,
            image_url=created_product.image_url,
            min_stock_level=created_product.min_stock_level,
            lsq_display_name=created_product.lsq_display_name,
            commission=created_product.commission,
            discount=created_product.discount,
            margin=created_product.margin,
            is_active=created_product.is_active,
            created_at=created_product.created_at
        )
    
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to create product: {str(e)}"
        )


@router.patch("/products/{product_id}", response_model=ProductResponse)
async def update_product(
    product_id: str,
    payload: ProductUpdateRequest,
    _: str = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.ADMIN, UserRole.WAREHOUSE_MANAGER))
):
    """
    Update product details
    Requires: super_admin, admin, or warehouse_manager role
    """
    try:
        updates = payload.dict(exclude_unset=True)
        
        if not updates:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="No fields to update"
            )
        
        updated_product = await product_manager.update(product_id, updates)
        
        return ProductResponse(
            uid=updated_product.uid,
            sku=updated_product.sku,
            product_name=updated_product.product_name,
            description=updated_product.description,
            category_id=updated_product.category_id,
            hsn_code=updated_product.hsn_code,
            tax_rate=updated_product.tax_rate,
            unit_price=updated_product.unit_price,
            cost_price=updated_product.cost_price,
            unit_of_measure=updated_product.unit_of_measure,
            barcode=updated_product.barcode,
            image_url=updated_product.image_url,
            min_stock_level=updated_product.min_stock_level,
            lsq_display_name=updated_product.lsq_display_name,
            commission=updated_product.commission,
            discount=updated_product.discount,
            margin=updated_product.margin,
            is_active=updated_product.is_active,
            created_at=updated_product.created_at
        )
    
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to update product: {str(e)}"
        )


@router.delete("/products/{product_id}", response_model=StatusResponse)
async def deactivate_product(
    product_id: str,
    _: str = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.ADMIN, UserRole.WAREHOUSE_MANAGER))
):
    """
    Deactivate product (soft delete)
    Requires: super_admin, admin, or warehouse_manager role
    """
    try:
        await product_manager.update(product_id, {"is_active": False})
        return StatusResponse(status="ok", message="Product deactivated successfully")
    
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to deactivate product: {str(e)}"
        )


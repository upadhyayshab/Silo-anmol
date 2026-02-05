from fastapi import APIRouter, HTTPException, Depends, status, Response
from typing import List, Optional
from datetime import datetime, date
from decimal import Decimal
from io import BytesIO

from config import get_settings, get_engine
from managers import (
    SalesInvoiceManager, SalesInvoiceItemManager, InventoryManager,
    ProductManager, OutletManager, UserManager, SystemConfigurationManager,
    SalesInvoiceSchema, SalesInvoiceItemSchema
)
from models import (
    InvoiceCreateRequest, InvoiceResponse, InvoiceItemResponse,
    ListResponse, StatusResponse
)
from utils.auth import require_roles, get_current_user_id
from utils.constants import UserRole, InvoiceType, PaymentStatus, PaymentMethod
from services.invoice_service import InvoiceService
import uuid

settings = get_settings()
engine = get_engine(settings.name)

invoice_manager = SalesInvoiceManager(engine)
invoice_item_manager = SalesInvoiceItemManager(engine)
inventory_manager = InventoryManager(engine)
product_manager = ProductManager(engine)
outlet_manager = OutletManager(engine)
user_manager = UserManager(engine)
config_manager = SystemConfigurationManager(engine)

# Initialize invoice service
invoice_service = InvoiceService(engine)

router = APIRouter(prefix="/invoices", tags=["Invoice Management"])


def build_invoice_response(invoice, items=None) -> InvoiceResponse:
    """Helper function to build InvoiceResponse with all fields including new payment fields"""
    item_responses = []
    if items:
        for item in items:
            item_responses.append(InvoiceItemResponse(
                uid=item.uid,
                product_id=item.product_id,
                product_name=item.product_name,
                hsn_code=item.hsn_code,
                quantity=item.quantity,
                unit_price=item.unit_price,
                total_price=item.total_price,
                product_manual_discount=getattr(item, 'product_manual_discount', Decimal('0.00')),
                discount_percentage=getattr(item, 'discount_percentage', None),
                discount_amount=item.discount_amount,
                taxable_amount=item.taxable_amount,
                tax_rate=item.tax_rate,
                cgst_rate=item.cgst_rate,
                cgst_amount=item.cgst_amount,
                sgst_rate=item.sgst_rate,
                sgst_amount=item.sgst_amount,
                igst_rate=item.igst_rate,
                igst_amount=item.igst_amount,
                total_tax=item.total_tax,
                total_amount=item.total_amount
            ))
    
    return InvoiceResponse(
        uid=invoice.uid,
        invoice_number=invoice.invoice_number,
        outlet_id=invoice.outlet_id,
        customer_name=invoice.customer_name,
        customer_phone=invoice.customer_phone,
        customer_email=invoice.customer_email,
        customer_address=invoice.customer_address,
        customer_gstin=invoice.customer_gstin,
        customer_state_code=invoice.customer_state_code,
        invoice_date=invoice.invoice_date,
        invoice_type=invoice.invoice_type,
        payment_method=invoice.payment_method,
        payment_status=invoice.payment_status,
        subtotal=invoice.subtotal,
        discount_amount=invoice.discount_amount,
        taxable_amount=invoice.taxable_amount,
        cgst_amount=invoice.cgst_amount,
        sgst_amount=invoice.sgst_amount,
        igst_amount=invoice.igst_amount,
        total_tax=invoice.total_tax,
        total_amount=invoice.total_amount,
        amount_paid=invoice.amount_paid,
        balance_amount=invoice.balance_amount,
        prepaid_amount=getattr(invoice, 'prepaid_amount', Decimal('0.00')),
        paid_at_outlet=getattr(invoice, 'paid_at_outlet', Decimal('0.00')),
        notes=invoice.notes,
        is_cancelled=invoice.is_cancelled,
        cancelled_reason=invoice.cancelled_reason,
        created_by=invoice.created_by,
        items=item_responses,
        created_at=invoice.created_at
    )


# SPECIFIC ROUTES FIRST (to avoid conflicts with generic routes)

@router.get("/next-number/{outlet_id}")
async def get_next_invoice_number(
    outlet_id: str,
    current_user_id: str = Depends(require_roles(
        UserRole.OUTLET_MANAGER, UserRole.ACCOUNTANT, UserRole.ADMIN, UserRole.SUPER_ADMIN
    ))
):
    """Get next invoice number for outlet"""
    try:
        # Get current user to check permissions
        current_user = await user_manager.fetch(current_user_id)
        
        # Role-based access control
        if current_user.role == UserRole.OUTLET_MANAGER:
            if current_user.outlet_id != outlet_id:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="You can only access invoice numbers for your outlet"
                )
        
        # Verify outlet exists
        try:
            outlet = await outlet_manager.fetch(outlet_id)
        except:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Outlet not found"
            )
        
        # Get next invoice number
        next_number = await invoice_service.get_next_invoice_number(outlet_id)
        
        return {"next_invoice_number": next_number}
    
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get next invoice number: {str(e)}"
        )


@router.get("/customer/{phone}")
async def get_customer_invoices(
    phone: str,
    limit: int = 20,
    offset: int = 0,
    current_user_id: str = Depends(require_roles(
        UserRole.OUTLET_MANAGER, UserRole.ACCOUNTANT, UserRole.ADMIN, UserRole.SUPER_ADMIN
    ))
):
    """Get invoices for a specific customer by phone number"""
    try:
        current_user = await user_manager.fetch(current_user_id)
        
        filters = {"customer_phone": phone}
        
        # Role-based filtering
        if current_user.role == UserRole.OUTLET_MANAGER:
            if current_user.outlet_id:
                filters["outlet_id"] = current_user.outlet_id
        
        invoices = await invoice_manager.fetch_all(
            filters=filters,
            limit=limit,
            offset=offset
        )
        
        invoice_responses = []
        for invoice in invoices.items:
            invoice_responses.append(await build_invoice_response(invoice))
        
        return ListResponse(items=invoice_responses, count=len(invoice_responses))
    
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to fetch customer invoices: {str(e)}"
        )


@router.get("/reports/gst-summary", response_model=dict)
async def get_gst_summary(
    outlet_id: Optional[str] = None,
    from_date: Optional[date] = None,
    to_date: Optional[date] = None,
    current_user_id: str = Depends(require_roles(
        UserRole.ACCOUNTANT, UserRole.ADMIN, UserRole.SUPER_ADMIN
    ))
):
    """Get GST summary report"""
    try:
        current_user = await user_manager.fetch(current_user_id)
        
        filters = {}
        if outlet_id:
            filters["outlet_id"] = outlet_id
        elif current_user.role == UserRole.OUTLET_MANAGER and current_user.outlet_id:
            filters["outlet_id"] = current_user.outlet_id
        
        invoices = await invoice_manager.fetch_all(filters=filters)
        
        # Calculate GST summary
        total_sales = Decimal('0')
        total_gst = Decimal('0')
        cgst_total = Decimal('0')
        sgst_total = Decimal('0')
        igst_total = Decimal('0')
        
        for invoice in invoices.items:
            if from_date and invoice.invoice_date < from_date:
                continue
            if to_date and invoice.invoice_date > to_date:
                continue
            
            total_sales += invoice.total_amount
            total_gst += invoice.gst_amount
            cgst_total += invoice.cgst_amount
            sgst_total += invoice.sgst_amount
            igst_total += invoice.igst_amount
        
        return {
            "period": {
                "from_date": from_date,
                "to_date": to_date
            },
            "summary": {
                "total_sales": float(total_sales),
                "total_gst": float(total_gst),
                "cgst_total": float(cgst_total),
                "sgst_total": float(sgst_total),
                "igst_total": float(igst_total)
            }
        }
    
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to generate GST summary: {str(e)}"
        )


@router.get("/reports/sales-summary", response_model=dict)
async def get_sales_summary(
    outlet_id: Optional[str] = None,
    from_date: Optional[date] = None,
    to_date: Optional[date] = None,
    current_user_id: str = Depends(require_roles(
        UserRole.OUTLET_MANAGER, UserRole.ACCOUNTANT, UserRole.ADMIN, UserRole.SUPER_ADMIN
    ))
):
    """Get sales summary report"""
    try:
        current_user = await user_manager.fetch(current_user_id)
        
        filters = {}
        if outlet_id:
            filters["outlet_id"] = outlet_id
        elif current_user.role == UserRole.OUTLET_MANAGER and current_user.outlet_id:
            filters["outlet_id"] = current_user.outlet_id
        
        invoices = await invoice_manager.fetch_all(filters=filters)
        
        # Calculate sales summary
        total_invoices = 0
        total_sales = Decimal('0')
        cash_sales = Decimal('0')
        online_sales = Decimal('0')
        
        for invoice in invoices.items:
            if from_date and invoice.invoice_date < from_date:
                continue
            if to_date and invoice.invoice_date > to_date:
                continue
            
            total_invoices += 1
            total_sales += invoice.total_amount
            
            if invoice.payment_method == PaymentMethod.CASH:
                cash_sales += invoice.total_amount
            else:
                online_sales += invoice.total_amount
        
        return {
            "period": {
                "from_date": from_date,
                "to_date": to_date
            },
            "summary": {
                "total_invoices": total_invoices,
                "total_sales": float(total_sales),
                "cash_sales": float(cash_sales),
                "online_sales": float(online_sales),
                "average_invoice_value": float(total_sales / total_invoices) if total_invoices > 0 else 0
            }
        }
    
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to generate sales summary: {str(e)}"
        )


# Duplicate /{invoice_id} route removed - moved to top of file
    """Get specific invoice details"""
    try:
        invoice = await invoice_manager.fetch(invoice_id)
        
        # Check access permissions
        current_user = await user_manager.fetch(current_user_id)
        if current_user.role == UserRole.OUTLET_MANAGER:
            if current_user.outlet_id != invoice.outlet_id:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Access denied to this invoice"
                )
        
        # Get invoice items
        invoice_items = await invoice_item_manager.fetch_all(
            filters={"invoice_id": invoice_id}
        )
        
        items = []
        for item in invoice_items.items:
            try:
                product = await product_manager.fetch(item.product_id)
                items.append(InvoiceItemResponse(
                    product_id=item.product_id,
                    product_name=product.product_name,
                    quantity=item.quantity,
                    unit_price=item.unit_price,
                    total_price=item.total_price,
                    gst_rate=item.gst_rate
                ))
            except:
                items.append(InvoiceItemResponse(
                    product_id=item.product_id,
                    product_name="Unknown Product",
                    quantity=item.quantity,
                    unit_price=item.unit_price,
                    total_price=item.total_price,
                    gst_rate=item.gst_rate
                ))
        
        return InvoiceResponse(
            uid=invoice.uid,
            invoice_number=invoice.invoice_number,
            invoice_date=invoice.invoice_date,
            customer_name=invoice.customer_name,
            customer_phone=invoice.customer_phone,
            customer_address=invoice.customer_address,
            outlet_id=invoice.outlet_id,
            total_amount=invoice.total_amount,
            tax_amount=invoice.tax_amount,
            discount_amount=invoice.discount_amount,
            payment_method=invoice.payment_method,
            payment_status=invoice.payment_status,
            notes=invoice.notes,
            items=items,
            created_at=invoice.created_at,
            created_by=invoice.created_by,
            is_cancelled=invoice.is_cancelled
        )
    
    except HTTPException:
        raise
    except Exception as e:
        if "not found" in str(e).lower():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Invoice not found"
            )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to fetch invoice: {str(e)}"
        )


# Duplicate /{invoice_id}/pdf route removed - moved to top of file
    """Generate PDF for invoice"""
    try:
        # Generate PDF using service
        pdf_bytes = await invoice_service.generate_invoice_pdf(invoice_id, current_user_id)
        
        return Response(
            content=pdf_bytes,
            media_type="application/pdf",
            headers={"Content-Disposition": f"attachment; filename=invoice_{invoice_id}.pdf"}
        )
    
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to generate PDF: {str(e)}"
        )


# Duplicate /{invoice_id}/print route removed - moved to top of file
    """Get invoice print view (redirect to PDF)"""
    return {
        "status": "success",
        "message": "Use PDF endpoint for printing",
        "pdf_url": f"/api/v1/invoices/{invoice_id}/pdf",
        "note": "Download PDF and print from your device"
    }


# GENERIC ROUTES LAST (after all specific routes)

@router.get("", response_model=ListResponse[InvoiceResponse])
async def list_invoices(
    outlet_id: Optional[str] = None,
    customer_phone: Optional[str] = None,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    is_cancelled: Optional[bool] = None,
    limit: int = 50,
    offset: int = 0,
    current_user_id: str = Depends(require_roles(
        UserRole.SUPER_ADMIN, UserRole.ADMIN, UserRole.OUTLET_MANAGER, UserRole.ACCOUNTANT
    ))
):
    """
    List invoices with filters
    Outlet managers can only see their outlet's invoices
    """
    try:
        # Check outlet access for outlet managers
        current_user = await user_manager.fetch(current_user_id)
        if current_user.role == UserRole.OUTLET_MANAGER:
            if outlet_id and outlet_id != current_user.outlet_id:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Access denied to this outlet's invoices"
                )
            outlet_id = current_user.outlet_id
        
        filters = {}
        if outlet_id:
            filters["outlet_id"] = outlet_id
        if customer_phone:
            filters["customer_phone"] = customer_phone
        if is_cancelled is not None:
            filters["is_cancelled"] = is_cancelled
        
        invoices = await invoice_manager.fetch_all(
            limit=limit,
            offset=offset,
            filters=filters if filters else None
        )
        
        # Filter by date range (would be better with SQL query)
        filtered_invoices = []
        for invoice in invoices.items:
            if start_date and invoice.invoice_date < start_date:
                continue
            if end_date and invoice.invoice_date > end_date:
                continue
            filtered_invoices.append(invoice)
        
        # Get items for each invoice
        invoice_responses = []
        for invoice in filtered_invoices:
            items = await invoice_item_manager.fetch_all(
                filters={"invoice_id": invoice.uid}
            )
            
            item_responses = [
                InvoiceItemResponse(
                    uid=item.uid,
                    product_id=item.product_id,
                    product_name=item.product_name,
                    hsn_code=item.hsn_code,
                    quantity=item.quantity,
                    unit_price=item.unit_price,
                    total_price=item.total_price,  # Add missing total_price field
                    discount_percentage=item.discount_percentage,
                    discount_amount=item.discount_amount,
                    taxable_amount=item.taxable_amount,
                    tax_rate=item.tax_rate,
                    cgst_rate=item.cgst_rate,
                    cgst_amount=item.cgst_amount,
                    sgst_rate=item.sgst_rate,
                    sgst_amount=item.sgst_amount,
                    igst_rate=item.igst_rate,
                    igst_amount=item.igst_amount,
                    total_tax=item.total_tax,
                    total_amount=item.total_amount
                )
                for item in items.items
            ]
            
            invoice_responses.append(InvoiceResponse(
                uid=invoice.uid,
                invoice_number=invoice.invoice_number,
                outlet_id=invoice.outlet_id,
                customer_name=invoice.customer_name,
                customer_phone=invoice.customer_phone,
                customer_email=invoice.customer_email,
                customer_address=invoice.customer_address,
                customer_gstin=invoice.customer_gstin,
                customer_state_code=invoice.customer_state_code,
                invoice_date=invoice.invoice_date,
                invoice_type=invoice.invoice_type,
                payment_method=invoice.payment_method,
                payment_status=invoice.payment_status,
                subtotal=invoice.subtotal,
                discount_amount=invoice.discount_amount,
                taxable_amount=invoice.taxable_amount,
                cgst_amount=invoice.cgst_amount,
                sgst_amount=invoice.sgst_amount,
                igst_amount=invoice.igst_amount,
                total_tax=invoice.total_tax,
                total_amount=invoice.total_amount,
                amount_paid=invoice.amount_paid,
                balance_amount=invoice.balance_amount,
                notes=invoice.notes,
                is_cancelled=invoice.is_cancelled,
                cancelled_reason=invoice.cancelled_reason,
                created_by=invoice.created_by,
                items=item_responses,
                created_at=invoice.created_at
            ))
        
        return ListResponse(items=invoice_responses, count=len(invoice_responses))
    
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to fetch invoices: {str(e)}"
        )


# Duplicate next-number route removed - moved to top of file


@router.post("", response_model=InvoiceResponse)
async def create_invoice(
    payload: InvoiceCreateRequest,
    current_user_id: str = Depends(require_roles(
        UserRole.SUPER_ADMIN, UserRole.ADMIN, UserRole.OUTLET_MANAGER
    ))
):
    """
    Create new invoice with GST calculations
    Note: Inventory deduction now handled during order creation
    """
    try:
        # Check outlet access
        current_user = await user_manager.fetch(current_user_id)
        if current_user.role == UserRole.OUTLET_MANAGER and current_user.outlet_id != payload.outlet_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Access denied to this outlet"
            )
        
        # Prepare customer details
        customer_details = None
        if any([payload.customer_name, payload.customer_phone, payload.customer_email]):
            customer_details = {
                "customer_name": payload.customer_name,
                "customer_phone": payload.customer_phone,
                "customer_email": payload.customer_email,
                "customer_address": payload.customer_address,
                "customer_gstin": payload.customer_gstin,
                "customer_state_code": payload.customer_state_code
            }
        
        # Prepare items (unit_price will be calculated by the service)
        items = [
            {
                "product_id": item.product_id,
                "quantity": item.quantity,
                "product_manual_discount": item.product_manual_discount
            }
            for item in payload.items
        ]
        
        # Create invoice using service
        result = await invoice_service.create_invoice(
            outlet_id=payload.outlet_id,
            items=items,
            customer_details=customer_details,
            payment_method=payload.payment_method,
            discount_amount=payload.discount_amount,  # Ignored in new logic
            prepaid_amount=payload.prepaid_amount,  # New field
            notes=payload.notes,
            created_by=current_user_id
        )
        
        invoice = result['invoice']
        created_items = result['items']
        
        # Prepare response
        item_responses = [
            InvoiceItemResponse(
                uid=item.uid,
                product_id=item.product_id,
                product_name=item.product_name,
                hsn_code=item.hsn_code,
                quantity=item.quantity,
                unit_price=item.unit_price,
                total_price=item.total_price,
                product_manual_discount=item.product_manual_discount,  # New field
                discount_percentage=getattr(item, 'discount_percentage', None),  # Optional field
                discount_amount=item.discount_amount,
                taxable_amount=item.taxable_amount,
                tax_rate=item.tax_rate,
                cgst_rate=item.cgst_rate,
                cgst_amount=item.cgst_amount,
                sgst_rate=item.sgst_rate,
                sgst_amount=item.sgst_amount,
                igst_rate=item.igst_rate,
                igst_amount=item.igst_amount,
                total_tax=item.total_tax,
                total_amount=item.total_amount
            )
            for item in created_items
        ]
        
        return InvoiceResponse(
            uid=invoice.uid,
            invoice_number=invoice.invoice_number,
            outlet_id=invoice.outlet_id,
            customer_name=invoice.customer_name,
            customer_phone=invoice.customer_phone,
            customer_email=invoice.customer_email,
            customer_address=invoice.customer_address,
            customer_gstin=invoice.customer_gstin,
            customer_state_code=invoice.customer_state_code,
            invoice_date=invoice.invoice_date,
            invoice_type=invoice.invoice_type,
            payment_method=invoice.payment_method,
            payment_status=invoice.payment_status,
            subtotal=invoice.subtotal,
            discount_amount=invoice.discount_amount,
            taxable_amount=invoice.taxable_amount,
            cgst_amount=invoice.cgst_amount,
            sgst_amount=invoice.sgst_amount,
            igst_amount=invoice.igst_amount,
            total_tax=invoice.total_tax,
            total_amount=invoice.total_amount,
            amount_paid=invoice.amount_paid,
            balance_amount=invoice.balance_amount,
            notes=invoice.notes,
            is_cancelled=invoice.is_cancelled,
            cancelled_reason=invoice.cancelled_reason,
            created_by=invoice.created_by,
            items=item_responses,
            created_at=invoice.created_at
        )
    
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to create invoice: {str(e)}"
        )


# Duplicate customer route removed - moved to top of file


# Duplicate /{invoice_id} route removed - moved to top of file
    """
    Get invoice details with items
    """
    try:
        invoice = await invoice_manager.fetch(invoice_id)
        
        # Check outlet access
        current_user = await user_manager.fetch(current_user_id)
        if current_user.role == UserRole.OUTLET_MANAGER and current_user.outlet_id != invoice.outlet_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Access denied to this invoice"
            )
        
        # Get invoice items
        items = await invoice_item_manager.fetch_all(
            filters={"invoice_id": invoice_id}
        )
        
        item_responses = [
            InvoiceItemResponse(
                uid=item.uid,
                product_id=item.product_id,
                product_name=item.product_name,
                hsn_code=item.hsn_code,
                quantity=item.quantity,
                unit_price=item.unit_price,
                total_price=item.total_price,  # Add missing total_price field
                discount_percentage=item.discount_percentage,
                discount_amount=item.discount_amount,
                taxable_amount=item.taxable_amount,
                tax_rate=item.tax_rate,
                cgst_rate=item.cgst_rate,
                cgst_amount=item.cgst_amount,
                sgst_rate=item.sgst_rate,
                sgst_amount=item.sgst_amount,
                igst_rate=item.igst_rate,
                igst_amount=item.igst_amount,
                total_tax=item.total_tax,
                total_amount=item.total_amount
            )
            for item in items.items
        ]
        
        return InvoiceResponse(
            uid=invoice.uid,
            invoice_number=invoice.invoice_number,
            outlet_id=invoice.outlet_id,
            customer_name=invoice.customer_name,
            customer_phone=invoice.customer_phone,
            customer_email=invoice.customer_email,
            customer_address=invoice.customer_address,
            customer_gstin=invoice.customer_gstin,
            customer_state_code=invoice.customer_state_code,
            invoice_date=invoice.invoice_date,
            invoice_type=invoice.invoice_type,
            payment_method=invoice.payment_method,
            payment_status=invoice.payment_status,
            subtotal=invoice.subtotal,
            discount_amount=invoice.discount_amount,
            taxable_amount=invoice.taxable_amount,
            cgst_amount=invoice.cgst_amount,
            sgst_amount=invoice.sgst_amount,
            igst_amount=invoice.igst_amount,
            total_tax=invoice.total_tax,
            total_amount=invoice.total_amount,
            amount_paid=invoice.amount_paid,
            balance_amount=invoice.balance_amount,
            notes=invoice.notes,
            is_cancelled=invoice.is_cancelled,
            cancelled_reason=invoice.cancelled_reason,
            created_by=invoice.created_by,
            items=item_responses,
            created_at=invoice.created_at
        )
    
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Invoice not found: {str(e)}"
        )


# Duplicate /{invoice_id}/pdf route removed - moved to top of file
    """
    Generate and download invoice PDF
    """
    try:
        # Check access permissions
        invoice = await invoice_manager.fetch(invoice_id)
        current_user = await user_manager.fetch(current_user_id)
        
        if current_user.role == UserRole.OUTLET_MANAGER and current_user.outlet_id != invoice.outlet_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Access denied to this invoice"
            )
        
        # Generate PDF
        pdf_buffer = await invoice_service.generate_invoice_pdf(invoice_id)
        
        # Return PDF as downloadable file
        from fastapi.responses import StreamingResponse
        
        return StreamingResponse(
            BytesIO(pdf_buffer.read()),
            media_type="application/pdf",
            headers={
                "Content-Disposition": f"attachment; filename=invoice_{invoice.invoice_number}.pdf"
            }
        )
    
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to generate PDF: {str(e)}"
        )


# Duplicate /{invoice_id}/print route removed - moved to top of file
    """
    Get invoice in print-ready format (HTML or redirect to PDF)
    """
    try:
        # Check access permissions
        invoice = await invoice_manager.fetch(invoice_id)
        current_user = await user_manager.fetch(current_user_id)
        
        if current_user.role == UserRole.OUTLET_MANAGER and current_user.outlet_id != invoice.outlet_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Access denied to this invoice"
            )
        
        # For now, redirect to PDF generation
        # In future, this could return HTML template for browser printing
        return {
            "status": "success",
            "message": "Use PDF endpoint for printing",
            "pdf_url": f"/api/v1/invoices/{invoice_id}/pdf",
            "note": "Download PDF and print from your device"
        }
    
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get print view: {str(e)}"
        )


@router.post("/{invoice_id}/email")
async def email_invoice(
    invoice_id: str,
    email_address: Optional[str] = None,
    _: str = Depends(require_roles(
        UserRole.SUPER_ADMIN, UserRole.ADMIN, UserRole.OUTLET_MANAGER
    ))
):
    """
    Email invoice to customer
    TODO: Implement email service integration
    """
    try:
        invoice = await invoice_manager.fetch(invoice_id)
        
        # Use provided email or customer email from invoice
        target_email = email_address or invoice.customer_email
        
        if not target_email:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="No email address provided and customer email not available"
            )
        
        # For now, return a placeholder response
        return {
            "status": "success",
            "message": "Email service not yet implemented",
            "target_email": target_email,
            "invoice_number": invoice.invoice_number,
            "note": "This endpoint will send invoice via email"
        }
    
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to email invoice: {str(e)}"
        )




@router.get("/{invoice_id}", response_model=InvoiceResponse)
async def get_invoice(
    invoice_id: str,
    _: str = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.ADMIN, UserRole.OUTLET_MANAGER, UserRole.ACCOUNTANT))
):
    """Get invoice by ID with items"""
    try:
        # Get invoice
        invoice = await invoice_manager.fetch(invoice_id)
        
        # Get invoice items
        items = await invoice_item_manager.fetch_all(filters={"invoice_id": invoice_id})
        
        # Convert to response format
        invoice_dict = invoice.__dict__.copy()
        invoice_dict['items'] = [item.__dict__ for item in items.items]
        
        return InvoiceResponse(**invoice_dict)
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Invoice not found: {str(e)}"
        )


@router.put("/{invoice_id}", response_model=InvoiceResponse)
async def update_invoice(
    invoice_id: str,
    payload: InvoiceCreateRequest,  # Reuse create request for updates
    _: str = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.ADMIN, UserRole.OUTLET_MANAGER))
):
    """Update invoice (only if not finalized)"""
    try:
        # Check if invoice exists and is not cancelled
        existing_invoice = await invoice_manager.fetch(invoice_id)
        
        if existing_invoice.is_cancelled:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Cannot update cancelled invoice"
            )
        
        # For now, only allow updating notes and customer details
        updates = {
            "customer_name": payload.customer_name,
            "customer_phone": payload.customer_phone,
            "customer_email": payload.customer_email,
            "customer_address": payload.customer_address,
            "customer_gstin": payload.customer_gstin,
            "customer_state_code": payload.customer_state_code,
            "notes": payload.notes
        }
        
        # Remove None values
        updates = {k: v for k, v in updates.items() if v is not None}
        
        updated_invoice = await invoice_manager.update(invoice_id, updates)
        return updated_invoice
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Failed to update invoice: {str(e)}"
        )


@router.get("/{invoice_id}/pdf")
async def download_invoice_pdf(
    invoice_id: str,
    _: str = Depends(require_roles(UserRole.SUPER_ADMIN, UserRole.ADMIN, UserRole.OUTLET_MANAGER, UserRole.ACCOUNTANT))
):
    """Download invoice as PDF"""
    try:
        from services.invoice_service import InvoiceService
        
        invoice_service = InvoiceService(engine)
        pdf_buffer = await invoice_service.generate_invoice_pdf(invoice_id)
        
        from fastapi.responses import StreamingResponse
        import io
        
        return StreamingResponse(
            io.BytesIO(pdf_buffer.getvalue()),
            media_type="application/pdf",
            headers={"Content-Disposition": f"attachment; filename=invoice_{invoice_id}.pdf"}
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to generate PDF: {str(e)}"
        )


@router.delete("/{invoice_id}")
async def cancel_invoice(
    invoice_id: str,
    reason: str,
    current_user_id: str = Depends(require_roles(
        UserRole.SUPER_ADMIN, UserRole.ADMIN, UserRole.OUTLET_MANAGER
    ))
):
    """
    Cancel invoice and restore inventory
    """
    try:
        # Check access permissions
        invoice = await invoice_manager.fetch(invoice_id)
        current_user = await user_manager.fetch(current_user_id)
        
        if current_user.role == UserRole.OUTLET_MANAGER and current_user.outlet_id != invoice.outlet_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Access denied to this invoice"
            )
        
        # Cancel invoice using service
        success = await invoice_service.cancel_invoice(invoice_id, reason, current_user_id)
        
        if success:
            return StatusResponse(
                status="ok",
                message="Invoice cancelled successfully and inventory restored"
            )
        else:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to cancel invoice"
            )
    
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to cancel invoice: {str(e)}"
        )
# Helper function for GST calculations
async def calculate_gst_amounts(taxable_amount: Decimal, tax_rate: Decimal, outlet_state_code: str, customer_state_code: str = None) -> dict:
    """Calculate CGST, SGST, IGST amounts based on state codes"""
    total_tax = (taxable_amount * tax_rate) / 100
    
    # If customer state is not provided or same as outlet state: CGST + SGST
    if not customer_state_code or customer_state_code == outlet_state_code:
        cgst_rate = tax_rate / 2
        sgst_rate = tax_rate / 2
        cgst_amount = total_tax / 2
        sgst_amount = total_tax / 2
        igst_rate = Decimal('0.00')
        igst_amount = Decimal('0.00')
    else:
        # Different states: IGST
        cgst_rate = Decimal('0.00')
        sgst_rate = Decimal('0.00')
        cgst_amount = Decimal('0.00')
        sgst_amount = Decimal('0.00')
        igst_rate = tax_rate
        igst_amount = total_tax
    
    return {
        "cgst_rate": cgst_rate,
        "cgst_amount": cgst_amount,
        "sgst_rate": sgst_rate,
        "sgst_amount": sgst_amount,
        "igst_rate": igst_rate,
        "igst_amount": igst_amount,
        "total_tax": total_tax
    }
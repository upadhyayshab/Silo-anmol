from typing import Dict, List, Optional, Tuple
from decimal import Decimal, ROUND_HALF_UP
from datetime import datetime, date
from io import BytesIO
import uuid
import json

from managers import (
    SalesInvoiceManager, SalesInvoiceItemManager, InventoryManager,
    ProductManager, OutletManager, SystemConfigurationManager,
    SalesInvoiceSchema, SalesInvoiceItemSchema
)
from utils.constants import InvoiceType, PaymentStatus, PaymentMethod
from services.pdf_service import InvoicePDFGenerator


class InvoiceService:
    """Service for invoice generation and GST calculations"""
    
    def __init__(self, engine):
        self.engine = engine
        self.invoice_manager = SalesInvoiceManager(engine)
        self.invoice_item_manager = SalesInvoiceItemManager(engine)
        self.inventory_manager = InventoryManager(engine)
        self.product_manager = ProductManager(engine)
        self.outlet_manager = OutletManager(engine)
        self.config_manager = SystemConfigurationManager(engine)
        self.pdf_generator = InvoicePDFGenerator()
    
    async def generate_invoice_number(self, outlet_code: str) -> str:
        """
        Generate sequential invoice number
        Format: INV-{OUTLET_CODE}-{YEAR}-{SEQUENTIAL}
        """
        current_year = datetime.now().year
        
        # Get last invoice for this outlet and year
        all_invoices = await self.invoice_manager.fetch_all()
        
        # Filter invoices for this outlet and year
        outlet_year_invoices = [
            inv for inv in all_invoices.items
            if inv.invoice_number.startswith(f"INV-{outlet_code}-{current_year}")
        ]
        
        # Get next sequential number
        if outlet_year_invoices:
            # Extract sequence numbers and find max
            sequences = []
            for inv in outlet_year_invoices:
                try:
                    seq_part = inv.invoice_number.split('-')[-1]
                    sequences.append(int(seq_part))
                except:
                    continue
            
            next_seq = max(sequences) + 1 if sequences else 1
        else:
            next_seq = 1
        
        return f"INV-{outlet_code}-{current_year}-{next_seq:06d}"
    
    def calculate_gst_amounts(
        self,
        taxable_amount: Decimal,
        tax_rate: Decimal,
        outlet_state_code: str,
        customer_state_code: Optional[str]
    ) -> Dict[str, Decimal]:
        """
        Calculate GST amounts based on state codes
        Returns dict with cgst_rate, cgst_amount, sgst_rate, sgst_amount, igst_rate, igst_amount
        """
        # Round to 2 decimal places
        def round_decimal(value: Decimal) -> Decimal:
            return value.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        
        # Determine if inter-state or intra-state
        is_inter_state = (
            customer_state_code and 
            customer_state_code != outlet_state_code
        )
        
        if is_inter_state:
            # Inter-state: IGST only
            igst_rate = tax_rate
            igst_amount = round_decimal(taxable_amount * igst_rate / 100)
            
            return {
                "cgst_rate": Decimal('0.00'),
                "cgst_amount": Decimal('0.00'),
                "sgst_rate": Decimal('0.00'),
                "sgst_amount": Decimal('0.00'),
                "igst_rate": igst_rate,
                "igst_amount": igst_amount
            }
        else:
            # Intra-state: CGST + SGST (split equally)
            cgst_rate = sgst_rate = tax_rate / 2
            cgst_amount = round_decimal(taxable_amount * cgst_rate / 100)
            sgst_amount = round_decimal(taxable_amount * sgst_rate / 100)
            
            return {
                "cgst_rate": cgst_rate,
                "cgst_amount": cgst_amount,
                "sgst_rate": sgst_rate,
                "sgst_amount": sgst_amount,
                "igst_rate": Decimal('0.00'),
                "igst_amount": Decimal('0.00')
            }
    
    async def create_invoice(
        self,
        outlet_id: str,
        items: List[Dict],
        customer_details: Optional[Dict] = None,
        payment_method: PaymentMethod = PaymentMethod.CASH,
        discount_amount: Decimal = Decimal('0.00'),  # Ignored in new logic
        prepaid_amount: Decimal = Decimal('0.00'),  # New field
        notes: Optional[str] = None,
        created_by: str = None
    ) -> Dict:
        """
        Create complete invoice with new pricing logic (no GST, simplified calculations)
        Note: Inventory deduction is now handled during order creation to prevent double deduction
        """
        try:
            # Get outlet details
            outlet = await self.outlet_manager.fetch(outlet_id)
            
            # Generate invoice number
            invoice_number = await self.generate_invoice_number(outlet.outlet_code)
            
            # Calculate totals with new pricing logic
            taxable_amount = Decimal('0.00')  # Sum of all selling prices
            invoice_items = []
            
            for item_data in items:
                product = await self.product_manager.fetch(item_data['product_id'])
                quantity = item_data['quantity']
                
                # New pricing logic
                unit_price = product.cost_price  # No more product.discount deduction
                product_manual_discount = Decimal(str(item_data.get('product_manual_discount', 0)))
                
                # Calculate selling price: (cost_price - manual_discount_per_unit) * quantity
                selling_price_per_unit = unit_price - product_manual_discount
                # Ensure selling price is not negative
                if selling_price_per_unit < 0:
                    selling_price_per_unit = Decimal('0.00')
                
                total_price = unit_price * quantity  # Before discount
                total_discount_amount = product_manual_discount * quantity  # Total discount for this line
                selling_price_total = selling_price_per_unit * quantity  # Final selling price
                
                # No GST calculations - all GST amounts are 0
                gst_amounts = {
                    "cgst_rate": Decimal('0.00'),
                    "cgst_amount": Decimal('0.00'),
                    "sgst_rate": Decimal('0.00'),
                    "sgst_amount": Decimal('0.00'),
                    "igst_rate": Decimal('0.00'),
                    "igst_amount": Decimal('0.00')
                }
                
                total_tax = Decimal('0.00')  # No tax
                item_total = selling_price_total  # Same as selling price (no tax)
                
                invoice_items.append({
                    'product_id': product.uid,
                    'product_name': product.product_name,
                    'hsn_code': product.hsn_code,
                    'quantity': quantity,
                    'unit_price': unit_price,
                    'total_price': total_price,  # Before discount
                    'product_manual_discount': product_manual_discount,  # Per unit discount
                    'discount_percentage': Decimal('0.00'),  # Not used anymore
                    'discount_amount': total_discount_amount,  # Total discount for line
                    'taxable_amount': selling_price_total,  # Same as selling price
                    'tax_rate': Decimal('0.00'),  # No tax
                    'total_tax': total_tax,
                    'total_amount': item_total,
                    **gst_amounts
                })
                
                taxable_amount += selling_price_total
            
            # Calculate invoice totals (simplified - no GST)
            subtotal = sum([item['total_price'] for item in invoice_items])  # Before discounts
            total_discount = sum([item['discount_amount'] for item in invoice_items])
            total_tax = Decimal('0.00')  # No GST
            total_amount = taxable_amount  # Same as taxable amount (no tax)
            
            # Calculate payment breakdown
            prepaid = prepaid_amount or Decimal('0.00')
            paid_at_outlet = total_amount - prepaid
            
            # Create invoice record
            invoice = SalesInvoiceSchema(
                invoice_number=invoice_number,
                outlet_id=outlet_id,
                customer_name=customer_details.get('customer_name') if customer_details else None,
                customer_phone=customer_details.get('customer_phone') if customer_details else None,
                customer_email=customer_details.get('customer_email') if customer_details else None,
                customer_address=customer_details.get('customer_address') if customer_details else None,
                customer_gstin=customer_details.get('customer_gstin') if customer_details else None,
                customer_state_code=customer_details.get('customer_state_code') if customer_details else None,
                invoice_date=date.today(),
                invoice_type=InvoiceType.REGULAR,
                payment_method=payment_method,
                payment_status=PaymentStatus.PAID,
                subtotal=subtotal,
                discount_amount=total_discount,
                taxable_amount=taxable_amount,
                cgst_amount=Decimal('0.00'),
                sgst_amount=Decimal('0.00'),
                igst_amount=Decimal('0.00'),
                total_tax=Decimal('0.00'),
                total_amount=total_amount,
                amount_paid=total_amount,
                balance_amount=Decimal('0.00'),
                prepaid_amount=prepaid,
                paid_at_outlet=paid_at_outlet,
                notes=notes,
                is_cancelled=False,
                created_by=created_by
            )
            
            created_invoice = await self.invoice_manager.create(invoice)
            
            # Create invoice items
            created_items = []
            for item_data in invoice_items:
                item = SalesInvoiceItemSchema(
                    invoice_id=created_invoice.uid,
                    **item_data
                )
                created_item = await self.invoice_item_manager.create(item)
                created_items.append(created_item)
                
                # Note: Inventory deduction removed - now handled during order creation
                # This prevents double deduction when frontend creates orders for walk-in customers
            
            return {
                'invoice': created_invoice,
                'items': created_items,
                'totals': {
                    'subtotal': float(subtotal),
                    'discount_amount': float(total_discount),
                    'taxable_amount': float(taxable_amount),
                    'cgst_amount': 0.00,  # No GST
                    'sgst_amount': 0.00,  # No GST
                    'igst_amount': 0.00,  # No GST
                    'total_tax': 0.00,  # No GST
                    'total_amount': float(total_amount)
                }
            }
        
        except Exception as e:
            raise Exception(f"Failed to create invoice: {str(e)}")
    
    async def deduct_inventory(self, outlet_id: str, product_id: str, quantity: int):
        """
        Deduct quantity from outlet inventory
        """
        try:
            # Find inventory record
            inventory_items = await self.inventory_manager.fetch_all(
                filters={"outlet_id": outlet_id, "product_id": product_id}
            )
            
            if not inventory_items.items:
                raise Exception(f"No inventory found for product {product_id} at outlet {outlet_id}")
            
            inventory = inventory_items.items[0]
            
            # Check available quantity
            available = inventory.quantity - inventory.reserved_quantity
            if available < quantity:
                raise Exception(f"Insufficient inventory. Available: {available}, Required: {quantity}")
            
            # Deduct quantity
            new_quantity = inventory.quantity - quantity
            await self.inventory_manager.update(inventory.uid, {"quantity": new_quantity})
            
        except Exception as e:
            raise Exception(f"Failed to deduct inventory: {str(e)}")
    
    async def get_next_invoice_number(self, outlet_id: str) -> str:
        """
        Get next invoice number for an outlet
        """
        try:
            outlet = await self.outlet_manager.fetch(outlet_id)
            return await self.generate_invoice_number(outlet.outlet_code)
        except Exception as e:
            raise Exception(f"Failed to generate invoice number: {str(e)}")
    
    async def cancel_invoice(self, invoice_id: str, reason: str, user_id: str) -> bool:
        """
        Cancel invoice and restore inventory
        """
        try:
            # Get invoice with items
            invoice = await self.invoice_manager.fetch(invoice_id)
            
            if invoice.is_cancelled:
                raise Exception("Invoice is already cancelled")
            
            # Get invoice items
            items = await self.invoice_item_manager.fetch_all(
                filters={"invoice_id": invoice_id}
            )
            
            # Restore inventory for each item
            for item in items.items:
                await self.restore_inventory(invoice.outlet_id, item.product_id, item.quantity)
            
            # Mark invoice as cancelled
            await self.invoice_manager.update(invoice_id, {
                "is_cancelled": True,
                "cancelled_reason": reason
            })
            
            return True
        
        except Exception as e:
            raise Exception(f"Failed to cancel invoice: {str(e)}")
    
    async def restore_inventory(self, outlet_id: str, product_id: str, quantity: int):
        """
        Restore quantity to outlet inventory
        """
        try:
            inventory_items = await self.inventory_manager.fetch_all(
                filters={"outlet_id": outlet_id, "product_id": product_id}
            )
            
            if inventory_items.items:
                inventory = inventory_items.items[0]
                new_quantity = inventory.quantity + quantity
                await self.inventory_manager.update(inventory.uid, {"quantity": new_quantity})
            else:
                # Create new inventory record if doesn't exist
                from managers import InventorySchema
                inventory = InventorySchema(
                    product_id=product_id,
                    outlet_id=outlet_id,
                    quantity=quantity,
                    reserved_quantity=0
                )
                await self.inventory_manager.create(inventory)
        
        except Exception as e:
            raise Exception(f"Failed to restore inventory: {str(e)}")
    
    async def generate_invoice_pdf(self, invoice_id: str) -> BytesIO:
        """
        Generate PDF for an existing invoice
        """
        try:
            # Get invoice with all details
            invoice = await self.invoice_manager.fetch(invoice_id)
            
            # Get outlet details
            outlet = await self.outlet_manager.fetch(invoice.outlet_id)
            
            # Get invoice items
            items = await self.invoice_item_manager.fetch_all(
                filters={"invoice_id": invoice_id}
            )
            
            # Prepare data for PDF generation
            pdf_data = {
                'invoice_number': invoice.invoice_number,
                'invoice_date': invoice.invoice_date.strftime('%d-%m-%Y'),
                'outlet_name': outlet.outlet_name,
                'outlet_gstin': outlet.gstin,  # Add outlet GSTIN
                'payment_method': invoice.payment_method.value.upper(),
                'customer_name': invoice.customer_name,
                'customer_phone': invoice.customer_phone,
                'customer_email': invoice.customer_email,
                'customer_address': invoice.customer_address,
                'customer_gstin': invoice.customer_gstin,
                'subtotal': float(invoice.subtotal),
                'discount_amount': float(invoice.discount_amount),
                'taxable_amount': float(invoice.taxable_amount),
                'cgst_amount': float(invoice.cgst_amount),
                'sgst_amount': float(invoice.sgst_amount),
                'igst_amount': float(invoice.igst_amount),
                'total_tax': float(invoice.total_tax),
                'total_amount': float(invoice.total_amount),
                'prepaid_amount': float(getattr(invoice, 'prepaid_amount', 0)),  # New field
                'paid_at_outlet': float(getattr(invoice, 'paid_at_outlet', invoice.total_amount)),  # New field
                'items': []
            }
            
            # Add items data
            for item in items.items:
                pdf_data['items'].append({
                    'product_name': item.product_name,
                    'hsn_code': item.hsn_code,
                    'quantity': item.quantity,
                    'unit_price': float(item.unit_price),
                    'product_manual_discount': float(getattr(item, 'product_manual_discount', 0)),  # New field
                    'discount_amount': float(item.discount_amount),
                    'taxable_amount': float(item.taxable_amount),
                    'tax_rate': float(item.tax_rate),
                    'cgst_rate': float(item.cgst_rate),
                    'sgst_rate': float(item.sgst_rate),
                    'igst_rate': float(item.igst_rate),
                    'total_tax': float(item.total_tax),
                    'total_amount': float(item.total_amount)
                })
            
            # Generate PDF. pisa.CreatePDF is sync + CPU-bound — offload to a thread
            # so it doesn't block the single-process event loop.
            from starlette.concurrency import run_in_threadpool
            return await run_in_threadpool(self.pdf_generator.generate_invoice_pdf, pdf_data)
        
        except Exception as e:
            raise Exception(f"Failed to generate invoice PDF: {str(e)}")


__all__ = ["InvoiceService"]
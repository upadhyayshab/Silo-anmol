from typing import Dict, List, Optional, Tuple
from decimal import Decimal, ROUND_HALF_UP
from datetime import datetime, date
import uuid
import json

from managers import (
    SalesInvoiceManager, SalesInvoiceItemManager, InventoryManager,
    ProductManager, OutletManager, SystemConfigurationManager,
    SalesInvoiceSchema, SalesInvoiceItemSchema
)
from utils.constants import InvoiceType, PaymentStatus, PaymentMethod


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
        discount_amount: Decimal = Decimal('0.00'),
        notes: Optional[str] = None,
        created_by: str = None
    ) -> Dict:
        """
        Create complete invoice with GST calculations and inventory deduction
        """
        try:
            # Get outlet details
            outlet = await self.outlet_manager.fetch(outlet_id)
            
            # Generate invoice number
            invoice_number = await self.generate_invoice_number(outlet.outlet_code)
            
            # Calculate totals
            subtotal = Decimal('0.00')
            invoice_items = []
            
            for item_data in items:
                product = await self.product_manager.fetch(item_data['product_id'])
                quantity = item_data['quantity']
                unit_price = Decimal(str(item_data.get('unit_price', product.unit_price)))
                discount_percentage = Decimal(str(item_data.get('discount_percentage', 0)))
                
                # Calculate item amounts
                line_total = unit_price * quantity
                item_discount_amount = line_total * discount_percentage / 100
                taxable_amount = line_total - item_discount_amount
                
                # Calculate GST for this item
                gst_amounts = self.calculate_gst_amounts(
                    taxable_amount,
                    product.tax_rate,
                    outlet.state_code,
                    customer_details.get('customer_state_code') if customer_details else None
                )
                
                total_tax = (
                    gst_amounts['cgst_amount'] + 
                    gst_amounts['sgst_amount'] + 
                    gst_amounts['igst_amount']
                )
                
                item_total = taxable_amount + total_tax
                
                invoice_items.append({
                    'product_id': product.uid,
                    'product_name': product.product_name,
                    'hsn_code': product.hsn_code,
                    'quantity': quantity,
                    'unit_price': unit_price,
                    'discount_percentage': discount_percentage,
                    'discount_amount': item_discount_amount,
                    'taxable_amount': taxable_amount,
                    'tax_rate': product.tax_rate,
                    'total_tax': total_tax,
                    'total_amount': item_total,
                    **gst_amounts
                })
                
                subtotal += line_total
            
            # Calculate invoice totals
            total_discount = discount_amount + sum([item['discount_amount'] for item in invoice_items])
            taxable_amount = subtotal - total_discount
            
            total_cgst = sum([item['cgst_amount'] for item in invoice_items])
            total_sgst = sum([item['sgst_amount'] for item in invoice_items])
            total_igst = sum([item['igst_amount'] for item in invoice_items])
            total_tax = total_cgst + total_sgst + total_igst
            
            total_amount = taxable_amount + total_tax
            
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
                cgst_amount=total_cgst,
                sgst_amount=total_sgst,
                igst_amount=total_igst,
                total_tax=total_tax,
                total_amount=total_amount,
                amount_paid=total_amount,
                balance_amount=Decimal('0.00'),
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
                
                # Deduct inventory
                await self.deduct_inventory(outlet_id, item_data['product_id'], item_data['quantity'])
            
            return {
                'invoice': created_invoice,
                'items': created_items,
                'totals': {
                    'subtotal': float(subtotal),
                    'discount_amount': float(total_discount),
                    'taxable_amount': float(taxable_amount),
                    'cgst_amount': float(total_cgst),
                    'sgst_amount': float(total_sgst),
                    'igst_amount': float(total_igst),
                    'total_tax': float(total_tax),
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


__all__ = ["InvoiceService"]
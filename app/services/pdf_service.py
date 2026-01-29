from decimal import Decimal
from typing import Dict, Any
from io import BytesIO
from datetime import date
import os
from pathlib import Path


class InvoicePDFGenerator:
    """Template-based invoice PDF generator for ERP system using xhtml2pdf."""

    def __init__(self):
        """Initialize the PDF generator with template support."""
        self.template_dir = Path(__file__).parent.parent / "templates" / "invoices"
        
    def _setup_jinja_env(self):
        """Setup Jinja2 template environment."""
        try:
            from jinja2 import Environment, FileSystemLoader
            return Environment(loader=FileSystemLoader(str(self.template_dir)))
        except ImportError:
            raise ImportError("jinja2 is required for template-based PDF generation. Install with: pip install jinja2")

    def _number_to_words(self, num: float) -> str:
        """Convert number to words (simplified Indian format)."""
        ones = ["", "One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight", "Nine",
                "Ten", "Eleven", "Twelve", "Thirteen", "Fourteen", "Fifteen", "Sixteen",
                "Seventeen", "Eighteen", "Nineteen"]
        tens = ["", "", "Twenty", "Thirty", "Forty", "Fifty", "Sixty", "Seventy", "Eighty", "Ninety"]

        def convert_less_than_thousand(n):
            if n == 0:
                return ""
            elif n < 20:
                return ones[n]
            elif n < 100:
                return tens[n // 10] + (" " + ones[n % 10] if n % 10 != 0 else "")
            else:
                return ones[n // 100] + " Hundred" + (" " + convert_less_than_thousand(n % 100) if n % 100 != 0 else "")

        if num == 0:
            return "Zero Rupees Only"

        num = int(round(num))
        result = ""

        # Crores
        if num >= 10000000:
            result += convert_less_than_thousand(num // 10000000) + " Crore "
            num %= 10000000

        # Lakhs
        if num >= 100000:
            result += convert_less_than_thousand(num // 100000) + " Lakh "
            num %= 100000

        # Thousands
        if num >= 1000:
            result += convert_less_than_thousand(num // 1000) + " Thousand "
            num %= 1000

        # Remainder
        if num > 0:
            result += convert_less_than_thousand(num)

        return result.strip() + " Rupees Only"

    def generate_invoice_pdf(self, invoice_data: Dict[str, Any]) -> BytesIO:
        """Generate invoice PDF using HTML template and return as BytesIO buffer."""
        
        try:
            from xhtml2pdf import pisa
        except ImportError:
            raise ImportError("xhtml2pdf is required for PDF generation. Install with: pip install xhtml2pdf")

        # Setup Jinja2 environment
        env = self._setup_jinja_env()
        
        # Load template
        template = env.get_template("silo_fortune.html")
        
        # Prepare template data
        template_data = {
            # Invoice details
            'invoice_number': invoice_data.get('invoice_number', 'N/A'),
            'invoice_date': invoice_data.get('invoice_date', 'N/A'),
            'outlet_name': invoice_data.get('outlet_name', 'N/A'),
            'outlet_gstin': invoice_data.get('outlet_gstin', ''),
            'payment_method': invoice_data.get('payment_method', 'CASH'),
            
            # Customer details
            'customer_name': invoice_data.get('customer_name', ''),
            'customer_phone': invoice_data.get('customer_phone', ''),
            'customer_email': invoice_data.get('customer_email', ''),
            'customer_address': invoice_data.get('customer_address', ''),
            'customer_gstin': invoice_data.get('customer_gstin', ''),
            
            # Items
            'items': invoice_data.get('items', []),
            
            # Totals
            'subtotal': float(invoice_data.get('subtotal', 0)),
            'discount_amount': float(invoice_data.get('discount_amount', 0)),
            'taxable_amount': float(invoice_data.get('taxable_amount', 0)),
            'cgst_amount': float(invoice_data.get('cgst_amount', 0)),
            'sgst_amount': float(invoice_data.get('sgst_amount', 0)),
            'igst_amount': float(invoice_data.get('igst_amount', 0)),
            'total_tax': float(invoice_data.get('total_tax', 0)),
            'total_amount': float(invoice_data.get('total_amount', 0)),
            
            # Amount in words
            'amount_in_words': self._number_to_words(float(invoice_data.get('total_amount', 0)))
        }
        
        # Render HTML
        html_content = template.render(**template_data)
        
        # Generate PDF using xhtml2pdf
        buffer = BytesIO()
        
        # Convert HTML to PDF
        pisa_status = pisa.CreatePDF(
            html_content,
            dest=buffer,
            encoding='utf-8'
        )
        
        if pisa_status.err:
            raise Exception(f"PDF generation failed: {pisa_status.err}")
        
        # Return as BytesIO buffer (same interface as before)
        buffer.seek(0)
        return buffer


__all__ = ["InvoicePDFGenerator"]
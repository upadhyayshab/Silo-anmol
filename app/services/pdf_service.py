from decimal import Decimal
from typing import Dict, Any
from io import BytesIO
from datetime import date


class InvoicePDFGenerator:
    """Clean and simple invoice PDF generator for ERP system."""

    # Clean color scheme - professional blues and grays
    HEADER_COLOR = '#2C3E50'      # Dark blue-gray for headers
    LIGHT_BG = '#F8F9FA'          # Very light gray background
    BORDER_COLOR = '#DEE2E6'      # Light gray borders
    TEXT_COLOR = '#212529'        # Dark text
    ACCENT_COLOR = '#495057'      # Medium gray for accents

    def __init__(self):
        """Initialize the PDF generator."""
        pass

    def _format_currency(self, amount: float) -> str:
        """Format amount with rupee symbol and proper formatting."""
        if amount is None:
            amount = 0.0
        
        amount = float(amount)
        if amount < 0:
            sign = "-"
            amount = abs(amount)
        else:
            sign = ""
        
        # Format with 2 decimal places
        return f"{sign}₹ {amount:,.2f}"

    def _format_percentage(self, rate: float) -> str:
        """Format tax rate as percentage."""
        if rate is None:
            rate = 0.0
        return f"{float(rate):.2f}%"

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
        """Generate clean invoice PDF and return as BytesIO buffer."""
        try:
            from reportlab.lib import colors
            from reportlab.lib.pagesizes import A4
            from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
            from reportlab.lib.units import inch, mm
            from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
            from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
            from reportlab.pdfgen import canvas
        except ImportError:
            raise ImportError("reportlab is required for PDF generation. Install with: pip install reportlab")

        # Create PDF in memory
        buffer = BytesIO()
        
        # Page setup
        page_width, page_height = A4
        margin = 20 * mm

        # Create PDF document
        doc = SimpleDocTemplate(
            buffer,
            pagesize=A4,
            rightMargin=margin,
            leftMargin=margin,
            topMargin=margin,
            bottomMargin=margin
        )

        # Styles
        styles = getSampleStyleSheet()
        
        title_style = ParagraphStyle(
            'InvoiceTitle',
            parent=styles['Heading1'],
            fontSize=18,
            alignment=TA_CENTER,
            textColor=colors.HexColor(self.HEADER_COLOR),
            spaceAfter=20,
            fontName='Helvetica-Bold'
        )
        
        section_header_style = ParagraphStyle(
            'SectionHeader',
            parent=styles['Heading2'],
            fontSize=12,
            alignment=TA_LEFT,
            textColor=colors.HexColor(self.HEADER_COLOR),
            spaceAfter=8,
            fontName='Helvetica-Bold'
        )
        
        normal_style = ParagraphStyle(
            'Normal',
            parent=styles['Normal'],
            fontSize=10,
            alignment=TA_LEFT,
            textColor=colors.HexColor(self.TEXT_COLOR)
        )

        elements = []

        # Invoice Title
        elements.append(Paragraph("TAX INVOICE", title_style))
        elements.append(Spacer(1, 10))

        # Invoice Header Information
        header_data = [
            ["Invoice Number:", invoice_data.get('invoice_number', 'N/A'), "Invoice Date:", invoice_data.get('invoice_date', 'N/A')],
            ["Outlet:", invoice_data.get('outlet_name', 'N/A'), "Payment Method:", invoice_data.get('payment_method', 'N/A')]
        ]
        
        header_table = Table(header_data, colWidths=[80, 150, 80, 150])
        header_table.setStyle(TableStyle([
            ('FONTSIZE', (0, 0), (-1, -1), 10),
            ('FONTNAME', (0, 0), (0, -1), 'Helvetica-Bold'),
            ('FONTNAME', (2, 0), (2, -1), 'Helvetica-Bold'),
            ('BACKGROUND', (0, 0), (-1, -1), colors.HexColor(self.LIGHT_BG)),
            ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor(self.BORDER_COLOR)),
            ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
            ('LEFTPADDING', (0, 0), (-1, -1), 8),
            ('RIGHTPADDING', (0, 0), (-1, -1), 8),
            ('TOPPADDING', (0, 0), (-1, -1), 6),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
        ]))
        elements.append(header_table)
        elements.append(Spacer(1, 15))

        # Customer Information (if available)
        if invoice_data.get('customer_name') or invoice_data.get('customer_phone'):
            elements.append(Paragraph("BILL TO", section_header_style))
            
            customer_info = []
            if invoice_data.get('customer_name'):
                customer_info.append(f"Name: {invoice_data['customer_name']}")
            if invoice_data.get('customer_phone'):
                customer_info.append(f"Phone: {invoice_data['customer_phone']}")
            if invoice_data.get('customer_email'):
                customer_info.append(f"Email: {invoice_data['customer_email']}")
            if invoice_data.get('customer_address'):
                customer_info.append(f"Address: {invoice_data['customer_address']}")
            if invoice_data.get('customer_gstin'):
                customer_info.append(f"GSTIN: {invoice_data['customer_gstin']}")
            
            customer_data = [[info] for info in customer_info]
            customer_table = Table(customer_data, colWidths=[460])
            customer_table.setStyle(TableStyle([
                ('FONTSIZE', (0, 0), (-1, -1), 10),
                ('BACKGROUND', (0, 0), (-1, -1), colors.HexColor(self.LIGHT_BG)),
                ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor(self.BORDER_COLOR)),
                ('LEFTPADDING', (0, 0), (-1, -1), 8),
                ('RIGHTPADDING', (0, 0), (-1, -1), 8),
                ('TOPPADDING', (0, 0), (-1, -1), 4),
                ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
            ]))
            elements.append(customer_table)
            elements.append(Spacer(1, 15))

        # Items Table Header
        elements.append(Paragraph("ITEMS", section_header_style))
        
        # Items table headers
        item_headers = [
            "S.No", "Product", "HSN", "Qty", "Rate", "Discount", 
            "Taxable", "Tax%", "Tax Amt", "Total"
        ]
        
        # Items data
        items_data = [item_headers]
        items = invoice_data.get('items', [])
        
        for idx, item in enumerate(items, 1):
            # Determine tax type and rate
            tax_rate = float(item.get('tax_rate', 0))
            cgst_rate = float(item.get('cgst_rate', 0))
            sgst_rate = float(item.get('sgst_rate', 0))
            igst_rate = float(item.get('igst_rate', 0))
            
            if igst_rate > 0:
                tax_display = f"IGST {self._format_percentage(igst_rate)}"
            else:
                tax_display = f"CGST {self._format_percentage(cgst_rate)}\nSGST {self._format_percentage(sgst_rate)}"
            
            items_data.append([
                str(idx),
                item.get('product_name', 'N/A'),
                item.get('hsn_code', 'N/A'),
                str(item.get('quantity', 0)),
                self._format_currency(item.get('unit_price', 0)),
                self._format_currency(item.get('discount_amount', 0)),
                self._format_currency(item.get('taxable_amount', 0)),
                tax_display,
                self._format_currency(item.get('total_tax', 0)),
                self._format_currency(item.get('total_amount', 0))
            ])
        
        # Items table
        items_table = Table(items_data, colWidths=[30, 80, 40, 30, 50, 50, 60, 60, 50, 60])
        items_table.setStyle(TableStyle([
            # Header row
            ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor(self.HEADER_COLOR)),
            ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
            ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
            ('FONTSIZE', (0, 0), (-1, 0), 9),
            ('ALIGN', (0, 0), (-1, 0), 'CENTER'),
            
            # Data rows
            ('FONTSIZE', (0, 1), (-1, -1), 8),
            ('BACKGROUND', (0, 1), (-1, -1), colors.white),
            ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor(self.BORDER_COLOR)),
            ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
            
            # Alignment
            ('ALIGN', (0, 1), (0, -1), 'CENTER'),  # S.No
            ('ALIGN', (2, 1), (2, -1), 'CENTER'),  # HSN
            ('ALIGN', (3, 1), (3, -1), 'CENTER'),  # Qty
            ('ALIGN', (4, 1), (-1, -1), 'RIGHT'),  # All amount columns
            
            # Padding
            ('LEFTPADDING', (0, 0), (-1, -1), 4),
            ('RIGHTPADDING', (0, 0), (-1, -1), 4),
            ('TOPPADDING', (0, 0), (-1, -1), 4),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
        ]))
        elements.append(items_table)
        elements.append(Spacer(1, 15))

        # Tax Summary
        elements.append(Paragraph("TAX SUMMARY", section_header_style))
        
        tax_summary_data = [
            ["Description", "Taxable Amount", "Tax Amount"],
        ]
        
        # Add tax breakdown
        cgst_total = float(invoice_data.get('cgst_amount', 0))
        sgst_total = float(invoice_data.get('sgst_amount', 0))
        igst_total = float(invoice_data.get('igst_amount', 0))
        
        if cgst_total > 0:
            tax_summary_data.append(["CGST", self._format_currency(invoice_data.get('taxable_amount', 0)), self._format_currency(cgst_total)])
        if sgst_total > 0:
            tax_summary_data.append(["SGST", self._format_currency(invoice_data.get('taxable_amount', 0)), self._format_currency(sgst_total)])
        if igst_total > 0:
            tax_summary_data.append(["IGST", self._format_currency(invoice_data.get('taxable_amount', 0)), self._format_currency(igst_total)])
        
        tax_summary_table = Table(tax_summary_data, colWidths=[200, 130, 130])
        tax_summary_table.setStyle(TableStyle([
            # Header
            ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor(self.HEADER_COLOR)),
            ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
            ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
            ('FONTSIZE', (0, 0), (-1, 0), 10),
            ('ALIGN', (0, 0), (-1, 0), 'CENTER'),
            
            # Data
            ('FONTSIZE', (0, 1), (-1, -1), 10),
            ('BACKGROUND', (0, 1), (-1, -1), colors.HexColor(self.LIGHT_BG)),
            ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor(self.BORDER_COLOR)),
            ('ALIGN', (1, 1), (-1, -1), 'RIGHT'),
            ('LEFTPADDING', (0, 0), (-1, -1), 8),
            ('RIGHTPADDING', (0, 0), (-1, -1), 8),
            ('TOPPADDING', (0, 0), (-1, -1), 6),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
        ]))
        elements.append(tax_summary_table)
        elements.append(Spacer(1, 15))

        # Totals Section
        elements.append(Paragraph("INVOICE TOTALS", section_header_style))
        
        totals_data = [
            ["Subtotal:", self._format_currency(invoice_data.get('subtotal', 0))],
            ["Discount:", self._format_currency(invoice_data.get('discount_amount', 0))],
            ["Taxable Amount:", self._format_currency(invoice_data.get('taxable_amount', 0))],
            ["Total Tax:", self._format_currency(invoice_data.get('total_tax', 0))],
            ["TOTAL AMOUNT:", self._format_currency(invoice_data.get('total_amount', 0))]
        ]
        
        totals_table = Table(totals_data, colWidths=[300, 160])
        totals_table.setStyle(TableStyle([
            ('FONTSIZE', (0, 0), (-1, -2), 10),
            ('FONTSIZE', (0, -1), (-1, -1), 12),  # Total amount larger
            ('FONTNAME', (0, 0), (0, -1), 'Helvetica-Bold'),
            ('FONTNAME', (0, -1), (-1, -1), 'Helvetica-Bold'),  # Total amount bold
            ('BACKGROUND', (0, 0), (-1, -2), colors.HexColor(self.LIGHT_BG)),
            ('BACKGROUND', (0, -1), (-1, -1), colors.HexColor(self.HEADER_COLOR)),  # Total amount highlighted
            ('TEXTCOLOR', (0, -1), (-1, -1), colors.whitesmoke),
            ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor(self.BORDER_COLOR)),
            ('ALIGN', (1, 0), (1, -1), 'RIGHT'),
            ('LEFTPADDING', (0, 0), (-1, -1), 8),
            ('RIGHTPADDING', (0, 0), (-1, -1), 8),
            ('TOPPADDING', (0, 0), (-1, -1), 6),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
        ]))
        elements.append(totals_table)
        elements.append(Spacer(1, 10))

        # Amount in words
        total_amount = float(invoice_data.get('total_amount', 0))
        amount_words = self._number_to_words(total_amount)
        
        words_data = [[f"Amount in Words: {amount_words}"]]
        words_table = Table(words_data, colWidths=[460])
        words_table.setStyle(TableStyle([
            ('FONTSIZE', (0, 0), (-1, -1), 10),
            ('FONTNAME', (0, 0), (-1, -1), 'Helvetica-Oblique'),
            ('BACKGROUND', (0, 0), (-1, -1), colors.HexColor(self.LIGHT_BG)),
            ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor(self.BORDER_COLOR)),
            ('LEFTPADDING', (0, 0), (-1, -1), 8),
            ('RIGHTPADDING', (0, 0), (-1, -1), 8),
            ('TOPPADDING', (0, 0), (-1, -1), 8),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 8),
        ]))
        elements.append(words_table)
        elements.append(Spacer(1, 20))

        # Footer
        footer_data = [["This is a computer-generated invoice."]]
        footer_table = Table(footer_data, colWidths=[460])
        footer_table.setStyle(TableStyle([
            ('FONTSIZE', (0, 0), (-1, -1), 8),
            ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
            ('TEXTCOLOR', (0, 0), (-1, -1), colors.HexColor(self.ACCENT_COLOR)),
            ('FONTNAME', (0, 0), (-1, -1), 'Helvetica-Oblique'),
        ]))
        elements.append(footer_table)

        # Build PDF
        doc.build(elements)
        buffer.seek(0)
        return buffer


__all__ = ["InvoicePDFGenerator"]
from decimal import Decimal
from typing import Dict, Any

class PayoutCalculator:
    """
    Calculates rider payouts based on performance and rate cards.
    Currently uses a flat rate of 50 INR per delivered order.
    Future: Will integrate slab-based and salary-based structures.
    """
    
    DEFAULT_RATE_PER_ORDER = Decimal('50.00')
    
    @classmethod
    def calculate_order_payout(cls, order: Any) -> Decimal:
        """
        Calculate the payout for a single delivered order.
        """
        # For now, it's a flat rate
        return cls.DEFAULT_RATE_PER_ORDER

    @classmethod
    def calculate_period_payout(cls, delivered_orders_count: int, distance_km: float = 0.0) -> Dict[str, Any]:
        """
        Calculate payout for a period (e.g., daily or weekly).
        Returns a dictionary with the breakdown.
        """
        base_earnings = delivered_orders_count * cls.DEFAULT_RATE_PER_ORDER
        
        return {
            "delivered_orders": delivered_orders_count,
            "base_earnings": base_earnings,
            "distance_earnings": Decimal('0.00'), # Placeholder for future
            "incentive_earnings": Decimal('0.00'), # Placeholder for future
            "total_earnings": base_earnings,
            "calculation_method": "flat_rate_50"
        }

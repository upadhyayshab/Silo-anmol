"""The only place that imports a concrete telephony adapter.

`get_telephony_provider()` returns the real `ExotelAdapter` when creds are
configured, else the `MockTelephonyProvider` so routes/tests work without
billing live calls. Routes depend on the abstract port, never on Exotel.
"""
from functools import lru_cache

from config import get_settings
from core.telephony import MockTelephonyProvider, TelephonyProvider


@lru_cache
def get_telephony_provider() -> TelephonyProvider:
    s = get_settings()
    if not (s.exotel_sid and s.exotel_api_key and s.exotel_api_token):
        return MockTelephonyProvider()
    from adapters.exotel import ExotelAdapter  # imported only when configured
    return ExotelAdapter(
        sid=s.exotel_sid,
        api_key=s.exotel_api_key,
        api_token=s.exotel_api_token,
        subdomain=s.exotel_subdomain,
        ccm_subdomain=s.exotel_ccm_subdomain,
        crm_flow_id=s.exotel_crm_flow_id,
        email_overrides=s.exotel_email_overrides,
        sip_map=s.exotel_sip_map,
        app_id=s.exotel_app_id,
        app_secret=s.exotel_app_secret,
        app_entity=s.exotel_app_entity,
        integrations_host=s.exotel_integrations_host,
    )

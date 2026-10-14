"""Unit tests for AgroInternalService — mocks httpx entirely."""
import sys
from pathlib import Path
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))


class TestAgroInternalService(IsolatedAsyncioTestCase):
    @patch("services.agroInternalService.httpx.AsyncClient")
    async def test_returns_farm_dict_on_200(self, mock_client_cls):
        from services.agroInternalService import AgroInternalService

        mock_response = MagicMock(status_code=200)
        mock_response.json.return_value = {"id": "farm-1", "center": [77.7, 13.1]}
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client_cls.return_value.__aenter__.return_value = mock_client

        service = AgroInternalService(base_url="http://agromonitoring-svc:8001", shared_secret="s3cret")
        result = await service.get_farm("farm-1", "usr_1")

        self.assertEqual(result["id"], "farm-1")
        call_kwargs = mock_client.get.call_args.kwargs
        self.assertEqual(call_kwargs["headers"]["X-Internal-Service"], "crop-recommendation")
        self.assertEqual(call_kwargs["headers"]["X-Internal-Secret"], "s3cret")
        self.assertEqual(call_kwargs["headers"]["X-User-Id"], "usr_1")

    @patch("services.agroInternalService.httpx.AsyncClient")
    async def test_returns_none_on_404(self, mock_client_cls):
        from services.agroInternalService import AgroInternalService

        mock_response = MagicMock(status_code=404)
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client_cls.return_value.__aenter__.return_value = mock_client

        service = AgroInternalService(base_url="http://agromonitoring-svc:8001", shared_secret="s3cret")
        result = await service.get_farm("nonexistent", "usr_1")

        self.assertIsNone(result)

    @patch("services.agroInternalService.httpx.AsyncClient")
    async def test_returns_none_on_network_error_not_raises(self, mock_client_cls):
        from services.agroInternalService import AgroInternalService
        import httpx

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=httpx.TimeoutException("timed out"))
        mock_client_cls.return_value.__aenter__.return_value = mock_client

        service = AgroInternalService(base_url="http://agromonitoring-svc:8001", shared_secret="s3cret")
        result = await service.get_farm("farm-1", "usr_1")

        self.assertIsNone(result)

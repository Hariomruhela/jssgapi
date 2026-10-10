from __future__ import annotations

from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from app.main import app


def test_translate_en_to_hi():
    mock_client = MagicMock()
    mock_response = MagicMock()
    mock_translation = MagicMock()
    mock_translation.translated_text = "गेहूं एक महत्वपूर्ण फसल है।"
    mock_translation.detected_language_code = "en"
    mock_response.translations = [mock_translation]
    mock_client.translate_text.return_value = mock_response

    with patch("app.services.translate_service._make_client", return_value=mock_client), TestClient(app) as client:
        response = client.post(
            "/api/translate",
            json={"text": "Wheat is an important crop.", "target_language": "hi"},
        )
    assert response.status_code == 200
    body = response.json()
    assert body["originalText"] == "Wheat is an important crop."
    assert body["translatedText"] == "गेहूं एक महत्वपूर्ण फसल है।"
    assert body["targetLanguage"] == "hi"


def test_translate_hi_to_en():
    mock_client = MagicMock()
    mock_response = MagicMock()
    mock_translation = MagicMock()
    mock_translation.translated_text = "Wheat is an important crop."
    mock_response.translations = [mock_translation]
    mock_client.translate_text.return_value = mock_response

    with patch("app.services.translate_service._make_client", return_value=mock_client), TestClient(app) as client:
        response = client.post(
            "/api/translate",
            json={"text": "गेहूं एक महत्वपूर्ण फसल है।", "target_language": "en", "source_language": "hi"},
        )
    assert response.status_code == 200
    body = response.json()
    assert body["targetLanguage"] == "en"


def test_translate_empty_input():
    with TestClient(app) as client:
        response = client.post("/api/translate", json={"text": "   ", "target_language": "hi"})
    assert response.status_code == 400


def test_translate_too_long():
    long_text = "a" * 5001
    with TestClient(app) as client:
        response = client.post("/api/translate", json={"text": long_text, "target_language": "hi"})
    assert response.status_code == 422  # pydantic validation catches > max_length


def test_translate_missing_config():
    from app.services import translate_service

    def raise_exc():
        raise translate_service.ServiceUnavailableException("Translation service is not configured")

    with patch("app.services.translate_service._make_client", side_effect=raise_exc), TestClient(app) as client:
        response = client.post("/api/translate", json={"text": "Hello", "target_language": "hi"})
    assert response.status_code == 503


def test_translate_api_failure():
    from app.services import translate_service

    def raise_exc(*args, **kwargs):
        raise translate_service.ServiceUnavailableException("Translation service failed")

    with patch("app.services.translate_service._translate_text", side_effect=raise_exc), TestClient(app) as client:
        response = client.post("/api/translate", json={"text": "Hello", "target_language": "hi"})
    assert response.status_code == 503


def test_existing_apis_unaffected():
    with TestClient(app) as client:
        response = client.get("/health")
    assert response.status_code == 200

"""Flexible 图片调用协议与响应处理测试。"""

import base64

import httpx
import pytest

from gsuid_core.plugins.Flexible.Flexible.core.utils import (
    build_payload,
    resolve_image_mode,
    resolve_chat_api_url,
    resolve_images_api_url,
    build_images_edit_request,
    build_images_generation_payload,
    extract_image_url_from_response,
)
from gsuid_core.plugins.Flexible.Flexible.image.service import ImageService
from gsuid_core.plugins.Flexible.Flexible.domain.providers import load_image_models


def test_legacy_model_config_defaults_to_auto():
    models = load_image_models(
        {
            "image_models": [
                {
                    "id": "gpt",
                    "model_name": "gpt-image-2",
                    "cost": 0.24,
                },
            ],
        }
    )

    assert models["gpt"]["mode"] == "auto"
    assert resolve_image_mode(models["gpt"]["model_name"], models["gpt"]["mode"]) == "images_api"


def test_auto_mode_keeps_non_gpt_image_models_on_chat():
    assert resolve_image_mode("gemini-3.1-flash-image", "auto") == "chat_completions"
    assert resolve_image_mode("gpt-image-2", "auto") == "images_api"
    assert resolve_image_mode("gpt-image-2", "chat_completions") == "chat_completions"


def test_chat_payload_remains_compatible():
    payload = build_payload("gemini-image", "测试", ["YWJj"], 1000)

    assert payload == {
        "model": "gemini-image",
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "测试"},
                    {
                        "type": "image_url",
                        "image_url": {"url": "data:image/png;base64,YWJj"},
                    },
                ],
            },
        ],
        "max_tokens": 1000,
        "stream": False,
    }


@pytest.mark.parametrize(
    ("configured_url", "expected"),
    [
        ("https://botcf.com/v1", "https://botcf.com/v1/chat/completions"),
        ("https://botcf.com/v1/", "https://botcf.com/v1/chat/completions"),
        ("https://botcf.com/v1/chat/completions", "https://botcf.com/v1/chat/completions"),
        ("https://botcf.com/v1/images/edits", "https://botcf.com/v1/chat/completions"),
    ],
)
def test_chat_url_normalization(configured_url, expected):
    assert resolve_chat_api_url(configured_url) == expected


@pytest.mark.parametrize(
    ("configured_url", "has_images", "expected"),
    [
        ("https://botcf.com/v1", False, "https://botcf.com/v1/images/generations"),
        ("https://botcf.com/v1/", True, "https://botcf.com/v1/images/edits"),
        (
            "https://botcf.com/v1/chat/completions",
            False,
            "https://botcf.com/v1/images/generations",
        ),
        (
            "https://botcf.com/v1/images/generations/",
            True,
            "https://botcf.com/v1/images/edits",
        ),
    ],
)
def test_images_url_normalization(configured_url, has_images, expected):
    assert resolve_images_api_url(configured_url, has_images) == expected


def test_images_generation_payload():
    assert build_images_generation_payload("gpt-image-2", "画一只猫") == {
        "model": "gpt-image-2",
        "prompt": "画一只猫",
    }


def test_images_edit_request_supports_multiple_images():
    first = base64.b64encode(b"first").decode()
    second = base64.b64encode(b"second").decode()

    data, files = build_images_edit_request("gpt-image-2", "修改图片", [first, second])

    assert data == {"model": "gpt-image-2", "prompt": "修改图片"}
    assert [part[0] for part in files] == ["image", "image"]
    assert files[0][1] == ("image_1.png", b"first", "image/png")
    assert files[1][1] == ("image_2.png", b"second", "image/png")


def test_images_response_parsing_supports_url_and_base64():
    url, error = extract_image_url_from_response(
        {
            "data": [{"url": "https://example.com/image.png"}],
        }
    )
    assert url == "https://example.com/image.png"
    assert error is None

    url, error = extract_image_url_from_response(
        {
            "data": [{"b64_json": "YWJj"}],
        }
    )
    assert url == "data:image/png;base64,YWJj"
    assert error is None


def test_chat_response_parsing_remains_compatible():
    url, error = extract_image_url_from_response(
        {
            "choices": [
                {
                    "message": {
                        "content": "已完成 ![image](https://example.com/chat-image.png)",
                    },
                },
            ],
        }
    )

    assert url == "https://example.com/chat-image.png"
    assert error is None


def _response(status_code: int, content: bytes, content_type: str) -> httpx.Response:
    return httpx.Response(
        status_code,
        content=content,
        headers={"Content-Type": content_type},
        request=httpx.Request("POST", "https://botcf.com/v1/images/generations"),
    )


def test_empty_response_has_readable_error():
    with pytest.raises(RuntimeError, match="API 返回空响应（HTTP 307）"):
        ImageService._parse_json_response(_response(307, b"", "application/json"))


def test_html_response_has_readable_error():
    with pytest.raises(RuntimeError, match="API 返回了非 JSON 内容（HTTP 502"):
        ImageService._parse_json_response(
            _response(502, b"<html>bad gateway</html>", "text/html")
        )


def test_json_without_content_type_is_still_accepted():
    response = _response(
        200,
        b'{"data":[{"url":"https://example.com/image.png"}]}',
        "",
    )

    assert ImageService._parse_json_response(response)["data"][0]["url"].endswith("image.png")


def test_json_api_error_message_is_preserved():
    response = _response(
        401,
        b'{"error":{"message":"Invalid token"}}',
        "application/json",
    )
    data = ImageService._parse_json_response(response)

    assert ImageService._get_api_error_message(data, response.status_code) == "Invalid token"

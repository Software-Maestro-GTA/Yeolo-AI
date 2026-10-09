import json
import logging

import pytest
from fastapi import HTTPException, Request
from httpx import ASGITransport, AsyncClient

from app.core.logger import setup_logging
from app.main import app, http_exception_handler, unhandled_exception_handler


def test_setup_logging_initialization(mocker):
    """setup_logging() 함수가 로거 및 핸들러를 올바르게 초기화하는지 검증"""
    mocker.patch("sys.stdout")
    setup_logging()
    
    root_logger = logging.getLogger()
    assert len(root_logger.handlers) >= 1
    assert any(isinstance(h, logging.StreamHandler) for h in root_logger.handlers)


@pytest.mark.asyncio
async def test_logging_middleware_and_healthcheck(caplog):
    """HTTP 요청 로깅 미들웨어가 헬스체크 이외의 요청을 잘 기록하는지 검증"""
    caplog.set_level(logging.INFO)
    transport = ASGITransport(app=app)
    
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # 헬스체크 엔드포인트 호출
        res_health = await client.get("/health")
        assert res_health.status_code == 200

        # 잘못된 엔드포인트 호출 (미들웨어 로깅 대상)
        res_404 = await client.get("/non-existent-path")
        assert res_404.status_code == 404

    # 404 요청에 대한 미들웨어 로그가 존재하는지 확인
    assert any("Status 404" in record.message for record in caplog.records)


@pytest.mark.asyncio
async def test_unhandled_exception_logging(caplog, monkeypatch):
    """처리되지 않은 예외 발생 시 unhandled_exception_handler가 스택 트레이스를 포함하여 로깅하는지 검증"""
    caplog.set_level(logging.ERROR)
    monkeypatch.setattr(app.router, 'routes', list(app.router.routes))
    
    # 임시 핸들러 등록으로 unhandled exception 유발
    @app.get("/test-error-endpoint")
    def trigger_error():
        raise RuntimeError("Test Fatal Error")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/test-error-endpoint")
        assert response.status_code == 500
        assert response.json()["status"] == 500
        assert response.json()["message"] == "서버 내부 오류가 발생했습니다."
        assert "Test Fatal Error" not in response.text

    # ERROR 로그에 "Unhandled Exception" 및 스택 트레이스 정보가 있는지 확인
    error_records = [r for r in caplog.records if r.levelname == "ERROR"]
    assert len(error_records) >= 1
    assert any("Unhandled Exception" in r.message for r in error_records)
    assert any("Test Fatal Error" in r.message for r in error_records)


@pytest.mark.asyncio
@pytest.mark.parametrize('handler,error,expected_status', [
    (unhandled_exception_handler, RuntimeError('private-provider-detail'), 500),
    (http_exception_handler, HTTPException(status_code=502, detail='private-provider-detail'), 502),
])
async def test_global_server_error_handlers_hide_internal_details(handler, error, expected_status):
    request = Request({'type': 'http', 'method': 'GET', 'path': '/test', 'headers': []})
    response = await handler(request, error)
    assert response.status_code == expected_status
    assert json.loads(response.body)['message'] == '서버 내부 오류가 발생했습니다.'
    assert b'private-provider-detail' not in response.body

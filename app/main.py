import logging
import time
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from app.api.taste_profile import router as taste_profile_router
from app.api.course import router as course_router
from app.core.logger import setup_logging

# 로깅 환경 초기화
setup_logging()
logger = logging.getLogger(__name__)

app = FastAPI(title="Yeolo AI Service", version="1.0.0")


@app.middleware("http")
async def log_requests(request: Request, call_next):
    """모든 HTTP 요청과 응답의 처리 시간 및 상태 코드를 로깅하고 예외 발생 시 수집하는 미들웨어"""
    start_time = time.time()
    try:
        response = await call_next(request)
        process_time_ms = (time.time() - start_time) * 1000

        # 헬스체크 로그 생략 (옵션: 필요시 주석 해제 가능)
        if request.url.path not in ("/health", "/healthz", "/"):
            logger.info(
                f"[{request.method}] {request.url.path} -> Status {response.status_code} ({process_time_ms:.2f}ms)"
            )
        return response
    except Exception as exc:
        process_time_ms = (time.time() - start_time) * 1000
        logger.error(
            f"Unhandled Exception on [{request.method}] {request.url.path} ({process_time_ms:.2f}ms): {str(exc)}",
            exc_info=True,
        )
        return JSONResponse(
            status_code=500,
            content={
                "status": 500,
                "message": f"서버 내부 오류가 발생했습니다: {str(exc)}",
            },
        )



@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    # 코스 생성 API 엔드포인트 요청 시의 validation error 문구 처리
    if "/courses" in request.url.path:
        message = "코스 생성 조건이 올바르지 않습니다."
    else:
        message = "전처리 메타데이터 부족/형식 오류"

    logger.warning(
        f"Validation Error on [{request.method}] {request.url.path}: {exc.errors()} -> Returning 400"
    )

    return JSONResponse(
        status_code=400,
        content={
            "status": 400,
            "message": message,
        },
    )


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    if exc.status_code >= 500:
        logger.error(
            f"HTTPException {exc.status_code} on [{request.method}] {request.url.path}: {exc.detail}"
        )
    else:
        logger.warning(
            f"HTTPException {exc.status_code} on [{request.method}] {request.url.path}: {exc.detail}"
        )

    return JSONResponse(
        status_code=exc.status_code,
        content={
            "status": exc.status_code,
            "message": exc.detail,
        },
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    """처리되지 않은 모든 서버 내부 예외에 대해 스택 트레이스를 기록하는 로깅 핸들러"""
    logger.error(
        f"Unhandled Exception on [{request.method}] {request.url.path}: {str(exc)}",
        exc_info=True,
    )

    return JSONResponse(
        status_code=500,
        content={
            "status": 500,
            "message": f"서버 내부 오류가 발생했습니다: {str(exc)}",
        },
    )


app.include_router(taste_profile_router)
app.include_router(course_router)


@app.get("/health")
@app.get("/healthz")
@app.get("/")
def health_check():
    return {"status": "ok", "message": "Healthy"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.main:app", port=8000, reload=True)


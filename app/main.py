"""
@file main.py
@description FastAPI 애플리케이션 진입점 및 예외 처리 라우터 설정 모듈
@requirements REQ-7, REQ-11
@functional FUN-1, FUN-2
@api API-BA-1, API-BA-6
@author Antigravity Agent
"""

from fastapi import FastAPI, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from app.api.taste_profile import router as taste_profile_router
from app.api.course import router as course_router

app = FastAPI()


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request, exc):
    # 코스 생성 API 엔드포인트 요청 시의 validation error 문구 처리
    if "/courses" in request.url.path:
        message = "코스 생성 조건이 올바르지 않습니다."
    else:
        message = "전처리 메타데이터 부족/형식 오류"

    return JSONResponse(
        status_code=400,
        content={
            "status": 400,
            "message": message,
        },
    )


@app.exception_handler(HTTPException)
async def http_exception_handler(request, exc: HTTPException):
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "status": exc.status_code,
            "message": exc.detail,
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

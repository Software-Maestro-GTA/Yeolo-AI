from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from app.api.taste_profile import router as taste_profile_router

app = FastAPI()

@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request, exc):
    return JSONResponse(
        status_code=400,
        content={
            "status": 400,
            "message": "전처리 메타데이터 부족/형식 오류"
        }
    )

app.include_router(taste_profile_router)

@app.get("/")
def read_root():
    return {"message": "Hello world!"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app", port=8000, reload=True)

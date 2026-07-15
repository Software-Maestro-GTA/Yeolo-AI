#!/bin/bash

# 실행 경로 기준 설정
DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
ROOT_DIR="$DIR/../.."

cd "$ROOT_DIR" || exit 1

if [ -z "$1" ]; then
    echo "Usage: bash run_test.sh [ai-server]"
    exit 1
fi

AREA="$1"

echo ">> [검증 시작] 대상 영역: $AREA"

case "$AREA" in
    "ai-server"|"backend"|"ai")
        # 1. ruff를 통한 정적 분석 및 린트 검사
        echo ">> Running uv run ruff check..."
        uv run ruff check .
        LINT_CODE=$?
        if [ $LINT_CODE -ne 0 ]; then
            echo "Error: Lint check failed (ruff check)."
            exit $LINT_CODE
        fi

        # 2. pytest를 통한 단위/통합 테스트 검사
        echo ">> Running uv run pytest..."
        uv run pytest
        TEST_CODE=$?
        if [ $TEST_CODE -ne 0 ]; then
            echo "Error: Tests failed (pytest)."
            exit $TEST_CODE
        fi

        echo ">> [검증 완료] 모든 테스트 및 린트를 통과했습니다!"
        exit 0
        ;;
    *)
        echo "Warning: 알 수 없는 영역 '$AREA'. 기본 검증을 성공으로 간주하고 건너뜁니다."
        exit 0
        ;;
esac

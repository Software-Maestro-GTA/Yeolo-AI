#!/bin/bash

# 하네스 초기화 스크립트 (init.sh)
# Planner 구동 전 일관된 진척 시트 환경 세팅 및 로그 초기화를 전담합니다.

set -e

# 스크립트가 위치한 hooks 디렉토리를 기준으로 하네스 코어 루트 탐색
WORKSPACE_DIR="$(cd "$(dirname "$0")/.." && pwd)"

TEMPLATE_PATH="$WORKSPACE_DIR/templates/progress_template.md"
TARGET_PROGRESS_PATH="$WORKSPACE_DIR/../progress.md"
TARGET_LOG_PATH="$WORKSPACE_DIR/../log.md"

echo "🔄 하네스 일관성 초기화 및 백로그 보드 리셋을 시작합니다..."

# 1. progress_template.md 존재 여부 확인 후 복사
if [ -f "$TEMPLATE_PATH" ]; then
    cp "$TEMPLATE_PATH" "$TARGET_PROGRESS_PATH"
    echo "✅ progress.md 보드가 templates/progress_template.md 내용으로 초기화되었습니다."
else
    echo "❌ 에러: progress_template.md 파일을 찾을 수 없습니다. ($TEMPLATE_PATH)"
    exit 1
fi

# 2. log.md 실행 로그 마크다운 문서 초기화
echo "# Harness Execution Log (하네스 실행 로그)" > "$TARGET_LOG_PATH"
echo "🧹 log.md 실행 기록 문서가 초기화되었습니다."

echo "🎉 하네스 개발 환경 일관성 초기화 작업 완료!"

#!/bin/bash

# 하네스 검증 및 테스트 스크립트 (test.sh)
# 구현 완료 자체 검증 및 Reviewer의 통합 무결성 빌드 검사를 자동으로 수행합니다.

set -o pipefail

# 스크립트가 위치한 hooks 디렉토리를 기준으로 하네스 코어 루트 탐색
WORKSPACE_DIR="$(cd "$(dirname "$0")/.." && pwd)"
LOG_FILE="$WORKSPACE_DIR/../log.md"

echo "🧪 하네스 무결성 검증을 시작합니다..."
echo "=============================================="
echo -e "\n## 검증 세션 시작 시각: $(date '+%Y-%m-%d %H:%M:%S')" >> "$LOG_FILE"

# 1. Linter 검사 (Ruff를 통한 정적 분석)
echo "🔍 1. 정적 분석 검사 (Ruff Linter) 구동 중..."
LINT_TMP=$(mktemp)
if uv run ruff check . > "$LINT_TMP" 2>&1; then
    echo "✅ [LINT] PASS - 코드 스타일에 위반 사항이 없습니다."
    echo "- **LINT**: PASS" >> "$LOG_FILE"
else
    echo -e "\n### ❌ [LINT] FAIL - 린트 위반 사항 발생\n\`\`\`text" >> "$LOG_FILE"
    cat "$LINT_TMP" >> "$LOG_FILE"
    echo -e "\`\`\`" >> "$LOG_FILE"
    rm -f "$LINT_TMP"
    echo "❌ [LINT] FAIL - 린트 위반 사항이 발견되었습니다. log.md를 참조하세요."
    exit 1
fi
rm -f "$LINT_TMP"

# 2. 테스트 스크립트 검사 (pytest를 통한 테스트 무결성)
echo "🔍 2. 단위 및 통합 테스트 (pytest) 구동 중..."
TEST_TMP=$(mktemp)
if uv run pytest > "$TEST_TMP" 2>&1; then
    echo "✅ [TEST] PASS - 모든 유닛/통합 테스트 케이스가 통과했습니다."
    echo "- **TEST**: PASS" >> "$LOG_FILE"
else
    echo -e "\n### ❌ [TEST] FAIL - 단위 및 통합 테스트 실패\n\`\`\`text" >> "$LOG_FILE"
    cat "$TEST_TMP" >> "$LOG_FILE"
    echo -e "\`\`\`" >> "$LOG_FILE"
    rm -f "$TEST_TMP"
    echo "❌ [TEST] FAIL - 일부 테스트 케이스가 실패했습니다. log.md를 참조하세요."
    exit 1
fi
rm -f "$TEST_TMP"

echo "=============================================="
echo -e "\n### 🎉 통합 검증 PASS - $(date '+%Y-%m-%d %H:%M:%S')\n- 모든 검증(LINT/TEST)을 완벽히 통과했습니다! 통합이 승인되었습니다." >> "$LOG_FILE"
exit 0

# Yeolo-AI CI/CD 구축 가이드 (AI 담당자용)

> 작성: BE 파트, 2026-08-06. BE(Yeolo-BE)의 prod/dev 이원화 파이프라인을 먼저 구축·운영하며
> 얻은 결론을 AI 서버(Yeolo-AI)에 그대로 적용할 수 있도록 정리한 문서입니다.
> 참고 구현체: `Yeolo-BE/.github/workflows/deploy.yml` (이 문서의 모든 패턴이 실제로 동작 중).

## 0. 왜 지금 필요한가

- **dev 네임스페이스(`app-dev`)의 `ai` Deployment가 `yeolo/ai:bootstrap` 태그로
  ImagePullBackOff 상태**입니다. 인프라가 자리만 만들어둔 것으로, Yeolo-AI에 dev 배포
  파이프라인이 없어서 실제 이미지가 한 번도 배포되지 않았습니다.
- dev BE는 이미 떠 있고 `http://ai.app-dev.svc.cluster.local:80` 으로 AI를 호출하도록
  설정돼 있습니다(`AI_COURSE_PROVIDER=internal`). **dev AI가 뜨기 전까지 dev 환경의
  코스 생성/성향 분석은 전부 실패합니다.**
- 2026-08에 GitHub Environment 이름이 정리됐습니다(과거: prod=`dev`, dev=`dev-app` →
  현재: `prod`/`dev`). 현재 Yeolo-AI의 deploy.yml은 main 배포 job에 **구식 이름
  `environment: dev`** 를 쓰고 있어, 인프라의 OIDC trust가 새 이름으로 바뀌는 순간
  **main 배포가 AssumeRole 단계에서 끊깁니다.** (BE는 같은 이유로 워크플로를 정리했습니다.)

## 1. 목표 구조 (BE와 동일)

| | prod | dev |
| :-- | :-- | :-- |
| 트리거 브랜치 | `main` push | `dev` push |
| GitHub Environment | `prod` | `dev` |
| OIDC Role (repo Variables에 이미 존재) | `AWS_ROLE_ARN` (`…/yeolo-dev-gha-ai`) | `AWS_ROLE_ARN_DEV` (`…/yeolo-dev-gha-ai-dev`) |
| K8s 네임스페이스 | `app` (repo var `K8S_NAMESPACE`) | `app-dev` (job에서 리터럴로 덮어쓰기) |
| 클러스터 | `yeolo-dev` 공용 (네임스페이스로만 분리) | 〃 |

- OIDC trust는 role별로 sub 두 형태만 허용합니다(인프라 `github-oidc.tf`):
  `ref:refs/heads/<branch>` (build job, environment 미지정) 와 `environment:<name>` (deploy job).
  **Environment 이름은 OIDC sub에 그대로 박히므로 인프라와 락스텝**입니다. 임의로 바꾸지 마세요.
- 빌드는 두 브랜치가 공유: environment 없이 ref sub로 나가고, 브랜치에 맞는 role만 고르면
  됩니다. ECR 리포(`yeolo/ai`)는 공용, 태그는 immutable git SHA — 섞이지 않습니다.
- **arm64 필수**: EKS 노드가 Graviton입니다. `runs-on: ubuntu-24.04-arm` 러너에서
  빌드하세요. amd64로 빌드하면 `exec format error`로 CrashLoopBackOff 납니다.

## 2. GitHub 설정 현황과 해야 할 일

2026-08-06 기준 Yeolo-AI 레포 실측:

| 항목 | 현황 | 해야 할 일 |
| :-- | :-- | :-- |
| repo Variables | `AWS_ROLE_ARN`, `AWS_ROLE_ARN_DEV`, `ECR_REPOSITORY`, `EKS_CLUSTER`, `K8S_NAMESPACE=app`, `DEPLOYMENT_NAME=ai` 등 준비됨 | 환경별로 달라야 하는 값(`LOG_LEVEL`, `LANGSMITH_PROJECT` 등)은 repo가 아닌 **Environment variables로 이동** |
| Environments | `prod`, `dev` 생성돼 있음 | — |
| Environment(prod) secrets/variables | **비어 있음** | `GEMINI_API_KEY`, `INTERNAL_API_KEY` 등 전부 등록 필요 (§4) |
| Environment(dev) secrets | `INTERNAL_API_KEY`만 있음 | 나머지 키 등록 필요 (§4) |
| repo secrets | 없음 | (그대로 두세요 — 비밀은 Environment 스코프에) |

> 현재 prod ai 파드가 쓰는 `ai-secrets`는 과거 배포 때 만들어진 것입니다. 위 상태에서
> 워크플로만 고쳐 재배포하면 **빈 값으로 덮여 키가 사라질 수 있으니**, prod Environment
> secrets를 먼저 채운 뒤 배포하세요.

## 3. deploy.yml 수정 사항

`Yeolo-BE/.github/workflows/deploy.yml` 을 열어 구조를 그대로 가져오는 것을 권장합니다.
차이점은 이름(`was`→`ai`, `was-secrets`→`ai-env`/`ai-secrets`)뿐입니다. 요점:

1. **트리거**: `branches: [main]` → `[main, dev]`
2. **build job**: 브랜치별 role 선택 스텝 추가 (BE의 `Resolve OIDC role` 참고 — 값이 비면
   fallback으로 흘리지 말고 즉시 실패시켜서 "role 변수 미설정"이 에러 메시지에 드러나게)
3. **기존 deploy job**: `environment: dev` → **`environment: prod`** (★ 가장 급한 수정)
4. **deploy-dev job 신설**: 기존 deploy job 복사 후
   - `if: github.ref == 'refs/heads/dev'`, `environment: dev`
   - `role-to-assume: ${{ vars.AWS_ROLE_ARN_DEV }}`
   - job env에 `K8S_NAMESPACE: app-dev` 리터럴 (repo var `app`을 덮어씀 — 실제 격리 경계)
5. **기본값 제거**: 현재 `${{ vars.X || '기본값' }}` 패턴이 많은데 **전부 제거하세요.**
   - BE에서 실제로 겪은 사고: repo 레벨에 dev용 값이 있으면 `||` 기본값이 무력화되고
     prod까지 상속돼, prod가 잘못된 값으로 "그럴듯하게" 배포됩니다. BE는 이걸로 prod가
     stub 모드·스키마 자동변경 상태로 운영되고 있었습니다.
   - 원칙: **기본값의 단일 출처는 앱**(앱 설정 파일). 값이 비면 env에서 키를 빼고,
     앱 기본값이 클러스터에서 위험한 키만 "없으면 배포 중단" 가드를 두세요(BE의
     `missing_vars` 가드 참고).
   - 같은 이유로 **환경별로 달라야 하는 값을 repo Variables에 두지 마세요.**
     (environment > repository 우선순위 상속이 조용한 오염 경로가 됩니다.)
6. **롤백 조건 좁히기**: `if: failure()` → `if: failure() && steps.set_image.outcome == 'success'`
   (`Set image` 스텝에 `id: set_image` 부여). 시크릿 sync 실패처럼 클러스터를 건드리기
   전의 실패에서 `rollout undo`가 돌면 멀쩡한 리비전이 뒤로 밀립니다.
7. **필수 키 가드**: env-file 생성 후 필수 시크릿(`GEMINI_API_KEY`, `INTERNAL_API_KEY` 등)이
   비었으면 즉시 `exit 1`. 없으면 파드가 빈 설정으로 기동 실패하고 원인이 "rollout 5분
   타임아웃"으로만 드러납니다(BE #27 인시던트).
8. **주의 — 같은 SHA 재배포**: `set image`는 태그가 같으면 no-op이라 ConfigMap/Secret만
   바뀐 경우 파드가 재시작되지 않습니다. 설정만 바꿔 반영하려면
   `kubectl rollout restart deployment/ai` 스텝이 별도로 필요합니다(또는 수동 실행).

## 4. 환경별 값 체크리스트

Environment secrets (비밀):

| 키 | prod | dev | 비고 |
| :-- | :-- | :-- | :-- |
| `INTERNAL_API_KEY` | 필수 | 필수(있음) | ★ **BE와 짝**: prod AI ↔ prod BE, dev AI ↔ dev BE가 서로 같은 값이어야 인증 통과. BE는 X-Internal-Api-Key 헤더로 보냅니다. |
| `GEMINI_API_KEY` | 필수 | 필수 | dev는 별도 키/쿼터 권장 |
| `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` | 사용 시 | 사용 시 | |
| `LANGSMITH_API_KEY` | 사용 시 | 사용 시 | 워크플로의 `LANGCHAIN_API_KEY` 이중화(`\|\|`)도 한 키로 정리 권장 |

Environment variables (비밀 아님):

| 키 | prod 권장 | dev 권장 |
| :-- | :-- | :-- |
| `LOG_LEVEL` | `INFO` | `DEBUG` |
| `LANGSMITH_PROJECT` | `YEOLO` | `YEOLO-dev` (트레이스 분리) |
| `GEMINI_MODEL_NAME`, `*_TIMEOUT_SECONDS` 등 | 값이 같다면 repo Variables 유지 가능 | 〃 |

## 5. 배포 후 확인

```bash
# dev 파드가 bootstrap에서 실제 SHA 이미지로 교체됐는지
kubectl -n app-dev get pods -l app=ai
kubectl -n app-dev get deploy ai -o jsonpath='{.spec.template.spec.containers[0].image}'

# 헬스 (Deployment probe가 이미 :8000 /health 를 보고 있음 — 1/1 Ready면 통과)
kubectl -n app-dev logs deploy/ai --tail=50
```

BE와의 연동 확인은 BE 파트에 요청하세요 — dev BE에서 코스 생성 SSE를 호출해
`ai.app-dev` 경유가 정상인지 함께 확인하겠습니다.

## 6. 클러스터 측 참고값 (인프라가 이미 만들어 둔 것)

- Service: `ai` (80 → 컨테이너 8000), 네임스페이스별 각 1개
- Deployment: `ai`, envFrom = ConfigMap `ai-env`(optional) + Secret `ai-secrets`(optional)
  → 워크플로가 이 두 리소스를 만들어 주입 (BE의 `was-secrets` sync 패턴과 동일)
- readiness probe: `GET :8000/health`
- BE가 호출하는 주소: prod `http://ai.app.svc.cluster.local:80`,
  dev `http://ai.app-dev.svc.cluster.local:80`

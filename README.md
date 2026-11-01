# Bad Apple Earth

Bad Apple!!의 각 프레임과 형태·명암이 닮은 **실제 위성·항공사진 한 장**을 찾아 영상을 재구성하는 로컬 제작 도구다. 그림을 새로 생성하거나 여러 장소를 합성하지 않고, 실제 지구에서 촬영된 장면 자체를 검색한다.

> **현재 상태: 수집·검색·10초 무음 렌더 파이프라인 구현.** uv 환경에서 MP4 목표 프레임 추출, Sentinel-2 수집·중단 재개·명암 특징 저장, MSE 검색과 실제 사진 MP4 출력을 실행할 수 있다. 실제 수집 규모·산출물·남은 작업은 [PLAN](docs/PLAN.md)에서 관리한다.

## 주요 기능과 목표

구현 목표는 공개 영상 데이터의 수집·전처리·검색·재추출·MP4 렌더링을 독립적으로 반복 실행하는 파이프라인이다. 데이터셋은 가용 자원·접근 조건 안에서 최대한 넓고 다양하게 구축한다. 먼저 소량 실제 데이터와 1만 후보로 기술적 동작을 확인하고 10초 기준 결과를 만든다. 소규모에서의 시각적 성공은 확대의 선행 조건이 아니며, 닮은 장면을 찾을 기회를 넓히도록 이후 규모를 늘린다. 개별 프레임의 장소가 계속 달라지는 것은 허용하며, 캐릭터·배경의 연속성은 목표가 아니다.

최초 비교 결과는 컬러 사진 버전을 기준으로 한다. 흑백·원본과 나란히 보기·명암 특징 비교도 평가에 사용한다. 기본 결과의 각 프레임은 하나의 실제 장소이며, 포토모자이크는 별도 합의 없이 기본 방식으로 바꾸지 않는다.

## 시작하기

### 1. 원본 MP4 준비

사용자는 원본 영상을 **이미 MP4 파일로 가지고 있다.** 다운로드 기능, YouTube 검색, 영상·음원 재확보는 필요하지 않다.

저장소의 `input/` 디렉터리에 보유한 파일을 `input/badapple.mp4`로 두는 것을 권장한다. 원본 파일을 이동하고 싶지 않으면 다른 로컬 경로를 지정해도 된다. 원본은 읽기 전용 입력으로 취급하고 덮어쓰지 않는다.

MP4는 Git에 포함하지 않는다. 파일이 없는 환경에서는 입력이 필요한 검증만 차단 상태로 남기고, 데이터 수집기·저장소·테스트 구현은 진행할 수 있다. 대체 영상을 임의 다운로드하지 않는다.

### 2. 개발 환경

uv와 FFmpeg/ffprobe가 필요하다. uv가 관리하는 Python 3.12로 시작하며 패키지 요구 범위는 Python 3.11 이상이다. 현재 실행 환경은 Windows 11 네이티브다. Linux/WSL2는 아직 실행 검증하지 않았다. GPU·클라우드 서버·LLM API 키는 필수가 아니다.

```bash
uv sync --locked
uv run badapple-earth --help
uv run pytest -q --basetemp .local/pytest
```

`pyproject.toml`과 `uv.lock`을 사용한다. 테스트는 네트워크를 사용하지 않으며 실제 원격 접근은 아래 `sample`과 `verify-sample`로 별도 검증한다. 구체적인 의존성 선택과 실행 환경은 [TECH](docs/TECH.md#실행-환경)를 따른다.

### 3. P1 실제 실행

FFmpeg/ffprobe가 PATH에 있고 MP4를 배치했다면 입력 메타데이터·SHA-256과 대표 프레임 비교 시트를 생성한다.

```bash
uv run badapple-earth probe --input input/badapple.mp4 --output data/input-inspection
```

다음으로 세 지역에서 총 120개의 실제 RGB 창을 읽고 구름·결측을 검사하며 위치·촬영 시점·원본 창·체크섬을 기록한다.

```bash
uv run badapple-earth sample --config config/p1.json --output data/access-check --limit 120
```

`sample`는 최대 500개로 제한한 P1 연결 검증기다. 모든 샘플의 PNG와 `manifest.json`, `contact-sheet.jpg`를 만들고, 요청한 수를 채우지 못하면 실제 수·제외 사유를 남기고 종료 코드 1을 반환한다. 덮어쓰기를 막기 위해 `probe`와 `sample`의 출력 경로는 새 디렉터리여야 한다. 정상 완료한 수신을 다시 확인하려고 재다운로드하지 않는다. 오류 조사에 원본 재추출이 필요한 경우에만 `verify-sample` 명령을 사용할 수 있다.

P1 샘플은 기술 검증 자료이며 매칭 성과나 작품 품질 검증 결과가 아니다.

### 4. 후보 수집과 재개

```bash
uv run badapple-earth collect --config config/collect.json --output data/index/p2-10k --limit 10000
```

같은 명령을 다시 실행하면 저장한 카탈로그·블록·체크포인트를 사용해 이어간다. 이미 목표 수를 채웠으면 네트워크 요청 없이 종료한다. Ctrl+C는 현재 인덱스를 보존하고, `--stop-after-blocks N`은 지정한 블록 수를 처리한 뒤 체크포인트에서 끝낸다. 같은 설정으로 `--limit`을 높이면 기존 후보를 보존하며 추가 수집한다. 추출·소스·AOI 설정을 바꿀 때는 새 출력 경로를 사용한다.

기본 설정은 12개 AOI·6개 지형군, 3개 지상 스케일, 2048px 읽기 블록과 경계 확장 영역이다. 스케일 균등 할당을 우선하고, 못 채운 수량은 같은 AOI의 다른 유효 스케일에 재배분한다. 실제 RGB·mask·SCL에서 품질을 판단하고, 공통 16×12·32×24 명암 특징과 출처를 Parquet/NumPy shard에 저장한다. 모든 후보 사진을 저장하는 대신 결정적 무작위 표본 200장의 비교 시트를 만든다. 인접한 유사 후보는 출처를 유지하며 표식과 통계로 구분한다.

출력의 `manifest.json`에는 실제 수·지역/스케일 분포·제외 사유·관측 자원이, `jobs.sqlite`에는 작업 상태가 있다. `catalog/`는 고정 장면·추출 계획, `shards/`는 완료 데이터, `cache/blocks/`는 아직 처리할 창이 남은 블록이다. 행 수가 목표보다 적거나 자원 한도에 도달하면 부분 결과를 남기고 종료 코드 1을 반환한다. 현재 RAM 상한 2GiB·인덱스 디스크 예산 4GiB·여유 디스크 하한 1GiB는 `config/collect.json`에서 조정한다.

### 5. 목표 프레임·검색·결과 영상

`config/targets.json`의 38초부터 10초 구간을 6fps로 샘플링한다. PyAV로 실제 PTS가 목표 시각에 가장 가까운 프레임을 고르고, 동률이면 이전 프레임을 사용한다. 현재 입력은 회전 없는 정사각 픽셀 영상이며, 다른 회전·픽셀 비율의 입력은 크롭을 확정한 후 지원을 보강해야 한다.

```bash
uv run badapple-earth targets --config config/targets.json --output data/targets/38s-10s-6fps
uv run badapple-earth match --targets data/targets/38s-10s-6fps --index data/index/p2-10k --output data/runs/p3-10k --top-k 20
uv run badapple-earth materialize --targets data/targets/38s-10s-6fps --index data/index/p2-10k --run data/runs/p3-10k
uv run badapple-earth render --targets data/targets/38s-10s-6fps --run data/runs/p3-10k
```

`targets`와 `match`의 출력 경로는 새 디렉터리여야 한다. 검색은 고정한 인덱스 스냅샷의 정확한 명암 MSE Top-20이며, 점수가 같으면 타일 ID 순서로 정한다. `materialize`는 기존 표본·블록 캐시를 우선 재사용하고 실제로 필요한 사진만 추가로 읽는다. 실패 시 차순위를 선택하고 이유를 기록한다.

결과는 `color.mp4`, `gray.mp4`, `comparison.mp4`, 고정 목표 20개의 `top5.jpg`, 전체 Top-K의 `matches.parquet`, 실제 선택의 `selected.parquet`와 출처 manifest다. 현재 렌더는 24fps 사진 hold와 명시적 무음 모드다. 원본 음원 동기화는 사용권 확인 후 구현할 후속 기능이다. 소규모 결과는 기술적 기준 결과이며 충분히 큰 데이터셋에서의 표현력을 대신 평가하지 않는다.

## 사용 방법

기본 제작 흐름은 다음과 같다.

1. 로컬 MP4에서 실제 콘텐츠 비율과 테스트할 10초 구간을 확정하고 6fps로 샘플링한다.
2. Sentinel-2에서 지형이 다양한 후보를 수집하고 품질·좌표·특징량을 저장한다.
3. 1만 후보에서 Top-K를 검색해 비교 시트와 10초 컬러 결과를 만든다.
4. 동일한 목표 프레임을 유지하며 10만·100만 및 가용 범위의 더 큰 후보 집합으로 확대하고 점수·재생 결과·자원을 비교한다. 소규모의 낮은 시각 품질만으로 확대를 중단하지 않는다.

데이터 수집에는 인터넷 연결이 필요하다. 실제 서비스의 접근·비용 조건을 확인하고, 과금 서비스나 AWS Requester Pays를 사용자 승인 없이 활성화하지 않는다. 공개 데이터라는 사실과 다운로드 경로가 무료라는 사실은 별개다. [TECH의 소스별 접근 조건](docs/TECH.md#2-외부-데이터-소스)을 확인한다.

## 개발 이어가기

다른 AI 또는 개발자는 **`AGENTS.md` → `docs/PLAN.md` → `docs/SPEC.md` → `docs/TECH.md`** 순서로 읽고 PLAN의 다음 작업부터 진행한다. 이전 대화는 필요하지 않다.

다음 내용을 그대로 작업 지시로 사용할 수 있다.

```text
이 워크스페이스의 AGENTS.md와 docs/PLAN.md를 먼저 읽고, SPEC의 F/N 요구사항과
TECH의 설계를 따라 PLAN의 다음 작업부터 구현하라. Python은 uv로 관리한다.
기본 콘셉트는 프레임당 실제 장소 사진 한 장이다.
입력 영상은 사용자가 가지고 있는 로컬 MP4이며 권장 경로는 input/badapple.mp4다.
없는 파일이나 도구는 확인한 범위만 차단 사유로 남기고, 독립적으로 가능한 구현은 진행하라.
기존 1만 후보 인덱스와 10초 기준 결과를 활용해 PLAN의 다음 작업을 진행하라.
같은 타깃과 metric으로 후보를 확대 비교하고, 가용 자원 안에서 데이터셋을 최대한
넓히며 중간 규모마다 계측하라.
작은 표본의 낮은 시각 품질을 방식의 실패로 판정하거나 포토모자이크로 방향을 바꾸지 마라.
실제 실행한 검증과 미검증을 구분하고 PLAN의 현재 상태·다음 작업을 갱신하라.
```

`src/`, `tests/`, `config/`에는 현재 구현한 코드와 설정을 둔다. 원문 보관본의 샘플 코드는 현재 CLI와 별개인 과거 자료다.

## 문서

| 문서 | 역할 |
| --- | --- |
| [AGENTS.md](AGENTS.md) | 지속적인 개발·판단 원칙 |
| [docs/SPEC.md](docs/SPEC.md) | 목표·범위, F 기능 요구와 N 제약, 수용 기준 |
| [docs/TECH.md](docs/TECH.md) | 데이터 출처·대량 수집·저장·검색·렌더링의 기술 계약 |
| [docs/PLAN.md](docs/PLAN.md) | P 개발 단계, 현재 상태와 다음 행동 |
| [구성 이력](docs/history/2026-11-01-workspace.md) | 템플릿 버전, 원문 통합 내역, 정정과 패키지 검증 |
| [이전 명세 원문](docs/history/Bad_Apple_Earth_Imagery_SPEC.original.md) | 누락 확인용 보관본. 현재 사양으로 중복 관리하지 않음 |

현재 판단 기준은 `SPEC / TECH / PLAN`이다. `docs/history/`의 과거 문서는 맥락 보존용이며, 현재 문서와 충돌하면 현재 문서를 따른다.

## 라이선스와 출처

프로젝트 자체의 배포 라이선스는 아직 정하지 않았다. 사용한 개발 템플릿의 출처와 고정 커밋은 구성 이력에 기록했다. 위성·항공사진의 이용 조건, 원본 영상과 음악의 권리는 서로 별개이며, 자세한 출처·고지 규칙은 [TECH](docs/TECH.md#10-출처권리비용-경계)에서 관리한다. 원본 영상·음원·수집 데이터는 Git에 넣지 않는다.

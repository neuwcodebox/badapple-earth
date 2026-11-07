# Bad Apple Earth

Bad Apple!!의 각 프레임과 형태·명암이 닮은 **실제 위성·항공사진 한 장**을 찾아 영상을 재구성하는 로컬 제작 도구다. 그림을 새로 생성하거나 여러 장소를 합성하지 않고, 실제 지구에서 촬영된 장면 자체를 검색한다.

> **기본 제작은 모든 원본 프레임의 32×24 MSE Top-20에서 Astra가 시간적 맥락을 고려해 한 장을 선택하는 방식이다.** 연속 구간을 최대 25개 서브에이전트에 병렬 배정한다. 16:9 화면에는 작은 원본·후보 다섯 장·선택 연결선·큰 사진을 배치한다. 시간·프레임·실제 MSE 순위와 참고용 MS-SSIM 점수는 작게 정돈하고 좌표·촬영일을 한 줄로 표시한다. 실제 실행·재생 평가 상태는 [PLAN](docs/PLAN.md)에서 관리한다.

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

### 전곡과 16:9 최종 영상

로컬 MP4·100만 후보·완료된 Astra 전곡 선택이 있으면 다음 명령으로 모든 프레임을 제작한다. 모델 선택을 준비하는 절차는 아래의 “Astra로 같은 후보에서 선택하기”를 따른다.

```powershell
$env:OPENBLAS_NUM_THREADS = '8'
$env:OMP_NUM_THREADS = '8'
$env:NUMBA_NUM_THREADS = '8'
uv run badapple-earth produce --config config/production.json --output data/runs/p6-astra-full
```

같은 명령으로 packing·검색·사진 준비·완료 인코딩 segment를 재사용하며 이어간다. `config/production.json`에서 입력·인덱스·로컬 글꼴·인코딩 작업 수·오디오를 조정한다. 기본 글꼴 캐시가 없으면 Windows Segoe UI 또는 사용 가능한 DejaVu Sans를 찾고, 없으면 직접 지정한다. 글꼴 파일은 Git에 포함하지 않는다.

최종 출력은 `final-16x9.mp4`(1920×1080), `satellite-full.mp4`(원래 콘텐츠 비율의 사진 전용), `preview.png`, 전 프레임 `selected.parquet`·`frame-plan.parquet`, 전체 Top-20의 `search/matches.parquet`, `credits.json`이다. Astra 선택과 MSE 상위의 다른 네 장소를 실제 MSE 순서대로 표시하고 선택 위치를 강조한다. 같은 장소의 다른 방향이 먼저 검색되어도 선택된 방향을 표시한다. 순위 숫자는 표시 행 번호가 아닌 원래 Top-20의 MSE 순위다. 표시된 MS-SSIM은 형태 대응의 참고 수치이며 Astra의 점수나 선택 기준을 수치화한 것이 아니다. 타이틀·슬로건은 표시하지 않는다.

`search_grid: "fine"`는 32×24, `coarse`는 비교용 16×12다. `selection_method: "astra"`는 `visual_run`의 완료 선택과 `visual_experiment_id`를 사용한다. `rerank_top_k: 20`은 모델이 본 후보 수, `display_top_k: 5`는 선택 이후의 표시 수다. 기본 `reuse_run`은 `data/runs/p6-grid32-full`의 타깃·검색·사진을 재사용하며 참고용 MS-SSIM도 이 run의 저장 점수에서 읽는다. 같은 격자·K의 검색과 버전이 맞는 캐시만 재사용한다. 선택 JSONL·시트·프레임별 출처는 `visual_run`에 보존하며 모델 판단을 seed만으로 재현한다고 가정하지 않는다.

기존 MS-SSIM 최댓값 선택 전곡은 `config/production-ms-ssim.json`과 `data/runs/p6-grid32-full`에 보존한다. `selection_method: "ms-ssim"`과 `"mse"`도 비교용으로 지원한다.

6,955개 원본 프레임을 모두 쓰고 30000/1001fps를 유지한다. 동일한 검색 특징은 계산만 공유하며 출력 프레임을 생략하거나 보간하지 않는다. 원본 AAC는 로컬 영상에 전체 복사하고, `audio_mode: "silent"`는 무음으로 만든다. 입력에 있는 약 65ms의 오디오 꼬리 때문에 영상 프레임을 추가하지 않는다. 이 경로는 현재 CFR·SAR 1:1 입력을 지원하며 외부 업로드는 하지 않는다.

같은 32×24 검색에서 **MSE 1위만 직접 선택**하는 전곡은 다음 프리셋으로 만든다. 완료된 `p6-grid32-full`의 타깃·검색·사진을 재사용하며 MS-SSIM을 계산하지 않는다. 화면의 점수도 실제 MSE다.

```powershell
uv run badapple-earth produce --config config/production-mse.json --output data/runs/p6-grid32-mse
```

후보 평가는 기존 RGB를 재사용하며 PNG는 화면에 표시할 사진만 준비한다. 큰 해상도는 선택된 큰 원본 창에만 요청하고, 실패하면 같은 view의 보관된 사진을 사용한다. 검색용 기본 주소/특징 packing 약 3.4GB와 32×24 uint8 packing 약 3.072GB는 완료 후 재사용하며 전체 float64 캐시를 만들지 않는다. 180프레임 단위로 두 결과를 인코딩해 재시작 시 완료한 구간을 다시 만들지 않는다.

### 선택 지표 비교

완료한 전곡 run의 Top-20 view를 재사용해 MSE·MS-SSIM·FSIM·다중 해상도 MSE·ZNCC를 같은 후보에서 비교한다. 지정 구간의 모든 원본 프레임을 사용하며 추가 검색·다운로드는 하지 않는다.

```powershell
$env:OPENBLAS_NUM_THREADS = '1'
$env:OMP_NUM_THREADS = '1'
uv run badapple-earth compare-metrics --run data/runs/p6-full --output data/runs/p6-metrics-top20 --start 38 --duration 10 --top-k 20 --workers 8
```

`comparison.mp4`는 위쪽 원본·MSE·MS-SSIM, 아래쪽 FSIM·Multi-MSE·ZNCC의 2×3 비교 영상이다. 알고리즘명·선택된 사진의 원래 MSE 순위·해당 지표 점수와 작은 시간·프레임 카운터를 표시한다. 무음이며 각 패널은 같은 canonical RGB를 사용한다. `evaluations.parquet`에는 모든 프레임×후보의 점수와 출처, `selected.parquet`에는 방법별 선택, `manifest.json`에는 설정·변경 수·선택 순위 분포·비용이 남는다. 같은 명령으로 완료 점수 배치를 재사용한다.

이 지표 비교는 `p6-full`의 16×12 검색 Top-20을 사용하며 현재 기본 제작의 32×24 검색과 구분한다. 지표 간 점수는 단위가 달라 직접 비교할 수 없다. FSIM 포트는 연구·교육 목적 이용 조건을 따르며 [고지](THIRD_PARTY_NOTICES.md)와 [계산 설정](docs/TECH.md#53-점수-조합과-반복-제어)을 참고한다. 비교 명령은 전곡의 설정을 변경하지 않는다.

### Astra로 같은 후보에서 선택하기

완료된 32×24 Top-20을 Astra 서브에이전트에게 보여주는 별도 실험이다. 프레임을 전체에서 무작위로 섞어 에이전트에 나누고 후보 순서도 독립적으로 섞는다. 시간·기존 점수·순위·선택은 가린다. 시트는 기존 RGB만 사용하며 새 검색·다운로드·API 결제를 하지 않는다.

```powershell
uv run python -m badapple_earth.production.visual prepare --run data/runs/p6-grid32-full --output data/reviews/astra-selection-300 --start 38 --duration 10 --workers 3 --seed 20261103
```

`jobs/review-*.json`의 시트를 각 에이전트에게 맡긴다. 에이전트는 자신에게 배정된 시트만 보고 각 case에서 후보 하나를 선택해 지정된 `choices/review-*.jsonl`에 `{"case_id":"C001","candidate_id":"P07"}` 형식으로 저장한다. `private-map.json`은 선택이 끝나기 전 에이전트에게 공개하지 않는다. 완료된 선택은 그대로 보존한다.

```powershell
uv run python -m badapple_earth.production.visual render --output data/reviews/astra-selection-300
```

모든 선택이 채워지면 `comparison.mp4`에 원본·MSE·MS-SSIM·Astra를 같은 canonical 크기와 원본 프레임 순서로 표시한다. `selected.parquet`에는 선택 사진의 출처·회전·source PTS가 남는다. 비교는 무음이다.

인접 프레임의 선택도 고려하는 비교는 `--mode sequential`을 사용한다. 연속 100프레임씩 세 에이전트가 병렬 처리하며 각 job의 `context`는 앞 구간 마지막 10프레임의 참고 시트다. 참고 프레임의 선택은 저장하지 않는다. 고정 `V` ID로 같은 사진을 추적하며 형태 대응이 비슷하면 직전 사진을 유지하되 실제 원본 변화에는 따라가도록 지시한다. 후보 20개와 사진 자체는 독립 비교와 동일하다.

```powershell
uv run python -m badapple_earth.production.visual prepare --run data/runs/p6-grid32-full --output data/reviews/astra-sequential-300 --mode sequential --workers 3 --independent-run data/reviews/astra-selection-300
uv run python -m badapple_earth.production.visual render --output data/reviews/astra-sequential-300
```

연속 모드의 비교 패널은 원본·독립 Astra·MS-SSIM·연속 Astra다. 각 에이전트는 자기 구간의 직전 선택을 고려하며 구간 사이의 실제 선택은 공유하지 않는다.

전곡은 독립 비교 run 없이 준비하고 25개 job을 같은 수의 Astra 서브에이전트에 맡긴다. 시트가 준비된 job부터 시작할 수 있도록 `jobs/review-*.ready`를 기록한다. 작업 지침은 `config/astra-reviewer.txt`에서 출력 폴더로 복사한다. 약 278~279프레임의 연속 구간마다 앞 10프레임의 참고 맥락이 있으며, 모든 대상 프레임을 실제로 보고 선택하도록 한다.

```powershell
uv run python -m badapple_earth.production.visual prepare --run data/runs/p6-grid32-full --output data/reviews/astra-sequential-full --mode sequential --workers 25 --all-frames
# 각 job을 Astra 서브에이전트에 맡겨 choices/*.jsonl을 저장한 뒤:
uv run python -m badapple_earth.production.visual collect --output data/reviews/astra-sequential-full
uv run badapple-earth produce --config config/production.json --output data/runs/p6-astra-full
```

`collect`는 전체 선택을 원본 순서의 Parquet로 연결한다. `produce`는 같은 검색의 실제 후보에 속하는 모델 선택을 그대로 사용하며 MS-SSIM 최대 후보로 다시 고르지 않는다. 출력 경로나 실험을 바꾸면 `visual_run`과 `visual_experiment_id`도 함께 지정한다.

### MSE 격자 해상도와 후보 수 높이기

최종 MS-SSIM은 그대로 두고 MSE 검색 격자를 16×12에서 32×24로 높이며, 후보는 격자마다 Top-512까지 넓힌다. 이미 저장한 특징을 사용하므로 새 수집·다운로드 없이 실행한다.

```powershell
$env:OPENBLAS_NUM_THREADS = '8'
$env:OMP_NUM_THREADS = '8'
$env:NUMBA_NUM_THREADS = '8'
uv run badapple-earth compare-grid --config config/grid.json --output data/runs/p6-grid32-wide
```

`comparison.mp4` 위쪽은 원본·16×12 Top-20·16×12 Top-512, 아래쪽은 32×24 Top-20·32×24 Top-512·두 Top-512 합집합이다. **모두 동일한 256×191 MS-SSIM으로 최종 선택**한다. 합집합은 중복 view를 제거해 최대 1,024개이며 장소 중복은 최종 표시 단계에서 정리한다. `WIDE`는 설정의 `top_k`(기본 512)를 뜻한다. 사진 아래 점수는 모두 MS-SSIM이다.

`config/grid.json`에서 구간·K·프로세스 수와 재사용할 점수 run을 정한다. 입력·인덱스·MS-SSIM 버전이 같은 출처와 점수만 재사용한다. 새 후보만 실제 RGB 캐시에서 읽고 계산하며, 원본 프레임을 건너뛰지 않는다. 검색·점수 배치가 완료되어 있으면 같은 명령으로 재개한다. `shortlists.parquet`는 두 격자의 원래 순위와 SSE, `evaluations.parquet`는 모든 MS-SSIM 점수, `selected.parquet`는 다섯 조건의 선택, `display-five.parquet`는 합집합 평가 후의 서로 다른 표시 장소를 담는다. 64×48 격자는 이 비교에 포함하지 않는다.

### 전체 데이터에서 ZNCC·Gradient Correlation으로 찾기

MSE Top-20 밖의 사진도 찾으려면 전체 회전 view에서 다중 해상도 ZNCC와 부호 있는 Gradient Correlation으로 각각 검색하고 합집합을 평가한다. 기본은 경로별 512개와 원래 MSE 20개의 합집합이다. 화면에 보여줄 다섯 장소는 평가가 끝난 뒤 고른다.

```powershell
$env:OPENBLAS_NUM_THREADS = '8'
$env:OMP_NUM_THREADS = '8'
$env:NUMBA_NUM_THREADS = '8'
uv run badapple-earth compare-correlation --config config/correlation.json --output data/runs/p6-correlation
```

설정 파일에서 구간·후보 수·구조 가중치·최소 실제 대비·밝기 선택 허용폭을 조정한다. 기존 RGB·회전 view를 재사용하며 다운로드는 하지 않는다. 추가 32×24 명암 packing은 약 3.072GB다. 검색은 후보 배치, 정밀 평가는 RGB shard 배치 단위로 재개한다. 정밀 평가 자식 프로세스의 BLAS는 각각 한 스레드로 제한한다.

`comparison.mp4` 위쪽은 원본·기존 Top-20 MS-SSIM·확대 후보 MS-SSIM, 아래쪽은 확대 후보의 ZNCC·GC·ZNCC+GC다. 같은 구간의 모든 원본 프레임을 사용하며 무음이다. `Z#`·`G#`는 독립 검색 경로의 원래 순위이며 `—`는 해당 경로 Top-K 밖이라는 뜻이다. 정규화는 검색 특징에만 적용하며 모든 패널은 실제 원색 RGB를 같은 크기로 표시한다.

`candidates.parquet`는 고유 view의 실제 출처, `shortlists.parquet`는 프레임별 후보·경로 순위, `evaluations.parquet`는 정밀 점수·실제 대비, `selected.parquet`는 다섯 방법의 선택과 fallback 사유를 담는다. `display-five.parquet`는 최종 ZNCC+GC 선택 이후의 서로 다른 다섯 표시 장소다. 새 방법의 시각적 채택 여부와 전곡 변경은 비교 결과로 판단한다.

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

기본 설정은 12개 AOI·6개 지형군, 3개 지상 스케일, 2048px 읽기 블록과 경계 확장 영역이다. 스케일 균등 할당을 우선하고, 못 채운 수량은 같은 AOI의 다른 유효 스케일에 재배분한다. 실제 RGB·mask·SCL에서 품질을 판단하고, 공통 16×12·32×24 명암 특징과 출처를 Parquet/NumPy shard에 저장한다. 결정적 무작위 표본 200장의 비교 시트를 만들며, `cache_rgb=true`이면 모든 후보의 canonical RGB도 무손실 shard로 보관한다. 인접한 유사 후보는 출처를 유지하며 표식과 통계로 구분한다.

출력의 `manifest.json`에는 실제 수·지역/스케일 분포·제외 사유·관측 자원이, `jobs.sqlite`에는 작업 상태가 있다. `catalog/`는 고정 장면·추출 계획, `shards/`는 완료 데이터, `cache/blocks/`는 아직 처리할 창이 남은 블록이다. 행 수가 목표보다 적거나 자원 한도에 도달하면 부분 결과를 남기고 종료 코드 1을 반환한다. 현재 RAM 상한 2GiB·인덱스 디스크 예산 4GiB·여유 디스크 하한 1GiB는 `config/collect.json`에서 조정한다.

### 5. 목표 프레임과 MSE 기본 검색

`config/targets.json`의 38초부터 10초 구간을 6fps로 샘플링한다. PyAV로 실제 PTS가 목표 시각에 가장 가까운 프레임을 고르고, 동률이면 이전 프레임을 사용한다. 현재 입력은 회전 없는 정사각 픽셀 영상이며, 다른 회전·픽셀 비율의 입력은 크롭을 확정한 후 지원을 보강해야 한다.

```bash
uv run badapple-earth targets --config config/targets.json --output data/targets/38s-10s-6fps
uv run badapple-earth match --targets data/targets/38s-10s-6fps --index data/index/p2-10k --output data/runs/p3-10k-rotations --top-k 20
uv run badapple-earth materialize --targets data/targets/38s-10s-6fps --index data/index/p2-10k --run data/runs/p3-10k-rotations
uv run badapple-earth render --targets data/targets/38s-10s-6fps --run data/runs/p3-10k-rotations
```

`targets`와 `match`의 출력 경로는 새 디렉터리여야 한다. 검색은 고정한 인덱스 스냅샷의 정확한 명암 MSE Top-20이다. 기본 `--rotations 0,90,180,270`은 시계 방향의 네 방향을 검색하고, 동률이면 view ID 순서로 정한다. 90/270도는 회전한 사진을 중앙 크롭해 목표 비율을 유지하며 선택한 변환을 실제 영상에도 적용한다. 1만 장소라면 최대 4만 view를 검색하지만 장소 수가 늘어난 것은 아니다.

최초 검색에서 추가 회전 특징을 만든 뒤 재사용한다. 기존 PNG·블록 캐시를 우선 사용하며, 캐시에 없는 기본 사진만 원본에서 읽는다. 예전 0도 결과의 사진이 있으면 `--photo-cache data/runs/p3-10k/photos`로 재사용할 수 있다. 0도 비교 기준선은 `--rotations 0`으로 실행한다. 직각 외 각도는 현재 지원하지 않는다.

`materialize`도 기존 표본·블록·기본 사진 캐시를 재사용하고 실제 선택 사진을 view ID로 보관한다. 실패 시 차순위를 선택하고 이유를 기록한다. 각도·크롭·원래 위치와 최종 화각은 순위/선택 Parquet에 남는다.

이 구간 실험의 결과는 `color.mp4`, `gray.mp4`, `comparison.mp4`, 고정 목표 20개의 `top5.jpg`, 전체 Top-K의 `matches.parquet`, 실제 선택의 `selected.parquet`와 출처 manifest다. 위 `render` 명령은 24fps 사진 hold·무음으로 비교 조건을 유지한다. 전체 프레임과 원본 오디오는 `produce` 경로에서 처리한다. 소규모 결과는 기술적 기준 결과이며 충분히 큰 데이터셋에서의 표현력을 대신 평가하지 않는다.

### 6. 기존 후보를 재사용하며 규모 확대

P4는 기존 1만 후보에 새 60개 AOI의 9만 후보를 더한다. 새 설정은 `config/collect-p4.json`이며 여섯 지형군, 폭별 초기 가중치 55:30:15, 수집 중 직각 회전 특징 생성을 사용한다. RGB를 읽은 시점에 세 방향 특징을 같은 배치에 저장해 후속 검색의 원본 읽기를 줄인다.

```bash
uv run badapple-earth collect --config config/collect-p4.json --output data/index/p4-additional-90k --limit 90000 --redistribute
uv run badapple-earth combine --index data/index/p2-10k --index data/index/p4-additional-90k --output data/index/p4-100k
uv run badapple-earth match --targets data/targets/38s-10s-6fps --index data/index/p4-100k --output data/runs/p4-100k --top-k 20
uv run badapple-earth materialize --targets data/targets/38s-10s-6fps --index data/index/p4-100k --run data/runs/p4-100k
uv run badapple-earth render --targets data/targets/38s-10s-6fps --run data/runs/p4-100k
uv run badapple-earth compare --baseline data/runs/p3-10k-rotations --run data/runs/p4-100k
```

`combine`은 원래 파일을 복사하지 않고 상대경로와 고정 shard 목록으로 참조한다. 같은 tile ID는 한 번만 검색한다. 원래 인덱스 폴더는 보관해야 하며, 이후 멤버의 shard 목록이 달라지면 `combine`으로 스냅샷을 다시 만든다. 원본 인덱스를 bundle 출력 경로로 덮어쓸 수 없다. 실제 장소 수는 중복을 뺀 합계이며, 일부만 수집됐으면 그 실제 수로 동작한다.

`compare`는 타깃·metric·각도 집합·회전 recipe가 같은 run만 비교하고 `baseline-comparison.json`에 목표별 MSE 차이와 개선/동률/악화 수를 저장한다. 지역·스케일 표본도 넓어지므로 후보 수만의 효과로 과장하지 않는다. P4 운영 한도는 관측 RSS 3GiB, 인덱스 디스크 24GiB, 여유 공간 하한 8GiB다.

두 run의 영상이 있으면 `scale-comparison.mp4`도 만든다. 좌측은 원본, 가운데는 기준선, 우측은 확대 결과다.

`--redistribute`는 일부 AOI의 가용 후보가 초기 할당보다 적을 때, 할당을 채운 AOI의 남은 창으로 부족분을 메운다. 같은 지형군을 우선하며 고갈된 AOI는 재배분 대상에서 제외한다. 모든 곳의 여유가 없거나 추가 수집이 진전되지 않으면 실제 부분 결과로 종료한다. 지역별 수량은 manifest에 실제 분포로 남긴다.

100만 단계는 기존 10만에 `collect-million.json`의 600개 AOI에서 90만을 더한다. 새 지역과 기존 지역 주변을 포함한 120개 중심 지역에 서로 겹치지 않는 1도 AOI를 다섯 개씩 배정한다. 지형군은 수집 계획의 분류이며 실제 토지피복을 검증한 라벨이 아니다. 폭 256/512/1024/2048px의 시작 가중치는 15:30:40:15이며, 실제 통과 분포는 부족분 재배분에 따라 달라진다.

```bash
uv run badapple-earth collect-parallel --config config/collect-million.json --output data/index/p5-additional-900k --limit 900000 --workers 4
uv run badapple-earth combine --index data/index/p4-100k --index data/index/p5-additional-900k/part-01 --index data/index/p5-additional-900k/part-02 --index data/index/p5-additional-900k/part-03 --index data/index/p5-additional-900k/part-04 --output data/index/p5-1m
uv run badapple-earth match --targets data/targets/38s-10s-6fps --index data/index/p5-1m --output data/runs/p5-1m --top-k 20
uv run badapple-earth materialize --targets data/targets/38s-10s-6fps --index data/index/p5-1m --run data/runs/p5-1m
uv run badapple-earth render --targets data/targets/38s-10s-6fps --run data/runs/p5-1m
uv run badapple-earth compare --baseline data/runs/p4-100k --run data/runs/p5-1m
```

`collect-parallel`은 AOI를 결정적으로 분할해 독립 인덱스 네 개를 만들며 같은 명령으로 재개한다. 작업별 로그·설정과 전체 `collection.json`은 출력 폴더에 남긴다. 완료를 기다리는 동안 인덱스를 반복 스캔하지 않는다. 각 작업의 RAM 예산은 3GiB, 디스크 예산은 55GiB이고 모든 작업은 같은 드라이브의 여유 공간 하한 20GiB를 지킨다. 신규 RGB 자체는 약 126GiB를 차지하므로 기존 실행보다 저장 공간이 많이 필요하다. 수집 중 RGB·네 방향 특징을 함께 저장하고 사진 출력에서 재사용한다. RGB를 보관하는 수집에서는 AOI 처리를 마치면 재생 가능한 원본 블록 캐시를 비워 중복 저장량을 제한한다. 이후 quota를 늘릴 때 아직 수집하지 않은 창의 원본을 다시 읽을 수 있다. 실제 수집·비교 완료 여부는 [PLAN](docs/PLAN.md)에서 확인한다.

### 항공사진으로 후보 풀 보강

NAIP·LINZ는 기존 수집·재개·RGB 캐시를 사용한다. 두 명령을 별도 터미널에서 실행하면 소스별 두 작업, 총 네 작업이 동작한다. 각 소스의 초기 목표는 10만 후보이며 완료 상태와 실제 수량은 `collection.json`, 세부 로그는 `part-01.log`·`part-02.log`에 남는다.

```bash
uv run badapple-earth collect-parallel --config config/collect-naip.json --output data/index/p5-naip-100k --limit 100000 --workers 2
uv run badapple-earth collect-parallel --config config/collect-linz.json --output data/index/p5-linz-100k --limit 100000 --workers 2
```

NAIP는 Microsoft Planetary Computer의 익명 임시 SAS, LINZ는 공개 HTTPS를 사용한다. 사용자 키나 AWS Requester Pays를 활성화하지 않는다. LINZ는 30cm 촬영 사업을 사용하며 6cm 도시 자료는 초기 대량 계획에 포함하지 않는다. RGB·실제 밴드·유효 마스크·촬영 단일 시점/구간·출처를 보존하고 구름 품질은 미평가로 구분한다. 세부 정책은 [TECH의 항공사진 소스](docs/TECH.md#22-naip--조건부-고해상도-보강)를 따른다.

수집을 계속하면서 공개된 shard를 중간 스냅샷으로 고정하고 기존 100만에 추가할 수 있다. 아래 명령은 사진·특징을 복사하지 않으며 같은 스냅샷 경로로 다시 실행해도 나중에 수집한 행을 섞지 않는다. 새 시점의 스냅샷은 새 경로를 사용한다.

```bash
uv run badapple-earth combine --freeze --index data/index/p5-naip-100k/part-01 --index data/index/p5-naip-100k/part-02 --index data/index/p5-linz-100k/part-01 --index data/index/p5-linz-100k/part-02 --output data/index/p5-aerial-interim-new
uv run badapple-earth combine --disjoint-sources --index data/index/p5-1m --index data/index/p5-aerial-interim-new --output data/index/p5-aerial-interim
uv run badapple-earth produce --config config/production-aerial-interim.json --output data/runs/p6-aerial-interim
```

중간 전곡은 완료된 기존 32×24 검색·평가·사진을 재사용하고 추가 후보만 검색한다. 선택은 이 비교용 설정의 MS-SSIM이며 기존 Astra 전곡은 보존한다. 현재 고정한 추가 후보 수와 실제 제작 상태는 [중간 전곡 기록](docs/history/2026-11-03-P6-aerial-interim.md)에 둔다.

두 소스가 각각 10만 개로 완료되면 별도의 전체 스냅샷으로 결합한다. 부분 결과를 완성된 120만으로 표시하지 않는다.

```bash
uv run badapple-earth combine --index data/index/p5-1m --index data/index/p5-naip-100k/part-01 --index data/index/p5-naip-100k/part-02 --index data/index/p5-linz-100k/part-01 --index data/index/p5-linz-100k/part-02 --output data/index/p5-aerial-1.2m
```

현재 실행 상태·자원·비교 여부는 [PLAN](docs/PLAN.md), 실제 경위는 [수집 기록](docs/history/2026-11-03-P5-aerial-collection.md)에 둔다. 새 인덱스를 기존 전곡이나 Astra 선택에 자동 적용하지 않는다.

### 추가 항공사진 30만 후보 수집

기존 120만 후보를 보존하고 NAIP·LINZ 각 10만, 네덜란드·스위스 각 5만을 별도 인덱스에 더한다. `config/collect-expand-{naip,linz,netherlands,swiss}.json`이 고정한 새 AOI·촬영 사업·지상 스케일을 사용한다. 두 소스씩 각각 두 작업으로 실행하며 공유 디스크 여유 하한 30GiB를 지킨다. 완료 후 `combine --keep-duplicates`로 저장된 참조·수량만 합한다. 별도 인덱스 사이의 중복 사진을 허용하며 수집 중 유사 사진 비교도 하지 않는다. 실제 진행은 [PLAN](docs/PLAN.md), 네 소스의 실행·재개·결합은 [확대 기록](docs/history/2026-11-06-P5-expansion-300k.md)에 둔다.

```powershell
uv run badapple-earth collect-parallel --config config/collect-expand-netherlands.json --output data/index/p5-expand-netherlands-50k --limit 50000 --workers 2
uv run badapple-earth collect-parallel --config config/collect-expand-swiss.json --output data/index/p5-expand-swiss-50k --limit 50000 --workers 2
```

네덜란드는 공식 2025년 RGB 색인과 공개 타일형 TIFF, 스위스는 공개 STAC의 RGB COG를 사용한다. 촬영 정보가 제품/대표 연도이면 정확도를 저장하고 화면에도 연도로 표시한다. 네덜란드의 파일 수정 시점·스위스 카탈로그의 1월 1일을 실제 촬영일로 해석하지 않는다. 구름 품질은 미평가이며 사진의 원래 RGB와 출처·창·변환을 보존한다. 같은 출력 폴더의 수집이 이미 실행 중이면 중복 시작하지 않는다.

### 7. 선택 실험: 30° 회전·작은 크롭

추가 수집과 동시에 신규 shard의 검색 인덱스를 준비하려면 다음 명령을 별도 터미널에서 실행한다. `prepare-expand.json`은 저장된 신규 RGB만 읽어 packing·32×24 fine 배열·30° 회전/75% 크롭 특징을 만들며, 이후 새 shard도 이어서 처리한다. 수집과 같은 일시중지 표식을 사용한다. 두 작업의 진행·중단·재개는 [병행 준비 기록](docs/history/2026-11-07-P5-live-preparation.md)을 따른다.

```powershell
uv run python -m badapple_earth.production.variants.preparation --config config/prepare-expand.json
```

고정된 기존 장소의 로컬 RGB를 재사용한다. `views-index`와 `compare-views`는 서로 다른 터미널에서 병행할 수 있다. 검색은 인덱서가 완료한 그룹만 읽고, 두 명령 모두 체크포인트부터 재개한다.

```powershell
uv run badapple-earth views-index --config config/views30.json
```

```powershell
$env:OPENBLAS_NUM_THREADS = '4'
$env:OMP_NUM_THREADS = '4'
$env:NUMBA_NUM_THREADS = '4'
uv run badapple-earth compare-views --config config/views30.json --output data/runs/p6-views30-comparison
```

작은 크롭은 회전 후 유효 영역의 가로·세로 75%이며, 새 장소 수로 세지 않는다. `comparison.mp4`는 같은 38–48초 원본 프레임으로 기존 직각·30°·30°+작은 크롭을 비교하는 무음 영상이다. 기존 전곡·Astra 선택은 보존한다. 실제 진행과 검증은 PLAN에 기록한다.

120만 장소에 두 크기·12방향을 적용하는 별도 전곡 설정은 다음과 같다. 기존 수집·인덱스·직각 검색·일부 affine 질의와 점수를 재사용하며 같은 경로의 중단 체크포인트부터 이어간다. 원본 오디오는 유지한다. 현재 작업이 일시중지 상태라면 사용자의 명시적 재개 요청 후 실행한다.

```powershell
$env:OPENBLAS_NUM_THREADS = '8'
$env:OMP_NUM_THREADS = '8'
uv run badapple-earth produce --config config/production-aerial120-views30.json --output data/runs/p6-aerial120-views30-full
```

### 8. 선택 실험: 형태 우선 검색과 같은 구간 비교

기존 후보 풀의 전체 view에 PHOG·Rank-layout 검색과 다중 임곗값 SDF·영역·양방향 방향성 Chamfer를 적용한다. 형태가 거의 같은 후보 사이에서 실제 사진 광도로 선택한다. MSE로 앞단 후보를 제한하지 않으며 원색 사진은 분석용 마스크로 바꾸지 않는다.

```powershell
$env:OPENBLAS_NUM_THREADS = '1'
$env:OMP_NUM_THREADS = '1'
uv run badapple-earth shape-index --index data/index/p4-100k --output data/shape-v2/p4-100k --workers 8
uv run badapple-earth shape-match --targets data/targets/38s-10s-6fps --index data/shape-v2/p4-100k --output data/runs/p5-shape-v2 --config config/shape-v2.json
uv run badapple-earth shape-render --targets data/targets/38s-10s-6fps --index data/shape-v2/p4-100k --run data/runs/p5-shape-v2 --baseline data/runs/p4-100k
```

분석 캐시와 검색 체크포인트는 같은 명령으로 재개한다. 수치 처리는 8개 작업 프로세스이며 원격 읽기·완료 파일 공개는 한 곳에서 관리한다. 최초 분석에는 기존 고해상도 캐시가 없는 원본만 추가 읽고, 이후 검색·선택 사진 출력은 공유 RGB 캐시를 재사용한다.

`method-comparison.mp4`는 왼쪽부터 원본·10만 MSE·10만 형태 검색이다. `report.html`에는 전체 목표의 사진·마스크·SDF·형태/광도/대비 점수와 출처가 있다. `low_visibility`는 비교 영상과 보고서에 표시한다. 설정·분기·저장 비용은 [형태 검색 계약](docs/SHAPE_MATCHING.md)에 둔다.

### 9. 영역 균형·윤곽·실제 대비의 10초 비교

기존 완성 전곡의 같은 원본 프레임과 전체 view 풀을 사용한다. 현재 선택을 보존하고 영역 균형만 적용한 선택, 윤곽과 실제 경계 대비까지 평가한 선택을 원본과 함께 비교한다. 원색 사진은 바꾸지 않으며 필요한 RGB는 로컬 캐시에서 읽는다.

```powershell
$env:OPENBLAS_NUM_THREADS = '8'
$env:OMP_NUM_THREADS = '8'
uv run badapple-earth compare-silhouette --config config/silhouette.json --output data/runs/p6-silhouette-visible-10s
```

검색과 점수 배치는 같은 명령으로 재개한다. 후보 검색은 기존 MSE Top-20로 제한하지 않는다. 기본 구간은 38–48초이며 원본 300프레임의 비교용 무음 영상이다. 초기 가중치·시인성 조건과 실제 결과는 TECH와 PLAN에 기록한다.

큰 형태와 작은 복잡한 형태를 따로 보는 후속 설정은 `config/silhouette-followup.json`이다. 135–145초와 205–215초의 원본 600프레임을 한 번에 검색하며, `layout: pair`로 왼쪽 현재 선택·오른쪽 윤곽과 대비·위쪽 작은 원본을 배치한다. 같은 명령의 config를 이 파일로, output을 `data/runs/p6-silhouette-followup-20s`로 바꾸면 각 10초인 `small-complex.mp4`와 `large.mp4`를 만든다. 구간별 사진 교체 횟수와 fallback도 manifest에 남긴다.

색감 연속성 비교는 완료한 윤곽 검색 결과만 재사용한다. 형태 점수가 최고점에서 .02 이내인 후보 중 앞뒤 색 변화가 작은 경로를 구간 전체에서 고른다. 왼쪽은 독립 윤곽 선택, 오른쪽은 색감 연속 선택이다.

```powershell
uv run python -m badapple_earth.production.silhouette.temporal --config config/silhouette-color.json --output data/runs/p6-silhouette-color-20s
```

### 10. 윤곽·색감 연속 선택의 전곡 제작과 중단·재개

최신 전곡은 `config/silhouette-full.json`으로 218.5초 이전의 원본 6,549프레임을 사용한다. 완료한 비교 구간 900프레임의 검색·형태 점수를 재사용하고, 전체 구간에 하나의 색감 경로를 적용한다. 기존 영상은 보존한다.

```powershell
$env:OPENBLAS_NUM_THREADS = '8'
$env:OMP_NUM_THREADS = '8'
$env:NUMBA_NUM_THREADS = '8'
uv run python -m badapple_earth.production.silhouette.full --resume
```

중단 요청은 다른 터미널에서 `uv run python -m badapple_earth.production.silhouette.full --pause`로 보낸다. 새 배치 배정을 멈추고 실행 중인 배치를 저장한 뒤 manifest가 `paused`로 바뀐다. 같은 `--resume` 명령은 중단 요청을 해제하고 이어간다. 갑자기 프로세스가 종료돼도 원자적으로 저장된 검색 체크포인트·평가 배치·사진·인코딩 구간을 재사용한다. 미완성 배치나 영상 구간만 다시 처리한다.

검색까지만 끝내고 멈추려면 실행 명령에 `--stop-after-search`를 추가한다. 완료된 검색을 저장한 뒤 후속 후보 구성·평가를 시작하기 전에 `paused`로 종료한다. 이후 이 옵션 없이 `--resume`하면 검색 결과를 재사용해 다음 단계부터 이어간다.

출력은 `data/runs/p6-silhouette-color-full/`의 `final-16x9.mp4`와 `satellite-full.mp4`이며, 로컬 원본 오디오도 콘텐츠 끝까지 보존한다. 현재 단계는 루트 `manifest.json`, 검색 진행은 `retrieval/progress.json`, 정밀 평가는 루트 `progress.json`에서 확인한다. 기존 출처·좌표·변환의 반복 검수는 하지 않는다.

같은 선택을 흑백으로 보는 버전은 해당 폴더 아래 `grayscale/final-16x9.mp4`와 `grayscale/satellite-full.mp4`다. 완성한 컬러 영상의 채도를 제거했으며 재검색·재선택 없이 시간축과 오디오를 유지했다. 컬러 원본도 보존한다.

### 11. 밝기 연속 선택과 여백을 줄인 흑백 전곡

흑백 제작은 완료한 형태 후보 안에서 색감 대신 밝기·명암 분포가 이어지는 경로를 고른다. 검색·형태 점수를 재계산하지 않는다. 설명 화면은 사진을 크게 배치하고 좌표·촬영일·출처를 하단 한 줄에 모으며 알고리즘명은 표시하지 않는다.

```powershell
uv run python -m badapple_earth.production.silhouette.monochrome --resume
```

설정은 `config/silhouette-luminance.json`, 출력은 `data/runs/p6-silhouette-luminance-full/`의 `final-16x9.mp4`와 `satellite-full.mp4`다. 중단하려면 같은 명령의 `--resume` 대신 `--pause`를 사용한다. 기존 컬러와 단순 흑백 변환 영상도 보존한다.

추가 30만 장의 준비가 완료된 150만 후보 전곡은 아래 명령으로 제작한다. 기존 120만의 2,880만 view 검색 결과와 프레임·view 점수를 재사용하고, 신규 720만 view만 검색한다. 형태 후보가 준비되면 명암 연속 선택과 두 흑백 영상 제작으로 이어진다.

```powershell
$env:OPENBLAS_NUM_THREADS='8'
$env:OMP_NUM_THREADS='8'
$env:NUMBA_NUM_THREADS='8'
uv run python -m badapple_earth.production.silhouette.expansion --resume
```

설정은 `config/produce-expand.json`이다. 중단은 같은 명령의 `--resume` 대신 `--pause`로 요청하며, 재개하면 저장한 검색·평가 배치·인코딩 구간을 사용한다. 형태 단계는 `data/runs/p6-silhouette-shape-1.5m-full/`, 최종 두 영상은 `data/runs/p6-silhouette-luminance-1.5m-full/`에 저장한다. 기존 완성 영상과 입력 MP4는 보존한다.

## 사용 방법

### 실제 사진의 이동·확대·회전을 맞추는 전곡

같은 사진의 프레이밍을 원본 실루엣 위치에 맞추려면 다음 두 단계를 실행한다. 기존 150만 후보의 특징·형태 점수·명암 선택을 재사용하고, 위치 변화에 덜 민감한 추가 검색→유효 범위의 이동·확대·회전 정합→원본 움직임을 따라 좋은 프레이밍 전파→명암·영상 변화의 시간축 선택→두 흑백 영상과 검증으로 이어간다. 완료된 단계는 재사용한다.

```powershell
$env:OPENBLAS_NUM_THREADS='8'
$env:OMP_NUM_THREADS='8'
$env:NUMBA_NUM_THREADS='8'
uv run python -m badapple_earth.production.registration.pipeline --resume
uv run python -m badapple_earth.production.registration.tracking --resume
```

설정은 순서대로 `config/registration-full.json`과 `config/tracking-full.json`이다. 최종 출력은 `data/runs/p6-tracked-1.5m-full/final-16x9.mp4`와 `satellite-full.mp4`, 정합 중간 결과는 `data/runs/p6-registered-1.5m-full/`이다. 실행 중인 단계에 `--pause`로 중단을 요청하고 같은 명령의 `--resume`으로 재개한다. 사진 밖의 패딩이나 생성 영역은 출력하지 않는다. 기준 영상은 보존한다. 실행·검증 상태는 [연속 프레이밍 기록](docs/history/2026-11-07-P6-continuous-framing.md)을 따른다.

기본 제작 흐름은 다음과 같다.

1. 로컬 MP4의 콘텐츠 비율·스트림·PTS를 확인하고 전체 프레임을 준비한다. 구간/규모 비교는 별도의 고정 10초 세트를 사용한다.
2. Sentinel-2에서 지형이 다양한 후보를 수집하고 품질·좌표·특징량을 저장한다.
3. 정확한 MSE Top-K를 검색하고 실제 사진·변환·출처를 보관한다. 규모 비교에서는 목표 세트와 점수 정의를 고정한다.
4. 최종 제작은 전체 프레임의 16:9 설명 화면과 사진 전용 영상으로 출력한다. 데이터셋은 가용 범위에서 확대하며 작은 표본의 낮은 시각 품질만으로 방식을 판정하지 않는다.

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

# Bad Apple Earth

Bad Apple!!의 각 프레임과 형태·명암이 닮은 **실제 위성·항공사진 한 장**을 찾아 영상을 재구성하는 로컬 제작 도구다. 그림을 새로 생성하거나 여러 장소를 합성하지 않고, 실제 지구에서 촬영된 장면 자체를 검색한다.

> **기본 제작은 모든 원본 프레임의 MSE 후보 검색과 MS-SSIM 재평가다.** 16:9 화면에는 작은 원본·후보 다섯 장·선택 연결선·큰 사진을 배치한다. 시간·프레임·점수는 작게 정돈하고 좌표·촬영일을 한 줄로 표시한다. 100만 장소·400만 회전 view와 기존 영상 캐시를 재사용한다. 실제 실행·재생 평가 상태는 [PLAN](docs/PLAN.md)에서 관리한다.

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

로컬 MP4와 수집한 `data/index/p5-1m/`가 있으면 다음 명령으로 모든 프레임을 처리한다.

```powershell
$env:OPENBLAS_NUM_THREADS = '8'
$env:OMP_NUM_THREADS = '8'
$env:NUMBA_NUM_THREADS = '8'
uv run badapple-earth produce --config config/production.json --output data/runs/p6-refined
```

같은 명령으로 packing·검색·사진 준비·완료 인코딩 segment를 재사용하며 이어간다. `config/production.json`에서 입력·인덱스·로컬 글꼴·인코딩 작업 수·오디오를 조정한다. 기본 글꼴 캐시가 없으면 Windows Segoe UI 또는 사용 가능한 DejaVu Sans를 찾고, 없으면 직접 지정한다. 글꼴 파일은 Git에 포함하지 않는다.

최종 출력은 `final-16x9.mp4`(1920×1080), `satellite-full.mp4`(원래 콘텐츠 비율의 사진 전용), `preview.png`, 전 프레임 `selected.parquet`·`frame-plan.parquet`, 전체 Top-20의 `search/matches.parquet`, `credits.json`이다. 후보는 MSE 순서대로 유지하고 MS-SSIM이 고른 위치를 강조한다. 타이틀·슬로건은 표시하지 않으며 시간·프레임 카운터·후보 점수와 사진 아래 좌표·촬영일만 간결하게 보여준다.

`selection_method`는 `ms-ssim` 또는 비교용 `mse`다. 재평가 대상은 표시할 다섯 장소이며 점수의 동률은 MSE 순서를 유지한다. 기본 `reuse_run`은 앞서 완성한 `data/runs/p6-full`의 타깃·검색·사진 캐시를 재사용한다. 이 run이 없는 환경에서는 `reuse_run`을 지우고 실행한다. `rerank/`에는 모든 프레임의 후보별 점수·선택·재개 상태가 남는다. 새 방법의 작품성은 실제 영상을 비교해 판단한다.

6,955개 원본 프레임을 모두 쓰고 30000/1001fps를 유지한다. 동일한 검색 특징은 계산만 공유하며 출력 프레임을 생략하거나 보간하지 않는다. 원본 AAC는 로컬 영상에 전체 복사하고, `audio_mode: "silent"`는 무음으로 만든다. 입력에 있는 약 65ms의 오디오 꼬리 때문에 영상 프레임을 추가하지 않는다. 이 경로는 현재 CFR·SAR 1:1 입력을 지원하며 외부 업로드는 하지 않는다.

후보 썸네일은 기존 RGB를 재사용한다. 큰 해상도는 선택된 큰 원본 창에만 요청하고, 실패하면 같은 view의 보관된 사진을 사용한다. 검색용 packing은 약 3.4GB의 추가 로컬 캐시이며 완료 후 재사용한다. 180프레임 단위로 두 결과를 인코딩해 재시작 시 완료한 구간을 다시 만들지 않는다.

### 선택 지표 비교

완료한 전곡 run의 Top-20 view를 재사용해 MSE·MS-SSIM·FSIM·다중 해상도 MSE·ZNCC를 같은 후보에서 비교한다. 지정 구간의 모든 원본 프레임을 사용하며 추가 검색·다운로드는 하지 않는다.

```powershell
$env:OPENBLAS_NUM_THREADS = '1'
$env:OMP_NUM_THREADS = '1'
uv run badapple-earth compare-metrics --run data/runs/p6-full --output data/runs/p6-metrics-top20 --start 38 --duration 10 --top-k 20 --workers 8
```

`comparison.mp4`는 위쪽 원본·MSE·MS-SSIM, 아래쪽 FSIM·Multi-MSE·ZNCC의 2×3 비교 영상이다. 알고리즘명·선택된 사진의 원래 MSE 순위·해당 지표 점수와 작은 시간·프레임 카운터를 표시한다. 무음이며 각 패널은 같은 canonical RGB를 사용한다. `evaluations.parquet`에는 모든 프레임×후보의 점수와 출처, `selected.parquet`에는 방법별 선택, `manifest.json`에는 설정·변경 수·선택 순위 분포·비용이 남는다. 같은 명령으로 완료 점수 배치를 재사용한다.

이 실험의 MS-SSIM은 Top-20 전체를 평가하므로 전곡 버전의 다섯 장소 재평가와 후보 범위가 다르다. 지표 간 점수는 단위가 달라 직접 비교할 수 없다. FSIM 포트는 연구·교육 목적 이용 조건을 따르며 [고지](THIRD_PARTY_NOTICES.md)와 [계산 설정](docs/TECH.md#53-점수-조합과-반복-제어)을 참고한다. 전곡의 기본 선택 설정은 이 비교 실행으로 바뀌지 않는다.

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

### 7. 선택 실험: 형태 우선 검색과 같은 구간 비교

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

## 사용 방법

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

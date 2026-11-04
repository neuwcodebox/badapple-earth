# 30°·작은 크롭 인덱싱과 비교 검색 재개

관련 요구·단계는 F5–F7, F9–F13, N1–N7, P5–P6다. 사용자가 2026-11-04에 명시적으로 재개를 요청했다. 전날의 [일시중지 인계](2026-11-03-P6-views30-paused.md)는 당시 중단 기록이며 현재 실행 여부는 이 문서와 PLAN을 따른다.

## 실행과 현재 범위

`.local/aerial-pause.json`을 재개 시각과 함께 `.local/aerial-resumed.json`에 보존한 뒤 표식을 제거했다. NAIP 수집은 64,673개부터, 특징 생성은 1,213,340개부터 재개했다. LINZ의 완료 10만은 다시 받지 않았다. 수집 감독은 `.local/run_aerial_batch.py`, 특징 생성은 `.local/build_variants.py`, 비교는 `.local/compare_variants.py`다. 복구용 이전 제어기는 재실행하지 않았다.

2026-11-04 **19:22 KST** 관측:

- LINZ 100,000개 완료, NAIP 66,506개 저장. 수집 감독은 NAIP 완료 후 고유 120만 스냅샷 결합·기존 고정 60타깃 비교로 이어진다.
- 고정된 1,131,800개 부모 사진에서 **5,390,960/22,636,000개** 새 view 특징 저장. 12개 프로세스를 사용한다. 회전과 크롭은 새 장소·새 촬영 수가 아니다.
- 비교 검색은 **1,252,860/22,636,000개** 새 view와 실제 300개 질의를 처리했다. 완료된 그룹만 소비하며 특징 생성과 병행한다. 4 BLAS 스레드다.
- CPU 약 25.5%, RAM 여유 약 12.65GiB, D 드라이브 약 170GiB 여유를 관측했다. 특징 생성은 캐시 읽기 대기가 커 workers만 늘리지 않았다.
- 특징 생성의 재시작 이후 평균 약 3,767 view/s로 완료 추정은 20:39 KST, 비교 영상은 약 20:50–21:15다. NAIP는 현재 수집 지역에서 약 1.43장/s로, 단순 외삽은 다음날 01:53이다. 네트워크·지역·I/O에 따라 달라지는 추정이며 완료 표시가 아니다.

## 구현과 비용

30°마다 두 크기를 만든다. 기본 크기의 기존 직각 네 view를 재사용하므로 추가는 20 view/부모다. 새 특징은 RGB가 아닌 uint8 32×24만 저장하며 약 17.38GB 파일을 사전 할당했다. 실제로 완료된 그룹은 manifest 목록으로만 판별한다. 그룹 공개는 비순차적이므로 완료 수를 유효 prefix로 사용하지 않는다.

큰 정적 descriptor 목록을 `inventory.json`으로 분리해 매 완료 그룹의 진행 manifest를 약 11.6MB에서 약 3KB로 줄였다. worker는 RGB 위치에 필요한 Parquet 컬럼만 읽는다. 그룹 주소·부모 ID·지리 사각형·변환 recipe를 연결하고, 원격 fallback 없이 로컬 RGB를 사용한다.

검색은 같은 float64 BLAS 내적을 두 후보군에 공유한다. 기본 크기 비직각 view만 추가한 Top-20과 작은 크롭까지 포함한 Top-20을 각각 기존 직각 Top-20과 SSE·view ID 순서로 병합한다. 부모의 수는 늘리지 않으며 기존 dense row 뒤에 새 행을 붙인다. MS-SSIM은 같은 canonical 크기와 기존 정의를 사용하고, 기존 평가 쌍은 보존된 행 범위에서만 재사용한다. view-space ID가 다른 점수를 지리 인덱스 ID만으로 재사용하지 않는다. 혼합 Arrow 테이블은 모든 행의 컬럼 합집합을 명시해 첫 기준선 행 때문에 뒤의 affine provenance가 누락되지 않게 했다.

새 검색 루프의 JIT 초기화가 llvmlite NRT 최적화/기계어 게시 단계에서 지연됐다. 기존 Numba scanner를 재사용해도 동일 초기화 지연이 관측됐고, 일반 권한 검증에서도 즉시 해결되지 않았다. 해당 미완료 검증·검색 프로세스를 종료하고, **정수 SSE·전역 tie rank를 int64 복합 키로 묶는 NumPy Top-K**로 전환했다. float64 내적·int32 SSE의 정확성은 유지한다. 미완료 JIT 검증을 통과한 것으로 세지 않는다.

## 검증과 미완료

- `tests/test_affine_views.py`, `tests/test_variant_bank.py`, `tests/test_incremental_collection.py`: **43개 통과**. 모든 각도에서 검은 padding 부재, RGB 변환/검색 명암의 정확한 픽셀 일치, 75% 크롭 포함 관계·면적, 실제 source quad/지리 footprint, 비순차 그룹 중단·재개, 두 후보군의 정수 전수 Top-K·동률, 점수 캐시의 view-space 격리를 확인했다.
- `tests/test_variant_comparison.py`: **2개 통과**. 같은 장소의 추가 view 병합·기존 dense 주소 유지와 기준선 행 뒤의 affine metadata 보존을 확인했다.
- `badapple-earth --help`에서 `views-index`, `compare-views`가 노출되는 것을 확인했다. 두 명령은 `config/views30.json`을 사용한다.
- 비교 검색의 실제 진행 체크포인트를 확인했다. **전체 bank·최종 10초 비교 영상·새 회전 전곡은 아직 미완료**다. 기존 중간 전곡을 새 회전 결과로 보고하지 않는다. 최종 영상은 실제 프레임 수·PTS·출처·변환을 확인하고 재생 품질은 별도로 평가한다.

## 확인·재개 위치

- 특징: `data/variants/p5-interim-angle30-crop75/manifest.json`, `inventory.json`, `gray_fine.npy`.
- 비교: `data/runs/p6-views30-comparison/manifest.json`, `search-progress.json`, `retrieval.npz`. 완료 후 `candidates.parquet`, `shortlists.parquet`, `evaluations.parquet`, `selected.parquet`, `comparison.mp4`, `preview.png`로 이어진다.
- 수집: 각 `data/index/p5-{naip,linz}-100k/part-*/manifest.json`, 각 part 로그, `.local/aerial-batch.json`.
- 시각별 관측: `.local/resume-current.json`.

동일 작업이 실행 중이면 새 writer를 시작하지 않는다. 중단된 경우 사용자 의도에 따라 아래 명령으로 같은 결과를 재개할 수 있다.

```powershell
uv run badapple-earth views-index --config config/views30.json
```

```powershell
$env:OPENBLAS_NUM_THREADS='4'
$env:OMP_NUM_THREADS='4'
uv run badapple-earth compare-views --config config/views30.json --output data/runs/p6-views30-comparison
```

기존 원본 MP4·100만 데이터·Astra 전곡·113만 중간 전곡·별도 수동 검토 변경은 보존한다. 유료 접근·클라우드 실행·외부 업로드·자동 일정은 시작하지 않았다.

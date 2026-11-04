# 30° 회전·75% 크롭 작업 일시중지 인계

사용자가 다음날 이어서 하도록 명시적으로 일시중지를 요청했다. **2026-11-03 22:56:35 KST**에 수집·view 인덱싱·자동 후속 제어기를 모두 중지했다. 관련 exec 세션에 종료 신호를 보냈고 수집 감독 두 개·각 part 네 개·새 view bank의 **일곱 writer 잠금을 모두 획득할 수 있어 writer가 종료됐음을 확인**했다. 사용자 재개 요청 없이 자동 실행하지 않는다.

## 보존한 상태

- LINZ: 100,000개 완료, NAIP: 두 part 32,751+31,922 = **64,673개**. manifest 기준 추가 수집 합계 **164,673개**다. SQLite·완료 RGB/특징 shard·원본 블록 체크포인트는 유지했다.
- 30°와 작은 크롭: 고정된 기존 1,131,800개 장소를 대상으로 새 view **1,213,340/22,636,000개(약 5.36%)**, 작업 그룹 **51/1,255개**를 저장했다. 12개 프로세스를 사용했다.
- 완료된 113만 중간 전곡은 `data/runs/p6-aerial-interim/`에 보존한다. 새 회전/크롭 비교 영상은 아직 없다.
- 일시중지 표식은 `.local/aerial-pause.json`이다. `.local/aerial-batch.json`, `.local/aerial-recovery.json`, 미완료 part와 view bank manifest도 `paused`로 기록했다. 로컬 실행 스크립트 세 개는 표식이 있으면 실행하지 않는다.

## 이번 요청과 현재 코드

사용자는 **30° 간격 회전과 작은 크롭 한 단계**를 요청했고, 후속으로 **CPU를 최대한 활용하는 병렬 처리**와 **예상 완료 시각을 항상 안내**하도록 요청했다. 작은 크롭은 회전 후 유효 영역의 가로·세로 75%로 설정했다. 12방향 × 2크기 = 총 24 view/장소 중 기존 네 직각 방향·기본 크기는 재사용하므로 추가 view는 20개/장소다. 새 장소·새 촬영을 추가한 수량으로 보고하지 않는다.

아직 미커밋된 구현:

- `config/views30.json`: 고정 입력 인덱스·원본 packing 두 개·30°·75%·12 workers·디스크 여유 하한.
- `matching/affine.py`: 실제 사진 안쪽의 최대 비율 유지 사각형과 1px 여유, 시계 방향 affine·정확한 지리 사각형·크롭/변환 metadata. nearest 좌표 매핑을 사용한다. RGB→L과 nearest 매핑의 교환 성질을 활용해 검색 특징 생성 비용을 줄이는 구현이다.
- `production/variants/bank.py`: 원본 RGB와 metadata를 참조하고 새 32×24 명암 특징만 저장하는 재개 가능한 bank. 기존 parent packing·RGB 캐시를 재사용하며 원격 읽기 fallback은 없다.
- `matching/views.py`, `production/grid/rerank.py`, `production/assets.py`: 신규 affine view를 실제 사진 출력·점수 평가에 연결하기 시작했다. 기존 후보 표시·수동 검토의 미커밋 변경과 같은 파일에 공존하므로 전체 파일을 되돌리거나 전부 이번 작업으로 커밋하지 않는다.
- `.local/build_variants.py`: 현재 실행한 병렬 인덱싱 스크립트. Git 제외 로컬 파일이다.

좌표·흰 이미지의 유효 영역 스모크 검사와 변환 속도 측정만 했다. **정식 계약 테스트, 추가 view 검색, 기존/30°/30°+작은 크롭의 비교 선택·영상은 아직 미완료**다. 기존 27개·7개 통과 기록은 이전 중간 전곡의 검증이며 이 신규 기능의 테스트 통과로 사용하지 않는다.

## 재개 시 주의와 우선 작업

1. 명시적인 사용자 재개 요청을 받은 뒤 `.local/aerial-pause.json`을 제거한다. 기존 자동 복구 제어기를 다시 시작할 필요는 없다. 수집은 `uv run python .local/run_aerial_batch.py`, 새 view 생성은 `uv run python .local/build_variants.py`로 기존 체크포인트를 사용한다.
2. 새 bank는 `data/variants/p5-interim-angle30-crop75/`다. **`gray_fine.npy`는 전체 용량을 사전 할당했으므로 파일 크기가 완료 수량을 뜻하지 않는다.** 완료 그룹 목록 `manifest.json.completed`에 속하는 범위만 사용할 수 있으며 그룹은 비순차적으로 완료됐다. 미완료 영역을 검색 후보로 사용하지 않는다.
3. `bank.py`가 약 11.6MB의 정적인 descriptor 목록을 매 그룹 완료 때 manifest와 함께 다시 쓰고 있다. 정적 inventory와 작은 진행 상태를 분리하고, worker의 Parquet 읽기는 필요한 컬럼만 읽도록 개선할 여지가 있다. 당시 CPU 약 13%로 캐시 I/O 대기가 많았으므로 workers 수만 늘려 효율을 주장하지 않는다. CPU·RAM·I/O를 측정하며 검색을 병행하는 소비자도 연결한다.
4. nearest RGB warp→L→BOX와 L→nearest warp→BOX가 정확히 같은 특징을 만드는 테스트, 모든 각도의 검은 패딩 부재·크롭 포함 관계·지리 footprint·실제 출력/검색 변환 일치·중단 재개 테스트를 먼저 만든다.
5. `production/variants`에는 비교/검색 소비자 구현이 아직 없다. 추가 view의 정확한 MSE Top-20을 기존 결과와 합치되, 같은 장소의 추가 각도이므로 기존 `grid/incremental.py`의 서로 다른 장소 합집합 검증을 그대로 사용할 수 없다. 기존 dense row를 유지하고 새 view에 offset을 주며 회전만/회전+작은 크롭의 효과를 구분한다. 기존 MS-SSIM 점수·사진 캐시도 재사용한다.
6. 우선 같은 38–48초 구간의 모든 원본 프레임으로 비교하고 특징 bank를 전곡에도 재사용할 수 있게 한다. 후보 제한 실험과 전체 후보 검색을 혼동하지 않는다. 미완료·미검증 상태와 추정 시간을 명확히 구분한다.

이번 중지는 삭제나 완료가 아니다. 원본 MP4·실제 사진·완료 특징·기존 영상·미커밋 코드를 모두 유지했다.

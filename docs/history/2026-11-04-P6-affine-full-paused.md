# 120만·30°·작은 크롭 전곡 제작과 일시중지 인계

관련 요구·단계는 F1, F5–F14, N1–N7, P5–P6다. 사용자가 기준선 비교를 중단하고 바로 영상 제작을 요청해 2026-11-04 22:22 KST에 기존 비교 감독을 종료했다. 이후 22:40에 다음날 이어서 하도록 명시적으로 일시중지를 요청했다. **현재는 중지 상태이며 사용자 재개 요청 없이 실행하지 않는다.**

## 보존 상태

- 수집은 NAIP·LINZ 각 100,000개로 완료했고 `data/index/p5-aerial-1.2m`은 기존 Sentinel-2와 고유 **1,200,000개**다. 기존 113만 중간 전곡·Astra 전곡·입력 MP4는 유지했다.
- 전체 120만 장소의 30°·75% 크롭 인덱스도 완료했다. 113만의 기존 affine **22,636,000개**, 나중에 수집한 **68,200개** 부모의 새 affine **1,364,000개**, 전체 직각 view **4,800,000개**를 합쳐 **28,800,000 view**다. 추가 변환을 새 장소로 세지 않는다.
- `data/runs/p6-aerial120-views30-full/`은 검색 단계에서 일시중지했다. `search/existing-affine/progress.json`과 `retrieval.npz`에 **3,538,944/22,636,000개**, 6,123개 검색 질의의 Top-20 상태가 저장됐다. 전체 원본 6,955프레임의 고유 fine 질의는 6,423개이고, 나머지 300개 질의의 기존 affine 검색 결과는 완료한 10초 비교에서 재사용한다.
- 제작 설정은 `config/production-aerial120-views30.json`이다. 전곡 MS-SSIM 선택, 원본 PTS/오디오, 1920×1080 설명 화면과 1444×1080 사진 전용 영상, 검색 BLAS 8스레드·정밀 평가 8 workers·native asset 8 threads·렌더 4 workers×인코더 2 threads다. **전곡 검색·선택·렌더가 아직 미완료이며 새 전곡 결과 파일은 없다.**
- `.local/aerial-pause.json`·`.local/resume-current.json`에 `paused`, `auto_resume:false`를 기록했다. 전곡 run manifest도 `paused`다. 전곡 제작·추가 특징 생성 스크립트는 표식이 있으면 실행하지 않는다.
- 제작 exec 세션을 종료했고 추가 인덱싱 세션은 이미 정상 완료했다. 제작 run·두 bank·두 수집 감독의 **다섯 writer 잠금을 모두 획득**했으며 관련 Python 프로세스가 남지 않은 것을 읽기 전용으로 확인했다. 자동 일정은 만들지 않았다.

## 구현과 재사용

`production/variants/expansion.py`는 이전/완료 인덱스의 불변 shard 목록 차이를 검증해 새 68,200개의 참조 인덱스를 만들었다. 기존 113만 metadata나 정상 RGB를 다시 받거나 전수 스캔하지 않는다. 신규 직각 packing은 `data/packed/p5-aerial-last68200`, fine은 그 경로의 `-fine`, 새 bank는 `data/variants/p5-aerial-last68200-angle30-crop75`다. 설정은 `config/views30-delta.json`이다. 추가 bank는 22:31:51 KST에 완료했다.

`production/variants/full.py`는 기존 전곡의 직각 Top-20·dense 주소를 유지하고, 기존 affine bank·신규 직각·신규 affine의 정확한 MSE Top-20을 차례로 병합한다. 이전/최종 snapshot의 출처 ID는 같은 실제 source path와 보존된 shard를 확인해 remap한다. 각 단계는 후보 배치 단위로 원자적 체크포인트를 저장한다. 전체 view 수가 120만×24와 다르면 완료 처리하지 않는다.

기존 전곡과 10초 affine 비교의 MS-SSIM 점수는 검증된 `preserved_view_spaces`의 dense 범위에서만 재사용한다. 혼합 후보/선택 테이블은 전체 컬럼 합집합을 저장해 기준선 행 뒤의 affine provenance가 빠지지 않게 했다. 현재 전곡 검색이 끝나지 않아 실제 새 전곡 점수 재사용량은 아직 미측정이다.

검색의 전체 int64 복합 키 partition 비용을 줄였다. int32 SSE로 먼저 Top-K를 뽑고 경계 동률만 전역 view rank로 정확하게 해소한다. 최종 K개는 기존 SSE·rank 순서로 정렬하므로 저장된 이전 검색 체크포인트와 같은 결과다. 실제 1,024질의×32,768 view의 제한 측정에서 4 BLAS 스레드·128 타깃 배치의 합계는 약 1.50초→1.07초였다. 전곡 입력은 유지한 채 저장된 지점부터 이 구현으로 재개해 실제 진행을 확인했다. 전체 성능이나 영상 품질 개선을 이 제한 측정으로 단정하지 않는다.

## 검증과 남은 주의

- 최초 영향 범위 테스트 **18개 통과**: 신규 shard 차집합, 전수 정수 SSE와 결과 일치, 캐시 질의·중단 재개, 기존 수집 재개·표시 계약·점수 재사용이다.
- Top-K 최적화 후 은행·표 직렬화·새 전곡 계약 **10개 통과**. 첫 실패는 새 테스트를 삽입하며 resume 검증 구문을 다른 테스트에 둔 `NameError`였고 위치를 바로잡았다. 알고리즘 결과 불일치가 아니었다.
- 최종 전곡 검색 통합 계약 **5개 통과**: 합성 테스트의 두 장소×24 view 전수 결과와 정확히 일치하고, dense 주소·view ID·frozen source ID remap을 확인했다. 같은 점수의 큰 경계 집합도 k=1/20에서 전역 순서를 유지했다.
- 위 합성 데이터는 테스트 전용이며 실제 수집·영상 성과로 세지 않는다. 사용자 중지 요청 뒤 새 테스트·특징 생성은 실행하지 않았다.
- 새 제작의 실제 전곡 결과·프레임 수·PTS·오디오·출처·변환·전체 재생 품질은 아직 검증 전이다. 완료된 10초 영상은 별도 `data/runs/p6-views30-comparison/`에 있다.
- 향후 이 새 전곡의 점수 캐시를 다시 재사용하려면 production 최상위 manifest에 `search.view_space_id`도 전파해야 한다. 현재 검색 결과에 이 ID를 쓰지만 일반 production 최상위 manifest에는 아직 전파하지 않는다. 새 전곡 제작 자체의 입력 캐시 재사용은 별도 검증된 범위를 사용한다. 이 작은 후속 수정은 중지 요청으로 아직 수행하지 않았다.
- 공유 `production/assets.py`, `cli.py`, `pipeline.py` 등에는 이전 수동 검토·표시 변경이 있다. 이번 작업으로 전체 파일을 스테이징하거나 되돌리지 않는다.

## 명시적 재개 요청 후

1. `.local/aerial-pause.json`을 보존/제거하고 run의 같은 설정을 유지한다. 수집·추가 bank는 완료했으므로 `.local/run_aerial_batch.py`와 `.local/prepare_views_delta.py`를 실행할 필요가 없다. 사용자가 취소한 기준선 비교도 재실행하지 않는다.
2. `uv run python .local/produce_aerial120_views30.py`로 동일 run을 재개한다. 스크립트는 8스레드 환경을 설정하고 일반 `produce`를 실행한다. 직접 CLI는 `uv run badapple-earth produce --config config/production-aerial120-views30.json --output data/runs/p6-aerial120-views30-full`이다.
3. 현재 체크포인트 이후의 기존 affine·신규 직각·신규 affine 검색, 기존 점수 재사용과 신규 MS-SSIM 평가, 필요한 사진만 재사용/읽기, 두 영상 인코딩과 원본 AAC mux로 이어진다. 완료 후 실제 프레임 수·원래 PTS·오디오와 선택 provenance를 확인한다.
4. 재개 직후 실제 속도로 ETA를 다시 계산한다. 중지 직전 기존 affine 검색은 약 3,538,944개/누적 531.776초였고 최적화 뒤 최근 처리 속도는 약 7,800 view/s였다. 잔여 전체 제작은 대략 1시간 안팎 이상으로 보되 네트워크 선택 사진 읽기·렌더 비용을 측정해 갱신한다. 이전 날짜의 완료 시각 추정을 그대로 쓰지 않는다.

로그는 `.local/produce-aerial120-views30-resumed.log`, 추가 bank 완료 로그는 `.local/prepare-views-delta.log`다. 새 전곡은 아직 없으며 이번 중지는 삭제나 완료 표시가 아니다.

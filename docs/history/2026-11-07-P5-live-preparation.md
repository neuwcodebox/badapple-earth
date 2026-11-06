# P5 — 수집 재개와 검색 인덱스 병행 준비

관련 요구: F3–F7, F10–F13, N2–N6. 사용자가 11월 7일 수집 재개를 요청하고, 외출 중 수집만 기다리지 말고 인덱싱도 진행하도록 지시했다. 기존 사진·영상은 보존하며 신규 데이터만 준비한다.

## 실행 범위

- **08:39 KST**에 `.local/run_expansion_20261106.py --resume`으로 기존 **86,321개**와 체크포인트에서 수집을 재개했다. 사용자 일시중지 표식을 명시적으로 해제했고 새 수집량 증가를 확인했다.
- 수집 시 함께 저장하는 기본 특징·직각 세 회전 외에, **검색용 특징 packing → 32×24 fine 배열 → 30° 회전·75% 크롭의 추가 20 view**를 완료 shard부터 병행 준비한다. 부모 사진당 총 24 view이며 회전/크롭을 새 장소로 세지 않는다.
- 입력은 새 여덟 part뿐이다. 기존 120만의 RGB·완료 인덱스·전곡을 다시 계산하지 않는다. 실제 신규 검색·형태 점수 평가·영상 제작은 이번 실행 범위에 넣지 않았다. 준비한 명암 특징 배열은 이후 기존 MSE/형태 검색 경로에서 재사용한다.

## 구현과 자원

`production/variants/preparation.py`가 `config/prepare-expand.json`에 따라 공개 shard descriptor만 확인하고, 미처리 shard를 약 2만 부모 단위의 불변 참조 스냅샷으로 고정한다. 미처리량 5천 미만은 수집을 기다렸다 묶고 마지막 잔량은 수집 종료 후 처리한다. 처리 목록에는 source index ID와 shard ID를 기록해 같은 shard의 재처리를 피한다. 사진 간 중복 검사나 전체 타일 표 재검사는 하지 않는다.

각 묶음은 기존 `pack`, `pack_fine`, affine `Bank`를 사용한다. 원본 RGB는 수집 shard에서 참조하며 파생 인덱스만 쓴다. 파일 수·행 순서·source row·view 변환·출처 연결을 유지한다. tie 정렬은 검색의 결정성을 위한 것이며 중복 사진 제거가 아니다. Bank의 추가 부모 ID 유일성 검사는 제거했다.

인덱싱 CPU 작업은 **4개**, 수집은 기존 네 writer다. 인덱싱에도 여유 공간 하한 **30GiB**를 적용한다. 전체 30만에 필요한 추가 회전 특징만 uint8 약 4.3GiB이며 packing·fine·ID·메타데이터가 별도로 필요하다. RGB 전체 복사·새 원격 다운로드·GPU·클라우드 실행은 하지 않는다.

## 중단·재개와 진행 확인

```powershell
uv run python .local/run_expansion_20261106.py --resume
uv run python -m badapple_earth.production.variants.preparation --config config/prepare-expand.json
```

첫 명령은 수집, 둘째는 인덱싱이다. 이미 실행 중인 writer에는 중복 실행하지 않는다. 현재 세션의 실행 참조는 수집 **9364**, 인덱싱 **21464**이며 재부팅 뒤에는 이 번호를 재사용하지 않는다. 수집 제어기 상태는 `.local/expansion-20261106.json`, 인덱싱 상태는 `data/prepared/p5-expand-300k/manifest.json`이다. 개별 묶음의 `packed/`, `fine/`, `bank/manifest.json`·`build-progress.json`으로 단계와 공개된 view를 확인한다. 코드·설정·테스트는 Git에, RGB·파생 인덱스·로컬 제어기는 로컬에 둔다.

두 작업 모두 `.local/expansion-20261106.pause.json`을 공유한다. 사용자 일시중지 요청 시 이 표식을 만들고 수집 제어기를 멈추며 인덱서도 확인한다. affine builder는 새 그룹 배정을 중단하고 실행 중인 그룹을 저장한 뒤 `paused`로 끝난다. packing·fine은 단계/체크포인트 경계에서 재개한다. 강제 종료·컴퓨터 종료 후에도 공개 완료분과 저장한 행 매핑·그룹 체크포인트를 재사용한다. 재개 요청 전에는 일시중지 표식을 제거하지 않는다.

인덱싱은 수집을 따라가며 새 shard가 나오면 계속 준비하고, 수집 제어기가 끝나면 마지막 잔량까지 처리한다. 수집이 부분 실패로 끝나면 준비한 부분도 `partial`로 기록한다. 준비가 끝났다는 사실을 실제 매칭·재생 개선으로 보고하지 않는다.

## 검증과 실제 상태

`tests/test_live_preparation.py`, `tests/test_variant_bank.py`, `tests/test_affine_views.py`, `tests/test_incremental_collection.py`의 **47개 테스트가 통과**했다. 타일 표를 열지 않는 shard 고정·이후 추가분만 선택·사용자 pause 준수와 기존 실제 RGB 변환/행 매핑/그룹 재개 계약을 검사했다. 기존 affine 위치 계산의 라이브러리 경고는 남아 있다.

**08:44 KST**에 실제 인덱싱을 시작했다. 첫 스냅샷 **20,013개 부모**의 직각 packing·fine **80,052개 view**, affine 추가 **400,260개 view**가 모두 완료됐다. 합계 **480,312개 view**이며 Bank manifest의 원격 읽기는 0이다. 다음 미처리 shard를 자동으로 이어간다. 전체 30만의 준비 완료나 새 전곡 제작 완료를 의미하지 않는다.

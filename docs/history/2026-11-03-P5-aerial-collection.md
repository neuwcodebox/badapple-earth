# P5 — NAIP·LINZ 항공사진 20만 후보 추가 수집

관련 요구: F2–F7, F9–F13, N1–N4, N6. 사용자가 NAIP 10만·LINZ 10만 추가 수집을 승인했다. 기존 Sentinel-2 100만과 전곡 결과를 보존하고 항공사진으로 다른 지상 범위의 후보를 보강한다.

## 구성과 구현

- `config/collect-naip.json`: 미국 36개 AOI, 도시·농경지·숲·하천·해안·건조 지역의 탐색 분류. AOI당 최대 100개 STAC item을 조사하고 같은 quarter-quad의 촬영 중복을 제한해 최대 6개를 선택한다. 날짜 범위는 2015–2023년이며 최신 촬영을 우선한다. 탐색 분류를 각 사진의 실제 토지피복 라벨로 사용하지 않는다.
- `config/collect-linz.json`: 뉴질랜드 13개 지역의 23개 0.3m RGB 촬영 사업. 각 사업의 item 링크를 seed로 섞어 최대 20개 메타데이터를 확인하고 최대 12개를 선택한다. 사업 목록·bbox는 실제 공개 STAC에서 고정했다. 6cm 자료와 RGBNIR 중복 컬렉션은 이번 계획에서 제외했다. LINZ의 `terrain_group`은 지역별 할당을 위한 지리 분류다.
- 공통 원본 폭은 256/512/1024/2048px, 초기 가중치는 20:30:35:15다. NAIP 0.6m에서 약 154–1229m, LINZ 0.3m에서 약 77–614m다. 처음부터 큰 창이 부족하면 기존 스케일 재배분을 사용하며 실제 분포를 최종 manifest에 남긴다.
- 소스별 두 독립 작업에 50,000개씩 할당해 총 네 작업을 실행한다. 작업별 RAM 상한 3GiB·인덱스 디스크 예산 24GiB·공유 드라이브 여유 하한 20GiB다. 시작 전 D 드라이브 여유는 약 114.3GiB였다. 기본 RGB 20만은 약 27.3GiB이며 특징·출처·일시 블록이 별도로 필요하다. 이 값은 예상 저장량이다.

`sources/aerial.py`에서 공개 카탈로그·무서명 자산·임시 SAS를 해석하고 기존 수집기의 블록·SQLite 체크포인트·불변 shard·RGB 캐시·직각 회전 특징을 재사용했다. 사용자 키·AWS 자격증명·Requester Pays·클라우드 실행은 사용하지 않는다. SAS는 메모리에만 있으며 승인한 호스트의 무서명 HTTPS 자산만 저장한다. 429/일시 서버 오류 재시도는 제한한다.

RGB 1·2·3번을 원래 uint8로 읽고 dataset mask를 사용한다. 구름 품질은 `unassessed`, 구름 비율·SCL 결측률은 `null`로 두며 SCL을 만들어내지 않는다. 원본 asset key·URL·밴드·CRS·원본 창·촬영 단일 시점/구간·licensor·회전/크롭을 추적한다. LINZ의 반복되는 도엽 ID는 collection별 지역 scene ID로 구분한다. 서로 다른 인덱스의 선택 필드가 Arrow 변환에서 빠지지 않도록 검색 결과의 전체 필드를 보존하며 구간 출력 크레딧은 실제 소스와 licensor를 반영한다.

## 검증과 실제 시작

네트워크 없는 수집·재개·RGB/alpha·구름 미평가·촬영 구간·출처·회전·혼합 인덱스 검색 계약 테스트 **39개가 통과**했다.

```bash
uv run pytest tests/test_aerial_collection.py tests/test_collection.py tests/test_expansion.py tests/test_parallel_collection.py tests/test_imagery.py tests/test_rotations.py tests/test_matching.py -q --basetemp .local/pytest-aerial-final
```

실제 수집 경로의 첫 블록에서 NAIP 443개·LINZ 345개를 저장한 뒤 체크포인트에서 이어갔다. 저장한 무작위 표본 contact sheet 일부에서 실제 농경지·도로·숲·수면과 스케일 변화가 표시되는 것을 확인했다. 전수 구름 검사나 Bad Apple 형태/재생 개선을 확인한 결과는 아니다.

2026-11-03 **19:22:50 KST** 중간 관측은 NAIP 4,080개·LINZ 5,642개, 합계 **9,722개**다. 네 작업의 관측 RSS 최대는 약 325–347MiB, 장면 읽기 실패 기록은 0개다. 값은 진행 중 스냅샷이며 최종 수량·저장량·총 실행시간이 아니다. 소스별 manifest의 `status`는 실행 종료 시 갱신되므로 실행 중에는 이전 `paused`가 남을 수 있다. 현재 행 수·로그 증가와 제어기 상태를 함께 본다. 최종 통계와 매칭 비교는 아직 대기 중이다.

## 수집 완료와 후속 비교 상태

2026-11-04 **22:11:09 KST**에 NAIP 병렬 수집이 종료 코드 0·총 **100,000개**로 완료됐다. 각 part는 50,000개이며, 먼저 완료된 LINZ 두 part의 100,000개와 합쳐 실제 신규 사진 **200,000개**다. 완료한 RGB·특징·출처를 확인하려고 다시 다운로드하지 않았다.

네 추가 part를 `data/index/p5-aerial-final-new`로 고정하고, 기존 `data/index/p5-1m`과 서로 다른 소스 namespace의 참조 합집합으로 `data/index/p5-aerial-1.2m`을 만들었다. 고유 **1,200,000개**, 중복 제외 0개이며 인덱스 ID는 `de3d0aadcfc48d09c6570d9d2938533fe27484b1ea1fe79d69faf3bdab4f2c16`이다.

22:19 기준 `.local/aerial-batch.json`은 `comparing`이다. 22:12:51부터 고정 60타깃의 기존 MSE 비교를 계산 중이며 실제 자식 프로세스의 CPU·읽기/쓰기 활동을 확인했다. 네 추가 snapshot의 직각 view descriptor 준비는 모두 완료됐다. 검색 결과 manifest와 최종 baseline 비교는 아직 게시되지 않았으므로 전체 후속 처리가 완료됐다고 표시하지 않는다. 검색 구간별 진행량은 기록하지 않아 종료 추정은 대략적인 범위로 안내한다. 같은 부모 사진의 30°·작은 크롭 비교는 별도 [회전 결과 기록](2026-11-04-P6-views30-resumed.md)을 따른다.

## 실행·재개와 후속 처리

```bash
uv run badapple-earth collect-parallel --config config/collect-naip.json --output data/index/p5-naip-100k --limit 100000 --workers 2
uv run badapple-earth collect-parallel --config config/collect-linz.json --output data/index/p5-linz-100k --limit 100000 --workers 2
```

동일 명령으로 재개하며 이미 저장한 RGB·완료 블록을 확인용으로 재다운로드하지 않는다. 소스별 `collection.json`, `part-01.log`·`part-02.log`, 각 part의 `manifest.json`·`jobs.sqlite`가 근거다.

이번 로컬 실행 제어기는 `.local/run_aerial_batch.py`, 상태는 `.local/aerial-batch.json`, 소스 로그는 `.local/aerial-{naip,linz}.log`다. 각 10만 완료와 종료 코드를 확인한 뒤 기존 100만과 네 part를 참조 결합해 `data/index/p5-aerial-1.2m`을 만든다. 고유 수량이 1,200,000개가 아니면 완성된 120만으로 후속 처리하지 않는다.

이후 기존 38–48초·60개 목표 `data/targets/38s-10s-6fps`에서 `match`·`materialize`·`render`·`compare`를 이어간다. 출력 예정 경로는 `data/runs/p5-aerial-1.2m`, 비교 기준은 `data/runs/p5-1m`이다. **16×12 MSE·Top-20·네 방향·24fps hold·10초 무음**이라는 기존 규모 비교 조건을 유지한다. 이는 데이터 확대의 고정 기준선 비교이며 현재 32×24/Astra 전곡의 제작 설정을 바꾸는 작업이 아니다. 실제 선택 소스·점수·캐시 실패·영상을 확인한 뒤 효과를 해석한다.

## 시작 과정에서 수정한 문제

- 첫 익명 JSON 요청에 빈 Basic 인증을 보내 S3가 403을 반환했다. 인증 헤더와 암묵적 netrc 인증을 보내지 않도록 수정하고 회귀 테스트를 추가했다.
- 로컬 연결 확인 스크립트의 `read_text()`가 Windows cp949로 UTF-8의 `Manawatū`를 읽어 한 collection 설명 문자열을 다르게 저장했다. 이후 정규 CLI의 UTF-8 설정과 일치하지 않아 LINZ part-01 재개가 거부됐다. 실제 URL·bbox·촬영 자료는 같았다. 해당 설명을 바로잡고 manifest와 유일한 완료 shard의 config identity를 일치시켰으며 345개의 RGB·특징·출처·원본 블록은 유지했다. 로컬 스크립트도 명시적 UTF-8로 수정하고 part-01을 따로 재개했다.
- 최초 병렬 제어기가 이미 실패한 part-01의 종료 코드를 관측했으므로 `.local/finish_aerial_recovery.py`를 함께 실행했다. 기존 병렬 작업과 별도 재개 part-01의 종료 뒤 같은 공개 명령으로 제어기를 다시 진입시켜 남은 체크포인트 또는 후속 결합·비교를 처리한다. 진행 중 인덱스에 두 writer를 열지 않는다. 복구 상태는 `.local/aerial-recovery.json`이며 24시간 기한 내 완료하지 못하면 체크포인트를 보존한 실패로 남긴다.
- 부족한 지역의 할당을 다른 지역으로 재배분해 총 목표 수를 채운 경우에도 재개가 원래 지역별 quota를 다시 요구하지 않도록, 완료 판정을 고유 행 수로 정리했다. 빈 지역과 재배분을 포함한 계약 테스트에서 목표 달성 후 카탈로그 접근 없이 종료함을 확인했으며 영향 범위 테스트 22개가 통과했다.
- 짧게 실행한 숨김 `Start-Process`는 환경에서 유지되지 않았다. 실제 장시간 실행은 지속 exec 세션에서 수행 중이다. 처리 여부는 프로세스 실행 요청 자체가 아니라 저장된 행·로그·완료 manifest로 확인한다.

위 실행 스크립트와 생성 데이터는 로컬 산출물이며 Git에는 넣지 않는다. 재개 가능한 정규 CLI·수집 설정·코드·테스트·문서는 Git에 보존한다. 20만은 상한이 아닌 중간 계측 지점이며 작은 표본의 시각 품질만으로 이후 확대를 중단하지 않는다.

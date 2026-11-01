# P4 — 고정 타깃의 10만 후보 확대

- **날짜:** 2026-11-01 (Asia/Seoul).
- **관련 요구:** F2–F11, F13, N1–N7.
- **상태:** 구현 완료, 실제 추가 수집 진행 중. 완료 수량과 결과는 실제 실행 후 기록한다.

## 실행 범위와 재사용

비교 기준은 `data/runs/p3-10k-rotations/`의 1만 장소·4만 view, 목표 60개, 38–48초 구간이다. MSE·특징·0/90/180/270도·회전/크롭 recipe는 그대로 유지한다. 새로운 지역에서 9만 후보를 모아 기존 1만과 결합하는 것이 목표다. 후보 수뿐 아니라 공간·스케일 범위도 넓어지므로 개선을 순수한 수량 효과로 해석하지 않는다.

새 AOI는 `config/collect-p4.json`의 60개다. 여섯 지형군에 같은 총량을 배분하고, 폭 256/512/1024px의 초기 가중치는 55:30:15다. 이는 기존 1만의 실제 분포(약 54/33/13%)를 참고한 시작값이며 부족분 재배분을 허용한다. 운영 한도는 관측 RSS 3GiB, 인덱스 디스크 24GiB, 여유 디스크 하한 8GiB다. 장면 동시성은 1이며 유료 소스·클라우드 실행은 추가하지 않았다.

신규 인덱스는 `data/index/p4-additional-90k/`에 독립적으로 저장한다. 원래 인덱스와 파생 회전 캐시를 옮기거나 지우지 않는다. `combine`은 멤버의 상대경로와 고정 shard 목록을 참조하는 bundle을 만들고 같은 tile ID를 한 번만 집계한다. 멤버의 snapshot이 바뀌면 명시적으로 bundle을 다시 만들어야 한다. 선택 결과의 `index_member`로 원래 캐시를 찾아 사진을 만든다.

## 병목을 줄인 구현

- 읽은 canonical RGB로 세 방향 view 특징을 즉시 만들고 부모 shard의 `views/`에 원자적으로 함께 공개한다. 후속 검색은 inline view를 참조하므로 회전 특징 생성용 부모 재다운로드가 필요 없다. 기존 1만의 별도 view 캐시도 그대로 사용한다.
- 겹치는 창의 결측·SCL 결함·구름 비율은 블록 누적 마스크로 계산한다. 품질 클래스와 임계값은 바꾸지 않았다. CRS별 좌표 변환기·측지 객체를 재사용한다.
- 디스크 파일 합계는 매 블록마다 전체 순회하지 않고 최대 15초 간격으로 관측한다. RAM과 여유 공간 확인은 블록 경계에서 유지한다.
- AOI별 카탈로그 결과도 즉시 보관해 전체 카탈로그 검색이 중단돼도 완료한 조회를 재사용한다.
- RGB 유효 마스크와 SCL의 축소 힌트를 함께 써 후보 중심이 무효인 블록을 새 계획에서 제외한다. 최종 품질 판정은 원본 격자의 누적 마스크다.

초기 해안 장면에서 무효 블록 읽기가 많아 위 RGB 힌트를 보완했다. 이 프로젝트의 수집 프로세스만 식별하고, RUNNING 블록이 없는 5,625개 완료 체크포인트에서 중지한 뒤 같은 인덱스로 재개했다. 완료 shard·작업 상태·이미 받은 블록은 보존했다. 기존에 고정한 계획은 유지하고 이후 새 계획에 RGB 힌트를 적용한다. 다운로드 성공 확인용 재검증은 하지 않았다.

## 명령과 확인 범위

```text
uv run badapple-earth collect --config config/collect-p4.json --output data/index/p4-additional-90k --limit 90000 --redistribute
uv run badapple-earth combine --index data/index/p2-10k --index data/index/p4-additional-90k --output data/index/p4-100k
uv run badapple-earth match --targets data/targets/38s-10s-6fps --index data/index/p4-100k --output data/runs/p4-100k --top-k 20
uv run badapple-earth materialize --targets data/targets/38s-10s-6fps --index data/index/p4-100k --run data/runs/p4-100k
uv run badapple-earth render --targets data/targets/38s-10s-6fps --run data/runs/p4-100k
uv run badapple-earth compare --baseline data/runs/p3-10k-rotations --run data/runs/p4-100k
```

관련 오프라인 계약 테스트 8개가 통과했다. 누적 마스크와 직접 계산의 동일 품질 판정, inline view의 원격 읽기 없는 재사용, bundle의 중복 제외와 원본 보존, 선택한 멤버의 실제 사진 출력, 동일 조건 run 비교, 기존 회전/검색 계약을 확인했다. 합성 데이터는 테스트에서만 사용했다. 실제 수집량과 결과를 이 테스트로 대체하지 않는다.

실제 수집에서 일부 해안 AOI의 가용 후보가 1,500개 초기 할당에 미달했다. `--redistribute`는 초기 라운드 후 부족분을 같은 지형군의 완료 AOI에 우선 배분한다. 고갈된 AOI의 현재 수량을 보존하고, 추가 수집이 진행되지 않으면 부분 완료로 끝낸다. 관련 할당 보존 테스트 1개도 통과했다. 이는 빈 이미지나 중복 카운트로 9만을 채우는 동작이 아니다.

`compare`는 target set·metric·회전 집합·recipe가 같은 run만 허용한다. `baseline-comparison.json`에는 목표별 최상위 MSE 차이, 평균 변화, 개선/동률/악화 수를 남긴다. 실제 영상 재생 인식성 평가는 별도로 기록한다. 요청 수와 수신 바이트는 계측하지 않는다.

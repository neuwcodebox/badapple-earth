# 직각 회전 view 검색과 출력

- **날짜:** 2026-11-01 (Asia/Seoul).
- **관련 요구:** F5–F9, F11, F12, N1–N3, N6, N7.
- **요청:** 실제 위성 사진의 방향을 고정하지 않고 회전을 검색에 포함한다.

## 현재 구현

기본 검색 각도는 시계 방향 0·90·180·270도다. 동일한 canonical RGB를 회전한 뒤 목표 비율로 중앙 크롭하고 BOX 리사이즈한다. 90/270도에서 가로세로를 늘리거나 검은 모서리를 만들지 않는다. 임의 각도와 반사는 도입하지 않았다.

0도 특징과 원래 인덱스는 보존한다. 추가 세 방향의 특징은 source shard별 파생 캐시에 저장하며 완료 파일을 재사용한다. 원래 tile ID는 장소를 식별하고 view ID는 각도·크롭·출력 크기·변환 버전을 식별한다. 검색 순위와 사진 파일은 view ID로 구분한다.

검색과 materialize가 같은 회전/크롭 함수를 사용한다. 기록에는 부모의 원본 `window`, 시계 방향 각도, 회전한 canonical 이미지의 `rotation_crop`, 원본 affine로 역매핑한 `view_source_window`, 최종 footprint와 중심 좌표, 원래 footprint가 포함된다. 90/270도의 화각 감소까지 반영하므로 이 비교는 회전과 비율 유지 크롭을 함께 추가한 효과다. 부모 tile의 품질·명암·중복 통계는 부모 창 기준으로 보존한다.

기본 RGB는 기존 PNG와 블록 캐시를 우선 사용하고 없을 때만 실제 원본에서 읽는다. 새로 읽은 기본 사진은 `cache/base-photos/`에 남긴다. 예전 0도 run의 사진을 넘긴 경우에도 후속 출력에서 재사용할 수 있게 공통 기본 사진 캐시에 보관한다. 다운로드 성공을 확인하기 위한 별도 원격 검증은 하지 않았다.

## 실제 실행

입력 목표와 물리적 후보 집합은 기존 P3와 같다. 목표 60개를 다시 추출하거나 후보를 재수집하지 않았다.

```text
uv run badapple-earth match --targets data/targets/38s-10s-6fps --index data/index/p2-10k --output data/runs/p3-10k-rotations --top-k 20 --photo-cache data/runs/p3-10k/photos
uv run badapple-earth materialize --targets data/targets/38s-10s-6fps --index data/index/p2-10k --run data/runs/p3-10k-rotations
uv run badapple-earth render --targets data/targets/38s-10s-6fps --run data/runs/p3-10k-rotations
```

| 항목 | 0도 기준 | 직각 회전 포함 |
| --- | ---: | ---: |
| 실제 장소 후보 | 10,000 | 10,000 |
| 검색 view | 10,000 | 40,000 |
| 목표 슬롯 | 60 | 60 |
| Best MSE 평균 | 0.07291096 | 0.06719378 |
| Best MSE 중앙값 | 0.06903018 | 0.06617531 |
| 선택된 장소 / view | 26 / 26 | 21 / 27 |

평균 Best MSE는 약 7.84% 감소했다. 회전 포함 결과의 선택 각도별 슬롯 수는 0도 10개, 90도 15개, 180도 10개, 270도 25개다. 선택 사진의 materialize 실패·차순위 대체는 없었다. 원래 0도 결과는 `data/runs/p3-10k/`에, 새 컬러·흑백·원본 비교 MP4와 Top-5는 `data/runs/p3-10k-rotations/`에 있다. 두 결과 모두 10초·24fps 무음 경로다.

새 run ID는 `97f0a0bab29aee65f3f0ee6a1e55a0c974bf93f3445837894a4bbb2b3c15e420`이다. 추가 view 캐시는 `data/index/p2-10k/views/canonical-quarter-turn-center-crop-v1/`에 있다. 최초 생성에서 부모 RGB 10,000개 중 PNG 1,068개와 기존 블록 4,834개를 사용했고, 캐시에 없는 4,098개는 실제 원본에서 읽어 기본 사진으로 보관했다. 이 읽기는 회전 특징 생성에 필요한 작업이며 별도 연결 검증이 아니다. 네트워크 요청 수·바이트는 계측하지 않았다.

생성 후 view 캐시는 37,885,942 bytes(약 37.9MB), 공통 기본 사진 캐시는 396,326,205 bytes(약 396.3MB)였다. 기존 블록·원래 인덱스·결과 영상 용량은 이 두 수치에 포함하지 않는다.

## 확인 범위와 후속

관련 오프라인 테스트 5개가 통과했다. 비정사각형 사진의 방향·중앙 크롭, 유효 source footprint, 서로 다른 view ID, 네 방향 검색과 실제 출력 픽셀의 일치를 확인했다. uint8 극값 MSE·동률과 PTS 기존 계약도 포함한다. 합성 이미지는 테스트에서만 사용했다. 전체 테스트·원본 재다운로드 검증·렌더 전체 재디코드는 하지 않았다.

원래 데이터셋 규모는 늘지 않았으며 점수 개선을 캐릭터 움직임 인식이나 작품 품질의 성공으로 해석하지 않는다. 이번 회전 run의 실제 재생 평가는 미실시다. 다음 규모 비교에서는 타깃·metric뿐 아니라 각도 집합도 고정해야 한다. 향후 추가 수집에서는 이미 읽은 RGB로 회전 특징을 동시에 저장하면 파생 특징 생성 단계의 원본 읽기를 줄일 수 있다.

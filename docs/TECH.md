# Bad Apple Earth — 기술 설계

이 문서는 데이터 접근부터 최종 영상까지의 계약을 정리한다. **기본 제작은 원본 MP4의 전체 프레임·Sentinel-2·직각 회전을 포함한 MSE 전수 검색·16:9 설명 영상과 사진 전용 영상이다.** 사용자 제공 원본 오디오는 로컬 결과에 복사하거나 명시적 무음으로 출력한다. PHOG/Rank·영역/SDF/방향성 Chamfer·형태 우선 선택은 비교 실험으로 보존한다. 실제 실행·시각 평가 상태는 [PLAN](PLAN.md)에 둔다.

## 전체 구조

<!-- 구성요소의 책임·소유 상태와 연결·데이터 흐름을 설명한다. -->

```text
로컬 MP4 ─ 분석·고정 콘텐츠 크롭·시점 추출 ─ 타깃 공통 특징 ─────────┐
                                                                    │
AOI/시기/할당량 ─ STAC 장면 검색 ─ 접근/자산 확인                     │
                    └─ COG 블록 읽기 ─ 다중 스케일 창 추출           │
                         └─ 품질·중복 검사 ─ 특징 + 출처 저장 ──────┤
                                                                    ▼
                                                     배치 Top-K 검색·재정렬
                                                                    │
                                            원본 COG 재추출 / 선택 사진 캐시
                                                                    │
                                       실제 사진 hold + 선택한 원본 오디오
                                                                    │
                                        MP4 / 비교 시트 / manifest / 평가
```

영상 입력과 지리 이미지 수집은 독립적이다. MP4가 없어도 수집기를 구현·검증할 수 있고, 수집이 멈춰도 완료된 인덱스로 검색할 수 있다. 네트워크가 없어도 캐시가 있는 결과는 재렌더링할 수 있어야 한다.

| 구성요소 | 책임·소유 상태 | 관련 요구 |
| --- | --- | --- |
| 입력 처리 | MP4 식별·probe·콘텐츠 크롭·목표 시각과 프레임 | F1, N2, N5 |
| 소스 adapter | 카탈로그, 자산 해석, 접근·서명·밴드·권리 정보 | F2, F11, F12, N4 |
| 수집기 | AOI quota, 원격 블록·창, 유효성·중복·작업 상태 | F3, F4, F10, N3 |
| 공통 특징 처리 | RGB→명암·축소·특징 버전 | F5, F6, N2 |
| 저장소 | 타일 ID, 불변 인덱스 shard, 선택 이미지, commit 상태 | F5, F7, F10, N2, N3 |
| 검색기 | 정확 MSE Top-K·선택 지표 비교, 독립 PHOG/Rank·SDF/Chamfer 실험 | F6, F12, N1, N3 |
| 재추출·렌더 | 소스 재해석, 정확한 창·변환, 시간 배치, 음원 | F7, F8, N1 |
| 보고·출처 | 비교 샘플·점수·자원·권리·프레임별 provenance | F9, F11, F13, N4, N6 |

STAC·래스터 읽기·인코딩은 검증된 라이브러리를 재사용한다. 프로젝트에서 구현할 부분은 AOI/작업 orchestration, 추출·특징·검색 계약, 출처 연결과 평가다. 처음부터 범용 플러그인 시스템·웹 서버·분산 작업 큐는 필요하지 않다.

## 주요 경계와 계약

<!-- 외부 연동과 모듈 사이에서 유지해야 할 입출력·상태·실패 계약을 설명한다. -->

### 1. 원본 MP4와 시간 축

**관련 요구: F1, F8, F9, N2, N5.**

입력은 사용자가 이미 보유한 로컬 MP4다. 권장 위치는 `input/badapple.mp4`지만 절대경로·상대경로를 모두 받을 수 있게 한다. 경로의 공백·한글을 처리하고 원본을 변경하지 않는다. 외부 다운로드·업로드는 하지 않는다.

최초 확인 항목은 영상/오디오 스트림 선택, duration, time base, start time, nominal/average fps, coded width/height, SAR/DAR, 회전 메타데이터, 디코드 가능 여부다. ffprobe는 이런 메타데이터를 기계 판독 가능한 형태로 출력할 수 있다.[R9]

콘텐츠 크롭은 여러 대표 프레임을 확인해 **전체 선택 구간에 하나의 고정 사각형**으로 저장한다. Bad Apple의 검은 화면과 검은 실루엣은 콘텐츠일 수 있으므로 프레임별 검은 경계 자동 제거를 하지 않는다. 파일이 실제 4:3인지 확인하기 전에는 4:3을 확정하지 않는다.

초기 실험 설정:

| 항목 | 값·결정 방식 |
| --- | --- |
| 테스트 길이 | 10초, 실제 영상 범위 안에서 |
| 구간 시작 | P1에서 다양한 형상·전환이 있는 구간을 선택해 고정. 30초는 과거 예시일 뿐 기본 확정값 아님 |
| 목표 시각 | `t_i = start + i / sample_fps`, `i = 0 … 59` (10초·6fps 예시) |
| 매칭 fps | 6 |
| 출력 fps | 24 기본, 30 선택 |
| 색상 | 원래 컬러 기본; 흑백은 비교 산출물 |
| 보간 | 사진 hold만. 기본 모핑·광학흐름·crossfade 없음 |

가변 프레임레이트도 실제 PTS를 기준으로 처리한다. 프레임 번호를 정수 fps로 나눈 값만 믿지 않는다. 구현 시 목표 시각에 가장 가까운 디코드 프레임을 고르는 규칙(동률은 이전 프레임)을 고정하고 목표 시각과 실제 선택 PTS를 함께 저장한다. 부족한 구간을 말없이 연장하거나 검은 프레임으로 채우지 않는다.

구간 샘플링은 PyAV로 필요한 구간을 디코드하며 Fraction 시간 비교로 위 규칙을 적용한다. 최종 제작의 `frame_mode=all`은 처음부터 끝까지 실제 디코드 프레임을 모두 저장한다. 각 프레임의 PTS·time base를 기록하고 일정한 PTS 간격에서 원래 유리수 fps를 유도한다. 현재 전곡 인코더는 CFR·회전 없는 SAR 1:1 입력을 지원한다. VFR·회전·비정사각 픽셀은 임의 변환하지 않고 명시적 오류로 끝낸다. 원본 6,955프레임·30000/1001fps의 모든 프레임이 각각 하나의 결과 프레임에 대응한다.

10초·6fps를 24fps로 표시하면 각각 4프레임을 hold한다. 일반 조합은 `floor(output_time * sample_fps)`로 목표 슬롯을 정하는 등 시간 기반 규칙을 사용하고 마지막 슬롯 길이를 처리한다. 오디오는 동일한 `start`와 `duration`으로 잘라 타임스탬프를 출력 시작에 정렬한다. 원본의 비영(非零) stream start time도 확인한다. 입력 오디오가 없거나 무음 모드를 요청하면 정상적인 무음 결과로 표시한다.

### 2. 외부 데이터 소스

**관련 요구: F2, F3, F11, F12, N3, N4.**

STAC은 촬영 장면·자산을 찾는 카탈로그이고 COG는 실제 영상 일부를 읽는 형식이다. 카탈로그 주소에서 곧바로 지도 타일을 긁는 것이 아니다. 컬렉션 메타데이터가 있다고 특정 장면의 픽셀 접근까지 검증된 것은 아니다.

#### 2.1 Sentinel-2 Collection 1 L2A — 첫 구현의 유일한 필수 소스

| 항목 | 설정 |
| --- | --- |
| 내부 source ID | `earthsearch-sentinel2-c1` |
| STAC API | `https://earth-search.aws.element84.com/v1` |
| Collection | `sentinel-2-c1-l2a` |
| 우선 RGB asset | `visual` |
| 보조 품질 asset | `scl` |
| 초기 날짜 범위 | `2023-01-01/2026-10-31`, 재실행 시 명시적으로 고정 |
| 초기 장면 구름 기준 | `eo:cloud_cover <= 10` (%) |
| 첫 접근 | 실제 item의 HTTPS COG href를 사용, 익명 읽기를 먼저 확인 |

2026-11-01 확인한 컬렉션 정의는 `visual`을 10m·3밴드·uint8 COG, `scl`을 20m·uint8 COG로 기술한다. 이는 **스키마 확인 결과**다. 실제 파일의 dtype·CRS·band order·mask·블록 크기·transform은 매 장면 읽을 때 확인한다.[R1]

AWS 안내에서 Collection 1 데이터 버킷 `e84-earth-search-sentinel-data`는 AWS 계정 없는 접근 경로를 제공한다.[R3] 임의로 이 버킷의 경로를 조립하지 말고 STAC item의 자산을 따른다. 오래된 `sentinel-2-l2a`와 현재 선택한 C1을 말없이 섞지 않는다. C1이 접근 불가하면 원인과 후보 대체 컬렉션을 기록한 뒤 설정을 바꾼다.

`visual`이 정상 RGB uint8이면 직접 사용한다. 존재하지 않거나 다른 형식이면 이유를 남겨 건너뛰는 것이 초기 기본이다. raw red/green/blue uint16 reflectance를 무조건 uint8로 cast하면 안 된다. 밴드 조합 지원이 필요해지면 radiometric scale/offset·stretch 정책을 명시한 별도 버전으로 구현한다.

#### 2.2 NAIP — 조건부 고해상도 보강

NAIP는 촬영 시기·지역에 따라 대략 0.3~1m급 항공영상으로 세부 도시·농경지 패턴을 보강할 수 있다. **AWS의 `naip-analytic`, `naip-source`, `naip-visualization`은 Requester Pays로 안내된다.** 공개 이용 가능 데이터라고 다운로드 경로까지 무료라고 간주하지 않는다.[R4]

| 경로 | 카탈로그·자산 | 접근 정책 |
| --- | --- | --- |
| Earth Search | 같은 API의 collection `naip`, asset `image` | 먼저 메타데이터 확인. 실제 href의 AWS 과금·인증 조건을 점검하고 승인 없이는 유료 읽기 금지 |
| Microsoft Planetary Computer | `https://planetarycomputer.microsoft.com/api/stac/v1`, collection `naip`, asset `image` | 사용자 API 키 없는 SAS 발급·실제 COG 창 읽기 확인. 대량 처리량·서비스 제한은 미검증 |

Earth Search의 NAIP 정의는 `image`를 RGB+NIR의 4밴드 COG로 기술한다. 컬렉션의 촬영기간 정보도 Sentinel-2와 같지 않으므로, Sentinel의 2023년 이후 필터나 `eo:cloud_cover`를 그대로 적용하지 않는다. RGB 밴드만 정확히 고르고 NIR를 가시광 채널로 사용하지 않는다.[R2]

Planetary Computer는 `planetary_computer.sign` 또는 `sign_inplace`를 사용하는 공식 Python SDK 경로를 제공한다.[R5] 실제 접근 검증에서는 SDK와 같은 `/api/sas/v1/token/{account}/{container}` API에서 사용자 API 키 없이 임시 SAS를 발급받았다. 서명된 URL은 메모리에서만 사용하고 영구 식별자로 저장하지 않는다. 검증한 0.6m 자료는 4밴드 uint8이며 RGB는 1·2·3번이다. 4번 NIR는 TIFF color interpretation에서 `undefined`일 수도 있으므로 밴드 의미는 STAC과 함께 해석한다. 소량 접근 성공과 대량 처리량·할당량 검증을 구분한다.

경로가 바뀌면 source ID도 `planetarycomputer-naip` 등으로 구분한다. 서로 다른 제공자의 동일 지역 영상이 자동으로 동일한 바이트·촬영분이라는 가정은 하지 않는다.

#### 2.3 LINZ — 뉴질랜드 항공사진 보강 후보

공개 정적 STAC 카탈로그는 `https://nz-imagery.s3.ap-southeast-2.amazonaws.com/catalog.json`이다. 컬렉션·item의 상대 링크를 각 문서 URL 기준으로 해석하고, item의 `visual` COG를 익명 HTTPS로 읽는다. 컬렉션 수는 여러 시기·종류의 촬영 사업 수이며 고유 지역 수가 아니다.[R11]

검증한 자료는 RGB+alpha의 4밴드 uint8·WebP 압축 COG다. RGB는 1·2·3번을 사용하고 유효성에는 alpha가 반영된 dataset mask를 쓴다. 기존 Rasterio/GDAL 환경에서 디코딩을 확인했다. 확장자가 `.tiff`일 수 있고 asset에 선택 필드인 `roles`가 없을 수 있다. `datetime=null`이면 `start_datetime`·`end_datetime` 촬영 구간을 보존하고 임의의 단일 촬영일로 바꾸지 않는다. 검증한 세 컬렉션의 라이선스는 CC-BY-4.0이며 실제 licensor는 각 컬렉션의 provider에서 추적한다.

NAIP와 LINZ에는 Sentinel-2 SCL이 없으므로 이를 합성하거나 구름 비율 0으로 보고하지 않는다. `aerial-rgb-dataset-mask-unassessed-cloud-v1` 정책은 dataset mask의 완전 유효 창만 채택하고 `local_cloud_ratio=null`, `cloud_quality=unassessed`로 저장한다. 유효한 저대비 지형은 유지한다. 구름 검출이나 전수 시각 검증을 수행한 정책으로 해석하지 않는다.

기존 `collect`·`collect-parallel`의 설정 `source`로 `planetarycomputer-naip` 또는 `linz-imagery`를 지정한다. `sources/aerial.py`가 공개 카탈로그·자산을 해석하고, 기존 블록·체크포인트·RGB shard·직각 회전 특징을 함께 저장한다. 항공사진에서는 `cache_rgb=true`가 필수다. NAIP는 최신 촬영분을 우선하며 같은 quarter-quad의 중복 촬영을 제한한다. LINZ는 명시한 collection의 item 링크를 seed 기반으로 섞어 제한된 수만 조회하고, collection·원본 item ID를 함께 보존하며 반복되는 도엽 ID를 지역 장면 ID로 구분한다. 카탈로그 한도는 조사한 범위이며 전체 데이터 소진과 구분한다.

초기 수집 설정은 NAIP 36개 AOI와 LINZ 23개 0.3m 촬영 사업이다. 256/512/1024/2048px 창으로 NAIP 0.6m 자료에서 약 154–1229m, LINZ 0.3m 자료에서 약 77–614m를 탐색한다. 6cm 도시 자료는 이번 대량 계획에 포함하지 않는다. 원본 밴드·mask·CRS·지상 범위·촬영 구간·licensor와 실제 변환을 저장하며 검색 결과의 서로 다른 소스별 선택 필드도 보존한다. 구간 비교의 크레딧은 선택한 Sentinel-2/NAIP/LINZ와 실제 licensor를 반영한다. 기존 전곡 제작 설정은 자동 변경하지 않는다.

접근 검증의 실제 장면·창은 [접근 기록](history/2026-11-02-P5-aerial-access.md), 구현·대량 수집·계측 상태는 [수집 기록](history/2026-11-03-P5-aerial-collection.md)에 둔다.

#### 2.4 후순위 소스와 공통 규칙

Landsat Collection 2는 넓은 지형 패턴을 위한 후순위 후보이며, OpenAerialMap은 영상별 이용 조건·품질 검토가 필요하다. 첫 구현에 추가하지 않는다. 상용 지도 서비스 화면 캡처와 타일 스크래핑은 제외한다.

모든 소스 adapter는 collection/item/asset과 접근 방식·밴드·품질 자산·라이선스 링크를 정규화해서 반환한다. `s3://`를 단순 문자열 치환해 HTTPS로 만들지 않는다. 401/403은 인증·과금·서명 만료를 구분하고 429/503만 무제한 반복하지 않는다. 정상 항목이 없으면 명시적 빈 결과와 이유를 반환한다.

`requester_pays`는 기본 `false`, 네트워크 동시 장면 수는 기본 2다. AWS 자격증명이 환경에 있다는 이유만으로 청구 동의를 가정하지 않는다.

### 3. 수집 계획과 대량 전처리

**관련 요구: F2–F5, F10, F12, N2–N4.**

#### 3.1 AOI와 장면 선별

전 세계 단일 BBOX를 무제한 검색하지 않는다. 작은 AOI를 GeoJSON 또는 BBOX 목록으로 관리하고 지형군별 quota를 나눈다. 각 AOI에는 `aoi_id`, `terrain_group`, geometry, enabled, scene/tile quota, 적용 소스를 둔다.

아래는 구현자가 초기 AOI 설정으로 옮길 수 있는 **수집 탐색용 제안 범위**다. 행정경계·촬영 품질·유효 후보 수를 검증한 목록은 아니며 P1에서 실제 장면과 contact sheet를 보고 조정한다. 순서는 `[서경도/최소경도, 남위도/최소위도, 동경도/최대경도, 북위도/최대위도]`, 즉 `[west, south, east, north]`다.

| AOI ID | 지형군 | BBOX |
| --- | --- | --- |
| coast-kr | 해안·섬 | `[125.6, 34.5, 126.8, 35.5]` |
| coast-jp | 해안·섬 | `[132.5, 33.5, 134.3, 34.8]` |
| desert-na | 사막·건조지 | `[11.5, 23.0, 12.5, 24.0]` |
| desert-au | 사막·건조지 | `[135.0, -26.0, 136.0, -25.0]` |
| mountain-himalaya | 산악 | `[85.0, 27.5, 86.2, 28.5]` |
| mountain-andes | 산악 | `[-71.5, -34.0, -70.3, -33.0]` |
| river-nile | 강·습지 | `[30.7, 30.2, 31.7, 31.3]` |
| river-amazon | 강·습지 | `[-60.5, -3.5, -59.5, -2.5]` |
| agriculture-us | 농경지 | `[-94.5, 41.0, -93.5, 42.0]` |
| agriculture-eu | 농경지 | `[1.0, 48.0, 2.0, 49.0]` |
| city-jp | 도시 | `[139.3, 35.3, 140.1, 36.1]` |
| city-us | 도시 | `[-118.5, 33.7, -117.7, 34.4]` |

첫 1만 후보는 6개 지형군에 가능한 한 균등하게 배정하고, 할당을 못 채운 군은 부족 이유를 남긴다. 제안 시작값은 AOI당 보관 장면 3개, 카탈로그 검사 상한 500 items다. 상한 때문에 일부만 조회했으면 `truncated_by_limit` 등을 기록한다. 최신 촬영 우선은 필수가 아니다.

현재 `collect.json`은 AOI당 서로 다른 MGRS 장면 최대 3개와 카탈로그 100 items를 사용한다. 장면·스케일 균등 할당을 먼저 시도하고, 남은 수량은 이미 읽은 블록의 유효 창을 우선 사용한다. 스케일별 부족분은 같은 AOI의 다른 스케일로 재배분할 수 있으며 실제 분포를 manifest에 기록한다. 저해상도 SCL은 읽기 계획의 우선순위를 정하는 힌트이며 최종 품질 판정은 원본 격자에서 한다.

같은 MGRS 타일/공간을 여러 날 촬영했다고 서로 다른 지형 샘플처럼 집계하지 않는다. 먼저 다른 공간을 배정하고 같은 공간에서는 품질 좋은 촬영분 하나를 고른다. 계절·광선 차이가 실험에 도움이 될 때만 추가 날짜를 선택한다. 선택된 item 목록을 저장하여 API 반환 순서나 최신 영상 추가로 실험이 바뀌지 않게 한다.

장면 BBOX가 AOI와 겹친다는 이유로 전체 장면을 무조건 수집하지 않는다. 기본은 **후보 중심이 AOI 안에 있는지** 검사하고 footprint를 기록한다. 국가 제한처럼 경계 엄수가 필요한 실험은 footprint 전체 포함 규칙으로 별도 설정한다.

P4의 추가 수집은 `collect-p4.json`의 별도 60개 AOI를 사용한다. 기존 수집의 실제 폭별 분포를 참고해 시작 가중치를 55:30:15로 두고, 부족분 재배분 원칙은 유지한다. RGB 유효 마스크와 SCL의 축소 힌트를 함께 사용해 유효 후보 중심이 없는 블록의 요청을 줄인다. 카탈로그는 AOI별로도 저장해 검색 중단 시 완료한 AOI 조회를 재사용한다.

100만 확대 설정 `collect-million.json`은 기존 72개 AOI와 면적으로 겹치지 않는 600개 AOI를 사용한다. 기존 60개 중심 지역과 새 60개 중심 지역 주변에 다섯 개씩 배정하며 지형군은 실제 토지피복의 정답 라벨이 아닌 계획 계층이다. 추가 90만의 초기 스케일 가중치는 256/512/1024/2048px 순서로 15:30:40:15다. 기존 10만은 그대로 참조하고 품질 기준·부족분 재배분 규칙도 유지한다.

#### 3.2 다중 스케일과 종횡비

아래 수치는 목표 콘텐츠가 4:3일 때의 시작값이다.

| 원본 창 | Sentinel-2 10m 기준 지상 범위 | 검색·미리보기 기준 이미지 |
| --- | --- | --- |
| 256×192 px | 약 2.56×1.92 km | 256×192 RGB |
| 512×384 px | 약 5.12×3.84 km | 256×192 RGB |
| 1024×768 px | 약 10.24×7.68 km | 256×192 RGB |
| 2048×1536 px | 약 20.48×15.36 km | 256×192 RGB |

실제 콘텐츠 비율이 다르면 창과 출력 높이를 같은 비율로 유도한다. 이미지가 16:9라고 4:3으로 늘리지 않는다. NAIP에는 같은 픽셀 창이 훨씬 좁은 지상 범위를 뜻하므로 `source_gsd`, `ground_width_m`, `ground_height_m`를 저장한다.

시작 stride는 각 창 크기의 50%다. 후보가 중복에 치우치면 75~100%를 비교한다. 장면의 공통 픽셀 원점에서 전역 윈도 목록을 결정하고 안정적인 순서로 배정한다. 영상 끝에 창을 억지로 끼워 맞추며 동일 창을 중복 생성하지 않는다.

후보는 한 원본 asset의 연속된 창이어야 한다. 두 장면 또는 서로 다른 날짜를 이어 붙이지 않는다. 포토모자이크나 cloud-free 합성으로 결측 영역을 메우지 않는다.

#### 3.3 COG 블록 읽기

타일마다 파일을 다시 열어 요청하지 않고 장면을 한 번 열어 큰 블록을 읽고 메모리 안에서 여러 창을 만든다. 초기 읽기 블록은 약 2048×2048px이며 실제 `block_shapes`, overview와 메모리 예산에 맞춘다. COG는 작은 창을 요청해도 내부 블록 전체를 읽을 수 있으므로 픽셀 수와 네트워크 바이트가 같다고 가정하면 안 된다.[R6]

기본 설계는 전역 후보 창을 읽기 블록에 하나씩 배정하고, 블록 오른쪽·아래에 최대 창 크기만큼 필요한 halo를 포함하는 방식이다. 다른 방식도 가능하지만 블록 경계 후보 누락과 중복을 테스트해야 한다. 경계 halo·SCL 정렬·float 변환까지 포함한 동시 메모리를 계산한다.

원격 RGB는 장면 전체를 읽지 않는다. 동일한 소스 창을 분할 읽은 결과와 단독 읽은 결과가 같은 canonical 이미지가 되도록 리샘플링 격자를 고정한다. 정확성 검증 전에는 과도한 overview·근사 읽기 최적화를 하지 않는다. SCL은 별도의 지리적 격자에 맞춰 nearest-neighbor로 정렬한다.

각 수집기는 장면 동시성 1, GDAL 내부 재시도 2회와 블록 작업 시도 최대 3회를 사용한다. 카탈로그는 urllib3의 제한된 재시도와 `Retry-After` 처리를 사용한다. 블록 일시 오류는 지수 대기 후 다시 시도하며, 접근 거절·자산 없음은 반복하지 않는다. 연결/읽기 타임아웃을 명시하고 실패 범위를 장면·블록·카탈로그로 구분한다.

`collect-parallel`은 AOI를 지형군별로 네 프로세스에 균등 분할한다. 프로세스마다 SQLite writer·원격 reader·체크포인트·출력 인덱스를 따로 두며 완료 인덱스는 기존 `combine`으로 결합한다. 같은 DB에 여러 writer를 두지 않는다. 전체 감독 프로세스는 자식 종료를 기다리고 완료/실패 시 상태를 기록하며 반복적인 전체 디렉터리 검사나 다운로드 확인을 하지 않는다. 작업별 RAM 예산 3GiB·디스크 예산 55GiB·공유 여유 공간 하한 20GiB이며 BLAS/OpenMP 스레드는 각 1개다. 수집기 내부 자원 검사는 블록 경계에서 계속 수행한다.

#### 3.4 품질 필터

필터 결과는 삭제 여부뿐 아니라 유효율·구름율·품질 태그와 사유 코드로 저장한다.

- 소스의 유효 mask, RGB 밴드 구성, dtype, 크기·범위를 확인한다. RGB 유효성은 Rasterio의 `dataset_mask`를 사용한다. 명시적 데이터셋 마스크를 우선하고, 밴드별 NoData 마스크만 있으면 OR로 결합한다. 8비트 RGB의 한 채널이 0인 어두운 픽셀을 결측으로 오인하지 않는다. SCL 결함 판정은 별도로 유지한다.
- 품질 정책은 행의 `quality_policy`로 추적한다. 초기 `rgb-all-band-mask-v1`은 모든 채널이 유효한 창만 허용했고, 현재 `rgb-dataset-mask-scl-v2`는 위 데이터셋 마스크를 따른다. 이전 통과 후보는 현재 정책에서도 유효하므로 재수집하지 않는다. 서로 다른 정책을 포함한 확대 비교는 순수한 수량 효과와 구분한다.
- 후보 수집의 초기 유효 픽셀 비율은 95% 이상으로 둘 수 있지만 **검색 기본 풀과 최종 출력에는 결측 픽셀이 없는 창을 사용**한다. 완전 유효가 아닌 창은 `partial_valid`로 분리하고 자동 선택하지 않는다. 검은 결측이 실루엣 매칭을 돕지 않게 한다.
- SCL이 있으면 RGB의 지리 범위로 정렬한다. 기본 오염 클래스는 구름 그림자 3, 구름 8·9, cirrus 10이며 NoData 0·결함 1도 따로 처리한다. 지형 그림자 2, 물 6, 눈/얼음 11을 구름이라고 제거하지 않는다.[R8]
- 로컬 구름 오염 비율의 시작 상한은 2%다. 실제 영상으로 튜닝하며 장면 구름 10% 필터와 구분한다. 구름 픽셀을 지우거나 덧칠해 사진을 수정하지 않는다.
- SCL이 없으면 `cloud_quality=unknown`으로 두고 RGB 휴리스틱을 보조로 쓴다. 이 후보를 최종 사용하려면 샘플/선택 결과를 따로 확인한다. 눈·모래·사막을 흰 구름으로 일괄 제거하지 않는다.
- 저분산·거의 단색 후보는 `uniform_dark/light` 같은 태그로 보존한다. Bad Apple의 단색 프레임에 필요할 수 있다. 단색이어도 NoData인 경우는 제외한다.
- 동일 tile ID는 정확 중복으로 제거한다. 현재 근접 중복은 같은 장면·스케일의 인접 창에서 dHash와 명암 차이가 모두 작은 후보를 `near_duplicate_of`로 연결하고 보관한다. 정상 단색 지형도 유지한다. 이 수는 별도 통계에 포함하며 전체 후보 수를 고유 형태 수로 보고하지 않는다. 해시 하나만으로 서로 다른 모든 장소를 제거하지 않는다.

#### 3.5 좌표·지상 범위

창 표현을 `col_off, row_off, width, height`로 통일한다. 픽셀 경계 기준의 창 중심은 다음 수학적 변환으로 계산한다.

```text
source_center_xy = affine_transform * (col_off + width/2, row_off + height/2)
(lon, lat) = source_crs → EPSG:4326 변환 (always_xy=True)
```

`src.xy(row, col)`는 기본적으로 픽셀 중심을 사용하므로 이미 경계 기준으로 계산한 중심에 다시 0.5px를 더하지 않게 한다.[R7] footprint는 원본 창 모서리를 변환한 폴리곤으로 기록한다. 회전·기울어진 affine도 단순 북향 픽셀로 가정하지 않는다.

위경도 순서는 GeoJSON/변환 입력에서 `(lon, lat)`, 메타데이터 필드는 이름으로 구분한다. 지상 길이는 투영좌표가 미터 단위일 때만 픽셀 크기로 계산하고, 경위도 CRS이면 측지 거리를 사용한다. `0.0001도 = 0.0001m`처럼 단위를 혼동하지 않는다.

### 4. 특징량과 저장소 계약

**관련 요구: F4–F7, F10, F11, N2, N3.**

#### 4.1 공통 특징 파이프라인

RGB→luminance, 고정 리사이즈와 선택적 정규화를 하나의 공통 경로로 구현해 타깃과 후보에 적용한다. 초기 기준선은 **표시된 RGB의 명암을 그대로 축소**한다. RGB/BGR 순서, 명암 계수, gamma 정책, resampler, 크기, dtype과 feature version을 고정한다.

형태 검색은 1,360D PHOG와 현재 768D Rank-layout을 사용한다. 256급 무손실 RGB 캐시로 각 view의 원래 grayscale을 재생성하며 두 구조 스케일을 분석한다. 세부 계산·버전·타깃 분기는 [형태 검색 계약](SHAPE_MATCHING.md)을 따른다. CLIP/LLM 의미 embedding은 기본 검색에서 제외하며, 강제 threshold만 저장하지 않고 원래 연속 명암을 보존한다.

| 표현 | 4:3 예시 | 용도 |
| --- | --- | --- |
| PHOG | 1,360D float32 | 경계의 공간·방향 배치 전수 검색 |
| Rank-layout | 32×24 float32 | 상대 명암 영역 배치 전수 검색 |
| canonical RGB | 256×192 uint8 예시 | 회전별 grayscale과 실제 원색 출력의 공통 무손실 캐시 |
| coarse gray | 16×12 uint8 | MSE 기준선 전수 검색 |
| fine gray | 32×24 uint8 | MSE 기준선 보조 특징·시각 비교 |
| 구역 통계 | 4×4 평균·밝은 면적 비율 | 큰 형태 분석 |
| 선택 edge/gradient | 32×24 uint8 또는 명시된 dtype | 경계 기반 실험 |
| 선택 색채 통계 | 평균 RGB·채도 | 다양성 분석 |
| 중복 해시 | pHash/dHash | 근접 중복 후보 확인 |

16×12, 32×24는 4:3 예시다. 다른 비율이면 폭을 기준으로 높이를 유도하고 실제 shape를 index manifest에 넣는다. 동일 인덱스 안에서는 shape가 같아야 하며 다르면 별도 인덱스를 만든다.

CLAHE·대비 정규화·이진화는 별도 feature variant다. 원래 명암 기준선과 나란히 비교한다. 프레임/후보마다 서로 다른 규칙을 쓰지 않고, 거의 단색일 때 분모 0·노이즈 증폭을 방지한다. 특징이 좋아져도 실제 컬러 출력이 비슷해 보이는지 평가해야 한다.

#### 4.2 이미지 캐시 정책

수집기는 **전체 후보의 특징 + 원본 위치**와 보고 표본·미처리 블록을 보존한다. `cache_rgb=true`인 대규모 수집에서는 이미 메모리에 있는 canonical RGB를 원본 후보당 한 행의 `rgb.npy` uint8 shard로 함께 저장한다. `rgb_cache_ref`·`rgb_cache_row`로 출처 행과 연결하며 세 회전 view도 같은 RGB 행을 참조한다. RGB와 특징·좌표는 동일한 staging/원자적 공개 경계를 사용한다. 사진 출력은 기존 PNG 다음에 이 RGB shard를 메모리 매핑으로 읽고, 캐시가 없는 이전 인덱스만 블록/원격 읽기로 넘어간다. 새 90만의 256×191 RGB는 약 126GiB이며 블록·메타데이터·특징 용량은 별도다.

형태 분석은 별도 버전의 전체 canonical RGB shard와 PHOG/Rank를 저장한다. 같은 RGB에서 네 회전의 grayscale을 정확히 재생성하고 view별 checksum을 남긴다. 모든 후보를 개별 WebP로 저장하지 않는다. 비교용 무작위 100~200장과 각 타깃 Top-5/Top-20은 필요할 때 materialize한다.

현재 수집기는 seed와 tile ID의 해시가 작은 보고 표본을 수집 중 PNG로 캐시해 표본 생성 때문에 원본을 다시 받지 않는다. 최종 목록은 manifest의 `report_tile_ids`이며, 불변 shard에는 수집 중 교체된 과거 표본 PNG도 남을 수 있다. 미처리 창이 남은 원본 블록 캐시는 별도 용량으로 계측한다.

전체 RGB를 보관하는 수집은 AOI의 네 패스를 마친 뒤 해당 원본 블록 캐시를 비운다. 기존 quota를 이미 채운 AOI의 캐시는 재개 시에도 정리한다. 정리는 단일 writer가 AOI 사이에서 수행하며 불변 RGB·특징·출처 shard는 유지한다. quota 재배분이나 향후 확대에서 아직 채택하지 않은 창이 필요하면 원본 블록을 다시 읽을 수 있다. `cache_rgb=false`인 이전 수집의 블록 보존 정책은 유지한다.

최종 사용 사진은 캐시에 반드시 남긴다. 재현성과 검증을 위해 기준 RGB의 무손실 PNG를 권장하고, WebP quality 80~85는 미리보기 파생물로 사용할 수 있다. 손실 압축 파일은 픽셀 동일성 검증의 기준으로 삼지 않는다. 같은 canonical 렌더 경로로 특징 계산과 최종 프레임을 만들어 창·회전·리사이즈 불일치를 막는다.

형태 분석 캐시는 memmap 가능한 RGB `.npy` shard를 사용하며 작은 이미지 파일 수를 늘리지 않는다. 기존 저해상도 특징만으로 새 특징을 복원할 수 없으므로 PNG·블록 캐시에서 시작하고 부족한 원본만 블록별로 추가 읽는다. 이후 알고리즘 변경과 선택 사진 출력은 공유 RGB를 재사용한다.

#### 4.3 예정 데이터 디렉터리

아래는 **현재 파이프라인의 구조**다. 설정은 `config/p1.json`(접근 확인), `config/collect.json`(수집), `config/targets.json`(입력 평가)이다. 수집·타깃·검색 run마다 독립 출력 경로를 지정한다. 재현 가능한 공유 설정은 `config/`, 대용량·로컬 데이터는 `data/`로 분리한다.

```text
config/
  p1.json                         # 소량 접근 확인
  collect.json                    # 소스·AOI·스케일·특징·자원 설정
  targets.json                    # 고정 입력 구간·크롭·fps
input/
  badapple.mp4                    # 사용자 제공, Git 제외
src/badapple_earth/                # 구현 시 책임별로 구성
  sources/                        # 소스별 adapter
  ...                             # 수집·특징·저장·검색·렌더·보고
tests/                            # 실제 테스트 작성 시 추가
data/
  index/<index_id>/
    manifest.json                 # 공개된 shard 목록·shape·버전·checksum
    jobs.sqlite                   # 수집 상태·체크포인트·재시도·행 검색
    catalog/                      # 고정 장면 목록·소스 메타데이터·블록 계획
      scenes.parquet
      catalog.json
    shards/<shard_id>/
      tiles.parquet               # 해당 shard의 행과 타일 연결
      gray_coarse.npy
      gray_fine.npy
      checkpoint.json
      shard.json
      <tile_id>.png               # 보고용 표본에 선택된 경우만
    staging/                      # 미완료 배치, 검색에서 무시
    cache/blocks/                 # 미처리 창이 남은 블록
    contact-sheet.jpg
  targets/<target_set_id>/
    frames/
    targets.parquet
    gray_coarse.npy
    gray_fine.npy
    manifest.json
    contact-sheet.jpg
  runs/<run_id>/
    matches.parquet
    selected.parquet
    photos/                       # 실제 선택·비교용 PNG
    frames/                       # 컬러·흑백·원본 비교 시퀀스
    color.mp4
    gray.mp4
    comparison.mp4
    top5.jpg
    manifest.json
```

디렉터리 표기의 `tests/`, `data/`는 루트 아래이며 특정 프레임워크에 강제 결합하지 않는다. `data/config/`와 루트 `config/`를 중복 운영하지 않는다.

#### 4.4 메타데이터 최소 필드

| 레코드 | 필요한 내용 |
| --- | --- |
| scene | `scene_id, source_id, stac_endpoint, collection_id, item_id, asset_key, unsigned_locator, acquired_at, footprint, crs, affine, width, height, dtype, bands, nominal_gsd, cloud_cover, aoi_ids, license_refs, retrieved_at, source_etag/version(가능할 때)` |
| tile | `tile_id, scene_id, shard_id, feature_row, col_off, row_off, width, height, latitude, longitude, footprint, source_gsd, ground_width_m, ground_height_m, valid_ratio, local_cloud_ratio, quality_flags, variance, duplicate_hash, extraction_version` |
| view | `view_id, tile_id, rotation_degrees, crop/transform_recipe, output_shape, feature_version` |
| target | `target_id, target_set_id, target_time, selected_source_pts, crop, frame_checksum, feature_shape/version` |
| match | `run_id, target_id, rank, tile_id, view_id, raw_scores, normalized_scores(사용할 때), penalty, final_score, materialization_status, selected, fallback_reason` |
| job | `job_id, source/scene/aoi/config identity, state, checkpoint, attempt_count, lease/heartbeat, next_retry, error_category, error_message_redacted` |

`tile_id`는 소스 namespace·collection·item·asset·원본 창·추출 버전의 canonical serialization 해시다. 같은 공간 원본의 회전/크롭 variant는 `view_id`, 다른 특징 알고리즘은 `feature_version/index_id`로 구분한다. 정확한 ID 직렬화 규칙을 테스트한다. 완료 순서나 임의 Python `hash()`를 영구 ID로 사용하지 않는다.

`feature_row`는 전체 데이터셋의 가변 행 번호가 아니라 **`shard_id` 내부 행 번호**다. 타일 메타데이터와 특징 배열을 함께 commit한다. 통합 인덱스를 만들면 통합 행→view ID 매핑과 index ID를 함께 저장한다.

`acquired_at`은 촬영 시각, `retrieved_at`은 수집 시각이다. 촬영 시각이 범위 또는 누락이면 그대로 표현한다. 임의 날짜나 국가명을 만들어 채우지 않는다. STAC snapshot에서도 서명 URL의 query token을 지우고 public locator와 resolver 정보를 남긴다.

#### 4.5 원자적 저장·중단 재개

크기가 계속 변하는 하나의 `.npy`에 임의 append하지 않는다. 작은 불변 shard를 staging에 만들고 다음 순서로 공개한다.

```text
배치 처리 → tiles와 feature 배열 저장 → 행 수·ID·shape·checksum 검증
→ 같은 파일시스템의 최종 shard 위치로 atomic rename
→ shard manifest를 원자적으로 교체 → job checkpoint 갱신
```

파일과 SQLite를 하나의 DB 트랜잭션이라고 가정하지 않는다. crash가 발생하면 미공개 staging은 무시/정리하고, 공개 shard와 job checkpoint의 차이를 재조정하여 이미 commit한 tile ID를 중복 추가하지 않는다. 가능하면 단일 writer가 manifest를 갱신한다.

작업 상태는 `PENDING / RUNNING / DONE / FAILED`를 사용하고, quota에 의한 중단은 `DONE`이 아니라 창 cursor가 있는 `PENDING`으로 남긴다. 단일 writer의 OS 파일 잠금은 프로세스 종료 시 해제되므로, 다음 writer가 잠금을 얻은 뒤 남은 RUNNING을 회수한다. 읽은 블록은 미처리 창이 남아 있는 동안 압축 캐시에 유지해 재개 시 다시 받지 않는다. 영구 실패와 재시도 가능한 오류를 분리한다. 완료 판단에 파일 존재 여부만 쓰지 않고 manifest와 행 매핑을 사용한다.

#### 4.6 용량·메모리 계획

`collect_rotations=true`에서는 이미 읽은 canonical RGB로 추가 세 방향의 특징과 view 메타데이터를 만들고 `shards/<shard_id>/views/`에 같은 배치로 공개한다. 파생 회전 캐시와 동일한 recipe·형상을 사용하므로 검색은 이를 바로 참조하며 부모 사진을 다시 받지 않는다. 이미 만들어 둔 1만 후보의 파생 캐시도 계속 사용할 수 있다.

`index-bundle-v1` manifest는 여러 물리 인덱스의 상대경로·고정 shard 목록을 참조한다. 결합 시 같은 tile ID를 중복 집계하지 않으며, 원본 파일을 복사하거나 원래 manifest를 덮어쓰지 않는다. 검색 결과의 `index_member`는 선택 사진과 캐시를 어느 멤버에서 읽을지 식별한다. 일반 멤버가 변경되면 bundle을 다시 만들어야 한다. bundle ID는 파일 배치 경로를 제외한 멤버 스냅샷으로 결정한다.

`combine --freeze`는 이미 공개된 shard만 `snapshot_manifest`로 고정한다. 원본 index ID와 고정 shard 목록으로 파생 ID를 만들고, reader는 원본에 새 shard가 추가되는 것은 허용하되 고정 descriptor의 변경·누락은 거부한다. 같은 출력 경로로 재실행하면 기존 스냅샷을 유지한다. 회전 특징 manifest도 스냅샷별 경로를 사용한다. 이미 공개된 RGB·특징 파일은 그대로 참조하며 수집기의 writer 잠금·캐시 퇴출을 방해하지 않는다.

`combine --disjoint-sources`는 이미 중복 정리한 bundle들의 소스 namespace가 실제로 겹치지 않을 때만 저장한 수량·할당 통계·참조 목록을 결합한다. tile ID에 source ID가 포함되므로 Sentinel-2와 NAIP/LINZ 결합에서 기존 100만의 타일 표를 다시 읽지 않는다. 같은 소스가 겹치면 이 경로를 거부하고 일반 중복 검사를 사용한다.

32×24 전곡 제작은 `incremental_base_run`의 완료된 기존 Top-K와 `incremental_index`의 고정 추가 후보만 검색한 Top-K를 합칠 수 있다. 같은 타깃 ID·쿼리 매핑·격자·metric·회전·K와 서로 겹치지 않는 후보 합집합을 검증한다. 각 후보군의 Top-K 합집합을 SSE·view ID로 정렬하면 전체 전수 검색의 Top-K와 같다. 기존 dense row 번호를 유지하고 추가 후보에 기존 view 수만큼 offset을 주므로 버전·타깃이 일치하는 기존 MS-SSIM 점수도 재사용한다. 임의로 다른 인덱스의 점수를 재사용하지 않는다. 입력 프레임·사진·원본 디테일은 필요한 항목만 로컬 캐시에서 재사용하고 새 후보만 계산한다.

다음은 **4:3, WebP 평균 30KB/장이라는 가정**의 계획치다. 다운로드 트래픽·원본 COG·metadata·edge·인덱스·selected PNG·파일시스템 overhead는 제외되어 있다. 1만 장에서 실제 크기를 측정한다.

| 후보 수 | 전체 WebP 가정 | coarse gray 192B | coarse+fine 960B |
| ---: | ---: | ---: | ---: |
| 10,000 | 0.3GB | 1.92MB | 9.6MB |
| 100,000 | 3GB | 19.2MB | 96MB |
| 1,000,000 | 30GB | 192MB | 960MB |
| 5,000,000 | 150GB | 960MB | 4.8GB |

위 단위는 십진수 GB/MB다. 500만은 용량 비교일 뿐 합의한 수집 목표가 아니다. 32×24 uint8 edge를 추가하면 100만 후보에서 768MB가 더 든다. coarse를 float32 FAISS에 넣으면 벡터만 768MB/100만 후보이므로 uint8 파일 크기를 전체 RAM 비용으로 오인하지 않는다.

기본은 coarse/fine을 memory-map 또는 shard 배치로 읽고 변환된 float 배열은 배치만 유지한다. 캐시·SCL·COG halo·검색 점수·원본 디코드 버퍼까지 합친 최고 메모리를 측정한다. 데이터 수집량·RSS·디스크 상한은 환경 확인 후 설정하며, 모르는 다운로드 바이트를 픽셀 수에서 환산해 실측이라고 보고하지 않는다.

### 5. 검색과 재정렬

**관련 요구: F5, F6, F9, F12, N1–N3, N6.**

#### 5.1 MSE 기본 검색과 형태 비교 실험

현재 제작 경로는 MSE 전수 검색이다. 같은 10만 후보·10초 구간의 사용자 재생 평가에서 MSE가 형태 우선 방식보다 나았다. 형태 점수 감소를 실제 영상 품질 개선으로 판정하지 않으며, 기본 경로의 변경에는 재생 품질의 근거를 둔다.

기본 제작은 `search_grid=fine`의 32×24 MSE Top-20 검색과 `selection_method=ms-ssim`, `rerank_top_k=20`의 전체 후보 평가다. `display_top_k=5`는 평가 뒤의 표시 수이며 검색·평가 수를 제한하지 않는다. `mse`는 첫 검색 후보를 선택하는 비교 모드다. 이전 전체 형태 검색 실험을 다시 전수 적용하지 않는다.

[형태 검색 v2.1](SHAPE_MATCHING.md)은 독립 실험이다. PHOG·Rank 각 Top-2048의 합집합을 두 스케일·다섯 분위수의 영역/SDF로 평가하고, coarse 상위 256개와 경로별 상위 64개에 SDF·양방향 Chamfer·Balanced IoU를 적용한다. `D_shape=0.40 SDF+0.35 Chamfer+0.25 region`과 최소 대비·형태 허용폭·광도 선택은 해당 실험의 계약으로 보존한다. MSE로 실험의 앞단 후보를 제한하지 않는다.

저해상도 타깃 `T`와 후보 `C`를 같은 shape의 `[0,1]` 명암으로 해석해 다음 거리를 계산한다.

```text
D_gray(T, C) = mean((T - C)^2)
```

uint8에서 직접 뺄셈·제곱하지 않는다. coarse의 192차원 uint8 내적은 최대 `192 × 255² = 12,484,800 < 2²⁴`인 비음수 정수다. 따라서 CPU float32 BLAS의 각 곱·부분합과 내적은 정확한 정수이며, 이를 정수로 변환한 뒤 정수 norm과 결합해 SSE를 계산한다. norm 결합을 float32로 수행해 상쇄 오차를 만들지 않는다. 차원이 이 범위를 넘으면 float64 내적을 사용한다. SSE를 `차원수 × 255²`로 나눠 같은 MSE를 얻는다. uniform 타깃도 같은 기준선이며 동률은 view ID 오름차순이다.

첫 60개 타깃은 벡터화 NumPy 전수 검색으로 처리한다. 후보를 최대 8192행씩 읽고 각 타깃의 Top-K를 누적 유지한다. 현재 60×8192 int64 거리 행렬은 약 3.93MB다. `targets × candidates × dimension` 방송 연산의 거대한 임시 배열을 만들지 않고 제곱합·행렬곱으로 계산한다.

MSE 기준선의 최종 보관 Top-K는 기본 20, 보고서에는 Top-5를 사용한다. 후보가 K보다 적으면 실제 수만 반환한다. 모든 점수는 metric 이름·버전·범위와 함께 남긴다. 서로 다른 metric의 숫자끼리 개선율을 계산하지 않는다.

#### 5.2 인덱스와 스트리밍 선택

하나의 짧은 구간만 만들 때는 수집 중 타깃과 즉시 비교해 Top-K를 갱신할 수 있다. 그래도 기본 요구인 **후보 특징과 출처 저장**은 유지해 다른 구간·알고리즘에서 재사용한다. Top-K만 남기고 전체 특징을 폐기하는 모드는 사용 목적이 명확할 때 별도 선택한다.

FAISS는 초기 필수 의존성이 아니다. 반복 검색이 병목으로 확인되면 `IndexFlatL2` 같은 정확 검색을 먼저 사용한다. ANN/PQ는 근사 오차를 평가할 자료와 성능 필요가 있을 때만 검토한다. FAISS의 squared L2 합과 MSE 평균은 차원수만큼 다르므로 보고 점수를 같은 단위로 맞춘다.

수백만 후보·전곡에서는 타깃도 배치로 나누고 전체 거리 행렬을 저장하지 않는다. 실험 중 index build/version과 타깃 집합을 고정한다. GPU를 필수로 만들지 않는다.

전곡용 `production/search.py`는 고정 bundle의 네 방향을 한 번 연속 배열로 packing한다. 400만×192 float32 특징은 약 3.072GB이고 norm·view ID·원본 shard 행 주소·동률 순위를 함께 보관한다. 원본 Parquet/특징은 유지하며 완료 캐시를 메모리 매핑으로 재사용한다. packing은 100개 source descriptor마다 체크포인트를 저장한다.

16×12 비교 경로는 타깃 128개·후보 65,536개씩 BLAS로 계산하고 Numba로 정확한 Top-20을 유지한다. 전체 view ID의 사전순 순위를 동률 처리에 사용하므로 source shard 순서와 무관하게 기존 선택이 유지된다. 타깃의 coarse 특징이 완전히 같은 경우 계산을 공유하되 `query_inverse.npy`로 모든 원본 프레임을 복원한다. 완료한 타깃 배치는 원자적 NPZ로 저장해 재개하며 전체 거리 행렬을 남기지 않는다. 기본 32×24 경로는 아래의 fine 배열·후보 스트리밍을 사용한다.

#### 5.3 점수 조합과 반복 제어

`production/rerank.py`는 256×191 grayscale에서 5단계 MS-SSIM을 계산한다. Gaussian 11×11·σ1.5, K1=.01·K2=.03, 2×2 평균 다운샘플과 홀수 경계 reflect padding, 원 논문의 가중치 `.0448/.2856/.3001/.2363/.1333`을 사용한다. 각 단계의 유효 창에서 평균 contrast/structure를 구하고 마지막 단계에 luminance를 포함해 곱한다. 음수 분수 거듭제곱을 피하도록 단계 평균은 [1e-8,1]로 제한한다. [MS-SSIM 원 논문](https://ece.uwaterloo.ca/~z70wang/publications/msssim.html)과 [SSIM 저자 설명](https://www.cns.nyu.edu/~lcv/ssim/)을 참고한다.

MS-SSIM 비교용 제작은 각 실제 source frame의 full canonical grayscale을 평가한다. 검색 특징이 같아 MSE 계산을 공유해도 미세한 명암 구조가 다를 수 있으므로 재평가는 source frame별이다. 20개 전체의 점수 최대를 선택하고 동률이면 MSE 순위·view ID 순서를 따른다. `grid/rerank.py`의 RGB shard별 여덟 프로세스·bounded 타깃 통계 캐시·버전이 맞는 기존 점수·완료 Parquet 배치를 재사용한다. PNG는 최종 선택과 다른 네 장소의 표시가 정해진 뒤 필요한 것만 준비한다. 같은 장소의 다른 회전이 더 앞에 있어도 최종 선택의 방향을 보여주며, 다섯 장소를 원래 MSE 순위로 정렬한다. 표시 순번과 원래 검색 순위를 혼동하지 않도록 숫자는 검색 순위를 사용한다. 점수가 높다는 사실만으로 재생 품질 개선을 판정하지 않는다.

기본 전곡의 `grid/search.production_search()`는 fine 배열만 사용하는 단일 검색 경로다. 같은 fine 원배열의 검색 계산만 공유하고 `query_inverse.npy`로 모든 원본 프레임을 복원한다. 기본 주소/ID packing과 같은 행 순서의 uint8 fine cache를 사용하며 float64 변환은 현재 배치에만 한다. 격자 또는 K가 다른 `reuse_run`에서는 타깃·사진만 재사용하고 검색은 복사하지 않는다. 새로운 선택에서 큰 표시 크기에 필요한 원본 픽셀만 추가 읽으며 이미 있는 native PNG는 재사용한다. 이전 16 격자·다섯 장소 전곡은 완료 비교 산출물로 보존한다.

`compare-metrics`는 전곡 선택 설정과 독립된 동일 후보 비교다. `production/comparison.py`가 완료 run의 Top-20 view와 실제 PTS를 읽고, 지정 구간의 모든 원본 프레임에서 아래 다섯 지표를 평가한다. 서로 다른 장소 다섯 장으로 줄이지 않으며 같은 장소의 다른 회전도 원래 검색 결과에 있으면 유지한다. 기존 다섯 장소 MS-SSIM의 선택도 같은 계산에서 구해 후보 범위 확대의 영향을 따로 기록한다.

| 지표 | 정의·고정 설정 | 선택 방향 |
| --- | --- | --- |
| MSE | 원래 16×12 특징의 정확 검색 점수. 기존 선택의 기준선 | 최소 |
| MS-SSIM | 위의 256×191·5단계 구현과 동일 | 최대 |
| FSIM | 256×191 명암, log-Gabor 4스케일·4방향, 최소 파장 6·배수 2·σ/f=.55·각도 계수 1.2, Scharr/16, T1=.85·T2=160 | 최대 |
| 다중 해상도 MSE | 같은 명암을 BOX로 폭 64·32·16(높이는 비율 유지)으로 각각 축소한 MSE의 동등 가중 평균 | 최소 |
| ZNCC | 256×191의 평균을 뺀 두 명암 배열의 정규화 내적. 명암 반전은 허용하지 않음 | 최대 |

FSIM은 [저자 코드](https://cslinzhang.github.io/home/FSIM/Files/FeatureSIM.m)의 grayscale 경로와 FSIM 전용 phase congruency 설정을 Python/NumPy/SciPy로 이식했다. 입력의 짧은 변이 256 이하라 저자 코드의 자동 추가 축소는 필요 없다. 저자 구현의 영 채움 Scharr 경계를 따른다. 수치상 phase weight 합이 0인 무특징 쌍에서는 `1-full_resolution_MSE`로 처리한다. ZNCC는 한쪽 분산이 0이면 -1로 처리하며, 타깃이 단색이면 모든 후보가 동률이 되어 원래 MSE 선택을 유지한다. 실제 동률은 모두 MSE 순위·view ID 순서로 해소한다. FSIM 포트의 연구·교육 목적 이용 조건과 Kovesi 필터의 고지는 [THIRD_PARTY_NOTICES](../THIRD_PARTY_NOTICES.md)에 둔다. MATLAB과의 수치 일치 검증은 수행하지 않았다.

사진은 기존 PNG 또는 RGB shard만 사용한다. 캐시가 없으면 오류로 끝내며 원격 재추출을 하지 않는다. 모든 패널은 같은 canonical 해상도·원색·표시 크기를 사용해 지표와 원본 디테일 차이가 섞이지 않게 한다. 여덟 CPU 프로세스·bounded 특징 캐시·20프레임 체크포인트를 사용한다. 결과에는 모든 후보별 점수·선택·원래 MSE 순위·실제 출처와 PTS를 남긴다. 비교 영상은 무음이며 각 연속 source PTS당 한 프레임을 인코딩한다. 지표별 점수 단위가 달라 서로의 수치를 직접 비교하지 않으며, 이 shortlist 실험은 다른 지표로 100만 장소를 전수 검색한 결과가 아니다.

##### Astra의 블라인드 후보 선택

`production/visual.py`는 완료한 32×24 검색의 Top-20과 canonical RGB를 재사용한다. `prepare --mode independent`는 대상 프레임 전체를 seed로 섞은 뒤 에이전트별로 나누고, `--mode sequential`은 시간순의 연속 구간을 나눠 병렬 배정한다. 연속 모드의 두 번째 이후 job은 앞 구간의 마지막 10프레임을 `context`로 제공한다. 이 프레임은 앞선 움직임·사진을 파악하는 참고용이며 해당 job의 출력 대상에는 포함하지 않는다. 각 작업자는 자기 구간 안의 직전 선택을 기억하지만 다른 작업자의 실제 최종 선택을 공유하지 않으므로 구간 경계의 완전한 일관성을 보장하지 않는다.

각 프레임 후보는 별도 seed로 섞는다. 원본 256×191과 후보 20장을 한 시트에 넣으며 공개 job에는 가림 case ID·시트 경로·결과 경로와 연속 모드의 참고 프레임만 제공한다. 연속 모드의 후보에는 exact view마다 고정된 `V` ID도 표시해 local `P` ID가 바뀌어도 같은 사진을 추적한다. 실제 source slot·PTS·후보 view·출처·순위는 `private-map.json`에 보관한다. 독립 모드의 무작위 배정은 인접 장면의 영향을 줄이지만 모델 판단의 완전한 독립성을 보장하지 않는다.

Astra 서브에이전트는 `low` reasoning으로 준비된 시트를 묶어 보고 case별 `candidate_id`를 JSONL에 저장한다. 연속 모드는 직전의 고정 photo ID를 기억하고, 형태 대응이 비슷하면 같은 사진이나 명암·색·배치가 가까운 사진을 우선한다. 원본이 실제로 변하거나 형태 대응이 나빠지면 즉시 바꾸며 이전 사진이 현재 Top-20 밖이면 억지로 유지하지 않는다. 후보 점수 계산·추가 조사·장황한 근거는 맡기지 않는다. 완료한 묶음은 즉시 저장하며 재개 시 이미 판단한 시트를 다시 보지 않는다. 전곡은 약 278~279프레임씩 최대 25개 에이전트에 병렬 배정한다. 시트 준비는 decoded RGB의 bounded 캐시와 네 스레드를 사용하고 준비된 job부터 `.ready`로 공개한다. 지침은 `config/astra-reviewer.txt`로 보존하며 API 유료 호출은 사용하지 않는다.

`render`는 저장된 case별 선택이 해당 job과 제시 후보에 속하며 대상 전체가 채워졌는지만 확인한다. 누락 시 임의 선택으로 대체하지 않는다. 원래 PTS 순서의 `selected.parquet`와 무음 비교 영상을 만든다. 독립 모드의 패널은 원본·MSE·MS-SSIM·Astra이고, 연속 모드는 원본·독립 Astra·MS-SSIM·연속 Astra다. 네 패널의 사진은 같은 canonical RGB이며 선택에 맞춰 색상·광도·마스크를 조작하지 않는다. 기존 검색·MS-SSIM은 재계산하지 않는다. 모델 판단은 seed만으로 재현되지 않으므로 원본 시트와 실제 선택 파일을 보관한다.

기본 전곡은 `--all-frames --mode sequential --workers 25`로 준비하고 `collect`로 선택을 원본 순서에 연결한다. 전곡에는 독립 비교 run이 필요 없다. `selection_method: astra`의 제작기는 `visual_run`·`visual_experiment_id`·target/index/search ID와 실제 source slot별 후보 소속을 입력 계약으로 사용한다. 가져온 모델 선택을 MS-SSIM 최대 후보로 바꾸지 않는다. 표시할 후보 점수는 원래 제작 run의 저장된 MS-SSIM을 참고값으로만 읽고 `score_role: reference`로 기록한다. 후보·선택 사진은 시트의 실제 PNG와 이전 native PNG를 재사용하며 큰 표시의 누락된 원본 디테일만 읽는다. `config/production-ms-ssim.json`은 이전 MS-SSIM 전곡의 비교용 설정을 보존한다.

##### 영역 균형·윤곽·실제 경계 대비 비교

`production/silhouette/`와 `compare-silhouette`는 완료한 전곡의 같은 10초 원본 프레임을 기준으로 기존 선택·영역 균형·형태와 시인성을 비교한다. 전체 120만 장소·2,880만 view의 기존 32×24 uint8 특징을 source 순서대로 참조하며 새 다운로드·거대한 고해상도 전수 캐시를 만들지 않는다. 두 독립 검색은 흑/백 각 클래스의 총 가중치가 .5인 MSE와 16/32 격자의 부호 있는 gradient correlation이다. 단색은 균일 가중치로 돌아간다. 각각 Top-512와 기존 MSE Top-20의 합집합을 정밀 비교하므로 기존 MSE 후보에만 제한되지 않는다. float32 점수·고정 전역 dense 주소로 동률을 해소하고 후보 배치별 두 경로의 체크포인트를 함께 저장한다.

정밀 비교는 64/128 폭의 원색 명암에서 계산한다. 영역 균형 MSE, 같은 두 해상도의 signed gradient correlation, 실제 경계 양쪽의 밝기 차이를 별도로 보관한다. 경계는 타깃 mask의 화면 안 4방향 클래스 전이이며 mask를 σ=.75로 평활화한 법선에서 반경 1/2/4px의 밝은 쪽·어두운 쪽을 짝짓는다. 타깃에서도 두 클래스가 맞게 갈라지고 이미지 안에 있는 쌍만 쓰며 각 경계점에 같은 비중을 둔다. 사진에 임곗값을 적용해 실루엣을 만들지 않는다. 경계 점별 평균 차이를 .25로 나눈 값을 [0,1]로 제한한 평균이 경계 대비 점수다.

최초 실험의 합성 점수는 `.35*(1-balanced_MSE)+.35*max(GC,0)+.30*boundary_contrast`다. 실제 흑백 영역 평균 차이 ≥.08, 경계점의 50% 이상에서 차이 ≥.08, 반대 명암(≤-.02) 경계점 비중 ≤.15, GC>0인 후보 중 합성 점수가 높은 사진을 고른다. 단색·통과 후보 없음은 같은 프레임의 기존 선택을 명시적 fallback으로 유지한다. 이 수치와 가중치는 검증할 최초 설정이며 최적값으로 주장하지 않는다. 영역 균형만의 선택도 함께 출력한다. signed-gradient로 윤곽의 위치·방향을 비교하며 이번 경로는 이전 shape-v2.1의 PHOG·임곗값 SDF/Chamfer와 별개다. 원색 사진·좌표·촬영 시점·회전/크롭은 그대로 보존한다.

##### MSE 검색 격자와 후보 폭 비교

`production/grid/`와 `compare-grid`는 최종 MS-SSIM을 256×191·기존 5단계 정의로 고정하고 검색 격자·후보 수만 바꾼다. 16×12와 32×24 각각 Top-512를 전수 검색하며, 16×12/20·16×12/512·32×24/20·32×24/512와 두 Top-512 합집합(최대 1,024 view)을 비교한다. 후보 수는 장소 수가 아닌 회전 view 수다. 표시할 다섯 장소는 전체 후보의 MS-SSIM 평가 이후에 고른다.

32×24는 기존 `gray_fine`과 같은 순서의 약 3.072GB uint8 packing을 재사용한다. 큰 float64 전체 캐시를 만들지 않고 후보 배치만 변환한다. 768차원의 내적 상한 49,939,200은 float32 정수 표현 범위를 넘으므로 `dot_dtype()`의 float64 BLAS를 사용한다. 정수 내적을 복원한 뒤 int32 norm·SSE를 조합한다. 현재 두 격자의 SSE 상한은 int32 안에 있으며 지원 범위를 넘는 격자는 명시적으로 거부한다. 실수 근사 순위로 바꾸거나 float32 소수부 손실을 허용하지 않는다. 동률은 기존 view ID 전역 순서다.

검색은 후보 32,768개를 바깥 루프로, 타깃 최대 128개를 안쪽 배치로 읽는다. 모든 실제 타깃 프레임을 보존하고 두 원래 명암 배열이 모두 같을 때만 계산을 공유한다. 후보 배치별 두 격자의 Top-K·행 주소·다음 offset을 저장한다. 기존 16×12 Top-20의 행 순서가 모두 보존되는지 실제 비교 실행에서 확인한다.

MS-SSIM 전용 정밀 평가기는 새로운 ZNCC·GC·FSIM 점수를 계산하지 않는다. 재사용할 run은 타깃 ID·인덱스 ID·명시적 `ms_ssim_version`이 일치해야 한다. 같은 `slot/dense_row`의 점수만 합치며, 누락된 쌍에 대해서만 로컬 RGB 행을 읽어 후보 pyramid를 만든다. 기존 출처 행도 Arrow에서 필요한 행만 걸러 재사용하고 나머지를 병렬 해석한다. 점수 동률은 해당 격자의 MSE 순위·view ID, 합집합은 32 격자 순위·16 격자 순위·view ID 순이다. 후보 수가 늘면 기존 최고 점수를 잃지 않는 계약을 유지하되 점수 상승을 시각 품질 개선으로 간주하지 않는다.

##### 독립 ZNCC·Gradient Correlation 검색

`production/correlation/`과 `compare-correlation`은 MSE shortlist 밖의 사진을 찾는 독립 비교 경로다. 전체 회전 view에서 폭 16·32의 평균 제거·단위 L2 명암 벡터를 각각 `1/√2`로 이어 붙인 다중 해상도 ZNCC로 Top-512를 찾는다. 별도 경로는 같은 두 크기의 Gaussian σ1·radius3·reflect 미분에서 부호를 보존한 가로·세로 배열을 각각 평균 제거·단위 L2 정규화한다. 두 축·두 크기의 내적 평균이 Gradient Correlation이다. 축 하나의 분산이 0이면 그 축은 0으로 기여한다. 부호 없는 경계 강도나 자유 위치 대응으로 바꾸지 않는다. [OpenCV ZNCC 정의](https://docs.opencv.org/4.x/df/dfb/group__imgproc__object.html)와 [GC의 두 축 NCC 평균 정의](https://pmc.ncbi.nlm.nih.gov/articles/PMC4640192/)를 참고하며, 이 문제에서의 우월성은 가정하지 않는다.

기존 packed view 순서·메타데이터·원래 16×12 특징을 재사용하고 같은 행 순서의 `uint8` 32×24만 약 3.072GB로 packing한다. 정규화 명암/미분의 거대한 float32 전수 캐시는 저장하지 않는다. 후보 배치를 바깥 루프로 하여 새 특징을 한 번 만들고 모든 타깃 배치에 재사용한다. 검색은 실수 유사도 내림차순·view ID 동률 순서의 별도 Numba Top-K를 사용하며 정수 MSE의 `int(dot)` 경로를 쓰지 않는다. 타깃 계산 공유는 16·32 배열 **둘 다** 같은 경우만 허용하며 모든 원본 프레임에 복원한다. 후보 배치마다 두 경로의 Top-K와 다음 offset을 원자적으로 저장한다.

두 경로 각 Top-512와 원래 MSE Top-20을 합친다. 중복 view만 제거하고 장소·회전 중복은 정밀 평가 이후에 처리한다. MSE 후보를 함께 넣는 것은 이전 MS-SSIM 결과를 같은 후보군 안에서 재현하고 검색 확대 효과를 분리하기 위함이다. `config/correlation.json`의 기본 정밀 평가는 폭 64·128의 동등 가중 ZNCC와 같은 두 크기의 GC를 `.7/.3`으로 합친 구조 점수다. 실제 원색 사진의 명암에서 원본 흰 영역(>.5) 아래 평균과 검정 영역 아래 평균의 차이가 `.08` 이상, 사진 전체 명암 표준편차 `.02` 이상, ZNCC 양수인 후보를 허용한다. 최상 구조 점수에서 `.01` 이내인 후보는 full canonical MSE·구조·view ID 순으로 선택한다. 이 수치들은 조정 가능한 최초 실험 설정이며 보편 최적값이 아니다. 대비를 만족하는 후보가 없으면 같은 프레임의 Top-20 MS-SSIM을 사용하고 사유를 기록한다. 타깃 표준편차가 `.005` 미만이면 형태를 억지로 맞추지 않고 원래 MSE를 선택한다.

동일 후보군의 최대 MS-SSIM도 별도로 선택해, **검색 확대+MS-SSIM**과 **검색 확대+새 조합**을 구분한다. ZNCC 단독·GC 단독도 같은 시인성/밝기 규칙으로 비교한다. MS-SSIM 두 비교에는 새 대비 guard나 단색 fallback을 섞지 않는다. 실제 RGB shard별로 작업을 묶어 필요한 사진 행을 정렬·중복 제거하고 연속 바이트로 읽는다. 작은 mmap page fault와 사용하지 않는 shard 전체 읽기를 피한다. 후보 특징을 한 번 만들고 그 사진이 등장하는 모든 프레임에 적용한다. 여덟 프로세스·프로세스별 BLAS 1스레드·bounded 타깃 통계 캐시와 점수 배치를 재사용한다. 모든 평가가 끝난 후에만 최종 선택과 최대 다섯 개의 서로 다른 표시 장소를 기록한다. 출력 사진에는 정규화·밝기 조정·실루엣 합성을 하지 않는다. 세부 출처는 고유 view 메타데이터와 프레임별 후보·선택 테이블의 `dense_row`로 연결한다.

형태 실험의 점수는 고정된 [0,1] 성분과 버전별 가중치를 사용하고 후보 집합마다 min/max 정규화하지 않는다. PHOG·Rank의 서로 다른 점수를 합산해 후보를 제거하지 않는다. 해당 실험의 최종 선택은 시인성→형태 허용폭→실제 광도 순서이며, 동일 마스크에서 계산한 성분만 결합한다. 이 수치는 검증된 보편 최적값이 아니다.

동일 장소의 반복은 기본적으로 허용한다. 상위 후보를 이유 없이 무작위로 고르거나 최근 N프레임 재사용을 무조건 금지하지 않는다. 다양성 옵션이 필요해도 형태 허용 집합 안의 동률 해소에만 적용한다. 현재 반복 페널티는 사용하지 않는다.

영상으로 봤을 때 변화가 너무 약한지, 매칭보다 다양성에 끌려가는지를 평가한다. 지리 이동의 자연스러움이나 대륙별 서사는 필수 요구가 아니다.

#### 5.4 회전·기타 변환

기본 검색은 시계 방향 0·90·180·270도다. 0도는 기존 특징을 그대로 사용한다. 추가 방향은 같은 장소의 canonical RGB를 회전하고 중앙을 목표 비율에 맞춰 크롭한 뒤 공통 크기로 리사이즈해 특징을 만든다. 90/270도에서도 가로세로를 강제로 늘리거나 검은 모서리를 만들지 않는다. **16×12 배열을 회전한 12×16 배열을 그대로 flatten해 비교하지 않는다.**

추가 특징은 원래 인덱스의 `views/canonical-quarter-turn-center-crop-v1/`에 source shard별로 저장한다. 원본 인덱스와 0도 결과는 보존한다. 최초 생성은 기존 PNG·블록 캐시를 우선 사용하고, 없는 기본 사진만 실제 원본에서 읽어 `cache/base-photos/`에 보관한다. 완료한 view shard는 다음 검색에서 재사용한다.

실제 출력과 검색은 **동일한 canonical RGB→회전→정수 크롭→BOX 리사이즈 recipe**를 사용한다. `view_id`는 tile ID·회전·크롭·출력 크기·변환 버전에 연결된다. `window`는 원래 창이고 `rotation_crop`은 회전한 canonical 이미지의 픽셀 경계다. 이를 원본 affine로 역매핑한 `view_source_window`와 최종 footprint·중심 좌표를 별도로 기록하며 원래 footprint도 보존한다. 90/270도는 비율 유지를 위해 화각이 줄어들므로 점수 변화는 회전과 그 크롭을 합친 view 확장 효과다.

검색은 실제 장소 수 `candidate_count`와 방향별 `view_count`를 구분한다. 각 방향을 독립 후보로 순위에 넣고 동률은 view ID 순서로 정한다. 선택 사진은 view ID별로 캐시해 같은 장소의 다른 방향이 덮어써지지 않는다. `--rotations 0`은 동일 장소·타깃의 방향 고정 기준선이다.

30° 간격과 작은 크롭 실험은 `production/variants/`의 별도 bank를 쓴다. 회전한 사진 안에 들어가는 목표 비율 사각형에 원본 1px 여유를 두고, 작은 크롭은 그 사각형의 가로·세로 75%다. 기존 네 직각 view를 재사용하고 장소마다 기본 크기의 비직각 여덟 방향과 작은 크기의 열두 방향을 추가한다. 원색 canonical RGB의 nearest affine 변환과 BOX 32×24 특징이 검색·출력에 공통이다. 특징 생성은 nearest와 정수 RGB→L의 교환 성질로 명암부터 변환하며 픽셀 일치 테스트를 둔다. 원래 source window와 실제 source quadrilateral·지리 footprint·중심·변환 버전을 보관한다.

bank는 사진·메타데이터를 복사하지 않고 parent packing과 로컬 RGB를 참조한다. 새 uint8 특징만 사전 할당하며 정적 inventory와 작은 진행 manifest를 분리한다. 병렬 작업은 완료한 그룹을 원자적으로 공개하고 검색은 공개된 그룹만 읽는다. 파일 크기나 완료 view 수를 연속된 유효 prefix로 해석하지 않는다. 비교 검색은 같은 float64 정수 내적을 30°만/작은 크롭 포함의 두 Top-20에 공유하고 기존 Top-20과 정확히 병합한다. 기존 dense row는 유지하고 새 행에는 별도 offset과 view-space ID를 부여해 MS-SSIM 점수의 잘못된 재사용을 막는다. 동일 38–48초의 원본 300프레임 비교와 전체 프레임 제작은 별도 run으로 보존한다.

전곡은 `production/variants/full.py`에서 같은 원본의 모든 프레임을 검색한다. 원래 직각 Top-20과 완료한 10초의 affine 질의 결과를 재사용하고, 나중에 공개된 shard만 차집합으로 인덱싱한다. 부모 경로와 보존된 shard를 확인해 이전/최종 frozen source ID를 remap한다. 각 부모가 12방향×두 크기의 24 view를 갖는지 확인하며 120만 장소에서는 총 2,880만 view다. 후보군별 Top-20을 정확히 합치고 후보 배치별 체크포인트를 보존한다. int32 SSE partition 뒤 경계 동률만 전역 view rank로 해소하며 최종 K개는 SSE·rank 순서로 정렬한다. 정밀 점수 캐시는 검증된 여러 `preserved_view_spaces`의 행 범위에서만 허용한다. 실제 전곡 선택·렌더·입력 PTS/오디오 검증의 완료 여부는 PLAN에 기록한다.

명암 반전·강한 대비 변경은 별도 실험만 허용하며 기본 컬러 결과에 섞지 않는다. 반전 특징으로 검색한 후 원본 컬러를 그대로 내보내면 매칭 의미가 반대가 되므로, 그런 실험은 변환된 출력임을 명시하고 기준선과 별도로 비교한다. 실루엣 합성·픽셀 덧칠은 허용하지 않는다.

### 6. 선택 사진 재추출과 렌더 경계

**관련 요구: F7, F8, F11, F14, N1, N2, N4.**

선택한 후보는 `source_id + collection + item + asset`으로 다시 찾는다. 필요 시 접근 URL을 새로 서명한다. 저장한 소스 버전·메타데이터가 달라졌으면 동일 영상이라고 가정하지 말고 drift를 기록한다. 원본이 달라진 후보는 재검증·재색인 또는 다른 후보 선택이 필요하다.

동일 소스 창·방향·화각과 공통 픽셀 전처리 경로로 사진을 만든다. 저장된 PNG·블록 캐시를 우선 사용하며 정상 수신을 확인하려고 다시 읽지 않는다. 소스 변경이나 명백한 출력 이상이 있을 때만 특징 차이와 캐시 손상을 조사한다. 선택된 캐시 checksum과 transform recipe를 기준으로 이후 렌더를 재현한다.

재추출 실패는 재시도 후 차순위 후보를 사용할 수 있으나, `fallback_reason`, 원래 순위와 실제 선택 순위를 남긴다. 후보가 모두 실패하면 해당 run은 실패/미완성이다. 임시 검은 화면·원본 프레임·생성 이미지로 채우지 않는다.

최초 결과는 canonical 256×192(4:3 예시) 사진으로도 가능하다. 보기 쉽게 업스케일할 수 있지만 새 디테일이 생겼다고 주장하지 않는다. 고해상도 최종 재추출은 같은 지상 창을 더 큰 출력으로 읽는 것이며 임의로 다른 지형을 잘라 대체하지 않는다.

MP4는 일반 재생 호환성을 고려한 H.264/yuv420p 계열을 우선 검토하되 실제 FFmpeg build의 encoder 지원을 확인한다. 픽셀 크기를 짝수로 맞출 필요가 있을 때 콘텐츠를 왜곡하지 않는 방식으로 처리한다. 원본과 비교하는 split-screen은 검증 산출물이며 기본 작품 프레임의 “단일 장소”와 혼동하지 않는다.

좌표·촬영 연도·소스 오버레이는 선택이다. 초당 6번 바뀌는 지역명이 읽히지 않으면 본편은 사진만 보여주고 별도 manifest/비교 시트에서 정보를 제공한다. 좌표만으로 충분하며 역지오코딩 API는 기본 필수가 아니다. 지도·누적 위치 시각화는 후순위 옵션이고 허구의 지명을 생성하지 않는다.

#### 6.1 전체 프레임의 16:9 설명 화면

`production/layout.py`는 작은 원본, 세로 후보 최대 다섯 장, 큰 결과 사진과 선택을 따라가는 연결선을 배치한다. Top-20 view에서 최종 선택과 tile ID가 다른 실제 장소를 최대 다섯 곳까지 표시하고 검색 순위와 선택 위치를 구분한다. 한 장소의 여러 방향·크롭이 후보를 차지해 고유 장소가 적으면 실제 수만 표시하며 빈 슬롯에 연결선을 그리지 않는다. 타이틀·슬로건·중복 설명은 표시하지 않는다. 왼쪽에는 경과 시간·프레임 카운터·가느다란 진행선을 모으고 후보 아래에는 정렬된 번호·재평가 점수를 넣는다. 큰 사진 아래에는 실제 위경도·촬영일, 맨 아래에는 작은 출처 고지만 둔다. 후보 수·선택 위치별 연결선은 미리 안티앨리어싱해 캐시하며 사진의 색상·광도는 바꾸지 않는다.

후보 썸네일은 수집 RGB와 기존 10만의 공유 형태 RGB 캐시를 재사용한다. 최종 선택 중 원본 폭이 256px보다 큰 사진만 공개 visual asset에서 native RGB를 읽어 표시 디테일을 보강한다. SCL 재수신·정상 다운로드 확인은 하지 않는다. 동일 원본 창을 회전하고 canonical 크롭 경계를 native 픽셀로 비례 매핑한 뒤 BOX 리사이즈한다. 화면용 큰 사진은 같은 view/화각의 고해상도 표현이며 검색 점수는 보관한 canonical 특징의 값이다. 추가 픽셀 읽기가 실패하면 그 view의 보관된 canonical 사진으로 출력하고 사유를 기록한다.

인코딩은 180개 원본 프레임 단위로 나누고 두 프로세스가 각각 두 영상과 encoder당 2개 스레드를 사용한다. 각 완료 segment는 frame 범위·원본 PTS·render ID와 함께 공개해 재개 시 재사용한다. 마지막에는 재인코딩 없이 이어 붙여 `final-16x9.mp4`와 원래 콘텐츠 비율의 `satellite-full.mp4`를 만든다. 프레임 보간·추가·생략은 하지 않는다. `source_local`은 입력 AAC 트랙 전체를 복사하므로 원본에 있는 약 65ms의 오디오 꼬리는 영상보다 길 수 있다. 별도 엔딩 프레임을 추가하지 않는다.

`frame-plan.parquet`는 모든 source frame과 원본/후보/선택 이미지·메타데이터를, `selected.parquet`는 각 프레임의 실제 장소·view·출처를 연결한다. `search/matches.parquet`와 `query_inverse.npy`에서 전체 Top-K를 추적한다. 공개 업로드는 이 로컬 제작 경로에 포함하지 않는다.

후속 제작의 `reuse_run`은 입력·크롭·인덱스가 같은 완료 run의 타깃·기존 썸네일·native 사진을 같은 디스크의 hard link 또는 복사로 재사용한다. MSE 검색은 격자와 Top-K도 같을 때만 재사용한다. 생성한 테이블/manifest는 원자적 교체로 게시해 이전 run을 수정하지 않는다. 선택이 달라져 처음 필요한 큰 사진만 추가 읽는다. MS-SSIM 전곡도 비교 run과 같은 고유 `candidates.parquet`·`evaluations.parquet`와 인덱스/타깃/지표 버전을 남겨 다음 실험에서 점수를 재사용할 수 있다.

### 7. 보고서와 품질 의사결정

**관련 요구: F9, F12, F13, N3, N6.**

기본 보고 자료는 다음을 묶는다.

| 산출물 | 필요한 내용 |
| --- | --- |
| contact sheet | 시드가 고정된 무작위 후보 100~200개, 지형군·좌표·품질 태그. 보기 좋은 후보만 뽑지 않음 |
| Top-5 비교 | 고정 타깃 20개 × 실제 후보 5개, 원본/후보 명암과 점수·위치 |
| 영상 비교 | 같은 구간·같은 시간축의 원본/컬러 결과, 흑백 비교 선택 |
| 통계 | 장면·유효 후보·중복·지역/지형군/스케일/소스 분포·오류·자원 |
| 평가 | 잘 보이는 구간·실패 구간·시각 확인 여부·다음 실험의 근거 |

타깃 20개는 단순/복잡/단색/전환을 포함하도록 시간 구간에서 결정적으로 고른다. 개선 기법을 튜닝할 타깃과 고정 비교용 타깃을 구분하면 과적합을 줄일 수 있다. 타깃 목록과 재생 구간을 수집 규모마다 바꾸지 않는다.

1만·10만·조건부 100만 후보에서 raw best MSE의 평균/중앙값/상위 분위, 선택 타일 반복률, 지형 다양성, 처리 시간과 공간을 비교한다. 정확 검색에서 후보 집합이 단순히 늘고 metric이 같다면 best raw MSE는 나빠지지 않아야 한다. 달라졌다면 필터·정규화·index 변경 또는 오류를 점검한다. 다양성 페널티를 포함한 점수는 별도 지표다.

MSE 개선에 임의의 절대 “작품 성공” 기준을 붙이지 않는다. 원본 움직임 인식, 실제 사진으로 보이는지, 재미/질감은 재생해서 판단한다. 소규모 결과는 해당 표본에서의 관측이며, 기술 검증에 통과했다면 닮은 장면을 찾을 기회를 넓히도록 데이터셋을 확대한다. 작은 표본의 낮은 인식성을 확대 중단이나 방식 전체의 표현력 한계로 해석하지 않는다. 시각 확인 도구가 없으면 `visual_review=not_performed`로 남기고 자료를 만든다. 리포트나 체크리스트를 만들었다는 사실만으로 검토가 끝났다고 표시하지 않는다.

매칭이 나쁠 때는 원본·사진 추출/마스크와 공통 명암 처리의 정확성을 먼저 확인한다. 이후 지역·지상 스케일·후보 규모를 넓히고, 필요하면 NAIP·재정렬·회전을 각각 비교한다. 데이터셋은 가용 자원·접근 조건 안에서 최대한 넓고 다양하게 구축하며 중간 지점마다 품질과 자원 사용을 기록한다. 수집 중단은 자원·접근 제약 또는 충분히 넓은 탐색 근거에 따라 판단한다. 기본 콘셉트를 임의로 모자이크로 바꾸지 않는다.

### 8. 운영 한도와 오류 계약

**관련 요구: F2, F3, F10, F13, N3–N6.**

필수 설정은 총 후보 수, 장면 수/AOI, 검색 item 상한, 동시성, retry/timeouts, RAM·디스크 한도와 사용 가능 공간 하한이다. 구체적인 머신 성능은 P1에서 확인한다. 첫 실데이터 연결은 100~500 후보, 첫 수집 체크포인트는 1만 후보다. 기술·비용을 검증한 뒤 [PLAN](PLAN.md)에 다음 실행의 한도와 범위를 남기고 확대한다. 1만·10만·100만은 중간 비교 지점이며 최종 상한이 아니다.

비용이 생기는 endpoint·새로운 클라우드 리소스는 자동으로 선택하지 않는다. 카탈로그 무료와 전송비 무료를 분리한다. STAC/COG 인증 토큰은 요청 시점 메모리에서만 다루고, 예외 메시지·GDAL debug log·원본 item JSON에도 남지 않도록 마스킹한다.

| 상황 | 필수 동작 |
| --- | --- |
| MP4 없음 | 입력 의존 단계만 차단, 입력 경로 안내. 독립 구현/수집은 가능 |
| item/asset 없음 | 원인별 skip, 실제 처리 가능한 후보 수 보고 |
| 401/403 | 서명 만료·접근권·Requester Pays 여부 구분. 무단 자격증명/과금 활성화 금지 |
| 429/일시 5xx | 제한된 backoff, 요청 감속, 실패 시 checkpoint 보존 |
| SCL 없음 | 품질 불확실 표시, 휴리스틱·선택 후보 검토, 무조건 clear로 표시 금지 |
| 디스크/RAM 한도 | 새 배치 시작 전 중단, 완료 shard 보존 |
| 중단 신호/프로세스 실패 | staging과 공개 데이터 구분, 다음 실행에서 복구 |
| 특징 shape/버전 불일치 | 검색 중단, 필요한 index 재생성 범위 명시 |
| 재추출 실패 | 기록된 Top-K 대체 또는 run 실패, 가짜 프레임 생성 금지 |
| 시각 평가 불가능 | 수치·비교 산출물 제공, 시각 평가는 미실시 표시 |

네트워크 요청 수·바이트는 수집할 수 있는 범위만 기록한다. GDAL 내부 요청 수를 계측하지 못했다면 `unmeasured`로 표시하고 실제 읽은 픽셀 수와 혼동하지 않는다. CPU/RSS/디스크·장면 처리시간·오류율은 관측 가능한 지표로 시작한다.

### 9. 결과 manifest와 CLI 계약

**관련 요구: F1, F5–F11, N2, N4, N5.**

manifest에는 run ID, 입력 MP4 checksum과 stream 정보, 목표 구간·시각·크롭, 코드/의존성/설정 버전, 시드, source/catalog snapshot 식별, index ID, 후보 규모, metric/점수 정책, 이미지 변환, 실제 선택 목록, 캐시 checksum, 오디오/권리 상태, 결과 파일 목록을 담는다.

영상 공개용 manifest는 사용자 절대경로·계정·서명 토큰을 제거한 별도 export로 만든다. 원격 href 자체가 영구 식별자가 아닐 수 있으므로 collection/item/asset을 반드시 유지한다. 원본 촬영 연도를 제작 연도로 대체하지 않는다.

CLI는 다음 책임 경계를 제공하도록 설계하되 이름·플래그는 구현자가 정하고 실제 검증 후 README에 적는다. **아래는 이미 설치된 명령 목록이 아니다.**

| 동작 계약 | 주요 입력 | 주요 출력 |
| --- | --- | --- |
| 입력 분석·targets | 로컬 MP4, start, duration, sample fps, crop | probe, 타깃 프레임·시각·특징 |
| discover | 소스, AOI, 날짜·품질·검색 한도 | 선택 장면 catalog |
| collect | catalog, 다중 스케일, quota, 자원 상한, resume | 품질 검사된 index shard·작업 상태 |
| match | target/index ID, metric, top K, 실험 설정 | 후보 순위·점수·선택 목록 |
| materialize | matches, source resolver, fallback 정책 | 실제 사진 캐시·재추출 증거 |
| render | 선택 캐시, 출력 fps/크기, 컬러/흑백, 오디오 정책 | MP4와 프레임 매핑 |
| report | 고정 평가 프레임, index/run ID | contact sheet·Top-5·통계·평가 자료 |

각 단계는 선행 산출물과 설정을 검증하고, 성공 시 생성한 실제 경로를 반환한다. `--help`만 있는 stub이나 빈 MP4를 전체 구현 성공으로 처리하지 않는다. 사용자가 CLI 표면보다 핵심 알고리즘을 요구하면 내부 API와 최소 명령만 먼저 완성해도 되지만 단계별 재실행 가능성은 유지한다.

### 10. 출처·권리·비용 경계

**관련 요구: F2, F8, F11, F12, F13, N4.**

Copernicus Sentinel 데이터는 재사용·수정·배포가 허용되는 조건과 출처 표시 의무가 있다. 가공한 데이터를 공개할 때는 `Contains modified Copernicus Sentinel data [Year]` 형식의 고지를 사용하며, 실제 사용 자료의 연도를 채운다. 정확한 적용 조건은 공식 고지문을 따른다.[R10]

NAIP on AWS의 데이터 라이선스 표시는 Public Domain with Attribution이지만, AWS 원본 접근은 Requester Pays로 안내된다.[R4] 데이터 권리·호스팅 비용·카탈로그 운영 제한은 독립적으로 검토한다. 소스별 고지와 접속일을 실제 사용 기록으로 생성한다.

사용자가 MP4를 가지고 있다는 사실로 원본 영상·음원의 공개 이용 권한을 추정하지 않는다. 공개 여부를 자동 결정하거나 플랫폼에 업로드하지 않는다. 무음 결과도 원본 영상 관련 권리 검토를 대체하지 않는다.

개발 템플릿과 이 프로젝트 코드의 라이선스는 영상 데이터 라이선스와 별개다. 확인한 템플릿 트리에는 LICENSE 파일이 없으므로 MIT 등으로 임의 지정하지 않는다. 템플릿 출처·커밋은 [구성 이력](history/2026-11-01-workspace.md)에 보존한다.

## 기술 선택과 중요한 결정

<!-- 바꾸기 어려운 선택의 이유·대안·제약과 관련 요구사항을 기록한다. -->

| 선택 | 상태·이유와 대안 | 관련 요구 |
| --- | --- | --- |
| 한 프레임 = 한 장소 | 확정. 작품의 핵심. 모자이크는 별도 합의 없는 대체안 아님 | F7, F8, N1 |
| 컬러 기본, 흑백 비교 | 확정. 실제 사진성을 유지하면서 형태 검증도 제공 | F8, F9, N1 |
| Sentinel-2 우선 | 확정. 전 세계 다양한 공간 패턴을 하나의 소스로 검증. NAIP는 조건부 | F2, F12, N4 |
| STAC + COG 부분 읽기 | 채택. 검색과 픽셀 접근을 분리하고 원본 전체 보관을 피함 | F2, F3, N3 |
| 수집 특징 + 공유 RGB 분석 캐시 | 채택. 형태 특징 재생성과 네 회전·원색 출력을 무손실 shard에서 공유 | F5, F7, N2, N3 |
| NumPy 정확 MSE 기준선 | 확정. 단순하고 결과 해석 가능. FAISS/ANN/의미 embedding은 최초 필수 아님 | F6, F9, N6 |
| PHOG/Rank + 영역/SDF/Chamfer | 비교 실험으로 보존. 이번 사용자 재생 평가에서 MSE가 우세해 기본 제작 채택은 보류. 수식은 v2.1로 고정 | F6, F9, N1 |
| 지속 worker + Numba 정확 거리 커널 | 채택. 원격 읽기와 파일 공개는 단일 writer, 수치 처리는 8개 프로세스. 후보·해상도 축소로 속도를 맞추지 않음 | F6, N2, N3 |
| 불변 shard + SQLite 작업 상태 | 설계 채택. 구체적 파일 naming/배치 크기는 구현 시 결정 | F5, F10, N2 |
| 구간 비교 6fps/24fps hold, 최종은 전체 프레임 | 고정 실험 조건과 전곡의 원래 프레임/시간축을 각각 보존 | F1, F8, F13, F14 |
| 소규모 기술 검증→단계적 데이터셋 확대 | 확정. 가용 자원 안에서 최대한 넓게 구축하며 1만·10만·100만에서 계측. 작은 표본의 시각적 성공을 확대 조건으로 요구하지 않음 | F9, F13, N3, N6 |
| 기본 로컬 CLI·CPU | 채택. GUI/상시 서버/GPU/LLM 도입은 근거가 있을 때만 | N3, N5, N7 |

블록 크기·시드·구름율·stride·가중치는 설계 초기값이지 사용자 요구를 바꿀 수 없는 상수는 아니다. 실제 측정에 따라 바꿀 때 설정 버전과 비교 근거를 남긴다. 단일 장소·실제 사진·출처 추적 같은 목표 제약은 튜닝 파라미터와 다르다.

## 실행 환경

<!-- 지원 플랫폼과 런타임, 의존성 및 실행 조건을 설명한다. -->

Python 3.11 이상, uv, FFmpeg/ffprobe, 인터넷 접근, 쓰기 가능한 로컬 데이터 디스크를 전제로 한다. `.python-version`은 3.12로 고정한다. P1은 Windows 11 네이티브에서 uv·Python 3.12·Rasterio/GDAL로 검증했다. Linux/WSL2 및 전체 제작 파이프라인의 플랫폼 지원은 실행 확인 전이다. GPU·고정 VRAM 사양·클라우드 계정은 필수가 아니다.

| 용도 | 우선 후보 |
| --- | --- |
| STAC | `pystac-client` |
| 원격 래스터·변환 | `rasterio`/GDAL, `pyproj` |
| 배열·이미지 | `numpy`, `Pillow` |
| 형태·거리 계산 | `scipy`, `numba` — 현재 float32 영상·정확 격자 거리·capped 방향 비교 |
| 로컬 영상 디코드 | `av`(PyAV), 실제 PTS와 선택 구간 처리 |
| 컬럼 저장·상태 | `pyarrow`, 표준 라이브러리 `sqlite3` |
| 설정·공간 연산 | 표준 JSON, `shapely` |
| 자원 계측 | `psutil` |
| 테스트·코드 검사 | `pytest`, 필요 시 `ruff` |
| 선택 기능 | `planetary-computer`, `faiss-cpu`, `opencv-python-headless` 또는 동등한 경계 연산. MS-SSIM은 기존 SciPy로 구현 |

Python 설치·가상환경·의존성·실행은 uv로 관리한다. `pyproject.toml`과 `uv.lock`을 추적하고 `uv sync --locked`, `uv run`을 기본 경로로 사용한다. 불필요한 선택 의존성을 기본 설치에 모두 넣지 않는다. 컨테이너와 CI는 필요가 생겼을 때만 추가한다.

초기 예산은 P1 연결용 100~500 후보와 P2 1만 후보다. 구체적인 RAM/디스크 예산은 사용자 머신을 확인하여 정한다. 임의로 고성능 GPU나 수백 GB 여유 공간이 있다고 가정하지 않는다.

메모리·여유 공간은 블록 경계에서 확인하고, 파일별 디스크 합계 순회는 최대 15초 간격으로 제한한다. 겹치는 창의 품질 비율은 블록의 누적 마스크로 계산해 동일 픽셀을 창마다 다시 합산하지 않는다. 좌표 변환기·측지 객체도 CRS별로 재사용한다. 이 최적화는 품질 클래스·MSE·회전 recipe를 바꾸지 않는다.

## 배포

<!-- 설치·설정·배포·이전과 복구 과정을 설명한다. -->

배포는 소스와 설정·문서를 로컬에서 실행하는 방식이다. uv 의존성 관리와 수집·정확 검색·구간 실험 렌더·전체 프레임 제작 명령을 구현했고 README에 실행법을 둔다. `produce`는 원본 오디오 복사와 무음 모드를 제공하며 외부 공개·업로드 기능은 포함하지 않는다.

입력 MP4, catalog/index, 선택 사진 캐시, run manifest와 공유 설정을 구분한다. 작업 이동 시 큰 원격 원본 전체가 아니라 **완료 index·catalog·필수 선택 캐시·manifest·설정**을 옮긴다. 비밀값은 별도로 전달하며 결과 ZIP에 포함하지 않는다. 입력 MP4는 사용자가 지정한 경로로 별도 제공한다.

인덱스 손상은 manifest/행 매핑 검증으로 감지하고 손상된 shard만 가능한 범위에서 재생성한다. 캐시가 손상되면 원본 재추출 또는 기록된 Top-K 대체를 사용한다. 원격 영상이 사라졌다면 해당 증거와 로컬 보관 상태를 명시한다.

## 유지보수

<!-- 업데이트·호환성 및 외부 의존성 변경에 대응하는 방식을 설명한다. -->

collection/asset 변경에 대응하도록 소스 설정과 resolver를 분리하되 필요 이상의 추상화는 하지 않는다. 소스별 실제 band·dtype·CRS 계약을 확인하고, provider 변경은 새로운 source namespace로 기록한다.

feature/schema/extraction 버전이 달라지면 기존 인덱스와 호환 가능한지 명시한다. 실제 사용 데이터가 없는데 미래 호환 코드를 미리 복잡하게 만들지 않는다. 원격 최신 검색 결과와 기존 고정 catalog를 구분한다.

문서 역할은 [AGENTS](../AGENTS.md)를 따른다. 기능·사용 계약 변경만 SPEC/README에 반영하고, 버그 수정 경위는 중요한 경우 history에 남긴다. 과거 원문은 byte 보존용으로 유지하며 중복 갱신하지 않는다.

## 검증 접근

<!-- 수용 기준 중 검증할 계약·위험과 필요한 환경을 설명한다. -->

### 빠른 오프라인 검증

- **관련 요구:** F1, F3–F8, F10, F11, N1–N3, N5, N7.
- **확인 방법:** 합성/최소 fixture로 창 좌표·affine 중심·CRS 순서·90도 비율·타깃/후보 특징 일치·uint8 overflow 방지·MSE Top-K·동률·빈 후보·시간 배치·원자적 저장/crash 복구·토큰 제거를 테스트한다.
- **필요 환경:** 네트워크 없는 로컬 Python. FFmpeg를 쓰는 통합 검증은 별도 표기한다. 합성 fixture는 단위 테스트 용도이며 실제 결과로 보고하지 않는다.

### 실제 외부 데이터 검증

- **관련 요구:** F2–F5, F7, F10, F11, F12, N2–N4, N6.
- **확인 방법:** 2~3개 서로 다른 AOI의 실제 STAC item·COG 창을 읽고 mask·SCL 정렬·좌표·재추출을 확인한다. source access와 소량 이미지 증거를 보존한다. NAIP는 조건부 별도 실행한다.
- **필요 환경:** 인터넷과 소스 접근 가능 환경. 비용을 발생시키지 않는지 확인하고 작은 quota를 사용한다. 실패/차단을 단위 테스트 성공으로 대신하지 않는다.

### 실제 입력·영상 검증

- **관련 요구:** F1, F6–F9, F11, F13, N1, N2, N6.
- **확인 방법:** 사용자 MP4를 분석해 목표 60개를 만들고 실제 선택 사진으로 10초 MP4를 출력한다. 프레임 수·duration·PTS·audio sync·출처를 확인하고 실제 재생으로 원본 인식성을 평가한다.
- **필요 환경:** 제공된 MP4, FFmpeg, 선택 이미지 캐시, 사람이거나 실제 영상 확인이 가능한 검토 환경. 확인하지 않은 감각적 평가를 추정해 작성하지 않는다.

### 규모 확대 검증

- **관련 요구:** F3, F5, F6, F9, F10, F12, F13, N2–N4.
- **확인 방법:** 고정 타깃과 누적 후보 집합에서 1만/10만/100만 및 이후 점수·재생·메모리·저장·네트워크 계측을 비교한다. 기술적 정확성·공간/스케일 범위·자원·접근 조건을 확인해 다음 규모를 정한다. 작은 표본의 낮은 시각 품질만으로 확대를 중단하지 않는다.
- **필요 환경:** 실제 머신 상한·완료 인덱스. 코드를 실행하지 않은 시간·용량은 계획치로 표시한다.

미해결 기술 가정은 [PLAN](PLAN.md)의 P1(환경/접근/입력), P3(표현력), P4(10만 효과), P5(보강), P6(긴 영상)에서 해소한다. 테스트 개수나 특정 내부 함수 이름보다 SPEC의 관찰 가능한 수용 기준을 검증한다.

## 공식 참고 자료

2026-11-01 문서 구성 시 확인한 기준이다. 라이브 서비스 정책과 item의 실제 자산은 실행 시 다시 확인한다. 이 참고 목록은 프로젝트 설명을 대체하지 않으며 위 절에 필요한 계약을 직접 기재했다.

- **[R1]** [Earth Search Sentinel-2 C1 L2A collection](https://earth-search.aws.element84.com/v1/collections/sentinel-2-c1-l2a) — collection·visual·SCL 정의.
- **[R2]** [Earth Search NAIP collection](https://earth-search.aws.element84.com/v1/collections/naip) — image asset·RGBIR·컬렉션별 범위.
- **[R3]** [Sentinel-2 COG on AWS](https://registry.opendata.aws/sentinel-2-l2a-cogs/) — C1 자산과 계정 없는 접근 안내.
- **[R4]** [NAIP on AWS](https://registry.opendata.aws/naip/) — 영상 유형·라이선스·Requester Pays.
- **[R5]** [Microsoft Planetary Computer Python SDK](https://github.com/microsoft/planetary-computer-sdk-for-python) — sign/sign_inplace와 접근 도구.
- **[R6]** [Rasterio Windowed I/O](https://rasterio.readthedocs.io/en/stable/topics/windowed-rw.html) — 창·블록 읽기 의미와 비용.
- **[R7]** [Rasterio Transforms](https://rasterio.readthedocs.io/en/stable/topics/transforms.html) — 픽셀 중심·affine·좌표 변환.
- **[R8]** [Sentinel Hub SCL 분류 설명](https://custom-scripts.sentinel-hub.com/sentinel-2/scene-classification/) — 구름·그림자·NoData·눈 등 클래스 의미.
- **[R9]** [ffprobe 공식 문서](https://ffmpeg.org/ffprobe.html) — 입력 메타데이터 검사.
- **[R10]** [Copernicus Sentinel 데이터 고지문](https://cds.climate.copernicus.eu/licences/ec-sentinel) — 재사용·변경·출처 고지.
- **[R11]** [LINZ imagery 공식 저장소](https://github.com/linz/imagery), [New Zealand Imagery 공개 데이터 안내](https://registry.opendata.aws/nz-imagery/) — 정적 STAC·익명 접근·COG·컬렉션별 출처.

원문에서 제시한 추가 구현 참고: [Earth Search 안내](https://element84.com/earth-search/), [사용 예](https://element84.com/earth-search/examples), [공개 저장소](https://github.com/Element84/earth-search), [GDAL 원격 파일 시스템](https://gdal.org/en/stable/user/virtual_file_systems.html), [Planetary Computer 접근 개념](https://planetarycomputer.microsoft.com/docs/concepts/sas/), [USDA NAIP 데이터 설명](https://catalog.data.gov/dataset/national-agriculture-imagery-program-naip-imagery). 이 보조 링크의 서비스 접속과 실제 데이터 읽기는 구현 단계에서 확인한다.

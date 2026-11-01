# Bad Apple!! — 실제 위성·항공사진 기반 영상 재구성

> **개발 전달용 최종 기획 및 기술 사양서**  
> 작성 기준: 2026-11-01  
> 이 문서는 프로젝트 구현에 필요한 목표, 결정 사항, 데이터 출처, 수집·전처리·검색·렌더링 방식, 검증 기준, 단계별 계획을 독립적으로 포함한다. 다른 대화나 설명을 전제로 하지 않는다.

## 0. 한 줄 설명과 핵심 원칙

**Bad Apple!! 원본 영상의 각 프레임과 명암·형태가 가장 비슷한 지구상의 실제 위성사진 또는 항공사진을 대규모 이미지 컬렉션에서 찾아, 그 사진들만으로 영상을 재구성한다.**

- **프레임당 실제 장소 이미지 1장**을 기본 방식으로 한다. 여러 이미지를 조합한 포토모자이크는 별도 확장안이다.
- 최종 영상의 프레임은 **실제로 촬영된 한 장소의 영상 조각**이어야 한다. 원본 실루엣을 강제로 덧씌우거나 생성형 AI로 지형을 만들지 않는다.
- 매 프레임 서로 다른 장소가 나와도 된다. 캐릭터·배경·카메라의 연속성은 요구하지 않는다.
- 검색은 흑백·저해상도 특징량으로 하더라도 **출력은 실제 사진의 컬러를 유지하는 버전을 우선 검증**한다. 흑백 출력도 비교용 옵션으로 제공한다.
- 핵심 연구 질문은 **'실제 지구 사진 중 Bad Apple의 형태와 충분히 닮은 장면이 존재하는가?'**이다. 구현보다 먼저 실험으로 판단한다.
- 이미지 확보부터 최종 재인코딩까지 **다시 실행 가능한 파이프라인**을 구현한다.
- 대량 데이터를 모았다는 것 자체가 성과가 아니다. 수집량 증가에 따른 **매칭 품질 개선**을 측정하며 확장한다.

## 1. 결과물과 성공 기준

### 1.1 필수 결과물

1. 원본 영상 중 10초가량을 실제 위성·항공사진으로 변환한 `preview.mp4`.
2. 동일한 타임스탬프의 원본 프레임 / 선택된 실제 사진 / 두 이미지의 저해상도 명암 특징을 보여주는 비교 자료.
3. 수집한 후보 이미지의 **위치·촬영 일시·원본 출처를 추적**할 수 있는 메타데이터.
4. 1만 장 → 10만 장 → 필요 시 100만 장으로 확장 가능한 수집·검색 프로그램.
5. 수집 통계, 후보 검색 점수, 오류 및 최종 사용 영상의 출처 목록.

### 1.2 성공 판정

- **실제 사진성:** 출력에 합성 실루엣·생성 이미지·임의로 그려 넣은 지형이 없다.
- **인지 가능성:** 여러 프레임을 재생할 때 원본의 큰 형상 변화나 움직임을 알아볼 수 있다. 이는 정량 점수뿐 아니라 **실제 재생 영상을 보고 평가**한다.
- **검증 가능성:** 임의의 출력 프레임에서 원본 STAC item, asset, raster window, 촬영 시점과 중심 좌표를 찾고 다시 추출할 수 있다.
- **재현 가능성:** 동일 입력·설정·수집 인덱스·랜덤 시드로 동일한 매칭 목록을 재생성할 수 있다.
- **확장성:** 10만~100만 후보를 다룰 때 RAM에 원본 컬러 이미지 전체를 올릴 필요가 없다.

### 1.3 하지 않을 일

- 처음부터 전곡·고프레임·고해상도 완성에 매달리기.
- Google Maps / Google Earth의 지도 타일을 무단 대량 스크래핑하기.
- GPT Image 등 생성 모델로 프레임을 새로 그리기.
- 얼굴/사물의 일관성, 자연스러운 장면 간 이동, 가상의 카메라 워크를 필수로 만들기.
- 매칭이 안 되는 프레임을 임의의 합성 이미지로 **성공한 것처럼** 처리하기.

## 2. 우선 선택한 기술 및 데이터 소스

### 2.1 Sentinel-2 L2A — 전 세계 베이스 데이터

- **검색 API:** Element 84 Earth Search STAC v1 — <https://earth-search.aws.element84.com/v1>
- **컬렉션:** `sentinel-2-c1-l2a`
- **컬렉션 문서:** <https://earth-search.aws.element84.com/v1/collections/sentinel-2-c1-l2a>
- **우선 사용할 asset:** `visual` (True Color RGB, COG/GeoTIFF; 실제 item에서 존재 여부 확인)
- **보조 asset:** `scl` (Scene Classification Layer; 구름·그림자·NoData 검사용, 해당 item에 있을 때)
- **공간 해상도:** RGB 가시광 영상 약 10m/픽셀. 촬영 지역/영상에 따라 검증한다.
- **용도:** 대륙 규모로 다양한 해안선, 산맥, 강, 농경지, 사막, 도시 등의 패턴 확보.
- **라이선스:** Copernicus Sentinel 데이터는 원칙적으로 무료·개방 사용 및 수정·재배포 가능. **공개 시 출처 고지 필수**. 아래 '권리' 항목 참고.

Earth Search는 영상을 검색하는 **카탈로그**이며, `visual` asset이 가리키는 COG URL에서 영상 일부를 읽는다. **원본 전체 파일을 매번 다운로드하지 않는다.**

### 2.2 NAIP — 미국 고해상도 항공사진

- **검색 API:** 우선 위와 동일한 Earth Search STAC v1 사용.
- **컬렉션:** `naip` — <https://earth-search.aws.element84.com/v1/collections/naip>
- **우선 asset:** `image` (실제 item의 RGB/밴드 구성·COG 여부를 런타임에서 검증)
- **다른 접근 경로:** [NAIP on AWS](https://registry.opendata.aws/naip/)의 RGB 시각화 COG 컬렉션, 또는 [Microsoft Planetary Computer STAC](https://planetarycomputer.microsoft.com/api/stac/v1/). 후자는 asset 읽기에 임시 SAS 서명이 필요할 수 있다.
- **공간 해상도:** 촬영 연도·장소에 따라 대략 0.3~1m급(과거 일부는 더 낮음).
- **용도:** 도로, 주택가, 공장, 운동장, 공원, 경작지와 같은 미세한 시각 패턴 보강.
- **권리:** NAIP on AWS에는 **Public Domain with Attribution**으로 표시되어 있다. 가공 영상 공개 시 USDA 관련 출처를 표기한다.

**권장 순서:** Sentinel-2만으로 P0~P2를 완료한 뒤, 실제로 매칭이 잘 안 되는 프레임이나 패턴의 종류를 보고 NAIP를 추가한다. 초반부터 모든 소스를 동시에 수집하지 않는다.

### 2.3 대체·후순위

- **Landsat Collection 2:** 30m급 대규모 지형 패턴 보강용. 첫 실험의 필수 소스 아님.
- **OpenAerialMap:** 개별 영상마다 권리·영역·품질이 달라질 수 있으므로 자동 수집 전 별도 검토.
- **상업용 지도 서비스:** 데이터 획득 정책이 별도로 확인되기 전에는 사용하지 않는다.

**중요:** STAC 컬렉션이나 asset 구성은 변경될 수 있다. 컬렉션 ID를 기준으로 검색하고 실제 응답의 `item.assets`를 확인한다. `s3://` 또는 인증 필요 URL을 임의로 `https://`로 바꾸지 않는다.

## 3. 전체 데이터 파이프라인

```text
Bad Apple 원본 영상
 └─ 일정 간격으로 프레임 샘플링
     └─ 원본 프레임 특징량 추출 ───────────────┐
                                               │
지역별 수집 계획(AOI/시즌/소스)                 │
 └─ STAC 카탈로그에서 촬영 장면 검색           │
     └─ 장면 품질·중복·커버리지 판단           │
         └─ 원격 COG의 큰 블록을 부분 읽기     │
             └─ 여러 위치/스케일로 창 추출     │
                 └─ NoData·구름·중복 검사      │
                     ├─ 타일 특징량 저장 ─────┤
                     └─ 장면/위치 정보 저장   │
                                               ▼
                                      유사도 검색/재정렬
                                               │
                                 상위 K개 후보와 점수 기록
                                               │
                               최종 후보 원본 COG에서 재추출
                                               │
                                   사진 프레임 + 원본 음원
                                               │
                                 MP4 / 비교 시트 / 출처 리포트
```

원본 영상 처리와 영상 데이터 수집은 **별도 작업으로 독립 실행**할 수 있어야 한다. 영상 데이터 수집이 중단되어도 이미 만든 특징량과 검색 결과를 잃지 않아야 한다.

## 4. 데이터 수집 설계 — 가장 중요한 부분

### 4.1 전체 지구를 순회하지 말고 지역을 나눠 샘플링

처음부터 전 세계 모든 위성사진을 모으는 방식은 불필요하게 크고 느리다. **대표 지역을 배정해 공간적으로 분산된 데이터**부터 만든다.

초기 지역 유형은 다음을 포함한다.

| 지형군 | 후보 지역 예 | 원하는 패턴 |
|---|---|---|
| 해안·섬 | 동아시아 해안, 인도네시아 | 긴 흑백 경계, 굴곡 |
| 사막·건조지 | 북아프리카, 호주 | 광범위한 밝은 영역, 능선 |
| 산악 | 히말라야, 안데스 | 능선과 그림자 |
| 강·습지 | 나일 삼각주, 아마존 유역 | 굽은 선, 분지 |
| 농경지 | 미국 중서부, 유럽 평야 | 규칙적 패턴, 구획 |
| 도시 | 동아시아 대도시, 미국 대도시 | 도로망·블록 형태 |

- 수집할 공간 범위는 **GeoJSON 폴리곤 또는 작은 BBOX(AOI)** 목록으로 관리한다. 전 세계 단일 BBOX 검색은 피한다.
- 지역·지형 유형별 최대 장면 수/타일 수를 정해 특정 지역에 편중되지 않도록 한다.
- 같은 장소를 여러 번 촬영했다면 우선 가장 적합한 촬영분 하나를 사용한다. 계절이나 채광 차이가 실제 매칭에 도움을 줄 때만 추가한다.
- 원격 카탈로그 검색 결과는 페이지를 모두 읽거나 한도를 명시한다. 임의의 `max_items=5`를 전체 수집으로 오인하지 않는다.
- STAC 장면의 BBOX가 AOI와 겹친다고 해서 유효 픽셀 전체가 AOI 내부에 있지는 않다. 실제 **타일 중심 또는 footprint**가 수집 대상 범위에 속하는지도 검사한다.

### 4.2 장면 검색/선정

Sentinel-2 초기 기본값(조정 가능한 권장 초기값):

```yaml
source: sentinel-2-c1-l2a
asset: visual
time_range: ["2023-01-01", "2026-10-31"]
scene_cloud_cover_max_percent: 10
max_scenes_per_aoi_initial: 3
prefer_recent: false   # 최신성이 작품 목표는 아님
```

- `eo:cloud_cover`는 **장면 수준 수치**이므로 구름이 10% 미만인 장면에도 개별 타일에 구름이 있을 수 있다.
- 초기에는 scene-level cloud cover로 먼저 걸러낸다. 그다음 가능하면 `scl`을 이용해 **타일 수준**의 구름·구름 그림자 비율을 계산한다.
- SCL과 `visual`은 픽셀 해상도/격자가 다를 수 있으므로 같은 인덱스로 잘라서는 안 된다. **지리 좌표 기준으로 정렬해 nearest-neighbor 재투영/리샘플링**한다.
- SCL이 없더라도 수집 전체가 실패하지 않도록 RGB 마스크, 극단적인 저대비·과다 흰색 등 보조 휴리스틱을 사용한다. **눈·사막·흰 모래처럼 유효한 밝은 지형을 무조건 제거하지 않는다.**
- NAIP에는 동일한 `eo:cloud_cover` 속성이 없을 수 있으므로 컬렉션별 필터링을 분리한다.

### 4.3 다중 공간 스케일로 타일 추출

한 장면에서 다른 위치를 자르는 것뿐 아니라 **보는 범위 자체**를 바꿔야 한다. 예를 들어 10m/픽셀짜리 Sentinel-2 원본에서:

| 원본 소스 창(px) | 대략적인 지상 범위 | 출력 이미지 |
|---|---|---|
| 256×192 | 2.56×1.92km | 256×192 |
| 512×384 | 5.12×3.84km | 256×192 |
| 1024×768 | 10.24×7.68km | 256×192 |

- 위 예시는 **원본 목표 종횡비 4:3**일 때만 그대로 사용한다. 원본 영상을 분석해 실제 영상 콘텐츠 종횡비를 먼저 확정하고, 다르면 `window_width/window_height`를 수정한다.
- 스케일별 stride는 시작값으로 **창 너비·높이의 50%**를 사용한다. 지나치게 겹치는 후보가 많으면 75~100%로 늘려 비교한다.
- NAIP에서는 동일한 픽셀 창도 지상 범위가 훨씬 좁다. Sentinel-2와 NAIP 모두 256×192로 출력하더라도 **원본 GSD 및 지상 범위(m 단위)**를 메타데이터에 기록한다.
- 지리적으로 실제 하나의 연속된 장소를 잘라야 한다. 두 장면을 억지로 이어 붙인 타일은 기본 모드에서 허용하지 않는다.
- 영상의 픽셀·CRS·회전 여부를 보고 정확한 geo-transform을 사용한다. `width/2`만으로 위도·경도를 만들지 않는다.

### 4.4 원격 COG 읽기 최적화

**원격 장면 하나를 열고, 가능한 한 큰 블록 단위로 읽어 메모리에서 여러 후보 타일을 만든다.** 타일 하나마다 파일을 다시 열거나 개별 HTTP 요청을 반복하지 않는다.

- `rasterio`/GDAL을 사용하고 COG asset을 `rasterio.open(href)`로 연다.
- 초기 원본 읽기 블록: 예를 들어 **2048×2048px**. 실제 COG 블록 크기(`block_shapes`) 및 메모리 성능에 맞춰 조정.
- 커다란 창(예: 1024×768)이 블록 경계를 넘어갈 수 있으므로, 블록 간 **halo/overlap**을 포함하거나 장면별 윈도우를 사전에 생성하여 중복 없이 담당 블록에 배정한다.
- 전체 장면을 한 번에 RGB 배열로 읽지 않는다.
- GDAL의 원격 range read, 타일 캐시를 활용한다. 초기에는 네트워크 병렬 장면 수를 2~4개 정도로 제한하고 서버 응답에 따라 조절한다.
- 429·503·네트워크 오류 시 지수 백오프 및 제한된 재시도. STAC 카탈로그 검색과 영상 COG 읽기의 실패를 별도로 기록한다.
- 소스 URL이 임시 서명 URL이면 만료된 토큰을 파일에 그대로 보관해 재사용하지 않는다. **컬렉션·item_id·asset_key**로 원본을 다시 찾고 서명한다.

### 4.5 타일 품질 검사

실제 콘텐츠가 없는 후보를 대량 보관하면 인덱스만 커진다.

필수 검사:

1. `dataset_mask` 또는 NoData 마스크 기준으로 유효한 픽셀 비율 검사(초기 기준 95% 이상).
2. 구름·구름 그림자 검사(Sentinel-2의 SCL 사용 가능 시). 단, 자연적인 흰색 지형을 오검출하지 않게 할 것.
3. 극단적인 단색/검은 화면/완전 흰 화면 후보를 분류하여 별도 태그 저장. 자동 삭제 기준은 시각적으로 검증.
4. 이미지 크기, 채널 수, dtype, 이미지 디코딩 오류 검사.
5. 동일 장면·인접 윈도우에 의한 중복 검사. 중복의 원본 위치는 보존할 수 있도록 기록.

**주의:** Bad Apple은 거의 완전 흑백이므로 유사한 '거의 단색 이미지'가 오히려 일부 프레임에 필요할 수 있다. 분산이 낮다는 이유만으로 전부 제거하면 안 된다. 기본은 **품질 태그 부여**, 제거는 NoData/오염 등 명확한 결함 위주로 한다.

## 5. 이미지 특징량과 로컬 데이터 저장

### 5.1 '모든 이미지 저장'보다 '특징량 + 원본 위치 저장' 우선

권장 구현은 **인덱스 중심의 하이브리드 캐시**이다.

- 수집 시 각 타일의 낮은 해상도 표현과 원본 위치·추출 윈도를 보관한다.
- 일단 인덱스를 구축한 후 선택된 상위 후보의 **컬러 이미지**만 원본 COG에서 다시 추출해 WebP/JPEG로 캐싱한다.
- 원격 데이터가 일시적으로 읽히지 않을 수 있으므로, 필요하면 상위 후보 **수백~수천 장**만 저장하는 '선택 후보 캐시'를 유지한다.
- 유사도 알고리즘을 자주 바꾸거나 여러 곡에 활용할 계획이면 **전체 축소 썸네일 캐시**를 선택적으로 활성화한다.
- 최종 이용할 이미지를 만드는 단계에서 원본 장면이 실제로 재접근 가능한지 반드시 검증한다.

### 5.2 처음 저장할 특징량

| 필드 | 권장 표현 | 목적 |
|---|---|---|
| 밝기 패턴 | 16×12 `uint8` | 빠른 전수 검색/1차 필터 |
| 세부 명암 | 32×24 `uint8` | 상위 후보 재정렬 |
| 큰 형태 통계 | 4×4 셀 밝기/밝은 픽셀 비율 | 면적·위치 관계 |
| 윤곽/그라디언트 | 32×24 edge 또는 gradient | 경계와 큰 구조 |
| 색채 통계 | 평균 RGB/채도 등 선택 | 출력 다양성 분석 |
| 근접 중복용 해시 | pHash/dHash 등 | 거의 같은 후보 관리 |

- 특징 추출에는 그레이스케일 변환, 다운샘플링, 필요 시 CLAHE/대비 정규화·이진화 등을 사용할 수 있다.
- **원본 영상과 위성사진에 동일한 변환을 적용**해야 한다. 흑백 기준값·동적 범위·정규화 방식이 어긋나면 검색 품질이 흔들린다.
- 강제 이진화 하나만 저장하지 않는다. 후보 영상의 실제 명암을 비교할 수 있도록 연속값 grayscale도 남긴다.
- 형상 검색에서 의미적 유사도(CLIP 등)보다 **픽셀 명암 배치/큰 구조**를 우선한다. 텍스트 프롬프트 임베딩은 기본 검색에서 제외한다.

### 5.3 디스크 예상치

256×192 컬러 WebP가 **평균 30KB/장이라는 가정**을 둔 계획용 추정이다. 실제 값은 샘플 1만 장을 저장해 측정한다.

| 타일 수 | 전부 WebP 저장 시 | 16×12 uint8 특징량만 |
|---:|---:|---:|
| 10,000 | 약 0.3GB | 1.92MB |
| 100,000 | 약 3GB | 19.2MB |
| 1,000,000 | 약 30GB | 192MB |
| 5,000,000 | 약 150GB | 960MB |

표에는 메타데이터, 인덱스 구조, 보조 특징량, 캐시, 파일시스템 오버헤드가 제외되어 있다. **32×24 uint8 특징량을 추가하면 장당 768바이트**가 더 필요하다.

### 5.4 데이터 모델 (권장)

```text
data/
  config/
    sources.yaml            # API, collection, 자산 종류, 접근 규칙
    areas.geojson           # 수집 AOI 목록
    sampling.yaml           # 시기, 구름, 스케일, stride, quota
  catalog/
    scenes.parquet          # 장면 목록, 품질, COG 자산 정보
    scene_jobs.sqlite       # 수집 상태/실패/재시도
  index/
    tiles.parquet           # 타일 ID, scene ID, window, 좌표, feature row
    gray16x12.npy           # uint8 [N, 192]
    gray32x24.npy           # uint8 [N, 768]
    edges.npy               # 선택
    faiss.index             # 선택: 필요한 시점에 생성
  cache/
    selected/               # 선택된 실제 컬러 사진만
  targets/
    frames/                 # Bad Apple에서 샘플링한 원본 프레임
    target_features.npz     # 타깃 특징량
  results/
    matches.parquet         # frame -> 상위 후보/선택 타일/점수
    preview.mp4
    compare/                # contact sheet, top-k 비교 이미지
    manifest.json           # 소스/기술/라이선스/설정/재현 정보
    stats.json              # 수집·검색 품질 통계
```

권장 `scenes` 필드:

```text
scene_id, source_id, collection_id, item_id, asset_key,
asset_href_or_resolver, acquired_at, cloud_cover, footprint,
crs, width, height, dtype, bands, nominal_gsd,
selected_aoi_id, processing_status, error, retry_count
```

권장 `tiles` 필드:

```text
tile_id, scene_id, feature_row, window_x, window_y,
window_width, window_height, latitude, longitude,
source_gsd, ground_width_m, ground_height_m,
valid_pixel_ratio, local_cloud_ratio, variance,
phash, extraction_config_version
```

- `tile_id`는 `scene_id + 윈도 좌표/크기 + 전처리 버전`을 정규화한 문자열의 해시로 만든다. 동일한 입력을 다시 수집해도 중복되지 않아야 한다.
- `feature_row`는 NumPy 배열 행 번호와 일치하도록 한다. 타일 필터링/재정렬 후에도 매핑이 깨지지 않도록 인덱스 빌드 버전을 관리한다.
- 위·경도는 **타일 중심점**의 WGS84(EPSG:4326)로 저장하고, 원본 CRS와 변환값도 기록한다.
- 서명 URL에 포함된 SAS/토큰은 로그·메타데이터에 저장하지 않는다.
- `scenes` 및 `tiles`에 실제 촬영 연도와 출처를 보존하여 최종 크레딧을 자동 생성한다.

## 6. Bad Apple 원본 프레임 처리

초기 프로토타입 기본값:

| 항목 | 설정 |
|---|---|
| 테스트 길이 | 10초 |
| 매칭 샘플링 | 6fps (60개 프레임) |
| 프레임 공간 비율 | **원본 실제 콘텐츠 비율을 분석해 확정**; 아래 코드는 4:3 예시 |
| 첫 특징 | 16×12 grayscale, 32×24 grayscale |
| 최종 출력 영상 | 24/30fps 인코딩, 매칭된 장면을 시간에 맞춰 반복 표시 |
| 기본 사진 색상 | 원본 컬러 유지, 흑백 렌더도 비교 |

- 영상의 해상도와 실제 내용 비율(검은 여백·레터박스 포함 여부)을 먼저 확인한다. 원본 콘텐츠를 찌그러뜨리며 4:3에 맞추지 않는다.
- 영상 원본에서 시각적으로 다양한 프레임이 나오는 구간을 10초 선정하고, 시작 오프셋을 설정값으로 관리한다.
- 6fps 매칭 결과를 단순히 6fps로만 재생하는 대신 24/30fps MP4로 렌더링하며 같은 타일을 해당 시간 동안 **hold**한다. **프레임 보간·모핑·임의 crossfade는 기본적으로 사용하지 않는다.**
- 오디오가 있다면 **선택한 10초 구간의 음악을 정확히 동기화**한다. 영상과 오디오의 구간 시작점이 일치해야 한다.
- 영상/음원 사용 권리가 확보되지 않았다면, 내부 테스트는 무음 영상으로도 실행 가능하게 만든다. 공개 업로드는 별도 권리 확인 후 결정.

## 7. 매칭 알고리즘

### 7.1 베이스라인

원본 대상 프레임을 `T`, 후보 사진을 `C`라 하면, 저해상도 grayscale에서:

```text
D_gray(T, C) = mean((gray16(T) - gray16(C))^2)
```

각 타깃에 대해 `D_gray`가 가장 낮은 후보를 선택한다. **이 단순한 베이스라인을 실제로 돌려보기 전에 복잡한 AI 검색으로 가지 않는다.**

### 7.2 개선 검색

베이스라인 결과가 불충분하면 아래 순서로 추가한다.

1. **1차 전수 검색:** 16×12 명암 벡터의 거리로 각 타깃의 top 100~500 후보 추출.
2. **2차 정밀 재정렬:** 32×24 명암 MSE, 명암 분포, 윤곽/gradient 일치도, SSIM 등을 조합.
3. **회전 검색:** 0/90/180/270도 회전된 후보 특징을 비교하는 옵션. 회전한 결과는 실제 같은 위치에서 촬영한 사진의 방향 변경이므로 별도 이미지 생성은 아니다. 회전값을 저장한다.
4. **다양성 제약:** 너무 같은 장소만 연속 선택되면 직전 N프레임의 동일 타일/가까운 좌표에 페널티 부여. 다만 **형태 매칭이 현저히 악화되면 다양성보다 유사도를 우선**한다.

예시 점수(초기값이 아닌 튜닝 대상):

```text
score = 0.55 * normalized_gray_MSE
      + 0.25 * normalized_edge_distance
      + 0.20 * normalized_coarse_shape_distance
      + reuse_penalty
```

거리 척도마다 범위가 달라서 **그대로 가중합하기 전에 분포를 보정**해야 한다. 최초에는 위 수치보다 **MSE 단독 베이스라인**을 우선 검증한다.

### 7.3 인덱스 구성의 선택

- **하나의 곡/짧은 구간 테스트:** 수집 중 타일을 배치로 읽고 원본 프레임 60개와 즉시 비교한 뒤 **프레임당 top K**만 유지하는 스트리밍 매칭이 가장 단순하다. FAISS가 반드시 필요한 것은 아니다.
- **다른 영상·알고리즘으로 반복 실험:** 모든 타일 특징량을 `npy`/Parquet로 남기고 `faiss-cpu`의 `IndexFlatL2` 또는 동등한 전수 검색 인덱스를 추가한다.
- 벡터화 NumPy/FAISS로 배치 처리한다. **Python 이중 루프로 수백만 장 × 수천 프레임을 픽셀별 비교하지 않는다.**
- 검색 결과에는 선택 후보 하나뿐 아니라 **Top-5~Top-20과 원점수/정규화 점수**를 남긴다. 나중에 시각적으로 재선택하기 위함이다.

### 7.4 매칭 품질이 낮을 때의 대응 순서

1. 출처 타일이 제대로 추출됐는지, NoData/흰 구름이 많이 남지 않았는지 확인.
2. 원본 프레임·위성사진의 **밝기 정규화/그레이스케일 처리**가 합리적인지 검사.
3. 타일의 지상 범위(스케일)를 바꿔 검색.
4. 지형 다양성이 부족한 지역 유형만 추가 수집.
5. NAIP 등 별도 해상도의 소스를 추가.
6. 경계·실루엣 기반 재정렬, 회전 허용, 밝기 반전 검색 **실험**. 단, 선택한 실제 출력 사진이 조건과 다르게 변형되면 변형 내역을 명시.
7. 그래도 안 되면 **원본 실루엣을 1장짜리 실제 사진으로 재현하는 것 자체가 제약**이라고 보고 결과를 정직하게 평가한다. 강제 원본 마스크 합성은 금지.

## 8. 수집 규모, 반복 실행, 성능

### 8.1 단계별 후보 수

| 단계 | 목표량 | 목적 | 진행 조건 |
|---|---:|---|---|
| P0 | 100~500장 | STAC/COG/좌표 추출 확인 | 여러 지역에서 실제 이미지 열림 |
| P1 | 10,000장 | 지역 수집·전처리·중복 검사 | 랜덤 contact sheet와 Top-5 결과 확인 |
| P2 | 100,000장 | 10초 영상 인식성 확인 | 품질이 나쁘면 데이터량보다 원인 분석 |
| P3 | 최대 1,000,000장 | 지역·스케일 다양성 확장 | P2보다 매칭/인지 품질이 실제 개선될 때만 |
| P4 | 필요한 수만큼 | 전곡 등 추가 제작 | 저작권·품질·저장·재처리 검증 후 |

10만 장까지는 일단 **특징량 우선 저장**으로 구현한다. 전부 WebP로 저장하는 설정은 후속 옵션이다.

### 8.2 재시작/중복 방지

`scene_jobs.sqlite` 등에 `PENDING/RUNNING/DONE/FAILED`를 저장한다.

- 같은 source item, asset, AOI, window, config 버전 조합은 재처리해도 동일 tile ID가 되어야 한다.
- 완료한 장면은 다시 다운로드하지 않는다. 실패한 장면은 재시도 시간·횟수를 남긴다.
- 수집 도중 중단되더라도 특징량·Parquet 목록이 불일치하지 않도록 **배치 단위 원자적 커밋** 또는 staging→commit 전략 사용.
- 결과 프레임용 후보 이미지를 나중에 재읽지 못하는 경우를 대비해 `cache/selected` 보관.

### 8.3 기록할 성능/품질 지표

- 요청 횟수, 다운로드 바이트, 장면당 처리 시간, COG 부분 읽기 실패율.
- 수집 장면 수 / 유효 타일 수 / 중복 제거율 / AOI·지형군·스케일별 타일 수.
- 타깃 프레임별 최상위 MSE 및 상위 5개 점수, 점수 분포.
- 1만/10만/100만 장 시점별 **동일 프레임 세트**에서의 매칭 점수 개선량.
- 재생 결과에 대한 짧은 정성 평가: '원본 형태가 보임/일부 구간만 보임/거의 알아볼 수 없음'.
- 저장 공간 및 메모리 최고 사용량.

## 9. 실행 가능한 연결 검증 예제 (P0)

아래는 **Earth Search Sentinel-2의 실제 COG에서 작은 영역을 읽어 WebP로 저장하고 지리 중심 좌표를 계산하는 샘플**이다. 본격적인 수집기는 아래 예제를 확장하여 AOI 목록, 다중 스케일, 블록 읽기, 품질 필터, 메타데이터 저장 등을 추가한다.

```bash
python -m pip install pystac-client rasterio pillow numpy pyproj
```

```python
# p0_sentinel.py
from pathlib import Path

import numpy as np
import rasterio
from PIL import Image
from pyproj import Transformer
from pystac_client import Client
from rasterio.enums import Resampling
from rasterio.windows import Window

OUT = Path('p0_output')
OUT.mkdir(exist_ok=True)

stac = Client.open('https://earth-search.aws.element84.com/v1')
items = list(stac.search(
    collections=['sentinel-2-c1-l2a'],
    bbox=[126.5, 34.0, 128.5, 37.0],  # 실험용 한반도 일부
    datetime='2024-01-01/2025-12-31',
    query={'eo:cloud_cover': {'lt': 10}},
    max_items=3,
).items())

if not items:
    raise RuntimeError('No STAC items; retry with another AOI/time range')

with rasterio.Env(GDAL_DISABLE_READDIR_ON_OPEN='EMPTY_DIR'):
    for item in items:
        if 'visual' not in item.assets:
            print('skip: visual asset unavailable:', item.id)
            continue

        # Earth Search public HTTPS COG. 유효 여부는 실제 열어서 확인.
        href = item.assets['visual'].href
        with rasterio.open(href) as src:
            if src.count < 3:
                print('skip: non-RGB:', item.id)
                continue

            w, h = 512, 384  # 원본 영상 4:3 가정. 실제 비율에 맞춰 변경.
            if src.width < w or src.height < h:
                continue

            x = (src.width - w) // 2
            y = (src.height - h) // 2
            window = Window(x, y, w, h)

            # visual은 보통 uint8 RGB. 이 가정이 아니면 명시적 스트레칭 필요.
            if src.dtypes[0] != 'uint8':
                print('skip: expected uint8 visual, got', src.dtypes[0])
                continue

            rgb = src.read(
                (1, 2, 3),
                window=window,
                out_shape=(3, 192, 256),
                resampling=Resampling.average,
            )
            valid = src.dataset_mask(window=window)
            valid_ratio = float(np.mean(valid > 0))
            if valid_ratio < 0.95:
                print('skip: low valid pixel ratio', item.id, valid_ratio)
                continue

            image = Image.fromarray(np.moveaxis(rgb, 0, 2))
            image.save(OUT / (item.id + '.webp'), quality=85)

            # 중심 좌표는 원본 픽셀 -> 원본 CRS -> WGS84 순서로 변환.
            center_x, center_y = src.xy(y + h / 2, x + w / 2)
            lon, lat = Transformer.from_crs(
                src.crs, 'EPSG:4326', always_xy=True
            ).transform(center_x, center_y)
            print(item.id, 'lon=', round(lon, 5),
                  'lat=', round(lat, 5), 'valid=', round(valid_ratio, 3))
```

**예제의 의도/한계:** P0의 연결·좌표·출력 검증용으로 **장면 중앙의 한 장만** 만든다. 중앙이 검색 AOI 안에 있는지 보장하지 않으므로 본 수집기에서는 AOI 교차검사를 추가한다. 네트워크 접근·계정·실제 컬렉션의 이미지 상태에 따라 실행 가능성은 P0에서 확인해야 한다. SCL 기반 구름 제거, 원격 블록 순회, 메타데이터 누적 저장은 별도 본 구현 대상이다. NAIP는 별도 adapter를 사용하고 `image` asset의 RGB 밴드·dtype·URL 인증 여부를 검사한다.

## 10. 구현 모듈 및 제안 CLI

**권장 기술 스택:** Python 3.11+, `pystac-client`, `rasterio`/GDAL, `pyproj`, `numpy`, `Pillow`, `pyarrow`, `sqlite3`, 선택적으로 `opencv-python`, `faiss-cpu`; 최종 영상 조립은 `ffmpeg`. GPU는 MVP의 필수 조건이 아니다.

프로젝트 구조 예시:

```text
badapple-earth/
  README.md                 # 설치·실행·권리 요약
  config/
    default.yaml
    areas.geojson
  src/badapple_earth/
    sources/                # EarthSearchSentinel2, EarthSearchNAIP adapters
    discover.py             # STAC 검색/장면 선별
    collect.py              # COG 블록 읽기/다중 스케일 타일화
    quality.py              # mask/SCL/중복/품질 태그
    features.py             # 타깃 및 타일 공용 특징 추출
    storage.py              # Parquet/NumPy/SQLite
    match.py                # 베이스라인 + TopK 검색/재정렬
    materialize.py          # 실제 원본에서 선택 프레임 재추출
    render.py               # 프레임→영상, 동기화, 출처
    report.py               # contact sheet/통계/비교 보고서
    cli.py
  tests/
    test_window_coordinates.py
    test_feature_consistency.py
    test_reprocessing.py
    test_matching.py
  data/                     # .gitignore
```

설계할 CLI(아직 구현되지 않은 명령 예시):

```bash
# 1. 프로젝트 입력 영상과 10초 목표 프레임 준비
badapple-earth targets --input badapple.mp4 --start 30 --duration 10 --sample-fps 6

# 2. 지역별 수집(예: 1만 후보)
badapple-earth collect --source sentinel-2 --areas config/areas.geojson --limit 10000

# 3. 품질 보고/샘플 이미지 확인
badapple-earth report --contact-sheet 200

# 4. 매칭 및 검색 품질 점검
badapple-earth match --top-k 20 --metric gray-mse
badapple-earth report --topk-examples 20

# 5. 실제 사진 재추출 및 10초 렌더
badapple-earth materialize --rank 1
badapple-earth render --output data/results/preview.mp4 --fps 24
```

CLI 이름·플래그는 구현자 판단으로 바꿀 수 있지만 각 기능은 별도로 반복 실행 가능해야 한다. **실제 파일이 없는 상태에서 전체 파이프라인이 실행된다고 가장하는 dummy success는 금지**한다.

## 11. 최종 렌더링과 사용 위치 표시

- 기본: 매칭된 실제 사진으로 전체 화면을 채우고, 선택된 구간의 음악과 동기화한 MP4 생성.
- 기본 버전은 사진의 고유한 컬러를 보존한다. **검색을 위한 밝기 정규화는 출력 파일에 영향을 주지 않는다.**
- 검증용 출력: 원본과 결과를 좌우로 비교하는 split-screen, 흑백 버전, 타깃/선택 사진의 명암 특징 비교.
- 선택 버전: 화면 구석에 좌표, 촬영 연도 또는 지역명, 데이터 소스 표시. 초당 여러 번 장소가 바뀌어 글자가 읽히지 않으면 **영상 구석 고정 텍스트 대신 설명란/별도 지도**로 제공.
- `matches.parquet`에 타임스탬프, source ID, tile ID, 유사도, 원본 좌표, 회전 여부를 저장한다.
- 최종 영상에서는 사진의 위치를 **알 수 없는 임의 장소로 지어내지 않는다.** 지역명 역지오코딩은 선택 옵션이며, 좌표만으로도 충분하다.

## 12. 포토모자이크는 별도의 확장안

기본 A 방식에서 원본 실루엣이 거의 보이지 않는 경우 **B 방식(포토모자이크)** 을 실험할 수 있다.

- A: **원본 프레임 하나 ↔ 실제 장소 사진 하나**. 본 프로젝트의 고유한 매력이 크고, 가장 먼저 검증할 방식.
- B: 프레임을 8×8 등 여러 칸으로 분할하고 각 칸을 실제 장소의 사진으로 채움. 원본을 재현하기 쉬워지는 대신 **하나의 프레임이 실제 한 장소라는 원래 개념은 사라진다.**
- B가 성공하더라도 A의 결과를 덮어쓰지 않는다. 별도 렌더 모드와 설명을 제공한다.
- 원본 실루엣 위에 강제로 사진 질감을 입히는 방식, 밝고 어두운 픽셀을 임의로 칠하는 방식은 본 프로젝트의 A/B 어느 쪽의 기본 사양도 아니다.

## 13. 단계별 실행 계획과 완료 조건

### P0 — 접근 검증

- [ ] Sentinel-2 STAC에서 서로 다른 지역 2~3개에 대해 item 조회.
- [ ] `visual` COG에서 실제 픽셀 영역 읽기(HTTP range 접근 확인).
- [ ] PNG/WebP 저장, 타일 중심 좌표 계산, 동일 윈도 재추출 확인.
- [ ] NAIP는 후보 소스로 연결 가능 여부·asset 키·밴드·인증 정책만 점검.
- [ ] 원본 Bad Apple 파일의 프레임 비율/길이/오디오 트랙 확인.

**통과 기준:** 실제 영상 이미지를 몇 장 눈으로 확인하고, STAC 원본 위치와 다시 일치시킬 수 있다.

### P1 — 1만 장 수집기

- [ ] AOI 설정 및 지역/스케일별 quota.
- [ ] 장면 선택, 3단계 윈도 크기, stride, 블록 기반 추출.
- [ ] NoData/지역 내 포함/구름·중복 품질 검사.
- [ ] `scenes.parquet`, `tiles.parquet`, 특징량 배열 및 작업 상태 저장.
- [ ] 재시작/중복 제거 검증.
- [ ] **랜덤 100~200장 contact sheet** 생성.

**통과 기준:** 사람이 사진을 봤을 때 여러 지형 유형이 충분히 섞여 있고, 누락/블랙 이미지가 적다.

### P2 — 10초 영상 / 10만 후보

- [ ] 10초 6fps 타깃 추출.
- [ ] 1만 장 후보에서 먼저 MSE TopK 매칭.
- [ ] **20개 타깃 프레임에 대해 Top-5 비교 이미지** 생성.
- [ ] 선택 후보만 COG에서 재추출하고 실제 컬러 10초 MP4 제작.
- [ ] 10만 후보까지 수집한 뒤 **동일 타깃 프레임**에서 점수/영상 품질 비교.
- [ ] 잘 맞는 프레임과 안 맞는 프레임의 특징 분석.

**통과 기준:** 이 방식이 흥미롭게 보이는지, 실제 원본 인식성이 존재하는지를 **재생 영상으로 판단**한다. 실패하더라도 근거가 있는 평가 리포트를 남긴다.

### P3 — 개선과 확대(조건부)

- [ ] 필요할 때만 NAIP 추가·회전 검색·더 다양한 스케일·형태 기반 재정렬.
- [ ] 100만 후보까지 늘릴 경우 품질 향상 곡선 측정.
- [ ] 효과가 거의 없으면 수집만 계속하지 않고 목표/알고리즘 재검토.
- [ ] 품질·리소스·사용권 확인 후 30초 또는 전곡으로 확대.

## 14. 구현자가 주의할 주요 실패 지점

| 문제 | 이유 | 대응 |
|---|---|---|
| 별로 닮지 않음 | 자연 사진의 명암 분포는 이진 실루엣과 다름 | **핵심 리스크**. 1만 장 결과로 즉시 확인, 스케일·지형·검색 기준 변경 |
| 새하얀 사진이 많이 선택됨 | 구름·눈/사막·촬영 조건이 유사도 왜곡 | SCL·로컬 품질 확인, 정상적인 밝은 지형은 보존 |
| 새까만 사진이 많이 선택됨 | NoData/검은 이미지가 잘못 유사도 1등 | raster mask 필수, 결함 분리 |
| 너무 비슷한 지역만 반복 | 중첩 창/패턴 편중 | 지리적 quota, 중복 필터, soft reuse penalty |
| COG 조회가 매우 느림 | 작은 window 반복 요청/장면별 open | 원격 블록 캐시/큰 window/제한된 동시성 |
| 선택한 사진을 다시 못 받음 | 원본 URL·임시 토큰 만료, 서비스 오류 | STAC item 재조회·재서명, 상위 후보 사진 캐시 |
| 사진의 위치가 틀림 | 픽셀/CRS 변환 오류 | `src.xy`와 `pyproj`, 타일 재추출 테스트 |
| 출력 비율이 어색함 | 타깃과 타일 비율 다름 | 콘텐츠 비율을 먼저 확인하고 동일하게 crop |
| 데이터셋만 커짐 | 중복 후보/효과 없는 지역 추가 | 매 수집 마일스톤마다 TopK 점수 개선 측정 |
| 재현되지 않음 | 샘플링 랜덤·파라미터·인덱스 불일치 | 모든 설정·seed·소스 ID·코드 버전 기록 |

## 15. 법적·운영적 조건

1. **Sentinel-2:** Copernicus Sentinel 데이터의 공개 사용은 무료·개방이지만, 작품 공개 시 해당 연도와 출처 고지가 필요하다. 가공했다면 `Contains modified Copernicus Sentinel data [Year]` 같은 문구를 사용한다. 정확한 연도는 최종 사용한 자료에서 계산한다. [공식 라이선스](https://cds.climate.copernicus.eu/licences/ec-sentinel).
2. **NAIP:** [NAIP on AWS](https://registry.opendata.aws/naip/)는 Public Domain with Attribution으로 표기한다. 미국 정부 데이터의 출처를 작품 설명에 표시한다. [USDA 데이터 설명](https://catalog.data.gov/dataset/national-agriculture-imagery-program-naip-imagery).
3. **STAC/데이터 제공 서비스:** 원본 데이터가 공개라고 해도 **카탈로그/API의 운영 정책과 속도 제한**은 별개다. 대량 수집 중 과도한 병렬 요청을 피한다. [Earth Search 안내](https://element84.com/earth-search/).
4. **Bad Apple 원본 영상과 음악:** 영상 및 음원의 별도 권리 확인이 필요하다. 원본을 입력으로 사용했다는 사실만으로 공개 업로드 권한이 생기지는 않는다.
5. **추적성:** 재공개 시 영상 설명란 또는 별도 manifest에 실제로 사용한 데이터 소스·연도·가공 여부를 기록한다. 원본 타일의 위성/항공촬영 날짜를 프로젝트 제작 날짜로 바꿔 표기하지 않는다.

## 16. 최종 납품 체크리스트

- [ ] 재현 가능한 실행법(`README.md`)과 Python 의존성/ffmpeg 설치 안내.
- [ ] 실제 STAC/COG 원격 데이터에 접근하는 수집기. 더미 이미지로 대체하지 않음.
- [ ] 지정된 AOI/스케일/수량으로 수집 후 일시 중단/재개 가능.
- [ ] 실제 타일의 위경도·출처·window·촬영 날짜 저장.
- [ ] 특징량 인덱스 및 TopK 검색 가능.
- [ ] 무작위 타일 contact sheet, 타깃별 Top-5 검색 비교 자료.
- [ ] 실제 컬러 사진 1장/프레임 방식의 10초 결과 영상.
- [ ] 저작권 확인 시 오디오 동기화 결과 제공.
- [ ] 1만 대 10만 후보 매칭 품질 비교 보고서.
- [ ] 사용한 이미지들의 출처·라이선스 고지용 manifest.
- [ ] 10만 장 이상 수집이 의미 있는지 평가하고, 무의미하면 확장 중단 근거 제시.

## 17. 참고 링크 (프로젝트 구현의 기준 자료)

- [Element 84 Earth Search — 공식 사이트](https://element84.com/earth-search/)
- [Earth Search API v1](https://earth-search.aws.element84.com/v1)
- [Earth Search 사용 예](https://element84.com/earth-search/examples)
- [Earth Search 공개 저장소 및 컬렉션 안내](https://github.com/Element84/earth-search)
- [Sentinel-2 Collection 1 L2A STAC](https://earth-search.aws.element84.com/v1/collections/sentinel-2-c1-l2a)
- [NAIP STAC Collection](https://earth-search.aws.element84.com/v1/collections/naip)
- [Sentinel-2 COG on AWS](https://registry.opendata.aws/sentinel-2-l2a-cogs/)
- [NAIP on AWS / 라이선스](https://registry.opendata.aws/naip/)
- [USDA NAIP 데이터 개요](https://catalog.data.gov/dataset/national-agriculture-imagery-program-naip-imagery)
- [Copernicus Sentinel 데이터 라이선스](https://cds.climate.copernicus.eu/licences/ec-sentinel)
- [Rasterio Windowed I/O](https://rasterio.readthedocs.io/en/stable/topics/windowed-rw.html)
- [GDAL 원격 파일 읽기/HTTP 관련 문서](https://gdal.org/en/stable/user/virtual_file_systems.html)
- [Microsoft Planetary Computer 데이터 접근(대체 경로)](https://planetarycomputer.microsoft.com/docs/concepts/sas/)

---

**최우선 판단 사항:** 이 프로젝트는 대규모 위성사진 다운로드를 성공시키는 프로젝트가 아니라, **실제로 존재하는 지구의 장면만으로 Bad Apple의 형태가 얼마나 나타나는지 확인하고 보여주는 프로젝트**다. 1만 장, 10만 장 시점의 검색 및 재생 결과가 핵심 의사결정 자료다. 그 결과에 따라 다음 단계를 정한다.

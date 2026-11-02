# P5 — NAIP·LINZ 항공사진 접근 검증

관련 요구: F2–F5, F7, F11–F13, N2–N4. 사용자의 추가 데이터 탐색·확인 요청에 따라 무료 접근 경로의 카탈로그와 실제 픽셀을 제한적으로 확인했다. 수집기 구현이나 대량 수집, 매칭·영상 품질 검증은 이번 작업 범위가 아니다.

## 실행과 증거

Windows 로컬의 기존 uv 환경·requests·Rasterio/GDAL·Pillow를 사용했다. 사용자 API 키·AWS 자격증명·Requester Pays·유료 서비스·클라우드 실행을 사용하지 않았다. NAIP는 공개 API가 익명 발급한 임시 SAS를 메모리에서만 붙였으며 저장한 자산 URL은 모두 서명 없는 원본이다. 새 패키지를 설치하거나 원본 MP4·기존 100만 인덱스를 변경하지 않았다.

- 실행: `uv run python .local/probe_aerial_access.py`. 이 스크립트는 로컬 조사용이며 Git에 포함하지 않는다.
- 증거: `data/access-check/aerial-2026-11-02/`의 `manifest.json`, 소스별 collection/search/item JSON, 18개 PNG, `contact-sheet.jpg`. 생성 데이터는 Git에서 제외한다.
- 읽기: 각 소스의 실제 타일 3개에서 폭 256/1024/2048px의 연속 창을 RGB 원해상도로 읽고, 기존 BOX 리사이즈로 256×191 컬러 미리보기를 만들었다. 출처·원본 창·CRS·위치/footprint·촬영 시점/구간·RGB 해시·mask·변환을 기록했다.
- 결과: 6개 타일·18개 창 모두 읽기 성공, dataset mask 기준 유효율 100%. 구름 품질은 미평가이며 이 유효율을 무구름 보증으로 해석하지 않는다. contact sheet에서 도시·숲/초지·수면과 스케일 변화가 실제 사진으로 표시되는 것을 확인했다. 농경지 탐색 AOI의 표본은 도시/농경지 경계이며 타일 중심 선택에는 바다 표본도 포함된다. 지역·컬렉션 명칭을 검증된 토지피복 라벨로 사용하지 않는다.
- 두 번의 실행에서 누적 관측 경과 약 36초, 최종 RSS 약 99MiB, 메타데이터·미리보기 출력 약 2.9MiB. RSS는 최종 스냅샷으로 peak가 아니다. 네트워크 수신량은 미계측이며 출력 크기나 실행시간을 대량 수집 비용으로 환산하지 않는다.

## NAIP — Microsoft Planetary Computer

STAC `https://planetarycomputer.microsoft.com/api/stac/v1`의 collection `naip`를 AOI bbox로 검색하고 asset `image`를 읽었다. 컬렉션의 현재 temporal extent는 2010–2023년이며 Sentinel-2의 2023년 이후 조건을 그대로 적용하면 과거 자료를 놓친다. SDK와 같은 `/api/sas/v1/token/{account}/{container}` 요청은 사용자 API 키 없이 성공했다. 실제 저장소는 `naipeuwest.blob.core.windows.net/naip`이다.

| 탐색 지역 | 실제 item | 촬영일 | GSD |
| --- | --- | --- | --- |
| Utah 도시 | `ut_m_4011125_sw_12_060_20211105` | 2021-11-05 | 0.6m |
| Idaho 농경지 주변 | `id_m_4311620_sw_11_060_20230711_20231127` | 2023-07-11 | 0.6m |
| California 해안 | `ca_m_3612110_se_10_060_20220519` | 2022-05-19 | 0.6m |

모두 uint8 4밴드·512px 내부 블록·Deflate 압축 COG였다. TIFF color interpretation은 red/green/blue/undefined이며 STAC 정의의 RGB+NIR와 함께 해석한다. RGB는 앞 세 밴드만 사용했다. 실제 창의 지상 폭은 153.6/614.4/1228.8m다. mask는 `all_valid`이므로 공급자가 별도 결측 마스크를 제공한다는 가정은 하지 않는다.

AWS NAIP 버킷은 여전히 Requester Pays 경로로 취급한다. Planetary Computer에서 소량 읽기에 성공한 사실이 서비스의 대량 요청 한도·지속 처리량을 보증하지 않는다. 익명 요청의 throttling이나 장기 가용성은 이번에 시험하지 않았다.

## LINZ — 공개 뉴질랜드 항공사진

정적 STAC `https://nz-imagery.s3.ap-southeast-2.amazonaws.com/catalog.json`은 894개 child collection을 반환했다. 이는 중복 지역·여러 시기·다른 영상 종류를 포함한 촬영 사업 수이며 894개 독립 지역이나 전부 같은 조건의 RGB 항공사진이라는 뜻이 아니다. 실제 검증한 collection은 다음과 같다.

| 컬렉션 | item 수 | 읽은 item | GSD | item 촬영 구간 |
| --- | ---: | --- | --- | --- |
| Auckland Central Urban Aerial Photos (2023) | 127 | `BA32_2000_2303` | 0.06m | 2023-08-21–08-23 |
| Canterbury Rural Aerial Photos (2014–2015) | 1,661 | `BU23_5000_0605` | 0.3m | 2015-01-08–01-23 |
| West Coast Rural Aerial Photos (2015–2016) | 1,346 | `BT19_5000_1003` | 0.3m | 2015-12-25–2016-05-31 |

위 item 수는 collection 메타데이터에서 센 링크 수이며 모든 타일을 읽은 결과가 아니다. 컬렉션별 중간 item 하나를 고른 접근 표본으로, 최신 시기·지형 다양성을 최적화한 수집 계획은 아니다. 세 컬렉션 모두 CC-BY-4.0이며 licensor는 각각 Auckland Council, Canterbury Aerial Imagery Consortium, West Coast Regional Council이다.

실제 파일은 EPSG:2193·RGB+alpha uint8·512px 내부 블록·WebP 압축의 `.tiff` COG였다. 기존 Rasterio 빌드에서 RGB와 alpha 기반 dataset mask를 읽었다. Auckland의 창 폭은 15.36/61.44/122.88m, 나머지는 76.8/307.2/614.4m다. 공급자 정사영상의 연속된 지리 창이며 프로젝트가 여러 장소를 조합한 포토모자이크가 아니다. 원본 정사영상의 여러 촬영분·시점은 메타데이터의 구간으로 추적한다.

첫 조사 스크립트가 선택 필드 `roles=data`를 필수로 가정해 LINZ 3개 item에서 픽셀 읽기 전에 `StopIteration`으로 끝났다. 저장한 카탈로그·item을 재사용해 실제 `visual` 자산의 TIFF 타입으로 해석하고 `.tiff` 허용을 추가해 해결했다. 성공한 NAIP 픽셀은 다시 받지 않았으며 실패 경위와 해결 여부는 manifest에 남겼다.

## 수집기 연결과 확대 판단

NAIP와 LINZ는 모두 실제 데이터 보강 대상으로 쓸 수 있다. 넓은 미국 지역의 0.6m NAIP와 뉴질랜드의 0.06–0.3m LINZ는 기존 10m Sentinel-2와 다른 지상 범위를 공급한다. 고해상도가 곧 형태 매칭 개선을 의미하지는 않으며, 이번 18개 창은 접근 증거다.

현재 수집기는 Sentinel-2의 collection·버킷·3밴드·SCL과 MGRS ID에 결합되어 있어 URL만 바꾸어 바로 수집할 수 없다. 연결에 필요한 작업은 다음과 같다.

1. 소스별 자산 해석과 임시 SAS 갱신, RGB 밴드 선택·dataset mask·촬영 단일 시점/구간·출처 보존.
2. SCL이 없는 항공사진의 별도 품질 정책. 미평가 구름 품질을 0으로 기록하지 않는다.
3. 픽셀 폭과 함께 지상 폭을 기준으로 수집 스케일을 정한다. 기존 256–2048px를 그대로 적용하면 6cm 자료는 15–123m만 탐색하게 된다.
4. 기존 RGB 캐시·회전 특징·참조 bundle을 재사용하고 타일 ID에는 소스·collection·item·원본 창·변환을 포함한다. 서로 다른 collection의 같은 지역·촬영분은 중복 관리 대상으로 본다.
5. 10만–30만을 중간 계측 지점으로 삼아 지역·스케일·데이터량·같은 타깃의 실제 검색/재생을 기록한다. 작은 표본의 낮은 시각 품질만으로 확대를 중단하지 않는다. 대량 실행은 이번에 하지 않았다.

Sentinel-2도 새 지역과 촬영 시기를 넓힐 여지가 있다. 현재 `collection.pipeline.catalog`는 MGRS 타일당 한 촬영분만 선택하므로 단순히 날짜 범위나 `scenes_per_aoi`만 늘려서는 같은 장소의 여러 계절을 확보하지 못한다. 계절 확대는 명시적인 촬영분 선택 규칙을 추가하는 별도 작업이다.

## 확인한 공식 자료

- [Planetary Computer NAIP collection](https://planetarycomputer.microsoft.com/api/stac/v1/collections/naip)
- [Microsoft NAIP 예제](https://github.com/microsoft/PlanetaryComputerExamples/blob/main/datasets/naip/naip-example.ipynb)
- [Microsoft SAS 서명 구현](https://github.com/microsoft/planetary-computer-sdk-for-python/blob/main/planetary_computer/sas.py)
- [AWS NAIP 접근 조건](https://registry.opendata.aws/naip/)
- [LINZ 공식 저장소](https://github.com/linz/imagery), [공개 데이터 접근·이용 안내](https://registry.opendata.aws/nz-imagery/)

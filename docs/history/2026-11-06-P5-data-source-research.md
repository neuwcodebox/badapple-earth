# P5 — 추가 항공사진 데이터원 조사

관련 요구: F2–F5, F7, F11–F13, N1–N4. 사용자의 추가 데이터 탐색 요청에 따라 기존 수집 범위와 새 공개 항공사진을 조사했다. **소스 접근 조사이며 신규 대량 수집·검색·영상 품질 개선은 미실행/미검증이다.**

## 판단과 우선순위

현재 고유 120만 후보는 Sentinel-2 100만·NAIP 10만·LINZ 10만이다. 닮은 사진이 없으면 검색 개선만으로 해결할 수 없으므로 신규 지역과 지상 범위를 함께 늘린다. 해상도나 국가 수가 곧 형태 대응 개선을 보장하지는 않는다.

1. **기존 NAIP 새 AOI·LINZ 미수집 도시 사업:** 현재 adapter와 수집·재개 경로를 재사용할 수 있어 구현 비용이 가장 작다. 인접 창만 늘리기보다 새 지역·사업을 우선한다.
2. **네덜란드 Beeldmateriaal:** 전국 25cm RGB로 수로·간척지·항만·도시의 다른 패턴을 탐색한다. 다운로드 색인 adapter와 촬영 시점 해석이 필요하다.
3. **스위스 SWISSIMAGE:** STAC·RGB COG가 기존 구조에 잘 맞는다. 도시·산림/초지 경계·산악의 보강 후보이며 촬영 연도 정확도와 별도 출처 조건을 처리해야 한다.
4. **스페인 PNOA·프랑스 BD ORTHO:** 넓은 후속 후보로 유지한다. 공식 문서까지 확인했으며 실제 픽셀·수집 연결은 미검증이다.

## 기존 데이터원의 남은 범위

- **NAIP:** 현재 설정은 미국 36개 AOI, AOI당 최대 100개 item 조사·6개 장면 선택, 2015–2023년이다. 미국 전체 소진이 아니다. Planetary Computer 익명 SAS는 기존 수집에서 검증했으므로 확인용 재다운로드는 하지 않았다. AWS Requester Pays 경로는 사용하지 않는다.
- **LINZ:** 현재 수집은 23개 30cm 촬영 사업이다. 11월 6일 공개 root는 child collection 895개를 반환했다. 제목이 `Urban Aerial Photos`인 것은 174개, 그중 제목에 2022–2026년을 포함한 것은 79개였다. 중복 지역·연도·제품을 포함한 사업 수이며 고유 지역이나 신규 사진 수가 아니다.
- 추가 확인한 **Auckland 0.075m Urban Aerial Photos (2024–2025)**는 CC-BY-4.0, item 링크 17,739개, collection 촬영 구간 2024-01-31–2025-03-05다. item은 지리 타일이며 독립 촬영 횟수가 아니다. 이번에는 collection 메타데이터만 읽었다. [고정 collection](https://nz-imagery.s3.ap-southeast-2.amazonaws.com/auckland/auckland_2024_0.075m/rgb/2193/collection.json).
- 7.5cm 자료에 기존 256–2048px를 그대로 적용하면 폭 19.2–153.6m에 치우친다. 목표 지상 폭과 원본 블록 비용을 함께 계획한다. 고해상도라는 이유만으로 기존 규모 설정을 복사하지 않는다.
- **Sentinel-2:** 신규 지역·계절을 보강할 수 있다. 현재 MGRS 타일당 한 촬영분, NAIP는 quarter-quad당 한 촬영분을 고르므로 날짜 범위 변경만으로 계절 다양성이 늘지는 않는다. 계절 확대는 선택 정책 변경이 필요하다.

## 신규 데이터원

| 데이터원 | 확인한 범위 | 접근·이용 조건 | 남은 작업 |
| --- | --- | --- | --- |
| 네덜란드 Beeldmateriaal 2025 RGB 25cm | 공식 색인의 3,534개 타일 항목, 3개 타일·6개 실제 창 | 무인증 HTTPS, CC-BY-4.0. RGB/CIR 구분 | 색인 adapter·촬영 시점 정확도 |
| 스위스 SWISSIMAGE | STAC collection·3개 item, 3개 타일·6개 실제 창 | 무인증 HTTPS COG, swisstopo OGD·출처 표시 | STAC adapter·연도 정확도·혼합 출처 |
| 스페인 PNOA Máxima Actualidad / Anuales | 공식 제품·다운로드·이용 안내 | 확정 정사영상 COG, CC-BY-4.0 호환 조건 | 파일 탐색·Range 읽기·촬영 메타데이터 검증 |
| 프랑스 IGN BD ORTHO | 공식 제품·배포 문서 | Licence Ouverte 2.0, JPEG2000·압축 묶음 | 다운로드·해제·로컬 전처리 비용 검토 |

네덜란드 3,534는 해당 2025년 RGB 색인 항목 수이며 전체 시기·제품 총량이나 중복 제거 후 장소 수가 아니다. 스위스 공식 설명은 1×1km 약 42,700타일, 원자료 평지·주요 계곡 10cm/알프스 25cm, 배포 10cm·2m를 안내한다. 전국 원해상도 약 2.4TB라는 안내를 전체 다운로드 계획으로 채택하지 않는다.

## 실제 접근 검증과 증거

- 기존 Windows 로컬 uv·Python·Requests·Rasterio/GDAL·Pillow를 사용했다. 새 패키지·사용자 키·클라우드 실행·유료 접근은 사용하지 않았다.
- 실행: `uv run python .local/probe_data_expansion.py`. 증거: `data/access-check/expansion-2026-11-06/`의 카탈로그/색인 JSON, `pixel-manifest.json`, RGB 미리보기 12장, `contact-sheet.jpg`. 조사 스크립트·생성 데이터는 Git에서 제외한다.
- **스위스:** Zurich·Alps·Bern 탐색 bbox의 첫 반환 item 하나씩을 골랐다. 최신·다양성 최적화 표본은 아니다. 실제 대표 연도는 2019·2017·2018, EPSG:2056·3밴드 uint8 RGB·0.1m·256px 내부 블록·JPEG95 COG였고 overview가 있었다.
- **네덜란드:** Amsterdam·Polder·Delta 탐색 지점을 포함하는 타일 하나씩의 중앙을 읽었다. 지점 중심으로 자른 창이나 검증된 토지피복 라벨이 아니다. EPSG:28992·3밴드 uint8 RGB·0.25m·256px 내부 블록·JPEG90 TIFF였다. **세 표본에는 overview와 `LAYOUT=COG` 표시가 없었다.** 원격 창 읽기 성공과 COG 검증을 구분한다.
- 파일마다 폭 256/2048px 연속 창을 원래 RGB로 읽고 1444:1080 비율의 높이를 적용했다. 미리보기는 BOX 축소만 적용했다. 지상 폭은 스위스 25.6/204.8m, 네덜란드 64/512m다. 전체 TIFF를 로컬에 저장하지 않았다.
- **6개 타일·12개 창 모두 읽기 성공, 오류 0개, dataset mask 유효율 100%.** 구름 검사·전체 원본 무결성 검사·데이터셋 전체 품질 검증은 아니다. 시트에서 도시·수로/수면·농경지·초지·수목 그림자가 실제 사진으로 표시되는 것을 확인했다. 매칭·재생은 실행하지 않았다.
- 픽셀 검증 실행은 약 29초였다. 수신 바이트·peak RSS는 미계측이며 지속 처리량이나 대량 예상 시간으로 환산하지 않는다. 웹 문서 조사 시간은 별도다. 성공한 창을 확인 목적으로 다시 읽지 않았다.

## 수집 연결 계약

현재 `sources/aerial.py`는 NAIP·LINZ의 호스트·발견 규칙만 지원한다. 새 URL만 설정에 넣어 바로 수집할 수 없다. 이번에는 production 코드와 기존 인덱스를 변경하지 않았다.

1. 네덜란드 색인 geometry는 EPSG:28992다. 경위도로 오인하지 않고 AOI를 연결하며 `file`·크기·SHA-256·제품 연도·geometry를 보존한다. `mtime`은 파일 수정 시점이며 촬영일이 아니다. 정확한 촬영일은 공식 비행/영상 색인과 별도 대조해야 한다.
2. 스위스 STAC의 `datetime=YYYY-01-01`은 정확한 1월 1일 촬영을 의미하지 않는다. 공식 FAQ상 대표 연도는 타일 70% 이상을 차지하는 영상의 촬영 연도이며 seamline별 날짜는 공개되지 않는다. 기존 단일 시점/구간 계약에 연도 정확도를 명시적으로 추가하고 원본 필드를 보존해야 한다.
3. 스위스 collection의 `license=proprietary`만으로 이용 불가로 처리하거나 CC-BY로 치환하지 않는다. 공식 OGD 조건은 수정·배포·상업 이용을 허용하고 출처 표시를 요구한다. 2018년 이후 큰 호수 중심에는 Planet RapidEye가 포함될 수 있어 공식 추가 고지와 혼합 출처를 보존한다.
4. RGB·dataset mask·촬영 정확도·원본 URL·식별자·CRS·창·회전/크롭을 저장한다. SCL 부재·구름 미평가는 기존 항공사진과 같은 의미로 처리하며 다른 타일/장면을 임의 접합하지 않는다.
5. 동일 지역의 다른 해상도 배포본·연도·회전/크롭을 독립 장소 증가로 과장하지 않는다. 지역·지상 폭·촬영분과 중복을 나눠 기록한다.

## 자원·다음 작업·보류

조사 당시 D 드라이브 여유는 약 **159.6GiB**였다. 기존 256×191×3 uint8 RGB만 신규 10만당 약 13.7GiB, 30만 약 41GiB, 50만 약 68GiB다. 특징·메타데이터·블록 캐시·회전/크롭 인덱스·검색·출력은 별도이므로 전체 원본 전국 다운로드보다 필요한 블록만 읽는 기존 방향을 유지한다.

새 AOI/사업별 quota와 지상 폭을 정하고 **30만–50만 추가를 자원 계측 중간 지점**으로 계획할 수 있다. 최종 상한은 아니다. 기존 NAIP·LINZ 미수집 범위, 네덜란드·스위스 adapter 순으로 진행하는 것이 합리적이다. 대량 수집은 이번에 시작하지 않았다. 취소된 이전 60타깃 규모 비교·기존 전곡 재검색도 자동 재개하지 않는다. 새 수집 후 당시의 동일 타깃·선택 조건에서 실제 형태 대응·재생 개선을 평가한다.

스웨덴 무료 STAC/COG는 공식 안내에 계정 권한 신청·이용 목적 검토·특별 조건 동의가 있어 즉시 익명 수집 대상으로 분류하지 않는다. 핀란드 무료 정사영상은 JP2 다운로드·API 키 경로 확인이 더 필요하며 이번 픽셀 미검증이다. 노르웨이 항공사진은 무료 지도 데이터와 이용 조건이 다르므로 공개 뷰어만으로 대량 수집 가능하다고 판단하지 않는다. OpenAerialMap은 개별 촬영 조건·품질·지역 선별이 필요해 전국 단위 후보보다 후순위다.

## 공식 근거

- 네덜란드 [데이터룸·라이선스](https://www.beeldmateriaal.nl/dataroom), [다운로드 포털](https://basisdata.nl/hwh-portal/download/index.html), [2025 RGB 색인](https://basisdata.nl/hwh-portal/20230609_tmp/links/nationaal/Nederland/BM_LRL2025O_RGB.json).
- 스위스 [제품·촬영 연도 FAQ](https://www.swisstopo.admin.ch/fr/orthophotos-swissimage-10-cm), [OGD 조건](https://www.swisstopo.admin.ch/fr/conditions-utilisation-geodonnees-et-geoservices-gratuit), [STAC collection](https://data.geo.admin.ch/api/stac/v1/collections/ch.swisstopo.swissimage-dop10), [서비스 FAQ](https://www.swisstopo.admin.ch/en/faq-free-geodata).
- 스페인 [PNOA 제품](https://pnoa.ign.es/pnoa-imagen/ortofotos-pnoa-maxima-actualidad), [다운로드 목록](https://centrodedescargas.cnig.es/CentroDescargas/fotos-imagenes-aereas), [IGN 이용 조건](https://www.ign.es/resources/licencia/Condiciones_licenciaUso_IGN.pdf).
- 프랑스 [BD ORTHO·라이선스](https://www.data.gouv.fr/datasets/bd-ortho-r), [배포 형식](https://geoservices.ign.fr/sites/default/files/2022-10/DL_raster_ORTHO_0.pdf).
- 기존 [LINZ 카탈로그](https://nz-imagery.s3.ap-southeast-2.amazonaws.com/catalog.json), [접근 안내](https://registry.opendata.aws/nz-imagery/), [NAIP collection](https://planetarycomputer.microsoft.com/api/stac/v1/collections/naip).
- [스웨덴 접근 조건](https://geotorget.lantmateriet.se/dokument/projects/ortofoto-nedladdning/released/2025.02/), [핀란드 다운로드](https://www.maanmittauslaitos.fi/en/e-services/mapsite)·[API 키](https://www.maanmittauslaitos.fi/en/rajapinnat/api-avaimen-ohje), [노르웨이 이용 조건](https://www.kartverket.no/en/api-and-data/terms-of-use).

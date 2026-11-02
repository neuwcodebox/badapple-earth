# P6 — 전체 프레임 전곡과 16:9 검색 설명 영상

관련 요구: F1, F5–F11, F13, F14, N1–N7. 사용자는 원본의 모든 프레임과 전곡 제작, 원본·세로 Top-K·선택 연결선·큰 결과 사진·메타데이터를 포함한 16:9 완성 영상을 요청했다. 중간 승인 없이 진행하며 공개 업로드는 범위에 없다.

## 시간축·검색·화면 계약

원본 MP4는 영상 6,955프레임·30000/1001fps·232.065167초, AAC 오디오는 약 232.129887초다. PyAV로 모든 프레임을 디코드해 PTS와 time base를 보존한다. 전체 프레임에서 얻은 coarse 특징 중 정확히 같은 배열의 검색만 공유하며 모든 출력 프레임을 유지한다. 이번 입력은 고유 검색 특징 6,413개다.

400만 view의 192차원 특징을 연속 float32 배열로 한 번 packing한다. 최대 내적은 12,484,800으로 2²⁴보다 작아 float32 BLAS의 곱과 비음수 정수 부분합이 정확하다. 내적을 정수로 변환한 뒤 norm과 결합한다. view ID의 전역 사전순 순위를 동률 처리에 사용해 기존 정확 MSE Top-K를 보존한다. 타깃 128개·후보 65,536개 배치, BLAS/Numba 각각 8스레드이며 완료 검색 배치는 체크포인트로 남긴다.

1920×1080 화면은 왼쪽 작은 원본, 서로 다른 다섯 장소의 세로 후보, 강조 연결선, 오른쪽 큰 선택 사진으로 구성한다. 후보는 Top-20 view에서 tile ID가 다른 첫 다섯 곳이며 Top-1을 선택한다. 실제 좌표·촬영일·Sentinel-2·지상 범위를 한 줄로 표시하고 확인하지 않은 지명은 만들지 않는다.

썸네일은 기존 RGB 캐시를 재사용한다. 큰 원본 창이 선택된 경우 표시 디테일에 필요한 native RGB만 최대 네 장면 그룹씩 읽는다. 정상 수신 확인을 위한 RGB/SCL 재다운로드는 하지 않는다. native 사진의 회전·크롭은 같은 canonical recipe의 경계를 원본 픽셀로 매핑하며 점수는 canonical MSE다. 실패 시 같은 선택의 보관된 canonical 이미지를 사용하고 원인을 기록한다.

180프레임 segment 단위로 두 작업이 16:9와 사진 전용 영상을 함께 인코딩한다. encoder당 2스레드, H.264/yuv420p·CRF 18·medium이다. source fps를 유지하고 segment를 stream-copy로 결합한다. 원본 AAC 트랙 전체를 복사해 로컬 결과에 보존하며 약 65ms 오디오 꼬리 때문에 영상 프레임을 늘리지 않는다. 원본 파일은 변경하지 않는다.

## 필요한 검증

오프라인 계약 테스트는 정수 극값·근접 일치·동률의 빠른 MSE/Top-K, 실제 디코드 PTS 전부 보존, 작은 segment의 프레임 수·fps·두 해상도·재개를 확인했다. 기존 검색 테스트와 합쳐 5개가 통과했다. 기존 100만의 10초 결과 60개와 새 전곡 검색의 해당 PTS Top-1이 전부 일치했다. 실제 자료를 다시 검색하거나 내려받는 비교 검증은 하지 않았다.

실제 레이아웃은 보관된 41초 사진으로 한 화면을 만들고 글자·이미지·연결선 배치를 확인했다. 최종 결과에서는 모든 프레임의 메타데이터와 연결선이 함께 바뀐다.

## 실행과 결과

```powershell
$env:OPENBLAS_NUM_THREADS = '8'
$env:OMP_NUM_THREADS = '8'
$env:NUMBA_NUM_THREADS = '8'
uv run badapple-earth produce --config config/production.json --output data/runs/p6-full
```

모든 단계가 완료됐다. `data/runs/p6-full/`의 산출물은 다음과 같다.

| 파일 | 실제 내용 |
| --- | --- |
| `final-16x9.mp4` | 1920×1080 설명 화면, H.264/yuv420p, 200,700,761bytes |
| `satellite-full.mp4` | 원래 1444×1080 비율의 사진 전용, H.264/yuv420p, 215,438,425bytes |
| `preview.png`, `native-detail-preview.png` | 완성 레이아웃과 native 디테일을 확인한 실제 대표 프레임 |
| `selected.parquet` | 모든 source frame의 선택·좌표·촬영일·변환·이미지 경로 |
| `frame-plan.parquet` | 모든 source PTS와 원본·다섯 후보·최종 사진·설명 메타데이터 |
| `search/matches.parquet`, `query_inverse.npy` | 고유 특징별 Top-20와 전체 source frame 복원 매핑 |
| `manifest.json`, `credits.json`, `assets/manifest.json` | 설정·ID·원본 식별·출처·추가 디테일 읽기 기록 |

두 완성 파일을 FFprobe로 한 번씩 확인했다. 영상은 원본과 같은 6,955프레임·30000/1001fps·time base 1/30000·start 0·232.065167초다. AAC도 원본과 같은 9,997프레임·time base 1/44100·start 0·232.129887초다. 복제·보간·프레임 생략 없이 전곡을 출력했다. 41초의 최종 화면과 약 33.93초의 native 해안 사진 화면에서 후보와 큰 사진의 방향·화각·글자·연결선을 확인했다. 전체 재생의 작품성에 대한 사용자 평가는 아직이다.

후보 캐시 5,765개 view·4,719개 장소를 준비했고 전곡은 1,352개 장소·1,536개 view를 선택했다. 큰 창의 선택 669개 view에서 native 디테일을 읽었으며 추가 읽기 실패나 canonical 대체는 0개다. 나머지는 원래 256px 창의 보관된 사진을 사용한다. 선택 각도별 source frame은 0/90/180/270도 순서로 1,372/1,969/1,387/2,227개다. 원본 창 256/512/1024/2048px별 source frame은 4,053/1,990/678/234개다.

51개 전수 검색 배치의 로그 계산시간 합계는 **47.24초**다. 초기 targets 추출·packing·메타데이터 해석·이미지 준비·렌더 시간은 포함하지 않으며 재검색 벤치마크는 하지 않았다. 이미 완료한 targets/검색을 재사용한 마지막 `produce` 실행의 이미지 준비·두 영상 인코딩·결합은 약 **479.58초**였다. 평균 전곡 Best MSE는 0.05315976이며 목표 집합이 달라 이전 10초 평균과 직접 비교하지 않는다. GPU·유료 실행·외부 업로드는 사용하지 않았다.

- source index ID: `0af3913546907b7000fc31ce96d5a659e23f9492856ba7eeae11ea9715e12a6f`
- target set ID: `6ed26d6e41b9fb13c45f6c2222ed659d93e574a4d95b856fd3b8d0e9946d969f`
- search run ID: `ac05e71aa81d830602c3ccab381001ab42f8f2638a93ec3a6d6c6c5a35c4a755`
- production ID: `6a740179036ab57d1a43f8d1528e3419be34380e6d5251a3b78a7cd4d66b8f7c`
- render ID: `53b24a66be29e4e09cb1f0863510a3b97c84a9ed328ea58e8124164ce2b4bca1`

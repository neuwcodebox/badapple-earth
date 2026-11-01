# 형태 우선 검색 v2.1

이 문서는 [TECH](TECH.md)의 형태 검색 세부 계약이다. 사용자 제공 [v2 원문](history/Bad_Apple_Shape_Matching_SPEC_v2.original.md)의 전체 파이프라인을 구현하며, 아래 규칙으로 모호한 부분을 확정한다. 실제 실행 상태와 성과는 [PLAN](PLAN.md)에 둔다.

## 입력과 특징

- 기존 수집 snapshot의 모든 유효 장소와 0/90/180/270도 view를 사용한다. 장소·회전·중앙 크롭 recipe는 기존 `canonical-quarter-turn-center-crop-v1`과 같다. 0도 view ID도 보존한다.
- 현재 입력은 256×191이다. 콘텐츠 비율을 보존하고 인코딩용 마지막 1px padding을 특징에 넣지 않는다. 90/270도는 기존 canonical RGB의 중앙 크롭을 확대하므로 새로운 세부 정보가 생기지 않는다.
- grayscale은 회전·크롭된 실제 RGB에 Pillow Rec.601 `L` 변환을 적용한 uint8이다. 분석 때 float32 `[0,1]`로 바꾼다. float 영상의 BOX 축소, Gaussian reflect, Sobel/8의 x/y gradient를 사용한다. 정확한 격자 EDT는 정수 제곱거리와 float64 포물선 교점으로 계산하고 기존과 같은 float32 제곱근·반경 정규화를 적용한다. Numba의 fastmath는 사용하지 않는다.
- 구조 영상은 폭 128, 원래 비율의 높이이며 sigma 1.5/3.0이다. 정밀 비교는 원래 canonical 크기에 sigma 3.0/6.0이다. Rank 격자는 32×24다.
- PHOG는 두 스케일에서 1/2/4/8 격자·unsigned 8방향을 사용한다. 크기 가중 투표를 인접 두 방향에 선형 배분한다. 각 스케일·레벨 전체 histogram을 L1 정규화→제곱근 변환→sqrt(레벨 가중치×0.5)로 가중하고, 연결 후 L2 정규화한다. 레벨 가중치는 0.10/0.20/0.30/0.40, 총 1,360차원이다. 에너지 0은 0벡터와 `flat_structure`로 남긴다.
- Rank는 sigma 1.5 구조 영상을 32×24로 축소한 값의 평균 순위다. `v=(rank-(N+1)/2)/N`, 질의는 `q=-dark_mask`이며 후보 rank를 L2 정규화하지 않는다. 동률을 임의의 공간 순서로 해소하지 않는다. 점수와 유효한 이진 질의의 AUC를 함께 남긴다.

특징 버전은 `shape-phog-rank-v2.1`이다. 명암 반전·평행이동·추가 정합·색상 변경은 실제 출력에 적용하지 않는다.

## 타깃 분기

타깃 구조는 원본 luma의 p1/p99 범위로 전역 정규화한다. 범위가 1/255 이하이면 min/max로 전환한다. 실제 광도는 정규화하지 않은 원본으로 평가한다.

- 원래 luma 범위가 1/255 이하이거나 정규화된 원래 256급 영상의 검정/흰 클래스 중 하나가 0.1% 미만이면 단색 경로다. `near_uniform`, luma 범위, 실제 dark fraction을 남긴다.
- 비단색 타깃의 0.5 마스크를 기준으로 5×5 이웃에 두 클래스가 함께 있는 경계 띠를 찾는다. 그 밖에서 정규화 luma 0.1–0.9인 픽셀이 전체의 0.5%를 넘으면 실제 중간 명암 타깃으로 분류한다.
- 이진 타깃은 0.5, 중간 명암 타깃은 0.25/0.5/0.75의 중첩 마스크를 사용하고 동일 마스크를 합친다. 각 구조 스케일의 같은 임곗값을 타깃과 후보에 적용하는 것이 아니라, 후보의 분위수 임곗값을 별도로 탐색한다.
- 32 격자에서 한 클래스가 사라지면 해당 Rank 질의만 생략하고 PHOG 경로를 유지한다. 256급 타깃의 단색 판정을 바꾸지 않는다. 구조 평활화가 비단색의 가는 형상을 없애면 해당 해상도의 비평활 마스크를 사용한다.

## 전체 view 검색과 절단

PHOG와 각 유효 Rank 질의가 전체 view를 독립적으로 float32 내적 전수 검색한다. 현재 실행은 NumPy CPU 행렬곱이며 데이터 8,192행·질의 16개 단위로 제한한다. GPU/FAISS나 ANN/PQ는 필수 의존성이 아니다. 라이브러리·백엔드를 run manifest에 남긴다.

각 경로의 전역 Top-2048을 `view_id`로 합친다. 서로 다른 단위의 경로 점수를 합산해 후보를 버리지 않는다. 각 배치의 K번째 점수와 동률인 후보를 모두 고려한 뒤 `(-score, view_id)`로 절단하고 전역 결과와 같은 순서로 병합한다. 반환된 K개만 나중에 정렬하는 방식으로 동률을 보장하지 않는다.

합집합 전체에 128급 두 스케일·최대 다섯 임곗값의 영역/SDF 비교를 수행한다. `D_coarse=(0.40 D_sdf+0.25 D_region)/0.65`의 상위 256개와 각 검색 경로 상위 64개의 합집합을 정밀 평가한다. MSE로 후보를 제한하거나 최종 순위를 정하지 않는다. 실제 후보 수와 `retrieval_truncated`, `rerank_truncated`를 남기며 최종 점수의 전역 최적이라고 주장하지 않는다.

## 영역·거리장·경계 비교

타깃 검정 면적 a에 대해 `delta=min(0.05,a/2,(1-a)/2)`로 두고, 후보 구조 영상의 `a+[-2,-1,0,1,2]*delta` 분위수를 `[1/N,1-1/N]`에 제한한다. 선형 분위수 보간 후 중복 임곗값을 합치며 마스크는 `candidate<=threshold`다. 면적 동률을 깨려고 임의 픽셀을 선택하지 않는다.

- `D_region=1-0.5*(IoU(black)+IoU(white))`. 두 집합이 빈 클래스는 IoU=1, 한쪽만 비면 0이다.
- `phi=clip(EDT(B)-EDT(~B),-r,r)/r`, `r=0.05*min(height,width)`. 빈 마스크는 -1, 전체 마스크는 +1이다. `D_sdf=mean(abs(phi_T-phi_C))/2`다.
- 경계는 화면 안의 4방향 이웃이 다른 마스크 쌍의 양쪽 픽셀이다. 화면 밖 padding은 경계로 추가하지 않는다. 마스크 Gaussian sigma 0.75·reflect·Sobel gradient의 unsigned 각도를 사용한다. 정확히 0 gradient의 각도는 `atan2(0,0)=0`으로 고정한다.
- Chamfer 쌍 비용은 `0.8*min(distance/r,1)+0.2*unsigned_angle_difference/(pi/2)`다. 두 방향의 각 경계점별 joint minimum 평균을 절반씩 합친다. 둘 다 빈 경계는 0, 한쪽만 비면 1이다.
- joint minimum은 경계 방향 격자에서 r 안의 정수 offset을 거리순으로 조회하고, r 밖에서는 공간 비용이 0.8로 같다는 성질을 사용해 전체 경계의 가장 가까운 방향을 비교한다. 현재 최솟값보다 offset의 공간 비용 자체가 크면 이후 offset을 생략한다. 양방향 최솟값 배열을 기존 순서로 평균한다. 방향 bin 근사나 최근접 공간 점의 방향만 비교하는 근사가 아니다.

정밀 점수는 `D_shape=0.40 D_sdf+0.35 D_chamfer+0.25 D_region`이다. 같은 후보 임곗값의 세 성분을 함께 계산하고 합성 점수로 마스크를 고른다. 여러 타깃 레벨에서는 후보 임곗값 비감소 제약을 동적 계획법으로 만족시키며 레벨·두 스케일에 각각 같은 가중치를 둔다. 임곗값과 성분·스케일별 점수를 보관한다. 후보 집합별 min/max 정규화는 사용하지 않는다.

임곗값 경로의 초기 점수는 Chamfer=0인 낙관적 하한이다. 현재 하한이 가장 낮은 순서 제약 경로의 미계산 Chamfer를 완전히 계산하고, 최선 경로 전체가 완전 계산될 때까지 반복한다. 모든 후보의 정확한 최소 형태 점수를 구하므로 최종 +0.01 집합과 저시인성 fallback을 바꾸지 않는다. 생략 가능한 임곗값이 없으면 전체 계산으로 돌아간다. 후보 수를 임의로 줄이지 않으며 coarse Top-256과 경로별 보호 Top-64는 유지한다.

## 실제 사진에서의 최종 선택

원래 사진 luma에서 각 유효 타깃 레벨의 검정/흰 위치 평균을 계산한다. `contrast_delta`는 레벨별 `white_mean-black_mean`의 최솟값이며 모두 0.03 이상이어야 한다. `D_tone`은 레벨별 흑/백 평균 절대 오차의 절반을 레벨 간 평균한 값이다.

시인성 통과 후보의 최소 D_shape에 0.01 이내인 집합에서 `(D_tone,D_shape,view_id)` 순서로 선택한다. 통과 후보가 없으면 `(D_shape,view_id)` 최선을 `low_visibility=true`로 보존한다. 순위만으로 사진을 대체하지 않는다. 원색 본편은 그대로 출력하며 비교 영상·보고서에 저시인성을 표시한다. 반복 장소는 허용한다.

단색 경로는 전체 view를 스캔해 구조 에너지 평균 0.005 이하·실제 luma 분산 0.0025 이하인 후보 중 원래 타깃 평균 밝기에 가장 가까운 것을 고른다. 이 후보가 없으면 `(energy,variance,tone,view_id)` 순서로 선택하고 `uniform_threshold_unmet`, `low_visibility`를 남긴다. NoData 검정 사진은 후보가 아니다.

## 저장·병렬 처리·재개

`shape-index`는 기존 수집 결과와 별개인 snapshot 인덱스를 만든다. 원본 PNG·이미 받은 블록을 우선 쓰고 부족한 원본은 씬·작업 블록별로 한 번 읽어 여러 창을 추출한다. 원격 읽기와 shard 공개는 단일 프로세스, PHOG/Rank 생성과 coarse/fine 계산은 Windows spawn 방식의 지속적인 8개 작업 프로세스다. BLAS·작업 프로세스의 Arrow 스레드는 1로 제한해 중첩 스레드 과다 생성을 피한다. 전수 내적 검색은 별도의 제한된 질의·데이터 배치다.

정밀 계산의 후보는 같은 RGB shard·행이 가까운 순서로 묶으며, 원색 접근은 고정된 base-major 네 방향 행 배치를 사용해 매번 Parquet를 해석하지 않는다. 전체 Gaussian/Sobel을 만든 뒤 경계 위치에서만 atan2를 계산한다. SDF는 [정확한 제곱거리 변환](https://cs.brown.edu/people/pfelzens/papers/dt-final.pdf)의 두 클래스 열 패스를 합치고, 행의 포물선 하단 포락선을 계산한다. 포화된 거리는 제곱근을 생략한다. 한 경계 EDT로 대체하거나 근사 stencil을 쓰지 않는다.

동일한 실제 마스크의 SDF·윤곽을 worker마다 배열 payload 128MiB의 LRU에 보관한다. 키는 격자 shape·전체 packed mask bytes·geometry 규칙 버전이며 해시값만 비교하지 않는다. 동일 배열에서 결정되는 거리장만 공유하므로 사진 ID가 달라도 실제 마스크가 같으면 재사용할 수 있다. 회전·크롭이 달라 마스크가 다르면 공유하지 않는다. 캐시가 적중하지 않는 자료나 하한이 약한 자료의 가속은 보장하지 않으며 `geometry_work`로 실제 적중·계산 횟수를 남긴다.

회전 전 canonical RGB를 무손실 uint8 `.npy`로 공유한다. 네 회전의 grayscale은 저장한 RGB+기존 recipe에서 픽셀 동일하게 재생성되며 각 view에 `gray_cache_ref`, `gray_checksum`을 둔다. RGB에서 회전·크롭·resize 후 L로 변환하므로 반대 순서의 반올림 차이를 만들지 않는다. 선택 사진도 이 캐시에서 출력해 원본을 다시 읽지 않는다.

현재 10만 장소는 공유 RGB 약 14.67GB, PHOG+Rank 약 3.40GB, 합계 약 18.07GB의 원시 배열을 쓴다. 메타데이터·기존 캐시·작업 파일은 별도다. 100만은 약 180.7GB이며 압축률을 가정하지 않는다. memmap과 제한된 LRU를 쓰고 임곗값별 SDF를 영구 저장하지 않는다. 여유 디스크 하한은 12GiB다.

원자적 shard 공개·checksum·SQLite 완료 상태를 분리해 저장한다. 인덱스 완료 shard와 검색 완료 타깃·coarse/fine 체크포인트를 재사용한다. 검색 run은 OS writer 잠금을 사용한다. 중단된 미공개 작업만 다시 수행하며 같은 설정인지 확인한다. 합성 자료는 오프라인 테스트에서만 쓴다.

`shape-match --stop-after-targets N`은 새로 완료한 N개 타깃에서 안전하게 끝난다. 완료 체크포인트의 계산 backend는 결과에 남긴다. 최적화는 같은 수식의 동치가 확인된 경우 기존 점수를 유지하며, backend별 수치 차이가 생기면 성과로 숨기지 않는다.

## 질의 회전에 대한 경계

원본 프레임을 회전해 검색하고 선택 사진을 역회전하는 구조는 가능하다. 저장 특징과 후보 전처리를 공유할 수 있지만 네 방향의 비교량 자체가 사라지지는 않는다. 현재는 원색 RGB를 이미 장소당 한 번만 저장한다.

현재 90/270도는 회전 후 중앙 크롭으로 가로형 콘텐츠 비율의 화각을 맞춘다. 정수 크롭의 반올림 때문에 두 방향의 원본 창에 1px 차이도 생긴다. 또한 현재 BOX 축소·방향 특징은 이미 저장한 배열을 단순히 뒤집어 대체했을 때 동일하지 않았다. 질의 회전 전환은 세로 질의의 후보 화각·분석 격자·리사이즈 규칙·출력 역회전을 함께 정의한 별도 표현 버전으로 다룬다. 현재 실행의 10만 snapshot·네 view recipe와 완료 점수는 보존한다.

## 명령과 산출물

```powershell
$env:OPENBLAS_NUM_THREADS = '1'
$env:OMP_NUM_THREADS = '1'
uv run badapple-earth shape-index --index data/index/p4-100k --output data/shape-v2/p4-100k --workers 8
uv run badapple-earth shape-match --targets data/targets/38s-10s-6fps --index data/shape-v2/p4-100k --output data/runs/p5-shape-v2 --config config/shape-v2.json
uv run badapple-earth shape-render --targets data/targets/38s-10s-6fps --index data/shape-v2/p4-100k --run data/runs/p5-shape-v2 --baseline data/runs/p4-100k
```

run에는 경로별 `candidates.parquet`, 타깃별 coarse/fine·최종 점수, `matches.parquet`, 출처와 recipe가 포함된 `selected.parquet`, 원색/명암/원본 비교 MP4, Top-5·마스크·SDF 비교 시트, 전체 프레임의 `report.html`을 둔다. `method-comparison.mp4`의 왼쪽부터 원본·10만 MSE·10만 형태 검색이다. 기존 MSE 점수와 새 D_shape는 다른 척도이며 숫자끼리 개선율을 계산하지 않는다. 실제 영상 인식성 평가는 생성 완료와 구분한다.

같은 후보 풀·타깃의 MSE 기준선이 주어지면 이미 선택된 60장의 실제 출력도 v2.1의 같은 정밀 형태 점수로 평가해 `baseline-shape-evaluation.json`에 남긴다. 이 진단은 검색·선택 후 수행하며 MSE 후보를 새 검색에 주입하지 않는다. 단색 프레임은 형태 평균에서 제외하고, 형태 점수와 실제 대비를 함께 기록한다.

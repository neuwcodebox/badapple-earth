# Bad Apple Earth — 형태 우선 검색 구현 명세 v2

- 작성: 2026-11-01
- 목적: 진행 중인 위성·항공사진 기반 Bad Apple 재구성 프로젝트의 검색·매칭 부분을 대체하는 독립적인 구현 지침.
- 적용 범위: 특징 추출, 인덱스, 후보 검색, 형태 정밀 비교, 최종 선택, 관련 저장·검증 계약.
- 유지 범위: 현재 수집기, 실제 장소/촬영 시점/출처 추적, 회전 view 생성, MP4 입력, 실제 사진 렌더링. 기존 작업을 처음부터 다시 만들지 않는다.
- 결정: 아래 전체 파이프라인을 처음부터 구현한다. MSE만 구현한 축소판, 소규모 비교 후 기능을 하나씩 추가하는 개발 순서는 사용하지 않는다.
- 근거 수준: PHOG·방향성 Chamfer·거리 변환·벡터 검색은 문헌/공식 구현에 근거한다. 이들을 결합한 점수, Rank-layout 응용, 수치 설정은 이 프로젝트를 위한 설계 결정이다. 실제 수집 데이터에서의 우월성이나 지각적 전역 최적성이 실측·증명되었다고 주장하지 않는다.

## 1. 최종 채택 구조

```text
실제 사진의 출력 가능한 view: source window + rotation + crop
    │
    ├─ 다중 스케일 PHOG ─────── 윤곽의 공간 배치 인덱스
    └─ Rank-layout ──────────── 상대 명암 영역 배치 인덱스
                    │
              두 인덱스 병렬 검색
              각각 전체 view 대상 Top-2048
                    │
              view_id 기준 합집합
                    │
              다중 임곗값 영역 비교
              Balanced IoU + 부호 있는 거리장(SDF)
                    │
              고해상도 정밀 형태 비교
              SDF + 양방향 Directional Chamfer + Balanced IoU
                    │
              읽을 수 있는 최소 대비 확인
                    │
              최선 형태와 거의 동급인 후보 집합
                    │
              실제 밝기 적합도로 최종 선택
                    │
              해당 view의 실제 컬러 사진 출력
```

위의 앞단/뒷단은 **한 번의 검색에서 모두 수행하는 연산 단계**다. 개발 단계별 축소판이 아니다. 모든 수집 데이터와 지정한 전체 영상 구간에 이 파이프라인을 적용한다.

현재 MSE는 과거 결과 비교용으로만 남길 수 있다. 새 검색 경로의 후보 제한이나 최종 주점수로 사용하지 않는다. CLIP 같은 의미 검색, 임의의 시간적 연속성, 같은 지역 유지, crossfade도 새 필수 조건으로 넣지 않는다.

## 2. 목적함수와 제약

### 2.1 무엇을 맞출 것인가

우선순위는 다음과 같다.

1. **형태:** 경계의 위치·방향, 덩어리의 크기·두께, 빈 공간, 내부 구멍, 화면 전체 배치.
2. **극성:** 원본에서 어두운 쪽은 사진에서도 상대적으로 어두워야 한다. 명암 반전을 같은 형태의 성공으로 취급하지 않는다.
3. **광도:** 실제 사진의 밝기와 대비. 형태가 비슷한 후보 사이에서 결정한다.

사람·강·숲·도시라는 의미적 분류는 목적함수가 아니다. 같은 모양이 화면 반대쪽에 있으면 다른 후보다. 자유로운 평행이동·확대·비선형 정합으로 형태를 맞추지 않는다. 실제 출력에 적용되는 crop/rotation만 허용하고 recipe에 남긴다.

### 2.2 '최적'의 범위

설계 목표는 **확보한 실제 사진 view 중 지각적으로 적합한 형상을 찾아내는 것**이다. 지구 전체나 가능한 모든 크롭을 탐색했다는 뜻은 아니다.

벡터 전수검색은 해당 벡터 점수의 Top-K를 빠짐없이 찾는다. 그러나 벡터 점수와 최종 정밀 점수는 다르므로, 후보 절단 이후의 결과를 전체 데이터에 대한 최종 점수의 전역 최적해라고 부르지 않는다. ANN을 쓰지 않아도 이 구별은 필요하다.

## 3. 공통 입력과 canonical view

### 3.1 회전 및 종횡비

현재 구현 중인 0°·90°·180°·270°를 모두 지원한다. 하나의 view는 하나의 실제 출력 recipe다.

```text
view_id = hash(source_id, scene_id, source_window, rotation,
               crop_recipe, extraction_version)
```

90° 회전된 4:3 이미지를 억지로 늘려 4:3으로 만들지 않는다. 세로형 원본 창을 읽어 회전하거나, 더 큰 공통 영역에서 회전 후 정확히 크롭하는 등 현재 recipe로 실제 출력 가능한 방법을 쓴다. 특징과 최종 출력은 반드시 같은 화각이어야 한다.

방향이 다른 view를 검색 전에 장소 기준으로 합치지 않는다. 검색 후에도 각 출력 프레임의 최종 방향은 유지한다.

### 3.2 분석 데이터

4:3을 예로 다음 표현을 사용한다. 실제 콘텐츠 비율이 다르면 종횡비를 보존하여 대응 크기를 정한다.

| 표현 | 해상도 | 용도 |
|---|---:|---|
| 보존용 grayscale | 256×192 | 무손실 로컬 보관, 정밀 형태 비교 |
| 구조 영상 | 128×96 | PHOG, 앞단 영역 비교 |
| 영역 인덱스 격자 | 32×24 | Rank-layout 내적 검색 |

해상도 축소는 검색 표현의 설계이지 데이터셋을 작게 시작하라는 의미가 아니다. 모든 후보에 동일하게 적용한다.

그레이스케일 변환은 고정된 RGB→luma 규칙을 사용하고 변환 버전을 기록한다. 기존 색상 처리 규칙을 재사용할 수 있으나 타깃/후보/재추출에 동일해야 한다. 계산은 float32의 [0,1] 범위에서 하고, 보존 캐시는 uint8 무손실로 유지한다. 특징 계산은 이 캐시로부터 결정적으로 재생성한다.

128×96 기준 Gaussian sigma 1.5와 3.0의 두 구조 스케일을 사용한다. 256×192에서는 대응되는 sigma 3.0과 6.0을 사용한다. 미세 질감을 억제하되 두 스케일 중 하나만 임의로 선택하여 형태를 과도하게 없애지 않는다. 경계 처리는 reflect로 고정한다.

NoData가 있는 view는 검정으로 채우지 않는다. 기존 수집기의 허용률과 별개로, 본 벡터 검색 경로는 최종 비교 창이 전부 유효한 view만 사용한다. 결함 view는 삭제하지 않고 원인과 제외 상태를 기록한다. 별도의 마스크-aware 인덱스를 암묵적으로 섞지 않는다.

## 4. 윤곽 인덱스 — 다중 스케일 PHOG

PHOG는 공간 피라미드의 각 칸에서 경계 방향 분포를 모아 형상과 공간 배치를 표현한다.[R1] 이 설계는 원 논문의 파라미터를 그대로 복제하는 대신 아래 규칙으로 고정한다.

| 항목 | 채택값 |
|---|---|
| 구조 영상 | 128×96, 평활화 2스케일 |
| 공간 격자 | 1×1, 2×2, 4×4, 8×8 |
| 방향 | unsigned, 0~180°, 8 bins |
| 투표 | gradient magnitude, 인접 방향 bin으로 선형 분배 |
| 레벨 가중치 | 0.10 / 0.20 / 0.30 / 0.40 |
| 스케일 가중치 | 0.5 / 0.5 |
| 전체 차원 | (1+4+16+64) × 8 × 2 = 1,360 |

각 스케일·레벨의 전체 histogram을 L1 정규화하고 제곱근 변환한 후 sqrt(weight)를 곱한다. 모든 블록을 연결하고 전체 L2 정규화한다. 각 셀을 따로 단위 정규화하여 약한 잡음을 강한 윤곽과 동등하게 만들지 않는다. gradient 계산과 histogram 정규화는 HOG 계열의 기존 구현 원리를 따른다.[R2]

전체 에너지가 수치적으로 0인 경우 0벡터와 `flat_structure=true`를 기록한다. 0벡터를 단위 벡터로 만들지 않는다. 실제 저대비 사진의 가시성은 최종 단계에서 평가한다.

검색은 inner product다. unsigned 방향은 명암 반전에 둔감하므로, 이 경로 단독으로 최종 선택하지 않는다. 극성은 영역 비교가 담당한다.

## 5. 영역 인덱스 — Rank-layout

### 5.1 정의

상대 밝기 순위가 어느 위치에 놓여 있는지를 저장한다. 원본 검정 영역의 위치가 사진에서도 상대적으로 어두운지를 직접 검색한다.

이 명칭은 본 프로젝트의 응용 설계 이름이다. 기존에 위성사진 검색 우월성이 검증된 모델명은 아니다. 평균 순위와 rank-sum/U 관계는 공식 통계 정의를 따른다.[R3][R4]

후보마다 128×96 구조 영상을 sigma 1.5로 평활화한 뒤 32×24로 area downsample한다. 이 768개 값에 평균 순위를 부여한다.

```text
N = 768
r_i = rankdata(candidate_grid, method='average')  # 1..N
v_i = (r_i - (N+1)/2) / N
```

원본 타깃도 같은 격자를 사용한다. 이진 영역 질의는 다음과 같다.

```text
B_i = 1 if 해당 위치가 타깃의 어두운 영역 else 0
q_i = -B_i
score_rank = dot(q, v)
```

한 타깃에서 내적이 클수록 적합하다. `k=sum(B)`, `0<k<N`일 때:

```text
AUC_dark = 0.5 + N*score_rank / [k*(N-k)]
```

이는 타깃 검정 위치의 후보 밝기가 타깃 흰 위치의 후보 밝기보다 낮을 확률에 동률의 절반을 더한 값이다. 같은 격자, 전체 유효 픽셀, 평균 순위와 이진 질의에서 성립한다. p-value를 계산하는 통계 검정이 아니다.

### 5.2 반드시 지킬 사항

- 후보 rank 벡터를 L2 정규화하지 않는다. 평균 순위 동률이 있는 경우 원래 내적 점수의 의미가 바뀐다.
- 동률을 공간 순서대로 풀어 단색 사진에 가짜 무늬를 만들지 않는다.
- 극성 반전 벡터를 자동으로 추가하지 않는다. 실제 출력에 반전을 허용하지 않기 때문이다.
- rank 공간에서의 일치는 실제 대비를 보장하지 않는다. 거의 동일한 회색 두 값도 순서가 맞으면 높은 점수를 받는다. 대비 확인을 생략하지 않는다.
- 32×24에서 사라지는 가는 형상은 PHOG 경로 및 정밀 비교가 보완한다. 이 경로를 유일한 후보 공급원으로 만들지 않는다.

### 5.3 타깃 마스크와 회색 전환부

완전히 흑백에 가까운 프레임은 0.5 임곗값의 이진 마스크를 사용한다. 안티앨리어싱만 있는 경우 경계 픽셀을 별도 soft weight로 보존할 수 있지만 AUC 식에는 이진 마스크만 넣는다.

전역 fade처럼 밝기 범위만 이동한 경우, 고정 0.5로 실루엣을 없애지 않는다. 프레임의 p1/p99를 이용한 전역 구조 정규화 후 0.5로 나누고, 원래 광도는 밝기 평가에 그대로 남긴다. 단, 가는 형상이 1% 미만이라고 percentile 과정에서 사라져서는 안 된다. p1/p99 범위가 붕괴했지만 원래 min/max 범위가 유효하면 min/max 정규화로 전환한다. 거의 완전 단색 여부는 이 대비 증폭 이전의 영상으로 판단한다. 원래 luma 범위가 1/255 이하인 경우는 단색 경로로 보낸다.

실제 중간 명암 영역이 있는 프레임은 구조 정규화 영상에서 0.25/0.5/0.75의 중첩 마스크를 사용한다. 동일 마스크는 합치고, 각 마스크의 Rank-layout 검색 결과를 합친다. 최종 영역 비교도 이 마스크들을 사용하며, 후보 임곗값은 타깃 임곗값 순서에 따라 비감소하도록 선택한다. 단순히 가장 유리한 타깃 마스크 하나만 고르지 않는다. 유효한 각 레벨의 점수를 평균한다.

## 6. 검색 실행 — 전체 데이터에 대한 GPU 분할 전수검색

본 작업은 대규모 인터랙티브 검색 서비스가 아니라, 이미 정해진 영상 질의를 일괄 처리하는 제작 작업이다. 따라서 기본 실행기는 **float32 특징에 대한 분할 exact search**로 정한다. 작은 데이터에서만 쓰는 임시 베이스라인이 아니다.

Faiss 공식 가이드는 적은 수의 질의에서는 인덱스 구축 비용 때문에 Flat 직접 비교가 적합할 수 있고, 메모리에 맞지 않으면 여러 작은 인덱스의 결과를 합칠 수 있다고 설명한다. FlatIP/L2는 해당 벡터 거리의 전수검색이다.[R5][R6]

### 6.1 채택 규칙

- PHOG: 정규화된 벡터에 `IndexFlatIP` 또는 동일한 배치 행렬곱.
- Rank-layout: 비정규화된 rank 벡터에 `IndexFlatIP` 또는 동일한 배치 행렬곱.
- GPU 사용 시 `GpuIndexFlatIP` 등 지원되는 공식 경로를 사용한다.[R7]
- 데이터 shard와 query batch 크기를 가용 RAM/VRAM에 맞춰 제한한다. 데이터 양을 줄이는 것이 아니라 한 번에 올리는 양만 조절한다.
- 각 shard의 Top-2048을 전역 Top-2048과 병합한다. 점수 동률은 `view_id` 순서로 결정한다.
- PHOG Top-2048과 각 유효 영역 질의의 Rank-layout Top-2048을 합친다.
- GPU를 사용할 수 없으면 CPU 동일 수식으로 실행한다. 의미 임베딩이나 MSE로 자동 강등하지 않는다.
- 기본 경로에 PCA·PQ·ANN을 넣지 않는다. 원 벡터 손실이나 ANN 누락을 품질 우선 실행의 필수 비용으로 만들지 않기 위해서다.

연산의 float32 반올림까지 무한정밀도 수학과 같다고 주장하지 않는다. 백엔드·연산 모드·라이브러리 버전을 기록하고, 축소정밀도 설정은 명시적으로만 허용한다.

### 6.2 후보 단계의 역할

서로 단위가 다른 PHOG 내적과 Rank-layout 내적을 합산하여 후보를 버리지 않는다. **합집합**을 사용한다. 형상 점수로는 좋은 후보가 두 벡터의 Top-K 모두에서 빠질 가능성은 남아 있다. `retrieval_truncated=true`, K, 인덱스 크기와 버전을 결과에 기록한다.

Top-K와 정밀 평가 대상 수는 실행 설정으로 노출한다. 데이터가 커져도 이미 구현된 검색·평가 경로의 범위를 늘리면 되고, 별도 기능 개발 단계를 요구하지 않는다. 기본 운영 모드는 위의 후보 검색이며, 전체 정밀 점수를 모든 view에 계산한 것처럼 표시하지 않는다.

## 7. 채워진 형태의 정밀 표현

### 7.1 사진에 임곗값 하나를 고정하지 않는다

실루엣의 검정 면적 비율을 `a`라고 하자. 후보 구조 영상의 밝기 분위수로 다음 5개의 임곗값을 만든다.

```text
delta_q = min(0.05, a/2, (1-a)/2)
quantiles = clip(a + [-2,-1,0,1,2]*delta_q,
                 1/pixel_count, 1-1/pixel_count)
thresholds = unique(quantile(candidate_structure, quantiles))
```

각 임곗값에서 어두운 영역 마스크 `B_C = candidate_structure <= threshold`를 만든다. 동률 때문에 실제 검정 면적이 목표 a와 달라지는 것은 그대로 평가한다. 픽셀을 임의 순서로 골라 목표 면적을 강제로 맞추지 않는다. 후보별 임곗값 탐색 범위는 같은 규칙을 적용한다.

마스크는 **형태 측정용**이다. 실제 컬러 사진에 이진화·대비 변경을 적용하지 않는다.

### 7.2 Balanced IoU

검정 덩어리와 흰 빈 공간을 각각 비교한다.

```text
D_region = 1 - 0.5 * [IoU(B_T, B_C) + IoU(not B_T, not B_C)]
```

같은 경계라도 채워진 방향이 반대면 나빠진다. 작은 검정 영역을 큰 흰 배경이 덮어 점수가 좋아지는 현상을 완화하려고 두 클래스를 동등하게 본다. 두 집합 모두 빈 클래스의 IoU는 1, 한쪽만 비어 있으면 0으로 명시한다.

### 7.3 부호 있는 거리장

픽셀의 실제 밝기가 아니라 **경계에서 얼마나 떨어져 있고 어느 쪽에 있는가**를 비교한다. 덩어리 두께, 빈 공간, 작은 경계 이동을 연속적으로 표현하려는 목적이다. 이를 위상 동일성을 보증하는 검정이라고 부르지는 않는다.

```text
r = 0.05 * min(width, height)
phi(B) = clip(EDT(B) - EDT(not B), -r, r) / r
D_sdf = mean(abs(phi(B_T) - phi(B_C))) / 2
```

EDT는 non-zero 위치에서 가장 가까운 zero 위치까지의 유클리드 거리를 계산한다.[R8] 위 식은 격자에서 정의한 SDF형 표현이며, 연속 경계까지의 정확한 부호 거리와 완전히 동일하다고 부르지 않는다.

완전 빈 마스크는 phi=-1, 전체 마스크는 phi=+1로 명시한다. 프레임 바깥에 검정/흰 padding을 만들어 가짜 경계를 추가하지 않는다.

## 8. 양방향 Directional Chamfer

방향성 Chamfer는 경계점 사이의 거리와 방향 차이를 함께 비교한다.[R9] 본 설계는 이를 양방향으로 적용한다. 스케치-사진 검색에서 경계의 위치·방향을 인덱싱하고 양방향 비교를 사용하는 관련 선행 연구가 있다.[R10]

형태 마스크의 내부 경계를 사용한다. 화면 테두리 자체를 추가 경계로 만들지 않는다. 경계점 방향은 마스크를 약하게 평활화한 gradient의 unsigned 방향으로 계산한다. 후보와 타깃에 동일하게 적용한다.

```text
spatial(p,q) = min(euclidean_distance(p,q) / r, 1)
angular(p,q) = unsigned_circular_difference(theta_p, theta_q) / (pi/2)
cost(p,q) = 0.8*spatial(p,q) + 0.2*angular(p,q)

D(T→C) = mean_{p in E_T} min_{q in E_C} cost(p,q)
D_chamfer = 0.5*D(T→C) + 0.5*D(C→T)
```

필요한 경계가 빠진 경우와 불필요한 경계가 추가된 경우를 둘 다 벌점으로 처리한다. 한 방향만 계산하면 경계가 빽빽한 사진이 우연히 유리해질 수 있다.

방향별 distance transform 등을 사용해 계산을 가속할 수 있다. 이때 위의 capped 공간 비용과 방향 비용을 실제로 구현하는지 확인한다. 단순히 가장 가까운 점을 찾은 뒤 그 점의 방향 벌점을 더하는 연산은 위의 joint minimum과 다를 수 있다. 근사 구현을 사용하면 방향 bin 수·근사 규칙을 명시한다.

두 경계 집합이 모두 비어 있으면 D_chamfer=0, 한쪽만 비어 있으면 1이다. 둘 다 빈 흑/백 반전은 region/SDF가 구분한다.

## 9. 고정된 형태 점수와 계산 계층

모든 거리 항목은 낮을수록 좋고 [0,1]에 들어오도록 위 정의를 따른다. 후보 집합마다 min-max 정규화하지 않는다.

```text
D_shape = 0.40*D_sdf + 0.35*D_chamfer + 0.25*D_region
```

가중치는 검증된 보편 최적값이 아니라 **이 버전에서 채택한 운영값**이다. 문서에 숫자를 넣는 것은 구현자가 매 단계 새로운 방법을 고르게 하지 않고, 한 가지 재현 가능한 목적함수를 제공하기 위해서다. 구현 및 자동 검증은 이 값으로 처음부터 수행한다. 변경 시 버전을 올리고 기존 점수를 보존한다.

### 9.1 계산량을 제한하는 내부 계층

- 합집합의 모든 후보에 128×96에서 2개 구조 스케일 × 5개 임곗값의 region/SDF 비교를 수행한다.
- 이 앞단 점수는 `D_coarse = (0.40*D_sdf + 0.25*D_region)/0.65`로 정의한다. 각 스케일 점수를 평균한다.
- D_coarse 상위 256개와, 각 원래 검색 경로 상위 64개의 합집합을 정밀 평가 대상으로 유지한다. 실제 개수와 절단 여부를 기록한다.
- 정밀 평가에서는 256×192의 두 구조 스케일에서 SDF/양방향 Chamfer/region을 모두 계산한다. 임곗값마다 세 항목을 함께 계산하고, **하나의 마스크에 대한 D_shape**가 가장 작은 것을 선택한다. 항목별로 서로 다른 최적 마스크의 점수를 조합하지 않는다.
- 각 스케일의 최적 점수를 동일 가중치로 평균한다. 실제 중간 명암 타깃이면 중첩 마스크 레벨의 임곗값 순서를 유지하면서 레벨 점수도 평균한다.

스케일별 임곗값은 검색 표현 내부의 해석이다. 원본을 변형하거나 서로 다른 사진을 합성하는 것이 아니다. 선택된 임곗값·스케일별 점수를 결과에 남긴다.

이 계층은 모든 사진에 비싼 거리장/경계 비교를 매번 수행하지 않기 위한 **운영 알고리즘의 일부**다. 축소판을 먼저 개발하라는 뜻이 아니다. 앞단 절단은 최종 최적 후보를 놓칠 수 있으므로 그 한계를 명시하고, 정밀 후보 개수를 실행 설정으로 노출한다.

## 10. 형태 우선 최종 선택

### 10.1 최소 시인성

전처리하지 않은 실제 사진 luma에서 타깃 검정/흰 위치의 평균을 구한다.

```text
contrast_delta = mean(Y_C on target_white) - mean(Y_C on target_dark)
```

비단색 타깃의 운영 하한은 contrast_delta >= 0.03([0,1] luma)로 둔다. 이것은 밝기를 최대화하는 점수가 아니라, 순위만 맞고 실제로는 거의 보이지 않는 후보를 거르는 최소 조건이다. 기준은 경험적으로 확정된 인지 임계값이 아니며 버전 관리 대상이다.

조건을 만족한 후보가 없으면 결과를 조작하지 않는다. 형태 최선 후보를 `low_visibility=true`로 남기고, 영상과 보고서에 이 프레임의 한계를 기록한다. 조용히 생성 이미지나 원본 실루엣으로 대체하지 않는다.

### 10.2 거의 같은 형태끼리만 밝기 비교

시인성 조건을 충족한 후보의 최소 형태 거리 D_best를 구한다.

```text
eligible = candidates with D_shape <= D_best + 0.01

D_tone = 0.5 * (
    abs(mean(Y_C on target_dark) - mean(Y_T on target_dark))
  + abs(mean(Y_C on target_white) - mean(Y_T on target_white))
)
```

eligible 안에서 D_tone이 가장 낮은 후보를 선택한다. 최종 동률은 D_shape, view_id 순서로 결정한다. 후보 사진 밝기는 원본 출력의 luma이고, 검색용 rank·이진화·대비 정규화 값을 사용하지 않는다.

`0.01`은 정규화된 형태 거리의 운영 허용폭이지 '사람 눈에 반드시 동일한 1%'라는 의미가 아니다. 엄격하게 형태 최선 하나만 선택해야 하는 실행에서는 허용폭을 0으로 설정할 수 있다.

동일 장소 반복은 기본적으로 허용한다. 일관성이나 다양성 때문에 더 나쁜 형태를 강제로 선택하지 않는다. 다양성 옵션을 제공하더라도 eligible 내부 동률 해소에만 사용한다.

### 10.3 단색·거의 단색 프레임

원본이 완전 검정/흰색이면 맞출 경계 자체가 없다. 대비 증폭 이전의 luma 범위가 1/255 이하이거나 한 클래스가 분석 면적의 0.1% 미만일 때는 단색 경로를 사용한다. 두 조건 및 실제 값을 결과에 기록한다.

실제 유효 사진 중 구조 에너지/명암 분산이 낮은 후보를 대상으로 원본 평균 밝기에 가까운 것을 선택한다. 후보가 NoData 검정인지 검사한다. 원본이 거의 단색이더라도 소수의 중요한 형태가 남는 경우를 숨기지 않도록 `near_uniform` 진단과 실제 dark fraction을 기록한다. rank의 k=0/k=N 분모를 억지로 epsilon으로 덮지 않는다.

## 11. 저장 구조와 비용

기존 데이터셋 옆에 특징 버전별 경로를 추가한다. 수집 데이터를 다시 만들지 않는다. 현재 캐시에 필요한 해상도/회전 화각이 없을 때만 원본 COG에서 재추출한다.

```text
data/
  shape_v2/
    manifest.json
    views.parquet
    gray256/shards/         # 무손실 grayscale, 고정 크기 shard
    phog/shards/            # float32, 1,360D/view
    rank/shards/            # float32, 768D/view
    progress.sqlite        # 추출·특징·검색 job 상태
    candidates/            # 타깃별 후보/검색 경로/원점수
    scores/                # 마스크·형태·실제 광도 세부 점수
    selected.parquet
```

최소 메타데이터:

```text
view_id, tile_id, source/scene/item/asset IDs,
source_window, rotation, crop_recipe, acquired_at, center_lon_lat,
gray_cache_ref, gray_checksum, full_valid,
feature_version, index_snapshot, feature_shard, feature_row
```

선택 결과:

```text
target_frame_id, target_pts, view_id,
retrieved_by, phog_score, rank_score,
D_sdf, D_chamfer, D_region, D_shape, D_tone,
contrast_delta, chosen_thresholds, scale_scores,
near_uniform, low_visibility, retrieval_truncated, rerank_truncated,
config_hash, source_recipe, selected_rgb_cache_ref
```

float32 원시 벡터와 무압축 캐시의 계산값은 다음과 같다. GB는 10^9 bytes다.

| 데이터 | 1 view | 100만 view | 100만 장소 × 4 view |
|---|---:|---:|---:|
| PHOG 1,360D | 5,440B | 5.440GB | 21.760GB |
| Rank 768D | 3,072B | 3.072GB | 12.288GB |
| grayscale 256×192 uint8 | 49,152B | 49.152GB | 196.608GB |
| 합계 | 57,664B | 57.664GB | 230.656GB |

무손실 압축률은 실측 전 보장하지 않는다. 중간 128/32 영상을 중복 파일로 영구 저장할 필요는 없다. 동일 원본 캐시에서 회전별 view를 정확하게 재생성할 수 있으면 캐시를 공유한다. 다만 회전마다 source window/crop이 다르면 공유를 가정하지 않는다.

모든 grayscale·특징·인덱스를 RAM/VRAM에 동시에 올리지 않는다. memmap 또는 shard와 유계 캐시를 사용한다. 각 view의 모든 임곗값 SDF/방향 distance map을 영구 저장하지 않는다. 정밀 후보에서 배치 계산하고 제한된 캐시에 둔다. 임곗값·마스크가 같은 계산만 재사용한다.

job은 기존 수집 완료 상태와 별개로 관리한다. 임시 shard→checksum 확인→원자적 commit 순서로 기록한다. feature row와 view_id 매핑을 재시작 때 검증하고, 실패 작업만 재실행한다.

## 12. 구현 경계와 통합

추가 또는 교체할 책임은 다음과 같다. 실제 파일명은 현재 코드 구조에 맞추되, 기존 수집/렌더 모듈을 새로 만들지 않는다.

| 책임 | 입력 | 출력 |
|---|---|---|
| canonical analysis cache | 현재 view recipe + 실제 RGB | gray256, valid, checksum |
| shape descriptor | gray256 + feature version | PHOG, rank, 품질 메타데이터 |
| exact vector retrieval | 전체 특징 shard + 전체 타깃 | 경로별 전역 Top-K |
| region screening | 후보 gray + 타깃 마스크 | coarse shape 점수/정밀 대상 |
| geometry reranker | 정밀 대상 + 타깃 | SDF/Chamfer/region/threshold |
| lexicographic selector | 형태·실제 광도 | 최종 view와 사유 |
| report | 후보/선택/좌표 | Top-K 비교, 거리장, 선택 근거 |

Python/NumPy/SciPy, 현재 영상 처리 라이브러리, FAISS 또는 동등한 배치 연산을 사용한다. 별도 학습 데이터나 새로운 생성형 모델은 요구하지 않는다. GPU 도입을 위해 현재 프로젝트 전체를 특정 프레임워크로 옮기지 않는다.

MP4 프레임 추출과 audio sync는 기존 계약을 유지한다. 매 프레임 독립 선택이 기본이다. 실제 출력은 선택한 source window/rotation/crop에서 나온 한 장소의 컬러 사진이다.

## 13. 완료 조건 — 전체 기능을 구현한 상태에서 확인

검증은 축소 버전의 선행 승인 절차가 아니다. 다음은 완성형 구현의 자동 테스트와 납품 확인 항목이다.

- 분할 검색의 Top-K가 동일 float32 특징의 비분할 검색과 허용 수치 오차 안에서 일치한다. 동률의 view_id 선택이 재현된다.
- 동률 포함 rank 식과 직접 검정/흰색 위치 쌍 비교가 일치한다. 단색·명암 반전·저대비 예외가 명시대로 작동한다.
- 같은 경계의 속이 찬 도형과 빈 도형, 위치가 바뀐 도형, 굵기가 다른 도형을 구분한다. 특수한 한 합성 예제에서 점수가 좋다고 실제 검색 성능이 입증되었다고 하지 않는다.
- Chamfer가 joint 공간/방향 최소를 계산하는지, 양방향 및 빈 집합 정의가 맞는지 확인한다.
- SDF가 NoData·외곽 padding 때문에 가짜 경계를 만들지 않는다.
- 90도 view의 특징과 최종 렌더 화각이 같고, 원본 위치를 다시 추출할 수 있다.
- 모든 실제 타깃 프레임에서 선택 결과 또는 명시적 실패/저시인성 상태가 남는다. 편한 프레임만 보고 성공 처리하지 않는다.
- 출력 영상과 함께 타깃/선택 사진/마스크/형태 세부 점수/실제 대비의 비교 자료를 생성한다. 인지 가능한 형태인지 확인하되 MSE 감소를 성공 기준으로 삼지 않는다.
- 중단/재개 시 기존 수집 결과·특징·완료 검색을 잃지 않는다.
- 이번 문서가 추가한 기능 중 PHOG, Rank-layout, 다중 임곗값, SDF, 양방향 Chamfer, 형태 우선 선택을 '추후 구현'으로 남기지 않는다.

## 14. 이전 연구안과의 변경점

| 이전 안내 | 이번 결정 |
|---|---|
| PHOG부터 작게 시작하고 영역 검색 추가 | 두 인덱스를 처음부터 함께 구현 |
| SDF는 나중에 재정렬로 실험 | SDF를 기본 정밀 비교에 포함 |
| MSE/여러 후보 방법의 작은 비교부터 진행 | 새 파이프라인 자체를 구현하고 전체 자료에 적용 |
| 인덱스는 규모가 커진 뒤 결정 | 품질 우선 GPU/CPU 분할 exact vector search 채택 |
| 가중치·세부 정책은 모두 미정 | 버전 관리되는 운영값과 예외 계약 제시 |
| 단순/확장 버전을 반복 개발 | 검색 내부 계층을 갖춘 하나의 완성형 구현 |

기존 수집 규모·진행 중인 작업을 축소하거나 다시 시작하라는 지시가 아니다. 기존 문서의 이미지 소스·좌표·영상·출처 사양은 그대로 유지하고, 현재 TECH의 검색/특징/선택 절과 PLAN의 해당 구현 작업만 이 명세에 맞춰 갱신한다.

## 참고 자료

1. [R1] Bosch, Zisserman, Muñoz. *Representing Shape with a Spatial Pyramid Kernel*, CIVR 2007. PHOG 및 공간 배치 표현. https://www.robots.ox.ac.uk/~vgg/publications/2007/Bosch07/
2. [R2] scikit-image. *Histogram of Oriented Gradients*. gradient, 공간 histogram, 정규화. https://scikit-image.org/docs/stable/auto_examples/features_detection/plot_hog.html
3. [R3] SciPy. `rankdata`. 동률 평균 순위 정의. https://docs.scipy.org/doc/scipy/reference/generated/scipy.stats.rankdata.html
4. [R4] SciPy. `mannwhitneyu`. U와 rank sum 관계. https://docs.scipy.org/doc/scipy/reference/generated/scipy.stats.mannwhitneyu.html
5. [R5] Faiss. *Guidelines to choose an index*. 질의 수·exact search·분할 실행. https://github.com/facebookresearch/faiss/wiki/Guidelines-to-choose-an-index
6. [R6] Faiss. *Faiss indexes*. FlatIP/L2 및 인덱스별 메모리/정확성. https://github.com/facebookresearch/faiss/wiki/Faiss-indexes
7. [R7] Faiss. *Faiss on the GPU*. GPU 인덱스와 CPU/GPU 전송. https://github.com/facebookresearch/faiss/wiki/Faiss-on-the-GPU
8. [R8] SciPy. `distance_transform_edt`. 유클리드 거리 변환 정의. https://docs.scipy.org/doc/scipy/reference/generated/scipy.ndimage.distance_transform_edt.html
9. [R9] Liu et al. *Fast Directional Chamfer Matching*, CVPR 2010. 공간·방향을 함께 고려한 윤곽 비교. https://www.merl.com/publications/TR2010-045
10. [R10] Cao et al. *Edgel Index for Large-Scale Sketch-based Image Search*, CVPR 2011. 대규모 경계 기반 이미지 검색. https://www.microsoft.com/en-us/research/publication/edgel-index-for-large-scale-sketch-based-image-search-3/

위 출처들은 구성 기법의 근거다. 이 문서의 결합 점수·파라미터·성능을 해당 논문에서 검증했다고 해석하지 않는다.

# Bad Apple Earth — 거리장·Chamfer 최적화 통합 구현 지침

> **인계용 단일 Markdown 파일** · 2026-11-01  
> 기존 `Bad_Apple_Shape_Matching_SPEC_v2.md`의 기능과 점수 규칙을 유지하면서 계산 시간을 줄이기 위한 통합 자료.  
> 이전 ZIP 패키지의 `OPTIMIZATION.md`, 구현 참고 코드 2개, 벤치마크 코드 및 실행 결과를 **이 파일 한 개**에 수록했다. 실제 프로젝트 코드는 수정하지 않았다.

## 이 문서의 사용법

- **개발 지침:** 아래의 최적화 설계를 읽고 현재 코드의 병목 위치를 프로파일링하여 교체한다. 단순히 파일을 추가했다고 현재 프로젝트에 자동 통합되는 것은 아니다.
- **참고 코드:** 문서 끝의 부록 A~C 코드 블록을 각각 `exact_capped_chamfer.py`, `exact_sdf.py`, `benchmark.py`로 저장하면 독립적인 재현 실험을 진행할 수 있다. `numpy`, `scipy`, `numba`, `opencv-python-headless` 또는 `opencv-python`이 필요하다. OpenCV 두 패키지는 동시에 설치하지 않는다.
- **현재 프로젝트와 통합:** 기존 목표 프레임/위성영상 view 전처리, 경계 추출, 임곗값 생성, 점수식, 회전 및 최종 선택 계약을 그대로 보존한다. 여기 수록한 코드는 CPU 계산 커널 참고 구현이지 전체 검색기나 실행 가능한 프로젝트가 아니다.
- **정확성:** 기존 수식의 동일성을 우선하고 float32 EDT 반올림·동률 경계 처리를 검증한다. 재현 벤치마크는 `python benchmark.py`로 실행한다.

---

## 최적화 설계 및 적용 지침

작성: 2026-11-01. 적용 대상은 형태 우선 검색 명세 v2의 계산 엔진이다.
현재 사용자 구현의 코드·프로파일·실제 위성사진은 제공되지 않았다. 아래에서 확인한 것은 v2 문서의 계산 구조, 공개 공식 구현, 첨부 참고 커널의 합성 검사다. 실제 저장소를 수정하거나 운영 성능을 측정한 것으로 해석하지 않는다.

## 1. 결론

후보 수, 해상도, 임곗값 수, 두 구조 스케일을 줄이기 전에 다음을 함께 적용한다.

1. 타깃의 마스크/SDF/경계를 후보 루프 밖에서 한 번만 만든다.
2. 후보의 동일 실제 마스크에 대한 거리장·윤곽을 캐시한다.
3. region → SDF → Chamfer 순서로 비음수 점수 하한을 갱신해, 이길 수 없는 경우 비싼 계산을 생략한다.
4. SDF는 precise EDT를 쓰고 작은 작업의 호출·할당·전송 비용을 줄인다.
5. Chamfer는 모든 점 쌍을 비교하지 않는다. v2의 거리 cap을 이용해 **반경 내 정확 비교 + 전역 최소 방향 벌점**으로 같은 점수를 구한다.

PHOG/Rank-layout, 사진 출처, 회전 view, 임곗값 선택, 형태/광도 우선순위는 유지한다. 저품질 축소판이나 작은 데이터셋부터 다시 시작하는 제안이 아니다.

## 2. 현재 명세의 호출 수

흑백 타깃 하나에서 PHOG/Rank 각 Top-2048이 겹치지 않으면 후보는 4096개다. coarse에서 2스케일 × 5임곗값 × SDF용 2EDT를 모두 만들면:

```
4096 × 2 × 5 × 2 = 최대 81,920 EDT 호출 / 타깃 프레임
```

이는 캐시·마스크 중복 제거가 없고 타깃 EDT 자체는 이미 재사용한다는 가정이다. 실제 개수는 후보 중복과 동일 임곗값에 따라 줄고, 중간 명암 타깃의 여러 마스크 경로에서는 다시 늘 수 있다.

고해상도 정밀 후보는 이진 타깃의 경우 coarse Top-256과 두 검색 경로 Top-64 합집합으로 최대 384개다. 여기에 2스케일 × 5임곗값의 SDF와 양방향 Chamfer가 추가된다. 이 구조에서 모든 경계점 쌍의 거리 행렬까지 생성하면 프레임당 수분은 충분히 가능한 연산량이다. 실제 원인이 이것인지는 현재 구현의 프로파일로 구분해야 한다.

## 3. 재사용 가능한 계산과 불가능한 계산

### 3.1 타깃 단위

프레임·구조 스케일·해상도·중첩 명암 레벨별 마스크, SDF, 경계 좌표, 경계 방향, 정렬된 방향 배열을 한 번 만든다. 다른 후보와 비교할 때 같은 타깃 특징을 다시 만들지 않는다. 완전히 같은 마스크가 여러 프레임에 반복되면 해시로 재사용해도 된다. 단순히 인접 프레임이라는 이유만으로 다른 마스크를 재사용하면 안 된다.

### 3.2 후보 단위

가장 중요한 캐시 키는 프레임 번호가 아니라 **실제 마스크를 결정하는 값**이다.

```
view_id + gray_checksum + 해상도 + blur/전처리 버전
+ 실제 threshold가 만드는 마스크 ID + radius/경계 규칙 버전
```

v2의 분위수는 타깃 검정 면적에 의존한다. 따라서 '후보당 다섯 SDF를 영구 사전 계산하면 끝'이라는 주장은 맞지 않는다. 같은 분위수인지보다 실제로 같은 마스크인지 확인해야 한다.

정렬된 후보 밝기 배열에서 `searchsorted(threshold, side='right')`로 구한 포함 픽셀 수가 같으면, 동일 원본 배열과 `<=` 규칙 아래에서는 같은 마스크다. 이 정수 또는 packed mask의 해시로 중복을 찾을 수 있다. 마스크 해시를 쓰면 충돌 방어 및 설정 버전을 포함한다.

- grayscale 디코딩, 두 blur, 필요할 때만 정렬 배열을 유계 캐시에 둔다.
- 실제 등장한 마스크의 SDF·윤곽만 on-demand로 만든다.
- 캐시는 항목 수가 아니라 바이트 예산으로 제한한다. 모든 사진×모든 임곗값×방향별 DT의 영구 저장은 하지 않는다.
- 128×96와 256×192 SDF는 별개다. 작은 SDF를 업스케일해서 고해상도 exact SDF로 사용하지 않는다.
- 회전된 동일 픽셀 격자이고 크롭 변경이 없다면 거리장 회전 재사용이 가능하다. 회전 후 다른 화각으로 크롭하는 view에는 성립하지 않는다. 더 큰 영상의 DT를 자르는 것도, 크롭 안에서 다시 계산한 DT와 일반적으로 같지 않다.

## 4. 점수 하한으로 비싼 계산을 안전하게 생략

이는 유망한 사진을 임의로 Top-32로 줄이는 근사 필터가 아니다. **남은 항목이 가능한 최선이어도 현재 해를 이길 수 없는 경우**만 생략한다.

### 4.1 한 마스크의 fine 점수

기존 목적함수:

```
D = 0.40*SDF + 0.35*Chamfer + 0.25*Region
```

모든 항이 0 이상이므로:

```
Region 계산 후 하한 = 0.25*Region
SDF 계산 후 하한    = 0.40*SDF + 0.25*Region
```

형태가 좋을 가능성이 큰 후보부터 방문해 실제 완전 계산한 해의 점수 U를 빨리 얻는다. 최소 시인성 조건을 통과한 후보의 U를 사용한다. v2 최종 선택은 U+0.01 안의 사진에서 밝기를 고르므로, fine 후보는 하한이 **U+0.01보다 엄격하게 큰 경우**에만 최종 eligible 집합에서 제외할 수 있다. U만 기준으로 자르면 밝기 선택에 필요한 후보를 잃는다.

부동소수점 오차 때문에 경계의 동률을 잘못 없애지 않도록 보수적인 numerical guard를 둔다. 시인성 통과 후보가 아직 없으면 visible 경로에 사용할 유효 U도 없다. v2의 low_visibility fallback을 위해 전체 후보의 형태 최선 기록도 별도로 유지한다.

### 4.2 여러 스케일·임곗값의 결합

v2는 스케일별로 임곗값의 최소 점수를 선택한 후 스케일 평균을 계산한다. 따라서 후보 전체의 하한은:

```
LB_view = mean_over_scales(min_over_thresholds(LB_mask))
```

한 임곗값의 하한이 크다는 이유로 후보 전체를 버리지 않는다. 다른 임곗값이 더 좋은 해를 낼 수 있다. 중간 명암 타깃의 임곗값 순서 제약은 유지한다. 순서 제약을 잠시 풀어 얻은 최소값은 더 낙관적이므로 하한으로는 쓸 수 있지만 최종 점수로 쓸 수 없다.

### 4.3 coarse Top-256도 같은 원리

coarse 점수는 `(0.40*SDF + 0.25*Region)/0.65`다. 처음에는 `0.25*Region/0.65`만으로 하한을 얻는다. 후보 전체에 대해 위의 min/mean 규칙을 적용하고 하한 순으로 방문한다.

실제 평가한 coarse Top-256 heap이 채워진 뒤, 남은 후보의 하한이 현재 256등 실제 점수보다 크면 그 후보의 EDT를 생략할 수 있다. 별도로 보존해야 하는 원래 검색 경로 Top-64 후보는 coarse 절단 여부와 무관하게 fine 단계로 보내는 v2 계약을 유지한다.

이 방법의 가속률은 후보 분포에 달려 있다. 하한이 약하면 많이 생략되지 않을 수 있으므로 절감 횟수를 기록한다. 목적함수를 바꾸지 않는 최적화이지 정해진 배수의 속도 보장이 아니다.

## 5. 거리장: 정확한 EDT를 더 적게, 더 효율적으로

### 5.1 CPU 경로

OpenCV의 `distanceTransform(mask, DIST_L2, DIST_MASK_PRECISE)`는 precise L2 EDT를 제공하며 float32 출력을 사용할 수 있다.[R1] 첨부 `exact_sdf.py`는 두 EDT의 차이, clip, radius 정규화와 공집합/전체집합 처리를 구현한다.

- 결과를 유지하려면 `DIST_MASK_3/5`를 precise의 동의어로 바꾸지 않는다. 해당 L2 설정은 근사다.[R1]
- SciPy EDT의 기본 거리 출력은 float64다. float32 OpenCV 결과는 같은 수학적 거리를 계산하더라도 bitwise 동일하지 않다.[R2]
- 목표마다 새 프로세스를 띄우지 말고 지속 worker pool을 쓴다. 작업은 mask 하나보다 후보 묶음 단위로 보낸다.
- 외부 후보 병렬화와 OpenCV 내부 병렬화가 과도하게 중첩되지 않도록 한 쪽을 제어한다. 내부를 1 thread로 고정한 뒤 여러 worker를 쓰는 구조도 선택지다. CPU 수와 실제 구현에 맞춰 결정한다.
- 두 EDT를 하나의 경계 EDT로 대충 대체하지 않는다. v2의 grid SDF와 달라질 수 있다.

### 5.2 GPU 경로

CuPy에는 PBA+ 기반 exact EDT가 있고 `float64_distances=False`를 사용할 수 있다.[R3] 그러나 마스크 하나씩 CPU→GPU 업로드→작은 EDT 호출→즉시 다운로드하면 전송·런치·동기화가 비용이 될 수 있다. 전체 mask 생성/거리장/점수 reduction을 가능한 한 같은 장치에서 처리하고 작업을 묶는다.

**주의:** `cupyx.scipy.ndimage.distance_transform_edt([B,H,W])`는 B개의 독립 2D EDT가 아니라 3D EDT다. 서로 다른 후보의 픽셀이 이웃이 되므로 틀린 결과다. 공식 구현도 2D/3D 공간 차원으로 분기한다.[R4] 독립 배치를 지원하는 전용 CUDA/PBA 구현 또는 개별 2D 호출을 관리해야 한다. CuPy로 함수 이름만 바꾸는 것이 배치 최적화는 아니다.

마찬가지로 타일을 무작정 붙인 2D atlas는 타일 사이를 가로지르는 거리나 padding의 가짜 경계 문제를 일으킨다. 독립성 증명 없이 사용하지 않는다.

## 6. Chamfer: 현재 식의 거리 cap을 직접 활용

### 6.1 원래 식

방향 차이는 [0,1]의 unsigned 원형 거리 A이고, 공간 반경은 r이다.

```
f(p,Q) = min_{q in Q} [0.8*min(||p-q||/r,1) + 0.2*A(theta_p,theta_q)]
```

v2의 256×192 분석에서는 `r=0.05*192=9.6px`다. 거리 100px와 200px의 공간 벌점은 둘 다 0.8이다. 따라서 각 점에서 전체 다른 경계점을 순회할 필요가 없다.

### 6.2 같은 식으로 정확하게 분해

```
local(p,Q) = min_{q in Q, ||p-q||<r} [0.8*||p-q||/r + 0.2*A(theta_p,theta_q)]
capped_angle(p,Q) = 0.8 + 0.2*min_{q in Q} A(theta_p,theta_q)

f(p,Q) = min(local(p,Q), capped_angle(p,Q))
```

`capped_angle`는 전체 비용 f의 하한이 아니라, 공간 비용을 cap 값으로 놓은 **도달 가능한 상한 후보**다. 가지치기 하한과 혼동하지 않는다.

정확한 이유: 최소 각도를 주는 q가 반경 밖이면 해당 항이 실제 비용이다. 반경 안이면 그 q의 실제 비용은 local에서 더 작거나 같게 계산된다. 따라서 위 min은 반경 밖의 중요한 방향 일치 후보도 놓치지 않으면서 전체 joint minimum을 복원한다. 반경 밖을 그냥 무시하면 안 된다.

### 6.3 구현

- 목적지 윤곽의 unsigned 방향을 [0,pi)에서 정렬한다. 각 질의 방향의 전역 최소 각도 차이는 원형 predecessor/successor 두 개를 검사해 구한다.
- 목적지 경계점의 방향을 2D lookup grid에 둔다. source 점마다 반경 r 안의 정수 offset만 방문해 실제 경계점인지 확인한다.
- offset별 공간 비용은 반경별로 미리 계산하고 거리 순으로 정렬한다.
- 현재 최소 비용보다 offset의 공간 비용 하한이 이미 크면 더 먼 offset은 종료한다.
- 양방향으로 수행한 평균을 취한다. 빈 경계 집합의 v2 처리도 유지한다.
- 방향을 bin으로 양자화하지 않는다. 현재 연속 방향 점수와 같은 정의다.

격자 준비 비용을 제외한 비교 비용은 대략 `O((|E_T|+|E_C|)*(r²+log |E|))`이며, `O(|E_T|*|E_C|)` 전수 쌍 비교와 다르다. 반경이 작게 capped된 현재 목적함수에 맞춘 방식이다. 이미지 크기나 r가 크게 달라지면 항상 최선이라고 주장하지 않는다.

첨부 `exact_capped_chamfer.py`는 이를 Numba nopython/nogil로 구현한다. Python 해석 루프로 offset을 돌리지 않는다. Numba 공식 성능 지침도 전체 루프를 nopython으로 컴파일하는 중요성을 설명한다.[R5]

### 6.4 방향별 distance map 방식과의 구별

방향별 DT와 lookup 역시 확립된 가속 접근이다.[R6] 하지만 후보 마스크마다 방향 bin 수만큼 전체 EDT를 새로 만들면 또 다른 반복 비용이 된다. 현재처럼 고정 위치 비교와 작은 radius cap을 쓰는 경우에는 위 국소 비교가 실용적인 대안이다.

이미 방향별 DT를 구현했다면 호출 수·재사용률을 먼저 계측하고 target map을 후보마다 다시 만들지 않는다. bin 양자화는 연속 방향 joint minimum의 근사이므로, 정확한 대체라고 표시하지 않는다. 가장 가까운 공간점 하나를 고른 뒤 그 점의 각도 벌점을 더하는 것도 joint minimum과 다르다.

## 7. 후보·마스크·GPU 배치의 실행 구조

```
타깃 전처리: 프레임별 한 번
전체 영상의 벡터 검색: 기존 shard/query batch 유지
후보 그룹별 grayscale/blur 로드: 로컬 캐시 재사용
region 하한 계산 → 이길 수 없는 후보/마스크 제외
필요한 SDF만 계산/캐시 → 하한 강화
필요한 정확 Chamfer만 계산
v2의 최소 시인성·형태 허용폭·밝기 선택 유지
```

프레임별 완전 고립 처리보다 여러 프레임의 후보를 view/마스크별로 묶으면 같은 자료의 I/O·blur·SDF를 재사용하기 쉽다. 이는 계산 스케줄 변경이며 시간적 연속성이나 동일 장소 선택 강제가 아니다.

GPU 경로에서 후보마다 `.item()`, `.cpu()`, 전역 synchronize를 호출하지 않는다. 블록 단위 결과만 회수한다. GPU 측정에는 CUDA event 등 완료를 반영하는 타이머를 사용하고 최초 컴파일/컨텍스트 비용을 별도 기록한다.[R7]

## 8. 실제로 수행한 검사와 벤치마크

환경: CPU x86-64, Python 3.13.5, NumPy 2.3.5, SciPy 1.17.0, OpenCV 4.13.0 (1 thread), Numba 0.65.1. GPU는 사용하지 않았다.

합성 인물형 실루엣과 평활화 난수장에서 추출한 256×192 경계: 타깃 1,028점, 후보 5,846점, r=9.6px.

| 양방향 Chamfer 계산 | 중앙값 |
|---|---:|
| NumPy 전체 점쌍 행렬 | 248.79 ms |
| Numba 전체 점쌍 루프 | 36.23 ms |
| Numba capped-radius 정확 비교 | 2.642 ms |

동일한 컴파일 환경의 Numba 전수 비교 대비 약 13.7배, NumPy 행렬 방식 대비 약 94.2배였다. structured 입력에서 NumPy 기준 점수와의 차이는 4.66e-15였다.

256×192 합성 마스크 40개의 두-EDT SDF 생성은 SciPy 평균 마스크당 5.070ms, OpenCV precise는 2.097ms로, 약 2.42배였다. 두 SDF의 최대 절대 차이는 7.74e-08였다. 이 시간은 각 반복에서 마스크 40개를 계산한 시간의 중앙값을 40으로 나눈 값이다.

두 Chamfer 구현은 동일한 입력 윤곽과 연속 방향을 사용했다. 시간에는 JIT 컴파일, 윤곽 추출/prepare, 파일 I/O가 포함되지 않는다. 참고 코드가 계산 엔진의 가속을 보인 것이며 전체 프레임 선택 시간이나 위성사진의 지각적 품질을 측정한 것은 아니다.

`benchmark.py`에는 무작위 크기·반경·빈 집합을 포함한 120개 동치 검사도 있다. 이번 실행의 최대 오차는 2.23e-16 미만이었다. structured 윤곽 검사 오차는 위 표/JSON에 별도로 기록한다. 입력 분포와 구현에 따라 속도는 달라진다.

SDF 결과 차이는 float32/float64 반올림을 포함한다. 최종 top 후보가 수치적으로 동률에 가까울 때는 보수적 동률 처리 또는 기존 백엔드 재평가로 선택 안정성을 관리한다.

## 9. 통합·검증 계약

- 실제 사용자 코드에서 어느 구현이 쓰이는지 확인한다. NumPy all-pairs, compiled all-pairs, orientation-DT, 이미 최적화된 GPU 등 출발점별 개선폭은 다르다.
- 타깃/후보 전처리, EDT, Chamfer, I/O, H2D/D2H, 동기화의 누적 시간과 호출 수를 분리 기록한다. 첫 실행과 warm 실행을 구분한다.
- 기존 경계 추출·임곗값·해상도·스케일·가중치·top-K를 유지한 상태에서 새 커널과 기준 식의 점수를 비교한다.
- 조기 종료를 켠 결과가 전체 점수 계산 결과의 coarse Top-256, protected retrieval 후보, 최종 eligible 집합/밝기 선택과 일치해야 한다.
- 마스크·threshold 중복, 캐시 미스/히트, 생략 EDT/Chamfer 수를 기록한다. 추정 속도 향상을 완료 결과로 보고하지 않는다.
- 회전 후 새 crop이 생기는 view의 거리장을 잘못 재사용하지 않는다.
- 원래 목적함수와 다른 근사를 몰래 도입하지 않는다. 방향 bin 축소, 해상도 축소, threshold 양자화, L1/L∞ 거리, JFA 등은 별도 점수 버전이 필요하다.

## 참고 자료

- [R1] OpenCV — precise/approximate distance transform과 float32 출력: https://docs.opencv.org/4.x/d7/d1b/group__imgproc__misc.html
- [R2] SciPy — exact EDT 및 기본 float64 출력: https://docs.scipy.org/doc/scipy/reference/generated/scipy.ndimage.distance_transform_edt.html
- [R3] CuPy — exact EDT, PBA+, float64_distances: https://docs.cupy.dev/en/stable/reference/generated/cupyx.scipy.ndimage.distance_transform_edt.html
- [R4] CuPy 공식 구현 — ndim 2/3 처리: https://github.com/cupy/cupy/blob/main/cupyx/scipy/ndimage/_distance_transform.py
- [R5] Numba — nopython/compiled loops 성능 지침: https://numba.readthedocs.io/en/stable/user/performance-tips.html
- [R6] Liu et al., Fast Directional Chamfer Matching, CVPR 2010: https://www.merl.com/publications/TR2010-045
- [R7] CuPy — GPU 비동기 실행과 benchmark/warm-up: https://docs.cupy.dev/en/stable/user_guide/performance.html

위 문헌은 기존 도구와 접근의 근거다. capped-radius 분해·캐시 계약·v2용 가지치기 결합은 이번에 작성한 설계와 수학적 유도이며, 논문이 이 프로젝트의 속도를 검증한 것으로 인용하지 않는다.

---

# 부록 — 한 문서에 포함된 참고 코드와 결과

## 부록 A. `exact_capped_chamfer.py`

연속 방향 각도를 유지하는 capped-radius 양방향 Chamfer의 CPU 참고 구현. 기존 경계 추출 결과와 연결해야 한다.

```python
"""Exact capped, bidirectional directional Chamfer for integer pixel contours.

Implements the v2 cost, without orientation bins or an all-pairs distance matrix:
    0.8 * min(||p-q||/r, 1) + 0.2 * unsigned_angle_difference/(pi/2)
Input contours are prepared externally using the SAME edge extraction as before.
Only integer pixel coordinates and one direction per edge pixel are supported.
Numba's first-call compilation is not representative of steady-state timing.
"""
from __future__ import annotations
from dataclasses import dataclass
from functools import lru_cache
import math
import numpy as np
from numba import njit

@dataclass(frozen=True)
class Contour:
    shape: tuple[int, int]
    yx: np.ndarray
    angles: np.ndarray
    angle_image: np.ndarray
    sorted_angles: np.ndarray


def prepare_contour(yx: np.ndarray, angles: np.ndarray,
                    shape: tuple[int, int]) -> Contour:
    """Normalize directions modulo pi; retain exact integer positions."""
    if len(shape) != 2 or min(shape) < 1:
        raise ValueError('shape must be (positive height, positive width)')
    points = np.asarray(yx)
    values = np.asarray(angles, dtype=np.float64)
    if points.ndim != 2 or points.shape[1] != 2 or values.shape != (len(points),):
        raise ValueError('Expected yx[N,2], angles[N]')
    if not np.isfinite(points).all() or not np.isfinite(values).all():
        raise ValueError('Non-finite contour input')
    if not np.equal(points, np.rint(points)).all():
        raise ValueError('Subpixel contours require a different spatial index')
    points = np.ascontiguousarray(points, dtype=np.int32)
    if len(points) and (np.any(points < 0) or np.any(points[:, 0] >= shape[0])
                       or np.any(points[:, 1] >= shape[1])):
        raise ValueError('Contour outside image')
    if len(np.unique(points, axis=0)) != len(points):
        raise ValueError('Only one direction per pixel is supported')
    values = np.ascontiguousarray(np.mod(values, math.pi))
    image = np.full(shape, -1.0, dtype=np.float64)
    if len(points):
        image[points[:, 0], points[:, 1]] = values
    return Contour(tuple(shape), points, values, image, np.sort(values))


@lru_cache(maxsize=16)
def _offsets(radius: float) -> tuple[np.ndarray, np.ndarray]:
    if not math.isfinite(radius) or radius <= 0:
        raise ValueError('radius must be finite and positive')
    n = math.ceil(radius)
    offsets = [(dy, dx, math.hypot(dy, dx))
               for dy in range(-n, n+1) for dx in range(-n, n+1)
               if dy*dy+dx*dx < radius*radius]
    offsets.sort(key=lambda v: (v[2], v[0], v[1]))
    xy = np.asarray([(dy, dx) for dy, dx, _ in offsets], dtype=np.int32)
    costs = np.asarray([0.8*d/radius for _, _, d in offsets], dtype=np.float64)
    return xy, costs


@njit(cache=True, nogil=True)
def _angle_diff(a: float, b: float) -> float:
    d = abs(a-b)
    return min(d, math.pi-d)


@njit(cache=True, nogil=True)
def _directed(yx, angles, other_image, other_sorted, offsets, spatial_cost):
    h, w = other_image.shape
    n_other = len(other_sorted)
    total = 0.0
    for i in range(len(yx)):
        y, x = yx[i, 0], yx[i, 1]
        angle = angles[i]
        pos = np.searchsorted(other_sorted, angle)
        # Circular predecessor and successor give the exact global nearest angle.
        prev = other_sorted[(pos-1) % n_other]
        next_ = other_sorted[pos % n_other]
        delta = min(_angle_diff(angle, prev), _angle_diff(angle, next_))
        best = 0.8 + (0.4/math.pi)*delta
        for j in range(len(offsets)):
            spatial = spatial_cost[j]
            # Sorted by radius. All subsequent points have a no-better lower bound.
            if spatial > best:
                break
            yy, xx = y+offsets[j, 0], x+offsets[j, 1]
            if yy < 0 or yy >= h or xx < 0 or xx >= w:
                continue
            other_angle = other_image[yy, xx]
            if other_angle < 0:
                continue
            cost = spatial + (0.4/math.pi)*_angle_diff(angle, other_angle)
            if cost < best:
                best = cost
        total += best
    return total / len(yx)


def capped_directional_chamfer(a: Contour, b: Contour, radius: float) -> float:
    """Same mathematical score as the v2 all-pairs formula (float rounding aside)."""
    if a.shape != b.shape:
        raise ValueError('Contours must share the same image grid')
    radius = float(radius)
    offsets, spatial = _offsets(radius)
    if not len(a.yx) and not len(b.yx):
        return 0.0
    if not len(a.yx) or not len(b.yx):
        return 1.0
    ab = _directed(a.yx, a.angles, b.angle_image, b.sorted_angles, offsets, spatial)
    ba = _directed(b.yx, b.angles, a.angle_image, a.sorted_angles, offsets, spatial)
    return float(0.5*(ab+ba))
```

## 부록 B. `exact_sdf.py`

OpenCV precise L2 EDT를 이용하는 SDF 참고 구현. SciPy 대비 float32 반올림 차이가 있을 수 있다.

```python
"""Float32 SDF matching the v2 grid definition, using precise OpenCV L2 EDT."""
from __future__ import annotations
import math
import cv2
import numpy as np


def sdf_v2(mask: np.ndarray, radius: float | None = None) -> np.ndarray:
    """Return clipped signed distances, positive inside the dark/True mask.

    Uses two exact Euclidean distance transforms (float32 rounding applies).
    Empty/full masks have explicit -1/+1 fields; no artificial frame padding.
    Does not change global OpenCV thread settings. Configure worker threads at
    the application level, avoiding nested thread oversubscription.
    """
    m = np.asarray(mask)
    if m.ndim != 2 or m.size == 0:
        raise ValueError('Expected a non-empty two-dimensional mask')
    if not np.isfinite(m).all():
        raise ValueError('Mask must contain finite values')
    m = np.ascontiguousarray(m != 0, dtype=np.uint8)
    r = 0.05 * min(m.shape) if radius is None else float(radius)
    if not math.isfinite(r) or r <= 0:
        raise ValueError('radius must be finite and positive')
    count = int(m.sum())
    if count == 0:
        return np.full(m.shape, -1.0, dtype=np.float32)
    if count == m.size:
        return np.ones(m.shape, dtype=np.float32)
    inside = cv2.distanceTransform(m, cv2.DIST_L2, cv2.DIST_MASK_PRECISE)
    outside = cv2.distanceTransform(1-m, cv2.DIST_L2, cv2.DIST_MASK_PRECISE)
    inside -= outside
    np.clip(inside, -r, r, out=inside)
    inside /= r
    return inside
```

## 부록 C. `benchmark.py`

합성 이미지 기반 동치 확인·속도 측정 코드. 세 코드 블록을 각각 이름대로 저장한 뒤 실행한다.

```python
"""Synthetic CPU-only microbenchmark, NOT the user's application workload."""
from __future__ import annotations
import json, math, os, platform, time
from pathlib import Path
from numba import njit
import cv2, numpy as np, scipy, numba
from scipy.ndimage import distance_transform_edt
from exact_capped_chamfer import prepare_contour, capped_directional_chamfer

cv2.setNumThreads(1)
rng = np.random.default_rng(20261101)


def naive(a, b, r):
    if not len(a.yx) and not len(b.yx): return 0.0
    if not len(a.yx) or not len(b.yx): return 1.0
    delta = a.yx.astype(np.float64)[:, None, :] - b.yx[None, :, :]
    spatial = np.minimum(np.sqrt(np.sum(delta*delta, axis=2))/r, 1.0)
    angular = np.abs(a.angles[:, None]-b.angles[None, :])
    angular = np.minimum(angular, math.pi-angular)/(math.pi/2)
    cost = 0.8*spatial + 0.2*angular
    return float((cost.min(axis=1).mean()+cost.min(axis=0).mean())/2)


@njit(cache=True, nogil=True)
def pairwise_compiled(ayx, at, byx, bt, r):
    mins_a = np.ones(len(at), dtype=np.float64)
    mins_b = np.ones(len(bt), dtype=np.float64)
    for i in range(len(at)):
        for j in range(len(bt)):
            dy = float(ayx[i,0])-float(byx[j,0])
            dx = float(ayx[i,1])-float(byx[j,1])
            spatial = min(math.sqrt(dy*dy+dx*dx)/r, 1.0)
            d = abs(at[i]-bt[j])
            angular = min(d, math.pi-d)/(math.pi/2)
            cost = 0.8*spatial+0.2*angular
            if cost < mins_a[i]: mins_a[i]=cost
            if cost < mins_b[j]: mins_b[j]=cost
    return 0.5*(mins_a.mean()+mins_b.mean())


def random_contour(shape, n):
    ids = rng.choice(shape[0]*shape[1], size=n, replace=False)
    yx = np.column_stack(np.unravel_index(ids, shape))
    return prepare_contour(yx, rng.uniform(0, math.pi, n), shape)


def timed(fn, repeat=7):
    fn()
    times=[]
    for _ in range(repeat):
        t=time.perf_counter(); fn(); times.append(time.perf_counter()-t)
    return float(np.median(times))

# Exact-score sanity checks, varying radius, density, borders and empty sets.
max_error=0.0
for i in range(120):
    shape=(int(rng.integers(12, 45)), int(rng.integers(12, 50)))
    a=random_contour(shape, int(rng.integers(0, 100)))
    b=random_contour(shape, int(rng.integers(0, 100)))
    r=float(rng.choice([0.25, 1.0, 2.4, 4.8, 9.6, 100.0]))
    e=abs(capped_directional_chamfer(a,b,r)-naive(a,b,r))
    max_error=max(max_error,e)
    assert e<1e-12, (i,e)

shape=(192,256); r=9.6
# Distinct grid sites with continuous angles, not natural-photo benchmarks.
a=random_contour(shape, 1200); b=random_contour(shape, 2400)
fast=timed(lambda:capped_directional_chamfer(a,b,r), repeat=11)
slow=timed(lambda:naive(a,b,r), repeat=5)
score_error=abs(capped_directional_chamfer(a,b,r)-naive(a,b,r))
compiled=timed(lambda:pairwise_compiled(a.yx,a.angles,b.yx,b.angles,r), repeat=7)
compiled_error=abs(pairwise_compiled(a.yx,a.angles,b.yx,b.angles,r)-capped_directional_chamfer(a,b,r))

# A second microbenchmark uses contours extracted from binary masks.
def contour_from_mask(mask):
    edge=np.zeros(mask.shape,dtype=bool)
    diff=mask[1:,:]!=mask[:-1,:]
    edge[1:,:]|=diff; edge[:-1,:]|=diff
    diff=mask[:,1:]!=mask[:,:-1]
    edge[:,1:]|=diff; edge[:,:-1]|=diff
    smooth=cv2.GaussianBlur(mask.astype(np.float64),(0,0),1.0,borderType=cv2.BORDER_REFLECT)
    dx=cv2.Sobel(smooth,cv2.CV_64F,1,0,ksize=3,borderType=cv2.BORDER_REFLECT)
    dy=cv2.Sobel(smooth,cv2.CV_64F,0,1,ksize=3,borderType=cv2.BORDER_REFLECT)
    points=np.argwhere(edge)
    angles=np.mod(np.arctan2(dy[edge],dx[edge]),math.pi)
    return prepare_contour(points,angles,mask.shape)
y,x=np.indices(shape)
target_mask=((x-130)**2/28**2+(y-98)**2/53**2<1) | ((x-130)**2+(y-32)**2<20**2)
target_mask |= ((x>45)&(x<205)&(y>85)&(y<99))
field=cv2.GaussianBlur(rng.normal(size=shape).astype(np.float32),(0,0),5.0,borderType=cv2.BORDER_REFLECT)
candidate_mask=field<np.quantile(field,0.4)
aa=contour_from_mask(target_mask); bb=contour_from_mask(candidate_mask)
structured_fast=timed(lambda:capped_directional_chamfer(aa,bb,r),repeat=9)
structured_compiled=timed(lambda:pairwise_compiled(aa.yx,aa.angles,bb.yx,bb.angles,r),repeat=5)
structured_numpy=timed(lambda:naive(aa,bb,r),repeat=3)
structured_error=abs(capped_directional_chamfer(aa,bb,r)-naive(aa,bb,r))
assert structured_error < 1e-12

# SDF timings on smooth random masks. Same original two-EDT SDF definition.
sdf_results=[]
for h,w in [(96,128),(192,256)]:
    masks=[]
    for _ in range(40):
        f=cv2.GaussianBlur(rng.normal(size=(h,w)).astype(np.float32), (0,0), 3.0,
                           borderType=cv2.BORDER_REFLECT)
        masks.append(np.ascontiguousarray((f<=np.quantile(f,0.4)),dtype=np.uint8))
    radius=0.05*min(h,w)
    def sdf_scipy(m):
        return np.clip(distance_transform_edt(m)-distance_transform_edt(1-m),-radius,radius)/radius
    def sdf_cv(m):
        dt1=cv2.distanceTransform(m,cv2.DIST_L2,cv2.DIST_MASK_PRECISE)
        dt0=cv2.distanceTransform(1-m,cv2.DIST_L2,cv2.DIST_MASK_PRECISE)
        return np.clip(dt1-dt0,-radius,radius)/radius
    t_s=timed(lambda:[sdf_scipy(m) for m in masks], repeat=5)/len(masks)
    t_c=timed(lambda:[sdf_cv(m) for m in masks], repeat=5)/len(masks)
    err=max(float(np.max(np.abs(sdf_scipy(m)-sdf_cv(m)))) for m in masks)
    sdf_results.append({'shape':[h,w],'scipy_s_per_mask':t_s,'opencv_s_per_mask':t_c,
                        'speedup':t_s/t_c,'max_absolute_sdf_error':err,'masks':len(masks)})
report={'scope':'Synthetic CPU microbenchmarks only; no real satellite imagery, production pipeline or GPU.',
        'environment':{'python':platform.python_version(),'numpy':np.__version__,'scipy':scipy.__version__,
         'opencv':cv2.__version__,'numba':numba.__version__,'opencv_threads':cv2.getNumThreads(),
         'cpu_count':os.cpu_count(),'machine':platform.machine()},
        'chamfer':{'shape':list(shape),'target_edges':len(a.yx),'candidate_edges':len(b.yx),'radius':r,
          'pairwise_numpy_s':slow,'pairwise_numba_s':compiled,'radius_numba_s':fast,'numpy_to_radius_speedup':slow/fast,'compiled_to_radius_speedup':compiled/fast,'absolute_score_error':score_error,'compiled_score_error':compiled_error,
          'random_equivalence_tests':120,'max_random_equivalence_error':max_error,
          'timing_excludes':'JIT, contour preparation, I/O; contours reusable; one comparison includes both directions'},
        'structured_contour_chamfer':{'shape':list(shape),'target_edges':len(aa.yx),'candidate_edges':len(bb.yx),'radius':r,
         'pairwise_numpy_s':structured_numpy,'pairwise_numba_s':structured_compiled,'radius_numba_s':structured_fast,
         'numpy_to_radius_speedup':structured_numpy/structured_fast,
         'compiled_to_radius_speedup':structured_compiled/structured_fast,
         'absolute_score_error':structured_error,'input':'Synthetic silhouette and smooth random binary field'},
        'sdf':sdf_results}
Path(__file__).with_name('benchmark_results.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
print(json.dumps(report,indent=2))
```

## 부록 D. `benchmark_results.json`

이전 실행 환경에서 얻은 측정 로그. 실제 사용 환경의 속도를 보장하지 않는다.

```json
{
  "scope": "Synthetic CPU microbenchmarks only; no real satellite imagery, production pipeline or GPU.",
  "environment": {
    "python": "3.13.5",
    "numpy": "2.3.5",
    "scipy": "1.17.0",
    "opencv": "4.13.0",
    "numba": "0.65.1",
    "opencv_threads": 1,
    "cpu_count": 5,
    "machine": "x86_64"
  },
  "chamfer": {
    "shape": [
      192,
      256
    ],
    "target_edges": 1200,
    "candidate_edges": 2400,
    "radius": 9.6,
    "pairwise_numpy_s": 0.11602664800000184,
    "pairwise_numba_s": 0.017545794000000114,
    "radius_numba_s": 0.0007418540000116991,
    "numpy_to_radius_speedup": 156.40091985508212,
    "compiled_to_radius_speedup": 23.651276396330566,
    "absolute_score_error": 1.1102230246251565e-16,
    "compiled_score_error": 0.0,
    "random_equivalence_tests": 120,
    "max_random_equivalence_error": 2.220446049250313e-16,
    "timing_excludes": "JIT, contour preparation, I/O; contours reusable; one comparison includes both directions"
  },
  "structured_contour_chamfer": {
    "shape": [
      192,
      256
    ],
    "target_edges": 1028,
    "candidate_edges": 5846,
    "radius": 9.6,
    "pairwise_numpy_s": 0.2487906519999683,
    "pairwise_numba_s": 0.036227263000000676,
    "radius_numba_s": 0.002642228000013347,
    "numpy_to_radius_speedup": 94.15941849027092,
    "compiled_to_radius_speedup": 13.710876956802243,
    "absolute_score_error": 4.6629367034256575e-15,
    "input": "Synthetic silhouette and smooth random binary field"
  },
  "sdf": [
    {
      "shape": [
        96,
        128
      ],
      "scipy_s_per_mask": 0.0012790889499996183,
      "opencv_s_per_mask": 0.0005623981500008313,
      "speedup": 2.2743477196675124,
      "max_absolute_sdf_error": 6.64037321795874e-08,
      "masks": 40
    },
    {
      "shape": [
        192,
        256
      ],
      "scipy_s_per_mask": 0.005070212699999388,
      "opencv_s_per_mask": 0.0020966468999986886,
      "speedup": 2.418248251530841,
      "max_absolute_sdf_error": 7.74230712874413e-08,
      "masks": 40
    }
  ]
}
```

## 적용 후 검증 체크리스트

- [ ] 현재 구현에서 EDT 호출 수, Chamfer 점쌍 수, 각 계산의 총 소요 시간을 측정했다.
- [ ] 기존 SDF 정의와 새 precise EDT의 오차 범위를 확인했다.
- [ ] capped Chamfer의 양방향 점수가 기존 all-pairs 정의와 실수 오차 내에서 일치한다.
- [ ] 타깃과 반복 후보의 동일 마스크를 캐시하고, 서로 다른 crop/rotation의 마스크를 잘못 공유하지 않는다.
- [ ] 조기 종료 기준은 **최선 형태 점수 + 0.01**의 밝기 선택 허용폭을 보존한다.
- [ ] coarse Top-256 및 검색 경로별 보호 Top-64, fine 후보와 최종 선택이 완전 계산 결과와 일치한다.
- [ ] 변경 전후 같은 원본 MP4/수집 데이터/설정으로 출력 프레임 선택과 결과 영상을 비교했다.
- [ ] 합성 마이크로벤치마크 개선치를 전체 파이프라인의 측정 결과로 혼동하지 않는다.

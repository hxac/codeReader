# u4-l7 PosEmbedding：KV RMSNorm+RoPE Cache 与旋转位置编码

## 1. 本讲目标

本讲深入 `posembedding` 算子族的两个目录：

- `ai_infra_kv_rms_norm_rope_cache`：MLA（Multi-head Latent Attention）推理中「RMSNorm + RoPE + 写 KV Cache + 可选量化」四合一融合算子，是仓库中**模式组合最密集**的中小型算子之一；
- `ai_infra_rotary_position_embedding`：独立的旋转位置编码算子（rotary_mul / rotate half），只做 \( y = x \odot \cos + x_{\text{rotate}} \odot \sin \) 一件事。

学完本讲，你应该能够：

1. 说出 `cache_mode`（Norm/PA/PA_NZ/PA_BLK_BNSD/PA_BLK_NZ）、`rotary_mode`（interleave-half/half）、`quant_mode`（none/static/pertile128）三个属性如何组合出 18 个 TilingKey、9 个 kernel 头文件的变体矩阵；
2. 理解 arch35（Ascend 950PR/DT，`__CCE_AICORE__ == 310`）专用 regbase 模板与通用 DS 模板的差异：三态返回值驱动的模板轮询、UB 预算公式、「D 全载 / 二分重算」两种兜底策略；
3. 读懂 RoPE 在 AscendC kernel 里的七步向量化实现，以及 rotate half 算子的「换半 + 取负 + 乘加」三步实现；
4. 为「PageAttention + NZ 格式 + 静态量化」这类具体场景，沿 host 侧 TilingKey 决策树和 device 侧分发表选出正确的 tiling 文件与 kernel 头文件。

本讲承接 u2-l3（TilingBaseClass 七步框架与 TilingKey）、u2-l4（kernel 入口与 `TILING_KEY_IS` 分支）、u4-l1（MLA 概念与 TilingKey 位段编码）、u4-l5（MoE 的 TilingKey 编码与 UT 镜像断言）。

## 2. 前置知识

### 2.1 MLA 的 KV Cache 写入为什么值得融合

回顾 u4-l1：MLA 把多头注意力的 K/V 压缩到一个低秩潜在向量 `kv` 中。每生成一个 token，推理框架要做三件事：

1. 把 `kv` 的前段 `kv_a` 做 **RMSNorm**（吸收缩放）；
2. 把尾段 `kv_pe` 做 **RoPE**（旋转位置编码）；
3. 按 `index` 指示的位置**散射写入（ScatterUpdate）KV Cache**，供后续注意力算子读取。

如果拆成三个算子，中间结果要在 GM 里来回倒。融合成一个算子后，数据从 GM 搬进 UB 一次，做完三件事直接写回 Cache——这就是本算子存在的理由。文档给出的公式（`docs/npu_ai_infra_kv_rms_norm_rope_cache.md`）：

\[ \operatorname{RmsNorm}(x_i)=\frac{x_i \cdot gamma_i}{\sqrt{\frac{1}{n}\sum_{j=1}^{n}x_j^2+\epsilon}} \]

\[ x_{\text{rotate}} = \operatorname{concat}(-x_2,\; x_1), \quad x_{\text{rope}} = x \odot \cos + x_{\text{rotate}} \odot \sin \]

### 2.2 两套 D 维规格：V1 与 V2

算子内部用 `methodMode` 区分两代输入排布（由可选输入 `v` 是否存在决定，见 `ai_infra_kv_rms_norm_rope_cache_comm.h:54-62` 的常量表）：

| methodMode | 判定 | kv 尾轴 D | RMSNorm 段 Dv | RoPE 段 Dk | v 输入 |
|---|---|---|---|---|---|
| V1（`methodMode_==0`） | 不传 `v` | 576 = 512 + 64 | 512 | 64 | 无 |
| V2（`methodMode_==1`） | 传 `v` | 192（RoPE 段） | 128（在 v 里） | 64 | (B,N,S,128) |

### 2.3 三个「模式旋钮」

- **cache_mode**：Cache 的物理排布。`Norm` 顺序连续；`PA`/`PA_BNSD` 页注意力（按 blockSize 分页）；`PA_NZ`/`PA_BLK_NZ` 页注意力 + NZ 矩阵排布（D 维切成 16 元素的小块再重排，见 4.2.3）；`PA_BLK_BNSD` 按页块索引的 BNSD。
- **rotary_mode**：`interleave-half`（偶奇交错取对，\( x_1=x_{2i},x_2=x_{2i+1} \)）与 `half`（前后两半 \( x_1=x_{[:D/2]},x_2=x_{[D/2:]} \)）。
- **quant_mode**：`none` / `static`（对称或非对称静态量化，scale/offset 作为输入张量给出）/ `pertile128`（每 128 元素一个 scale 的动态量化，代码里叫 c8）。

### 2.4 MTP 与 B1SD

- **MTP**（Multi-Token Prediction）：一次前向写多个 token（`Skv > 1` 且 `Srope = 1`，多 batch 共享同一份 rope 系数），tiling 侧 `isMTP_ = (seqLen > 1)`。
- **B1SD/CutT**：`kv` 与 `cos/sin` 的 S 维对齐（`Skv == Srope > 1`）时，B、S 双重循环可坍缩成 token 流水，是吞吐最高的切分策略。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| `ascendc/src/ops-transformer/posembedding/ai_infra_kv_rms_norm_rope_cache/op_api/aclnn_ai_infra_kv_rms_norm_rope_cache.cpp` | aclnn 两段式接口：参数检查、Contiguous/CreateView、调 L0 |
| `ascendc/src/ops-transformer/posembedding/ai_infra_kv_rms_norm_rope_cache/op_host/ai_infra_kv_rms_norm_rope_cache_def.cpp` | OpDef：五属性（epsilon/cache_mode/rotary_mode/quant_mode/is_output_kv）、多 SOC 配置 |
| `ascendc/src/ops-transformer/posembedding/ai_infra_kv_rms_norm_rope_cache/op_host/ai_infra_kv_rms_norm_rope_cache_tiling.h` | 三套 TilingData 定义、CacheMode/QuantClass/RotaryMode/CacheLayout 枚举、tiling 类层次 |
| `ascendc/src/ops-transformer/posembedding/ai_infra_kv_rms_norm_rope_cache/op_host/ai_infra_kv_rms_norm_rope_cache_base_tiling.cpp` | tiling 公共基类：形状/属性解析、平台信息、`isRegbase_` 探测 |
| `ascendc/src/ops-transformer/posembedding/ai_infra_kv_rms_norm_rope_cache/op_host/ai_infra_kv_rms_norm_rope_cache_ds_tiling.cpp` | DS 模板：TilingKey 决策树（1000~5021） |
| `ascendc/src/ops-transformer/posembedding/ai_infra_kv_rms_norm_rope_cache/op_host/arch35/ai_infra_kv_rms_norm_rope_cache_regbase_full_load_tiling.cpp` | arch35 D 全载模板：UB 预算公式，TilingKey=10000 |
| `ascendc/src/ops-transformer/posembedding/ai_infra_kv_rms_norm_rope_cache/op_host/arch35/ai_infra_kv_rms_norm_rope_cache_regbase_recompute_tiling.cpp` | arch35 二分重算模板：大 D 兜底，TilingKey=20000 |
| `ascendc/src/ops-transformer/posembedding/ai_infra_kv_rms_norm_rope_cache/op_kernel/ai_infra_kv_rms_norm_rope_cache.cpp` | kernel 入口：arch 分支 + 18 路 TilingKey 分发表 |
| `ascendc/src/ops-transformer/posembedding/ai_infra_kv_rms_norm_rope_cache/op_kernel/ai_infra_kv_rms_norm_rope_cache_b16_pa.h` | PA 家族通用 kernel：Init/Process、RoPE 七步、NZ 散射 |
| `ascendc/src/ops-transformer/posembedding/ai_infra_kv_rms_norm_rope_cache/op_kernel/ai_infra_kv_rms_norm_rope_cache_comm.h` | kernel 类继承层次与 V1/V2 常量表 |
| `ascendc/src/ops-transformer/posembedding/ai_infra_kv_rms_norm_rope_cache/op_kernel/arch35/ai_infra_kv_rms_norm_rope_cache_regbase_full_load.h` | 950 专用 regbase kernel（全载） |
| `ascendc/src/ops-transformer/posembedding/ai_infra_rotary_position_embedding/op_host/ai_infra_rotary_position_embedding_def.cpp` | rotary_mul 的 OpDef：x/cos/sin/rotate 可选 + mode 属性 |
| `ascendc/src/ops-transformer/posembedding/ai_infra_rotary_position_embedding/op_host/ai_infra_rotary_position_embedding_tiling.h` | RotateHalf 的 TilingData 与布局枚举 |
| `ascendc/src/ops-transformer/posembedding/ai_infra_rotary_position_embedding/op_host/ai_infra_rope_rotate_half_tiling.h` | rotate_half tiling 模板类：SOC 白名单 + IsCapable |
| `ascendc/src/ops-transformer/posembedding/ai_infra_rotary_position_embedding/op_kernel/ai_infra_rotary_position_embedding.cpp` | rotary kernel 入口：11xx 全载 / 10xx 常规两路分发 |
| `ascendc/src/ops-transformer/posembedding/ai_infra_rotary_position_embedding/op_kernel/ai_infra_rotate_half.h` | RotateHalf kernel：五种 layout 的 Process 分派 |
| `ascendc/src/ops-transformer/posembedding/ai_infra_rotary_position_embedding/op_kernel/ai_infra_rotate_half_base.h` | SinCompute/XNewCopy/ComputeInner 三个数学原语 |

## 4. 核心概念与源码讲解

### 4.1 模块一：KV Cache 写入——一个算子吃下四件事

#### 4.1.1 概念说明

MLA 预填充/解码每步都要「归一化 + 旋转 + 写缓存」，本算子把它们融成一次 kernel 调用，并额外承担两件 host 侧杂务：

- **原地写 Cache**：`k_cache`/`ckv_cache` 既是输入又是输出（scatter update），不能整块拷贝；
- **可选量化**：写 Cache 前把 fp16/bf16 压成 int8/fp8（省一半以上 Cache 带宽），scale/offset 由框架给。

「写 Cache」的本质是**按 index 散射**：`index[i]` 给出第 i 个 token 要写到 Cache 的哪一行，负值跳过（见 4.3.3 的 `seqIndex < 0` 分支）。

#### 4.1.2 核心流程

```text
aclnnAiInfraKvRmsNormRopeCacheGetWorkspaceSize   (Host, 同步)
  ├─ CheckAiInfraKvRmsNormRopeCacheParams   空指针 → 维度 → dtype 三层检查
  ├─ 只读输入 kv/gamma/cos/sin/index → l0op::Contiguous 连续化
  ├─ ckv_cache/k_cache(原地目标)  → executor->CreateView 零拷贝保留 stride
  │    └─ k_cache 缺省时造一个 shape={0} 的占位张量（SFA 融合缓存场景）
  └─ l0op::AiInfraKvRmsNormRopeCache 登记进 executor → 触发 tiling
       └─ TilingRegistry 轮询：DS(1000) → FullLoad(2000) → Recompute(3000)
aclnnAiInfraKvRmsNormRopeCache                   (第二段, 异步)
  └─ CommonOpExecutorRun(workspace, size, executor, stream)
```

#### 4.1.3 源码精读

**（1）参数检查与 dtype 白名单**。[aclnn_ai_infra_kv_rms_norm_rope_cache.cpp:37-69](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/posembedding/ai_infra_kv_rms_norm_rope_cache/op_api/aclnn_ai_infra_kv_rms_norm_rope_cache.cpp#L37-L69) 先判空（`OP_CHECK_NULL`）、再查维度（kv 必须 4 维）、再查 dtype：kv/gamma/cos/sin 只允许 FP16/BF16，而 **cache 允许 INT8/FLOAT8_E4M3FN/HIFLOAT8**（[第 33-35 行](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/posembedding/ai_infra_kv_rms_norm_rope_cache/op_api/aclnn_ai_infra_kv_rms_norm_rope_cache.cpp#L33-L35)）——输入半精度、存储更窄，正是量化 Cache 的接口体现。这套「先判空 → 再解引用」的三步检查与 u2-l2 讲过的套路一致。

**（2）原地目标的 CreateView 与占位张量**。[aclnn_ai_infra_kv_rms_norm_rope_cache.cpp:94-118](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/posembedding/ai_infra_kv_rms_norm_rope_cache/op_api/aclnn_ai_infra_kv_rms_norm_rope_cache.cpp#L94-L118)：`ckv_cache` 是原地写入目标，必须 `CreateView` 零拷贝保留 view stride/offset（否则原地语义被破坏，同 u4-l4 conv_states 的处理）；而 `k_cache` 为空时（SFA 模式：K 与 V 合并存一份），手工 `aclCreateTensor` 一个 shape 为 `{0}` 的空张量顶位，让下游 L0 接口签名统一。

**（3）五个属性即三个模式旋钮**。[ai_infra_kv_rms_norm_rope_cache_def.cpp:249-253](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/posembedding/ai_infra_kv_rms_norm_rope_cache/op_host/ai_infra_kv_rms_norm_rope_cache_def.cpp#L249-L253)：

```cpp
this->Attr("epsilon").AttrType(OPTIONAL).Float(1e-5);
this->Attr("cache_mode").AttrType(OPTIONAL).String("Norm");
this->Attr("rotary_mode").AttrType(OPTIONAL).String("interleave-half");
this->Attr("quant_mode").AttrType(OPTIONAL).String("static");
this->Attr("is_output_kv").AttrType(OPTIONAL).Bool(false);
```

**（4）字符串属性翻译成枚举**。tiling 侧把字符串映射成强类型枚举：[ai_infra_kv_rms_norm_rope_cache_base_tiling.cpp:319-334](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/posembedding/ai_infra_kv_rms_norm_rope_cache/op_host/ai_infra_kv_rms_norm_rope_cache_base_tiling.cpp#L319-L334) 用 `unordered_map` 把 `"PA"→CacheMode::PA`、`"PA_BNSD"→CacheMode::PA`、`"PA_NZ"→PA_NZ`、`"PA_BLK_BNSD"→PA_BLK_BNSD`、`"PA_BLK_NZ"→PA_BLK_NZ`，查不到一律回落 `NORM`。枚举本体定义在 [ai_infra_kv_rms_norm_rope_cache_tiling.h:132-157](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/posembedding/ai_infra_kv_rms_norm_rope_cache/op_host/ai_infra_kv_rms_norm_rope_cache_tiling.h#L132-L157)：`CacheMode`（NORM/PA/PA_NZ/PA_BLK_BNSD/PA_BLK_NZ）、`QuantClass`（NONE/STATIC/PERTILE_128）、`RotaryMode`（INTER_HALF/HALF），外加一个 host 内部用的 `CacheLayout`（CONTIGUOUS/SWA/SFA_NO_QUANT/SFA_C8）——**用户属性说的是「想要什么」，CacheLayout 说的是「实际拿到什么」**，后者由 k_cache 是否为空、stride 是否大于默认值推断（见 4.2.3）。

**（5）V1/V2 与索引校验**。[ai_infra_kv_rms_norm_rope_cache_base_tiling.cpp:160-181](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/posembedding/ai_infra_kv_rms_norm_rope_cache/op_host/ai_infra_kv_rms_norm_rope_cache_base_tiling.cpp#L160-L181) 的 `CheckIndexValid`：NORM/PA/PA_NZ 模式下 `index` 的元素个数必须等于 \( B \times S \)（每 token 一个槽位）；而 PA_BLK_* 模式下按页给索引，个数须等于 \( B \times \lceil S / \text{blockSize} \rceil \)——两种索引粒度（token 级 / 页级）是 `PA` 与 `PA_BLK_*` 的本质区别。

#### 4.1.4 代码实践

**实践目标**：用 ST 测试观察三个模式旋钮的组合如何被真实调用。

**操作步骤**（有昇腾环境时）：

1. 打开 [tests/st/test_ai_infra_kv_rms_norm_rope_cache.py](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/posembedding/ai_infra_kv_rms_norm_rope_cache/tests/st/test_ai_infra_kv_rms_norm_rope_cache.py)（约第 774-793 行有三个 v2 用例），观察它们只改三个属性就切换了三种 Cache 布局：

```python
# 示例代码（摘自 ST 测试的调用参数，运行需昇腾环境 + 已装 run/wheel 包）
def test_..._v2_SFA_NQ_bf16_01(self):
    self.run_case(cache_mode="PA_BNSD", rotary_mode="half",
                  quant_mode="static", ...)      # SFA 无量化布局

def test_..._v2_SWA_bf16_01(self):
    self.run_case(cache_mode="PA_BNSD", rotary_mode="half",
                  quant_mode="static", ...)      # K/V 分离（SWA）布局

def test_..._v2_SFA_C8_bf16_01(self):
    self.run_case(cache_mode="PA_BNSD", rotary_mode="half",
                  quant_mode="pertile128", ...)  # 动态每 128 量化
```

2. 执行单条用例：`pytest ascendc/src/ops-transformer/posembedding/ai_infra_kv_rms_norm_rope_cache/tests/st/test_ai_infra_kv_rms_norm_rope_cache.py -k "SFA_C8" -x`。

**需要观察的现象**：三条用例输入张量形状几乎相同，仅 stride/scale 张量有无不同，但各自命中不同 kernel。

**预期结果**：三条用例分别输出不同 TilingKey（可在 host 日志 `TilingKey Decision Start` 打点处确认，见 4.2.3 第 (4) 点）。无硬件时标注：**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `ckv_cache` 用 `CreateView` 而 `kv` 用 `l0op::Contiguous`？
**答案**：`kv` 是只读输入，不连续时拷贝一份连续副本即可（占 workspace）；`ckv_cache` 是原地写入目标，必须保留原张量的 stride/offset 让 kernel 按视图偏移写回，若做拷贝就会写进副本、原始 Cache 不被更新。

**练习 2**：`k_cache` 为空时造的占位张量 shape 是 `{0}`，为什么不直接传 nullptr？
**答案**：L0 算子登记接口的签名是固定的（所有输入按位置传入），下游 kernel 统一用 `kCacheGm` 访问；造空张量可以让「SFA 融合缓存」与「K/V 分离缓存」两种物理布局共用同一条调用链，kernel 内再用 `CacheLayout` 区分实际写到哪里。

**练习 3**：`index` 在 PA 模式与 PA_BLK 模式下的语义差别是什么？
**答案**：PA 模式下 index 是 token 级绝对位置（元素数 = B×S，kernel 内 `seqIndex * dk` 直接定位）；PA_BLK 模式下 index 是页编号（元素数 = B×页数），kernel 内还要除/模 blockSize 拆出「第几页 + 页内第几个 token」。

### 4.2 模块二：RoPE 与多模式 kernel 变体（b16/pa/nz/mtp/quant）

#### 4.2.1 概念说明

三个模式旋钮 × V1/V2 × MTP/B1SD × 是否量化，组合爆炸出十几种执行场景。仓库的做法是：**host 侧一棵 TilingKey 决策树**（ds_tiling 的 `DoOpTiling`）+ **device 侧一张分发表**（kernel 入口的 `TILING_KEY_IS` 链）+ **kernel 侧一组按场景命名的头文件**。这与 u4-l5 MoE 的「位段编码 TilingKey」是同一设计模式，但这里 key 是**手工分段分配**的：

- 1xxx：NORM（非分页）Cache；
- 2xxx：PA + CutB；
- 3xxx：B1SD/CutT（V2 为主）；
- 4xxx：NZ 家族（4xxx 十位上是量化标记）；
- 5xxx：BNSD 家族（5xxx 十位上是量化标记，5021 专门给 pertile128）；
- 个位 1（部分分支）表示 MTP 或量化类型；
- 10000/20000：arch35 专用（见模块三）。

#### 4.2.2 核心流程：TilingKey 决策树

```text
DoOpTiling()（ds_tiling.cpp:405-607）
  ├─ cacheMode == PA 且 quant != NONE      → key = 5001 + quantClass×10   (50X1)
  ├─ cacheMode == PA_BLK_BNSD  quant? →5010 / 5000
  ├─ cacheMode == PA_NZ         quant? →4011 / 4001
  ├─ cacheMode == PA_BLK_NZ     quant? →4010 / 4000
  ├─ outputKv 且 cacheMode == PA           → 5001
  └─ 其余（NORM / PA 且 outputKv=false）
       ├─ IsB1SD?（Skv == Srope） → isCutT_ = true
       ├─ isCutT_? → key = 3001(PA) / 3000|3010(NORM, 量化→3010)
       └─ 否则 CutB → key = 2000(PA) / 1000|1010(NORM)
            └─ !isCutT_ 且 isMTP_（Skv>1 且 Srope=1）→ key += 1
```

#### 4.2.3 源码精读

**（1）NZ 分支的 key 落点**。[ai_infra_kv_rms_norm_rope_cache_ds_tiling.cpp:469-487](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/posembedding/ai_infra_kv_rms_norm_rope_cache/op_host/ai_infra_kv_rms_norm_rope_cache_ds_tiling.cpp#L469-L487)：

```cpp
if (currentCacheMode_ == CacheMode::PA_NZ) {
    DoOpTilingPaBlkNz();
    if (quantMode_ != NON_QUANT_MODE) {
        tilingKey_ = TLING_KEY_4011;   // PA + NZ + 静态量化
    } else {
        tilingKey_ = TLING_KEY_4001;   // PA + NZ + 无量化
    }
    return ge::GRAPH_SUCCESS;
}
if (currentCacheMode_ == CacheMode::PA_BLK_NZ) { ... 4010 / 4000 ... }
```

注意量化判据 `quantMode_` 是**从输入张量反推**的：[ai_infra_kv_rms_norm_rope_cache_base_tiling.cpp:183-208](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/posembedding/ai_infra_kv_rms_norm_rope_cache/op_host/ai_infra_kv_rms_norm_rope_cache_base_tiling.cpp#L183-L208) 的 `GetComponentStaticQuantMode` 按「scale 张量是否存在 / offset 是否存在」返回 NONE / 对称 / 非对称——**属性字符串只是声明意图，真正的量化模式由张量在场与否决定**（`SetupQuantClass` 还会把「声明 static 但没给 scale」降级成 NONE，见 [ds_tiling.cpp:290-294](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/posembedding/ai_infra_kv_rms_norm_rope_cache/op_host/ai_infra_kv_rms_norm_rope_cache_ds_tiling.cpp#L290-L294)）。

**（2）量化类型拼进 key 的个十位**。[ds_tiling.cpp:397-403](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/posembedding/ai_infra_kv_rms_norm_rope_cache/op_host/ai_infra_kv_rms_norm_rope_cache_ds_tiling.cpp#L397-L403)：`TilingKeyAttachQuantStat(5001, quantClass)` = \( 5001 + \text{quantClass} \times 10 \)，即 none→5001、static→5011、pertile128→5021（5021 恰好与专门的 C8 kernel 呼应，见下）。

**（3）B1SD/MTP 的切分决策**。[ds_tiling.cpp:58-78](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/posembedding/ai_infra_kv_rms_norm_rope_cache/op_host/ai_infra_kv_rms_norm_rope_cache_ds_tiling.cpp#L58-L78) 的 `IsB1SD`：`Skv == Srope` 则 B、S 轴对齐，选 CutT（token×head 一维切分，注释称其为最高吞吐策略）；否则走 CutB（多 batch 共享 rope 系数），且仅此时 `Skv > 1` 才算 MTP。[ds_tiling.cpp:514-530](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/posembedding/ai_infra_kv_rms_norm_rope_cache/op_host/ai_infra_kv_rms_norm_rope_cache_ds_tiling.cpp#L514-L530) 给出 CutT 的分核：\( \text{blockFactor} = \lceil B \cdot N \cdot S / \text{coreNum} \rceil \)，`ubFactor` 按 UB 是否 ≥170KB 取 16/32（V2）或 1；[ds_tiling.cpp:603-606](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/posembedding/ai_infra_kv_rms_norm_rope_cache/op_host/ai_infra_kv_rms_norm_rope_cache_ds_tiling.cpp#L603-L606) 是 MTP 的 +1 修正。

**（4）决策打点**。[ds_tiling.cpp:549-554](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/posembedding/ai_infra_kv_rms_norm_rope_cache/op_host/ai_infra_kv_rms_norm_rope_cache_ds_tiling.cpp#L549-L554) 有一条 `OP_LOGD` 日志输出 `isPagedAttention_ / inputQuantMode_ / 静态量化三元组`——排查「为什么命中了错误 kernel」时先看这条日志。

**（5）device 侧分发表**。[ai_infra_kv_rms_norm_rope_cache.cpp:33-50](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/posembedding/ai_infra_kv_rms_norm_rope_cache/op_kernel/ai_infra_kv_rms_norm_rope_cache.cpp#L33-L50) 用 `#define` 把 18 个 key 命名成可读宏（`..._B16_NORM 1000`、`..._B16_PA_NZ_QUANT 4011`、`..._B16_PA_BNSD_C8 5021` 等），入口函数 [第 52-217 行](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/posembedding/ai_infra_kv_rms_norm_rope_cache/op_kernel/ai_infra_kv_rms_norm_rope_cache.cpp#L52-L217) 逐个 `TILING_KEY_IS` 匹配后实例化对应 kernel 类。例如 key 4011 落到 [第 148-156 行](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/posembedding/ai_infra_kv_rms_norm_rope_cache/op_kernel/ai_infra_kv_rms_norm_rope_cache.cpp#L148-L156)：

```cpp
} else if (TILING_KEY_IS(AI_INFRA_KV_RMS_NORM_ROPE_CACHE_B16_PA_NZ_QUANT)) {
    GET_TILING_DATA_WITH_STRUCT(AiInfraKvRmsNormRopeCacheTilingData, tiling_data_in, tiling);
    ...
    KernelAiInfraKvRmsNormRopeCacheB16PANZQUANT<true, DTYPE_KV, DTYPE_K_CACHE, DTYPE_CKV_CACHE> op(&pipe, tilingData);
```

**（6）一个头文件服务三个 key：scatterType 模板参数**。[ai_infra_kv_rms_norm_rope_cache_b16_pa.h:21-32](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/posembedding/ai_infra_kv_rms_norm_rope_cache/op_kernel/ai_infra_kv_rms_norm_rope_cache_b16_pa.h#L21-L32)：

```cpp
constexpr int64_t PA_NZ_NO_QUANT = 1;
constexpr int64_t PA_BLK_BNSD_NO_QUANT = 2;
constexpr int64_t PA_BNSD_NO_QUANT = 3;
template <bool isPagedAttention, typename KV_DTYPE, int64_t scatterType>
class KernelAiInfraKvRmsNormRopeCacheB16PA : ...
```

key 4001/5000/5001 分别以 `scatterType = 1/2/3` 实例化**同一个类**——散射地址计算不同、数学计算相同，用编译期参数避免运行期分支。

**（7）RoPE 的七步向量化实现**。[ai_infra_kv_rms_norm_rope_cache_b16_pa.h:549-641](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/posembedding/ai_infra_kv_rms_norm_rope_cache/op_kernel/ai_infra_kv_rms_norm_rope_cache_b16_pa.h#L549-L641)，模板参数 `rpMode` 区分两种取对方式：

- interleave 模式（[第 591-597 行](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/posembedding/ai_infra_kv_rms_norm_rope_cache/op_kernel/ai_infra_kv_rms_norm_rope_cache_b16_pa.h#L591-L597)）：用 `GatherMask` 以步长 1/2 抽出偶数位（real）与奇数位（imag）；
- half 模式（[第 599-607 行](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/posembedding/ai_infra_kv_rms_norm_rope_cache/op_kernel/ai_infra_kv_rms_norm_rope_cache_b16_pa.h#L599-L607)）：`DataCopy` 直接搬前半段/后半段。

随后（[第 616-631 行](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/posembedding/ai_infra_kv_rms_norm_rope_cache/op_kernel/ai_infra_kv_rms_norm_rope_cache_b16_pa.h#L616-L631)）：\( y_0 = [\text{real},\text{imag}] \odot \cos \)、\( y_1 = [-\text{imag},\text{real}] \odot \sin \)、\( y = y_0 + y_1 \)。以 interleave 为例，\( x=[x_0,x_1,\dots] \)，输出偶位 \( x_0\cos\theta - x_1\sin\theta \)、奇位 \( x_1\cos\theta + x_0\sin\theta \)——正是旋转矩阵 \( \begin{pmatrix}\cos\theta & -\sin\theta\\ \sin\theta & \cos\theta\end{pmatrix} \) 作用于 (x0,x1) 对。所有乘加先 `Cast` 到 fp32（[第 585-587 行](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/posembedding/ai_infra_kv_rms_norm_rope_cache/op_kernel/ai_infra_kv_rms_norm_rope_cache_b16_pa.h#L585-L587)），算完再压回 T（bf16 用 `CAST_RINT`、fp16 用 `CAST_NONE`，[第 634-639 行](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/posembedding/ai_infra_kv_rms_norm_rope_cache/op_kernel/ai_infra_kv_rms_norm_rope_cache_b16_pa.h#L634-L639)）。

**（8）运行期二级分派**。该 kernel 类的 `ProcessV1`（[ai_infra_kv_rms_norm_rope_cache_b16_pa.h:110-135](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/posembedding/ai_infra_kv_rms_norm_rope_cache/op_kernel/ai_infra_kv_rms_norm_rope_cache_b16_pa.h#L110-L135)）在编译期分支（rpMode/scatterType）之外，还按 TilingData 里的 `cacheLayOut`、`vStride` 运行期选出 `ProcessV1Impl<rpMode, clLayout, isSFACountiguous>` 的 8 种组合——TilingKey 管大场景，CacheLayout 管细分布局，两层正交。

#### 4.2.4 代码实践（本讲主实践）

**实践目标**：整理 kernel 变体命名规律表，并为「PageAttention + NZ 格式 + 静态量化」场景选出正确的 tiling 与 kernel 文件。

**第一步：填表**。对照 kernel 入口分发表（[ai_infra_kv_rms_norm_rope_cache.cpp:33-50](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/posembedding/ai_infra_kv_rms_norm_rope_cache/op_kernel/ai_infra_kv_rms_norm_rope_cache.cpp#L33-L50) 与 [第 88-217 行](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/posembedding/ai_infra_kv_rms_norm_rope_cache/op_kernel/ai_infra_kv_rms_norm_rope_cache.cpp#L88-L217)）完成下表（参考答案已给出，建议先自己填再核对）：

| TilingKey | 场景（cache/切分/量化） | kernel 类 | kernel 头文件 |
|---|---|---|---|
| 1000/1001 | NORM, CutB, 无量化（+1=MTP） | `...B16MTP<false>` | `ai_infra_kv_rms_norm_rope_cache_b16_mtp.h` |
| 1010/1011 | NORM, CutB, 静态量化（+1=MTP） | `...B16MTPQUANT<false>` | `..._b16_mtp_quant.h` |
| 2000/2001 | PA, CutB, 无量化（+1=MTP） | `...B16MTP<true>` | `..._b16_mtp.h` |
| 3000/3001 | CutT（B1SD）, NORM/PA, 无量化 | `...B16B1SD` | `..._b16_b1sd.h` |
| 3010 | V2, NORM, CutT, 静态量化 | `...B16BNSDQUANT<false>` | `..._b16_pa_bnsd_quant.h` |
| 4000 | PA_BLK_NZ, 无量化 | `...B16PABLKNZ<true>` | `..._b16_pa_blk_nz.h` |
| **4011** | **PA_NZ, 静态量化** | **`...B16PANZQUANT<true>`** | **`..._b16_pa_nz_quant.h`** |
| 4001 | PA_NZ, 无量化 | `...B16PA<true,,PA_NZ_NO_QUANT=1>` | `..._b16_pa.h` |
| 4010 | PA_BLK_NZ, 静态量化 | `...QuantB16PABLKNZ<true>` | `..._b16_pa_blk_nz_quant.h` |
| 5000 | PA_BLK_BNSD, 无量化 | `...B16PA<true,,PA_BLK_BNSD_NO_QUANT=2>` | `..._b16_pa.h` |
| 5001 | PA/PA_BNSD, 无量化（isOutputKv） | `...B16PA<true,,PA_BNSD_NO_QUANT=3>` | `..._b16_pa.h` |
| 5010 | PA_BLK_BNSD, 静态量化 | `...B16PABLKBNSDQUANT<true>` | `..._b16_pa_blk_bnsd_quant.h` |
| 5011 | PA, quantClass=STATIC（5001+1×10） | `...B16BNSDQUANT<true>` | `..._b16_pa_bnsd_quant.h` |
| 5021 | PA_BNSD, pertile128（C8） | `...B16PAC8<true>` | `..._b16_pa_bnsd_c8.h` |
| 10000 | arch35 regbase D 全载 | `AiInfraKvRmsNormRopeCacheRegbaseFullLoad` | `arch35/..._regbase_full_load.h` |
| 20000 | arch35 regbase 二分重算 | `...RegbaseRecompute` | `arch35/..._regbase_recompute.h` |

命名规律总结：`b16`（输入 fp16/bf16）→ `mtp`（NORM/CutB）| `b1sd`（CutT）| `pa[_blk]_[nz|bnsd][_quant|_c8]`（PA 家族 + 排布 + 量化），arch35 变体进 `arch35/` 子目录并以 `regbase_` 前缀命名。

**第二步：场景选型推导**（PA + NZ + 静态量化）：

1. 调用侧传 `cache_mode="PA_NZ"`、`rotary_mode` 任选、`quant_mode="static"`，且 `k_rope_scale`/`ckv_scale` 张量真实传入（否则会被降级为 NONE）；
2. host：`GetShapeAttrsInfo` 把字符串映射为 `CacheMode::PA_NZ`（[base_tiling.cpp:319-334](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/posembedding/ai_infra_kv_rms_norm_rope_cache/op_host/ai_infra_kv_rms_norm_rope_cache_base_tiling.cpp#L319-L334)），`GetQuantMode` 因 scale 在场返回 `QUANT_MODE`；
3. tiling：命中 [ds_tiling.cpp:469-477](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/posembedding/ai_infra_kv_rms_norm_rope_cache/op_host/ai_infra_kv_rms_norm_rope_cache_ds_tiling.cpp#L469-L477) 的 PA_NZ 分支 → **TilingKey = 4011**，tiling 实现文件为 `ai_infra_kv_rms_norm_rope_cache_ds_tiling.cpp`（切分计算在 `DoOpTilingPaBlkNz`，[第 377-395 行](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/posembedding/ai_infra_kv_rms_norm_rope_cache/op_host/ai_infra_kv_rms_norm_rope_cache_ds_tiling.cpp#L377-L395)），TilingData 结构为 `AiInfraKvRmsNormRopeCacheTilingData`（[tiling.h:25-49](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/posembedding/ai_infra_kv_rms_norm_rope_cache/op_host/ai_infra_kv_rms_norm_rope_cache_tiling.h#L25-L49)）；
4. device：key 4011 → **kernel 头文件 `ai_infra_kv_rms_norm_rope_cache_b16_pa_nz_quant.h`**，类 `KernelAiInfraKvRmsNormRopeCacheB16PANZQUANT<true, DTYPE_KV, DTYPE_K_CACHE, DTYPE_CKV_CACHE>`（入口 [kernel cpp:148-156](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/posembedding/ai_infra_kv_rms_norm_rope_cache/op_kernel/ai_infra_kv_rms_norm_rope_cache.cpp#L148-L156)）；
5. 附加约束：PA_NZ 模式下 sin 的 D 维必须按 cache dtype 对齐——fp16 要求 D 是 16 的倍数、int8 要求 32 的倍数（[base_tiling.cpp:126-135](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/posembedding/ai_infra_kv_rms_norm_rope_cache/op_host/ai_infra_kv_rms_norm_rope_cache_base_tiling.cpp#L126-L135)），本场景 Dk=64 满足。

**第三步（可选，有硬件时验证）**：在 UT 测试 [test_ai_infra_kv_rms_norm_rope_cache_tiling.cpp](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/posembedding/ai_infra_kv_rms_norm_rope_cache/tests/ut/op_host/test_ai_infra_kv_rms_norm_rope_cache_tiling.cpp) 里找一个用例名含 `5011` 的测试（用例名直接把期望 TilingKey 编进名字，如 `..._static_quant_contiguous_default_5011_pangu_000000`），仿照它写一个 `PA_NZ + static` 用例并断言 TilingKey 为 4011；用 `bash build.sh -u --ophost` 运行。**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：为什么 key 5021（pertile128）不和 5010/5011 一样走静态量化通道？
**答案**：pertile128 是**动态**量化——scale 要在 kernel 里按每 128 个元素现算（`RmsNormDynamicQuantPertile128*VF` 系列），需要额外输出 scale 的 buffer 与 `dynamicScaleAlign` 字段，计算结构与静态量化完全不同，所以单独给了 `..._b16_pa_bnsd_c8.h` 与 key 5021。

**练习 2**：`isOutputKv` 在什么条件下会被强制置 0？
**答案**：[ds_tiling.cpp:556-559](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/posembedding/ai_infra_kv_rms_norm_rope_cache/op_host/ai_infra_kv_rms_norm_rope_cache_ds_tiling.cpp#L556-L559)：非分页且无量化时输出布局与 Cache 写入内容一致，单独再输出一份 kv 纯属浪费，故强制 `set_isOutputKv(0)`；量化或布局变换时输出才有意义。

**练习 3**：host 与 kernel 两侧的 TilingKey 数值靠什么保持一致？
**答案**：没有自动机制——host 用 `TLING_KEY_4011` 等常量（ds_tiling.cpp:40-56），device 用 `#define ..._B16_PA_NZ_QUANT 4011`（kernel 入口 cpp:33-50），两侧硬编码镜像，靠 UT 用例名里的 key 断言兜底（同 u4-l5 MoE 的做法）。

### 4.3 模块三：arch 特化——regbase D 全载与二分重算

#### 4.3.1 概念说明

Ascend 950PR/DT（代码里称 arch35/regbase，`__CCE_AICORE__ == 310`、`__NPU_ARCH__ == 3510`）向量寄存器更宽（VL 变长），适合把整行 D 维一次装进 UB 直接算（「D 全载」）。但通用模板（DS）把 V1/V2 的 D 固定成 576/320 几个魔法数（`RMS_NORM_LENGTHS`、`D_LENGTH` 等校验，[ds_tiling.cpp:435-449](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/posembedding/ai_infra_kv_rms_norm_rope_cache/op_host/ai_infra_kv_rms_norm_rope_cache_ds_tiling.cpp#L435-L449)），而 950 场景 D 可变——因此 950 走**另一套 tiling 模板与 kernel**，D 不限死、按 UB 预算现算。

#### 4.3.2 核心流程：模板轮询与两级兜底

回顾 u2-l3：`TilingRegistry` 按注册优先级轮询 tiling 模板，`IsCapable()` 决定本模板是否接单，`DoOpTiling` 返回 `GRAPH_PARAM_INVALID` 表示「接了单但干不了」让下一个模板继续。本算子注册了三个模板（[tiling.h:123-125](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/posembedding/ai_infra_kv_rms_norm_rope_cache/op_host/ai_infra_kv_rms_norm_rope_cache_tiling.h#L123-L125)）：

```text
优先级 1000  DsTiling          IsCapable = !isRegbase_   → 910B/910_93 接单
优先级 2000  RegbaseFullLoad   IsCapable = isRegbase_    → 950 接单；D 装不进 UB 时
                                    返回 GRAPH_PARAM_INVALID 交棒 ↓
优先级 3000  RegbaseRecompute  IsCapable = isRegbase_    → 950 兜底（大 D）
```

关键在 **UB 预算公式**（full_load_tiling.cpp:376-379）：

\[ \text{ubFactor} = \frac{\text{ubSize} - 1024 - S_{\text{scale}} - V_{\text{scale}} - S_\gamma - S_{\text{rope}}}{2 \cdot S_{\text{in}} + 2 \cdot S_{\cos\sin} + 2 \cdot S_{\text{out}} + S_{\text{rms}}} \]

分母是各类 buffer（双缓冲 ×2）的字节数；若结果 ≤0 说明「D 全载」放不下，返回 `GRAPH_PARAM_INVALID`，轮询落到 Recompute 模板——它把 RMSNorm 的平方和累加拆成**二分折叠**（TilingData 字段 `basicFoldCount`/`mainFoldCount`，用 `FindNearestPower2` 找折叠点，[tiling.h:109-116](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/posembedding/ai_infra_kv_rms_norm_rope_cache/op_host/ai_infra_kv_rms_norm_rope_cache_tiling.h#L109-L116)），分段累加再合并，牺牲重复计算换 D 不设上限。

#### 4.3.3 源码精读

**（1）`isRegbase_` 的探测**。[ai_infra_kv_rms_norm_rope_cache_base_tiling.cpp:240-251](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/posembedding/ai_infra_kv_rms_norm_rope_cache/op_host/ai_infra_kv_rms_norm_rope_cache_base_tiling.cpp#L240-L251)：读平台 `GetSocVersion()`，在 `#ifdef USE_ASCEND950` 编译开关内判断 `ASCEND950` 才置 `isRegbase_ = true`——**编译期宏 + 运行期 SOC 双重门槛**，与 build.sh 按 `-c` 出不同 SOC 包呼应（u1-l2）。

**（2）两侧 IsCapable 互斥**。[ds_tiling.cpp:226-229](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/posembedding/ai_infra_kv_rms_norm_rope_cache/op_host/ai_infra_kv_rms_norm_rope_cache_ds_tiling.cpp#L226-L229) 返回 `!isRegbase_`；[regbase_full_load_tiling.cpp:45-48](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/posembedding/ai_infra_kv_rms_norm_rope_cache/op_host/arch35/ai_infra_kv_rms_norm_rope_cache_regbase_full_load_tiling.cpp#L45-L48) 与 [regbase_recompute_tiling.cpp:49-52](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/posembedding/ai_infra_kv_rms_norm_rope_cache/op_host/arch35/ai_infra_kv_rms_norm_rope_cache_regbase_recompute_tiling.cpp#L49-L52) 返回 `isRegbase_`。

**（3）注册与 key 落账**。[regbase_full_load_tiling.cpp:400-412](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/posembedding/ai_infra_kv_rms_norm_rope_cache/op_host/arch35/ai_infra_kv_rms_norm_rope_cache_regbase_full_load_tiling.cpp#L400-L412) 的 `PostTiling` 把 `SetBlockDim(usedCoreNum_)`（实际用核数，可能少于物理核）并落 key=10000；[regbase_recompute_tiling.cpp:301](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/posembedding/ai_infra_kv_rms_norm_rope_cache/op_host/arch35/ai_infra_kv_rms_norm_rope_cache_regbase_recompute_tiling.cpp#L301) 落 key=20000；注册宏分别在 [full_load_tiling.cpp:414-415](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/posembedding/ai_infra_kv_rms_norm_rope_cache/op_host/arch35/ai_infra_kv_rms_norm_rope_cache_regbase_full_load_tiling.cpp#L414-L415)、[recompute_tiling.cpp:319](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/posembedding/ai_infra_kv_rms_norm_rope_cache/op_host/arch35/ai_infra_kv_rms_norm_rope_cache_regbase_recompute_tiling.cpp#L319)。

**（4）TilingData 按 key 分家**。三套 TilingData 用**带后缀的算子名**注册：`AiInfraKvRmsNormRopeCache`（DS）/ `AiInfraKvRmsNormRopeCache_10000` / `AiInfraKvRmsNormRopeCache_20000`（[tiling.h:49](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/posembedding/ai_infra_kv_rms_norm_rope_cache/op_host/ai_infra_kv_rms_norm_rope_cache_tiling.h#L49)、[L90](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/posembedding/ai_infra_kv_rms_norm_rope_cache/op_host/ai_infra_kv_rms_norm_rope_cache_tiling.h#L90)、[L121](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/posembedding/ai_infra_kv_rms_norm_rope_cache/op_host/ai_infra_kv_rms_norm_rope_cache_tiling.h#L121)）——device 侧 `GET_TILING_DATA_WITH_STRUCT(AiInfraKvRmsNormRopeCacheRegbaseFullLoadTilingData, ...)` 靠这个名字找到正确的结构体反序列化布局，防止 host/device 字段错位。

**（5）kernel 入口的 arch 编译开关**。[ai_infra_kv_rms_norm_rope_cache.cpp:27-30](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/posembedding/ai_infra_kv_rms_norm_rope_cache/op_kernel/ai_infra_kv_rms_norm_rope_cache.cpp#L27-L30) 只在 `__CCE_AICORE__ == 310` 时 include arch35 头文件；[第 58-86 行](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/posembedding/ai_infra_kv_rms_norm_rope_cache/op_kernel/ai_infra_kv_rms_norm_rope_cache.cpp#L58-L86) 的分支里只认 10000/20000 两个 key。注意开头还做了 **浮点溢出模式寄存器的保存/清零/恢复**（`GetCtrlSpr`/`SetCtrlSpr<FLOAT_OVERFLOW_MODE_CTRL>`，仅 `__NPU_ARCH__ == 3510`）——arch35 特有的 kernel 级全局开关管理，退出时必须还原，否则污染同核后续 kernel。

**（6）regbase kernel 的 NZ 散射**。[ai_infra_kv_rms_norm_rope_cache_regbase_full_load.h:33-56](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/posembedding/ai_infra_kv_rms_norm_rope_cache/op_kernel/arch35/ai_infra_kv_rms_norm_rope_cache_regbase_full_load.h#L33-L56) 把 D 维拆成 \( dk_0 = 32/\text{sizeof}(T) \)（fp16 即 16）与 \( dk_1 = dk / dk_0 \)；[第 156-197 行](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/posembedding/ai_infra_kv_rms_norm_rope_cache/op_kernel/arch35/ai_infra_kv_rms_norm_rope_cache_regbase_full_load.h#L156-L197) 的 `ScatterUpdateK`：`seqIndex` 负值直接 `continue`（跳过无效 token），PA_NZ 模式下地址按 \( (\text{pageId} \cdot dk_1 \cdot \text{blockSize} + \text{tokenInPage}) \cdot dk_0 \) 计算——NZ 排布里 **D 维的每个 16 元素小块按页内 token 连续存放**，正好喂给 Cube 核的 fractal 加载（与 u4-l1 讲的 NZ/BSND 布局动机一致）。输出侧用 `copyOutKParamsNz`：`blockCount=dk1, blockLen=dk0×字节, dstStride=(blockSize-1)×dk0×字节`，一次 DataCopyPad 完成「逻辑一行 → NZ 物理多段」的跳写。

**（7）regbase 的 Process 主循环**。[ai_infra_kv_rms_norm_rope_cache_regbase_full_load.h:795-909](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/posembedding/ai_infra_kv_rms_norm_rope_cache/op_kernel/arch35/ai_infra_kv_rms_norm_rope_cache_regbase_full_load.h#L795-L909)：先搬 gamma、静态量化的 scale/offset（标量则 `Duplicate` 广播成向量），循环内依次搬 rope 段与 cos/sin（`cosSinNeedBrc` 决定逐 token 广播还是整块搬）→ `Rope(...)`（带实部/虚部四象限分离的 real/img cos/sin 布局）→ 散射写 kCache → 再搬 rms 段做 `RmsNorm*`（按 kQuantMode/vQuantMode/isOutputKv 选 `RmsNormSymQuantVF`/`RmsNormDynamicQuantPertile128*` 等向量化变体，[第 780-793 行](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/posembedding/ai_infra_kv_rms_norm_rope_cache/op_kernel/arch35/ai_infra_kv_rms_norm_rope_cache_regbase_full_load.h#L780-L793)）。

#### 4.3.4 代码实践

**实践目标**：用纯 CPU 的 UT 框架验证「950 走 regbase 模板、910B 走 DS 模板」的分流。

**操作步骤**（源码阅读型，无需硬件）：

1. 打开 [test_ai_infra_kv_rms_norm_rope_cache_tiling.cpp](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/posembedding/ai_infra_kv_rms_norm_rope_cache/tests/ut/op_host/test_ai_infra_kv_rms_norm_rope_cache_tiling.cpp)，找一个用例名含 `_5011_` 的测试（文件开头约第 35 行即是）；
2. 阅读它的 `gert::TilingContextPara` 构造：第二个参数是算子名，compileInfo 里塞了 `coreNum/ubSize`（对应 `AiInfraKvRmsNormRopeCacheCompileInfo`，[tiling.h:127-130](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/posembedding/ai_infra_kv_rms_norm_rope_cache/op_host/ai_infra_kv_rms_norm_rope_cache_tiling.h#L127-L130)）——faker 环境里 `GetPlatformInfo()` 返回空，tiling 走 compileInfo 分支（[base_tiling.cpp:219-232](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/posembedding/ai_infra_kv_rms_norm_rope_cache/op_host/ai_infra_kv_rms_norm_rope_cache_base_tiling.cpp#L219-L232)，u2-l3 讲过的双来路）；
3. 回答：faker 里 `GetSocVersion()` 拿不到 ASCEND950，`isRegbase_` 恒为 false，所以 UT 只能覆盖 DS 模板；要测 regbase 需要 faker 支持伪造 SOC 版本。

**需要观察的现象**：用例断言的期望 TilingKey（写在用例名与期望数据里）与 4.2.2 决策树逐条对应。

**预期结果**：能说清「UT 为什么测不到 10000/20000 分支」。如需实际运行：`bash build.sh -u --ophost`（u6-l1 详述）。**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：FullLoad 模板 `DoOpTiling` 里 `return ge::GRAPH_PARAM_INVALID` 与 `return ge::GRAPH_FAILED` 的后果有何不同？
**答案**：`GRAPH_PARAM_INVALID` 是三态中的「本模板不支持」，注册表会继续轮询下一个模板（Recompute 接手）；`GRAPH_FAILED` 是硬失败，整个 tiling 直接报错终止。D 全载放不下属于前者（[full_load_tiling.cpp:381-384](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/posembedding/ai_infra_kv_rms_norm_rope_cache/op_host/arch35/ai_infra_kv_rms_norm_rope_cache_regbase_full_load_tiling.cpp#L381-L384)），所以大 D 有兜底而形状非法没有。

**练习 2**：为什么 arch35 kernel 入口要先备份再清零浮点溢出模式寄存器？
**答案**：RMSNorm 平方和可能超出 fp16/bf16 表示范围，清零溢出模式（关饱和/上溢行为）由 kernel 自行用 fp32 累加规避精度问题；该寄存器是核级全局状态，不恢复会污染后续 kernel 的数值行为。

**练习 3**：`SetBlockDim` 在 DS 与 FullLoad 两个模板里分别传什么？
**答案**：DS 传 `tilingData_.get_numBlocks()`（CutB 的 batch 块数或 CutT 的 token 块数）；FullLoad 传 `usedCoreNum_` = \( \lceil bs / \text{blockFactor} \rceil \)，即按实际工作量算出的用核数，可能小于物理核数。

### 4.4 模块四：rotate half——独立的 rotary_mul 算子

#### 4.4.1 概念说明

`ai_infra_rotary_position_embedding`（torch 侧名字 `npu_ai_infra_rotary_mul`）只做标准 rotate-half RoPE：\( y = x \odot \cos + \operatorname{concat}(-x_2, x_1) \odot \sin \)。它与 kv_rms_norm_rope_cache 的关系是「通用件 vs 专用件」：前者服务任意 3/4 维张量、任意可广播 cos/sin，支持 fp16/bf16/fp32 与五种 layout；后者把 RoPE 融进写 Cache 的流水线里。注意它的 `mode` 属性（int，默认 0）对应 `RotaryPosEmbeddingMode` 枚举（HALF=0/INTERLEAVE=1/QUARTER=2/DEEPSEEK_INTERLEAVE=3，[ai_infra_rotary_position_embedding_tiling.h:94-99](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/posembedding/ai_infra_rotary_position_embedding/op_host/ai_infra_rotary_position_embedding_tiling.h#L94-L99)），本仓库只实现了 rotate_half 一个 tiling 模板。

#### 4.4.2 核心流程：三步数学原语

kernel 把公式拆成三个可复用原语（均在 `ai_infra_rotate_half_base.h`）：

```text
XNewCopy(x → xNew)：xNew 左半 = x 右半，xNew 右半 = x 左半   （换半）
SinCompute(sin)：  sin 左半逐元素 ×(-1)                       （取负）
ComputeInner：     y = xNew； y = x⊙cos + xNew⊙sin            （乘加）
```

三者组合正好等价于 \( y_l = x_l\cos_l - x_r\sin_l,\; y_r = x_r\cos_r + x_l\sin_r \)。

#### 4.4.3 源码精读

**（1）tiling 模板的 SOC 白名单**。[ai_infra_rope_rotate_half_tiling.h:29-38](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/posembedding/ai_infra_rotary_position_embedding/op_host/ai_infra_rope_rotate_half_tiling.h#L29-L38)：`IsCapable` 要求 `inputMode_ != MODE_ROTATE_INTERLEAVED`（即 mode 属性不是 interleave）**且** SOC 在 {ASCEND910B, ASCEND910_93} 白名单内——注意与 arch35 相反，这个算子**不**支持 950（def 里也只 `AddConfig` 了 910b/910_93，[ai_infra_rotary_position_embedding_def.cpp:57-58](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/posembedding/ai_infra_rotary_position_embedding/op_host/ai_infra_rotary_position_embedding_def.cpp#L57-L58)）。模板以优先级 50000 注册（[ai_infra_rotary_position_embedding_tiling.cpp:67](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/posembedding/ai_infra_rotary_position_embedding/op_host/ai_infra_rotary_position_embedding_tiling.cpp#L67)）——五位数优先级与 u4-l1 FIA 的「位数编码体系」一致，为未来更多 rotary 模板预留区间。

**（2）kernel 入口的 key 编码**。[ai_infra_rotary_position_embedding.cpp:22-65](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/posembedding/ai_infra_rotary_position_embedding/op_kernel/ai_infra_rotary_position_embedding.cpp#L22-L65)：key = 前缀 + tilingMode×权 + dtype 尾数，尾数 1/2/3 对应 FP32/FP16/BF16；10xx 走常规 `RotateHalf`/`RotateHalfBf16`，11xx 走 `RotateHalfRopeFullLoadXd`（XD 维全载加速路径，由 `tailAxesFLBoost` 标记开启）。

**（3）五路 layout 分派**。[ai_infra_rotate_half.h:74-85](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/posembedding/ai_infra_rotary_position_embedding/op_kernel/ai_infra_rotate_half.h#L74-L85)：BNSD/BSND/SBND/无广播走 `NormalProcess`，R_B1SD（cos/sin 只有 B、S、D 三维而 x 是 B、N、S、D）走 `RB1sdProcess`，BND 走 `BndProcess`——**同一份三原语，搬运寻址按 layout 换**。`BndProcess`（[第 126-151 行](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/posembedding/ai_infra_rotary_position_embedding/op_kernel/ai_infra_rotate_half.h#L126-L151)）先搬一份 cos/sin，`Muls(sinLocal, sinLocal, -1.0, halfDPadLength)` 只对前半取负，再 `RBroadCast` 广播到多行——又一次印证「sin 只取负前半」。

**（4）三原语实现**。[ai_infra_rotate_half_base.h:344-353](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/posembedding/ai_infra_rotary_position_embedding/op_kernel/ai_infra_rotate_half_base.h#L344-L353) 的 `XNewCopy` 用两条 `DataCopy`（stride = 半行块数）完成换半；[第 259-288 行](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/posembedding/ai_infra_rotary_position_embedding/op_kernel/ai_infra_rotate_half_base.h#L259-L288) 的 `SinCompute` 按repeat分块做 `Muls(…, -1.0)`（注释 `sin_l = -1 * sin_l`）；[第 290-298 行](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/posembedding/ai_infra_rotary_position_embedding/op_kernel/ai_infra_rotate_half_base.h#L290-L298) 的 `ComputeInner` 就三条向量指令：`Mul(x,x,cos)`、`Mul(xNew,xNew,sin)`、`Add(xNew,xNew,x)`。对比 4.2.3 第 (7) 点：**kv 算子里的 RoPE 要先 GatherMask/DataCopy 拆实虚部再乘加（interleave/half 两种取对），rotary_mul 因为输入本身就是 rotate-half 约定，两条 DataCopy 换半即可**——同一数学，两种输入约定的两种实现。

**（5）对齐与尾部处理**。[ai_infra_rotate_half.h:243-291](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/posembedding/ai_infra_rotary_position_embedding/op_kernel/ai_infra_rotate_half.h#L243-L291)，其(https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/posembedding/ai_infra_rotary_position_embedding/op_kernel/ai_infra_rotate_half.h#L243-L291) 的 `CopyInX`：D/2 是 32B 块对齐倍数时用 `DataCopy` 直搬，否则退化为两条 `DataCopyPad`（前半/后半各一条，`dstStride` 留出对齐 padding）——`isAligned` 标志贯穿所有搬运函数，TilingData 里对应的 `halfDPadLength`/`dPadLength` 字段就是为这条对齐分支准备的。

#### 4.4.4 代码实践

**实践目标**：用 PyTorch 参考实现验证 rotate-half 公式与 kernel 语义一致（纯阅读 + 推导，无硬件也可完成）。

**操作步骤**：

1. 阅读 [docs/npu_ai_infra_rotary_mul.md](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/posembedding/ai_infra_rotary_position_embedding/docs/npu_ai_infra_rotary_mul.md) 的三步公式；
2. 对照 `XNewCopy`（换半）+ `SinCompute`（前半取负）+ `ComputeInner`（乘加），手推 8 元素例子 \( x=[1,2,3,4,5,6,7,8] \)、\( \cos,\sin \) 任取，验证两种途径结果一致；
3. 有环境时用 ST 测试 [test_ai_infra_rotary_position_embedding.py](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/posembedding/ai_infra_rotary_position_embedding/tests/st/test_ai_infra_rotary_position_embedding.py) 对拍：

```python
# 示例代码：NPU 算子 vs CPU 参考（需昇腾环境）
import torch, torch_npu, omni_custom_ops
x  = torch.randn(2, 4, 16, dtype=torch.float16).npu()
cos = torch.randn(2, 1, 16, dtype=torch.float16).npu()   # 可广播
sin = torch.randn(2, 1, 16, dtype=torch.float16).npu()
y_npu = torch.ops.custom.npu_ai_infra_rotary_mul(x, cos, sin, rotary_mode='half')
# CPU 参考
x1, x2 = x.float().chunk(2, dim=-1)
cos_f, sin_f = cos.float().expand_as(x.float()), sin.float().expand_as(x.float())
y_ref = (torch.cat([-x2, x1], dim=-1) * sin_f + x.float() * cos_f).half()
assert torch.allclose(y_npu.cpu().float(), y_ref.float(), atol=1e-2, rtol=1e-2)
```

**需要观察的现象**：CPU 参考的 `torch.cat([-x2, x1], -1) * sin` 与 kernel 的「xNew=换半(x)，sin 前半取负」在数学上逐元素相等。

**预期结果**：手推两者恒等；上机对拍通过。**待本地验证**（无硬件时完成手推即可）。

#### 4.4.5 小练习与答案

**练习 1**：`rotary_mul` 与 kv 算子内嵌 RoPE 的 `rotary_mode="half"` 语义完全一样吗？
**答案**：数学输出一样（都是 rotate-half），但取对约定不同：`rotary_mul` 的输入 x 本身按「前半/后半」组织，两条 DataCopy 换半即可；kv 算子还支持 `interleave-half`（偶奇交错），需要 `GatherMask` 抽取。另外 `rotary_mul` 的 mode 枚举里还有 QUARTER/DEEPSEEK_INTERLEAVE，但仓库当前只注册了 half 的 tiling 模板。

**练习 2**：为什么 `BndProcess` 里 cos/sin 只搬一次？
**答案**：BND layout 下 cos/sin 与 batch 维广播（每个 batch 共用同一份 D 维系数），循环外搬一次并广播到 UB 多行，循环内只搬 x、做乘加、写出，节省重复的 GM→UB 带宽——与 kv 算子 MTP「多 batch 共享 rope 系数」是同一优化思想。

**练习 3**：这个算子的 tiling 模板优先级是 50000，DS 模板是 1000，数值大小代表什么？
**答案**：注册表按优先级从小到大轮询（u5-l1 详述），数值本身只是排队次序；50000 是给 rotary 模板族预留的独立区间，避免与其他算子的模板冲突（每个算子有自己的注册表，这里主要是可读性与扩展语义）。

## 5. 综合实践

**任务：给「950 上 PA_NZ + 静态量化」场景写一份完整的执行路径说明书。**

假设部署环境是 Ascend 950PR，调用参数 `cache_mode="PA_NZ"`、`quant_mode="static"`、`k_rope_scale/ckv_scale` 在场、V1 输入（kv 尾轴 576）。请沿调用链回答并成文：

1. **aclnn 层**：哪些输入会被 `Contiguous`、哪些走 `CreateView`？cache 的合法 dtype 集合里 950 会用哪个（提示：静态量化 Cache 通常 INT8，见 [aclnn cpp:33-35](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/posembedding/ai_infra_kv_rms_norm_rope_cache/op_api/aclnn_ai_infra_kv_rms_norm_rope_cache.cpp#L33-L35)）？
2. **tiling 层**：`isRegbase_` 取值？轮询顺序中哪个模板接单？接单后 TilingKey 是多少（提示：regbase 模板不再走 ds 的 4xxx 决策，见 [full_load_tiling.cpp:396](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/inference/ascendc/src/ops-transformer/posembedding/ai_infra_kv_rms_norm_rope_cache/op_host/arch35/ai_infra_kv_rms_norm_rope_cache_regbase_full_load_tiling.cpp#L396)）？D 全载若放不下会怎样？
3. **kernel 层**：入口函数走 `#if` 的哪个分支？kernel 类名与头文件是什么？NZ 散射地址公式中 \( dk_0 \)、\( dk_1 \) 取值（INT8 cache）？溢出模式寄存器在进入/退出时各做什么？
4. **验证**：把你的结论与 4.2.4 表格中 4011 行对比，说明「同一个 cache_mode + quant_mode 组合，在 910B 与 950 上命中的 TilingKey、TilingData 结构、kernel 文件为何完全不同」。

**参考要点**（做完再对）：950 上 `isRegbase_=true` → DS `IsCapable` 为假 → FullLoad（或 Recompute）接单 → key=10000（或 20000）→ kernel 走 arch35 分支的 `AiInfraKvRmsNormRopeCacheRegbaseFullLoad`（`arch35/ai_infra_kv_rms_norm_rope_cache_regbase_full_load.h`）；INT8 时 \( dk_0 = 32/1 = 32 \)、\( dk_1 = 64/32 = 2 \)；量化模式不再进 TilingKey，而是作为 TilingData 字段（`kQuantMode/vQuantMode`）在 kernel 内运行期分派——这正是「通用模板把模式编进 key、regbase 模板把模式放进 TilingData 字段」的架构差异。

## 6. 本讲小结

- **一个算子四件事**：`kv_rms_norm_rope_cache` 融合 RMSNorm、RoPE、量化、ScatterUpdate 写 Cache；原地 Cache 目标在 aclnn 层用 `CreateView` 保留 stride，k_cache 缺省时以 `{0}` 占位张量统一签名。
- **模式旋钮驱动变体矩阵**：cache_mode（5 种）× rotary_mode（2 种）× quant_mode（3 种）× V1/V2 × MTP/B1SD 在 host 侧被编成 18 个手工分段的 TilingKey，在 device 侧由 `TILING_KEY_IS` 链分发给 9 个 kernel 头文件；`b16_pa.h` 用 `scatterType` 模板参数让一份代码服务三个 key。
- **声明与实证分离**：属性字符串只是意图，量化模式真正由 scale/offset 张量在场与否反推，「声明 static 但没给张量」会被降级为 none。
- **arch35 特化是第二套世界**：`isRegbase_`（编译宏 USE_ASCEND950 + 运行期 SOC）互斥地切换 tiling 模板族；D 全载按 UB 预算公式算 ubFactor，放不下返回 `GRAPH_PARAM_INVALID` 交棒给二分重算模板（key 10000/20000），TilingData 用带后缀的算子名防字段错位；kernel 还要管理 3510 独有的浮点溢出模式寄存器。
- **同一数学、两种实现**：rotate-half 的 \( y = x\cos + \operatorname{concat}(-x_2,x_1)\sin \) 在 rotary_mul 里是「换半 + 前半取负 + 乘加」三原语（半约定输入），在 kv 算子里是「GatherMask/DataCopy 拆实虚 + 七步乘加」（还支持 interleave 约定）。
- **两侧硬编码镜像**：TilingKey 数值在 host（常量）与 device（宏）各写一份，靠 UT 用例名中的 key 断言兜底——修改任何一侧必须同步另一侧。

## 7. 下一步学习建议

- **u4-l8（ai_infra_matmul）**：看 ops-nn 侧另一套 common 基建（tiling 缓存、simplified key、runtime_kb.json），与本文的「TilingData 带后缀注册」对照，理解不同算子族对多 SOC 的两种工程化解法。
- **u5-l1（公共 Tiling 框架深入）**：本讲的「优先级 1000/2000/3000 轮询 + 三态返回」只是预热，下一讲系统拆解 `TilingBaseClass` 的模板注册表、key 体系与 tiling 缓存。
- **u5-l2（AIV/AIC 协同）**：本讲全部是 AIV 向量核；kv_rms_norm_rope_cache 产出的 NZ 格式 Cache 正是给 Cube 核 fractal 消费的，可在那一讲回头看 NZ 排布的「消费端」。
- 想动手的读者：仿照 4.2.4 的表格方法，自己去盘 `ai_infra_fused_infer_attention_sink` 的 TilingKey 编码（u4-l1 已给出框架），检验是否真正掌握了「决策树 + 分发表 + 命名规律」三件套。

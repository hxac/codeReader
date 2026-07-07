# 量化格式与张量族

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 ds4 用到的四种量化块（block）格式——`q8_0`、`q8_K`、`q2_K`、`q4_K`、`iq2_xxs`——各自的字节布局与「每值位数」。
- 区分「写端」（离线量化工具 `gguf-tools/quants.c`）和「读端」（运行时 `ds4.c`）为什么必须共享同一套字节布局。
- 读懂 `ds4.c` 中 `ds4_vec_dot_*_q8_K` 这一族点积函数的「量化权重 × Q8_K 激活」参考实现思路。
- 解释「为什么只有 routed MoE 专家被压缩，而 shared 专家、投影、路由保持高精度」这一非对称策略在源码里是如何被强制约束的。
- 回答本讲的核心问题：**为什么 2bit 量化（IQ2_XXS）需要 imatrix（列重要性），而 q4_K 不一定需要。**

本讲承接 u3-l2 的「权重绑定」。u3-l2 讲的是「张量按名字填进语义指针表」，本讲往下钻一层：这些张量的**字节本身**是按什么格式排列的，推理时又如何把它们还原成数学上的向量点积。

## 2. 前置知识

在进入源码前，先用三段话建立直觉。

**为什么要量化。** 大模型权重原始精度通常是 FP16/BF16（每值 16 bit）。DeepSeek V4 Flash 有 284B 总参，若全用 FP16 存储需要上百 GB，普通机器塞不下。量化（quantization）就是把一组连续浮点数，用一个「缩放因子 + 少数几个比特的整数码」来近似表示，从而把每值平均位数从 16 压到 2～5 bit，体积压缩数倍。代价是引入「量化误差」——还原出的值和原始值有偏差。

**块（block）量化。** 不是给每个数单独存一个 scale（那样太费空间），而是把连续的若干个值（叫一个 block，ds4 里通常是 256 个，记为 `QK_K`）打包成一块：块内共享或分级共享少量 scale/min 系数，主体是低比特整数码。这样平均每值的元数据开销很小。本讲涉及的所有 K-quant 格式块大小都是 256。

**点积（dot product / vec_dot）是量化的用武之地。** Transformer 前向里最密集的运算是「权重矩阵 × 激活向量」。若权重已量化成低比特码 `q`，激活也量化成 8bit 整数 `q8`，那么点积

\[ s = \sum_i w_i \cdot x_i \]

就可以**几乎完全用整数乘加**完成，最后再乘回 scale 即可。整数乘加在 CPU/GPU 上远比浮点快、且省电。GGML 的经典套路是：把激活预先量化成 `Q8_K`（一次性），然后让同一份激活和很多行量化权重做点积时复用。本讲的 `vec_dot` 函数做的就是这件事。

一个最关键的术语：**imatrix（重要性矩阵）**。它不是模型权重，而是一组「每一列有多重要」的统计值，由真实推理时该层输入激活的平方和 `sum(x[column]^2)` 累积得到（见 `gguf-tools/imatrix/README.md`）。量化器拿它当**误差加权**：重要列要量化得更准，不重要列可以随便压。我们会在 4.1 详细看到为什么这对 2bit 是刚需。

## 3. 本讲源码地图

本讲横跨两个目录，务必先建立「写端 vs 读端」的对应关系：

| 文件 | 角色 | 本讲关注点 |
|------|------|-----------|
| `ds4.c` | **读端**（运行时推理引擎） | 量化块的 C 结构体定义、`vec_dot_*_q8_K` 点积函数、routed 专家类型校验 |
| `gguf-tools/quants.c` | **写端**（离线量化工具） | 各格式的「traits 表」（块大小/类型大小/是否可量化/是否需要 imatrix）、量化算法实现 |
| `gguf-tools/quants.h` | 写端头文件 | 量化类型枚举 `ds4q_type` 与公开 API 声明（含 `ds4q_requires_imatrix`） |
| `gguf-tools/deepseek4-quantize.c` | 写端入口 | imatrix 加载、imatrix 缺失时的「合成兜底」 |
| `gguf-tools/imatrix/README.md` | 文档 | imatrix 是什么、怎么收集、为什么 2bit 离不开它 |

一句话记忆：**`quants.c` 负责「把 float 数组压成字节」，`ds4.c` 负责「把那些字节还原成点积」**。两端必须对字节布局达成共识——所以你会看到两边都在用 `84 / 144 / 292 / 66` 这几个相同的尺寸常量。

## 4. 核心概念与源码讲解

### 4.1 量化 block 结构：四种格式的字节布局

#### 4.1.1 概念说明

ds4 实际「读」的量化格式只有四种，`ds4.c` 文件头一段注释把它们和 MoE 的对应关系直接点明：

> - Q2_K routed down experts
> - Q4_K routed experts in the high-memory variant
> - IQ2_XXS routed gate/up experts
> - Q8_K temporary activation blocks for dot products

也就是说：**模型权重**用 Q2_K / Q4_K / IQ2_XXS 三种低比特格式（且只用在 routed 专家上），**激活向量**临时用 Q8_K。`q8_0` 是写端 `quants.c` 里也实现了的更简单的 8bit 格式（块大小 32），但运行时点积统一走 256 宽的 Q8_K。

每种格式都对应一个固定大小的 C 结构体（一个 block）。衡量压缩率的指标是「每值平均位数」：

\[ \text{bits/value} = \frac{\text{type\_size} \times 8}{\text{block\_size}} \]

#### 4.1.2 核心流程

把一段 256 个 float 压成一个 block，再由 vec_dot 还原，其抽象流程是：

1. **量化（写端，离线一次）**：输入 256 个 float → 找到块内极值/分组的 scale 与 min → 把每个值映射成低比特整数码 → 按固定字节布局写成一个 block。
2. **存储**：GGUF 文件里整张权重张量就是 `行数 × (列数/256) × type_size` 字节的连续 block 数组。
3. **反量化点积（读端，每次推理）**：读一个权重 block + 一个 Q8_K 激活 block → 解码出每组的 scale/min/码 → 用整数乘加算点积 → 乘回全局 scale 得到 float 结果。

四种格式的对照表（尺寸来自 `ds4.c` 的结构体与静态断言，(bits/value) 由上式算出）：

| 格式 | block 大小 | type_size (字节) | bits/value | 用途 | 是否需要 imatrix |
|------|-----------|-----------------|------------|------|-----------------|
| `q8_K` | 256 | 292 | 9.125 | 激活（临时） | 否 |
| `q4_K` | 256 | 144 | 4.5 | routed 专家（高内存档） | 否（可选） |
| `q2_K` | 256 | 84 | 2.625 | routed down 专家 | 否（可选） |
| `iq2_xxs` | 256 | 66 | 2.0625 | routed gate/up 专家 | **是** |
| `q8_0` | 32 | 34 | 8.5 | （写端实现，运行时不用） | 否 |

> 注：`q8_K` 的 9.125 bit/value 看起来比 8 还大，因为它额外存了每 16 个 int8 的分组求和 `bsums`（用于加速 min 校正项），是「为速度优化过的激活格式」，不是用来压缩权重的。

#### 4.1.3 源码精读

**读端的四个 block 结构体**（带静态断言锁死字节大小）：

[ds4.c:344-375](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L344-L375) 定义了 `QK_K=256` 以及四个结构体，并用 `DS4_STATIC_ASSERT` 在编译期保证 `sizeof` 等于 84/144/292/66。这几行是「读端对字节布局的契约」。我们逐个看：

```c
#define QK_K 256

typedef struct {
    uint8_t  scales[QK_K / 16];   // 16 字节：每 16 值一组的 scale(低4bit)+min(高4bit)
    uint8_t  qs[QK_K / 4];        // 64 字节：每字节打包 4 个 2bit 码
    uint16_t d;                    // 块全局 scale (f16)
    uint16_t dmin;                 // 块全局 min  (f16)
} block_q2_K;                      // 共 84 字节
```

[ds4.c:346-351](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L346-L351) — `block_q2_K`。256 个值被切成 16 组（每组 16 值），每组共用一个 4bit scale 和一个 4bit min（拼在一个字节里），外加块级 f16 的 `d`/`dmin` 做二次缩放。码本身只有 2bit（`qs[64]`，每字节塞 4 个码）。

```c
typedef struct {
    uint16_t d;          // 块全局 scale (f16)
    uint16_t dmin;       // 块全局 min  (f16)
    uint8_t  scales[12]; // 8 组的 scale+min，6bit 编码，紧凑打包
    uint8_t  qs[QK_K/2]; // 128 字节：每字节打包 2 个 4bit 码
} block_q4_K;            // 共 144 字节
```

[ds4.c:353-358](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L353-L358) — `block_q4_K`。256 值切成 8 组（每组 32 值），码 4bit。`scales[12]` 用了一种「6bit scale + 6bit min」的紧凑编码（见 4.2 的 `q4_k_get_scale_min` 解包）。

```c
typedef struct {
    float   d;                // 块 scale (f32)
    int8_t  qs[QK_K];         // 256 个 int8 码
    int16_t bsums[QK_K / 16]; // 16 个分组求和，加速 min 校正
} block_q8_K;                 // 共 292 字节
```

[ds4.c:360-364](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L360-L364) — `block_q8_K`，激活专用。注意它**带符号**（`int8_t qs`）且预存了 `bsums`（每 16 个码的整数和），这是给 K-quant 点积里 `dmin*summs` 那一项加速用的。

```c
typedef struct {
    uint16_t d;            // 块 scale (f16)
    uint16_t qs[QK_K / 8]; // 32 个 uint16：打包了网格索引 + 符号位 + 分组 scale
} block_iq2_xxs;           // 共 66 字节
```

[ds4.c:366-369](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L366-L369) — `block_iq2_xxs`，最极端的 2bit 格式。它不是「每值一个 2bit 码」那么简单，而是把 8 个值一组，用一个「查表（grid lookup）+ 符号掩码」的方式编码——见 4.2.3。

**写端的 traits 表**（与读端结构体尺寸必须一致）：

[gguf-tools/quants.c:39-74](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/gguf-tools/quants.c#L39-L74) — `ds4q_type_traits` 数组，每项是 `{name, block_size, type_size, can_quantize, requires_imatrix}`。注意：

- 只有 `Q8_0 / Q2_K / Q4_K / IQ2_XXS` 四项的 `can_quantize=true`（写端只能产出这四种）。
- **`requires_imatrix=true` 的只有 `IQ2_XXS / IQ2_XS / IQ1_S` 三种最低比特格式**，Q2_K 和 Q4_K 都是 `false`。这正是本讲核心问题的源头。

[gguf-tools/quants.c:1039-1042](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/gguf-tools/quants.c#L1039-L1042) 把 `requires_imatrix` 暴露成查询 API：

```c
bool ds4q_requires_imatrix(ds4q_type type) {
    if (type < 0 || type >= DS4Q_TYPE_COUNT) return false;
    return ds4q_type_traits[type].requires_imatrix;
}
```

[gguf-tools/quants.h:18-54](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/gguf-tools/quants.h#L18-L54) — 枚举 `ds4q_type` 的数值**故意与 GGUF/GGML 类型 ID 对齐**（如 IQ2_XXS=16、Q2_K=10、Q4_K=12），这样模板 GGUF 的元数据可以直接拷贝不用翻译。这也解释了为什么 `ds4.c` 里到处用魔法数字 `16`/`10` 校验专家类型——它们就是这套枚举值。

#### 4.1.4 代码实践

**实践目标**：亲手核对「写端 traits 表」与「读端结构体」的尺寸是否一致，并算出每个格式的 bits/value。

**操作步骤**：

1. 打开 `gguf-tools/quants.c:39-74`，记下 `Q2_K / Q4_K / IQ2_XXS / Q8_K / Q8_0` 各自的 `(block_size, type_size)`。
2. 打开 `ds4.c:372-375`，读四个 `DS4_STATIC_ASSERT`，确认 `sizeof(block_q2_K)==84`、`sizeof(block_q4_K)==144`、`sizeof(block_q8_K)==292`、`sizeof(block_iq2_xxs)==66`。
3. 用公式 bits/value = type_size × 8 / block_size 填表。

**需要观察的现象**：写端的 `type_size` 应当与读端 `sizeof` 完全相等——若不等，vec_dot 读出来的码就是错位的，结果全错。这正是文件头注释「byte layout compatibility is more important here than generality」的含义（见 `gguf-tools/quants.c:9-12`）。

**预期结果**：你应该得到 q2_K≈2.625、q4_K=4.5、iq2_xxs≈2.0625、q8_K≈9.125、q8_0=8.5（bit/value）。换句话说，IQ2_XXS 比 Q4_K 省 ~2.2 倍空间，比 FP16 省约 7.8 倍。

> 待本地验证：若你手头有 ds4 的 GGUF，可用 `ls -l` 量出某张 routed 专家张量的字节数，除以「行数×列数/256」反推 type_size，与本表对照。

#### 4.1.5 小练习与答案

**练习 1**：`block_q2_K` 里 `qs` 字段是 `QK_K/4 = 64` 字节，为什么是除以 4？

**参考答案**：每个值占 2 bit，8 bit/字节 ÷ 2 bit/值 = 4 个值塞进一个字节，所以 256 个值需要 256/4 = 64 字节。

**练习 2**：为什么 `block_q8_K` 要额外存 `bsums[16]`，而 `block_q4_K` 不存？

**参考答案**：Q8_K 是**激活**格式，一份激活要和成百上千行权重做点积，预存分组求和能让 K-quant 点积里的 min 校正项 `dmin*summs` 直接用，省掉重复求和；而 Q4_K 是**权重**格式，每行只参与少量点积，存 bsums 不划算，让 vec_dot 现场算即可。

---

### 4.2 dot product 参考实现：量化权重 × Q8_K 激活

#### 4.2.1 概念说明

四种格式的点积函数都遵循同一个 GGML 模板，名字都是 `ds4_vec_dot_<权重组态>_q8_K`：

- 先把当前 token 的 float 激活**一次性**量化成 Q8_K（`ds4_quantize_row_q8_K`）。
- 然后这份 Q8_K 激活被**复用**去和很多行量化权重做点积（例如一个专家的几百行权重）。
- 点积内部：整数乘加算出 `isum`，再乘回 scale（`d`/`dmin`）得到 float。

每个 vec_dot 函数都有两条路径：带 `__ARM_FEATURE_DOTPROD` 的 NEON 向量化快路径，和一个可移植的标量 `#else` 参考路径。**本节只读标量参考路径**——它最能讲清数学，也方便移植到 CUDA/Metal 时做对照基准（这正是 u5 会讲的事）。

#### 4.2.2 核心流程

通用点积的数学形式（以 K-quant 仿射量化为例）：

权重块把每个值近似成

\[ \hat{w}_i = d \cdot s_j \cdot q_i - d_{\min} \cdot m_j \]

其中 \(j\) 是该值所属的分组，\(s_j/m_j\) 是组级 scale/min，\(d/d_{\min}\) 是块级 scale/min，\(q_i\) 是低比特码。于是与激活 \(x_i\) 的点积：

\[ \text{dot} = d \cdot \underbrace{\sum_j s_j \sum_{i\in j} q_i x_i}_{\text{整数乘加 isum}} \;-\; d_{\min} \cdot \underbrace{\sum_j m_j \sum_{i\in j} x_i}_{\text{用 Q8_K 的 bsums 加速}} \]

两个求和项都可以在整数域完成，最后一次性乘回 float 的 `d`/`dmin`。这就是所有 `vec_dot_*_q8_K` 标量路径的统一骨架。

**先看激活怎么变成 Q8_K：**

[ds4.c:2584-2622](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L2584-L2622) — `ds4_quantize_row_q8_K`。关键三步：

```c
const float iscale = -127.0f / max;        // 对称量化，max 是绝对值最大的（带符号）值
for (int j = 0; j < QK_K; j++)
    y[b].qs[j] = clamp(round(iscale * x[j]), -128, 127);  // 编码
for (int j = 0; j < QK_K/16; j++)
    y[b].bsums[j] = sum(qs[j*16 .. j*16+15]);             // 预算分组求和
y[b].d = 1.0f / iscale;                                    // 反量化 scale
```

`iscale` 用 `-127/max` 是为了让「最大幅值的那一项」精确映射到 -127（落在 int8 对称范围内），符号在后面 `d*qs` 时自然抵消。`bsums` 直接服务 min 校正项。

#### 4.2.3 源码精读

**Q2_K 点积（标量参考路径）：**

[ds4.c:2704-2741](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L2704-L2741) — `ds4_vec_dot_q2_K_q8_K` 的 `#else` 分支。核心：

```c
const float dall = y[i].d * f16_to_f32(x[i].d);
const float dmin = y[i].d * f16_to_f32(x[i].dmin);
int summs = 0;
for (int j = 0; j < 16; j++)
    summs += y[i].bsums[j] * (sc[j] >> 4);   // min 校正项：用 bsums，省一次求和
int isum = 0;
for (...) {
    int d = sc[is++] & 0x0f;                 // 该组 4bit scale
    isum += d * dot_q2_16(q2, q8, shift);    // 解包 2bit 码做 16 整数乘加
}
sumf += dall * (float)isum - dmin * (float)summs;
```

注意 `sc[j]>>4` 取的是该组 4bit min，乘上 Q8_K 预算好的 `bsums[j]`——这正是练习 2 答案的落点。`dot_q2_16`（[ds4.c:560](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L560)）负责把一个字节里的 4 个 2bit 码按 `shift` 解包出来再和 16 个 int8 激活做点积。

**Q4_K 点积（标量参考路径）：**

[ds4.c:2807-2843](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L2807-L2843) — `ds4_vec_dot_q4_K_q8_K` 的 `#else` 分支。结构与 Q2_K 同构，差别在解包：4bit 码每字节塞 2 个，用 `(qs[byte_off+l] >> shift) & 0xF` 取出（`shift` 是 0 或 4）。组级 scale/min 通过 `q4_k_get_scale_min`（[ds4.c:2744-2752](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L2744-L2752)）从那 12 字节紧凑编码里解出。

**IQ2_XXS 点积（标量参考路径）：**

[ds4.c:2902-2932](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L2902-L2932) — `ds4_vec_dot_iq2_xxs_q8_K` 的 `#else` 分支。这是最不一样的一个，它不是「直接取码」，而是**查表**：

```c
const uint32_t ls = 2 * (aux32[1] >> 28) + 1;          // 该 32 值组的 scale
for (int l = 0; l < 4; l += 2) {
    const uint32_t sign_idx = (aux32[1] >> (7*l)) & 127;
    sumi += dot_iq2_pair_16(iq2xxs_signed_grid[aux8[l]][sign_idx],   // 查表得 8 个 int8
                            iq2xxs_signed_grid[aux8[l+1]][sign_idx2],
                            q8);
}
bsum += sumi * (int32_t)ls;
...
*s = 0.125f * sumf;   // 最后的固定缩放（成对变体是 0.25）
```

`aux8[l]` 是网格索引，`iq2xxs_signed_grid[索引][符号]` 直接查出 8 个带符号 int8 值——也就是说，IQ2_XXS 的「2bit 码」并不直接是数值，而是一个**预计算查表**的键，真正用的量化值来自一张固定的码本网格。这种「码本量化」正是它能把每值压到 ~2bit 还能用的关键，也是它**必须**靠 imatrix 来选好码本的原因（见 4.1 与综合实践）。

> NEON 快路径（如 [ds4.c:2627-2703](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L2627-L2703)）数学完全等价，只是用 `vdotq_s32` 等指令把整数乘加向量化。GPU 后端（u5）则在 Metal/CUDA/ROCm 内核里重写同一套数学。

#### 4.2.4 代码实践

**实践目标**：用一条「读源码」的线索，确认四个 vec_dot 都遵循「整数乘加 + 最后乘回 scale」的同一骨架。

**操作步骤**：

1. 在 `ds4.c` 中分别打开 `ds4_vec_dot_q2_K_q8_K`（2624 行起）、`ds4_vec_dot_q4_K_q8_K`（2754 行起）、`ds4_vec_dot_iq2_xxs_q8_K`（2846 行起）的 `#else` 标量分支。
2. 在每个函数末尾找到那行 `sumf += ...` 或 `*s = ... * sumf`，确认它们都是「(某 scale) × (整数 isum) −/＋ (某 min scale) × (整数 summs)」的形式。

**需要观察的现象**：三个函数的收尾公式结构高度相似——先在 int 域累加，再乘 float scale。这告诉你：**新增一种量化格式，只要保证它能写成「整数点积 + 线性 scale」的形式，就能套进这套 matvec 框架**。

**预期结果**：你会看到 Q2_K/Q4_K 用 `dall*isum - dmin*summs`（带 min 校正），IQ2_XXS 用 `d*bsum` 后再 `*0.125`（码本量化，无单独 min 项）。三种格式的差异全在「码怎么解、scale 怎么分层」，乘加骨架不变。

#### 4.2.5 小练习与答案

**练习 1**：为什么激活要预先量化成 Q8_K，而不是直接用 float 激活和量化权重做点积？

**参考答案**：一份激活要被同层许多行权重（一个专家几百行、多个专家）复用。预先量化成 int8 后，大量点积都变成纯整数乘加，比浮点快得多也省电；而权重是静态的，量化一次存盘即可。这是「以一次激活量化换千万次整数乘加」的划算买卖。

**练习 2**：`ds4_vec_dot_iq2_xxs_q8_K` 标量路径末尾为什么是 `*s = 0.125f * sumf`，而成对版本 `ds4_vec_dot_iq2_xxs_pair_q8_K` 是 `0.25f`？

**参考答案**：成对版本一次处理两个权重块但共用一份激活，其 NEON 路径的中间累加方式与单块版不同，固定缩放系数（0.25 vs 0.125）是把码本网格的固定增益吸收进最后一步的结果；标量回退时成对版直接调用两次单块版（见 [ds4.c:3011-3012](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L3011-L3012)），所以两个单块结果各自已是 0.125 缩放后的正确值，无需再乘。

---

### 4.3 量化与 MoE：为什么只有 routed 专家被压缩

#### 4.3.1 概念说明

回顾 u3-l2：一层 transformer 的 FFN 部分由「shared 专家 + 若干 routed 专家」组成。DeepSeek V4 有 256（Flash）/384（PRO）个 routed 专家，每 token 只激活其中少数几个。**routed 专家占了模型体积的绝大部分**，但每次推理只用得到一小撮——这是「压得起也压得值」的理想量化对象。

ds4 的非对称策略（见 README 与 u1-l2）：

- **routed 专家**：压到 2bit（gate/up 用 IQ2_XXS，down 用 Q2_K）或 4bit（Q4_K，高内存档）。
- **shared 专家、所有投影、路由权重、output head**：保持高精度（F16/Q8），不动。

为什么这么切？因为 routed 专家总量大、单次激活少，压它收益最大、对每次推理的精度冲击却被「路由稀疏」稀释；而投影、shared 专家每次推理必经，压了会无差别伤所有 token。

#### 4.3.2 核心流程

这条策略在源码里是被**硬约束**的，不是软建议：

1. **类型枚举白名单**：`tensor_is_routed_expert_type` 只允许 `IQ2_XXS / Q2_K / Q4_K` 三种类型作为 routed 专家。
2. **绑定期校验**：权重绑定时，routed 专家张量必须命中白名单，否则 `ds4_die` 退出。
3. **kernel 分流**：不同量化类型走不同的 matvec 函数，运行时再次断言类型符合预期。

#### 4.3.3 源码精读

**类型白名单：**

[ds4.c:3227-3231](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L3227-L3231) —

```c
static bool tensor_is_routed_expert_type(uint32_t type) {
    return type == DS4_TENSOR_IQ2_XXS ||
           type == DS4_TENSOR_Q2_K ||
           type == DS4_TENSOR_Q4_K;
}
```

这三个枚举值定义在 [ds4.c:1589-1597](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L1589-L1597)（`DS4_TENSOR_IQ2_XXS=16`、`DS4_TENSOR_Q2_K=10`、`DS4_TENSOR_Q4_K=12`，与写端 `ds4q_type` 对齐）。

**绑定期强校验：**

[ds4.c:3415-3430](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L3415-L3430) — `tensor_expect_routed_expert`：若某张量被当作 routed 专家绑定，但类型不在白名单，直接报错 `expected a routed expert quant type` 并 `exit(1)`。这是把「routed 专家必须量化、且只能用这三种格式」焊死在加载阶段。

**kernel 分流时的二次断言**（运行时，防御性）：每个 matvec 入口都复查类型。例如：

- gate/up 专家要求 IQ2_XXS：[ds4.c:5581](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L5581) 与 [ds4.c:5653](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L5653)（`if (w0->type != 16 ...) ds4_die("expected IQ2_XXS expert tensors")`）。
- down 专家要求 Q2_K：[ds4.c:5713](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L5713) 与 [ds4.c:5767](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L5767)（`if (w->type != 10) ds4_die("expected a Q2_K expert tensor")`）。
- 高内存档 Q4_K 专家：[ds4.c:6004](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L6004) 与 [ds4.c:6072](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L6072)。

注意 4.1.3 提到的「混合精度（boosted）GGUF」注释 [ds4.c:3337-3343](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L3337-L3343)：少数层的 routed 专家会被「升档」到更大格式（例如在 IQ2 层里混几层 Q4_K），这时那些升档层无法走统一的 SSD 流式专家缓存（slab 分配器只认一种 size class），必须从模型映射视图读取。这是量化策略影响 u9 SSD 流式设计的直接证据。

**README 的官方表述：**

[README.md:99-103](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/README.md#L99-L103) 明确写：「only the routed MoE experts are quantized, up/gate at `IQ2_XXS`, down at `Q2_K` ... the other components (shared experts, projections, routing) are left untouched to guarantee quality.」——与本节源码约束一一对应。

#### 4.3.4 代码实践

**实践目标**：把「量化类型 ↔ MoE 张量角色」的对应关系在源码里走一遍，确认它是被强制而非建议。

**操作步骤**：

1. 读 [ds4.c:3227-3231](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L3227-L3231)，列出允许的 routed 专家类型。
2. 跳到 [ds4.c:5713](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L5713) 与 [ds4.c:5581](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L5581)，看 down 专家断言 `type==10`（Q2_K）、gate/up 专家断言 `type==16`（IQ2_XXS）。
3. 设想：如果有人把 shared 专家也量化成 IQ2_XXS，会在哪一步被挡住？

**需要观察的现象**：你会看到「绑定期 + 运行时」两道断言把 routed 专家的量化类型锁死，而 shared/投影张量没有这类 IQ2/Q2/Q4 断言——后者天然走高精度路径。

**预期结果**：结论是 ds4 的「只压 routed 专家」不是配置项，而是写死在加载与 kernel 分发里的契约。任何不符合的 GGUF 都会在 `ds4_engine_open` 阶段 `die`。

> 待本地验证：用 `--inspect`（u3-l1 讲过的「只加载不推理」模式）加载一个非标准量化混搭的 GGUF，观察它是否在绑定校验阶段就退出。

#### 4.3.5 小练习与答案

**练习 1**：假设 shared 专家也用 Q2_K 存储，从「每次推理都必经」的角度说明为什么 ds4 不这么做。

**参考答案**：shared 专家每个 token 都会被激活并参与计算，它的量化误差会无差别污染**所有** token 的输出；而 routed 专家每 token 只激活少数几个，误差被路由稀疏性稀释。把误差预算花在「总量大但单次少用」的 routed 专家上性价比最高。

**练习 2**：`tensor_is_routed_expert_type` 为什么不把 `Q8_K` 也列进去？

**参考答案**：Q8_K 在 ds4 里是**激活**的临时格式（block_q8_K 存的是 int8 激活码 + bsums），不是权重存储格式。权重侧的「高精度」用的是 F16/F32 或 Q8_0，而非 Q8_K。把 Q8_K 排除在白名单外，等于声明「它不是合法的专家权重存储类型」。

---

## 5. 综合实践

**任务**：用本讲三块知识（block 结构、vec_dot、量化↔MoE）回答本讲的核心问题——**为什么 2bit 量化（IQ2_XXS）需要 imatrix，而 Q4_K 不一定需要**——并用源码与文档证据组织你的答案。

请按以下步骤完成：

1. **看「需要」是如何被代码定义的**。读 [gguf-tools/quants.c:39-74](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/gguf-tools/quants.c#L39-L74) 的 traits 表，确认 `requires_imatrix` 只有 IQ2_XXS/IQ2_XS/IQ1_S 为 true，Q2_K/Q4_K 为 false。结论：这里的「需要」=「缺失时量化器会主动合成一个兜底」，而不是「可以用」。

2. **看「合成兜底」长什么样**。读 [gguf-tools/deepseek4-quantize.c:1117-1128](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/gguf-tools/deepseek4-quantize.c#L1117-L1128)：

   ```c
   if (!im_ptr && ds4q_requires_imatrix(type)) {
       synthetic = xcalloc(ncols, sizeof(float));
       for (r ...) for (c ...)
           synthetic[c] += row[c] * row[c];   // 列平方和 = 列能量
       im_ptr = synthetic;
   }
   ```
   即「没有真 imatrix 时，用每列元素平方和」当列重要性。这印证 imatrix 的本质就是「列重要性权重」。

3. **看 imatrix 在量化里怎么当权重用**。读 [gguf-tools/quants.c:822-895](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/gguf-tools/quants.c#L822-L895) 的 `ds4q_write_iq2_xxs_block`，注意 `weight[i] = qw[i] * sqrtf(sigma2 + xb[i]^2)`，imatrix（`qw`）和「局部能量」相乘作为**量化误差的加权**，去搜索最优的码本索引。再看 [gguf-tools/quants.c:434-464](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/gguf-tools/quants.c#L434-L464) 的 `ds4q_write_q4_k_block_weighted`：Q4_K **也能**吃 imatrix（`quant_weights` 非空时走 weighted 分支），但它**不强制**——`quant_weights` 为 NULL 时走 `ds4q_write_q4_k_block_ref`（[gguf-tools/quants.c:369-432](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/gguf-tools/quants.c#L369-L432)）照样能量化。

4. **读官方解释收尾**。读 [gguf-tools/imatrix/README.md:148-151](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/gguf-tools/imatrix/README.md#L148-L151)：「For Q4, the imatrix ... changes how quantization error is weighted ... For Q2, it replaces the previous synthetic weight-energy fallback used for `IQ2_XXS` gate/up experts with real activation statistics.」

**你要给出的答案要点**（可用作自检）：

- IQ2_XXS 每值只有 ~2bit，且是「码本查表」式量化（4.2.3），可表示的码字极少。若均匀对待每一列，稀缺的码字会浪费在不重要列上，重要列反而失真严重。imatrix 提供「列重要性」，让量化器在搜索码本时对重要列的误差施以高权重，把宝贵的码字分配给真正影响输出的列。所以 IQ2_XXS **离不开**列重要性；没有真 imatrix 时量化器宁可用「列平方和」合成一个，也不能裸量化。
- Q4_K 每值 4bit，且是「分组 scale + min」的仿射量化（4.1.3），码字多、自带组级自适应缩放，本身就有足够分辨率，**没有** imatrix 也能量化得不错（走 `_ref` 路径）；有 imatrix 时它会作为可选加权进一步降误差（走 `_weighted` 路径，README 实测相对 NLL 降约 1.95%）。
- 一句话：**比特越少、码本越受约束，越依赖重要性先验；比特越多、量化模型越具自适应，imatrix 越是锦上添花而非雪中送炭。**

> 待本地验证：若有 Q4 GGUF 与对应 imatrix，可用 `gguf-tools/quality-testing/`（u11-l2 会讲）对比「有/无 imatrix」两种 Q4 的官方续写 NLL，验证 imatrix 对 Q4 的增益是「可选小幅」而非「必需」。

## 6. 本讲小结

- ds4 运行时只读四种量化格式：权重侧 `Q2_K`（84B/256，down 专家）、`Q4_K`（144B/256，高内存档专家）、`IQ2_XXS`（66B/256，gate/up 专家），激活侧 `Q8_K`（292B/256，临时）。尺寸由 `ds4.c:372-375` 的静态断言锁死。
- 写端 `gguf-tools/quants.c` 的 `ds4q_type_traits` 表与读端结构体**必须字节对齐**；枚举值与 GGUF 类型 ID 故意一致，便于元数据直传。
- 所有 `ds4_vec_dot_*_q8_K` 都遵循「激活预量化成 Q8_K → 整数乘加 → 乘回 scale」的同一骨架；标量参考路径讲清数学，NEON 路径与 GPU 内核（u5）只是把同一数学向量化。
- 「只压缩 routed MoE 专家」是 ds4 的非对称策略，被 `tensor_is_routed_expert_type` 白名单 + 绑定/运行时双重断言**强制**执行，而非软配置。
- **IQ2_XXS 因每值仅 ~2bit 且用码本查表，必须靠 imatrix（列重要性）指导码本搜索；Q4_K 有 4bit + 分组自适应仿射量化，imatrix 是可选增益而非必需**——这是 `requires_imatrix` 标志只有最低比特格式为 true 的根本原因。

## 7. 下一步学习建议

- **进入推理内核**：本讲只讲了「单行点积」。下一讲 u4-l1（DeepSeek V4 架构总览）会把这些点积拼成一次完整的「hidden → attention → router → routed+shared experts → 输出」前向，你会看到 Q8_K 激活如何被多个 routed 专家复用（即 4.3 提到的 matvec 批处理与成对点积 `ds4_vec_dot_iq2_xxs_pair_q8_K`）。
- **看 GPU 实现**：标量参考路径之外，同一套数学在 `metal/*.metal`、`ds4_cuda.cu`、`ds4_rocm.cu` 里被重写（u5），值得对照本讲的 `#else` 分支去读。
- **量化工具链**：想动手生成 GGUF 或比较 imatrix 增益，直接进 u11-l1（GGUF 生成与量化工具）与 u11-l2（imatrix 收集与质量测试），它们是本讲写端的完整工作流。
- **测试向量**：u11-l3 会讲 ds4 如何用 golden 向量捕捉「量化格式或 vec_dot 实现漂移」——本讲的字节布局与点积数学正是那些向量要守护的对象。

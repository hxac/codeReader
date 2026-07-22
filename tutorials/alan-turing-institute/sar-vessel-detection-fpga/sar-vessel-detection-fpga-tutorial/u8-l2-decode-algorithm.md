# 解码算法实现

## 1. 本讲目标

上一篇讲义（u8-l1）我们看懂了 `decode_kernel` 的「门面」：函数签名、`ap_uint<64>` 打包、`m_axi`/`s_axilite` 接口、`gmem0`/`gmem1` 两条通道。本讲走进内核的「大脑」——主循环 `ANCHOR_LOOP` 真正做了什么计算。

读完本讲你应该能：

1. 说出每个 anchor 的 67 个 `int8` 值在内存里是怎么排列的（4 段距离 ×16 bin + 3 个类别）。
2. 用位运算（`>> 3`、`& 0x7`、`>> (byte_idx*8)`）从一个 64 位字里取回任意一个 `int8` 特征。
3. 解释为什么用「整数阈值 `conf_thresh_inverse = 21`」就能提前剔除绝大多数背景 anchor，而不需要算 sigmoid。
4. 写出 4 个距离分支 ×16-bin softmax 的 DFL 期望值公式，并由 anchor 中心 + 距离还原出 `box_cx / box_cy / box_w / box_h`。

本讲只讲**算法**，不讲 HLS 优化指令（UNROLL/PIPELINE/ARRAY_PARTITION 留给 u8-l3），也不讲接口（u8-l1 已覆盖）。

## 2. 前置知识

### 2.1 YOLOv8 的 anchor-free + DFL 解码（一句话版）

YOLOv8 是 **anchor-free** 的：检测头在每个网格（grid cell）放一个「参考点」（anchor point，位于格子中心），网络不去预测框的绝对坐标，而是预测**这个参考点到框的四条边**的距离。为了让网络对小位移更敏感，每条边的距离不是直接回归一个数，而是回归一个 **16 个 bin 上的概率分布**，再用分布的期望值当最终距离——这就是 **DFL（Distribution Focal Loss）**。

所以一个 anchor 要被解码成框，需要：

1. 4 条边（左/上/右/下）各算一次 16-bin softmax 期望值 → 得到 4 个浮点距离。
2. 用「参考点 ± 距离」拼出框的左上角和右下角。
3. 看这 3 个类别（非船/船/渔船）的置信度是否过阈值，决定要不要把这个框输出。

本讲的 `decode_kernel` 就是在硬件上把这三步做完。

### 2.2 每层网格尺寸与 stride

不同检测头分辨率不同，本项目 4 个头的参数（与 testbench 完全一致）：

| 头 | `layer_size`（网格边长） | `layer_stride` | 对应 800 输入 |
|----|--------------------------|----------------|--------------|
| P2 | 200 | 4.0 | 200×4 = 800 |
| P3 | 100 | 8.0 | 100×8 = 800 |
| P4 | 50  | 16.0 | 50×16 = 800 |
| P5 | 25  | 32.0 | 25×32 = 800 |

`layer_size` 决定该层有多少个 anchor（`sizeOut = layer_size²`），`layer_stride` 把网格坐标换算回输入图像像素坐标。内核被 host 逐层调用，每调用一次处理一层（见 testbench 的 `for (layer ...)` 循环）。

### 2.3 int8 量化与 det_scale

DPU 输出的特征图是 **int8 定点**。要还原成原来的浮点 logit，需要乘一个固定的反量化缩放 `det_scale = 0.1`（见源码第 55 行）。也就是说，硬件读到的 `int8` 值 `q` 对应的浮点 logit 是 `q * 0.1`。

> 关键术语：**anchor-free**、**DFL**、**det_scale（反量化缩放）**、**softmax 期望值**。这些会在下面反复出现。

## 3. 本讲源码地图

本讲只涉及一个文件，但它是本单元的核心：

| 文件 | 作用 |
|------|------|
| [platform/post_processing/decode_krnl/decode_kernel.cpp](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/post_processing/decode_krnl/decode_kernel.cpp) | HLS 解码内核：把 DPU 的 int8 特征图解码成 NMS 前的候选框。本讲精读其算法部分。 |
| [platform/post_processing/decode_krnl/test_bench.cpp](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/post_processing/decode_krnl/test_bench.cpp) | C++ testbench：构造 4 层打包输入、逐层调内核。本讲用它验证常量。 |

本讲聚焦 `decode_kernel.cpp` 的算法主体（约 L52–L210），优化指令（L98、L101、L118、L132 等）留给 u8-l3。

## 4. 核心概念与源码讲解

先把整个算法的骨架画出来，再逐模块拆。内核顶层做完阈值常量预算后，进入对每个 anchor 的主循环：

```
预算：  conf_thresh_inverse = floor(-log(1/0.9 - 1) / 0.1)   // = 21
对每个 anchor n（n = 0 .. layer_size²-1）：
    ① 按 byte 取出 3 个类别 logit（存在 int8 数组里）
    ② 整数比较：若 3 个类别 logit 都 ≤ 21  → continue（跳过本 anchor）
    ③ 否则算 4 个距离分支的 16-bin softmax 期望值 → distances[0..3]
    ④ 由 anchor 中心 + distances 还原 box_cx/cy/w/h
    ⑤ 对每个过阈值的类别，写一条候选框（含 score=sigmoid(logit*0.1)）
全部 anchor 处理完，把本地数组拷回 AXI 输出数组
```

注意 ② 是「提前剔除」：在花算力的 softmax ③ **之前**就用一次整数比较把绝大多数背景 anchor 扔掉，这是整个内核最重要的优化。

下面把骨架拆成三个最小模块：**按字节提取 int8**、**整型阈值剔除**、**16-bin 距离 softmax 与边框还原**。

### 4.1 64 位字按字节提取 int8

#### 4.1.1 概念说明

上一篇（u8-l1）已经交代：主机把整层 int8 特征图按「8 个字节一组」塞进 `ap_uint<64>` 数组传给内核，为的是填满 64 位 AXI 通道。那么内核拿到一个 64 位字后，怎么把里面的第 `k` 个 int8 单独取出来？

这就是「按字节提取」要解决的问题：给定一个**元素索引** `idx`（把整个特征图看作一个连续的 int8 数组的下标），求出它藏在哪个 64 位字里、又是那个字的第几个字节，再把那一字节拿出来按**有符号** int8 解释。

要让这一步正确，必须先记住每个 anchor 的 67 个 int8 是怎么排的。`OUTPUT_DIM = 67 = 4×16 + 3`，对第 `n` 个 anchor，它的 67 个特征占用元素下标区间 `[n*67, n*67+67)`：

| 区间（相对 `n*67`） | 长度 | 含义 |
|--------------------|------|------|
| `[0, 16)` | 16 | 距离分支 0（左）的 16 个 DFL bin |
| `[16, 32)` | 16 | 距离分支 1（上）的 16 个 DFL bin |
| `[32, 48)` | 16 | 距离分支 2（右）的 16 个 DFL bin |
| `[48, 64)` | 16 | 距离分支 3（下）的 16 个 DFL bin |
| `[64, 67)` | 3 | 3 个类别 logit |

所以类别 logit 的元素下标是 `n*67 + 64 + m`（`m=0,1,2`），这正是源码第 97 行 `idx_base = n * OUTPUT_DIM + 64` 的来源；距离 bin 的下标是 `n*67 + t*16 + m`（源码第 148 行）。

#### 4.1.2 核心流程

给定元素索引 `idx`，三步取出 int8：

1. **定位字**：`word_idx = idx >> 3`（除以 8，整除），即 `idx` 在第几个 64 位字里。
2. **定位字节**：`byte_idx = idx & 0x7`（对 8 取模），即在该字里的第几个字节（0–7）。
3. **取字节**：`logit_q = (int8_t)( word >> (byte_idx * 8) )`——先把整个 64 位字右移，把目标字节挪到最低 8 位，再强制转成 `int8_t`，让那 8 个比特按**有符号二补码**解释。

位运算示意（小端打包，与 testbench 一致）：

```
ap_uint<64> word:  [byte7 .. byte(idx&7) .. byte0]
想取 byte_idx 这一字节：
    word >> (byte_idx * 8)   →  目标字节落到最低 8 位
    (int8_t)(...)            →  低 8 位按有符号解释（0xFF -> -1）
```

这套 `idx>>3` / `idx&0x7` / `>>(byte_idx*8)` 三连在源码里出现两次：一次取类别 logit，一次取距离 bin。

#### 4.1.3 源码精读

取类别 logit 的 PREFETCH_LOOP：

> [decode_kernel.cpp:L96-L111](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/post_processing/decode_krnl/decode_kernel.cpp#L96-L111) —— 对 3 个类别（`m=0..2`），算出元素下标 `idx = n*67+64+m`，再用 `idx>>3` / `idx&0x7` 从对应 64 位字取出 `int8` logit，存进片上数组 `cls_logits[3]`。

```cpp
int idx_base = n * OUTPUT_DIM + 64;
...
for (int m = 0; m < NUM_CLASSES; ++m) {
    int idx = idx_base + m;
    int word_idx = idx >> 3;    // 除以 8：第几个 64 位字
    int byte_idx = idx & 0x7;   // 模 8：字内第几字节
    ap_uint<64> word = input_data[word_idx];      // 读整个 64 位字
    int8_t logit_q = (int8_t)(word >> (byte_idx * 8)); // 移位后取低字节并转有符号
    cls_logits[m] = logit_q;
}
```

取距离 bin 的 LOGIT_LOAD 用的是同一套位运算，只是下标换成 `n*67 + t*16 + m`，并在取出后乘 `det_scale` 还原成浮点：

> [decode_kernel.cpp:L145-L162](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/post_processing/decode_krnl/decode_kernel.cpp#L145-L162) —— 对某距离分支 `t` 的 16 个 bin，逐个按字节取出 int8，乘 `det_scale=0.1` 还原成浮点 logit，存进 `logits[16]` 供后续 softmax。

注意 testbench 打包时用的是同样的「小端字节序」：`word.range(b*8+7, b*8) = val` 把元素 `i*8+b` 放进字 `i` 的第 `b` 字节（见 [test_bench.cpp:L37-L47](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/post_processing/decode_krnl/test_bench.cpp#L37-L47)），与内核 `>>(byte_idx*8)` 的取法严格对应——否则字节顺序一错，整层特征全乱。

#### 4.1.4 代码实践

**实践目标**：亲手验证「按字节提取」能把打包数据正确还原。

**操作步骤**（示例代码，用 numpy 模拟一个 64 位字）：

```python
import numpy as np

def pack(int8_vals):                       # 8 个 int8 -> 一个 uint64（小端）
    w = np.uint64(0)
    for b in range(8):
        w |= np.uint64(np.uint8(int8_vals[b])) << np.uint64(b * 8)
    return w

def extract(word, idx):                    # 模拟内核：从 packed 字里取第 idx 个 int8
    word_idx = idx >> 3
    byte_idx = idx & 0x7
    # 注意：这里 word 已是单个 64 位字，word_idx 应为 0
    shifted = np.uint64(word) >> np.uint64(byte_idx * 8)
    return int(np.int8(shifted & np.uint64(0xFF)))   # 取低 8 位按有符号解释

vals = [0, 1, -1, 127, -128, 50, -50, 7]   # 8 个有符号值
w = pack(vals)
for i in range(8):
    assert extract(w, i) == vals[i], (i, extract(w, i), vals[i])
print("byte 提取校验通过")
```

**需要观察的现象 / 预期结果**：断言全部通过。重点看 `-1`（比特 `0xFF`）和 `-128`（比特 `0x80`）：若忘记 `(int8_t)`/`np.int8` 的有符号转换，它们会被错误地读成 255 和 128。

#### 4.1.5 小练习与答案

**练习 1**：元素下标 `idx = 67` 对应哪个 `word_idx`、哪个 `byte_idx`？它属于哪个 anchor 的哪个特征？

> **答案**：`word_idx = 67 >> 3 = 8`，`byte_idx = 67 & 0x7 = 3`。因为 `67 = 1*67 + 0`，它是第 1 个 anchor（n=1）的第 0 个元素，即距离分支 0（左）的第 0 个 DFL bin。

**练习 2**：为什么用 `idx >> 3` 而不是 `idx / 8`？为什么用 `idx & 0x7` 而不是 `idx % 8`？

> **答案**：对正整数两者等价，但 `>> 3` 和 `& 0x7` 是单周期位运算，`/` 和 `%` 在硬件上要综合成除法器（昂贵）。HLS 里写位运算是告诉工具「我要的是廉价移位/掩码」。

### 4.2 整型置信阈值剔除

#### 4.2.1 概念说明

YOLOv8 检测图里绝大多数 anchor 都是背景，对应的类别 logit 很小。如果对每个 anchor 都先算 sigmoid、再和 0.9 比，那几万个 anchor 就要算几万次 `expf`——`expf` 在 FPGA 上又慢又费资源。

关键洞察：**sigmoid 是单调函数**，所以「`sigmoid(x) > 0.9`」完全可以等价地写成「`x > sigmoid⁻¹(0.9)`」。而 `x = logit * det_scale = logit * 0.1`，于是阈值可以预先算成**一个固定的整数**，整个比较退化成一次**整数大小比较**——零开销。这个预先算出的整数就是源码里的 `conf_thresh_inverse`。

这个「整型阈值」是后续 `ANCHOR_LOOP` 能跑得快的总开关：在动用任何 `expf` 之前，先用它把背景 anchor 整批 `continue` 掉。

#### 4.2.2 核心流程

阈值推导（数学）：

设类别 logit 为 `q`（int8），反量化得 `x = q · det_scale = 0.1·q`，置信度

\[
s = \sigma(x) = \frac{1}{1+e^{-x}}.
\]

要求 \(s > \text{conf\_thresh} = 0.9\)。sigmoid 单调递增，反解：

\[
x > \sigma^{-1}(0.9) = -\ln\!\left(\frac{1}{0.9}-1\right) = \ln 9 \approx 2.197.
\]

代回 \(x = 0.1 q\)：

\[
q > \frac{2.197}{0.1} = 21.97.
\]

整数化（源码用 `floor`）：

\[
\text{conf\_thresh\_inverse} = \left\lfloor 21.97 \right\rfloor = 21.
\]

判定 `q > 21`（即 `q ≥ 22`）等价于 `sigmoid(0.1·q) > 0.9`。源码里 `ct = -logf(1/conf_thresh - 1)/det_scale` 正是 \(21.97\)，再 `floor` 成 21。

主循环里的两次剔除（一前一后）：

1. **PREFETCH 后、softmax 前**（CLASS_CHECK_LOOP）：3 个类别 logit 全 ≤ 21 → `continue`，**跳过整个 4 分支 softmax**（最大头的优化）。
2. **算完框后、写候选时**（CLASS_EMIT_LOOP）：逐个类别再判一次 `q > 21`，只对过阈值的类别写一条候选（一个 anchor 最多写 `NUM_CLASSES` 条）。

> 注意「两次比较」不是冗余 bug：第一次是「该 anchor 值不值得算 softmax」的粗筛；第二次是「具体哪几个类别该出框」的细判。

#### 4.2.3 源码精读

阈值常量预算（在主循环之前，只算一次）：

> [decode_kernel.cpp:L55-L58](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/post_processing/decode_krnl/decode_kernel.cpp#L55-L58) —— 用 `conf_thresh=0.9`、`det_scale=0.1` 算出 `ct≈21.97`，`floor` 成整数 21，作为后续整数比较的阈值。

```cpp
const float det_scale = 0.1f;
const float conf_thresh = 0.9f;
float ct = -logf(1.0f / conf_thresh - 1.0f) / det_scale;   // ≈ 21.97
int32_t conf_thresh_inverse = (int32_t)floorf(ct);          // = 21
```

softmax 前的提前剔除：

> [decode_kernel.cpp:L115-L124](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/post_processing/decode_krnl/decode_kernel.cpp#L115-L124) —— 3 个类别 logit 若都 ≤ 21，则 `found` 保持 false，`continue` 直接跳过本 anchor 的所有距离 softmax。

```cpp
bool found = false;
for (int m = 0; m < NUM_CLASSES; ++m) {
    if ((int)cls_logits[m] > conf_thresh_inverse)   // 整数比较，零 expf
        found = true;
}
if (!found)
    continue;     // 背景锚点：省掉整个 4×16 softmax
```

写候选时的细判 + 真正的 sigmoid（只对过阈值类别算一次）：

> [decode_kernel.cpp:L224-L241](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/post_processing/decode_krnl/decode_kernel.cpp#L224-L241) —— 对每个类别再判一次 `logit_q > 21`，过阈值才算 `score = sigmoid(logit_q*0.1)` 并写一条候选框。

```cpp
int8_t logit_q = cls_logits[m];
if ((int)logit_q > conf_thresh_inverse && tb < MAX_BOXES) {
    float score = 1.0f / (1.0f + expf(-((float)logit_q) * det_scale)); // 这里才用 expf
    local_score[tb] = score; ...
    tb++;
}
```

注意：直到这一刻（已知要输出该框）才动用 `expf`。背景 anchor 全程零次 `expf`。

#### 4.2.4 代码实践

**实践目标**：验证「整数阈值 21」与「sigmoid > 0.9」判断结果完全一致。

**操作步骤**（示例代码）：

```python
import numpy as np
DET_SCALE, CONF_THRESH = 0.1, 0.9
ct = -np.log(1.0/CONF_THRESH - 1.0) / DET_SCALE
conf_thresh_inverse = int(np.floor(ct))        # 应为 21
print("整数阈值 =", conf_thresh_inverse)

for q in range(-30, 128):                      # 遍历所有 int8 候选
    by_int   = q > conf_thresh_inverse         # 内核做法
    by_float = 1/(1+np.exp(-q*DET_SCALE)) > CONF_THRESH  # 原始 sigmoid 比较
    assert by_int == by_float, q
print("两种判定在所有 int8 上完全一致")
```

**预期结果**：打印 `整数阈值 = 21`，且一致性断言通过。

**需要观察的现象**：如果把 `conf_thresh` 改成 `0.5`，`ct` 会变成 0、`conf_thresh_inverse` 变成 0（任何正 logit 都过）——直观感受阈值与置信度的对应关系。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `conf_thresh_inverse` 用 `floor` 而不是 `round` 或 `ceil`？

> **答案**：要保证「整数判定」是「浮点判定」的**安全近似**。我们要求 `q > 21.97`，最小的合格整数是 22，即判定 `q > 21`。`floor(21.97)=21` 正好给出 `q>21`。若用 `ceil` 得 22，判定 `q>22` 会漏掉 `q=22` 这个本该合格的值；`round` 得 22 同样会漏。

**练习 2**：把 `det_scale` 从 0.1 改成 0.2（其余不变），`conf_thresh_inverse` 会变成多少？这说明了什么？

> **答案**：`ct = 2.197 / 0.2 = 10.98`，`floor → 10`，判定 `q > 10`。说明阈值与 `det_scale` **强耦合**——`det_scale` 是量化参数，一旦改变，整个整数阈值必须重算。这也是为什么全链路的 `det_scale` 必须训推一致。

### 4.3 16-bin 距离 softmax 与边框还原

#### 4.3.1 概念说明

通过阈值筛选后，这个 anchor 被认为「可能有目标」，接下来要把它解码成一个真正的框。DFL 的核心思想是：每条边（左/上/右/下）的网络输出不是单个数，而是 16 个 bin 上的一个**分布**。解码时取这个分布的**期望值**作为最终距离（而不是 argmax 选一个 bin），从而获得亚 bin 精度。

具体地，对某条边，设其 16 个反量化 logit 为 \(l_0, \dots, l_{15}\)。先做数值稳定的 softmax 得到概率 \(p_m\)，再取期望：

\[
p_m = \frac{e^{l_m - \max_k l_k}}{\sum_{k=0}^{15} e^{l_k - \max_k l_k}},\qquad
d = \sum_{m=0}^{15} m \cdot p_m.
\]

\(d\) 就是这条边（以 anchor 中心为基准）的距离，单位是**网格步**（grid cell）。4 条边各算一次，得到 `distances[0..3]`（对应左、上、右、下）。

拿到 4 个距离后，用「参考点 ± 距离」拼框。参考点取格子中心（`grid + 0.5`，与 Ultralytics 的 `make_anchors` 一致）：

\[
\begin{aligned}
x_1 &= \text{pt}_x - d_{\text{左}}, & y_1 &= \text{pt}_y - d_{\text{上}},\\
x_2 &= \text{pt}_x + d_{\text{右}}, & y_2 &= \text{pt}_y + d_{\text{下}}.
\end{aligned}
\]

再换算到输入图像像素坐标（乘 `layer_stride`）：

\[
\text{box\_cx} = \frac{x_1+x_2}{2}\cdot \text{stride},\quad
\text{box\_w} = (x_2-x_1)\cdot \text{stride},
\]

`cy`、`h` 同理。这套「softmax 期望 → dist2bbox → 乘 stride」就是 YOLOv8 在 CPU 后处理里做的 `dist2bbox`，内核把它逐行翻成硬件。

#### 4.3.2 核心流程

对单个 anchor 的距离解码（4 分支并行展开）：

```
for t in 0..3:                          // 左/上/右/下 四条边
    load logits[16]  = (int8 * 0.1)     // 取 16 个 bin，反量化
    max_logit = max(logits)             // 数值稳定
    exps[m]    = exp(logits[m] - max)
    sum        = Σ exps[m]
    acc        = Σ (exps[m]/sum) * m    // DFL 期望值 d
    distances[t] = acc
```

然后由 anchor 中心 + distances 还原框：

```
grid_x = n % layer_size;  grid_y = n / layer_size
pt_x = grid_x + 0.5;      pt_y = grid_y + 0.5        // 参考点=格子中心
x1 = pt_x - distances[0];  y1 = pt_y - distances[1]  // 左、上
x2 = pt_x + distances[2];  y2 = pt_y + distances[3]  // 右、下
box_cx = (x1+x2)*0.5 * layer_stride
box_cy = (y1+y2)*0.5 * layer_stride
box_w  = (x2-x1)     * layer_stride
box_h  = (y2-y1)     * layer_stride
```

> 小贴士：DFL 用期望值而非 argmax，是它对小目标位移敏感的根因。SAR 船舶是点状小目标，这正是本项目用 YOLOv8 + DFL 的动机之一（参见 u3-l1 的 P2 头讨论）。

#### 4.3.3 源码精读

4 个距离分支的外层循环（4.2 已保证只对有目标的 anchor 才走到这里）：

> [decode_kernel.cpp:L130-L191](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/post_processing/decode_krnl/decode_kernel.cpp#L130-L191) —— 对 4 条边各做：取 16 bin（L145-L162，含 4.1 的按字节提取）→ 找 max（L166-L171）→ 算 exps 与 sum（L174-L180）→ 加权期望（L183-L190）。

取 16 bin 并反量化（4.1 的位运算 + 乘 det_scale）：

```cpp
for (int m = 0; m < DIST_BINS; ++m) {
    int idx = n * OUTPUT_DIM + t * DIST_BINS + m;
    int word_idx = idx >> 3;  int byte_idx = idx & 0x7;
    ap_uint<64> word = input_data[word_idx];
    int8_t logit_q = (int8_t)(word >> (byte_idx * 8));
    logits[m] = ((float)logit_q) * det_scale;     // 反量化
}
```

数值稳定 softmax + DFL 期望值：

```cpp
float max_logit = logits[0];
for (int m = 1; m < DIST_BINS; ++m) if (logits[m] > max_logit) max_logit = logits[m];
float sum = 0.0f;
for (int m = 0; m < DIST_BINS; ++m) {
    exps[m] = expf(logits[m] - max_logit);        // 减 max 防溢出
    sum += exps[m];
}
float inv_sum = 1.0f / sum;  float acc = 0.0f;
for (int m = 0; m < DIST_BINS; ++m) {
    acc += (exps[m] * inv_sum) * (float)m;        // Σ p_m · m
}
distances[t] = acc;
```

> 注意「减最大值」这一步不能省：`expf` 对大正数会溢出到 `inf`。这是 softmax 的标准数值稳定写法。

anchor 中心 + 距离还原框（dist2bbox + 乘 stride）：

> [decode_kernel.cpp:L195-L210](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/post_processing/decode_krnl/decode_kernel.cpp#L195-L210) —— 由 `n` 算网格坐标、取格子中心作参考点，用 `distances[0..3]` 还原左上/右下角，最后乘 `layer_stride` 得到图像像素坐标的 `box_cx/cy/w/h`。

```cpp
int grid_x = n % layer_size;  int grid_y = n / layer_size;
float pt_x = (float)grid_x + 0.5f;   // anchor 中心 = 格子中心
float pt_y = (float)grid_y + 0.5f;
float x1 = pt_x - distances[0];      // 左
float y1 = pt_y - distances[1];      // 上
float x2 = pt_x + distances[2];      // 右
float y2 = pt_y + distances[3];      // 下
float box_cx = (x1 + x2) * 0.5f * layer_stride;
float box_cy = (y1 + y2) * 0.5f * layer_stride;
float box_w  = (x2 - x1) * layer_stride;
float box_h  = (y2 - y1) * layer_stride;
```

这四个浮点值随后连同类别与 score 写入 `local_boxes_*`（4.2 的 CLASS_EMIT_LOOP）。

#### 4.3.4 代码实践

**实践目标**：用 Python 把内核的核心算法（取 3 个类别 logit → 整数阈值判断 → 4×16 softmax 期望值 → 还原 box）逐行复刻一遍，并跑通一个手工构造的 anchor。

**操作步骤**（示例代码——用扁平 int8 数组模拟特征图，省去 64 位打包以突出算法；numpy 的 `buf[idx]` 即等价于内核的「word/byte 提取」，见 4.1.4）：

```python
import numpy as np

DIST_BINS, NUM_CLASSES, OUTPUT_DIM = 16, 3, 67   # 4*16+3
DET_SCALE, CONF_THRESH = 0.1, 0.9
conf_thresh_inverse = int(np.floor(-np.log(1/CONF_THRESH - 1)/DET_SCALE))  # 21

def dfl_expected(logits16):                       # DIST_BRANCH_LOOP 体
    l = np.asarray(logits16, dtype=np.float64) * DET_SCALE
    l = l - l.max()                               # MAX_FIND + 数值稳定
    e = np.exp(l)                                 # EXPS_LOOP
    p = e / e.sum()                               # inv_sum
    return float((p * np.arange(DIST_BINS)).sum())# WEIGHTED_MEAN: Σ p_m·m

def decode_anchor(buf, n, layer_size, layer_stride):
    # ① 取 3 个类别 logit（idx = n*67+64+m）
    base = n * OUTPUT_DIM + 64
    cls = [int(np.int8(buf[base + m])) for m in range(NUM_CLASSES)]
    # ② 整数阈值提前剔除
    if not any(q > conf_thresh_inverse for q in cls):
        return []
    # ③ 4 个距离分支 ×16-bin softmax 期望值
    dist = []
    for t in range(4):
        d16 = [int(np.int8(buf[n*OUTPUT_DIM + t*DIST_BINS + m])) for m in range(DIST_BINS)]
        dist.append(dfl_expected(d16))
    # ④ anchor 中心 + 距离还原框
    grid_x, grid_y = n % layer_size, n // layer_size
    pt_x, pt_y = grid_x + 0.5, grid_y + 0.5
    x1, y1 = pt_x - dist[0], pt_y - dist[1]
    x2, y2 = pt_x + dist[2], pt_y + dist[3]
    box_cx = (x1+x2)*0.5 * layer_stride
    box_cy = (y1+y2)*0.5 * layer_stride
    box_w  = (x2-x1)   * layer_stride
    box_h  = (y2-y1)   * layer_stride
    # ⑤ 对过阈值类别写候选（含 sigmoid）
    out = []
    for m, q in enumerate(cls):
        if q > conf_thresh_inverse:
            score = 1.0/(1.0+np.exp(-q*DET_SCALE))
            out.append((box_cx, box_cy, box_w, box_h, m, round(score,4)))
    return out

# —— 构造一个 P2 层(layer_size=200, stride=4)，让 anchor n=12345 命中 ——
layer_size, layer_stride = 200, 4.0
total = layer_size * layer_size * OUTPUT_DIM
buf = np.zeros(total, dtype=np.int8)
n = 12345
buf[n*OUTPUT_DIM + 64 + 1] = 30                      # 类别 1(船) logit=30 → 过阈
# 让左/右距离分布分别聚集在 bin 2 / bin 5（其余为 0）
buf[n*OUTPUT_DIM + 0*16 + 2] = 40
buf[n*OUTPUT_DIM + 2*16 + 5] = 40
print(decode_anchor(buf, n, layer_size, layer_stride))
```

**需要观察的现象 / 预期结果**：输出形如 `[(cx, cy, w, h, 1, 0.9526)]` 的一条候选（类别 1，score≈sigmoid(3.0)≈0.9526）。`dist[0]≈2.0`、`dist[2]≈5.0`（单峰 softmax 的期望≈峰值 bin），左/上分支全 0 时 `dist[1]=dist[3]=0`，于是框大致以 `pt=(grid_x+0.5, grid_y+0.5)` 为中心、偏向右下。可手算 `box_w=(2+5)*4=28`、`box_h=0`（因为上下距离都为 0）——这正暴露了「只设了左右距离」的构造缺陷，读者可补全上下距离让框变成合理矩形。

> 说明：本实践用扁平 int8 数组代替 64 位打包，纯粹是为了聚焦算法。若要严格复现内核的字节提取，可把 `buf` 改成 `ap_uint<64>`/`np.uint64` 数组并套用 4.1.4 的 `extract(word, idx)`。

#### 4.3.5 小练习与答案

**练习 1**：若某距离分支的 16 个 bin 全相等（logit 全为同一个值），`dfl_expected` 返回多少？物理含义是什么？

> **答案**：softmax 后每个 bin 概率 \(1/16\)，期望 \(d = \sum_{m=0}^{15} m \cdot \frac{1}{16} = \frac{0+1+\dots+15}{16} = \frac{120}{16} = 7.5\)。物理含义：网络对这条边的距离「完全不确定」，输出退化为分布的中位附近值。这也是 DFL 比 argmax（会硬选 bin 0）更平滑的地方。

**练习 2**：`box_cx = (x1+x2)*0.5 * layer_stride`。把它展开成 `grid_x`、`distances[0]`、`distances[2]` 的表达式，说明为什么它等价于「参考点 ± 半个宽」。

> **答案**：`x1 = pt_x - distances[0]`，`x2 = pt_x + distances[2]`，故
> \((x_1+x_2)/2 = \text{pt}_x + (\text{distances}[2]-\text{distances}[0])/2\)。
> 即中心 = 参考点 + (右距 − 左距)/2，再乘 `layer_stride` 落到像素坐标。当左右距离相等（框关于参考点对称）时，中心恰为参考点。

**练习 3**：主循环结束后有一句 `if (total_boxes & 1) total_boxes--;`（[L302-L303](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/post_processing/decode_krnl/decode_kernel.cpp#L302-L303)），它在做什么？为什么？

> **答案**：若候选框总数是奇数，就丢弃最后一个。这是一个吞吐/对齐相关的小技巧（注释写 `drop last box`）：把候选数凑成偶数，便于下游成对处理或写回对齐。它属于工程妥协而非算法必需，阅读时应意识到它会悄悄少输出至多一个框。

## 5. 综合实践

**任务**：把本讲三个模块串起来，做一个「单 anchor 的软件解码器」，并与 testbench 的设定对账。

1. 阅读内核主循环 [decode_kernel.cpp:L68-L300](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/post_processing/decode_krnl/decode_kernel.cpp#L68-L300)，在纸上列出第 `n` 个 anchor 从「读字节」到「写候选」的全部步骤。
2. 基于 4.3.4 的 Python 代码，扩展成一个**逐层**解码器：外层 `for layer in 4 个头`，每层用对应的 `layer_size`/`layer_stride`（见 2.2 表格）调用 `decode_anchor`，统计每层产出多少候选框——结构上对齐 testbench 的 `for (layer ...)` 循环。
3. 对照 [testbench 的打包方式](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/post_processing/decode_krnl/test_bench.cpp#L37-L47)（`val = (idx % 127) - 63` 的填充规律），手工预测：testbench 用这种「按 idx 循环」填充的随机数据，会有 anchor 过得了 `conf_thresh_inverse=21` 的整数阈值吗？（提示：`idx%127-63` 的取值范围是 `[-63, 63]`，类别 logit 在 `idx ≡ ...` 时可能很大。）
4. **待本地验证**：若你有 Vitis HLS 环境，把 testbench 跑起来，对比你的 Python 解码器与硬件 `global_box_count` 是否一致（注意 testbench 是随机数据，需用相同种子/相同填充复现）。

通过这个任务，你会完整走过「字节提取 → 阈值剔除 → DFL softmax → dist2bbox → 逐层聚合」整条算法链，并理解它如何与 host 的逐层调用配合。

## 6. 本讲小结

- 每个 anchor 的 67 个 int8 按 `[4 段×16 bin 距离][3 类别]` 排列；类别在下标 `n*67+64+m`，距离 bin 在 `n*67+t*16+m`。
- 从 `ap_uint<64>` 取 int8 的固定三连：`word_idx = idx>>3`、`byte_idx = idx&0x7`、`logit_q = (int8_t)(word >> (byte_idx*8))`，必须配 testbench 的小端打包。
- 用 sigmoid 单调性把「`sigmoid(0.1·q) > 0.9`」预解成整数 `q > 21`（`conf_thresh_inverse`），在 softmax 之前用整数比较把背景 anchor 整批 `continue` 掉——`expf` 只对真正要输出的框才算。
- 4 条边各做一次「减最大值的 16-bin softmax」取期望值得 `distances[0..3]`（DFL 解码），再用「格子中心 ± 距离 × stride」还原 `box_cx/cy/w/h`（dist2bbox）。
- 本讲只讲算法；这些循环上的 `#pragma HLS UNROLL/PIPELINE/ARRAY_PARTITION` 如何把它们变成并行硬件，是 u8-l3 的主题。

## 7. 下一步学习建议

- **下一步**：学 u8-l3《HLS 优化指令》，看本讲的 `CLASS_CHECK_LOOP`、`DIST_BRANCH_LOOP`、`LOGIT_LOAD`、`MAX_FIND`、`EXPS_LOOP`、`WEIGHTED_MEAN` 上的 `UNROLL`、`PIPELINE II=1`、`ARRAY_PARTITION complete` 如何把这套串行算法展开成流水线，以及 `local` 写指针 `tb` 如何帮 HLS 分析依赖。
- **回头对照**：本讲的 DFL softmax + dist2bbox 与 u6-l3 软件后处理 `yolov8_post_process` 完全同构（常量 67/3/16、阈值公式逐行对齐），可作为「软硬一致性」的交叉验证；板载 profiling（u7-l3）把这段标为 `YOLOV8_DECODING`，正是本内核要加速的对象。
- **源码延伸**：读完本讲可通读 [decode_kernel.cpp 全文](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/post_processing/decode_krnl/decode_kernel.cpp)，重点看本讲未展开的「`local` 写指针 `tb` 回写」「奇数框丢弃」「6 个 `WRITE_BACK_*` 拷贝循环」这三处工程细节，为 u8-l3 的优化讨论做准备。

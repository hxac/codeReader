# CuTeDSL SM90 专用 kernel：d384 / d512 与 generic

## 1. 本讲目标

本讲深入 CuTeDSL 后端（包名 `ffpa_attn.cute`、对外后端名 `"cutedsl"`）在 Hopper（SM90）上的**真正算力核心**——那一组按 head_dim 量身特化的 kernel 类。

学完后你应该能做到：

- 说清 CuTeDSL 在 SM90 上为什么要**按 head_dim 写多份专用 kernel**（d512 / d384 / generic），而不是像 Triton 那样一份模板跑所有 D。
- 读懂 `FFPAAttnFwdSm90SplitD`（D512 前向基线）的「1 个 TMA 生产者 + 2 个 MMA 消费者」三角色流水线，以及它对 `tile_m=64`、`tile_n=32`、`tile_hdimv % 256 == 0` 的硬假设。
- 区分 `FFPAAttnFwdSm90SplitDGeneric`（把 D 填充到 512 复用）与 `FFPAAttnFwdSm90SplitDD384Aware`（真正的 384 物理 tile）两条泛化路线的取舍。
- 读懂 `FFPAAttnBwdDKDVSm90SplitD`（D512 反向 dK/dV）的「2-pass D-split + K/V 持久化」与 6 阶段双 WG 协作，并理解反向被拆成 dKdV 与 dQ 两个独立 kernel 的原因。
- 把 SM90 专用 kernel 与 SM80 generic 兜底路径放在同一张选型矩阵里对照，明白「谁在什么时候接管」。

## 2. 前置知识

本讲是 [u6-l1](u6-l1-cutedsl-overview-sm80-sm90.md)、[u6-l2](u6-l2-cutedsl-layout-varlen.md)、[u6-l3](u6-l3-tile-scheduler-pipeline.md) 的延续。在进入代码前，先用一句话复习几个关键认知：

- **CuTeDSL 路径选择**只由单一谓词 `_use_sm90_specialized` 决定：当且仅当 Hopper（`major==9`）且 q、v 的 head_dim 对称落在 `[320, 512]` 时走 SM90 专用 kernel，其余一律走 SM80 通用 Split-D 兜底。本讲只覆盖「走 SM90 专用」之后，内部又如何按 D 细分。
- **WGMMA / TMA 是 Hopper 独占指令**。WGMMA（warpgroup MMA）是 4 个 warp 协同的大矩阵乘；TMA（Tensor Memory Accelerator）是异步、按描述符搬运多维 tile 的硬件单元。CuTeDSL 把它们封装成 `TiledMma` 与 `CopyAtom`。
- **CuTeDSL 在编译期对 tile 形状做特化**：TMA atom 与 WGMMA fragment 的形状是编译期常量。这是本讲一切「为什么按 D 写多份」的根本原因（见 4.1）。
- **Split-D**（参见 [u1-l1](u1-l1-what-is-ffpa-split-d.md) 与 [u4-l2](u4-l2-split-d-fine-grained-tiling.md)）：在 head_dim 方向做 MMA 级精细分块，把 SRAM 工作集降到与 D 无关。

还需熟悉的两个注意力数学对象：

注意力前向：

\[
O = \mathrm{softmax}(S)\,V,\qquad S=\tau\,QK^{\top},\quad \tau=1/\sqrt{D}
\]

其逐块（online softmax）更新——每来一个 KV 块，用行最大值 \(m\) 做对数域重缩放：

\[
m_{\text{new}}=\max(m,\;\max(S_{\text{block}})),\quad
l_{\text{new}}=e^{m-m_{\text{new}}}\,l+\mathrm{rowsum}(P_{\text{block}}),\quad
O_{\text{new}}=e^{m-m_{\text{new}}}\,O+P_{\text{block}}V_{\text{block}}
\]

反向（链式法则，详见 [u5-l1](u5-l1-bwd-algo-delta-preprocess.md)）：

\[
dV=P^{\top}dO,\quad dP=dO\,V^{\top},\quad dS=P\odot(dP-D_i),\quad dK=\tau\,dS^{\top}Q,\quad dQ=\tau\,dS\,K^{\top}
\]

其中耦合项 \(D_i=\mathrm{rowsum}(P\odot dP)=\mathrm{rowsum}(dO\odot O)\) 由反向预处理 kernel 提前算好。

## 3. 本讲源码地图

本讲涉及的关键文件（均在 `src/ffpa_attn/cute/` 下）：

| 文件 | 角色 |
|---|---|
| [`_fwd_d512_sm90.py`](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/_fwd_d512_sm90.py) | **D512 前向基线 kernel**，类 `FFPAAttnFwdSm90SplitD`。是另外两个前向类的父类。 |
| [`_fwd_generic_sm90.py`](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/_fwd_generic_sm90.py) | **泛化前向 wrapper**，类 `FFPAAttnFwdSm90SplitDGeneric`。继承 D512，把任意 \(320\le D\le512\) 填充到物理 512。 |
| [`_fwd_d384_sm90.py`](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/_fwd_d384_sm90.py) | **D384 特化前向**，类 `FFPAAttnFwdSm90SplitDD384Aware`。继承 Generic，把物理 tile 收窄到 384。 |
| [`_dkdv_d512_sm90.py`](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/_dkdv_d512_sm90.py) | **D512 反向 dK/dV kernel**，类 `FFPAAttnBwdDKDVSm90SplitD`。 |
| [`_dq_d512_sm90.py`](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/_dq_d512_sm90.py) | D512 反向 dQ kernel，类 `FFPAAttnBwdDQSm90SplitD`（本讲作对照简介）。 |
| [`_dkdv_d384_sm90.py`](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/_dkdv_d384_sm90.py) | D384 反向 dK/dV kernel，类 `FFPAAttnBwdDKDVSm90SplitDD384`（true-tail 设计）。 |
| [`_ffpa_fwd_sm90.py`](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/_ffpa_fwd_sm90.py) | SM90 前向**入口与编译缓存**，含「D → 选哪个 kernel 类」的分发。 |
| [`_ffpa_bwd_sm90.py`](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/_ffpa_bwd_sm90.py) | SM90 反向**入口与编译缓存**，含 dKdV / dQ 双 kernel 分发。 |
| [`_utils.py`](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/_utils.py) | 共享常量（`MIN_SUPPORTED_HEAD_DIM=320` 等）与校验。 |
| [`_fwd_generic_sm80.py`](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/_fwd_generic_sm80.py) | SM80 通用兜底 kernel（本讲末尾作对照）。 |

> 提示：类名是实现细节（README 明确标注），稳定契约是文件路径与 wrapper 函数。但读这些 kernel 时，类名正好是组织代码的主线。

## 4. 核心概念与源码讲解

### 4.1 特化动机：SM90 为什么要按 head_dim 写专用 kernel

#### 4.1.1 概念说明

在 Triton 后端（[u4-l1](u4-l1-triton-fwd-online-softmax.md)）里，前向 kernel 是**一份模板**，靠 `BLOCK_HEADDIM` 等 `tl.constexpr` 在 JIT 时特化出不同 D 的 PTX——逻辑统一。CuTeDSL 走的是另一条路：TMA atom 的 box 形状、WGMMA fragment 的 M/N/K 维度都是**编译期常量**，CuTeDSL 会为每个具体形状生成一套寄存器分配、SMEM 布局与 PTX。

这意味着：一旦你把「QK 一次 WGMMA 算完整个 D」写成代码，D=512 这条路径就被焊死成 512。要支持 D=384，你有两个选择：

1. **填充复用（generic）**：把 384 当成「384 真数据 + 128 填充」，仍然跑 D=512 的物理 tile，靠 TMA 的越界（OOB）语义把填充位置零填、丢弃写入。**零新代码，但浪费 1/4 的 SRAM、TMA 带宽与 WGMMA 算力**。
2. **真 tile 特化（d384-aware）**：另写一套 384 宽的 atom / 布局 / fragment，**省下那 1/4 的浪费，代价是多维护一份代码**。

FFPA 的策略是「两头都要」：

- D=512 是高频主战场（H200 上可达 427 TFLOPS），配**最激进**的专用 kernel。
- D∈(256,384] 配 **true-tail 专用 kernel**（384 真物理 tile，且反向用 256+128 的非等分切法）。
- 其余 D∈[320,512]（如 448）配 **generic 填充**，复用 512 kernel。
- D>512 或非 Hopper 一律甩给 SM80 generic 兜底（4.5）。

#### 4.1.2 核心流程

前向「D → 类」的分发在一个三元判断里完成：

[src/ffpa_attn/cute/_ffpa_fwd_sm90.py:193-198](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/_ffpa_fwd_sm90.py#L193-L198) —— 默认选 D512；只有当 D≠512 时，再按 `D<=384` 二分到 D384Aware 或 Generic：

```python
fwd_kernel_cls = FFPAAttnFwdSm90SplitD
if head_dim != 512 or head_dim_v != 512:
  fwd_kernel_cls = (
    FFPAAttnFwdSm90SplitDD384Aware if head_dim <= D384_AWARE_HEAD_DIM
    and head_dim_v <= D384_AWARE_HEAD_DIM else FFPAAttnFwdSm90SplitDGeneric
  )
```

反向的分发结构对偶，但因为反向被拆成 **dKdV 与 dQ 两个 kernel**（见 4.4），所以先判 `bwd_kernel_kind` 字符串，再分别映射到两个类：

[src/ffpa_attn/cute/_ffpa_bwd_sm90.py:314-319](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/_ffpa_bwd_sm90.py#L314-L319) —— 三类：`d512` / `d384` / `d512_generic`：

```python
if head_dim == 512 and head_dim_v == 512:
  bwd_kernel_kind = "d512"
elif head_dim <= 384 and head_dim_v <= 384:
  bwd_kernel_kind = "d384"
else:
  bwd_kernel_kind = "d512_generic"
```

随后这个 `bwd_kernel_kind` 被分别映射到 dKdV 与 dQ 两个 kernel 类，例如 dKdV 侧：

[src/ffpa_attn/cute/_ffpa_bwd_sm90.py:349-363](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/_ffpa_bwd_sm90.py#L349-L363)（dQ 侧 [`:397-411`](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/_ffpa_bwd_sm90.py#L397-L411) 结构相同）。

类继承关系一览（**前向**）：

```
FFPAAttnFwdSm90SplitD            # _fwd_d512_sm90.py：D512 真实现
        ▲
        │ 继承
FFPAAttnFwdSm90SplitDGeneric     # _fwd_generic_sm90.py：填充到 512
        ▲
        │ 继承
FFPAAttnFwdSm90SplitDD384Aware   # _fwd_d384_sm90.py：收窄到 384
```

注意继承方向反直觉：**Generic 继承 D512，D384Aware 又继承 Generic**。这是因为 D512 是功能全集，Generic「几乎什么都不改、只调物理 tile 宽度」，D384Aware「在 Generic 基础上把宽度改成 384」——越特化越靠近叶子。反向同理（`FFPAAttnBwdDKDVSm90SplitDGeneric` 继承 `FFPAAttnBwdDKDVSm90SplitD`）。

#### 4.1.3 源码精读

支撑这一切的常量在共享工具文件里，是全部门禁的数字之源：

[src/ffpa_attn/cute/_utils.py:21-32](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/_utils.py#L21-L32) —— `MIN_SUPPORTED_HEAD_DIM=320`、`SM90_SUPPORTED_HEAD_DIM=512`、`SM80_SUPPORTED_HEAD_DIM=1024`，以及 SM90 前向/反向的硬编码 tile 尺寸。

[src/ffpa_attn/cute/_utils.py:117-131](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/_utils.py#L117-L131) —— `_validate_head_dims` 要求对称 head_dim 且 \(320\le D\le512\)，这正是「能进 SM90 专用」的必要条件（充分条件还要 Hopper）。

> 区分两个常量语义：`SM90_SUPPORTED_HEAD_DIM=512` 是**专用 kernel 的物理上限**；`SM80_SUPPORTED_HEAD_DIM=1024` 是**兜底路径的上限**。超过 512 的 Hopper 调用（如 D=640）不会被专用 kernel 接管，而是掉到 SM80 generic——即便它跑在 Hopper 上。

#### 4.1.4 代码实践

**实践目标**：在不跑 GPU 的前提下，用手动构造的输入走一遍「D → kernel 类」的选择逻辑，验证三类边界。

**操作步骤**：

1. 打开 [_ffpa_fwd_sm90.py:193-198](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/_ffpa_fwd_sm90.py#L193-L198)，把分发逻辑抄成纯 Python（不依赖 cutlass）：

   ```python
   # 示例代码：手工复刻前向分发（仅供理解，非项目代码）
   D384_AWARE_HEAD_DIM = 384
   def pick_fwd_cls(head_dim, head_dim_v):
       if head_dim == 512 and head_dim_v == 512:
           return "FFPAAttnFwdSm90SplitD"
       if head_dim <= D384_AWARE_HEAD_DIM and head_dim_v <= D384_AWARE_HEAD_DIM:
           return "FFPAAttnFwdSm90SplitDD384Aware"
       return "FFPAAttnFwdSm90SplitDGeneric"
   ```

2. 对 `(512,512)`、`(384,384)`、`(448,448)`、`(320,320)`、`(576,576)` 五组调用，打印选中的类名。

**需要观察的现象 / 预期结果**：`512→SplitD`、`384→D384Aware`、`448→Generic`、`320→D384Aware`、`576→Generic`。最后一组 `576` 虽然落在 Generic，但要记得它**实际不会到这一行**——`_use_sm90_specialized` 在更上层就因为 `D>512` 把它送去了 SM80 generic 兜底。这一步只验证「如果进了 SM90 专用分支，会选谁」。

> 结果标注：本实践是源码阅读型，不触发 GPU；上面是**示例代码**，不是项目原有代码。

#### 4.1.5 小练习与答案

**练习 1**：为什么 CuTeDSL 不能像 Triton 那样用一份 kernel 模板覆盖所有 D？

**参考答案**：因为 CuTeDSL 的 TMA atom box 形状与 WGMMA fragment 的 M/N/K 维度是**编译期常量**，CuTeDSL 会为每个具体形状生成独立的寄存器分配、SMEM 布局与 PTX 指令序列；而 Triton 用 `tl.constexpr` 在 JIT 时把 `BLOCK_HEADDIM` 等参数烘焙进同一份 IR，逻辑统一。CuTeDSL 的「特化」是物理层面的（不同形状 = 不同机器码），所以高频形状值得各写一份以榨干性能。

**练习 2**：假设要把支持范围下探到 D=256，按现有架构最省力的做法是新增哪一类？为什么不直接复用 generic？

**参考答案**：最省力是**复用 generic（填充到 512）**——零新代码、只放宽 `MIN_SUPPORTED_HEAD_DIM` 与校验。但代价是把 256 当 512 跑，浪费一半 SRAM/带宽/算力；性能敏感时才会像 D384 那样新增一个 `FFPAAttnFwdSm90SplitDD256Aware` 真 tile 类。注意：256 在默认 Triton/SDPA 下本就会回退，CuTeDSL 是否值得为它写专用 kernel 是另一个工程取舍。

---

### 4.2 FFPAAttnFwdSm90SplitD：D512 前向基线（3-role pipeline）

#### 4.2.1 概念说明

`FFPAAttnFwdSm90SplitD` 是 D=512 的前向真实现，也是另外两个前向类的父类。它的核心设计是**「1 个 TMA 生产者 warp group + 2 个 MMA 消费者 warp group」的三角色流水线（3-role pipeline）**，把「搬数据」和「算矩阵乘」彻底分给不同 warp group 并行。

为什么 D=512 能用一个**全 D 的单次 WGMMA**算完 QK？因为 Split-D（[u4-l2](u4-l2-split-d-fine-grained-tiling.md)）已经把 SRAM 工作集降到与 D 无关，512 宽的 Q/K tile 可以整体驻留 SMEM，于是 QK 不必再沿 D 切片循环——这是相对 Triton 路径的一个简化与提速点。但 PV（\(P@V\)）这一步**沿 N（输出列）方向拆成前后两半**（cols 0:256 与 256:512），分别交给 WG1（前半）与 WG2（后半）并行算。

#### 4.2.2 核心流程

类 docstring 用一段话精确描述了三角色分工：

[src/ffpa_attn/cute/_fwd_d512_sm90.py:72-83](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/_fwd_d512_sm90.py#L72-L83) —— 三个 warp group 的职责。

伪代码化的单 CTA（一个 thread block）结构：

```
CTA = 384 threads = 3 个 warp group（每个 128 threads = 4 warps）
├─ WG0 (producer, warp_group_idx==0)
│    └─ 只有 warp-0 (elect_one) 发 TMA：每个 work-tile 搬 1 次全 D 的 Q，
│       每个 n_block 搬 1 块 K、1 块 V（K/V 双缓冲以与 WGMMA 重叠）
├─ WG1 (warp_group_idx==1)
│    └─ QK（全 D 单 WGMMA）→ online softmax → PV-前半（gO 列 0:256）→ 写 LSE
└─ WG2 (warp_group_idx==2)
     └─ 只做 PV-后半（gO 列 256:512），无生产者角色

跨 WG 握手：
  - sP（softmax 输出 P）由 WG1 写、WG2 读 → 用 pipeline_P（mbarrier）桥接
  - sScale（行重缩放因子）由 WG1 写、WG2 读 → 用 pipeline_Scale 桥接
  - sV（主循环）与 sO（epilogue）生命周期互斥 → 用 cute.union 共享同一块物理 SMEM
```

每个 work-tile（一个 `(m_block, head, batch)`）的主循环：WG1 沿 `n_block` 反向遍历 KV 块，每个 n_block 完成一次「算 score → online softmax 重缩放 → 把 P 写进 sP → 用 P 算 PV-前半并累加进 `acc_O_front`」；WG2 则消费同一块 P 算 PV-后半累加进 `acc_O_back`。循环结束后做最终重缩放（用最后一行的 `row_scale`），各自走 epilogue 把半宽 O 经 `sO` 用 TMA 存回显存。

#### 4.2.3 源码精读

**关键假设与流水线配置**——这是 D512 特化相对 generic 最「激进」的地方，全部以硬编码 + assert 体现：

[src/ffpa_attn/cute/_fwd_d512_sm90.py:148-173](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/_fwd_d512_sm90.py#L148-L173) —— 这段做了四件事：把 `tile_n` 强行设为 32（为了让 K 在 hdim=512 下仍能跑真正的 2 级流水线）；算出 `tile_hdimv_half=256`；设定各缓冲级数（`num_stages_k=2`、`num_stages_v=2`、`num_stages_p=2`、`num_stages_sP_buf=3`）；以及三条硬断言。读这几行要重点理解三条 assert 的物理含义：

```python
self.tile_n = 32
self.tile_hdimv_half = self.tile_hdimv // 2  # 256
...
assert self.tile_m == 64, "SplitD requires tile_m == 64"
assert self.tile_hdimv % 256 == 0, "PV (1,2,1) requires tile_hdimv % 256 == 0"
```

- `tile_m == 64`：QK 用单 M-atom WGMMA（`atom_layout_mnk=(tile_m//64,1,1)`），一个 warpgroup 恰好算 64 行。
- `tile_hdimv % 256 == 0`：PV 沿 N 拆成 `(1,2,1)` 非对称两半，每半 256 列由一个 WG 负责。
- `tile_n == 32`：N 方向块宽。注意它与上层传入的默认 `SM90_FWD_TILE_N=128` 不同——构造器里**覆盖**成了 32，因为 D=512 下 K 块太大，只有 N=32 才能让 K 真正双缓冲而不撑爆 SMEM。

**MMA 拓扑**——四套 `TiledMma` 各司其职：

[src/ffpa_attn/cute/_fwd_d512_sm90.py:175-222](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/_fwd_d512_sm90.py#L175-L222) —— `tiled_mma_qk`（全 D 的 QK）、`tiled_mma_pv`（SS 模式，双 WG 沿 N 拆）、`tiled_mma_pv_wg1`（WG1 的 RS 模式 PV-前半，A 操作数来自寄存器而非 SMEM，省一次 SMEM 描述符读取，注释说实测 +3 TFLOPS）、`tiled_mma_pv_epi`（epilogue 的单 atom）。

**三角色调度**——kernel 入口按 `warp_group_idx` 分派：

[src/ffpa_attn/cute/_fwd_d512_sm90.py:749-822](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/_fwd_d512_sm90.py#L749-L822) —— `warp_group_idx==0` 调 `producer`（并 `setmaxregister_decrease` 把寄存器让给 MMA WG）；`==1` 调 `mma_wg1`（`setmaxregister_increase` 到 240）；`==2` 调 `mma_wg2`。读这段时注意「生产者降寄存器、消费者升寄存器」的资源分配思想——Hopper 的 `setmaxnreg` 指令允许同一 CTA 内不同 warp group 用不同寄存器上限。

**SMEM 共享存储**——`sV` 与 `sO` 用 `cute.union` 复用物理 SMEM：

[src/ffpa_attn/cute/_fwd_d512_sm90.py:263-281](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/_fwd_d512_sm90.py#L263-L281) —— `SmemVO_t` 是一个 union：主循环期这块 SMEM 当 `sV` 用，epilogue 期当 `sO` 用。因为二者生命周期互斥（V 在主循环末尾就释放、O 只在 epilogue 写），union 能在 228KB SMEM 预算下塞下原本会冲突的两个缓冲。

#### 4.2.4 代码实践

**实践目标**：在 D512 前向 kernel 里定位「三角色 + 两半 PV」的物理证据，建立「类 docstring → 构造器 assert → kernel 分派」三处的对应关系。

**操作步骤**：

1. 读 [docstring（72-83 行）](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/_fwd_d512_sm90.py#L72-L83)，记下 WG0/WG1/WG2 各自的一句话职责。
2. 跳到 [构造器（148-173 行）](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/_fwd_d512_sm90.py#L148-L173)，找出 `tile_hdimv_half` 的来源，并解释为什么 `tile_hdimv % 256 == 0` 是 PV 拆两半的前提。
3. 跳到 [kernel 分派（749-822 行）](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/_fwd_d512_sm90.py#L749-L822)，确认 WG2 调用 `mma_wg2` 时**没有**传 `tiled_mma_qk`（因为它不参与 QK），只传了 PV 相关的 MMA。

**需要观察的现象 / 预期结果**：你会看到 WG2 的实参列表明显比 WG1 短——它只做 PV-后半，不需要 Q、K、softmax、LSE。这正是「3-role 非对称」在调用签名上的直接体现。

> 结果标注：源码阅读型实践，不触发 GPU。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `tile_n` 在构造器里被强行覆盖成 32，而不是用上层默认的 128？

**参考答案**：D=512 时单块 K 的体积是 `tile_n × 512`。若 `tile_n=128`，一块 K 就是 128×512，再要双缓冲（`num_stages_k=2`）会撑爆单 SM 的 228KB SMEM 预算，无法跑真正的多级流水线。把 `tile_n` 降到 32 让 K 块足够小，从而 `num_stages_k=2` 成立、K 的加载能与 WGMMA 真正重叠。这是 D 越大、N 块越小的典型 SRAM 预算权衡。

**练习 2**：WG1 的 PV-前半用了 `tiled_mma_pv_wg1`（RS 模式，A 来自寄存器），而 WG2 的 PV-后半用 `tiled_mma_pv`（SS 模式，A 来自 SMEM）。为什么不让两者都用 RS 模式？

**参考答案**：P 是 WG1 自己算 softmax 得到的、天然在它的寄存器里，所以 WG1 直接用 RS 模式省一次 SMEM 描述符读取（注释实测 +3 TFLOPS）。但 WG2 不算 softmax，P 必须经 SMEM（`sP`）从 WG1 传过来，所以 WG2 只能走 SS 模式从 SMEM 读 P。两个 WG 数据来源不同，MMA 模式也就不同——这是「跨 WG 握手」带来的不可避免的非对称。

---

### 4.3 FFPAAttnFwdSm90SplitDGeneric 与 D384Aware：泛化（填充）与真 tile

#### 4.3.1 概念说明

D512 基线只认 `head_dim == 512`。对 `[320, 512]` 区间内其余的 D，需要两条泛化路线：

- **`FFPAAttnFwdSm90SplitDGeneric`**（泛化填充）：把逻辑 D 当成「真数据 + 填充」，物理 tile 仍用 512，让 D512 kernel 原样跑。TMA 的 OOB 语义负责把超出逻辑 D 的填充位置在**加载时零填**、在**存储时丢弃**。**零新 kernel 代码**，只改构造器把 `tile_head_dim` 钉死成 512、并记下逻辑 D 供 OOB 判定。
- **`FFPAAttnFwdSm90SplitDD384Aware`**（真 384 tile）：当 `D≤384` 时，填充到 512 会浪费最多 1/4 的资源，于是**收窄物理 tile 到 384**。它继承 Generic、复用全部流水线逻辑，只覆盖几个 `tile_hdim*` 属性。

二者都靠一个关键机制——`check_hdim_oob`：当逻辑 D 小于物理 tile 宽度时，kernel 内部需要知道「这次加载/存储要不要做 OOB 处理」。父类 D512 里 `head_dim == tile_hdim` 时该标志为假，分支被编译期消除；子类把它置真。

#### 4.3.2 核心流程

Generic 的核心是一个**永远返回 512** 的辅助函数：

[src/ffpa_attn/cute/_fwd_generic_sm90.py:17-28](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/_fwd_generic_sm90.py#L17-L28) —— `_generic_tile_head_dim` 校验对称性与区间后，无条件 `return SM90_SUPPORTED_HEAD_DIM`（即 512）。

Generic 类用这个函数把物理 tile 钉死，再调父类 D512 的构造器，同时把**逻辑** head_dim 单独存下来：

[src/ffpa_attn/cute/_fwd_generic_sm90.py:31-68](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/_fwd_generic_sm90.py#L31-L68) —— 关键两行：`tile_head_dim = _generic_tile_head_dim(...)` 拿到 512；`self.logical_head_dim = head_dim` 记下真值；然后 `super().__init__(dtype, tile_head_dim, head_dim_v=tile_head_dim, ...)`。

D384Aware 在 Generic 基础上**收窄**物理 tile：

[src/ffpa_attn/cute/_fwd_d384_sm90.py:17-67](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/_fwd_d384_sm90.py#L17-L67) —— 校验 `320 ≤ head_dim ≤ 384` 后，调 `super().__init__(...)`（即 Generic，Generic 又调 D512），**最后再覆盖**几个属性：

```python
self.tile_hdim = D384_AWARE_HEAD_DIM        # 384
self.tile_hdimv = D384_AWARE_HEAD_DIM       # 384
self.tile_hdimv_half = D384_AWARE_HEAD_DIM // 2   # 192
self.tile_hdim_full = D384_AWARE_HEAD_DIM   # 384
self.check_hdim_oob = head_dim != self.tile_hdim
self.check_hdim_v_oob = logical_head_dim_v != self.tile_hdimv
```

> 注意一个有趣的点：D384Aware 把 `tile_hdimv_half` 设为 192（384/2）。这意味着它的 PV-前半/后半各 192 列，而不是 D512 的 256。但 **PV 拆半要求 `tile_hdimv % 256 == 0`**（来自父类 assert，[4.2.3](#423-源码精读)）——这里 192 不满足！实际上 D384Aware 收窄到 384 后，父类那条 `tile_hdimv % 256 == 0` 的断言在它的实例上**不再成立**，因此 D384 路径真正落地时走的是「Generic 填充到 512」的同一套 SMEM/WGMMA 形状（tile_hdimv_half 仍按 256 走），`tile_hdim` 等属性更多服务于 OOB 与逻辑寻址。这层细节属于实现内部，理解「D384Aware 的本意是收窄物理 tile 以省资源」即可。

#### 4.3.3 源码精读

**Generic 的「最小改动」哲学**：它**不重写**任何 `@cute.kernel` 方法，只重写 `__init__`。所有流水线、MMA、producer/consumer 代码全部继承自 D512 原样复用。这是「填充复用」能零成本成立的根本——同一份编译产物，只是 tile 宽度参数变了。

**OOB 的语义**：[Generic 模块 docstring（1-7 行）](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/_fwd_generic_sm90.py#L1-L7) 说清了——「TMA OOB handling zero-fills padded loads and drops padded stores for D below the selected physical tile」。即：逻辑 D=448、物理 tile=512 时，加载 K 的后 64 列（填充位）被 TMA 自动零填，存储 O 的后 64 列被丢弃。数值上 \(Q@K^\top\) 不受影响（因为填充位是 0），开销只是「白搬了 64 列、白算了 64 列的 WGMMA」。

**D384Aware 的校验门禁**：

[src/ffpa_attn/cute/_fwd_d384_sm90.py:42-46](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/_fwd_d384_sm90.py#L42-L46) —— 只接受 `MIN_SUPPORTED_HEAD_DIM(320) ≤ head_dim ≤ 384`，否则抛 ValueError。

#### 4.3.4 代码实践（对应本讲核心实践任务）

**实践目标**：对比 `_fwd_d512_sm90` 与 `_fwd_generic_sm90` 的类结构，列出 d512 特化相对 generic 做了哪些针对 head_dim 的优化假设。

**操作步骤**：

1. 打开 [_fwd_d512_sm90.py](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/_fwd_d512_sm90.py)，浏览 `FFPAAttnFwdSm90SplitD` 的方法清单：`__init__`、`_get_tiled_mma`、`_get_shared_storage_cls`、`__call__`、`kernel`、`producer`、`mma_wg1`、`_mma_wg1_one_n_block`、`mma_wg2`、`_mma_wg2_one_n_block`、`epilogue_wg`、`apply_score_mod`——这是功能全集。
2. 打开 [_fwd_generic_sm90.py](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/_fwd_generic_sm90.py)，确认 `FFPAAttnFwdSm90SplitDGeneric` **只**重写了 `__init__`，没有任何 `@cute.kernel` / `@cute.jit` 方法。
3. 列一张表，归纳 D512 特化相对 Generic「多承担了哪些 head_dim 假设」。

**需要观察的现象 / 预期结果**：你的表大致应包含以下几条 D512 专有的 head_dim 假设（Generic 因复用而全部继承，但这些假设的**数值**只在 D=512 时「正好」最优）：

| 维度 | D512 特化的假设 | Generic 如何处理 |
|---|---|---|
| QK 是否沿 D 切片 | 否——全 D 单次 WGMMA（`tile_hdim_full=512` 一次装下） | 物理仍 512，逻辑 D<512 时填充位零填 |
| `tile_n` | 硬覆盖为 32（D=512 下 K 块大，需小 N 才能双缓冲） | 继承同样的 32 |
| `tile_hdimv % 256 == 0` | assert 强制（PV 拆 `(1,2,1)` 两半各 256） | 物理仍 512 满足；逻辑 D 不影响 |
| `tile_m == 64` | assert 强制（单 M-atom WGMMA） | 继承 |
| OOB 处理 | `check_hdim_oob=False`（D==tile，分支编译期消除） | `check_hdim_oob=True`（需运行期 OOB 判定，逻辑 D<512） |
| 逻辑 head_dim 记录 | 不需要（逻辑=物理=512） | 额外存 `self.logical_head_dim` |

结论一句话：**Generic 不是「另一套实现」，而是「D512 实现的 OOB-aware 包装」**——它把 D512 那套针对 512 优化的流水线，借 TMA OOB 语义推广到任意 \([320,512]\) 的 D，代价是填充位的带宽/算力浪费与 OOB 分支开销。

> 结果标注：源码阅读型实践，不触发 GPU。上表为基于源码的归纳，非项目自带文档。

#### 4.3.5 小练习与答案

**练习 1**：Generic 把逻辑 D=448 填充到物理 512，数值结果会和「真 448 tile」一致吗？为什么？

**参考答案**：一致。因为填充位被 TMA 零填，\(Q@K^\top\) 中填充列贡献为 0，softmax 与 PV 都不受影响；存储时填充列被丢弃，输出形状仍是逻辑 D。差异只在**性能**（白搬/白算了 64 列）而非**数值**。

**练习 2**：既然 D384Aware「收窄物理 tile 省资源」，为什么不让所有 D 都走「真 tile 特化」，反而保留 Generic 填充？

**参考答案**：因为每加一个真 tile 类就要多维护一套 atom/布局/fragment/编译产物，工程与编译时间成本高。D=384 是高频形状、值得特化；但 448、480 这类长尾形状若各写一份真 tile，收益（省几十列浪费）盖不过维护成本，不如统一填充到 512。这是「高频形状特化、长尾形状填充」的经典工程权衡。

---

### 4.4 FFPAAttnBwdDKDVSm90SplitD：D512 反向 dK/dV（2-pass D-split + 持久化）

#### 4.4.1 概念说明

反向不像前向那样一个 kernel 算全部梯度，而是**拆成两个独立 kernel**：

- `FFPAAttnBwdDKDVSm90SplitD`（本文件）：只算 dK、dV。
- `FFPAAttnBwdDQSm90SplitD`（[_dq_d512_sm90.py](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/_dq_d512_sm90.py)）：只算 dQ。

为什么拆？因为 dK/dV 与 dQ 的「所有权（ownership）」不同（参见 [u5-l2](u5-l2-dkdv-dq-shared-pid.md) 与 [u5-l3](u5-l3-decode-bwd-dq-reduce.md) 的 Triton 对照）：dK/dV 的下标是 KV 位置，dQ 的下标是 Q 位置，二者沿不同的轴归约，最优的 tile 形状、循环嵌套与持久化策略都不同。CuTeDSL 选择为它们各写一个 CTA 结构最优的 kernel，而不是强行塞进一个。

D512 的 dKdV kernel 用三条核心设计榨性能：

1. **2-pass D-split**：把输出 D 维（dK/dV 的列）切成两段 `d_chunk=256`，外层 `d_pass` 循环两次，每次产出 256 列。
2. **K/V 持久化（persistence）**：K、V 在每个 work_tile（一个 n_block）开头**一次性**载入 SMEM，跨所有 `d_pass`、所有 Q 头、所有 m_block 复用，避免重复搬运。
3. **双 MMA WG 协作**：WG1 算 \(S\to P\to dV\)，WG2 算 \(dP\to dS\to dK\)，中间用 P 的 SMEM 缓冲做跨 WG 握手。

#### 4.4.2 核心流程

类 docstring 给出了精确的 6 阶段分工：

[src/ffpa_attn/cute/_dkdv_d512_sm90.py:10-33](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/_dkdv_d512_sm90.py#L10-L33) —— 头注释，列了 3 WG 配置、tile 参数与 6 阶段（P1~P6）。

每个 `(n_block, m_block)` 的 6 阶段（数学对应 §2 的反向公式）：

```
WG1 (S/softmax/dV)                  WG2 (dP/dS/dK)
─────────────────────               ─────────────────────
P1: S = Σ_d Q_d @ K_d^T              (等 WG1)
P2: P = exp2(S*scale_log2 - LSE);
    写 sP(bf16) + sP_fp32(fp32)
              ─── sP/sP_fp32 跨 WG 传递 ───→
                                    P3: dP = Σ_d dO_d @ V_d^T
                                    P4: dS = P*(dP - dPsum)*scale;
                                        写 sdS
              ←── sdS 跨 WG 传递（dK 用） ───
P5: dV += P^T @ dO_d_pass            P6: dK += dS^T @ Q_d_pass
```

外层还有 `d_pass` 循环（2 次）与 GQA 的 Q 头循环；K/V 在最外层载入一次后全程复用。每个 `d_pass` 结束，WG1/WG2 分别把 dV/dK 的对应 256 列切片经 `sEpi` 用 TMA 存回显存。

#### 4.4.3 源码精读

**SplitD 参数与持久化**——这是 dKdV kernel 的骨架：

[src/ffpa_attn/cute/_dkdv_d512_sm90.py:108-137](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/_dkdv_d512_sm90.py#L108-L137) —— `d_chunk=256`、`num_d_passes = tile_hdim // d_chunk = 2`、`K_persist_chunks = V_persist_chunks = 2`（即 2 个 256 宽的 K/V chunk 常驻 SMEM）。注意 SMEM 预算权衡的注释：`A_stage` 从 3 降到 2，因为 `d_chunk=256` 让单级体积翻倍，得砍一级才能塞进 228KB。

**双精度 P 通道（bf16-roundtrip-free）**——这是该 kernel 的一个精度巧思：

[src/ffpa_attn/cute/_dkdv_d512_sm90.py:185-197](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/_dkdv_d512_sm90.py#L185-L197) —— 同时维护 `sP`（bf16，给 WG1 自己的 dV WGMMA 用，因为 WGMMA 的 A 操作数必须是 fp16/bf16）和 `sP_fp32`（fp32，给 WG2 算 \(dS=P\odot(dP-D_i)\) 用）。这样 WG2 读到的是原汁原味的 fp32 P，避免了「fp32→bf16→fp32」两次舍入带来的精度损失。

**三角色调度**——与前向对偶，但 WG 分工不同：

[src/ffpa_attn/cute/_dkdv_d512_sm90.py:644-721](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/_dkdv_d512_sm90.py#L644-L721) —— `warp_group_idx==0` 调 `load`（生产者，载 K/V/Q/dO/LSE/dPsum）；`==1` 调 `mma_wg1`（S+P+dV）；`==2` 调 `mma_wg2`（dP+dS+dK）。读这段时对照前向 [4.2.3](#423-源码精读) 的分派，注意反向 WG1 同时承担了「算 S 和 P」与「算 dV」两件事（前向 WG1 是「算 QK+softmax+PV-前半」），因为反向里 P 既是 dV 的输入（\(dV=P^\top dO\)）又是 dS 的输入（\(dS=P\odot\ldots\)）。

**K/V 持久化的生产者侧**——`load` 里 K/V 只在最外层载一次：

[src/ffpa_attn/cute/_dkdv_d512_sm90.py:781-824](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/_dkdv_d512_sm90.py#L781-L824) —— `for d_inner in range_constexpr(num_d_inner)` 把两个 256 宽的 K chunk 载入 `sK_persist`，之后整个 `d_pass` × Q 头 × m_block 三重循环都不再碰 K 的全局内存。这是「持久化」得名之处——K/V 像「常驻嘉宾」一样待在 SMEM 里被反复读。

**dQ kernel 的差异（对照）**：dQ kernel 把循环嵌套**反转**——`n_block` 在外、`d_pass` 在内，并采用「cooperative N-axis split」让 WG1/WG2 各算 dQ 输出 tile 的一半列（`dQ_n_half=128`），从而消除跨 d_pass 的 S/dP 重算（头注释说减少 40% WGMMA）：

[src/ffpa_attn/cute/_dq_d512_sm90.py:14-44](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/_dq_d512_sm90.py#L14-L44) —— 5 阶段（Phase A~E）与反转循环嵌套的设计。

#### 4.4.4 代码实践

**实践目标**：追踪 dKdV kernel 里「K/V 持久化」与「2-pass D-split」如何叠加，理解为什么这套设计对 D=512 高效。

**操作步骤**：

1. 在 [load（生产者）](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/_dkdv_d512_sm90.py#L723-L980) 里找到 K/V 的载入循环（`for d_inner in range_constexpr(self.num_d_inner)`），确认它在「Q 头循环」与「m_block 循环」**之外**——即每个 work_tile 只载一次。
2. 在 [mma_wg1](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/_dkdv_d512_sm90.py#L985-L1146) 里找到 K 的消费（`for _k in range_constexpr(self.K_persist_chunks)` 的 `consumer_wait`），确认它读的是 `sK_persist`（持久缓冲）而非重新发 TMA。
3. 数一下：对一个 n_block，K 被从全局内存搬了几次？被 WGMMA 读了几次？

**需要观察的现象 / 预期结果**：K 从全局内存只搬 **1 次**（2 个 chunk 一起载入 sK_persist）；但在 `d_pass(2) × q_head × m_block` 的全程被 WGMMA 反复读。持久化把「搬运 1 次」摊到「多次读取」上，正是它对 D=512（K 体积大、搬运贵）高效的根源。

> 结果标注：源码阅读型实践，不触发 GPU。

#### 4.4.5 小练习与答案

**练习 1**：为什么 dKdV kernel 要同时维护 bf16 的 `sP` 和 fp32 的 `sP_fp32` 两个 P 缓冲？

**参考答案**：WG1 算 dV 时用 \(dV=P^\top dO\)，这里的矩阵乘（WGMMA）要求 A 操作数是 fp16/bf16，所以 WG1 读 bf16 的 `sP`。但 WG2 算 \(dS=P\odot(dP-D_i)\) 是逐元素乘、希望用全精度 P 避免误差；若让它读 bf16 的 sP 再转回 fp32，会引入「fp32→bf16→fp32」两次舍入。于是 WG1 在写 P 时**同时**写一份 fp32 的 `sP_fp32` 给 WG2 读，用多一块 SMEM 换精度。

**练习 2**：反向为什么不像前向那样用一个 kernel 同时算 dQ/dK/dV，而要拆成 dKdV 与 dQ 两个 kernel？

**参考答案**：dK/dV 与 dQ 沿不同的轴归约、所有权不同，最优 tile 形状与循环嵌套也不同。dKdV 是「Q 流式、K/V 持久化」，dQ 是「K 流式、Q 持久化」且循环嵌套反转以消除重算。强行塞进一个 CTA 会迫使其中一组梯度用次优布局。CuTeDSL 的编译期形状特化也让「两个各最优的 kernel」比「一个折中 kernel」更划算——多一次 launch 的开销远小于换来的算力。

---

### 4.5 SM80 generic 兜底与选型矩阵

#### 4.5.1 概念说明

SM90 专用 kernel 的覆盖面是有限的：只 Hopper、只对称 D∈[320,512]。其余所有合法情况——非 Hopper（SM80/SM89/SM100/SM120…）、或 Hopper 上 D>512——一律落到 **SM80 generic Split-D 兜底路径**（[_fwd_generic_sm80.py](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/_fwd_generic_sm80.py)、[_ffpa_fwd_sm80.py](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/_ffpa_fwd_sm80.py)）。

它与 SM90 专用的根本差异是**指令栈**：

- SM90 专用：Hopper 的 **WGMMA + TMA**，warpgroup 级大矩阵乘 + 异步描述符搬运，需要 SM90a。
- SM80 generic：Ampere 的 **warp 级 MMA + cp.async**，前向兼容到 SM89/SM90/SM120，沿 D 以 32 列切片（`SM80_FWD_SPLIT_D_CHUNK=32`），故要求 `D % 32 == 0`。

SM80 generic 是「不挑架构、不挑 D（只要 ≤1024 且 %32==0）」的万能钥匙，性能不如 SM90 专用，但覆盖面最广。它也用 Split-D（参见 [u1-l1](u1-l1-what-is-ffpa-split-d.md)），只是粒度和指令不同。

#### 4.5.2 核心流程

路由规则只有一条（[README:127-131](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/README.md#L127-L131)）：

- `major == 9` 且对称 `head_dim ≤ 512` → SM90 专用（再按 §4.1 细分 d512/d384/generic）。
- 其余 → SM80 generic。

本讲的 §4.1~§4.4 全部发生在「SM90 专用」分支内部；SM80 generic 是它外面的兜底。

#### 4.5.3 源码精读

**SM80 兜底的 head_dim 上限与切片粒度**：

[src/ffpa_attn/cute/_utils.py:31-56](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/_utils.py#L31-L56) —— `SM80_SUPPORTED_HEAD_DIM=1024`、`SM80_FWD_SPLIT_D_CHUNK=32`，以及反向 dkdv/dq 的 tile 与多级参数。

**SM80 路径的校验**：

[src/ffpa_attn/cute/_utils.py:134-152](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/_utils.py#L134-L152) —— `_validate_sm80_head_dims` 要求对称 head_dim、落在 \([dense\_min, 1024]\)、且 `head_dim % 32 == 0`。注意它的注释明确说：这条路径覆盖「Hopper 上 D>512」与「所有 Blackwell」。

**架构校验的两套门**：

[src/ffpa_attn/cute/_utils.py:209-244](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/_utils.py#L209-L244) —— `_validate_sm90_arch` 要求 Hopper 且 CuTeDSL 选了 `sm_90a`（因为要发 WGMMA/TMA/setmaxnreg）；`_validate_sm80_arch` 只要 `≥8.0`。这解释了为什么 SM90 专用「挑架构」而 SM80 generic「不挑」——前者依赖 Hopper 独占指令。

#### 4.5.4 代码实践

**实践目标**：把本讲涉及的所有 kernel 路径放进一张选型矩阵，能在给定 `(arch, head_dim)` 后秒答「走哪条」。

**操作步骤**：

1. 准备一张表，列为：架构、head_dim 区间、前向路径、反向路径、关键约束。
2. 填入以下 6 个用例并给出路径：
   - (Hopper, D=512)
   - (Hopper, D=384)
   - (Hopper, D=448)
   - (Hopper, D=640)
   - (A100/SM80, D=512)
   - (Blackwell/SM120, D=512)

**需要观察的现象 / 预期结果**：

| 架构 | head_dim | 前向路径 | 反向 dKdV/dQ 路径 | 关键约束 |
|---|---|---|---|---|
| Hopper (SM9.x) | 512 | `FFPAAttnFwdSm90SplitD` | `...DKDV/DQ...Sm90SplitD` | `tile_m=64, tile_n=32` |
| Hopper | 384 | `...D384Aware` | `...D384`（true-tail 256+128） | `256 < D ≤ 384` |
| Hopper | 448 | `...Generic`（填充到 512） | `...Generic`（填充） | OOB 零填/丢弃 |
| Hopper | 640 | SM80 generic | SM80 generic | `D % 32 == 0` |
| A100 (SM80) | 512 | SM80 generic | SM80 generic | 非 Hopper 即兜底 |
| Blackwell (SM120) | 512 | SM80 generic | SM80 generic | WGMMA 栈不支持，见 README Blackwell 说明 |

> 结果标注：本表基于 [_utils.py](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/_utils.py) 与 [README](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/README.md) 的路由规则归纳，非项目自带表格。Blackwell 行参考 README 的「Blackwell / SM120 investigation note」——当前 Hopper warpgroup 栈无法在 Blackwell 上跑，需另写 tcgen05 栈。

#### 4.5.5 小练习与答案

**练习 1**：一台 Hopper 机器上跑 D=768 的注意力，会走 SM90 专用还是 SM80 generic？为什么？

**参考答案**：走 SM80 generic。因为 `_use_sm90_specialized` 要求对称 `head_dim ≤ 512`；768 超过 `SM90_SUPPORTED_HEAD_DIM=512)`，即便在 Hopper 上也会被甩到 SM80 generic 兜底（要求 `768 % 32 == 0`，成立）。SM90 专用 kernel 的物理 tile 焊死在 ≤512，无法承接 768。

**练习 2**：SM80 generic 与 SM90 专用都用 Split-D，二者的「Split」有何不同？

**参考答案**：粒度与指令不同。SM80 generic 用 Ampere 的 warp 级 MMA + cp.async，沿 D 以 **32 列**切片（`SM80_FWD_SPLIT_D_CHUNK=32`）；SM90 专用用 Hopper 的 WGMMA + TMA，D=512 时甚至**不沿 D 切**（全 D 单次 WGMMA），只有 PV 沿 N 拆两半，反向才沿 D 以 **256 列**大粒度切（`d_chunk=256`）。同样是「把 D 方向压力转移出 SRAM」，SM80 用细粒度多次小 MMA，SM90 用粗粒度少次大 WGMMA。

---

## 5. 综合实践

**任务**：为一个「假想的」新 head_dim=448 写一份「FFPA 该如何接管它」的分析报告，把本讲全部知识点串起来。

要求在报告中回答：

1. **路由判定**：D=448、对称、跑在 Hopper 上，`_use_sm90_specialized` 是否放行？放行后落在 §4.1 的哪一类？（提示：看 [_ffpa_fwd_sm90.py:193-198](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/_ffpa_fwd_sm90.py#L193-L198)）。
2. **物理 tile 选择**：这一类把物理 tile 设成多少？逻辑 448 与物理 tile 的差由什么机制兜底？（提示：看 [_fwd_generic_sm90.py:17-28](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/_fwd_generic_sm90.py#L17-L28) 与 OOB 语义）。
3. **反向**：反向 `bwd_kernel_kind` 取哪个字符串？dKdV 与 dQ 各选哪个类？（提示：看 [_ffpa_bwd_sm90.py:314-363](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/_ffpa_bwd_sm90.py#L314-L363)）。
4. **性能预期**：相对 D=512 专用 kernel，D=448 这条路多了哪些开销？如果 448 变成高频形状，按本讲的设计哲学该怎么优化？（提示：参照 D384Aware 的做法）。
5. **边界对照**：同样的 D=448 跑在 A100 上，路径变成什么？为什么？（提示：看 §4.5）。

**预期产出**：一份不超过一页的 Markdown，含上述 5 个问题的明确答案，并附一条「如果要为 448 写真 tile 特化，需要新增/修改哪些文件」的清单（参照 [_fwd_d384_sm90.py](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/_fwd_d384_sm90.py) 与 [_dkdv_d384_sm90.py](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/_dkdv_d384_sm90.py) 的模式）。

> 结果标注：本实践为源码阅读 + 设计分析型，不触发 GPU，无需运行命令。所有结论应基于本讲引用的真实源码行号。

## 6. 本讲小结

- CuTeDSL 在 SM90 上**按 head_dim 写多份专用 kernel**，因为 TMA atom 与 WGMMA fragment 形状是编译期常量；高频形状（512/384）各写一份榨干性能，长尾形状（如 448）用 generic 填充复用。
- **前向类继承链**：`FFPAAttnFwdSm90SplitD`（D512 真实现）→ `...Generic`（填充到 512，只重写 `__init__`）→ `...D384Aware`（收窄物理 tile 到 384）。Generic 不是另一套实现，而是 D512 实现的 OOB-aware 包装。
- **D512 前向**用「1 TMA 生产者 WG + 2 MMA 消费者 WG」的 3-role 流水线：QK 全 D 单 WGMMA、PV 沿 N 拆前后两半（各 256 列）由 WG1/WG2 并行；硬假设 `tile_m=64`、`tile_n=32`、`tile_hdimv%256==0`。
- **反向拆成 dKdV 与 dQ 两个 kernel**，因为二者所有权与最优循环嵌套不同；D512 dKdV 用 2-pass D-split（`d_chunk=256`）+ K/V 持久化（载一次、跨 d_pass/Q 头/m_block 复用）+ 双 MMA WG 6 阶段协作，并用 `sP`/`sP_fp32` 双通道避免精度损失。
- **D384 特化**走 true-tail 设计（反向 pass 0 是 256 宽全块、pass 1 是 128 宽真尾块），避免填充浪费；代价是为 full/tail 两条路径各维护一套 atom/布局/MMA。
- **SM80 generic 是万能兜底**：非 Hopper 或 D>512 全落这里，用 Ampere 的 warp MMA + cp.async、D 以 32 切片、上限 1024、要求 `D%32==0`；SM90 专用与它的分界只由 `_use_sm90_specialized`（Hopper 且对称 D≤512）一刀切。

## 7. 下一步学习建议

- **反向另一半**：本讲只精读了 dKdV，建议接着读 [_dq_d512_sm90.py](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/_dq_d512_sm90.py)，重点看它「循环嵌套反转（n_block 外、d_pass 内）」与「cooperative N-axis split（dQ_n_half=128）」如何消除跨 d_pass 的 S/dP 重算。
- **SM80 generic 内部**：读 [_fwd_generic_sm80.py](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/_fwd_generic_sm80.py) 与 [_dkdv_generic_sm80.py](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/_dkdv_generic_sm80.py)，对照本讲理解「Ampere 栈的 Split-D」与「Hopper 栈的 Split-D」在切片粒度与指令上的差异。
- **调度与同步层**：若对 3-role 流水线的 barrier/mbarrier 细节意犹未尽，回到 [u6-l3](u6-l3-tile-scheduler-pipeline.md) 读 `PipelineTmaAsync`、`PipelineAsync` 与 `NamedBarrierFwd/Bwd` 的实现。
- **二次开发**：若想新增一个 head_dim 的真 tile 特化（如综合实践里的 448），参照 [_fwd_d384_sm90.py](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/_fwd_d384_sm90.py) 与 [_dkdv_d384_sm90.py](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/_dkdv_d384_sm90.py) 的 true-tail 模式，并复习 [u9-l4](u9-l4-extension-guide.md) 的扩展清单。

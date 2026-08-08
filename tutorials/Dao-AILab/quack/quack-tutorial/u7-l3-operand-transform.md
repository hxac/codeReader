# A 算子变换：dequant、dropout

## 1. 本讲目标

本讲讲解 QuACK GEMM 中**对称于 epilogue 的另一条扩展轴**——A 操作数变换（A-operand transform）。读完本讲，你应当能够：

- 说清「为什么 dequant 是 A 侧变换而不是 epilogue」这个核心架构判断背后的数学依据。
- 读懂 `transform.py` 里的 `TransformA` 声明式契约、`copy_block` 这个唯一接缝（seam），以及 `TransformAW4` / `TransformAValue` / `TransformADropout` 三种内核侧实现。
- 理解 `kinds.py` 的 KIND 分类法如何把「运行期操作数」也做成一个对称于 EpiOp 的体系。
- 掌握 `@a_transform` 函数式前端、`w4_transform` / `dropout_a` 把手（handle）与 `TransformModBase` 协议。
- 理解 `host.py` 如何把任意 handle 归一化成 mod、如何用同一份 blob/strip 几何同时生成运行期视图和 trace 期 fake，以及 `pick_w4_cfg` 这套「实测出来的」W4 配置规则。

本讲依赖 [u5-l1 GemmBase 共享主循环与 epilogue 驱动](u5-l1-gemm-base.md)：你需要已经知道 mainloop（主循环）、accumulator（累加器）、epilogue、warp group、AB pipeline 这些概念。本讲会把同样的设计哲学（声明式契约 + 固定调度时刻 + 一处实现服务运行期与 trace）搬到 MMA 的**输入侧**。

---

## 2. 前置知识

### 2.1 一句话定位：epilogue 在 D 之后，operand transform 在 A 之前

矩阵乘的核心是规约：

\[ D[m,n] = \sum_{k} A[m,k] \cdot B[n,k] \]

epilogue 改造的是**结果** \(D\)（位于 \((M,N)\) 空间），发生在所有 MMA 指令累加完之后；而本讲的 A 操作数变换改造的是**输入** \(A\)（位于 \((M,K)\) 空间），发生在每一条 MMA 指令吃进 A 片段**之前**。一个在乘法之后、一个在乘法之前——这是两条完全对称的扩展轴。

为什么需要单独搞一条 A 侧的轴？因为有些操作**无法**搬到 epilogue：

- **反量化（dequant）**：权重以 4-bit 压缩形式存储，MMA 指令吃的是真实 bf16/fp8 数值，必须先把压缩比特展开成数值再喂给张量核心。这发生在乘法之前、且依赖于 A 自己的比特。
- **per-k-group 缩放**：缩放因子沿 K 轴变化（块缩放量化），不能整体提到求和号外面。
- **dropout 掩码**：掩码是 \((m,k)\) 的函数，必须逐元素作用到 A 上。

但有一类操作**能**搬到 epilogue：**与 K 无关的 per-row 缩放**（k-invariant colvec）。这正是「什么时候用 A 侧变换、什么时候用 epilogue」的分界线（详见 4.1.1）。

### 2.2 你需要先记住的术语

| 术语 | 含义 |
|---|---|
| `copy_block` | 主循环每算一个 k16 块就调用一次的「生产」接缝；默认是 ldmatrix 的 s2r 加载 |
| RS mainloop | register-sourced 主循环：A 片段从寄存器来（SM90 的 WGMMA、SM120 的 warp MMA） |
| `owns_a_layout` | 变换是否「拥有」A 的存储布局（mA 是重打包的 blob，而非普通 (M,K) 张量） |
| aux 操作数 | 搭 A 侧 AB pipeline「顺风车」的额外操作数（如 SF strip），随 A 一起 TMA 进 smem |
| KIND 分类法 | 运行期操作数在变换 \((M,K)\) 空间上的分类，对称于 EpiOp 在 \((M,N)\) 空间上的分类 |
| mod | 一个可调用对象 `gemm -> TransformA`，作为 `transform_a=` 传入 |
| bundle | `TransformAOperand(blob, sf)`，在 mA 槽里**作为一个参数**跨过内核边界 |

---

## 3. 本讲源码地图

本讲涉及的关键文件，全部位于 `quack/operand_transform/` 子包下：

| 文件 | 层次 | 作用 |
|---|---|---|
| [transform.py](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/operand_transform/transform.py) | 内核侧 | `TransformA` 契约 + `TransformAW4/Value/Dropout` 三种实现 + `AuxOperandA` 协议 + `TransformAOperand` bundle |
| [kinds.py](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/operand_transform/kinds.py) | 内核侧 | 运行期操作数的 KIND 分类法（strip 家族 + seed），对称于 EpiOp 词汇表 |
| [frontend.py](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/operand_transform/frontend.py) | 前端 | `@a_transform` 装饰器、`w4_transform` / `dropout_a` 把手、`TransformModBase` 协议 |
| [host.py](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/operand_transform/host.py) | 主机侧 | `as_transform_mod` 归一化、bundle 构造、W4 blob/strip 几何、`pick_w4_cfg` 配置规则 |
| [formats/\_\_init\_\_.py](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/operand_transform/formats/__init__.py) | 格式 | `DecodeFormat` 基类 + `W4_FORMATS` 注册表（nvfp4/int4/int4sm/qtip…） |
| [formats/qtip.py](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/operand_transform/formats/qtip.py) | 格式 | QTIP 无查表 4-bit 解码格式（练习重点） |
| [gemm_sm90.py](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_sm90.py) | 内核 | mainloop 消费 `copy_block` 接缝、`canonical_a_load` 默认生产 |
| [gemm_w4.py](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_w4.py) | API | `gemm_w4a16` / `gemm_w4a8`——A 侧变换之上的薄糖 |

包的 [__init__.py](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/operand_transform/__init__.py) 顶部 docstring 给出了关键的分层注记：`transform.py` / `kinds.py` 是**被 `GemmSm90` 导入的内核侧代码**，而 `frontend.py` 在 host 层**之上**、`host.py` 是 host 层——前两者用 PEP 562 惰性 re-export 避免导入环。记住这条分层，后面所有调用方向都不会乱。

---

## 4. 核心概念与源码讲解

本讲按三个最小模块展开：**内核侧（transform/kinds）→ 前端（@a_transform）→ 主机侧 bundle 与 W4 规则（host）**。

### 4.1 内核侧：copy_block 接缝与 TransformA 契约

#### 4.1.1 概念说明：为什么 dequant 是 A 侧而不是 epilogue

这是本讲最根本的一个判断，值得用数学讲透。GEMM 是：

\[ D[m,n] = \sum_{k=0}^{K-1} A[m,k] \cdot B[n,k] \]

**情形一：与 K 无关的 per-row 缩放** \(u[m]\)（即「整行乘同一个标量」）。

\[ (u \odot A) @ B [m,n] = \sum_{k} u[m]\, A[m,k]\, B[n,k] = u[m] \sum_{k} A[m,k]\, B[n,k] = u[m] \cdot (A@B)[m,n] \]

因为 \(u[m]\) 与求和指标 \(k\) 无关，可以提到求和号外面——也就是说 \((u \odot A)@B = u \odot (A@B)\)。这种线性、k 无关的变换**穿过**了 GEMM，完全可以挪到 epilogue 里用一个 fp32 的 colvec 精确地乘在结果上。QuACK 因此**故意不提供** k 无关的 colvec kind（见 `kinds.py` docstring 与 `frontend.py` docstring 的同一句话）。

**情形二：per-k-group 缩放**（块缩放量化，缩放因子沿 K 轴变化）。

\[ \sum_{k} s(\lfloor k/g \rfloor)\, A[m,k]\, B[n,k] \]

这里 \(s\) 依赖 \(k\)，**不能**提到求和号外，必须随 A 逐 k-tile 一起喂进来。

**情形三：反量化**（A 是压缩比特）。

权重以 4-bit 存储，\(\tilde A[m,k] = \text{decode}(\text{bits})\) 是比特的、逐元素的函数。MMA 指令吃的是展开后的真实数值 \(\tilde A\)，所以解码必须发生在乘法之前、且对每个 \((m,k)\) 元素独立进行。它根本不是「能不能搬到 epilogue」的问题——压缩比特如果不先解码，张量核心压根吃不下。

**结论**：A 侧变换的存在意义，正是承载那些**非线性、依赖 K、或依赖 A 自身比特**、因而无法穿过 GEMM 的操作。epilogue 和 operand transform 各管一头：一个在 \(D\) 之后（\((M,N)\) 空间），一个在 \(A\) 之前（\((M,K)\) 空间）。这条对称性会贯穿本讲——下面的 KIND 分类法就是 EpiOp 词汇表的镜像。

#### 4.1.2 核心流程：copy_block 这个唯一接缝

SM90 的 RS 主循环（`mma_rs_interleaved`）和 SM120 的 warp-MMA 主循环，都**一个 k16 块接一个 k16 块地**生产 A 片段。它们通过一个抽象接缝拿到每个块的生产函数：

```
copy_block(stage_idx, b, k_tile)   # 生产「stage_idx 级、第 b 个 k16 块」的 A 片段
```

- 默认生产是标准的 ldmatrix s2r 加载（`gemm.canonical_a_load`）。
- 一个变换会替换成自己的生产——比如「LDS 原始压缩字 + 在寄存器里反量化」，或「标准加载后再施加一个值函数」。

**关键分工**：变换只负责「生产 A 片段」这一件事；WGMMA 指令的下发、commit-group 纪律（SM90）、pipeline wait，全部仍由主循环拥有。这一点写死在 `TransformA` 的契约 docstring 里。主循环侧在 `gemm_sm90.py` 构造 `copy_block`：

[quack/gemm_sm90.py:1216-1232](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_sm90.py#L1216-L1232) —— 有 `transform_a` 时调用它的 `make_copy_block(...)`，否则回退到 `canonical_a_load`。这段是「变换接入主循环」的唯一点。

然后在 `mma_rs_interleaved` 里反复调用它，把「生产块 b+1」夹在 `WGMMA(b)` 和 `WGMMA(b+1)` 之间（软件流水线）：

[quack/gemm_sm90.py:1752-1756](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_sm90.py#L1752-L1756) —— 首个 k-tile：`copy_block(stage, 0, kt)` 然后 `copy_block(stage, k+1, kt)` 夹着 `wgmma_block(...)`。

该函数的 docstring 把接缝语义讲得最清楚（值得逐字读）：

[quack/gemm_sm90.py:1712-1737](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_sm90.py#L1712-L1737) —— 注意它强调 `k_tile` 是**全局** k-tile 索引（split-k 正确），且「produce 是调用方的；WGMMA 下发和 commit-group 纪律留在这里」。

#### 4.1.3 源码精读：TransformA 契约

[quack/operand_transform/transform.py:86-149](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/operand_transform/transform.py#L86-L149) 是 `TransformA` 基类。它的 docstring 是一整份「声明式契约」——内核永远不知道变换**算了什么**，只知道下面几件事（逐字段对应类属性）：

- `a_major_mode`（默认 K）：layout-owning 变换声明的片段主序。重打包 blob 没有天然主序，必须 canonical in K。
- `tile_k`（默认 None）：需要的 tile_K，否则用内核默认值。
- `owns_a_layout`（默认 False）：为 True 时 mA 不再是普通 (M,K) 操作数（而是重打包 blob），变换**接管** A 的 smem 布局、TMA、gmem 切片；内核跳过基于 (M,K) 的检查、批次旋转与长度推导（M 从 D 推）。
- `aux`（默认 None）：本变换安装的可选 `AuxOperandA`；它的 smem 在 `make_copy_block` 里以 `sAux` 送达。
- `aux_raw`（默认 False）：bundle 的 `sf` 张量**不是** AuxOperandA 的 TMA 操作数，而是一个小的原始 gmem 张量（如 dropout 的 seed），原封不动以 `mAux` 送达——无 smem、无 pipeline。
- `uses_work_tile`（默认 False）：主循环在每个 work-tile 开始时调 `on_work_tile(tile_coord_mnkl)`，让变换刷新每 tile 的寄存器状态（如 dropout 的 per-row RNG 坐标）。
- `promote`（默认 False）：慢累加变换（W4A8）——主循环在每个 k-tile 的 block 0 把 WGMMA 累加器清零、最后一个块后 `wait_group(0)` 排空，再调 `promote_acc(acc_slow, acc_wave, zero_init)` 把这一 k-tile 的 wave 折进持久累加器。

核心方法 `make_copy_block(tiled_mma, sA, tCrA, tidx, warp_group_idx, sAux, mAux)` 返回那个 `copy_block(stage_idx, b, k_tile)` 闭包。`b` 是一个**静态 Python int**（用于寄存器索引）。

bundle 的定义只有两个字段：

[quack/operand_transform/transform.py:49-61](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/operand_transform/transform.py#L49-L61) —— `TransformAOperand`：`blob`（重打包存储）+ 可选 `sf`（aux strip）。它**作为 mA 槽里的一个参数**跨过内核边界——host 层永远不会去拆解 bundle 的内部结构，普通 GEMM 的签名 arity 因此保持不变。这是「mainloop 版的 EpilogueArguments」。

#### 4.1.4 三种内核侧实现一览

基类之下有三个具体子类，覆盖三类典型场景：

**`TransformAW4`——压缩权重反量化**（[transform.py:187-295](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/operand_transform/transform.py#L187-L295)）：

- `owns_a_layout = True`（mA 是离线重打包的 blob）。
- `__init__` 懒加载格式（`decode_format(w4_format)`），断言 `mma_a_dtype` 与格式解码出的 dtype 一致，读取 `promote` / `tile_k` / `sf_words`，并按 arch 与 tile 形状调整 occupancy 与寄存器预算（详见 4.1.5 与 4.3）。
- 它**拥有** A 的 smem 布局、TMA、gmem 切片：[transform.py:298-328](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/operand_transform/transform.py#L298-L328)（`a_bytes_per_stage` / `make_a_smem_layout_staged` / `make_a_tma` / `a_gmem_slice`）。
- `make_copy_block`：[transform.py:344-409](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/operand_transform/transform.py#L344-L409)。每个 k-tile 的 block 0：LDS 原始字 + SF strip 字到寄存器；之后调用 `_decode_block`，由**格式**的 `decode_k16` 产出 4 个打包 bf16x2 寄存器，按片段槽位写入 `tCrA`。解码逻辑由 `self.fmt` 提供，变换本身是格式无关的——这就是 `formats/` 子包存在的理由。
- `promote_acc`（仅 W4A8）：[transform.py:411-426](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/operand_transform/transform.py#L411-L426) 把 k-tile 的 wave 折进 fp32 持久累加器 `acc_slow += scale_row * wave`。

**`TransformAValue`——值函数**（[transform.py:435-525](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/operand_transform/transform.py#L435-L525)）：

- 不拥有布局（A 是普通 16-bit 张量）。先做标准 ldmatrix 加载，再用 mod 的 `fn` 逐 `vec_size` 元素就地施加（在 WGMMA 影子下运行）。
- 函数契约：一个 lane 的 `vec_size` 个片段元素（TensorSSA 向量、片段槽位序、不是 k 连续）进去，等长向量出来。
- `args` 声明的运行期操作数由各 KIND 的 `device_arg` 分级暂存，每个元素取值由 `impl.element(...)` 提供：[transform.py:487-494](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/operand_transform/transform.py#L487-L494)。

**`TransformADropout`——dropout 掩码**（[transform.py:528-643](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/operand_transform/transform.py#L528-L643)）：

- `aux_raw = True`、`uses_work_tile = True`。seed（一个 `(2,)` int64 张量）走 bundle 的 sf 槽**裸传**，无 TMA / 无 smem。
- **mask-only**：只把 keep 掩码 AND 到片段上，**不做** \(1/(1-p)\) 缩放（缩放折进 epilogue）。掩码是 \((m,k,\text{seed},\text{offset})\) 的纯函数——任何内核（dgrad epilogue、wgrad）都能复现同一张掩码，且 split-k 不变（因为接缝的 `k_tile` 是全局的）。
- 每个寄存器一次 PRMT + SET + AND，约 3 SASS 处理 2 个元素，无浮点运算：[transform.py:586-601](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/operand_transform/transform.py#L586-L601)。

#### 4.1.5 aux 操作数协议与 KIND 分类法

变换可以「搭顺风车」塞一个额外的 A 侧操作数，随 A 一起 TMA 进 smem、在同一个 mbarrier 下到达。这是 `AuxOperandA` 协议（[transform.py:64-83](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/operand_transform/transform.py#L64-L83)）：`GemmSm90` 鸭子类型地消费它（`dtype` / `bytes_per_stage` / `make_smem_layout_staged` / `make_tma` / `gmem_slice` / `multicast`）。`AuxKTileStrip`（[transform.py:152-185](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/operand_transform/transform.py#L152-L185)）是字节粒度的 per-(row-block, k-tile) strip——W4 的 SF 字就是它的一个实例。

> 内核消费 aux 的入口在 [gemm_sm90.py:1296-1301](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_sm90.py#L1296-L1301)：`uses_work_tile` 为真时每个 work-tile 调一次 `on_work_tile`。

值函数的运行期操作数则走一个更通用的 **KIND 分类法**（`kinds.py`），它是 EpiOp 词汇表在 \((M,K)\) 空间上的镜像：

[quack/operand_transform/kinds.py:263-270](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/operand_transform/kinds.py#L263-L270) —— `ARG_KINDS` 注册表。注意其中**故意没有** k 无关的 colvec（呼应 4.1.1 的数学结论）。

[quack/operand_transform/kinds.py:38-50](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/operand_transform/kinds.py#L38-L50) —— `strip_geometry` 是「strip 几何的**唯一陈述**」：返回 `(gran_m, g_m, gran_k, g_k, k_inner)`。设备分级、运行期视图、trace fake 都调它，保证三处不会漂移。`k_inner`（更细的轴是否是 K）决定 box 的轴序——因为 TMA 要求每个非内层 stride 满 16 B 对齐，只有更细轴的组数才够大。

每一个 `StripKind`（[kinds.py:183-223](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/operand_transform/kinds.py#L183-L223)）是一个**自管单元**，同时拥有：几何、`device_arg`（内核侧分级 `_StripArg`，[kinds.py:103-181](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/operand_transform/kinds.py#L103-L181)）、`host_view`（运行期 torch 视图）与 `host_fake`（trace 期 fake）。这种「一个 feature = 一个对象包揽所有侧面」正是 EpiOp 的规则（见 u6-l1/l2）。

`_StripArg.on_block` 的精髓：把 smem 里的 (inner, outer, stage) box 用嵌套零步长模式广播到 (tile_M, tile_K)，用片段自己的 `tiled_mma` 分区，再缓存一个片段同构的 rmem 张量（重复值共享寄存器）——所以 staging 全是 select，函数数学保持打包（HMUL2）。

#### 4.1.6 代码实践：跟踪一次 TransformAW4 的 produce

**实践目标**：看清 `TransformAW4` 如何在不改动 WGMMA 调度的前提下，把一个 k16 块「生产」进 A 片段。

**操作步骤**：

1. 打开 [transform.py:344-409](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/operand_transform/transform.py#L344-L409) 的 `make_copy_block`。
2. 在 `copy_block` 闭包里定位 `b == 0` 分支：它做了三件事——(a) `autovec_copy` 把 smem 原始字 LDS 到 `xw`、(b) 从 `sAux_i32` 取 SF strip 字到 `sfw`、(c) 若 `tile_state_words > 0` 调 `fmt.build_tile_state`。
3. 接着无条件调 `_decode_block`（[transform.py:332-342](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/operand_transform/transform.py#L332-L342)），它对每个 m-atom 调 `self.fmt.decode_k16(xw, sfw_or_tstate, b, consts)` 产出 4 个打包寄存器并写入 `frag_i32[(...), m, b]`。
4. 对照 [gemm_sm90.py:1752-1756](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_sm90.py#L1752-L1756)，确认主循环只是按 `b = 0,1,...` 调用这个闭包，再下发 WGMMA。

**需要观察的现象**：变换的 produce（LDS + decode）被夹在两条 WGMMA 之间；WGMMA 的下发、`wait_group`、commit-group 全不归变换管。

**预期结果**：你能画出「smem 字 → xw/sfw 寄存器 → decode_k16 → frag_i32 片段槽位 → WGMMA 消费」这条数据流，且明白这条流里变换只动了「smem 字 → 片段」这一段。

> 本实践为源码阅读型，无需 GPU；若要运行验证，参考 4.3.4 的 W4 roundtrip 实践。

#### 4.1.7 小练习与答案

**练习 1**：`TransformA` 的 `aux` 与 `aux_raw` 有何区别？为什么 dropout 用 `aux_raw` 而 W4 用 `aux`？

> 答案：`aux` 是一个走 AB pipeline 的 `AuxOperandA`——有 smem 缓冲、每 k-tile 一个 TMA box、在 A 的 mbarrier 下到达（如 W4 的 SF strip 字节条带）。`aux_raw` 则是 bundle 的 `sf` 张量**原封不动**作为小 gmem 张量以 `mAux` 送达，无 smem、无 pipeline。dropout 的 seed 只是一个 `(2,)` int64，太小、且只需读一次（不是每 k-tile），所以走 `aux_raw`；W4 的 SF 字节是每 k-tile 都要的不同数据，必须随 pipeline 流式到达，所以走 `aux`。

**练习 2**：用 4.1.1 的求和号论证，解释为何「per-token 激活缩放」\(s[m]\) 可以折进 epilogue，而 W4A8 的 per-k-group 权重缩放不行。

> 答案：per-token 缩放 \(s[m]\) 与求和指标 \(k\) 无关：\(\sum_k (s[m]\,q_{act}[m,k])\,W[n,k] = s[m]\sum_k q_{act}[m,k]W[n,k]\)，所以它穿过 GEMM，作为 D 的 per-output-row（转置后是 colvec）因子在 epilogue 精确施加——这正是 `gemm_w4a8` 用 `ColVecLoad("v")`（见 [gemm_w4.py:149-151](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_w4.py#L149-L151)）的原因。而 per-k-group 权重缩放 \(s(\lfloor k/g\rfloor)\) 依赖 \(k\)，提不到求和号外，必须随 A 逐 k-tile 喂进 mainloop。

---

### 4.2 前端：@a_transform、把手与 TransformModBase 协议

#### 4.2.1 概念说明：函数即组合点，内核管线写一次

`@gemm_epilogue`（u6-l4）让你用一个普通 Python 函数描述「对累加器的逐元素求值」，免去手写 mixin；`@a_transform` 是它在 A 侧的对应物。心法完全一致：**函数是组合点（计算顺序由源码显式写明），内核侧管线（标准 s2r 加载或 blob TMA、交错 produce / WGMMA 调度、fence、commit-group）写一次、永远不由函数作者操心**。

前端用**一个装饰器**承载两族变换：

- **值变换（value，默认）**：解包后的 16-bit A，标准 ldmatrix 加载；函数按 lane、按 `vec_size` 个片段元素被调用，输入是 MMA dtype 的 TensorSSA 向量（片段槽位序），输出等长变换后向量。`vec_size ∈ {2,4,8}`，被一个 k16 块封顶（调度归框架）。
- **打包解码（packed）**：函数**就是** `decode_k16` 的函数体 `fn(xw, sfw, b, consts) -> 4 packed regs`，`PackedInput` 携带几何（w8 / tile_k）与必须一致的 host bundle。mod 会「铸」出一个 `DecodeFormat`，从而能塞进类形式式能去的一切地方。

外加两个专用把手：`w4_transform(fmt)`（给注册格式名或 `DecodeFormat` 实例）和 `dropout_a(p)`（专用掩码变换）。

一个 mod 是一个工厂 `gemm -> TransformA`，可以直接 `GemmSm90(transform_a=mod)` 传进去。

#### 4.2.2 核心流程：从 handle 到内核对象

```
用户传入 transform_a=<handle>
        │  as_transform_mod(handle)        # host.py：归一化
        ▼
   TransformModBase（mod，带 semantic_digest）
        │  mod.__call__(gemm)              # 在内核构造期
        ▼
   TransformA 子类实例（TransformAW4 / Value / Dropout）
        │  make_copy_block(...)            # 接缝
        ▼
   copy_block(stage_idx, b, k_tile)        # 主循环反复调用
```

关键在于：**通用层（gemm_runtime.host、EpiMod、torch_op）从不枚举变换风味**——它们把任意 handle 归一化成 mod，然后调 mod 的统一方法（`owned_fmt` / `plan_key` / `config_ok` / `resolve_operands` / `bundle` …）。layout-owning 与 value 两条分支是 mod 内部的单一陈述。

#### 4.2.3 源码精读：装饰器与三个把手

`@a_transform` 装饰器本身很薄（[frontend.py:520-545](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/operand_transform/frontend.py#L520-L545)），它只是把参数包成 `ATransformMod`。

`ATransformMod`（[frontend.py:323-416](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/operand_transform/frontend.py#L323-L416)）：

- 构造期校验：packed 强制 `vec_size=8`（整 k16 块解码）、且不能有 `consts`/`args`；value 强制 `vec_size ∈ {2,4,8}`。
- `__call__(gemm)`：packed 走 `TransformAW4`，否则走 `TransformAValue`——**这是两条分支的唯一陈述点**（[frontend.py:351-354](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/operand_transform/frontend.py#L351-L354)）。
- `as_decode_format()`：把 packed fn 铸成一个 `_FnFormat(DecodeFormat)` 子类并缓存（[frontend.py:363-392](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/operand_transform/frontend.py#L363-L392)），其 `decode_k16` 直接调用户的 `mod.fn`。
- `__quack_semantic_key__`：fail-closed 地深度指纹函数源码 + 所有 capture + `vec_size` + `consts` + `regs` + `packed_key` + `args`（[frontend.py:394-416](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/operand_transform/frontend.py#L394-L416)）。这个 digest 让 mod 能接入 jit-cache 与 torch.compile 机制。注册到 `TORCH_OP_TRANSFORM_MODS[semantic_digest]` 供 `quack::gemm_epi` 自定义算子解析。

`PackedFormatMod` / `w4_transform`（[frontend.py:438-480](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/operand_transform/frontend.py#L438-L480)）：给注册格式名（按名取 `compile_ref`，磁盘 key 稳定、不传 payload）或 `DecodeFormat` 实例（走本地注册表）的把手。

`DropoutAMod` / `dropout_a`（[frontend.py:483-517](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/operand_transform/frontend.py#L483-L517))：

- `threshold = min(int(round(p * 256)), 255)`，keep 当且仅当随机字节 `>= threshold`，于是 \(P(\text{drop}) = \text{threshold}/256\)。
- `args = (("seed", "seed_i64x2"),)`——声明 seed 为运行期操作数，使通用 host 管线把 seed 当成普通 strip 操作数对待（mod.gemm 解包、trace fake、plan key 全自动）。

#### 4.2.4 源码精读：TransformModBase 协议

[quack/operand_transform/frontend.py:147-321](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/operand_transform/frontend.py#L147-L321) 是所有把手共享的协议基类。最值得记住的几个方法：

- `owned_fmt`（[frontend.py:161-166](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/operand_transform/frontend.py#L161-L166)）：拥有布局时返回 `DecodeFormat`（打包权重），值变换返回 None。整个通用层靠它区分两条分支。
- `config_ok(cfg)`（[frontend.py:177-192](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/operand_transform/frontend.py#L177-L192)）：autotune 时的廉价剪枝——`swap_ab` 永远拒绝；值变换全过；打包变换要求无 pingpong、`cluster_m==1`、`tile_m%64==0`、`tile_k` 匹配。注意它只是「避免浪费一次编译」，真正的几何/内核 assert 仍守正确性（剪错的配置会在 host 校验失败、bench 为 inf）。
- `default_config(A, B)`（[frontend.py:207-237](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/operand_transform/frontend.py#L207-L237)）：值变换返回 None（用调用方的按架构默认）；打包变换走 `pick_w4_cfg` / `pick_w4a8_cfg`（见 4.3）。
- `resolve_operands`（[frontend.py:248-284](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/operand_transform/frontend.py#L248-L284)）：把用户传入的 A/B 规整成内核 mA/mB 槽——值变换要求 A 是 `TransformAOperand` bundle；layout-owning 则把「激活 = caller A、blob = caller B」调换成内核视角，并校验 K 一致、padded N 能被 tile_M 整除。

#### 4.2.5 代码实践：写并验证一个值变换

**实践目标**：写一个最简单的 `@a_transform`，并用 `tests/test_gemm_transform.py` 的「值函数 gate」思路验证它「对片段施加函数」等价于「在 host 上预先缩放 A」。

**操作步骤**（需要 SM90 或 SM120 的 GPU，无 GPU 则做源码阅读）：

1. 阅读 [tests/test_gemm_transform.py:178-196](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/tests/test_gemm_transform.py#L178-L196) 里的四个例子：`_identity2_a`、`_identity8_a`、`_halve_a`（`x * 0.5`）、`_scale_by_const_a`（`consts=lambda: 0.5`）。
2. 注意 gate 的断言（[test_gemm_transform.py:225](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/tests/test_gemm_transform.py#L225)）：`torch.equal(D_fn, D_ss)`——对 2 的幂缩放是**逐位相等**。
3. 自己写一个 `@a_transform(vec_size=4)` 的 `def _quarter_a(x): return x * 0.25`，预测它应与 `ref_scale=0.25` 的 host 预缩放 SS 路径逐位相等。
4. （有 GPU 时）仿照 `_check_value_mod` 跑一次。

**需要观察的现象 / 预期结果**：变换路径的输出与「先在 host 把 A 乘 0.25 再跑普通 mainloop」逐位相同。若换成 `x * 0.3`（非 2 的幂），数值仍接近但**不再逐位相等**——因为片段里的乘法与 host fp32→bf16 的舍入点不同。这正好印证「值变换作用于寄存器片段、而非 host 张量」。

#### 4.2.6 小练习与答案

**练习 1**：为什么 `ATransformMod.__call__` 里 packed 与 value 的分流只用一个 `if self.packed is not None`，而不用 `isinstance`？

> 答案：这是 u6-l4 里 `fn_port` 「只按属性分发、绝不 isinstance 分发」的同一原则。`owned_fmt` / `packed` 都是属性，通用层据此分流转 `TransformAW4` 或 `TransformAValue`。好处是新增一种变换风味（比如未来某个不拥有布局但需要 blob 的格式）只需提供正确的属性，不必改任何 `isinstance` 链。

**练习 2**：`dropout_a(p)` 的语义指纹里包含 `threshold`（即 `round(p*256)`）。为什么把 `p` 离散成 threshold 进 key，而不是把浮点 `p` 直接进 key？

> 答案：因为 keep 判据是 `byte >= threshold`，threshold 是个整数，掩码行为完全由它决定——`p=0.5` 和 `p=0.501` 都映射到 `threshold=128`，产生**逐位相同**的掩码。把离散后的 threshold 进 key，既保证语义正确（相同掩码 → 共享 cubin），又避免把无意义的浮点尾数带进缓存键。

---

### 4.3 主机侧：bundle 构造与 W4 配置规则

#### 4.3.1 概念说明：归一化、bundle 与「一份几何服务两端」

`host.py` 做三件事，让 A 侧变换成为通用 GEMM host 层里的一等公民（对称于 EpiOp 的 host 钩子）：

1. **归一化**：任何 `transform_a=` handle（注册格式名 / `DecodeFormat` 实例 / mod）都被 `as_transform_mod` 归一成带 digest 的 mod；名字和格式实例做 memoize，热路径只是字典查表，绝不重算源码指纹。
2. **bundle 构造**：`transform_a_operand` 把值变换的「原始操作数张量」按 KIND 派发成运行期视图，打包成 `TransformAOperand`；`transform_a_fake_operand` 是它的 trace 期孪生。
3. **W4 几何与配置规则**：W4 的 blob/strip 几何**一份实现**（`_w4_views`）同时服务运行期 torch 视图与 trace 期 fake 张量，使「编译出的布局」与「启动时的布局」**按构造不可能漂移」；`pick_w4_cfg` 把实测出的 tile/split-k 规则集中在一处。

#### 4.3.2 核心流程：值变换 vs layout-owning 的 bundle 构造

```
值变换（带运行期操作数）：
  host.transform_a_operand(mod, A, {"u": strip_tensor}, tile_m, tile_k)
     │  对每个 (name, kind) 调 ARG_KINDS[kind].host_view(A, value, tile_m, tile_k)
     ▼
  TransformAOperand(blob=A, sf=view)        # 一个 aux-delivered 操作数（单 aux 槽）

layout-owning（W4）：
  host.w4_operand_views(fmt, blob, sf, tile_m)
     │  _w4_views(fmt, blob_u8, sf_u8, tile_m)   # 同一函数
     ▼
  TransformAOperand(blob=view_of_blob, sf=view_of_sf_strip)
```

注意「single aux slot」约束：值变换目前最多一个 aux-delivered 操作数（[host.py:273](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/operand_transform/host.py#L273) 与 [transform.py:464](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/operand_transform/transform.py#L464) 两处同名 assert）。

#### 4.3.3 源码精读：归一化与 bundle

`as_transform_mod`（[host.py:50-68](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/operand_transform/host.py#L50-L68)）：str → `_named_w4_mod`（lru_cache）；`DecodeFormat` 实例 → 缓存在实例 `__dict__` 上的 `PackedFormatMod`；已有 `semantic_digest` → 原样返回。

`transform_a_operand`（[host.py:260-274](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/operand_transform/host.py#L260-L274))：校验 `values` 的键集合与 `mod.args` 完全一致、A 是 2-D，然后对每个声明参数调 `ARG_KINDS[kind].host_view(A, values[name], tile_m, tile_k)` 得到视图，断言只有一个（单 aux 槽），打包成 bundle。`tile_m`/`tile_k` **必须与本次启动的 config 匹配**——不匹配会在 trace 期对 KIND 的 fake 失败。

W4 blob/strip 几何 `_w4_views`（[host.py:288-303](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/operand_transform/host.py#L288-L303))：

- 把 blob（字节视图）reshape/permute 成 `(256, wpt, tm64, Gt, Kt, 1)`——一条 256 B 连续的 TMA 内层 run。
- SF strip → `(sfb, tm64, Gt, Kt, 1)`。
- **关键**：它对真张量与 meta 张量都成立，`w4_fake_operands`（[host.py:318-338](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/operand_transform/host.py#L318-L338))复用它造出**精确相同**静态 shape/stride 的 fake 张量。于是「trace 期布局 == 启动期布局」是构造保证的，而非测试保证的。

`w4_padded_n`（[host.py:71-74](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/operand_transform/host.py#L71-L74))：`blob.shape[0] * 64`——blob 的 dim0 数的是 m64 行块，这是「blob 行数规则」的**唯一陈述**。凡是按 M tile 计数的主机缓冲都要用它（与 u5-l3 的 `cta_tile_shape_m` 同源）。

#### 4.3.4 源码精读：W4 配置规则

W4 是「权重带宽受限」的解码型 GEMM，tile/split-k 选择与普通 dense GEMM 不同。`host.py` 把实测规则集中成两个函数：

`pick_w4_cfg(m_act, n_full, k_tiles, ...)` → `(tile_m, tile_n, split_k)`（[host.py:160-236](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/operand_transform/host.py#L160-L236))。核心实测不变量（H100）：

- 把 grid 放到约 112–128 CTA，**用能到达该覆盖的最大 tile**：`tile_m=128` 比 64 在同等 CTA 数下快 10–25%（2× TMA box、每字节的 per-k-tile 流水线开销减半）。
- `tile_n` 是「在 m 上少于半个 tile 的 padding」里最大的那个。
- 串行 split-k 在「每个 split 至少留 ~24 个 k-tile 且 `tile_n<=128`」时补足 grid 覆盖（f32 finalize 往返代价随 tile 面积增长）。
- prefill（m > 256）：H100 用固定 `(128,256,1)`；SM120 用 `_pick_prefill_cfg` 按「波效率 × tile 填充率 / split-k 罚项」打分（[host.py:129-157](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/operand_transform/host.py#L129-L157)），因为 RTX 5090 的 170-SM 在窄 N / 长 K 形状上会被固定 tile 饿死。
- SM120 上 `m<=64` 一律要 128 行 tile（[host.py:199-219](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/operand_transform/host.py#L199-L219)）——170-SM 的部件在远超 H100 的 112 目标后仍奖励更多 CTA。

`pick_w4a8_cfg(m_act, n_full)` → `(tile_m, tile_n, split_k)`（[host.py:239-257](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/operand_transform/host.py#L239-L257))：慢累加（doubled accumulator）翻转了偏好——`m<=128` 一律 64 行 tile（occupancy-2 的 64 行 tile 更能藏 decode+promote 延迟），split-k 总是输（fp32 finalize 往返叠在 promote 上）。

辅助函数 `_wave_eff`（[host.py:111-115](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/operand_transform/host.py#L111-L115))计算「机器忙的平均比例」；`_sms_or_default`（[host.py:88-108](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/operand_transform/host.py#L88-L108))惰性、GPU-blind 地取 SM 数（CPU-only 交叉编译 `QUACK_ARCH` + `CUDA_VISIBLE_DEVICES=""` 时不触发驱动查询，默认 170）。猜错只会选不同 tile，**绝不**会错结果。

> 这些规则被 `TransformModBase.default_config`（4.2.4）和 `gemm_w4` 的显式 tile 表面共同消费。

#### 4.3.5 代码实践：formats/qtip.py 这类 packed-weight 解码格式的作用

**实践目标**：用 `qtip` 这一无查表 4-bit 格式，看清「packed-weight 解码格式」如何作为 `TransformAW4` 的可插拔策略，并对比它与 epilogue 的分工。这是本讲总练习（第 5 节）的预热。

**操作步骤**：

1. 读 [formats/qtip.py:1-30](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/operand_transform/formats/qtip.py#L1-L30) 的 docstring：QTIP（arXiv 2406.11235）是 trellis-coded 4-bit，用**纯计算**的 bf16「3INST」解码——一个线程的 16 字节 LDS 是一条自洽的 tail-biting 比特流，无码本内存。解码公式 `h = s*89226354 + 64248484 (mod 2^32)`，再 `r = (h & 0x81FF81FF) ^ 0x3E003E00`（常量见 [qtip.py:63-66](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/operand_transform/formats/qtip.py#L63-L66)）。
2. 对照 [formats/\_\_init\_\_.py:482-509](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/operand_transform/formats/__init__.py#L482-L509) 的 `Qtip` 类：它的 `decode_k16` 直接调 `Q.decode_qtip_k16`，`sf_words=0`（**无 SF strip**），`prepare` 只做 N 补齐到 128 的倍数。
3. 注意 docstring 末句：「per-tensor weight scale 折进 epilogue alpha」。结合 4.1.1 想想为什么：per-tensor scale 是个标量 \(\alpha\)，与 \(m,k\) 都无关，所以 \((\alpha A)@B = \alpha(A@B)\)，能精确折进 epilogue。这正是 [gemm_w4.py:56-58](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_w4.py#L56-L58) 的 `_w4a16_alpha` epilogue `{"D": acc * alpha}` 的依据。
4. 在 [formats/\_\_init\_\_.py:570-596](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/operand_transform/formats/__init__.py#L570-L596) 的 `W4_FORMATS` 注册表里数一下共有多少格式（答案：15 个，分属 nvfp4/int4 系/int4sm 系/mxfp*/int8/fp8/qtip 系）。

**需要观察的现象 / 预期结果**：

- qtip 的「解码」是寄存器里的整数比特运算（IMAD + funnel shift + mask），不读任何码本 smem——这正是它「fits the register-only `decode_k16` contract」的原因。
- **对比 operand_transform 与 epilogue**：比特解码（非线性、依赖 A 自身比特）**必须**在 A 侧 mainloop 里逐元素做（`decode_k16`）；而 per-tensor 标量缩放（线性、k 无关）**可以**也**应当**留在 epilogue。两者通过 `transform_a=wformat` + `epi_args={"alpha": tensor_scale}` 在 [gemm_w4.py:124-140](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_w4.py#L124-L140) 里**同时**出现在一次 GEMM 调用中——这就是「A 侧变换与 epilogue 是正交可组合的两条轴」最鲜活的例子。

**预期结论**：`formats/qtip.py` 这类 packed-weight 解码格式的作用，是把「如何把压缩比特变成 MMA 能吃的数值」这件事**完全封装进 `decode_k16` 一个方法**，让 `TransformAW4` 的 TMA/smem/片段缓冲管线对所有格式不变；新增格式 = 一个类 + 注册，零内核改动（见 `formats/__init__.py` docstring）。

#### 4.3.6 小练习与答案

**练习 1**：`_w4_views` 同时被 `w4_operand_views`（运行期）和 `w4_fake_operands`（trace 期）复用。这解决了什么潜在 bug？

> 答案：解决了「编译出的 smem/TMA 布局与启动时实际数据的布局不一致」的 bug。如果运行期视图和 trace fake 用两套独立的几何代码，一旦有人改了其中一套忘了同步另一套，编译期假设的 stride 与启动期真实 stride 就会错位，导致 TMA 取错数据——这类 bug 极难复现。让两端口共用同一函数，是把「一致性」从测试保证升级为**构造保证**。

**练习 2**：`pick_w4_cfg` 在 SM120 上对 prefill 形状用打分函数而非固定 tile。为什么 H100 不也打分？

> 答案：H100 的 prefill 规则是当初对着 machete baseline 实测调出来的固定 `(128,256,1)`，而本仓库没有 Hopper 部件来重新测量一个替换规则；SM120（RTX 5090，170 SM）则发现固定 tile 在窄 N/长 K 上会饿死部件，所以专门写了 `_pick_prefill_cfg` 的覆盖打分。打分函数对 SM 数敏感（`_wave_eff` 依赖 sms），而 sms 在 GPU-blind 的交叉编译路径上可能猜错——但猜错只影响选哪个 tile，绝不影响正确性，最坏只是漏掉 `.o` 缓存、在进程内重编一次。

---

## 5. 综合实践：用 A 侧变换 + epilogue 组合还原一次 W4 GEMM

把本讲三条主线串起来：**内核侧接缝 → 前端把手 → 主机 bundle + 配置规则**。

**任务**：阅读 [gemm_w4.py](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_w4.py) 的 `gemm_w4a16`，画出从「用户调用」到「WGMMA 吃到反量化后的 A 片段」的完整链路，并标注哪一步属于本讲的哪个模块。

**操作步骤**：

1. **格式解析**：`fmt = decode_format(wformat)`（[gemm_w4.py:76](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_w4.py#L76))——解析格式名（如 `"qtip2s"`）为 `DecodeFormat` 实例（formats 模块）。
2. **配置选择**：`_pick_w4_cfg(m_act, n_full, k//tk, sm120=..., device=...)`（[gemm_w4.py:87-93](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_w4.py#L87-L93))——host 模块的实测规则。
3. **split-k 工作区**：显式 tile 调用方走 grid-starvation 规则（[gemm_w4.py:106-121](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_w4.py#L106-L121))。
4. **接入 EpiMod**：`_w4a16_alpha.gemm(act, blob, out, epi_args={"alpha": tensor_scale}, transform_a=wformat, transform_sf=sf, tile_M=..., ...)`（[gemm_w4.py:124-140](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_w4.py#L124-L140))。这是「A 侧变换 + epilogue 同时出现」的关键调用点。
5. **追踪 `transform_a=wformat` 这一路**：
   - `as_transform_mod("qtip2s")` → `PackedFormatMod`（4.2.3）。
   - `owned_fmt` 非 None → layout-owning 分支 → `resolve_operands` 把 caller A（激活）/ caller B（blob）调换成内核视角，调 `w4_operand_views` 建 bundle（4.3.3）。
   - 编译期：`TransformAW4.__init__` 读 `fmt.tile_k` / `sf_words` / `promote`，`make_copy_block` 把 `fmt.decode_k16` 编进 cubin（4.1.4）。
   - 启动期：主循环 `mma_rs_interleaved` 反复调 `copy_block`，每个 k16 块在寄存器里解码（4.1.2）。
6. **追踪 `epi_args={"alpha": tensor_scale}` 这一路**：`_w4a16_alpha` 是个 `@gemm_epilogue()`（[gemm_w4.py:56-58](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_w4.py#L56-L58)），alpha 作为标量走 epilogue 的 Scalar EpiOp（u6），在累加器上乘一次——这正是 4.1.1 论证的「per-tensor 标量穿过 GEMM」。

**需要观察的现象 / 预期结果**：

- 你应当得到一张图，其中 **A 侧**（比特解码）与 **epilogue 侧**（标量 alpha）是两条独立的、正交组合的路径，最终汇入同一次 GEMM。
- 两条路径共享同一套 plan/jit/异步编译缓存（见 `gemm_w4.py` docstring：「W4 kernels share the plan cache, jit/disk cache, async compile, and EpiOp argument machinery with every epilogue variant」）——因为它们都只是 `quack::gemm_epi` 自定义算子的不同配置。

**若无法运行**：本实践为源码阅读型，画出的链路图即是产出。如需数值验证，参考 `tests/test_gemm_w4.py` 的 roundtrip fixture（它对每个注册格式跑 `quantize_reference` → `prepare` → GEMM → `dequant_reference` 比对），其中 qtip* 的 `tensor_scale` 由 fixture 喂入 alpha。

---

## 6. 本讲小结

- **A 侧变换与 epilogue 是对称的两条扩展轴**：epilogue 在 MMA 之后改 \(D\)（\((M,N)\) 空间），operand transform 在 MMA 之前改 \(A\)（\((M,K)\) 空间）。KIND 分类法正是 EpiOp 词汇表在 \((M,K)\) 上的镜像。
- **为什么 dequant 是 A 侧**：非线性、依赖 K、或依赖 A 自身比特的操作**无法穿过** GEMM 求和号；只有与 K 无关的线性 per-row 缩放能折进 epilogue（故 KIND 故意不含 k 无关 colvec）。
- **唯一的内核接缝是 `copy_block`**：主循环按 k16 块反复调用它生产 A 片段，变换只管「生产」、WGMMA 下发与 commit-group 纪律归主循环。`TransformA` 的字段（`owns_a_layout`/`aux`/`aux_raw`/`uses_work_tile`/`promote`）是一份声明式契约。
- **三种内核实现**：`TransformAW4`（压缩权重反量化，拥有布局）、`TransformAValue`（值函数，标准加载后施加 fn）、`TransformADropout`（philox 掩码，mask-only，seed 裸传）。aux 操作数协议让额外 A 侧数据搭 AB pipeline 顺风车。
- **前端把变换写作普通函数**：`@a_transform`（值/packed 两族）+ `w4_transform`/`dropout_a` 把手 + `TransformModBase` 协议；通用层只按属性（`owned_fmt`/`packed`）分发，从不 `isinstance`；语义指纹让 mod 接入 jit-cache 与 torch.compile。
- **主机侧一份几何服务两端**：`as_transform_mod` 归一化、`transform_a_operand` 建 bundle、`_w4_views` 让运行期视图与 trace fake 共用同一函数（构造保证不漂移）、`pick_w4_cfg` 集中 W4 的实测 tile/split-k 规则；`gemm_w4` 只是这条路径上的薄糖。

---

## 7. 下一步学习建议

- **接 u7-l2（量化 GEMM 输出与 W4 权重）**：那一讲从「输出量化」与「W4 权重」的产品视角讲，本讲从「变换架构」视角讲同一套 `gemm_w4` 机制。两讲对照阅读能形成完整闭环。
- **接 u7-l1（Blockscaled 操作数与格式）**：本讲的 `sf_words` / SF strip 与 u7-l1 的块缩放 SF 布局是同一概念在不同层（A 侧变换 vs 主机侧容器）的投影；`colvec_k16/32/64` 这些 dense blockscaled-SF 颗粒度直接对应 u7-l1 的 `sf_vec_size`。
- **回看 u6 全系列**：本讲的 KIND 分类法、`@a_transform` 函数式前端、语义指纹、minted class、`compile_ref` 跨进程解析，全部是 u6 epilogue 体系在同一架构下的 A 侧复刻。把 u6 与本讲并排，能看清 QuACK「mainloop 两侧对称可组合」的整体设计。
- **继续阅读源码**：想深入 dropout 的 philox 推导读 `quack/operand_transform/rng.py`；想看 15 种格式各自的解码数学读 `quack/operand_transform/formats/__init__.py` 各类与 `quack/blockscaled/nvfp4_utils.py`（`decode_*` / `repack_*`）；想看 mainloop 如何消费 promote 与 aux 读 `quack/gemm_sm90.py` 的 `mma_rs_interleaved`（1712 行起）与 `mma`（SM120）。
```

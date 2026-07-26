# tvm.tl 变体与卷积（AMD/HIP）

## 1. 本讲目标

本讲把视线从 NVIDIA 的 CUDA 内核转向 AMD 的 ROCm/HIP 平台，聚焦 `cdna_benchmark/conv_benchmark/` 下的卷积内核。学完后你应该能够：

1. **区分两种 API 入口**：看一眼文件头几行的 `import`，就能判断这个内核走的是「独立 `tilelang` 包」还是「TVM 内置的 `tvm.tl` 变体」，并理解 `profiler="tvm"`、`target="hip"` 的来历。
2. **看懂 im2col 的内核内实现**：理解为什么这个卷积内核长得跟块级 GEMM（u3-l9）几乎一模一样，以及它如何用 `T.Buffer` 重排 + `in_bound` 掩码把卷积「伪装」成矩阵乘。
3. **手推边界索引**：能够对任意输出行 `m`、归约列 `k`，算出 `access_h/access_w/in_bound`，并据此解释 padding、stride 是如何被实现的。
4. **建立跨框架、跨架构的坐标**：知道同一套算子（卷积）在不同基线（tvm.tl / Ladder / torch）和不同后端（HIP / CUDA）下如何落地。

> 本讲是第 6 单元「高级机制」的一篇，承接 u3-l9 块级 GEMM 五要素。如果你对 `T.Kernel / alloc_shared / alloc_fragment / T.copy / T.gemm / T.Pipelined` 还不熟悉，建议先读 u3-l9。

## 2. 前置知识

在进入源码前，先用三段话补齐背景。

**卷积到底在算什么。** 一个 2D 卷积层有输入特征图 `data`（形状 `N,H,W,C`：批次、高、宽、通道）和权重 `kernel`（形状 `F,KH,KW,C`：输出通道数、卷积核高、卷积核宽、输入通道数）。它还有四个超参数：步长 `S`（stride）、膨胀 `D`（dilation）、填充 `P`（padding）。输出特征图 `out` 的形状是 `N,OH,OW,F`，其中：

\[ OH = \left\lfloor \frac{H + 2P - D(KH-1) - 1}{S} \right\rfloor + 1, \quad OW = \left\lfloor \frac{W + 2P - D(KW-1) - 1}{S} \right\rfloor + 1 \]

对每个输出位置 `(n, oh, ow, f)`，计算公式是：

\[ \text{out}[n,oh,ow,f] = \sum_{kh,kw,c} \text{data}[n,\; oh\cdot S + kh\cdot D - P,\; ow\cdot S + kw\cdot D - P,\; c] \cdot \text{kernel}[f,kh,kw,c] \]

当 `oh*S + kh*D - P` 落在 `[0, H)` 之外时，对应的输入就是 padding 补的 0。这就是 padding 和 stride 的全部含义。

**im2col：把卷积变成矩阵乘。** 上面的求和号 `∑_{kh,kw,c}` 其实就是一个内积。如果我们把每个输出位置 `(n,oh,ow)` 看作矩阵的一「行」，把 `(kh,kw,c)` 的全部组合看作「列」，把权重按 `(f, kh·kw·c)` 排成另一个矩阵，那么卷积就变成了：

\[ C = A \times B^\top \]

其中 `A` 形状 `(N·OH·OW, KH·KW·C)`，`B` 形状 `(F, KH·KW·C)`，`C` 形状 `(N·OH·OW, F)`。这就是经典的 im2col（image to column）技巧。传统 im2col 会**物理地**把 `A` 矩阵显式构造出来（很费显存）；本讲内核的高明之处在于：**它不构造 `A`，而是在加载 `data_shared` 时用索引算术当场算出每个元素该读哪个输入像素**，从而省掉中间大矩阵。

**两种 TileLang API。** TileLang 既可以作为独立的 `tilelang` Python 包使用（`import tilelang as tl`），也作为 TVM 项目内置的 `tvm.tl` 子模块存在（`from tvm import tl`）。两者语言原语几乎一致（`T.Kernel`、`T.gemm`、`T.copy` 等都一样），但 `import` 路径、`target`、`profiler` 参数有差异。本讲内核用的就是后者 `tvm.tl`，跑在 AMD MI300X（gfx 架构、ROCm/HIP 后端）上。

## 3. 本讲源码地图

本讲只涉及一个算子目录 `cdna_benchmark/conv_benchmark/`，共两类源码：

| 文件 | 角色 |
| --- | --- |
| [cdna_benchmark/conv_benchmark/benchmark_tilelang_conv.py](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/cdna_benchmark/conv_benchmark/benchmark_tilelang_conv.py) | **主角**。用 `tvm.tl` 写的卷积内核 + autotune 驱动，`target="hip"`。本讲几乎所有源码精读都来自它。 |
| [cdna_benchmark/conv_benchmark/benchmark_torch_conv.py](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/cdna_benchmark/conv_benchmark/benchmark_torch_conv.py) | **PyTorch 参考基线**。用 `torch.conv2d` 跑一组 ResNet 风格的卷积 shape，作为「正确但慢」的标尺，负责产 latency 与 TFlops。 |
| [cdna_benchmark/conv_benchmark/benchmark_tilelang.sh](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/cdna_benchmark/conv_benchmark/benchmark_tilelang.sh) | 编排脚本，逐 shape 调用上面的 `.py`，日志写到 `logs/`。 |
| [cdna_benchmark/conv_benchmark/benchmark_ladder_conv.py](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/cdna_benchmark/conv_benchmark/benchmark_ladder_conv.py) | Ladder 基线（另一套 AMD 专用编译栈 `welder`），作为对照组，本讲仅作定位说明。 |

注意：这个目录**没有** `data/`、`plot/` 子目录（不像 hopper 的 dense_matmul），数据提取脚本 `conv_tlops_extract.py` / `ladder_conv_tlops_extract.py` 直接平铺在算子目录里。

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：① `tvm.tl` API 变体与 `import` 差异；② `target="hip"` 与 AMD/ROCm 适配；③ im2col 把卷积映射成 GEMM；④ `T.Buffer` 重排与 `in_bound` 掩码。

### 4.1 tvm.tl API 变体：import 差异与 profiler="tvm"

#### 4.1.1 概念说明

回顾 u3-l8 / u3-l9，你在 NVIDIA 的 hopper dense_matmul 里见到的 TileLang 内核，文件头是这样写的：

```python
import tilelang as tl
import tilelang.language as T
from tilelang.autotuner import autotune, jit
```

这是**独立 `tilelang` 包**的写法。而本讲的卷积内核，文件头是：

```python
from tvm import tl
import tvm.tl.language as T
from tvm.tl.autotuner import *
```

这是 **TVM 内置的 `tvm.tl` 子模块**。两者其实是同一套 DSL（TileLang 最初就是在 TVM 里孵化的，后来才抽出独立包），语言层原语 `T.Kernel`、`T.copy`、`T.gemm`、`T.Pipelined`、`T.alloc_shared` 完全一致。区别在于：

1. **入口包不同**：`tilelang` 是独立 pip 包；`tvm.tl` 随 TVM 安装。
2. **装饰器导入风格不同**：独立包常用具名导入 `autotune, jit`；这里用 `from tvm.tl.autotuner import *` 的通配导入，直接写裸的 `@autotune`、`@jit`。
3. **`profiler` 参数**：`tvm.tl` 路径显式传 `profiler="tvm"`，而独立 `tilelang` 包即便也跑在 AMD 上（`target="hip"`）也**不**传这个参数。

#### 4.1.2 核心流程

判断一个 cdna 内核走哪条 API 路径，只需看文件前三行：

```
读到 from tvm import tl / import tvm.tl.language / tvm.tl.autotuner
        ↓
        属于 tvm.tl 变体
        ↓
@jit(...) 里通常带 profiler="tvm"，target="hip"
```

```
读到 import tilelang as tl / tilelang.language
        ↓
        属于独立 tilelang 包
        ↓
@jit(...) 里 target="hip"（AMD）或 "auto"（NVIDIA），不传 profiler="tvm"
```

#### 4.1.3 源码精读

本讲的卷积内核文件头，确认是 `tvm.tl` 变体：

[tvm.tl 三件套 import（第 2-4 行）](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/cdna_benchmark/conv_benchmark/benchmark_tilelang_conv.py#L2-L4) —— 从 TVM 里取出 `tl`（含 `TensorSupplyType`）、`T`（语言原语）、通配导入 autotuner 的 `@autotune`/`@jit`。

紧接着装饰器层（第 46-47 行）：

[@autotune / @jit 装饰器（第 46-47 行）](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/cdna_benchmark/conv_benchmark/benchmark_tilelang_conv.py#L46-L47) —— 注意末尾两个 `tvm.tl` 专属标志：`profiler="tvm"` 与 `target="hip"`。

> **关于 `profiler="tvm"` 的确切含义**：本仓库的代码层面呈现一个清晰的规律——凡是用 `tvm.tl` 三件套（`from tvm import tl`）的内核（如本文件、`cdna_benchmark/mha_benchmark/test_tilelang_mha.py`），都在 `@jit` 里显式写 `profiler="tvm"`；而用独立 `tilelang` 包的内核（如 `cdna_benchmark/gemm_benchmark/1.tilelang_benchmark/benchmark_tilelang_matmul.py`、`cdna_benchmark/mha_benchmark/benchmark_tilelang_mha.py`）即便 `target="hip"` 也**不写** `profiler="tvm"`。由此可以确定地推断：`profiler="tvm"` 是 `tvm.tl` 路径用来选择 TVM 自带 profiler（计时器）实现的一个开关，与独立包自带的 profiler 是两套实现。至于这两套 profiler 在计时口径（warmup、统计量）上的具体差异，**待确认**（需查 TVM 侧实现），但「`profiler="tvm"` ⇔ `tvm.tl` 变体」这一关联在本仓库是确定的。

#### 4.1.4 代码实践

**实践目标**：用肉眼在仓库里把 `tvm.tl` 内核与独立 `tilelang` 内核区分开。

**操作步骤**：
1. 打开 `cdna_benchmark/conv_benchmark/benchmark_tilelang_conv.py` 第 2-4 行，确认它是 `tvm.tl`。
2. 打开 `cdna_benchmark/gemm_benchmark/1.tilelang_benchmark/benchmark_tilelang_matmul.py` 第 2-5 行，确认它是独立 `tilelang` 包，且 `target="hip"`。
3. 在两个文件里分别搜索 `profiler=`，观察哪个有、哪个没有。

**需要观察的现象**：两个文件都跑在 AMD（`target="hip"`），但只有 conv 文件（tvm.tl 变体）带 `profiler="tvm"`；gemm 文件（独立包）不带。

**预期结果**：印证「`profiler="tvm"` 与 `tvm.tl` import 强绑定」这一规律。运行结果无须在本机验证，纯源码阅读即可。

#### 4.1.5 小练习与答案

**练习 1**：如果某个 cdna 内核文件第 2 行是 `import tilelang as tl`，第 3 行是 `from tilelang import language as T`，它的 `@jit` 里大概会不会有 `profiler="tvm"`？

> **答案**：不会。这是独立 `tilelang` 包的标志，本仓库中此类文件即使 `target="hip"` 也不写 `profiler="tvm"`。

**练习 2**：本讲的 `@autotune` 用了 `keys=['block_M', 'block_N', 'block_K', 'num_stages', 'thread_num']`（第 46 行）。这里的 `keys` 指的是什么，shape（N/C/H/W…）为什么不在 keys 里？

> **答案**：`keys` 是「调优 config 字典的键」，autotuner 会为不同的 config 组合分别编译计时并缓存。shape（N,C,H,W,F,K,S,D,P）不在 keys 里，因为它们是通过外层 `convolution(N,C,H,W,F,K,S,D,P)` 函数闭包**直接烤进** `@T.prim_func` 的张量形状里的（见第 52-54 行的 `T.Buffer((N,H,W,C),...)`），每次调用 `convolution` 都会重新生成一个针对该 shape 的内核，shape 变化天然触发重编译，不需要走 autotuner 的 key 机制。

---

### 4.2 target="hip"：把内核编到 AMD ROCm

#### 4.2.1 概念说明

`target` 是 TileLang/tvm.tl 的编译目标参数。你之前在 NVIDIA 内核里见到的 `target="auto"` 表示「让编译器自动探测当前机器的 CUDA 后端」。而本讲卷积内核写的是 `target="hip"`。

- **CUDA**：NVIDIA 的 GPU 编程模型，编译产物是 `.ptx`/`.cubin`，跑在 NVIDIA GPU（ada/ampere/hopper）上。
- **HIP**：AMD ROCm 栈里的 GPU 编程模型，语法与 CUDA 高度相似，编译产物跑在 AMD GPU（如 MI300X，gfx942，本仓库记作 cdna 架构）上。

`tvm.tl` / `tilelang` 的内核是后端无关的声明式描述，同一份 `@T.prim_func` 源码，给 `target="cuda"` 就走 NVIDIA，给 `target="hip"` 就走 AMD。本讲内核之所以用 `tvm.tl` + `target="hip"`，是因为它属于 `cdna_benchmark/`（AMD 专区）。

#### 4.2.2 核心流程

```
@T.prim_func 声明的算子逻辑（与后端无关）
        ↓
@jit(target="hip", ...) 把逻辑编译成 HIP/ROCm kernel
        ↓
profiler="tvm" 用 TVM 的 profiler 在 AMD GPU 上计时
        ↓
返回 best_latency / best_config / ref_latency
```

#### 4.2.3 源码精读

[target="hip" 与 profiler="tvm" 在 @jit 中的位置（第 47 行）](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/cdna_benchmark/conv_benchmark/benchmark_tilelang_conv.py#L47) —— `@jit(out_idx=[2], supply_type=tl.TensorSupplyType.Integer, ref_prog=ref_program, skip_check=True, profiler="tvm", target="hip")`。

几个参数一并说明：
- `out_idx=[2]`：第 2 个参数 `out`（从 0 数起，`data=0, kernel=1, out=2`，见第 51-54 行）是输出，profiler 据此知道该为哪个张量分配输出显存。
- `supply_type=tl.TensorSupplyType.Integer`：profiler 造测试输入时，用整数范围的随机值（而不是浮点随机），避免 fp16 下大数相乘溢出，便于稳定计时。
- `ref_prog=ref_program` 与 `skip_check=True`：传了参考实现但又显式跳过数值校验——本文件是「纯延迟」基准（与 u5-l18 的 mha 文件同理），数值正确性靠兄弟脚本 `benchmark_torch_conv.py` 间接保证。
- `target="hip"`：编译到 AMD ROCm。

#### 4.2.4 代码实践

**实践目标**：体会「同一套 DSL 描述，换 target 换后端」。

**操作步骤**：
1. 把本文件第 47 行的 `target="hip"` 与 hopper 的 `hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.py` 里 `@jit` 的 `target="auto"` 对比。
2. 对比两份内核的 `@T.prim_func` 主体（本文件第 50-90 行 vs hopper matmul），看 `T.Kernel / T.copy / T.gemm / T.Pipelined` 是否完全一致。

**需要观察的现象**：除了 import、target、profiler 之外，内核主体几乎可以逐行对应。

**预期结果**：理解 TileLang 的「逻辑与后端解耦」——这正是 DSL 相比手写 CUDA/HIP 的核心价值。**待本地验证**（在 MI300X 上 `target="hip"` 才能真正编译通过；在没有 AMD GPU 的机器上无法运行）。

#### 4.2.5 小练习与答案

**练习**：为什么 cdna 下的内核必须显式写 `target="hip"`，而不能像 hopper 那样用 `target="auto"`？

> **答案**：`target="auto"` 会让编译器探测本机后端。在 NVIDIA 机器上它会探测到 CUDA，导致 AMD 内核编译成错误的产物。cdna 目录里的内核设计目标就是 AMD/ROCm，显式写 `target="hip"` 可以避免被本机环境误导，确保无论在什么机器上解析这份脚本，目标后端都是 HIP。（注：`benchmark_tilelang.sh` 第 2 行还显式 `export HIP_VISIBLE_DEVICES=0`，进一步说明这是 AMD 环境。）

---

### 4.3 im2col 思路：把卷积映射成 GEMM

#### 4.3.1 概念说明

现在进入本讲真正的内核逻辑。如果你把本讲内核的 `@T.prim_func` 主体（第 50-90 行）和 u3-l9 的块级 GEMM 内核并排放，会发现它们是**同一个骨架**：`T.Kernel` 切网格 → `alloc_shared/alloc_fragment` 分配片上存储 → `T.clear` 清零累加器 → `T.Pipelined` 的 K 循环里 `T.copy` 取数据、`T.gemm` 乘加 → 回写。

唯一的不同是：普通 GEMM 直接从全局内存 `T.copy` 一个数据块；而卷积内核在 `T.copy` 之前，**用一段索引算术（access_h/access_w/in_bound）当场把卷积的「输入像素」算出来**。这正是「im2col」的思想——把卷积看成两个矩阵的乘法：

- 矩阵 A（虚拟）：`(N·OH·OW, KH·KW·C)`，每行是一个输出位置在卷积窗内展开的输入像素。
- 矩阵 B：权重 `(F, KH·KW·C)` 展平。
- 输出 C：`(N·OH·OW, F)`，再 reshape 回 `(N,OH,OW,F)`。

传统 im2col 会先把 A 物理构造出来；本内核**不构造 A**，而是在加载共享内存时即时计算 A 的每个元素，从而省掉 `N·OH·OW·KH·KW·C` 大小的中间矩阵。

#### 4.3.2 核心流程

先约定两个一维索引（这是理解全部源码的钥匙）：

- **m**：输出矩阵的行号，范围 `[0, N·OH·OW)`。把它拆回三维：
  \[ n = m \;//\; (OH\cdot OW), \quad oh = (m \bmod (OH\cdot OW)) \;//\; OW, \quad ow = m \bmod OW \]
- **k**：归约（列）号，范围 `[0, KH·KW·C)`。把它拆回三维：
  \[ kh = k \;//\; (KW\cdot C), \quad kw = (k\;//\; C) \bmod KW, \quad c = k \bmod C \]

把这两组代入卷积公式，就得到本内核读输入的位置：

\[ ih = oh\cdot S + kh\cdot D - P, \quad iw = ow\cdot S + kw\cdot D - P \]
\[ A[m, k] = \text{data}[n,\; ih,\; iw,\; c] \]

权重矩阵 B 因为已经是 `(F, KH, KW, C)`，直接展平成 `(F, KH·KW·C)` 即可（`B[f, k] = kernel[f, kh, kw, c]`，展平顺序与 k 的拆解方式一致）。于是：

\[ C[m, f] = \sum_k A[m,k] \cdot B[f,k] \]

这就是一个标准 GEMM，`T.gemm(..., transpose_B=True)` 对应 `C += A @ B^T`。

#### 4.3.3 源码精读

先看网格如何把输出切成块：

[T.Kernel 网格映射（第 56-59 行）](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/cdna_benchmark/conv_benchmark/benchmark_tilelang_conv.py#L56-L59) —— `bx` 维度切 `F`（输出通道，每块 `block_N` 个），`by` 维度切 `N·OH·OW`（输出行，每块 `block_M` 个）。所以每个 block 算的是「一批输出行 × 一批输出通道」的 `block_M × block_N` 子块，与块级 GEMM 完全同构。

权重与输出用 `T.Buffer` 做零拷贝重排（详见 4.4）：

[kernel_flat / out_flat 的 Buffer 重排（第 65-66 行）](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/cdna_benchmark/conv_benchmark/benchmark_tilelang_conv.py#L65-L66) —— 把权重 `(F,KH,KW,C)` 重排成 `(F, KH·KW·C)`，把输出 `(N,OH,OW,F)` 重排成 `(N·OH·OW, F)`。

K 维流水线循环与 gemm：

[T.Pipelined + T.copy 权重 + T.gemm（第 70-86 行）](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/cdna_benchmark/conv_benchmark/benchmark_tilelang_conv.py#L70-L86) —— 这是 im2col GEMM 的核心。循环按 `block_K` 分块遍历归约轴 `KH·KW·C`：
  - 第 71-84 行：构造 `data_shared`（即虚拟矩阵 A 的一个 `block_M×block_K` 子块）——**这里就是 im2col 的「当场展开」**，索引算术在 4.4 详解。
  - 第 85 行：`T.copy` 把权重的 `block_N×block_K` 子块搬进 `kernel_shared`（B 的子块）。
  - 第 86 行：`T.gemm(data_shared, kernel_shared, out_local, transpose_B=True, k_pack=k_pack)` —— `out_local += data_shared @ kernel_shared^T`，即 `C += A @ B^T`。

注意 `k_pack=2`（第 44 行）：这是 `T.gemm` 的一个调优旋钮，表示把 K 维按 2 个元素打包送进 TensorCore 的 MMA 指令。对 fp16，MMA 指令天然以偶数 K 步处理，`k_pack=2` 让内核布局与 MMA 指令形状对齐，提升 TensorCore 利用率。

#### 4.3.4 代码实践

**实践目标**：手算一个具体 shape 的 im2col 矩阵尺寸，建立直觉。

**操作步骤**：取 `benchmark_torch_conv.py` 里的 ResNet 第一层卷积 shape：`(N,C,H,W,F,K,S,D,P) = (1, 3, 224, 224, 64, 7, 2, 1, 3)`（即 `benchmark_torch_conv.py` 第 73 行与 `benchmark_tilelang.sh` 第 29 行都在用的 `32 3 224 224 64 7 2 1 3` 的 N=1 变体）。

1. 算 `OH, OW`：\(OH = (224 + 2·3 - 1·(7-1) - 1)//2 + 1 = (224+6-6-1)//2 + 1 = 223//2 + 1 = 111+1 = 112\)，同理 `OW = 112`。
2. 算虚拟矩阵 A 的形状：`(N·OH·OW, KH·KW·C) = (1·112·112, 7·7·3) = (12544, 147)`。
3. 算矩阵 B 的形状：`(F, KH·KW·C) = (64, 147)`。
4. 算输出 C 的形状：`(12544, 64)`，reshape 回 `(1, 112, 112, 64)`。
5. 用源码第 109 行公式 `total_flops = 2*N*C*OH*OW*F*K*K` 验证 FLOPS：`2·1·3·112·112·64·7·7 = 2·1·3·12544·64·49`。

**需要观察的现象**：虚拟矩阵 A 的列数 `147 = KH·KW·C` 与 `total_flops` 公式里的 `K·K` 因子一一对应；若物理构造 A，需要 `12544×147×2 bytes ≈ 3.5 MB` 的中间显存，而内核内 im2col 完全省掉它。

**预期结果**：理解「im2col 把卷积变成 (12544,147)×(147,64) 的 GEMM」。FLOPS 与 TFlops 的实际数值**待本地验证**（依赖真实 latency）。

#### 4.3.5 小练习与答案

**练习 1**：为什么本内核的 `T.gemm` 要写 `transpose_B=True`？

> **答案**：因为权重矩阵 B（`kernel_shared`）的形状是 `(block_N, block_K)`，即「输出通道 × 归约」，而行优先矩阵乘 `C += A @ B^T` 里需要 B 的转置（归约 × 输出通道）才能与 A（输出行 × 归约）对齐 K 轴。`transpose_B=True` 让 `T.gemm` 内部把 B 当作 `(block_K, block_N)` 的转置来算，省去物理转置。

**练习 2**：如果把卷积核大小从 `K=7` 改成 `K=1`（1×1 卷积），im2col 矩阵 A 的列数变成多少？

> **答案**：`KH·KW·C = 1·1·C = C`。1×1 卷积退化成纯通道间的矩阵乘，A 形状变成 `(N·OH·OW, C)`，这正是 1×1 卷积等价于 GEMM 的原因。

---

### 4.4 Buffer 重排与 in_bound 掩码

#### 4.4.1 概念说明

本模块解决两个细节问题，它们让 im2col 在内核内优雅落地：

1. **`T.Buffer` 重排**：权重本是 4 维 `(F,KH,KW,C)`，输出本是 4 维 `(N,OH,OW,F)`，但 GEMM 需要它们是 2 维。本内核用 `T.Buffer(新形状, dtype, 原buffer.data)` 创建一个**共享底层存储、仅改形状**的视图，零拷贝地把 4 维张量看成 2 维矩阵。注意 `.data` 取的是底层裸指针，所以新 Buffer 和原 Buffer 指向同一块显存，没有任何数据搬运。

2. **`in_bound` 掩码**：im2col 的 A 矩阵里，当 `ih`/`iw` 落在 `[0,H)`/`[0,W)` 之外时（即 padding 区域），对应元素应是 0。本内核不显式构造 padding 后的输入，而是用 `in_bound` 布尔判断 + `T.if_then_else(in_bound, data[...], 0)` 在加载时直接置 0。这样 padding、stride 全部由索引算术吸收，不需要任何额外的填充步骤。

#### 4.4.2 核心流程

加载 `data_shared[i, j]`（即虚拟矩阵 A 的第 `m` 行第 `k` 列）的流程：

```
m = by * block_M + i          # 本线程负责的输出行
k = k_iter * block_K + j      # 本线程负责的归约列
        ↓  拆 m
n  = m // (OH*OW)             # 批次
oh = (m % (OH*OW)) // OW      # 输出行
ow = m % OW                   # 输出列
        ↓  拆 k
kh = k // (KW*C)              # 卷积核高索引
kw = (k // C) % KW            # 卷积核宽索引
c  = k % C                    # 通道索引
        ↓  算输入像素坐标
access_h = oh*S + kh*D - P    # 等价于 m%(OH*OW)//OW*S + k//(KW*C)*D - P
access_w = ow*S + kw*D - P    # 等价于 m%OW*S + k//C%KW*D - P
        ↓  判边界
in_bound = (0 <= access_h < H) and (0 <= access_w < W)
        ↓  带掩码加载
data_shared[i,j] = in_bound ? data[n, access_h, access_w, c] : 0
```

- **stride**：`S` 同时出现在 `access_h` 和 `access_w` 里（`oh*S`、`ow*S`），实现了「输出每走一步，输入跨 S 步」。
- **dilation**：`D` 出现在 `kh*D`、`kw*D`，实现了「卷积核内每跨一个权重点，输入跨 D 步」。
- **padding**：`-P` 把坐标平移到带 padding 的坐标系；再用 `in_bound` 把超出 `[0,H)/[0,W)` 的部分判为 0。两者合起来等价于「先在输入周围补 P 圈 0，再做无 padding 的卷积」。

#### 4.4.3 源码精读

[T.Buffer 重排：kernel_flat / out_flat（第 65-66 行）](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/cdna_benchmark/conv_benchmark/benchmark_tilelang_conv.py#L65-L66) —— `kernel_flat = T.Buffer((F, KH*KW*C), dtype, kernel.data)`：用 `kernel.data` 这个底层指针，把 4 维权重重解释成 2 维 `(F, KH·KW·C)`，行优先展开顺序正好是 `(kh, kw, c)` 与下文 k 的拆解方式一致。`out_flat` 同理把输出 `(N,OH,OW,F)` 重排成 `(N·OH·OW, F)`。

[access_h / access_w 的索引算术（第 74-75 行）](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/cdna_benchmark/conv_benchmark/benchmark_tilelang_conv.py#L74-L75) —— 这两行就是把 4.4.2 的公式直接写成代码：`access_h = m % (OH * OW) // OW * S + k // (KW * C) * D - P`，`access_w = m % OW * S + k // C % KW * D - P`。注意它**把 m、k 拆解与坐标计算合并成一个表达式**，省掉中间变量，是整段内核最值得逐字符读的两行。

[in_bound 边界判断与带掩码加载（第 76-84 行）](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/cdna_benchmark/conv_benchmark/benchmark_tilelang_conv.py#L76-L84) —— `in_bound` 要求 `access_h`、`access_w` 都落在输入范围内；`data_shared[i,j] = T.if_then_else(in_bound, data[m//(OH*OW), access_h, access_w, k%C], 0)` 在越界时写 0，实现隐式 padding。`data` 的第一维下标 `m//(OH*OW)` 正是批次号 `n`。

最后是回写（与 u3-l9 同构的「fragment→shared→全局」两步）：

[回写 out_local → out_shared → out_flat（第 87-88 行）](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/cdna_benchmark/conv_benchmark/benchmark_tilelang_conv.py#L87-L88) —— `out_local`（fragment 累加器）先经 `out_shared`（共享内存）做布局规整，再写到 `out_flat[by*block_M, bx*block_N]` 这个 `(block_M, block_N)` 子块。之所以要中转 `out_shared`，是因为 fragment 的寄存器分布布局不能直接合并写成全局内存，需要 shared memory 重排（与 u3-l9 同理）。

#### 4.4.4 代码实践（对应本讲指定的实践任务）

**实践目标**：把 `data_shared` 加载时的 `access_h/access_w/in_bound` 整理成公式，并说明它如何实现 padding 与 stride。

**操作步骤**：

1. **写出公式**（基于第 74-81 行源码，设 `m = by*block_M + i`，`k = k_iter*block_K + j`）：

   | 量 | 公式 |
   | --- | --- |
   | 批次 `n` | \( n = m \;//\; (OH\cdot OW) \) |
   | 输出行 `oh` | \( oh = (m \bmod (OH\cdot OW)) \;//\; OW \) |
   | 输出列 `ow` | \( ow = m \bmod OW \) |
   | 核高索引 `kh` | \( kh = k \;//\; (KW\cdot C) \) |
   | 核宽索引 `kw` | \( kw = (k\;//\; C) \bmod KW \) |
   | 通道 `c` | \( c = k \bmod C \) |
   | **access_h** | \( ih = oh\cdot S + kh\cdot D - P \) |
   | **access_w** | \( iw = ow\cdot S + kw\cdot D - P \) |
   | **in_bound** | \( 0 \le ih < H \;\land\; 0 \le iw < W \) |
   | 加载值 | `in_bound ? data[n, ih, iw, c] : 0` |

2. **代值验证 padding**：取上面 4.3.4 的 ResNet 第一层（`S=2, D=1, P=3, H=W=224, OH=OW=112`），看 `oh=0, ow=0`（第一个输出像素）在 `kh=0`（左上角卷积核点）时：
   \[ ih = 0·2 + 0·1 - 3 = -3 < 0 \Rightarrow \text{in\_bound}=\text{False} \Rightarrow \text{写 0} \]
   这正是 padding 区——左上输出像素的卷积窗大部分落在输入外，被正确置 0。

3. **代值验证 stride**：看同一行 `oh` 从 0 变到 1 时，`ih` 从 `-3` 变到 `1·2 + 0·1 - 3 = -1`，跨了 `S=2`——输出每走一步，输入坐标确实跨了 stride。

**需要观察的现象**：`-P` 把坐标平移到「带 padding 的原点」，`in_bound` 把平移后仍为负或超 H/W 的部分置 0；两者合起来精确复现了「补 P 圈 0」的语义。stride 则完全由 `·S` 这一项承担。

**预期结果**：你能对任意 `(m, k)` 算出读哪个输入像素、是否被 padding 掩掉。无需运行即可完成；若想机器验证，可在 `ref_program`（第 35-42 行，用 `torch.conv2d`）里对同一 shape 打印输出，再与内核输出比对——**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：`T.Buffer((F, KH*KW*C), dtype, kernel.data)` 改变了权重的物理数据吗？为什么这样做是零拷贝？

> **答案**：没有改变。`kernel.data` 取的是底层显存裸指针，新 `T.Buffer` 只是用一个新的形状（和对应的 stride）去**解释**同一块显存，不分配新存储、不搬运数据，所以是零拷贝。它本质上是一个「视图」（view）。

**练习 2**：如果 `P=0`（无 padding），`in_bound` 还有用吗？

> **答案**：仍然有用，但触发频率更低。即使 `P=0`，当卷积窗滑到输入边缘时，`kh`/`kw` 较大仍可能让 `ih≥H` 或 `iw≥W`（例如 `K>1` 且输出尺寸刚好取整），此时 `in_bound` 同样要把越界部分置 0。所以 `in_bound` 是卷积边界处理的通用机制，不专属于 padding。

**练习 3**：第 71 行的 `T.Parallel(block_M, block_K, coalesced_width=coalesced_width)` 里 `coalesced_width=None`（第 45 行）是什么意思？

> **答案**：`coalesced_width` 控制 `T.copy`/`T.Parallel` 加载时的内存合并（coalescing）粒度，影响相邻线程访问相邻地址以提升显存带宽利用率的方式。`None` 表示把这个旋钮交给 TileLang 自动决定，不在搜索空间里手动调（注意它没出现在第 46 行 `@autotune` 的 `keys` 里，所以 autotuner 不会搜索它）。

---

## 5. 综合实践

设计一个把本讲四个模块串起来的小任务：**为一个新的卷积 shape 读懂并预测内核行为**。

**任务背景**：`benchmark_torch_conv.py` 第 50 行有一个 shape `(1,64,56,56,64,3,1,1,1)`（3×3 卷积、stride 1、padding 1）。假设你要让 `benchmark_tilelang_conv.py` 跑这个 shape。

**要求**：

1. **API 识别**：确认 `benchmark_tilelang_conv.py` 走的是 `tvm.tl` 变体、`target="hip"`、`profiler="tvm"`（读第 2-4、47 行）。
2. **尺寸推算**：手算 `OH, OW`、虚拟矩阵 A 的形状 `(N·OH·OW, KH·KW·C)`、矩阵 B 的形状、`total_flops`。
3. **索引追踪**：取输出像素 `oh=ow=0`、卷积核点 `kh=0, kw=2, c=0`，写出对应的 `m`、`k`、`access_h`、`access_w`、`in_bound`，判断该位置是否被 padding 置 0。
4. **运行编排**：参照 `benchmark_tilelang.sh` 第 112-126 行的循环，写出一条调用命令（`python benchmark_tilelang_conv.py --n 1 --c 64 --h 56 --w 56 --f 64 --k 3 --s 1 --d 1 --p 1`），并说明日志会写到 `logs/` 下哪个文件名。
5. **对照基线**：说明这个 shape 的「正确但慢」参考值由哪个文件、哪个函数提供（答：`benchmark_torch_conv.py` 的 `conv2d_nchw`，第 122-124 行）。

**参考答案要点**：
- `OH = OW = (56 + 2·1 - 1·2 - 1)//1 + 1 = 56`。
- A 形状 `(1·56·56, 3·3·64) = (3136, 576)`；B 形状 `(64, 576)`；`total_flops = 2·1·64·56·56·64·3·3`。
- `m = 0`（第一个输出像素），`k` 对应 `(kh=0,kw=2,c=0)`：`k = 0·(3·64) + 2·64 + 0 = 128`；`access_h = 0·1 + 0·1 - 1 = -1`，`access_w = 0·1 + 2·1 - 1 = 1`；`in_bound = (-1≥0?) = False` ⇒ 置 0。
- 运行需 AMD ROCm 环境；**待本地验证**。

## 6. 本讲小结

- 本讲内核走的是 **`tvm.tl` 变体**（`from tvm import tl` / `tvm.tl.language as T` / `tvm.tl.autotuner`），与独立 `tilelang` 包（`import tilelang as tl`）在 import、`profiler` 上有差异。
- 在本仓库，「`profiler="tvm"` ⇔ `tvm.tl` 变体」是确定的关联；`profiler="tvm"` 与独立包 profiler 的**计时口径差异待确认**。
- `target="hip"` 把同一份声明式内核编到 AMD ROCm/HIP；同一套 `T.Kernel/T.copy/T.gemm` 原语在 CUDA 与 HIP 上通用，体现「逻辑与后端解耦」。
- 卷积内核的骨架就是 u3-l9 的块级 GEMM，区别在于 im2col：它**不物理构造 col 矩阵**，而在加载 `data_shared` 时用 `access_h/access_w` 索引算术当场展开输入像素。
- padding、stride、dilation 全部被索引算术吸收：`-P` 平移原点 + `in_bound` 把越界置 0 = 隐式 padding；`·S` = stride；`·D` = dilation。
- `T.Buffer(新形状, dtype, 原.data)` 是零拷贝的视图重排，把 4 维权重/输出看成 2 维 GEMM 矩阵；`k_pack=2`、`coalesced_width=None` 是辅助调优旋钮。

## 7. 下一步学习建议

- **继续读 AMD 侧的 `tvm.tl` 内核**：`cdna_benchmark/mha_benchmark/test_tilelang_mha.py`（同样是 `from tvm import tl` + `profiler="tvm"` + `target="hip"`），可作为巩固 `tvm.tl` API 的第二份样例；以及 u6-l20 讲过的 MLA decode 内核。
- **对照 GEMM 版本**：读 `cdna_benchmark/gemm_benchmark/1.tilelang_benchmark/benchmark_tilelang_matmul.py`，体会「独立 tilelang 包 + target=hip」与「tvm.tl + target=hip」的并存。
- **进入显式 AutoTuner**：本讲用的是装饰器式 `@autotune/@jit`；下一讲 u6-l22 将讲解 `AutoTuner.from_kernel(...).run(...)` 的显式调优 API，以及多 `@T.prim_func` 条件组合。
- **跨架构适配**：u7-l24 会系统讲同一算子在 Ada/Ampere/Hopper/CDNA 间的迁移，届时可回看本讲的 `target="hip"` 作为 AMD 侧的具体落地。

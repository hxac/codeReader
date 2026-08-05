# 张量核 WGMMA 引擎

## 1. 本讲目标

本讲打开 Vortex 的**张量核（Tensor Core Unit, TCU）**——一个把 RISC-V 扩展成类 GPU 矩阵加速器的硬件单元。学完后你应该能够：

- 说清 **WGMMA（warpgroup MMA）** 的分块矩阵乘加语义，以及它与单 warp 的 **WMMA** 的区别；
- 根据 `NUM_THREADS`（`NT`）和 `NRC` 推导出一次 WGMMA 操作的分块形状（`tcM/tcN/tcK`、`xtileM/xtileN/tileK`）；
- 读懂 kernel 侧 API `vx_tensor.h`（`wgmma_context`、`vx_make_smem_desc`、`wgmma_sync`）如何把一条矩阵乘指令编码成 RISC-V custom0 指令；
- 理解 SimX 中 `TcuUnit` + `TcuUopGen` + `TcuTbuf` 三者如何协作地建模 TCU 的功能与时序；
- 说清 **2:4 结构化稀疏** 与 **MX（microscaled）** 量化格式在 TCU 中的支持方式。

本讲是「进阶层→专家层」的衔接：它承接 u6-l4（功能单元 ALU/FPU/LSU/SFU）中「SFU 是分派器、FuncUnit 用 channel 承载延迟」的模型，并把执行单元扩展到一个真正的矩阵乘加速器。

## 2. 前置知识

在进入 TCU 之前，请确认你已理解以下概念（若不熟，先回看对应讲义）：

- **SIMT 与 warp**（u1-l1、u4-l2）：一条 warp 指令在 `NUM_THREADS` 个线程上同时执行，共享 PC。TCU 把「一条 warp 指令」视作一个矩阵微块的并行计算。
- **FuncUnit 模型**（u6-l4）：SimX 中所有执行单元都继承自 `FuncUnit<NUM_BLOCKS>`，靠 `Inputs/Outputs` 两条 channel 收发指令 trace，延迟由 `output.send(trace, latency)` 承载。TCU 也是一个 FuncUnit。
- **宏指令→微操作展开**（u6-l2 的 Sequencer）：译码出的复杂指令在发射阶段被 `Sequencer`（如 `TcuUopGen`）裂成多条独立流过流水线的 uop。
- **本地内存 LMEM**（u8-l3）：每核私有、单周期近存储、完全绕过缓存栈。TCU 的矩阵操作数就是从 LMEM（即 CUDA 里的 shared memory）取的。
- **基数规则与 model_parity**（u5-l3、u7-l4）：模块只通过 channel 通信，SimX 与 RTL 必须功能+时序一致。本讲的 `TcuUnit`/`TcuTbuf` 就是 RTL `hw/rtl/tcu/` 的预言机。

几个本讲会用到的矩阵术语：

- **GEMM**：通用矩阵乘 \(C = A \times B + C\)，其中 \(A\) 是 \(M \times K\)，\(B\) 是 \(K \times N\)，\(C\) 是 \(M \times N\)。
- **MMA / FMA**：Matrix Multiply-Accumulate / Fused Multiply-Add。一次「乘加」即 \(d = a \cdot b + c\)。
- **分块（tiling）**：把大 GEMM 切成小块，让小块装进寄存器/LMEM，是所有矩阵加速器的核心思想。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [docs/designs/tensor_core_wgmma_engine.md](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/designs/tensor_core_wgmma_engine.md) | TCU 总体设计文档：架构图、数据类型、RTL 模块清单、SimX 模型说明。本讲的「骨架」。 |
| [sw/kernel/include/vx_tensor.h](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/kernel/include/vx_tensor.h) | 设备侧 kernel API：`wmma_context` / `wgmma_context`、fragment 类型、`wgmma_sync` 内联函数。 |
| [sw/common/tensor_cfg.h](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/common/tensor_cfg.h) | 纯几何模板：`wmma_config_t` / `wgmma_config_t`，从 `NT` 推导所有分块尺寸。 |
| [sim/simx/tcu/tcu_unit.cpp](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/tcu/tcu_unit.cpp) | SimX 的 TCU 功能+时序模型：`TcuUnit`（FuncUnit）、`TcuUopGen`（uop 展开）、`wgmma()` 功能计算、lockstep 门控。 |
| [sim/simx/tcu/tcu_tbuf.cpp](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/tcu/tcu_tbuf.cpp) | Tile Buffer：`abuf×Q + bbuf×1` 的 line cache，仲裁到单一 LMEM 端口。 |
| [sim/simx/tcu/tcu_tbuf.h](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/tcu/tcu_tbuf.h) | `TcuTbuf` 的 `plan/read/ready` 接口契约。 |
| [tests/regression/sgemm_tcu_wg/](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/tests/regression/sgemm_tcu_wg/kernel.cpp) | 用 WGMMA 实现的 sgemm 回归测试，是本讲代码实践的样本。 |
| [VX_config.toml](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/VX_config.toml) `[tcu]` 段 | TCU 的硬件配置开关。 |

---

## 4. 核心概念与源码讲解

### 4.1 WGMMA 分块矩阵乘加语义与几何

#### 4.1.1 概念说明

**TCU 是什么。** TCU 是 Vortex 的张量核，一个矩阵乘加加速器，是 RISC-V 的一类 ISA 扩展（`MISA` bit 9，由 `VX_CFG_EXT_TCU_ENABLE` 开启，见 [VX_config.toml:229-245](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/VX_config.toml#L229)）。它实现两类指令：

- **WMMA**（Warp MMA）：单 warp 的矩阵乘加，A/B 操作数都来自浮点寄存器堆。
- **WGMMA**（Warpgroup MMA）：**一组 warp（warpgroup）** 协同的矩阵乘加，B 必来自共享内存（LMEM），A 可来自寄存器（RS 模式）或 LMEM（SS 模式）。WGMMA 是 NVIDIA Hopper 风格的「大块」MMA，单条指令完成的有效算力远大于 WMMA。

**为什么要 WGMMA。** GEMM 的核心瓶颈是**访存**：每取一个 \(A\) 或 \(B\) 元素只换来一次乘加，算术强度太低。WGMMA 的做法是**用 B 的广播换 A 的复用**——把 B 的一行/块广播给 warpgroup 内所有块，每块各持自己的 A 行，于是一份 B 数据被多次复用。这正是 TCU 把 B 放进「全 warpgroup 共享的 bbuf」、把 A 放进「每块私有 abuf」的根本原因（见 [tensor_core_wgmma_engine.md:184-188](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/designs/tensor_core_wgmma_engine.md#L184)）。

**warpgroup = Q 个 block。** SimX/RTL 把 `ISSUE_WIDTH` 个发射通道视作一个 warpgroup，即 `Q = VX_CFG_NUM_TCU_BLOCKS = ISSUE_WIDTH` 个 lock-stepped 块（见 [tensor_core_wgmma_engine.md:46-50](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/designs/tensor_core_wgmma_engine.md#L46)）。一条 WGMMA 宏指令横跨这 Q 个块同时执行。

#### 4.1.2 核心流程：从 NT 推导分块几何

WGMMA 的所有分块尺寸**只由 `NT = NUM_THREADS` 和 `NRC`（每个线程持有的累加器寄存器数）决定**，公式定义在 [tensor_cfg.h:335-361](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/common/tensor_cfg.h#L335)。记 \(\ell = \log_2 NT\)：

\[
tcM = 2^{\,\lfloor (\ell+1)/2 \rfloor} = 2^{\,\lceil \ell/2 \rceil},\quad tcN = tcK = 2^{\,\lfloor \ell/2 \rfloor}
\]

> 说明：代码用 C++ 整数除法 `tcM = 1u << ((lg_NT + 1) / 2)`（即向下取整 \(\lfloor(\ell+1)/2\rfloor\)，它等价于 \(\lceil\ell/2\rceil\)），`tcN = 1u << (lg_NT / 2)`（即 \(\lfloor\ell/2\rfloor\)）。

\[
xtileM = 2 \cdot tcM,\quad xtileN = \frac{NRC \cdot NT}{xtileM},\quad n\_steps = xtileN / tcN
\]

\[
fedpK = tcK \ (\text{或}\ 2\cdot tcK\ \text{当开启 FEDP2K}),\quad k\_steps = \frac{2\cdot tcK}{fedpK},\quad tileK = 2\cdot tcK \cdot i\_ratio
\]

其中 \(i\_ratio = 32 / \text{元素位宽}\)（fp16 为 2，int8 为 4，tf32/fp32 为 1）。**微块** `tcM × tcN` 是一次 FEDP（见 4.3）产出的输出小块；**每 warp 的输出瓦片** `xtileM × xtileN` 由 `m_steps × n_steps` 个微块拼成；**每 warp 沿 K 归约** `tileK` 个元素（分 `k_steps` 步）。

**一条 WGMMA 宏指令被展开成多少条 uop？** 由 `k_steps × NRC` 给出（[tcu_unit.cpp:1429-1431](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/tcu/tcu_unit.cpp#L1429)），展开顺序为 **k 最外、n 居中、m 最内**：

\[
uop\_idx = k \cdot (n\_steps \cdot m\_steps) + n \cdot m\_steps + m
\]

这个顺序刻意让 A 的复用最大化：每个 `A[m,k]` 沿整个 `(n,m)` 内层扫描被反复消费。

**一个具体例子（取自真实测试 sgemm_tcu_wg-8）**：`NT=8`、`ITYPE=fp16`、`NRC=8`、`ISSUE_WIDTH=4`（见 [ci/testcases/tensor_wg.yaml:63-65](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/testcases/tensor_wg.yaml#L63)）。

| 量 | 值 | 说明 |
|---|---|---|
| \(\ell=\log_2 8\) | 3 | |
| `tcM / tcN / tcK` | 4 / 2 / 2 | 微块 \(4\times2\)，K-word 步长 2 |
| `xtileM / xtileN` | 8 / 8 | 每 warp 输出瓦片 \(8\times8=64\) |
| `m_steps / n_steps / k_steps` | 2 / 4 / 2 | |
| `tileK`（fp16, \(i\_ratio=2\)） | 8 | 每 warp 沿 K 归约 8 个元素 |
| warpgroup 块数 Q | 4 | 4 个 lock-stepped 块 |
| 宏→uop 数 | 16 | \(k\_steps \times NRC = 2\times8\) |

所以一条这样的 WGMMA 指令，每个 warp 完成 \(8\times8\) 输出、沿 K 归约 8 个 fp16 元素，并被裂成 16 条 uop 流水执行。

#### 4.1.3 源码精读

几何常量在 kernel 侧与 SimX 侧各有一份镜像，二者必须一致（model_parity）。

kernel 侧（[vx_tensor.h:722-742](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/kernel/include/vx_tensor.h#L722)）：

```cpp
static constexpr uint32_t tcM = 1u << ((lg_NT + 1) / 2);
static constexpr uint32_t tcN = 1u << (lg_NT / 2);
static constexpr uint32_t tcK = tcN;
static constexpr uint32_t m_steps = 2;
static constexpr uint32_t k_steps = (2 * tcK) / fedpK;
static constexpr uint32_t xtileM  = m_steps * tcM;
static constexpr uint32_t xtileN  = (NRC_ * NT) / xtileM;
static constexpr uint32_t tileK   = k_steps * fedpK * i_ratio;
```

SimX 侧用同一个模板（[tcu_unit.cpp:33-35](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/tcu/tcu_unit.cpp#L33)），确保两侧几何同源：

```cpp
using cfg    = vt::wmma_config_t<VX_CFG_NUM_THREADS>;
using wg_cfg = vt::wgmma_config_t<VX_CFG_NUM_THREADS, vt::fp32, vt::fp32>;
static constexpr uint32_t kFedpWords = wg_cfg::fedpK;
```

FEDP 流水线深度随 RTL 选定的 PE 后端（`VX_CFG_TCU_TYPE`）不同，端到端单 uop 延迟 = 派发 1 拍 + FEDP 流水（[tcu_unit.cpp:42-56](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/tcu/tcu_unit.cpp#L42)）：

```cpp
#if defined(VX_CFG_TCU_TYPE_DPI)
static constexpr uint32_t kFedpLatency = 2 + 2;        // 仿真专用 DPI-C
#elif defined(VX_CFG_TCU_TYPE_TFR)                      // 默认，ASIC/SimX
static constexpr uint32_t kFedpLatency = 1 + 1 + 1 + 1;// 定点归约树
#endif
static constexpr uint32_t kMmaLatency = 1 + kFedpLatency;
```

> 说明：四种后端 `DPI/DSP/BHF/TFR`（设计文档 [tensor_core_wgmma_engine.md:108-121](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/designs/tensor_core_wgmma_engine.md#L108)）都计算 \(\Sigma(a\cdot b)+c \to d\)，只是实现与延迟不同；`TFR`（定点归约树）是默认。

#### 4.1.4 代码实践：手算一条 WGMMA 的形状

1. **实践目标**：不用看答案，独立推出 `sgemm_tcu_wg-3`（`NT=16`、fp16、`NRC=32`、`ISSUE_WIDTH=4`）的一次 WGMMA 形状。
2. **操作步骤**：
   - 打开 [tensor_cfg.h:335-361](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/common/tensor_cfg.h#L335)，找到 `wgmma_config_t` 的几何公式。
   - 代入 \(\ell=\log_2 16=4\)、\(i\_ratio=2\)（fp16）。
   - 依次算 `tcM/tcN/tcK`、`xtileM/xtileN`、`tileK`、宏→uop 数。
3. **需要观察的现象**：`NRC=32` 时每 warp 的输出瓦片明显比 `NRC=8` 大。
4. **预期结果**（供核对）：\(\ell=4\)，故 `tcM = 2^⌈4/2⌉ = 4`、`tcN = tcK = 2^⌊4/2⌋ = 4`、`fedpK=4`、`m_steps=2`、`k_steps = 2*tcK/fedpK = 2`；`xtileM = 2*tcM = 8`、`xtileN = NRC*NT/xtileM = 32*16/8 = 64`、`n_steps = 64/4 = 16`；`tileK = 2*tcK*i_ratio = 16`（fp16）；宏→uop 数 \(= k\_steps \times NRC = 2 \times 32 = 64\)。即每 warp 完成 \(8\times64\) 输出、沿 K 归约 16 个 fp16。
5. 待本地验证（编译后可用 4.3 的 trace 实践确认 uop 数）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 WGMMA 把 B 放共享 bbuf、A 放每块私有 abuf，而不是反过来？
**答**：在 \((n,m)\) 内层扫描中，每个 `A[m,k]` 只被该块自己用，而 `B[k,n]` 要被所有块用——共享 B + 广播能让一份 B 数据被 Q 个块复用，算术强度最高；反之共享 A 没有复用收益。

**练习 2**：`k_steps` 在开启 `VX_CFG_TCU_FEDP2K` 时会怎样变化？为什么？
**答**：`fedpK` 从 `tcK` 翻倍为 `2*tcK`，于是 `k_steps = 2*tcK/fedpK` 减半。FEDP2K 让单条 uop 在一个加宽的 FEDP 里消费两倍的 K，从而用更少的 uop 完成同样的归约。

---

### 4.2 kernel API：vx_tensor.h 与 wgmma_sync

#### 4.2.1 概念说明

设备侧 kernel 通过 `vx_tensor.h` 使用 TCU，编程模型与 CUDA 的 `wmma`/`mma` fragment 高度对齐：

- **fragment（矩阵片段）**：`fragment_a` / `fragment_b` / `fragment_acc`，分别是 A、B、C(D) 在寄存器中的分布。每个线程只持有矩阵的一部分，warp 全员合起来才是完整瓦片。
- **wgmma_context**：一个模板上下文，封装了所有几何常量与操作（`fill/load/store/wgmma_sync`）。
- **smem 描述符**：因为 WGMMA 的 B（以及 SS 模式的 A）直接从 LMEM 取，kernel 需要把「LMEM 里的矩阵地址 + 行步长」打包成一个 32 位描述符交给硬件。

#### 4.2.2 核心流程：一次 WGMMA 的 kernel 调用

以 `tests/regression/sgemm_tcu_wg/kernel.cpp` 为例（[kernel.cpp:33-94](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/tests/regression/sgemm_tcu_wg/kernel.cpp#L33)）：

1. 协作地把 A、B 瓦片从全局内存 load 进 LMEM（用 `a_blockmajor_idx`/`b_blockmajor_idx` 计算 block-major 下标）。
2. `__syncthreads()`。
3. 构造 B 的 smem 描述符：`desc_b = vx_make_smem_desc(B_smem, 0)`。
4. 选择 RS 或 SS 模式调用 `wgmma_sync`：
   - **RS**（A 来自寄存器，仅 `NRC ≤ 16`）：先 `load_matrix_sync(fragA, ...)` 把 A 装进 fragment，再 `wgmma_sync(fragC, fragA, desc_b, fragC)`。
   - **SS**（A、B 都来自 LMEM）：`desc_a = vx_make_smem_desc(A_warp, ...)`，再 `wgmma_sync(fragC, desc_a, desc_b, fragC)`。
5. `__syncthreads()`，循环下一个 K 瓦片；最后 `store_matrix_sync` 写回 C。

#### 4.2.3 源码精读

**smem 描述符**（[vx_tensor.h:27-39](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/kernel/include/vx_tensor.h#L27)）：32 位打包，`[31:16]` 是行步长（字节），`[15:0]` 是相对 LMEM 基址的字节偏移。

```cpp
struct smem_matrix_desc { uint32_t value; };
static inline smem_matrix_desc vx_make_smem_desc(const void* ptr, uint32_t leading_bytes) {
  size_t lmem_base = csr_read_nv(VX_CSR_LOCAL_MEM_BASE);
  uint32_t offset = static_cast<uint32_t>(static_cast<size_t>(reinterpret_cast<uintptr_t>(ptr)) - lmem_base);
  return {((leading_bytes << 16) | offset)};
}
```

> 说明：`ldm==0`（步长字段为 0）在硬件里被解释为 **block-major** 布局；`ldm!=0` 为 **row-major / k-major**。`B_smem` 用步长 0，即 block-major。

**wgmma_sync 内联函数**（[vx_tensor.h:972-993](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/kernel/include/vx_tensor.h#L972)）：用 `if constexpr` 在编译期区分 SS 与 RS，并把累加器 fragment 绑定到物理寄存器 `f0..f31`。SS 路径把两个描述符放进 `a0/a1`：

```cpp
constexpr bool a_is_smem = is_smem_desc<OpA>::value;
constexpr bool b_is_smem = is_smem_desc<OpB>::value;
static_assert(b_is_smem, "B must be smem_matrix_desc (SR mode is not supported)");
...
if constexpr (a_is_smem && b_is_smem) {
  register uint32_t ra __asm__("a0") = op_a.value;
  register uint32_t rb __asm__("a1") = op_b.value;
  ...
  __asm__ volatile (".insn r %[insn], 1, 2, x%[fmd], x%[fms], x%[flags]"
      : "+f"(fd0), ... : [insn]"i"(RISCV_CUSTOM0), [fmd]"i"(Ot::id),
        [fms]"i"(It::id), [flags]"i"(flags), "r"(ra), "r"(rb));
```

> 说明：`.insn r opcode, func3, func7, rd, rs1, rs2`——`opcode=RISCV_CUSTOM0(0x0B)`、`func3=1` 标识 WGMMA（WMMA 是 `func3=0`）、`func7=2` 是 TCU 子功能槽；`rd` 字段编码输出格式 `Ot::id`、`rs1` 编码输入格式 `It::id`、`rs2` 编码 `flags`（含 `is_sparse`、`cd_nregs_code`、`a_is_smem` 三位，见 [vx_tensor.h:700-708](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/kernel/include/vx_tensor.h#L700)）。这与设计文档的 opcode 表（[tensor_core_wgmma_engine.md:66-74](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/designs/tensor_core_wgmma_engine.md#L66)）一致。

**block-major 下标助手**（[vx_tensor.h:753-776](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/kernel/include/vx_tensor.h#L753)）：kernel 协作 load 时用它把逻辑 `(r,c)` 映射到 block-major 的物理下标，保证写入顺序与硬件 fetch 顺序吻合。

```cpp
static __attribute__((always_inline)) uint32_t b_blockmajor_idx(uint32_t r, uint32_t c) {
  uint32_t k_blk = r / (fedpK * i_ratio);
  uint32_t r_in  = r % (fedpK * i_ratio);
  uint32_t n_blk = c / tcN;
  uint32_t n_in  = c % tcN;
  return (k_blk * n_steps + n_blk) * b_blk_elems + n_in * (fedpK * i_ratio) + r_in;
}
```

#### 4.2.4 代码实践：阅读 sgemm_tcu_wg 的 kernel

1. **实践目标**：读懂 `wgmma_sync` 在真实 kernel 里的两种用法。
2. **操作步骤**：
   - 打开 [kernel.cpp:82-91](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/tests/regression/sgemm_tcu_wg/kernel.cpp#L82)，找到 `#if defined(WGMMA_RS) && (WGMMA_NRC <= 16)` 分支。
   - 对照 [Makefile:14-21](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/tests/regression/sgemm_tcu_wg/Makefile#L14)：默认 `WGMMA_RS`、`NRC=8`、`-m/-n/-k=16`。
3. **需要观察的现象**：RS 分支调 `load_matrix_sync` 装 A，SS 分支（`WGMMA_SS`）改为构造 `desc_a`。
4. **预期结果**：你能指出 RS 模式下 A 的来源是 `fragment_a`（寄存器），SS 模式下 A 的来源是 `desc_a`（LMEM），两者 B 都来自 `desc_b`。
5. 待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：`wgmma_sync` 为什么用 `static_assert(b_is_smem, ...)` 禁止 B 来自寄存器？
**答**：WGMMA 的 warpgroup 模型依赖 B 在 warpgroup 内广播（共享 bbuf），只有从 LMEM 取才能广播；寄存器是每 warp 私有的，无法跨 warp 共享，故不支持 SR（B-from-register）模式。

**练习 2**：`wgmma_flags<a_is_smem>()` 把 `a_is_smem` 编码进指令的哪一位？硬件为何需要它？
**答**：编码进 `flags` 的 bit 3（[vx_tensor.h:703-708](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/kernel/include/vx_tensor.h#L703)）。硬件（`TcuUopGen` 与 RTL `VX_tcu_uops`）需要它在发射期决定 A 来自寄存器还是 LMEM，从而决定是否需要 setup uop 与 tile-buffer 取数。

---

### 4.3 SimX 建模：TcuUnit、TcuUopGen 与 TcuTbuf

#### 4.3.1 概念说明

SimX 用三个对象协同建模 TCU（[tensor_core_wgmma_engine.md:208-226](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/designs/tensor_core_wgmma_engine.md#L208)）：

- **`TcuUnit`**（继承 `FuncUnit`）：TCU 的执行单元壳。有 `Inputs[Q]`/`Outputs[Q]` 两条 channel（Q = `NUM_TCU_BLOCKS`），`on_tick` 驱动每拍逻辑。它**不**用 `core_->mem_read` 后门——操作数全走 channel（`load_lmem_word` 经 `TcuTbuf`）。
- **`TcuUopGen`**：uop 展开器。把一条 WGMMA 宏指令裂成 `k_steps*NRC` 条 uop，每条带 `(step_m, step_n, step_k)`，并打上 `fu_lock`/`fu_unlock`、`is_first_uop`/`is_last_uop` 标记。
- **`TcuTbuf`**（tile buffer）：`abuf×Q + bbuf×1` 的 line cache，把 Q+1 路取数仲裁到单一 LMEM 端口。

#### 4.3.2 核心流程：一拍内 TcuUnit 做了什么

`TcuUnit::Impl::tick()`（被 [tcu_unit.cpp:1649-1651](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/tcu/tcu_unit.cpp#L1649) 的 `on_tick` 调用）分两遍扫描 Q 个块：

```
Pass 1（仅 WGMMA，[tcu_unit.cpp:540-617]）：
  for 每个 block b：
    若 Inputs[b] 队头是 WGMMA 且为首个 uop (step_m=n=k=0)：
      - CTA 重叠围栏：若别的 block 正在跑不同 CTA 的 WGMMA，暂缓本块
      - 解码 A/B 描述符，invalidate 旧 abuf/bbuf，调 plan_wgmma_lines() 把所需 line 地址灌进 TcuTbuf
      - 标记 in_wgmma_[b]=true，记下 cta_owner
Pass 2（[tcu_unit.cpp:619-636]）：
  所有 active 块的 A/B 操作数都必须 ready，否则整拍 return（tbuf_stalls++）
逐块执行（[tcu_unit.cpp:639-782]）：
  取队头 trace，按 op_type 分派：
    WMMA/WMMA_SP → wmma()
    WGMMA/WGMMA_SP → wgmma()（内含 lockstep 不变量检查）
    TCU_LD → agu_start()（元数据加载）
  算好 delay（kMmaLatency），Outputs[b].try_send(trace, delay) 成功则 pop
```

**关键不变量：CTA lockstep**。共享 bbuf 假设「同一时刻只有一个 CTA 占用 warpgroup」。若某块试图执行与在飞块不同 `cta_id` 的 WGMMA，会触发 `std::abort`（[tcu_unit.cpp:692-705](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/tcu/tcu_unit.cpp#L692)），与 RTL 的 `VX_tcu_lockstep` 一致（[tensor_core_wgmma_engine.md:177-182](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/designs/tensor_core_wgmma_engine.md#L177)）。

#### 4.3.3 源码精读

**uop 计数与展开**（[tcu_unit.cpp:1404-1436](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/tcu/tcu_unit.cpp#L1404)）：

```cpp
uint32_t k_count = is_sparse ? std::max(1u, wg_cfg::k_steps / 2) : wg_cfg::k_steps;
uint32_t mma_uops = k_count * nrc;
return mma_uops + needs_setup;   // needs_setup = FEDP2K && RS && dense
```

展开顺序在 `get()` 里（[tcu_unit.cpp:1580-1599](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/tcu/tcu_unit.cpp#L1580)）：`mma_idx → (k, n, m)`，k 最外、m 最内，首尾 uop 打 `fu_lock`/`fu_unlock`：

```cpp
uint32_t k = mma_idx / mn;  uint32_t rem = mma_idx % mn;
uint32_t n = rem / m_steps; uint32_t m = rem % m_steps;
uop_instr->set_args(IntrTcuArgs{..., m, n, k, first?1:0, last?1:0, 0});
uop_instr->set_fu_lock(uop_index == 0);
uop_instr->set_fu_unlock(uop_index == (total - 1));
```

> 说明：`fu_lock` 锁住功能单元到 `fu_unlock`，保证一个 WGMMA 的所有 uop 连续占有 TCU，不被别的指令插入——这是 lockstep 与 tile-buffer 一致性的前提。

**WGMMA 功能计算**（[tcu_unit.cpp:953-1086](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/tcu/tcu_unit.cpp#L953)）：每个 uop 取出 `tcM×k_words` 的 A 微块和 `tcM×tcN×k_words` 的 B 微块，再交给 `fedp_tile` 做点积。dense 路径（[tcu_unit.cpp:1068-1085](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/tcu/tcu_unit.cpp#L1068)）：

```cpp
fedp_tile(wid, step_m, step_n, step_k, fmt_s, fmt_d,
          a_tile, b_tile, rs3_data, rd_data,
          is_sparse, k_words, k_words, wg_cfg::k_steps * wg_cfg::fedpK);
```

**FEDP 点积核**（[tcu_unit.cpp:1311-1346](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/tcu/tcu_unit.cpp#L1311)）：对微块里每个 \((i,j)\)，从寄存器/LMEM 取一行 A、一列 B，调用格式相关的 FEDP 函数做 \(\Sigma a_k b_k + c\)，结果 NaN-box 后写回：

```cpp
for (uint32_t i = 0; i < cfg::tcM; ++i)
  for (uint32_t j = 0; j < cfg::tcN; ++j) {
    auto c_val = rs3_data.at(i*cfg::tcN + j).u32;        // 累加器旧值
    auto d_val = ... fedp(a_row, b_col, c_val, k_words); // Σ(a·b)+c
    rd_data.at(i*cfg::tcN + j).u64 = nan_box(d_val);
  }
```

格式特化的 `FMA`（如 fp16→fp32，[tcu_unit.cpp:81-87](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/tcu/tcu_unit.cpp#L81)）用 `rvfloats`/softfloat 算 `fmadd`，与 RTL FEDP 后端语义对齐。

**TcuTbuf：line cache + 单端口仲裁**（[tcu_tbuf.h:36-66](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/tcu/tcu_tbuf.h#L36)）。`Q+1` 路（Q 个 abuf + 1 个 bbuf）source 共享一个 LMEM 端口对（`lmem_req_out`/`lmem_rsp_in`）。`plan_*` 登记要取的 line 地址，`ready_*` 表示是否全部驻留，`read_*` 返回驻留的 `mem_block_t`。仲裁逻辑在 `Impl::tick`（[tcu_tbuf.cpp:122-159](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/tcu/tcu_tbuf.cpp#L122)）：每拍排干一个响应、按轮转挑一个有 pending 的 source 发一个请求：

```cpp
// 每拍：先排干一个响应并按 source 路由
for (i in 0..kNumSources) { s = (rr_next_ + i) % kNumSources;
  if (!bufs_[s].pending_q_.empty()) { ... MemReq ... req.send(m, 1); break; }
}
```

> 说明：source ID 编码进 `MemReq::tag` 的高 16 位（[tcu_tbuf.cpp:80-87](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/tcu/tcu_tbuf.cpp#L80)），响应据此自路由回正确的 abuf/bbuf。这正是基数规则在 TCU 的体现——操作数只经 channel 流动。

#### 4.3.4 代码实践：跟踪一次 WGMMA 在 SimX 里的生命周期

1. **实践目标**：把「宏指令 → uop 展开 → 取数 → FEDP → 写回」串成一条调用链。
2. **操作步骤**：
   - 从 [tcu_unit.cpp:639](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/tcu/tcu_unit.cpp#L639)（逐块执行循环）出发。
   - 跟 `case TcuType::WGMMA` → [tcu_unit.cpp:1674](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/tcu/tcu_unit.cpp#L1674) `TcuUnit::wgmma` → `Impl::wgmma`（[tcu_unit.cpp:953](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/tcu/tcu_unit.cpp#L953)）→ `fedp_tile`（[tcu_unit.cpp:1311](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/tcu/tcu_unit.cpp#L1311)）。
   - 再看 uop 是怎么产生的：[tcu_unit.cpp:1440](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/tcu/tcu_unit.cpp#L1440) `TcuUopGen::get`。
3. **需要观察的现象**：`fu_lock` 在第一条 uop 置位、`fu_unlock` 在最后一条置位；中间所有 uop 的 `Inputs[b]` 队头连续推进。
4. **预期结果**：你能画出 `宏指令 → uop_count → get(0..N-1) → 每 uop 进 Inputs[b] → wgmma() → fedp_tile → Outputs[b]` 的时序图。
5. 待本地验证（可加 `DT(3,...)` trace 或设 `VORTEX_TCU_TRACE=1` 观察日志）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `tick()` 的 Pass 2 要求「所有 active 块同时 ready 才推进任何一个」？
**答**：warpgroup 的 Q 个块是 lock-stepped 的，必须同时拿到各自的 A 和共享的 B 才能同步前进；否则先推进的块会在 bbuf 里读到为别的块准备的数据，破坏一致性。

**练习 2**：`TcuTbuf` 为什么把 Q+1 路 source 仲裁到一个 LMEM 端口，而不是每路一个端口？
**答**：LMEM 端口（bank 交叉 SRAM）是稀缺资源；TCU 复用一个端口靠 line cache 吸收重复访问、靠轮转仲裁分时复用，与 RTL `VX_mem_arb` 的 (Q+1→1) 仲裁一致（[tensor_core_wgmma_engine.md:98](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/designs/tensor_core_wgmma_engine.md#L98)），是 model_parity 的落点。

---

### 4.4 2:4 结构化稀疏与 MX 缩放

#### 4.4.1 概念说明

**2:4 结构化稀疏**（structured sparsity）：沿 K 维每连续 4 个元素中**至多 2 个非零**，硬件只存这 2 个非零（压缩）+ 2 位位置元数据。这样 B 的存储与有效乘法数都减半，而矩阵乘结果不变。Vortex 用独立的 opcode `WGMMA_SP`/`WMMA_SP` 区分稀疏（不再是运行时 flag，见 [tensor_core_wgmma_engine.md:66-74](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/designs/tensor_core_wgmma_engine.md#L66)）。

**MX（microscaling）量化**：mxfp8/mxbf8/mxfp4/nvfp4 等「块缩放」格式——每 16 或 32 个元素共享一个 scale 字节。TCU 为 A、B 各维护独立的 scale SRAM，FEDP 时按 K 块选 scale。

#### 4.4.2 核心流程：稀疏 B 的 gather

稀疏 WGMMA 中，B 的 K 维被压缩一半（`kCompression=2`），硬件按元数据从两个候选 word 中 gather 出真正参与乘法的元素：

1. kernel 在 MMA 前 `load_sp_metadata(fragA, meta_sp_ptr)` 发一条 `TCU_LD`，把位置元数据预取进 TCU 的 `sparse_meta_` SRAM（[vx_tensor.h:216-239](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/kernel/include/vx_tensor.h#L216)）。
2. MMA 时，对每个候选 K 位置读两个 B word（`bword0/bword1`），按元数据位 `lo_mask/hi_mask` 选出非零元素。
3. `gather_sparse` 把选中的元素密集打包，再进 FEDP。

#### 4.4.3 源码精读

**gather 函数**（[tcu_unit.cpp:316-333](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/tcu/tcu_unit.cpp#L316)）：从两个候选 word 按 mask 选元素，断言「选中总数 == 一个 word 的元素数」即体现 2:4（选 2 留 2）：

```cpp
static inline uint32_t gather_sparse(uint32_t bword0, uint32_t bword1,
                                     uint32_t lo_mask, uint32_t hi_mask, uint32_t elem_bits) {
  uint32_t elem_count = 32 / elem_bits;   // fp16→2, int8→4
  assert((__builtin_popcount(lo_mask)+__builtin_popcount(hi_mask)) == elem_count);
  uint32_t out = 0, k = 0;
  for (uint32_t i = 0; i < elem_count; ++i)            // 先从 bword0 选
    if (lo_mask & (1u<<i)) out |= ((bword0>>(i*elem_bits))&elem_mask) << (k++*elem_bits);
  for (uint32_t i = 0; i < elem_count; ++i)            // 再从 bword1 选
    if (hi_mask & (1u<<i)) out |= ((bword1>>(i*elem_bits))&elem_mask) << (k++*elem_bits);
  return out;
}
```

> 说明：对 fp16（`elem_count=2`），`lo_mask`/`hi_mask` 各从 `bword0`/`bword1`（各含 2 个 fp16）中选出若干，合计选 2 个——正好是「4 选 2」的 2:4 语义。元数据位从预取的 `sparse_meta_` 按 `(step_m, step_k_half)` bank 读出（[tcu_unit.cpp:1027-1040](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/tcu/tcu_unit.cpp#L1027)）。

**稀疏让 k_count 减半**（[tcu_unit.cpp:1429](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/tcu/tcu_unit.cpp#L1429)）：`k_count = is_sparse ? k_steps/2 : k_steps`——K 维因压缩只需一半的步数，uop 总数也随之减半，这就是「2:4 减少有效计算量」的直接体现。

**TCU_LD 加载元数据**（[vx_tensor.h:222-228](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/kernel/include/vx_tensor.h#L222)）：是一条 custom0 指令，`rd=x0`（SP 命名空间）。它绕开寄存器堆，经独立的 AGU 路径写入 per-warp 元数据 SRAM（[tensor_core_wgmma_engine.md:198-204](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/designs/tensor_core_wgmma_engine.md#L198)）：

```cpp
__asm__ volatile (".insn r %[insn], 2, 2, x0, %[base], x%[fmt]"
    : : [insn]"i"(RISCV_CUSTOM0), [base]"r"(addr), [fmt]"i"(It::id) : "memory");
```

**MX 缩放**：kernel 用 `load_mx_metadata`（[vx_tensor.h:243-263](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/kernel/include/vx_tensor.h#L243)）把 A/B 的 scale 数组分别预取进独立 SRAM（`rd=x16` A 轴、`x17` B 轴）；FEDP 时 `eval_mx_fedp`（[tcu_unit.cpp:1135-1162](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/tcu/tcu_unit.cpp#L1135)）按逻辑行列与 K 块选 scale 字节，并在稀疏时计入 2:4 的逻辑 K 展开。

#### 4.4.4 代码实践：用计数器观察稀疏的省算效果

1. **实践目标**：量化 2:4 稀疏把一次 WGMMA 的 uop 数减半。
2. **操作步骤**：
   - 在 [tcu_unit.cpp:1425-1432](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/tcu/tcu_unit.cpp#L1425) 的 `uop_count` 里，对同一 `NT/NRC` 比较 `is_sparse=false` 与 `true` 的返回值（dense = `k_steps*NRC`，sparse = `(k_steps/2)*NRC`）。
   - 设环境变量 `VORTEX_TCU_TRACE=1` 跑一个稀疏用例，观察 [tcu_unit.cpp:1033-1064](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/tcu/tcu_unit.cpp#L1033) 打印的 `META_RD` 与 `GATHER` 日志。
3. **需要观察的现象**：sparse 路径多了 `META_RD`/`GATHER` 行，但总 uop 数约为 dense 的一半。
4. **预期结果**：dense uops = \(2 \times NRC\)，sparse uops = \(1 \times NRC\)（对 `k_steps=2`）。
5. 待本地验证。

#### 4.4.5 小练习与答案

**练习 1**：为什么稀疏只压缩 B（A 不压缩）？
**答**：2:4 结构化稀疏要求「4 选 2」的非零结构，GEMM 里通常权矩阵（B）天然稀疏且可重排满足该结构；A 是激活值，运行时变化，难以保证结构。Vortex 的实现也只在 B 侧做 gather、K 步数减半，A 侧仍按完整 K 取（见 [tcu_unit.cpp:1022-1067](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/tcu/tcu_unit.cpp#L1022)）。

**练习 2**：`gather_sparse` 的断言 `popcount(lo)+popcount(hi) == elem_count` 在 fp16 下等价于什么？
**答**：fp16 时 `elem_count=2`，断言要求两个 mask 合计选 2 个元素——而候选池 `bword0`（2 个）+ `bword1`（2 个）= 4 个，即「4 选 2」，正是 2:4 稀疏的定义。

---

## 5. 综合实践

把本讲四节串起来，完成一个「**用 WGMMA 跑通 sgemm 并解读一条指令的全栈旅程**」的小任务：

1. **配置并构建**：参照 [ci/testcases/tensor_wg.yaml:63-65](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/testcases/tensor_wg.yaml#L63) 的 `sgemm_tcu_wg-8` 配置，用 `ci/blackbox.sh` 在 SimX 上跑通：

   ```bash
   ./ci/blackbox.sh --driver=simx --app=sgemm_tcu_wg \
     --threads=8 --warps=8 --issue=4 \
     --configs="-DVX_CFG_TCU_WGMMA_ENABLE -DITYPE=fp16 -DOTYPE=fp32 -DWGMMA_NRC=8 -DWGMMA_SS"
   ```

   （`--issue`、`--configs` 等旋钮的具体名字以本地 `ci/blackbox.sh` 实际支持的参数为准；若不符，可直接在 `tests/regression/sgemm_tcu_wg/Makefile` 改 `CONFIGS` 后 `make run-simx`。）

2. **画几何图**：手算 `NT=8, fp16, NRC=8` 的瓦片形状，画出 `tcM×tcN=4×2` 微块如何拼成 `xtileM×xtileN=8×8` 的每 warp 输出瓦片，再由 8 个 warp 拼成 CTA 的 `cta_M×xtileN=64×8` 输出块。

3. **跟踪全栈旅程**：写一份表格，把一条 `wgmma_sync` 从 kernel 到落地逐层标注——
   - **kernel**：[vx_tensor.h:1034](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/kernel/include/vx_tensor.h#L1034) 的 `.insn r 0x0B, 1, 2, ...`；
   - **译码/uop 展开**：[tcu_unit.cpp:1425](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/tcu/tcu_unit.cpp#L1425) `TcuUopGen`，裂成 16 条 uop；
   - **取数**：[tcu_unit.cpp:788](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/tcu/tcu_unit.cpp#L788) `plan_wgmma_lines` → [tcu_tbuf.cpp:122](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/tcu/tcu_tbuf.cpp#L122) `TcuTbuf::tick`；
   - **计算**：[tcu_unit.cpp:953](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/tcu/tcu_unit.cpp#L953) `wgmma` → [tcu_unit.cpp:1311](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/tcu/tcu_unit.cpp#L1311) `fedp_tile`；
   - **时序**：[tcu_unit.cpp:730-736](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/tcu/tcu_unit.cpp#L730) 的 `kMmaLatency` 经 `Outputs[b].try_send(trace, delay)` 承载。

4. **对比 RTL**：打开 [docs/designs/tensor_core_wgmma_engine.md:88-107](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/designs/tensor_core_wgmma_engine.md#L88) 的 RTL 模块表，找到 `TcuUnit`↔`VX_tcu_unit.sv`、`TcuTbuf`↔`VX_tcu_tbuf.sv`、`TcuUopGen`↔`VX_tcu_uops.sv` 的一一对应，体会 model_parity 的物理基础。

**预期结果**：程序打印 `PASSED!` 且退出码 0；你产出一张「分块几何图」+ 一张「全栈旅程表」。若数值或周期对不上 RTL，回到 4.3 的 lockstep 与几何公式排查。无法本地运行时，至少完成第 2、3、4 步的源码阅读与绘图。

## 6. 本讲小结

- **TCU = RISC-V 的矩阵乘加速器扩展**，用 `WMMA`（单 warp，全寄存器）与 `WGMMA`（warpgroup，B 必来自 LMEM）两类指令把 GEMM 装进流水线；WGMMA 靠「共享 B + 每块私有 A + 广播」最大化算术强度。
- **分块几何只由 `NT` 与 `NRC` 决定**：`tcM/tcN/tcK` 是微块，`xtileM/xtileN` 是每 warp 输出瓦片，沿 K 归约 `tileK`；一条 WGMMA 被展开成 `k_steps*NRC` 条 uop，顺序 k 外/n 中/m 内以最大化 A 复用。
- **kernel API `vx_tensor.h`** 用 fragment 抽象 + `wgmma_sync` 内联函数把矩阵乘编码成 custom0 指令（`func3=1` 表 WGMMA），并用 32 位 smem 描述符把 LMEM 矩阵交给硬件。
- **SimX 用 `TcuUnit`（FuncUnit）+ `TcuUopGen`（展开）+ `TcuTbuf`（line cache）三件套建模**，遵循基数规则（操作数只走 channel）与 CTA lockstep 不变量（共享 bbuf 单 CTA 占有），与 RTL 逐模块对应。
- **2:4 结构化稀疏**用独立 opcode（`*_SP`）+ 预取的位置元数据，在 `gather_sparse` 里实现「4 选 2」，让 K 步数与 uop 数减半；**MX 块缩放**用独立的 A/B scale SRAM 在 FEDP 里按 K 块施加。
- **FEDP 后端**（DPI/DSP/BHF/TFR）都算 \(\Sigma a\cdot b + c\)，延迟随 `VX_CFG_TCU_TYPE` 不同，端到端单 uop 延迟 `kMmaLatency` 由 channel delay 承载。

## 7. 下一步学习建议

- **u9-l2（DXA 异步拷贝与多播）**：TCU 的 k-major LMEM 布局正是为 DXA 的 DMA 写入设计的，两讲合读才能看清「DXA 喂数据 → TCU 消费」的完整数据通路。
- **u8-l3（访存合并、本地内存与 DRAM 模型）**：本讲的 `TcuTbuf` 与 `local_mem` 共享同一 LMEM 端口模型，读完 u8-l3 能更懂 line cache 的背压与 bank 交叉。
- **继续阅读源码**：
  - RTL 侧 [hw/rtl/tcu/VX_tcu_uops.sv](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/tcu/VX_tcu_uops.sv) 与 [VX_tcu_core.sv](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/tcu/VX_tcu_core.sv)，对照 SimX 验证 model_parity；
  - 稀疏与 MX 的 kernel 用例 `tests/regression/` 下以 `_sp` / `_mx` 命名的测试；
  - 设计文档 [tensor_core_wgmma_engine.md](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/designs/tensor_core_wgmma_engine.md) 第 7 节列出的「已规划未实现」项，是做二次开发的好选题。

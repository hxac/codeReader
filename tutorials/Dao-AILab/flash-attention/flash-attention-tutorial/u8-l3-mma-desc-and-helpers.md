# UMMA Descriptor 与 Blackwell Helpers

> 本讲对应讲义规格 `u8-l3`，依赖前置讲义 [u8-l1 Blackwell 前向 Kernel 全景](u8-l1-blackwell-forward.md)。
> 阅读本讲前，请确认你已经知道：UMMA（`tcgen05.mma`）是什么、为什么累加器住在片上 **tmem** 而不是寄存器、Blackwell 前向 kernel 是 persistent + warp 专门化的。

## 1. 本讲目标

本讲把 u8-l1 里「一句话带过」的两样东西彻底拆开：

1. **指令描述符 idesc（32 位）**：UMMA 指令「算什么、怎么算」的硬件编码。
2. **共享内存描述符 smem descriptor（64 位）**：UMMA 操作数「在哪里、怎么布局」的硬件编码。

读完本讲，你应当能够：

- 逐位解读 `mma_sm100_desc.py` 里 `make_instr_desc` 打包出的 32 位 idesc，说出每个字段（类型、形状、major、取反、饱和）的含义。
- 逐位解读 64 位 smem descriptor，说出「起始地址 / leading byte offset / stride byte offset / swizzle」分别编码了什么。
- 读懂 `blackwell_helpers.py` 的 `gemm_ptx_*` 系列如何用内联 PTX 把这两类描述符喂给一条 `tcgen05.mma` 指令。
- 说清 2CTA 模式下「共享 mbarrier + `tx_count` 翻倍 + tmem dealloc 屏障」三件套如何让 cluster 内两个 CTA 协作完成一条跨 CTA 的 MMA。

## 2. 前置知识

本讲是「硬件很近、Python 很薄」的一讲。先用大白话把几个术语补齐。

- **描述符（descriptor）**：GPU 硬件单元（如 UMMA）参数太多，不可能全塞进指令的操作数寄存器。约定把一堆参数预先打包成一个定宽整数（这里 32 位或 64 位），指令只引用这个整数。可以类比 CPU 里的「页表项」或「DMA 描述符」——一个数字里浓缩了一整套配置。
- **idesc**：instruction descriptor，编码 MMA 指令本身（元素类型、M/N 形状、是否取反、是否饱和）。
- **smem descriptor**：编码某个操作数在共享内存里的位置与布局（起始地址、行步长、swizzle 模式）。UMMA 的 A/B 操作数若来自 smem，就用它；若来自 tmem，则改用一个 tmem 地址（32 位）。
- **Swizzle**：共享内存里用 XOR 重排行列，避免 bank conflict。UMMA 只认几种合法的 swizzle（`SWIZZLE_32B/64B/128B` 等），不合法的布局硬件拒绝。
- **内联 PTX**：CuTeDSL 里用 `llvm.inline_asm` 直接写一段 PTX 汇编，绕过编译器自己生成 `tcgen05.mma`。本讲的 `blackwell_helpers.py` 大量这样做。
- **2CTA**：把 cluster 设成 `(2,1)`，cluster 内两个 CTA 协同发射同一条 `tcgen05.mma.cta_group::2`，合力算一个更大的 M 块。
- **mbarrier**：按字节数计数的异步屏障，配合 TMA 的 `complete_tx::bytes` 通知「这块 smem 数据搬完了」。前置讲义 [u5-l3 命名屏障与 warp 同步](u5-l3-named-barriers.md) 已讲过它的基本用法。

> 与 u8-l1 的分工：u8-l1 讲了 UMMA **做什么**（tmem 累加、persistent、warp 专门化）；本讲专门讲它的两条「参数管道」——idesc 与 smem descriptor——怎么填、怎么用、2CTA 下怎么协调。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [`flash_attn/cute/mma_sm100_desc.py`](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/mma_sm100_desc.py) | 把 CUTLASS 类型 / smem 布局编码成硬件 idesc（32 位）与 smem descriptor（64 位）。**本讲主角一**。 |
| [`flash_attn/cute/blackwell_helpers.py`](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/blackwell_helpers.py) | 用内联 PTX 包装 `tcgen05.mma` 的 `gemm_ptx_*` 系列，组装 idesc + smem descriptor + K 维循环。**本讲主角二**。 |
| [`flash_attn/cute/flash_fwd_sm100.py`](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd_sm100.py) | Blackwell 前向 kernel，调用上面两个文件，并实现 2CTA cluster 协调。**使用方 + 2CTA 协调现场**。 |
| [`flash_attn/cute/sm100_hd256_2cta_fmha_forward.py`](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/sm100_hd256_2cta_fmha_forward.py) | head_dim=256 专用 2CTA kernel，展示 cluster 级共享 mbarrier 的最完整形态。**2CTA 例子**。 |

## 4. 核心概念与源码讲解

本讲的三个最小模块：

1. **MMA descriptor 字段**——idesc（32 位）与 smem descriptor（64 位）的位域编码。
2. **UMMA GEMM 与布局工具**——`blackwell_helpers.py` 的 `gemm_ptx_*` 系列如何调起一条 `tcgen05.mma`。
3. **2CTA 共享 mbarrier 协调**——cluster 内两 CTA 如何共享屏障、翻倍 `tx_count`、安全释放 tmem。

---

### 4.1 两类描述符：idesc（指令）与 smem descriptor（操作数）

#### 4.1.1 概念说明

一条 `tcgen05.mma` 指令需要回答两类问题：

- **「算什么」**：A/B 元素是什么类型（fp16/bf16/fp8…）？累加器是 fp16/fp32/int32？形状 M×N×K 多大？A/B 谁是 K-major、谁是 MN-major？要不要对 A/B 取负？累加要不要饱和？——这些参数**与具体数据位置无关**，只描述「这次乘法长什么样」，被打包进 **32 位 idesc**。
- **「操作数在哪、怎么排」**：A/B 这两个矩阵块在 smem 的哪个地址、行步长多少、用什么 swizzle？——这些参数**与数据位置有关**，被打包进 **64 位 smem descriptor**（每个操作数一个）。

之所以分两个描述符、两个位宽，是因为：

- idesc 几乎是**编译期常量**（MMA op 一旦选定，类型/形状/major 就定了），适合用 `const_expr` 在编译期算好、直接 `mov.b32 idesc, 0x...` 硬编码进 PTX。
- smem descriptor 的「布局部分」（stride、swizzle）也是编译期常量，但「起始地址部分」是**运行期**值（每个 K 子块、每个 pipeline stage 地址都不同），所以拆成「base（编译期）+ start addr（运行期）」两段，运行时用 `OR` 拼起来。

> 直觉：idesc 像菜谱（红烧肉：用什么肉、多大块、要不要辣），smem descriptor 像食材在厨房哪个货架、货架怎么摆。UMMA 单元同时读这两张「卡片」才知道这次要算什么、去哪取数。

#### 4.1.2 核心流程

描述符的生成链路：

```text
cutlass TiledMma.op  ──►  mma_op_to_idesc  ──►  make_instr_desc  ──►  32 位 idesc
（MmaOp：含 a/b/acc dtype、shape、major）            （位打包）            （编译期常量）

smem 张量 sA  ──►  smem_desc_base_from_tensor  ──►  make_smem_desc_base  ──►  64 位 base（编译期）
                          + sA.iterator（地址）         + make_smem_desc_start_addr  ──►  起始地址（运行期）
                                                                                     （base | start_addr = 完整 64 位）
```

注意 64 位被拆成两个 32 位寄存器传给 PTX（GPU 寄存器是 32 位的），所以 `blackwell_helpers.py` 用 `i64_to_i32x2` 切成 lo/hi 两半。

#### 4.1.3 源码精读

**idesc 的位域**——`make_instr_desc` 把字段逐个左移、`OR` 进一个 32 位整数：

[`mma_sm100_desc.py`:L144-L162](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/mma_sm100_desc.py#L144-L162) — 逐位打包 32 位 idesc。关键字段：

| 位段 | 字段 | 含义 |
| --- | --- | --- |
| 4–5 | `c_format` | 累加器类型：`F16=0 / F32=1 / S32=2` |
| 7–9 | `a_format` | A 元素类型（3 位，编码 fp16/bf16/tf32/int8/fp8…） |
| 10–12 | `b_format` | B 元素类型 |
| 13 | `a_negate` | 是否对 A 取负（`ScaleIn.Neg`） |
| 14 | `b_negate` | 是否对 B 取负 |
| 15 | `a_major` | A 的布局主向：`K=0 / MN=1` |
| 16 | `b_major` | B 的布局主向 |
| 17–22 | `n_dim` | `N >> 3`（N 是 8 的倍数，除以 8 节省位数） |
| 24–28 | `m_dim` | `M >> 4`（M ∈ {64,128,256}，除以 16） |
| 3 | `saturate` | 累加是否饱和截断 |
| 30–31 | `max_shift` | 最大移位（MX 数据类型用） |

类型到编码的映射在两个辅助函数里：[`to_UMMA_format`](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/mma_sm100_desc.py#L68-L90)（A/B，3 位）与 [`to_C_format`](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/mma_sm100_desc.py#L93-L103)（累加器，2 位）。比如 `cutlass.Float16 → F16F32Format.F16=0`、`cutlass.Float32 → CFormat.F32=1`。

M/N 的范围校验与压缩在 [`mma_sm100_desc.py`:L136-L142](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/mma_sm100_desc.py#L136-L142)：

```python
if M not in (64, 128, 256):
    raise ValueError("M must be 64, 128 or 256")
if N < 8 or N > 256 or (N & 7):
    raise ValueError("N must be a multiple of 8 in the range 8…256")
m_dim = M >> 4  # 5-bit field
n_dim = N >> 3  # 6-bit field
```

把 MMA op 转成 idesc 的一行入口是 [`mma_op_to_idesc`](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/mma_sm100_desc.py#L165-L174)：它从 `op.a_dtype/b_dtype/acc_dtype/shape_mnk/a_major_mode/b_major_mode` 取参，把 `OperandMajorMode.K` 映射成 `Major.K`。

**smem descriptor 的位域**——`make_smem_desc_base` 算的是「不含起始地址」的 64 位 base：

[`mma_sm100_desc.py`:L268-L282](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/mma_sm100_desc.py#L268-L282) — 打包 64 位 smem descriptor。关键字段：

| 位段 | 字段 | 含义 |
| --- | --- | --- |
| 0–13 | `start_addr` | smem 起始地址右移 4 位（以 16 字节为单位）——**运行期填** |
| 16–29 | `leading_byte_offset` | 「跨 swizzle atom」的步长（字节，单位 16B） |
| 32–45 | `stride_byte_offset` | 「沿 K/main 维」的步长（字节，单位 16B） |
| 46–47 | `version` | 固定 `1` |
| 49–51 | `base_offset` | CUTLASS 恒置 0 |
| 52 | `lbo_mode` | 恒置 0 |
| 61–63 | `layout_type` | swizzle 家族：`NONE/32B/64B/128B/128B_BASE32B` |

注意低 14 位（start_addr）在 `make_smem_desc_base` 里**故意留空**，由单独的 [`make_smem_desc_start_addr`](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/mma_sm100_desc.py#L285-L287) 在运行期算出、再 `OR` 进去：

```python
def make_smem_desc_start_addr(start_addr: cute.Pointer) -> cutlass.Int32:
    # 14 bits, remove 4 LSB (bits 0-13 in desc)
    return (start_addr.toint() & 0x3FFFF) >> 4
```

之所以右移 4 位，是因为 UMMA 的最小寻址粒度是 16 字节（一个 `uint128`），低 4 位地址位无意义。

**swizzle 家族的判定**——`_layout_type` 从 CuTe 的 `Swizzle<B,M,S>` 三元组反查合法的 UMMA 布局：[`mma_sm100_desc.py`:L191-L209](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/mma_sm100_desc.py#L191-L209)。比如 `Swizzle<3,4,3>` → `SWIZZLE_128B`、`Swizzle<2,5,2>` → `SWIZZLE_128B_BASE32B`，其余三元组直接抛错——硬件不认。

`leading_byte_offset` / `stride_byte_offset` 的推导需要按 A/B 是 K-major 还是 MN-major 走不同分支（`make_smem_desc_base` 里 `if major is Major.MN ... else ...`），本质是把 CuTe 的逻辑布局 `logical_divide` 成 swizzle atom 后取 canonical stride。这一段是纯几何推导，初读不必死磕，知道「它把布局压成了两个字节步长」即可。

#### 4.1.4 代码实践

**实践目标**：手算 FA4 前向 QK MMA 的 idesc，逐位解读，理解它如何指导 UMMA 单元读取 tmem/smem 中的矩阵块。

**操作步骤**：

1. 打开 [`flash_fwd_sm100.py`:L476-L491](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd_sm100.py#L476-L491)，确认 QK MMA 的配置：
   - `q_dtype = k_dtype = Float16`，`qk_acc_dtype = Float32`（默认）；
   - `q_major_mode = k_major_mode = OperandMajorMode.K`（Q、K 都 K-major）；
   - 非 2CTA 时 `mma_tiler_qk = (m_block_size, n_block_size, head_dim_padded)`，典型 `m_block_size=128, n_block_size=128`。
2. 代入 `make_instr_desc` 的位域，手算 idesc（以 `M=128, N=128` 为例）：

   | 字段 | 值 | 位段 | 贡献 |
   | --- | --- | --- | --- |
   | `c_format` | `F32=1` | 4–5 | `1 << 4 = 0x10` |
   | `a_format` | `F16=0` | 7–9 | `0` |
   | `b_format` | `F16=0` | 10–12 | `0` |
   | `a_major` | `K=0` | 15 | `0` |
   | `b_major` | `K=0` | 16 | `0` |
   | `n_dim` | `128>>3=16` | 17–22 | `16 << 17 = 0x200000` |
   | `m_dim` | `128>>4=8` | 24–28 | `8 << 24 = 0x8000000` |
   | 其余 | 0 | — | `0` |

   求和：`idesc = 0x10 | 0x200000 | 0x8000000 = 0x8200010`。
3. 用一句话解读这个数字告诉 UMMA 单元什么：「fp16×fp16 输入、fp32 累加、M=128 行、N=128 列、两个操作数都 K-major、不取反、不饱和」。

**需要观察的现象**：在 `blackwell_helpers.py` 里搜 `mov.b32 idesc,`，你会看到 `gemm_ptx_*` 把算好的 `hex(idesc)` 直接拼进 PTX 字符串（例如 `gemm_ptx` 里的 `f"mov.b32 idesc, {hex(idesc)};"`）。对照你手算的值是否与 PTX 里出现的一致。

**预期结果**：当 `q_dtype=Float16`、`M=N=128`、双 K-major 时，PTX 里 `mov.b32 idesc,` 后应出现 `0x8200010`。

**待本地验证**：不同 `head_dim` / `m_block_size` 组合下 `m_dim/n_dim` 会变；2CTA 模式下 `M` 会翻倍（见 4.3）。请用本讲 4.1.3 的位表代入你实际的 `mma_tiler_qk` 复算，并对照导出的 PTX（见综合实践）确认。

#### 4.1.5 小练习与答案

**练习 1**：把累加器从 fp32 换成 fp16（`qk_acc_dtype=Float16`），idesc 的哪几位会变？变成什么？

> **答案**：`c_format`（位 4–5）从 `F32=1` 变成 `F16=0`，即 idesc 的第 4 位被清零，`0x8200010` 变成 `0x8200000`。

**练习 2**：`make_smem_desc_start_addr` 为什么要 `>> 4`？如果 smem 起始地址是 `0x100`（256 字节偏移），填进 descriptor 低 14 位的是多少？

> **答案**：UMMA 最小寻址粒度是 16 字节（`uint128`），地址低 4 位恒为 0，右移 4 位去掉冗余、把 14 位空间用于 16 字节为单位的偏移。`0x100 >> 4 = 0x10 = 16`，即填 `16`。

**练习 3**：为什么 idesc 是 32 位而 smem descriptor 是 64 位？

> **答案**：idesc 只编码「算什么」（类型/形状/major），字段少、且几乎全是编译期常量，32 位够用且可一条 `mov.b32` 装载；smem descriptor 还要编码运行期的起始地址（14 位）+ 两个字节步长（各 14 位）+ swizzle 等，字段多且需拆成 lo/hi 两个 32 位寄存器传给 PTX，故用 64 位。

---

### 4.2 blackwell_helpers：UMMA GEMM 的 PTX 包装

#### 4.2.1 概念说明

有了 idesc 和 smem descriptor 两张「卡片」，还要有人把它们递给 `tcgen05.mma` 指令——这就是 `blackwell_helpers.py` 干的事。它提供一族 `gemm_ptx_*` 函数，每个对应「一次完整 K 维循环的 MMA」，内部用 `llvm.inline_asm` 直接写 PTX。

为什么不用 CuTeDSL 自带的 `cute.gemm`？因为 Blackwell 的 `tcgen05.mma` 是**异步**指令、操作数可以在 tmem/smem、还有 `accumulate` 谓词、`cta_group`、可选的 mbarrier 同步——标准 `cute.gemm` 抽象盖不全这些细节，FA4 选择手写 PTX 拿到最大控制力（代价是可读性低）。注意 `blackwell_helpers.py` 里也保留了一个走标准路径的 [`gemm`](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/blackwell_helpers.py#L96-L107) 作为对照/回退。

#### 4.2.2 核心流程

一条 `tcgen05.mma` 的 PTX 形如（非 2CTA、A 来自 smem）：

```ptx
tcgen05.mma.cta_group::1.kind::f16  [tmem_acc], smem_desc_a, smem_desc_b, idesc, p;
```

五个操作数的含义：

1. `[tmem_acc]`——累加器在 tmem 的地址（**输出也是它**，原地累加）。
2. `smem_desc_a`——A 操作数的 64 位 smem descriptor（或 `[tmem_a]` 当 A 来自 tmem）。
3. `smem_desc_b`——B 操作数的 64 位 smem descriptor。
4. `idesc`——32 位指令描述符。
5. `p`——谓词：`accumulate`（true=累加进现有 acc，false=覆盖）。第一条 K 子块 `p=0`（覆盖），后续 `p=1`（累加）——这就是 `zero_init` 参数的归宿。

K 维循环就是「发射第一条（覆盖）→ 发射后续若干条（累加）」。`gemm_ptx_*` 系列的差异在于「描述符怎么准备、循环怎么展开」：

| 函数 | 描述符准备方式 | 典型用途 |
| --- | --- | --- |
| [`gemm_ptx`](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/blackwell_helpers.py#L115-L228) | 循环内每步重算 lo | 最基础版 |
| [`gemm_ptx_loop`](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/blackwell_helpers.py#L231-L392) | 用 `offset_diff` 增量加 | 减少地址计算指令 |
| [`gemm_ptx_partial`](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/blackwell_helpers.py#L395-L613) | acc 地址作参数传入 + 可选 mbarrier 同步 | **FA4 PV MMA**（A=P 来自 tmem） |
| [`gemm_ptx_precomputed`](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/blackwell_helpers.py#L796-L973) | 描述符全预算好，运行期只填地址 | 高性能版 |
| [`gemm_ptx_precomputed_varname`](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/blackwell_helpers.py#L1035-L1115) | 用 PTX 寄存器名（`declare_ptx_*` 预声明）复用描述符 | **FA4 QK MMA** |

两个辅助声明函数把编译期常量提前「固定」成 PTX 寄存器名，避免每次 GEMM 重发 `mov`：

- [`declare_ptx_idesc`](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/blackwell_helpers.py#L1020-L1032)——把 idesc 装进一个命名 PTX 寄存器（如 `fa_fwd_qk_mma_idesc`）。
- [`declare_ptx_smem_desc`](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/blackwell_helpers.py#L976-L1017)——把每个 K 子块的 smem descriptor 装进一个 PTX 寄存器数组（`smem_desc_<N>`）。

#### 4.2.3 源码精读

以最基础的 [`gemm_ptx`](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/blackwell_helpers.py#L115-L228) 为例，看 PTX 模板（A 来自 smem 分支）：

[`blackwell_helpers.py`:L183-L207](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/blackwell_helpers.py#L183-L207) — 发射一条 `tcgen05.mma`。要点：

```ptx
with cute.arch.elect_one():                 # 只让一个线程（leader）真正发射
    ...
    mov.b32 idesc, {hex(idesc)};            # 编译期常量直接装载
    mov.b64 smem_desc_a, {$1, {hi_a}};      # 运行期 lo + 编译期 hi 拼成 64 位
    setp.ne.b32 p, $3, 0;                   # p = (not zero_init or k != 0)
    tcgen05.mma.cta_group::1.kind::{kind} [$0], smem_desc_a, smem_desc_b, idesc, p;
```

三个关键技巧：

1. **`elect_one()` + `@leader_thread`**：`tcgen05.mma` 是 CTA 级指令，只需一个线程发射，其余线程空转。`elect.sync` 选出 leader，`@leader_thread` 谓词保护，避免 128 个线程重复发射。
2. **`const_expr` 编译期裁剪**：`is_ts`（A 是否来自 tmem）是编译期布尔，`const_expr(not is_ts)` 在编译期二选一，特化出「A 来自 smem」或「A 来自 tmem（`[tmem_a]`）」两种 PTX，运行时无分支。
3. **`zero_init` → 谓词 `p`**：`pred_str = "p" if isinstance(zero_init, Boolean) else "0" if zero_init else "1"`。首块 `p=0`（覆盖），后续 `p=1`（累加）；当 `zero_init` 是运行期 `Boolean` 时用动态谓词 `"p"`。

K 维循环的地址推进：每个 K 子块的 smem 起址 = `smem_desc_start_lo + (crd2idx((0,0,k), layout) * width // 8) >> 4`，即把逻辑 K 坐标换成 16 字节为单位的字节偏移（[`blackwell_helpers.py`:L172-L179](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/blackwell_helpers.py#L172-L179)）。`gemm_ptx_loop` 优化为只算相邻块的差值 `offset_diff`，循环里用 `add.u32` 增量更新。

**使用方现场**——前向 kernel 怎么挑函数：

[`flash_fwd_sm100.py`:L1576-L1642](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd_sm100.py#L1576-L1642) — 同时配置 QK 与 PV 两组 GEMM：

- 先用 `mma_op_to_idesc` 算两个 idesc、用 `smem_desc_base_from_tensor` 算 Q/K/V 三个 smem base；
- 用 `declare_ptx_smem_desc` / `declare_ptx_idesc` 把 Q 的 smem desc 和两个 idesc 预声明成 PTX 寄存器名；
- `gemm_Si`（QK）用 `gemm_ptx_precomputed_varname`——**A=Q 来自 smem**，复用预声明的 `fa_fwd_q_smem_desc`；
- `gemm_Pi`（PV）用 `gemm_ptx_partial`——**A=P 来自 tmem**（`sA=None`，走 `is_ts` 分支，A 用 `[tmem_a]` 即 tmem 地址），还支持 `split_arrive` 做 SplitKV 的部分到达同步。

```python
qk_mma_idesc, pv_mma_idesc = sm100_desc.mma_op_to_idesc(qk_mma_op), sm100_desc.mma_op_to_idesc(pv_mma_op)
q_smem_base = sm100_desc.smem_desc_base_from_tensor(sQ, sm100_desc.Major.K)
...
sm100_utils.declare_ptx_smem_desc(..., var_name_prefix="fa_fwd_q_smem_desc")
sm100_utils.declare_ptx_idesc(qk_mma_op, var_name="fa_fwd_qk_mma_idesc")
```

#### 4.2.4 代码实践

**实践目标**：在前向 kernel 里定位 QK 与 PV 两次 MMA，分别指出它们的 A 操作数来源（smem 还是 tmem）与使用的 `gemm_ptx_*` 函数。

**操作步骤**：

1. 打开 [`flash_fwd_sm100.py`:L1590-L1642](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd_sm100.py#L1590-L1642)。
2. 对 `gemm_Si[0]`（QK GEMM）：记下它调用的函数名（`gemm_ptx_precomputed_varname`）、传入的 `smem_var_name_prefix`（`fa_fwd_q_smem_desc`，说明 A=Q 来自 smem）、`idesc_var_name`（`fa_fwd_qk_mma_idesc`）、`smem_desc_base_b`（`k_smem_base`，B=K 来自 smem）、`cta_group`。
3. 对 `gemm_Pi[0]`（PV GEMM）：记下它调用 `gemm_ptx_partial`、`sA=None`（**A=P 来自 tmem**）、`tCrA=tOrP`（P 在 tmem）、`smem_desc_base_b` 缺省（V 的 base 在函数内部从 `sB` 取）。
4. 画一张对照表：

   | GEMM | A 来源 | B 来源 | 函数 | idesc |
   | --- | --- | --- | --- | --- |
   | QK (gemm_Si) | Q @ smem | K @ smem | `gemm_ptx_precomputed_varname` | `fa_fwd_qk_mma_idesc` |
   | PV (gemm_Pi) | P @ tmem | V @ smem | `gemm_ptx_partial` | `fa_fwd_pv_mma_idesc` |

**需要观察的现象**：QK 的 A 用 64 位 smem descriptor（`smem_desc_a`），PV 的 A 用 tmem 地址（PTX 里是 `[tmem_a]` 而非 `smem_desc_a`）——这正是 4.1 里「操作数来自 smem 用 descriptor、来自 tmem 用地址」的体现。

**预期结果**：你能指出 PV 分支在 `blackwell_helpers.py` 里走的是 `is_ts=True`（`else`）分支（[`L523-L613`](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/blackwell_helpers.py#L523-L613)），PTX 里 A 操作数写作 `[tmem_a + offset]`。

#### 4.2.5 小练习与答案

**练习 1**：`gemm_ptx` 里为什么用 `elect_one()` 只让一个线程发射 `tcgen05.mma`？如果所有线程都发射会怎样？

> **答案**：`tcgen05.mma` 是 CTA 级（甚至 cluster 级）指令，硬件只需一个线程发起即可完成整块 MMA；多个线程重复发射是非法/冗余的（语义上要求同一 warp 一致发起）。`elect.sync` 选 leader 后用 `@leader_thread` 谓词保护，保证只发一次。

**练习 2**：`zero_init=True` 时，第一条 MMA 的谓词 `p` 是 0 还是 1？为什么？

> **答案**：`p=0`（覆盖）。`zero_init=True` 表示「从零开始、不累加进旧值」，故首条指令用 `p=0` 让硬件直接覆盖 acc；后续 K 子块 `p=1` 累加。

**练习 3**：QK 的 A（Q）为什么能用 `declare_ptx_smem_desc` 预声明，而 PV 的 A（P）不能？

> **答案**：Q 常驻 smem、其 smem descriptor 的布局部分是编译期常量，可预声明成 PTX 寄存器名复用；P 住在 tmem，A 操作数用的是 tmem 地址（运行期值，每个 pipeline stage 不同），没有「smem descriptor」可预声明，只能每次传入 tmem 地址。

---

### 4.3 2CTA：cluster 内两 CTA 协作与共享 mbarrier

#### 4.3.1 概念说明

2CTA（`use_2cta_instrs=True`）让 cluster 形状变成 `(2,1)`——cluster 内两个 CTA 在 M 维上并排，合力发射**一条** `tcgen05.mma.cta_group::2` 指令，算一个两倍大的 M 块。每个 CTA 仍只拥有 `m_block_size` 行、只写自己的 O 份，但 MMA 指令跨两个 CTA。

这带来三个必须协调的点：

1. **指令翻倍**：`mma_tiler` 的 M 维 = `cta_group_size * m_block_size`，idesc 里的 `m_dim` 也随之翻倍；PTX 用 `cta_group::2`。
2. **数据翻倍**：一条 2CTA TMA 加载要填满两个 CTA 的 smem，所以 `tx_count`（mbarrier 期待的字节数）必须 `*= cta_group_size`。
3. **生命周期翻倍**：tmem 分配/释放、softmax→correction 的跨 warp 信号，都要跨 CTA 同步——靠 **cluster 级共享 mbarrier** 与专门的 **tmem dealloc mbarrier**。

> 与 u5-l3 的衔接：u5-l3 讲过「命名屏障管线程到齐、mbarrier 管字节到齐」。2CTA 把 mbarrier 的「字节到齐」阈值翻倍，并新增「跨 CTA 的 tmem 生命周期屏障」。

#### 4.3.2 核心流程

```text
use_2cta_instrs=True
   │
   ├── cluster_shape_mn = (2,1)            # cluster 内 2 个 CTA
   ├── cta_group_size = 2
   ├── mma_tiler_qk = (2*m_block_size, n, hd)   # MMA 覆盖两 CTA
   ├── cta_group = tcgen05.CtaGroup.TWO    # 选 2CTA MMA op
   ├── tma_copy_bytes[name] *= 2           # 共享 mbarrier 期待字节翻倍
   ├── UMMA pipeline 的非 MMA 侧线程数 *= 2（softmax_warps_cluster 等）
   ├── tmem = TmemAllocator(is_two_cta=True, two_cta_tmem_dealloc_mbar_ptr=...)
   └── PTX: tcgen05.mma.cta_group::2.kind::{kind} ...
```

#### 4.3.3 源码精读

**cluster 与 tiler 配置**——[`flash_fwd_sm100.py`:L171-L180](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd_sm100.py#L171-L180)：

```python
self.use_2cta_instrs = use_2cta_instrs
self.cta_group_size = 2 if self.use_2cta_instrs else 1
# With 2CTA, the MMA tiler M covers both CTAs, so it's cta_group_size * m_block_size.
self.mma_tiler_qk = (self.cta_group_size * m_block_size, n_block_size, self.head_dim_padded)
self.cluster_shape_mn = (2, 1) if self.use_2cta_instrs else (1, 1)
```

注意注释明确：「MMA tiler M 覆盖两个 CTA，每个 CTA 只拥有 `m_block_size` 行」——这正是 2CTA 的工作划分。

**选 2CTA MMA op + `tx_count` 翻倍**——[`flash_fwd_sm100.py`:L476-L477](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd_sm100.py#L476-L477) 选 `CtaGroup.TWO` 的 MMA op；[`L568-L569`](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd_sm100.py#L568-L569) 把 Q/K/V 的 TMA 字节数翻倍：

```python
for name in ("Q", "K", "V"):
    self.tma_copy_bytes[name] *= self.cta_group_size
```

这条 `tx_count` 喂给 UMMA-bridging pipeline 的共享 mbarrier——它要等到「两个 CTA 的 smem 都填满」才放行 consumer。

**非 MMA 侧线程数翻倍**——softmax/correction 是「UMMA 桥接」的另一侧，在 2CTA 下要同时服务两个 CTA，所以线程数乘以 `cta_group_size`：[`flash_fwd_sm100.py`:L908-L918](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd_sm100.py#L908-L918)：

```python
# For UMMA-bridging pipelines: the non-MMA side spans both CTAs in the cluster,
# so the thread count must include warps from both CTAs.
softmax_warps_cluster = ThreadCooperativeGroup(len(self.softmax0_warp_ids) * self.cta_group_size)
correction_threads_cluster = ThreadCooperativeGroup(cute.arch.WARP_SIZE * len(self.correction_warp_ids) * self.cta_group_size)
```

**tmem 生命周期的跨 CTA 屏障**——这是 2CTA 最关键的安全机制。tmem 是 cluster 内两 CTA 共享的资源，必须等**两个 CTA 的所有使用者**都结束才能释放。`TmemAllocator` 用一个专门的 dealloc mbarrier：[`flash_fwd_sm100.py`:L875-L891](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd_sm100.py#L875-L891)：

```python
tmem_alloc_barrier = pipeline.NamedBarrier(
    barrier_id=int(NamedBarrierFwdSm100.TmemPtr),
    num_threads=cute.arch.WARP_SIZE * len((self.mma_warp_id, *self.softmax0_warp_ids, ...)),
)
tmem = cutlass.utils.TmemAllocator(
    storage.tmem_holding_buf.ptr,
    barrier_for_retrieve=tmem_alloc_barrier,
    allocator_warp_id=self.mma_warp_id,
    is_two_cta=self.use_2cta_instrs,
    two_cta_tmem_dealloc_mbar_ptr=storage.tmem_dealloc_mbar.ptr,   # ← 跨 CTA dealloc 屏障
)
```

只有 MMA warp 拥有分配权，其余 warp（softmax/correction）靠 `tmem.retrieve_ptr` + `tmem_alloc_barrier.arrive_and_wait()` 取指针（[`L1246-L1247`](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd_sm100.py#L1246-L1247)）。MMA warp 算完后 `tmem.relinquish_alloc_permit()` + `tmem_alloc_barrier.arrive_and_wait()` + `tmem.free(tmem_ptr)`（[`L1212-L1214`](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd_sm100.py#L1212-L1214)）。2CTA 下 `two_cta_tmem_dealloc_mbar_ptr` 确保 dealloc 等到 cluster 内两个 CTA 都到达才执行——否则一个 CTA 先释放、另一个还在读，就会读到垃圾。

**最完整的 2CTA 共享 mbarrier 形态**——hd256 专用 kernel（cluster 恒为 `(2,1)`）显式列出了一批 cluster 级 mbarrier：[`sm100_hd256_2cta_fmha_forward.py`:L520-L538](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/sm100_hd256_2cta_fmha_forward.py#L520-L538)：

```python
load_q_mbar_ptr: ...           # TMA Q 加载完成通知（producer load → consumer mma）
load_kv_mbar_ptr: ...          # TMA K/V 加载完成通知
mma_s_mbar_ptr: ...            # MMA → softmax（S 块就绪）
p_mma_mbar_ptr: ...            # softmax → MMA（P 块就绪）
s_corr_mbar_ptr: ...           # softmax → correction
sum_mbar_ptr: ...              # row_sum 就绪
mma_corr_mbar_ptr: ...         # MMA → correction（O_partial token 就绪，触发 rescale）
tmem_dealloc_mbar: Int64       # ← CTA-wide tmem 生命周期屏障
```

每个 pipeline 创建时都传 `cta_layout_vmnk=cluster_layout_vmnk`（[`L638/L646/L656/L666`](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/sm100_hd256_2cta_fmha_forward.py#L636-L668)），把它们登记为 cluster 级（跨 CTA）屏障。

#### 4.3.4 代码实践

**实践目标**：阅读 hd256 2CTA kernel，列出 cluster 级共享 mbarrier 清单，并解释 `tmem_dealloc_mbar` 为何必须是 cluster 级。

**操作步骤**：

1. 打开 [`sm100_hd256_2cta_fmha_forward.py`:L520-L545](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/sm100_hd256_2cta_fmha_forward.py#L520-L545)，抄下 `SharedStorage` 里所有 `*_mbar_ptr` 字段。
2. 对每个 mbarrier，在 [`L630-L690`](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/sm100_hd256_2cta_fmha_forward.py#L630-L690) 找到它创建时的 `producer`/`consumer` 与 `cta_layout_vmnk`，填表：

   | mbarrier | 生产者 | 消费者 | 作用 |
   | --- | --- | --- | --- |
   | `load_q_mbar_ptr` | load warp（TMA） | mma warp | Q 块 smem 填满 |
   | `mma_s_mbar_ptr` | mma warp | softmax warp | S 块在 tmem 算完 |
   | ... | ... | ... | ... |

3. 回答：为什么 `tmem_dealloc_mbar` 不能用普通命名屏障，而必须是 cluster 级 mbarrier？

**需要观察的现象**：所有 UMMA-bridging mbarrier 创建时都带 `cta_layout_vmnk=cluster_layout_vmnk`，且 `num_threads` / `tx_count` 都按 cluster 宽度（`* cluster_shape_mnk[0]`）放大。

**预期结果**：你得出结论——`tmem_dealloc_mbar` 必须跨 CTA，因为 tmem 分配是 cluster 级共享资源，必须等两个 CTA 的 mma + softmax + correction 全部用完才能安全释放；用单 CTA 屏障会导致一个 CTA 先释放、另一个 CTA 的 MMA 读到已释放的 tmem。

#### 4.3.5 小练习与答案

**练习 1**：开启 2CTA 后，idesc 的 `m_dim` 字段会怎么变？以 `m_block_size=128` 为例。

> **答案**：`mma_tiler_qk` 的 M 从 `128` 变成 `2*128=256`，`m_dim = 256 >> 4 = 16`（而单 CTA 时是 `128>>4=8`）。idesc 编码的是**完整 M（含两 CTA）**，配合 PTX 的 `cta_group::2` 硬件才知道这是跨 CTA 指令。

**练习 2**：为什么 `tma_copy_bytes[name] *= cta_group_size`？

> **答案**：2CTA 下一条 TMA `cp.async.bulk` 要同时填满 cluster 内两个 CTA 的 smem 缓冲，搬的总字节数翻倍；共享 mbarrier 的 `tx_count` 必须也翻倍，否则 `complete_tx::bytes` 永远凑不齐、consumer 死等——这正是 2CTA 死锁的典型成因之一（详见 [AI/DEBUG_2CTA.md](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/AI/DEBUG_2CTA.md)，下一讲 u8-l4 会深入）。

**练习 3**：每个 CTA 写多少行的 O？是 `2*m_block_size` 还是 `m_block_size`？

> **答案**：`m_block_size`。注释「epi_tile is per-CTA (not full 2CTA) since each CTA writes its own O portion」（[`L507-L508`](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd_sm100.py#L507-L508)）说明：MMA 指令跨两 CTA 算 2 倍 M，但输出 O 由每个 CTA 各自写自己那 `m_block_size` 行。

---

## 5. 综合实践

**任务**：追踪 FA4 Blackwell 前向 **QK MMA** 从「cutlass MMA op」到「最终 PTX 指令」的完整链路，把本讲三个模块串起来。

**步骤**：

1. **起点**：在 [`flash_fwd_sm100.py`:L484-L491](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd_sm100.py#L484-L491) 找到 `make_trivial_tiled_mma(...)`，记录它产出的 `tiled_mma_qk.op`（含 a/b/acc dtype、shape、major）。
2. **算 idesc**：跟随 [`L1576`](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd_sm100.py#L1576) 的 `mma_op_to_idesc(qk_mma_op)` → `make_instr_desc`，用 4.1.4 的位表手算 idesc（单 CTA 与 2CTA 各算一次，对比 `m_dim`）。
3. **算 smem descriptor**：跟随 [`L1578-L1581`](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd_sm100.py#L1578-L1581) 的 `smem_desc_base_from_tensor(sQ, Major.K)` / `(sK, Major.K)`，确认 Q、K 都 K-major。
4. **预声明**：看 [`flash_fwd_sm100.py`:L1583-L1585](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd_sm100.py#L1583-L1585) 的 `declare_ptx_smem_desc` / `declare_ptx_idesc` 把它们固定成 PTX 寄存器名。
5. **发射**：跟随 `gemm_Si` → `gemm_ptx_precomputed_varname`（[`blackwell_helpers.py`:L1035-L1115](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/blackwell_helpers.py#L1035-L1115)），定位最终的 `tcgen05.mma.cta_group::{cta_group}.kind::{kind} [tmem_acc], {smem_var}_0, smem_desc_b_0, {idesc_var}, {pred_str}`。
6. **验证**：设 `CUTE_DSL_KEEP_PTX=1` 与 `CUTE_DSL_LINEINFO=1` 跑一次 Blackwell 前向（参考 [u1-l3](u1-l3-install-and-first-run.md) 的运行方式），在导出的 PTX 里搜 `tcgen05.mma` 与 `mov.b32 idesc,`，核对你手算的 idesc 与导出值是否一致。

**产出**：一张从 `MmaOp` → `idesc/smem_desc` → PTX 寄存器名 → `tcgen05.mma` 的完整追踪表，并标注 2CTA 开/关时 `m_dim`、`cta_group::`、`tx_count` 三处的差异。

> 若无 Blackwell GPU，第 6 步可改为纯源码阅读：在 PTX 模板字符串里手动代入你算出的 `hex(idesc)`，确认它会被拼进 `mov.b32 idesc, {hex(idesc)}`。导出 PTX 验证标记为「待本地验证」。

## 6. 本讲小结

- UMMA 用**两类描述符**配置一次 MMA：32 位 **idesc** 编码「算什么」（类型/形状/major/取反/饱和，几乎全是编译期常量），64 位 **smem descriptor** 编码「操作数在哪、怎么排」（起始地址 + leading/stride byte offset + swizzle）；运行期只有起始地址（低 14 位）会变，靠 `base | start_addr` 拼接。
- [`mma_sm100_desc.py`](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/mma_sm100_desc.py) 提供 `mma_op_to_idesc`/`make_instr_desc`（idesc）与 `smem_desc_base_from_tensor`/`make_smem_desc_base`/`make_smem_desc_start_addr`（smem descriptor）两条生成链；M/N 用 `>>4`、`>>3` 压缩是因为最小粒度是 16 字节、且 M∈{64,128,256}、N 是 8 的倍数。
- [`blackwell_helpers.py`](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/blackwell_helpers.py) 的 `gemm_ptx_*` 系列用内联 PTX 把这两类描述符喂给 `tcgen05.mma`，靠 `elect_one()` 只让 leader 发射、`const_expr` 编译期裁剪 smem/tmem 分支、`zero_init`→谓词 `p` 控制「覆盖 vs 累加」；QK 用 `gemm_ptx_precomputed_varname`（A=Q 在 smem），PV 用 `gemm_ptx_partial`（A=P 在 tmem，支持 mbarrier 同步）。
- **2CTA** 把 cluster 设 `(2,1)`、`mma_tiler` 的 M 翻倍、PTX 用 `cta_group::2`；配套要 `tma_copy_bytes *= cta_group_size`、softmax/correction 线程数翻倍、以及 cluster 级共享 mbarrier 与专门的 `tmem_dealloc_mbar`——后者保证两 CTA 都用完 tmem 才释放，是 2CTA 正确性的关键。

## 7. 下一步学习建议

- **下一讲 [u8-l4 hd256 2CTA 专用 Kernel](u8-l4-hd256-2cta-kernel.md)**：把本讲的 2CTA 协调推向极致——hd256 必须用 2CTA，且 `tx_count` 未乘 `cta_group_size`、屏障错配会直接死锁。配合 [`AI/DEBUG_2CTA.md`](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/AI/DEBUG_2CTA.md) 学排查方法。
- **回看 [u5-l3 命名屏障与 warp 同步](u5-l3-named-barriers.md)**：本讲的 mbarrier / `tx_count` / `barrier_id` 都建立在那里，值得对照重读。
- **延伸阅读 CUTLASS 原始 C++ 头**：`mma_sm100_desc.py` 顶部注释指向了 CUTLASS 的 `include/cute/arch/mma_sm100_desc.hpp` 与 `mma_traits_sm100.hpp`，若想理解「为什么位域这样排」可对照硬件 ISA 文档。
- **导出 PTX 实操**：本讲的位域全是确定的位运算，设 `CUTE_DSL_KEEP_PTX=1` 跑一次前向，用你手算的 idesc 对照 PTX 里的 `mov.b32 idesc,`，是验证「我真的看懂了」的最直接方式（详见综合实践第 6 步）。

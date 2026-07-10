# CMAC：乘加阵列与定点/浮点计算

> 本讲承接 u3-l4（CSC 时隙/条带控制器）。CSC 已把 CBUF 中的特征图（dat）与权重（wt）按卷积节拍对齐好，并广播给 MAC 阵列。本讲进入卷积主流水线的第四级——CMAC，看那一排排乘加单元到底如何把“特征 × 权重”算成“部分和（partial sum）”。

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 CMAC 在卷积主流水线（CDMA→CBUF→CSC→**CMAC**→CACC）中的位置与职责：它只做“大规模并行乘加 + 部分积压缩”，不做最终累加。
- 用源码数字解释“2048 个 INT8 MAC / 1024 个 INT16 或 FP16 MAC”是怎么拼出来的：两半阵列 × 8 个 cell × 64 个乘法器。
- 读懂一个叶子乘法器 `NV_NVDLA_CMAC_CORE_MAC_mul` 的 Booth 基 4 编码、CSA 压缩树，以及它在 INT8/INT16/FP16 三种精度下如何复用同一套硬件。
- 理解 `exp`（FP16 指数对齐）、`nan`（异常传播）两个配套单元的作用。
- 解释 CMAC 的影偶（shadow）寄存器与 `op_en`/producer/consumer 控制逻辑，以及 SLCG 时钟门控如何省电。

## 2. 前置知识

- **MAC（Multiply-Accumulate，乘加运算）**：神经网络里最常见的运算，一次“乘一个数、加到累加器”。算力通常用“每秒多少次 MAC”衡量。NVDLA 的算力 = MAC 个数 × 核心频率。
- **卷积 = 大量点积**：一个输出通道的一点点结果，等于“输入通道维上 64 个 (dat × wt) 再求和”。这 64 就是 reduction（缩减）维，对应 `MAC_ATOMIC_C_SIZE_64`。
- **部分和（partial sum）**：尚未累加完成的中间结果。CMAC 把乘积用“压缩树”压成冗余形式（sum/carry 两半）交给 CACC，由 CACC 做最终的进位传播与跨批次累加。这样 CMAC 内部不用放大位宽的加法器，时序更好。
- **Booth 编码**：一种把二进制乘法转成“少量部分积相加”的技巧。基 4（radix-4）Booth 每次看 3 位、产生 ±0/±1/±2 倍的部分积，能把手算乘法里的部分积数目减半，从而缩小后续加法树。
- **CSA 压缩树（Carry-Save Adder tree）**：用 3:2 压缩器把很多个数压成“和 + 进位”两路，不在每级做进位传播，直到最后一步才用普通加法器合并。它是高速乘法器的核心结构。
- **影偶寄存器（shadow / dual register）**：CPU 写一组、引擎用另一组，靠 producer/consumer 指针切换，实现“当前层在跑时下一层参数已经在悄悄装载”。这一机制在 u2-l3 已以 CDMA 为例讲过，CMAC 沿用同样套路。
- **FP16 与指数（exponent）**：FP16 浮点数由符号、指数、尾数组成。要把两个浮点数先对齐再相加，就要先把尾数按“指数差”移位。CMAC 在做 FP16 卷积时，必须先求出同一条带内 64 路的“最大指数”，再把每路尾数积右移对齐。
- **NaN（Not a Number）**：浮点运算里的特殊值，表示“非法结果”。硬件需要在出现 NaN 时把它传播到输出，而不是算出一个看似正常却错误的数。

## 3. 本讲源码地图

CMAC 全部源码集中在 `vmod/nvdla/cmac/` 目录，是一个自上而下的清晰层次：

| 文件 | 角色 | 大小特征 |
|------|------|----------|
| `NV_NVDLA_cmac.v` | CMAC 半个阵列的顶层包装，例化 datapath（u_core）+ 寄存器（u_reg） | 端口极宽（128 字节 dat + 128 字节 wt） |
| `NV_NVDLA_CMAC_core.v` | 半阵列的数据通路，例化 cfg/rt_in/active/**8 个 mac cell**/rt_out/slcg | 例化 8 个 `CORE_mac` |
| `NV_NVDLA_CMAC_CORE_mac.v` | **一个输出通道的 MAC cell**，例化 64 个叶子乘法器 + exp + nan + CSA 树 | 例化 64 个 `MAC_mul` |
| `NV_NVDLA_CMAC_CORE_MAC_mul.v` | **叶子乘法器**：Booth 编码 + CSA 压缩，支持 int8/int16/fp16 | 本讲最核心的算术单元 |
| `NV_NVDLA_CMAC_CORE_MAC_booth`（在 mul 文件内） | Booth 选择器子单元 | |
| `NV_NVDLA_CMAC_CORE_MAC_exp.v` | FP16 指数最大值与每路移位量计算 | |
| `NV_NVDLA_CMAC_CORE_MAC_nan.v` | NaN 检测与树形传播 | |
| `NV_NVDLA_CMAC_CORE_cfg.v` | 精度/模式译码，产出 cfg_is_int8/int16/fp16/wg | |
| `NV_NVDLA_CMAC_CORE_active.v` | 输入激活：非零标志(nz)、nan 标志、FP16 指数收集 | 体积巨大（约 2MB） |
| `NV_NVDLA_CMAC_CORE_slcg.v` | 二级时钟门控单元 | |
| `NV_NVDLA_CMAC_reg.v` | 寄存器顶层：影偶选择、op_en、producer/consumer、status、CSB 接口 | 手写“大脑” |
| `NV_NVDLA_CMAC_REG_dual.v` | 自动生成的影偶寄存器（MISC_CFG、OP_ENABLE） | RDL/Ordt 生成 |
| `NV_NVDLA_CMAC_REG_single.v` | 自动生成的单组寄存器（POINTER、STATUS） | RDL/Ordt 生成 |

阵列规模相关定义在 `spec/defs/nv_full.spec`，物理双半例化在 `vmod/nvdla/top/NV_NVDLA_partition_m.v`。

## 4. 核心概念与源码讲解

### 4.1 CMAC 阵列总体结构：两半 × 8 cell × 64 乘法器

#### 4.1.1 概念说明

CMAC（Convolution MAC，卷积乘加阵列）是卷积核心里最“重”的运算资源。它的任务很纯粹：**接收 CSC 送来的对齐好的 dat 与 wt，做大量并行乘法，把乘积压成部分和，交给 CACC**。它自己**不保存最终结果**，也**不做跨批次的累加**——这些都由 CACC 完成。这种“乘加阵列只管算、累加器只管攒”的分工，是 NVDLA 流水线高速运转的关键。

整个 CMAC 在物理上分为**两半**：

- **cmac_a / cmac_b**：两半阵列**接收同一份 dat（特征数据广播）**，但**各自使用不同的 wt（权重）**。于是在同一拍里，两半针对同一批输入数据、并行算出**两组不同的输出通道**，把输出通道吞吐翻倍。

每一半的内部结构是相同的层次：

```
NV_NVDLA_cmac（半阵列）
├── u_reg   : NV_NVDLA_CMAC_reg        ← 影偶寄存器 + op_en 控制
└── u_core  : NV_NVDLA_CMAC_core       ← 数据通路
              ├── u_cfg    : 精度/模式译码
              ├── u_rt_in  : 输入重定时（流水寄存器）
              ├── u_active : 激活（nz/nan/exp 收集）
              ├── u_mac_0..u_mac_7 : 8 个输出通道 cell
              ├── u_rt_out : 输出重定时
              └── u_slcg_*: 二级时钟门控
```

而**一个 cell**（`NV_NVDLA_CMAC_CORE_mac`）内部是 64 个叶子乘法器 + 1 个 exp + 1 个 nan + 一棵 CSA 压缩树：

```
NV_NVDLA_CMAC_CORE_mac（一个输出通道 cell）
├── u_mul_0 .. u_mul_63 : 64 个叶子乘法器（= C 维 reduction 深度）
├── u_exp  : FP16 指数对齐
├── u_nan  : NaN 传播
└── CSA tree（DW02_tree 多级）→ mac_out_data[175:0]
```

#### 4.1.2 核心流程：算力是怎么数出来的

NVDLA 的算力规格锁死在 `spec/defs/nv_full.spec`：

[spec/defs/nv_full.spec:L16-L17](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/spec/defs/nv_full.spec#L16-L17) 定义了 `MAC_ATOMIC_C_SIZE_64` 与 `MAC_ATOMIC_K_SIZE_32`，分别对应卷积阵列的两个维度。

由此可以精确推导算力：

\[
\text{每半乘法器数} = 8\;(\text{cell}) \times 64\;(\text{每 cell 的 mul}) = 512
\]

\[
\text{全阵列乘法器数} = 512 \times 2\;(\text{两半}) = 1024
\]

- 在 INT16 / FP16 模式下，每个乘法器算 1 个乘法 → **1024 个 MAC**。
- 在 INT8 模式下，每个乘法器**同时算 2 个 8×8 乘法**（见 4.3）→ **2048 个 MAC**。

这正是 u3-l1 提到的“2048 INT8 = 1024 INT16”的来源。两个维度含义如下：

- `MAC_ATOMIC_C_SIZE_64` = 每个 cell 里 64 个乘法器沿**输入通道（C）缩减维**做点积的深度。
- `MAC_ATOMIC_K_SIZE_32` = 一次原子操作覆盖的**输出通道（K）批量**；由两半阵列与多个节拍共同覆盖。

数据宽度上也呈现“窄进宽出”：dat/wt 各 1024 位进，8 路部分和（每路 176 位冗余表示）出，最后由 CACC 拼装。

#### 4.1.3 源码精读：半阵列顶层与双半例化

先看半阵列顶层 `NV_NVDLA_cmac.v` 的端口，直观感受“一拍吃多少数据、吐多少结果”。它的输入是 CSC 送来的 128 字节 dat 与 128 字节 wt（广播给两半的是同一份 dat），输出是 8 路 176 位部分和：

[NV_nvdla/cmac/NV_NVDLA_cmac.v:L310-L321](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cmac/NV_NVDLA_cmac.v#L310-L321) 声明了向 CACC 输出的 8 路 `mac2accu_data0..7`（各 176 位）加 `mask`/`mode`/`pvld`。每个 cell 产出一个输出通道的部分和。

该顶层只例化两个子模块：数据通路 `u_core` 与寄存器 `u_reg`：

[NV_nvdla/cmac/NV_NVDLA_cmac.v:L600-L603](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cmac/NV_NVDLA_cmac.v#L600-L603) 例化 `NV_NVDLA_CMAC_core u_core`，把所有 sc2mac_dat/wt 喂进去、把 mac2accu 结果送出。

[NV_nvdla/cmac/NV_NVDLA_cmac.v:L890-L903](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cmac/NV_NVDLA_cmac.v#L890-L903) 例化 `NV_NVDLA_CMAC_reg u_reg`，接 CSB 配置总线、输出 `reg2dp_conv_mode`/`reg2dp_op_en`/`reg2dp_proc_precision` 等控制信号。

注意：本文件内部信号一律叫 `cmac_a_*`，但它是“半个阵列”的通用模块。它被例化在 `partition_m` 里：

[NV_nvdla/top/NV_NVDLA_partition_m.v:L635-L642](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/top/NV_NVDLA_partition_m.v#L635-L642) 在 partition_m 中例化 `NV_NVDLA_cmac u_NV_NVDLA_cmac`。而 partition_m 自身在顶层被例化两次（`u_partition_ma`、`u_partition_mb`，见 u1-l5），于是构成 cmac_a / cmac_b 两半——这就是“同数据、异权重、双倍输出通道”的物理基础。

再看数据通路 `NV_NVDLA_CMAC_core.v` 里例化的 8 个 cell 与配套单元：

[NV_nvdla/cmac/NV_NVDLA_CMAC_core.v:L1064-L1064](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cmac/NV_NVDLA_CMAC_core.v#L1064) `u_cfg`：精度译码。

[NV_nvdla/cmac/NV_NVDLA_CMAC_core.v:L1655-L1655](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cmac/NV_NVDLA_CMAC_core.v#L1655) `u_active`：输入激活（nz/nan/exp）。

[NV_nvdla/cmac/NV_NVDLA_CMAC_core.v:L2059-L2276](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cmac/NV_NVDLA_CMAC_core.v#L2059-L2276) 连续例化 `u_mac_0` … `u_mac_7` 共 8 个输出通道 cell。

[NV_nvdla/cmac/NV_NVDLA_CMAC_core.v:L2444-L2564](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cmac/NV_NVDLA_CMAC_core.v#L2444-L2564) 例化 11 个 `u_slcg_op_*`（操作域时钟门控）与若干 `u_slcg_wg_*`（Winograd 域时钟门控）。

#### 4.1.4 代码实践：用源码核对算力

1. **实践目标**：用源码里的实例化计数，亲手核对“每半 512、全阵列 1024、INT8 下 2048”这几个数字。
2. **操作步骤**：
   - 在 `NV_NVDLA_CMAC_core.v` 中搜索 `NV_NVDLA_CMAC_CORE_mac u_`，数出 cell 个数（应为 8：`u_mac_0`..`u_mac_7`）。
   - 在 `NV_NVDLA_CMAC_CORE_mac.v` 中搜索 `NV_NVDLA_CMAC_CORE_MAC_mul u_mul_`，数出每 cell 的乘法器个数（应为 64：`u_mul_0`..`u_mul_63`）。
   - 计算 8 × 64 = 512（每半），×2 半 = 1024。
   - 打开 `spec/defs/nv_full.spec` 确认 `MAC_ATOMIC_C_SIZE_64`（=64 个 mul）与 `MAC_ATOMIC_K_SIZE_32`（输出通道原子批量）。
3. **需要观察的现象**：乘法器实例化是高度规整的“复制粘贴”，每 20 行一个 `u_mul_N`，这正反映了硬件阵列的规整性。
4. **预期结果**：cell 数 = 8，mul/cell = 64，全阵列乘法器 = 1024（INT16/FP16）或 2048（INT8）。
5. 待本地验证：若你已按 u1-l4 跑通仿真，可在 VCS/Verdi 中用层次化信号名（如 `u_partition_ma.u_NV_NVDLA_cmac.u_core.u_mac_0.u_mul_0`）确认例化层次与计数。

#### 4.1.5 小练习与答案

- **练习 1**：为什么 NVDLA 要把 CMAC 拆成两半（cmac_a/b），而不是做一个更大的单阵列？
  - **答案**：两半共享同一份广播 dat、各自用不同 wt，能在同一拍并行算出两组输出通道，把输出通道吞吐翻倍；同时每个半阵列规模更小、走线更短、时序更易收敛。
- **练习 2**：`MAC_ATOMIC_C_SIZE_64` 这个 64 在 CMAC 源码里对应什么物理实体？
  - **答案**：对应一个 cell 内 64 个叶子乘法器 `u_mul_0..63`，即沿输入通道缩减维做点积的深度。

---

### 4.2 一个输出通道 cell：64 路乘法 + exp + nan + 压缩树

#### 4.2.1 概念说明

`NV_NVDLA_CMAC_CORE_mac` 是“一个输出通道、一个节拍”的计算单元。它的工作可以用一句话概括：**把 64 路 (dat × wt) 乘积，连同 FP16 的指数对齐与 NaN 处理，压成一个 176 位的冗余部分和，交给 CACC**。

一个 cell 包含三类协作单元：

- **64 个叶子乘法器（mul）**：每个负责一路乘法。op_a 是权重（wt）、op_b 是数据（dat）。
- **exp 单元（仅 FP16 用）**：算出本条带内 64 路的“最大指数”，并给出每路的移位量，让乘积在相加前先对齐。
- **nan 单元（仅 FP16 用）**：检测 dat/wt 是否出现 NaN，若有则把异常标志一路传播到输出。
- **CSA 压缩树**：用 `DW02_tree`（Synopsys DesignWare 的树形压缩器）把 64 路（INT8 下 128 路）乘积多级压缩，得到冗余的 `mac_out_data[175:0]`。

> 名词解释：**冗余表示（redundant form）** = 一个数用“和（sum）+ 进位（carry）”两路同时表达，不在每一级合并进位。它让多输入加法变得又快又规整，最后才在 CACC 里做一次完整加法。

#### 4.2.2 核心流程

一个 cell 一拍的数据流：

```
        dat[1023:0] (64×16b 或 128×8b)          wt[1023:0]
              │                                      │
              ▼                                      ▼
        ┌───────────── u_active（激活：nz/nan/exp）─────────────┐
        │                                                       │
        ▼                                                       ▼
  dat_actv_data[1023:0] / dat_actv_nz[127:0] / dat_actv_nan   wt_actv_data/nz/nan
        │                                                       │
        │  ┌──────────── u_exp（FP16：exp_max → exp_sft_*）──────┐
        │  │                                                      │
        ▼  ▼                                                      ▼
   u_mul_0 ───────── res_a/res_b (冗余) ─────────┐           （wt 也送 mul）
   u_mul_1 ───────── res_a/res_b ────────────────┤
        ...                                       ├── CSA tree（DW02_tree）
   u_mul_63 ──────── res_a/res_b ────────────────┘    （level0: 64→16/128→32, level1, level2）
                                                       │
                                       u_nan ── out_nan_mts / out_nan_pvld
                                                       │
                                                       ▼
                                            mac_out_data[175:0]  →  CACC
```

要点：

1. **op_a = 权重，op_b = 数据**。Booth 编码施加在权重上（见 4.3）。
2. **零值跳过**：每路有 `nz`（non-zero）标志，只有 dat 和 wt 都非零的那一路才真正产出有效部分积，权重稀疏时能省功耗/简化。
3. **CSA 树把乘积压成冗余形式**，不在 cell 内做最终加法；最终求和在 CACC。
4. INT8 模式下，每路 mul 产出“两个 8×8 积”，CSA 树把它们用 `2'b0` 间隔隔开，互不串扰地压在一起。

#### 4.2.3 源码精读

[NV_nvdla/cmac/NV_NVDLA_CMAC_CORE_mac.v:L49-L67](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cmac/NV_NVDLA_CMAC_CORE_mac.v#L49-L67) 给出了 cell 的关键端口：`dat_actv_data[1023:0]`（64 个 16 位数据）、`wt_actv_data[1023:0]`（64 个 16 位权重）、`dat_actv_nz[127:0]`（128 个字节级非零标志）、`mac_out_data[175:0]`（一路冗余部分和）。

64 个乘法器规整例化，最后一个为 `u_mul_63`：

[NV_nvdla/cmac/NV_NVDLA_CMAC_CORE_mac.v:L3901-L3917](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cmac/NV_NVDLA_CMAC_CORE_mac.v#L3901-L3917) `u_mul_63` 的例化：`op_a_dat` 接 `wt_actv_data63`（权重为 op_a）、`op_b_dat` 接 `dat_actv_data63`（数据为 op_b），`exp_sft` 由 exp 单元提供，输出 `res_a`/`res_b`/`res_tag`。这正是 4.3 要精读的叶子乘法器。

乘法器之后立刻进入压缩树，注释写得很清楚：

[NV_nvdla/cmac/NV_NVDLA_CMAC_CORE_mac.v:L3921-L3938](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cmac/NV_NVDLA_CMAC_CORE_mac.v#L3921-L3938) 注释 `MAC cell CSA tree level 0 // 64(128) -> 16(32)` 说明：INT16/FP16 下 64 路压成 16，INT8 下 128 路压成 32。代码里 `cfg_is_int8_d0` 选择把 `res_a` 的高/低 16 位“隔 2 位”地放进压缩树（INT8 双积不串扰），或整体符号扩展放进树里（INT16/FP16）：

```verilog
pp_in_l0_a_00 = cfg_is_int8_d0[0] ? {2'b0, res_a_00[31:16], 2'b0, res_a_00[15:0]} :
                {4'b0, res_a_00[31:0]};
```

这行是理解“同一套硬件如何同时服务 INT8 与 INT16”的关键。

exp 单元为 FP16 算指数对齐：

[NV_nvdla/cmac/NV_NVDLA_CMAC_CORE_MAC_exp.v:L96-L105](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cmac/NV_NVDLA_CMAC_CORE_MAC_exp.v#L96-L105) 输入 `dat_pre_exp[191:0]`（数据指数）、`wt_sd_exp[191:0]`（权重的 scale 指数），输出 `exp_max[3:0]`（本条带最大指数）与 64 路 `exp_sft_*`（每路移位量）。

[NV_nvdla/cmac/NV_NVDLA_CMAC_CORE_MAC_exp.v:L567-L576](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cmac/NV_NVDLA_CMAC_CORE_MAC_exp.v#L567-L576) 说明指数收集的节拍：条带起始（`dat_pre_stripe_st`）时用权重指数“置位”，条带结束（`dat_pre_stripe_end`）时“清零”，从而在一条带内维护一个共享的最大指数。

nan 单元用树形结构把 64 路 NaN 标志压成 11 位状态：

[NV_nvdla/cmac/NV_NVDLA_CMAC_CORE_MAC_nan.v:L28-L43](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cmac/NV_NVDLA_CMAC_CORE_MAC_nan.v#L28-L43) 输入 `dat_actv_nan[63:0]`、`wt_actv_nan[63:0]`（每路一个 NaN 标志），内部 `nan_flag_l0..l7` 是逐级折半的树形或 reduction（128→64→32→…→1），最终输出 `out_nan_mts[10:0]` 与 `out_nan_pvld`。只要有一路出现 NaN，整个 cell 的输出就被标记为 NaN，确保异常被忠实传播而不是被掩盖。

#### 4.2.4 代码实践：跟踪一个 partial sum 的诞生

1. **实践目标**：把 4.2.2 的流程图在源码里走一遍，理解一个部分和从 dat/wt 到 `mac_out_data` 的完整旅程。
2. **操作步骤**：
   - 在 `NV_NVDLA_CMAC_CORE_mac.v` 里定位 `u_mul_63`（约 L3901），记下它输出 `res_a_63`/`res_b_63`。
   - 顺 `res_a_63` 往下找到 CSA level0 的 `pp_in_l0_a_??`（约 L3930 起），看 64 路如何进树。
   - 继续往下找 `DW02_tree` 实例（level0/level1/level2），看冗余结果如何逐级压缩。
   - 在文件后段找到把压缩结果装配成 `mac_out_data[175:0]` 的 always 块。
   - 对照 `u_exp`（输入 exp，输出 `exp_sft_*` 喂给每个 mul）与 `u_nan`（输出 `out_nan_mts`）。
3. **需要观察的现象**：INT8 与 INT16 在 CSA level0 的输入选择不同（`cfg_is_int8_d0` 三元运算符），这是“一套硬件两种精度”的体现。
4. **预期结果**：能画出“64 个 mul → CSA level0(64→16) → level1 → level2 → mac_out_data[175:0]”的压缩链，并指出 exp 在最前、nan 在最旁路。
5. 待本地验证：压缩树最终位宽与字段分组的具体切分，建议在 Verdi 里观察 `mac_out_data` 各 bit 段随输入的变化（待本地验证）。

#### 4.2.5 小练习与答案

- **练习 1**：为什么 exp 单元只在 FP16 模式才需要？
  - **答案**：定点（INT8/INT16）没有指数，乘积直接对齐相加即可；只有浮点才需要先用“最大指数”把每路尾数积移位对齐，否则浮点相加会出错。
- **练习 2**：nan 单元为什么用“树形或”而不是简单地把 64 个标志相或？
  - **答案**：树形或（逐级折半）是规整的多级组合逻辑，时序更平衡、便于综合工具优化；同时它顺带产出一个有定位信息的 `out_nan_mts`，便于下游定位是哪一段出了 NaN。

---

### 4.3 叶子乘法器 MAC_mul：Booth 基 4 编码与三精度复用

#### 4.3.1 概念说明

`NV_NVDLA_CMAC_CORE_MAC_mul` 是整个阵列里最小的算术单元，也是理解 CMAC 算力的“原子”。它做的事情看似简单——算 op_a × op_b——但用了一套精巧的硬件让**同一个乘法器在 INT8/INT16/FP16 三种模式下都高效工作**。

设计要点：

- **Booth 基 4 编码**：对 op_a（权重）做 Booth 编码，每 3 位产生一个 ∈ {0, ±1, ±2} 的部分积，把部分积数目减半。
- **Booth 选择器（booth）**：根据编码从 op_b（数据）的 ±0/±1/±2 倍里挑出对应部分积，并给出符号补偿标志。
- **CSA 压缩树**：用 `DW02_tree` 把这些部分积压成冗余的 `res_a`/`res_b`（各 32 位），不做最终加法。
- **三模式复用**：
  - **INT8**：把 16 位的 op_a/op_b 当成“高字节 + 低字节”，并行算两个 8×8 积，分别压成冗余结果，再用 `cfg_is_int8` 选择“双积打包”输出。
  - **INT16**：算一个 16×16 有符号积。
  - **FP16**：算 16 位尾数积（同 INT16 的数据通路），再按 exp 单元给的 `exp_sft` 右移对齐，符号单独处理。

> 名词解释：**DW02_tree** 是 Synopsys DesignWare 提供的“通用部分积压缩树”IP，输入 N 个等宽数、输出 2 个（sum/carry）压缩结果，是工业乘法器的标准积木。

#### 4.3.2 核心流程

以 INT16 的一个 16×16 有符号乘法为例，Booth 基 4 的过程是：

1. **编码**：把 op_a（16 位）按 3 位一组重叠分窗，得到若干个 3 位 `code`。代码里分成低字节 `code_lo`（处理 op_a[7:0]）和高位 `code_hi`：
   - INT8 时 `code_hi` 来自 op_a[15:8]（第二组 8×8）；
   - INT16/FP16 时 `code_hi` 来自 op_a[15:7]（16×8 的后半）。

2. **选择**：8 个 `booth` 选择器各看一个 `code`，从 op_b 里挑出 ±0/±1/±2 倍，并给出 `out_inv`（是否取反，用于负数部分积的符号补偿）。低 4 个选择器用 `src_data_0`（op_b 低字节或整体），高 4 个用 `src_data_1`。

3. **压缩**：8 个部分积经两级 `DW02_tree`（l0n0/l0n1 → l1n0）压成冗余的 `pp_out_l1n0_0/_1`。

4. **输出选择**：按模式拼装 `res_a`/`res_b`：
   - INT8：`res_a = {pp_out_l0n1_0[15:0], pp_out_l0n0_0[15:0]}`（两个 8×8 积各占 16 位）。
   - INT16/FP16：`res_a = pp_out_l1n0_0`（一个 16×16 积）。
   - FP16：在 INT16 通路基础上，把结果按 `exp_sft` 右移（`pp_fp16_*_sft`）再做符号补偿（`pp_sign_tag`）。

5. **有效性门控**：`op_out_pvld` 由 `op_a_pvld & op_b_pvld & op_a_nz & op_b_nz` 决定；无效或零路用 `res_gate` 填零，避免脏数据进入压缩树。

#### 4.3.3 源码精读

端口明确 op_a=权重、op_b=数据，并带 2 位 `nz` 标志（INT8 时每字节一位）：

[NV_nvdla/cmac/NV_NVDLA_CMAC_CORE_MAC_mul.v:L34-L43](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cmac/NV_NVDLA_CMAC_CORE_MAC_mul.v#L34-L43) 输入 `op_a_dat[15:0]`/`op_a_nz[1:0]`（权重）、`op_b_dat[15:0]`/`op_b_nz[1:0]`（数据）、`exp_sft[3:0]`（FP16 移位量），输出冗余 `res_a`/`res_b`（各 32 位）与 `res_tag`（符号补偿）。

有效性门控逻辑，体现零值跳过：

[NV_nvdla/cmac/NV_NVDLA_CMAC_CORE_MAC_mul.v:L255-L263](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cmac/NV_NVDLA_CMAC_CORE_MAC_mul.v#L255-L263) 只有 dat 与 wt 都有效且都非零的那半（高/低字节）才置 `op_out_pvld`：

```verilog
op_out_pvld[1] = op_a_pvld & op_b_pvld & op_a_nz[1] & op_b_nz[1];
op_out_pvld[0] = op_a_pvld & op_b_pvld & op_a_nz[0] & op_b_nz[0];
```

Booth 基 4 编码与高低字节分流：

[NV_nvdla/cmac/NV_NVDLA_CMAC_CORE_MAC_mul.v:L322-L364](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cmac/NV_NVDLA_CMAC_CORE_MAC_mul.v#L322-L364) `code_lo`、`code_hi` 的生成，以及 `src_data_0/1` 按 `cfg_is_int8` 选择 op_b 的低字节或整体——这就是 INT8 双乘 vs INT16 单乘的分流点。

8 个 Booth 选择器例化：

[NV_nvdla/cmac/NV_NVDLA_CMAC_CORE_MAC_mul.v:L375-L452](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cmac/NV_NVDLA_CMAC_CORE_MAC_mul.v#L375-L452) 例化 `u_booth_0`..`u_booth_7`。每个选择器按 `code` 从 `src_data` 选 ±0/±1/±2 倍并给出 `out_inv`。

`DW02_tree` 压缩：

[NV_nvdla/cmac/NV_NVDLA_CMAC_CORE_MAC_mul.v:L518-L577](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cmac/NV_NVDLA_CMAC_CORE_MAC_mul.v#L518-L577) 两级 `DW02_tree`：l0n0/l0n1 各压 5 个部分积，l1n0 再合并。INT8 时 l1n0 输入填零（`128'b0`）以隔离两组 8×8 积。

输出按精度拼装：

[NV_nvdla/cmac/NV_NVDLA_CMAC_CORE_MAC_mul.v:L625-L649](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cmac/NV_NVDLA_CMAC_CORE_MAC_mul.v#L625-L649) 关键的三元选择：

```verilog
res_a_ori = cfg_is_fp16_d1[2] ? pp_fp16_0_sft :        // FP16：移位后的尾数积
            cfg_is_int8_d1   ? {pp_out_l0n1_0[15:0], pp_out_l0n0_0[15:0]} : // INT8：双 8×8 打包
                               pp_out_l1n0_0;           // INT16：16×16
```

Booth 选择器子单元本身的编码表（±0/±1/±2 与符号取反）：

[NV_nvdla/cmac/NV_NVDLA_CMAC_CORE_MAC_mul.v:L707-L796](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cmac/NV_NVDLA_CMAC_CORE_MAC_mul.v#L707-L796) `NV_NVDLA_CMAC_CORE_MAC_booth` 模块的 case 表：把 `{is_8bit, in_code}` 映射到 `out_data`（±0/±1/±2 倍的 src_data，含 16 位与 8 位两套），并输出 `out_inv`（负数部分积的取反补偿）。这是 Booth 算法的直接硬件实现，值得逐行对照教科书理解。

#### 4.3.4 代码实践：在 INT8/INT16 间切换观察同一乘法器

1. **实践目标**：直观体会“一个乘法器、两种精度”的复用，找出代码里决定分流的那几个信号。
2. **操作步骤**：
   - 在 `NV_NVDLA_CMAC_CORE_MAC_mul.v` 里找出所有出现 `cfg_is_int8_d1` 与 `cfg_is_fp16_d1` 的地方（编码、压缩、输出选择三处）。
   - 对照 `NV_NVDLA_CMAC_CORE_cfg.v` 确认这两个信号如何由 `proc_precision` 译出。
   - 思考：当 `cfg_is_int8_d1=1` 时，`pp_in_l1n0` 被填零、两个 8×8 积分别从 `pp_out_l0n0_0`/`pp_out_l0n1_0` 取出——这为什么不会让两个积互相污染？
3. **需要观察的现象**：分流点共有三处（编码 `code_hi`、压缩 `pp_in_l1n0`、输出 `res_a_ori`），全部由 `cfg_is_int8`/`cfg_is_fp16` 一致控制。
4. **预期结果**：能解释“INT8 两个积各占 16 位、中间隔零，所以不串扰”。
5. 待本地验证：可在仿真里构造一个 cell，分别给 INT8 与 INT16 配置，观察 `res_a` 的位段含义差异（待本地验证）。

#### 4.3.5 小练习与答案

- **练习 1**：为什么 Booth 编码施加在权重（op_a）而不是数据（op_b）上？
  - **答案**：两者数学上对称，但 NVDLA 的权重常是稀疏/共享的，把编码施加在权重侧更便于配合权重的压缩/掩码（wmb）与零值跳过逻辑；同时 op_b（数据）作为被选源，便于广播给多个输出通道。
- **练习 2**：`res_a`/`res_b` 为什么是“两个 32 位”而不是“一个最终结果”？
  - **答案**：它们是 CSA 树的冗余输出（sum 与 carry），不在这里做进位传播加法；最终合并由下游 cell 的压缩树和 CACC 完成，以缩短关键路径、提升频率。

---

### 4.4 CMAC 影偶寄存器与 op_en / producer / consumer 控制

#### 4.4.1 概念说明

CMAC 需要被 CPU 通过 CSB 配置：告诉它“这一层用什么精度（INT8/INT16/FP16）、是不是 Winograd、现在开始干活”。这些配置用**影偶（shadow）机制**管理，和 u2-l3 讲过的 CDMA 完全同构：

- **dual_reg（影偶组）**：装“逐层会变”的参数——`MISC_CFG`（精度 conv_mode、proc_precision）与 `OP_ENABLE`（启动开关 op_en）。例化为两份 `d0`/`d1` 轮换。
- **single_reg（单组）**：装“即时状态”——`POINTER`（producer/consumer 指针）与 `STATUS`（两组状态）。
- **reg.v（手写大脑）**：把上面两份 dual + 一份 single 串起来，实现 producer/consumer 切换、op_en 状态机、地址译码选组、写保护与 CSB 接口适配。

核心思想（承接 u2-l3）：**引擎跑第 N 层时，CPU 把第 N+1 层参数写进另一组；第 N 层 done 时，consumer 指针翻转，引擎无缝切到新组继续跑**，没有空泡。CMAC 的配置项很少（只有精度和模式），但影偶骨架完整，是理解“配置无缝切换”的最佳小例子。

此外，CMAC 用 **SLCG（Second-Level Clock Gating，二级时钟门控）** 省电：`op_en` 经几级寄存后广播成 `slcg_op_en[10:0]`，驱动 11 个操作域时钟门；当某半阵列空闲（op_en=0），它的时钟就被关掉。

#### 4.4.2 核心流程：一次配置切换的时间线

1. **CPU 写参数**：CPU 经 CSB 写 `MISC_CFG`（设 proc_precision）→ 写 `OP_ENABLE`（点火）。reg.v 按 `producer` 指针把写操作路由到 d0 或 d1。
2. **写保护**：被选中且正在运行（`op_en=1`）的那组禁止再写，避免覆盖运行中的配置。
3. **引擎运行**：reg.v 按 `consumer` 指针把对应组的 `conv_mode`/`proc_precision` 选出来送给 datapath；`op_en` 也按 consumer 选择后送给 core。
4. **完成与切换**：datapath 算完一层拉 `dp2reg_done` → reg.v 翻转 `consumer`、清掉本组 `op_en`、按新 consumer 选通输出 → 引擎无缝接跑下一层。
5. **状态可见**：CPU 随时读 `STATUS`，看到每组的“空闲/运行/待命”三态，据此决定能否安全写下一组。
6. **时钟门控**：`op_en` 的去assertion 经 3 级寄存产生 `slcg_op_en`，关闭空闲阵列的时钟。

#### 4.4.3 源码精读

先看自动生成的两份寄存器。dual_reg 装精度与启动：

[NV_nvdla/cmac/NV_NVDLA_CMAC_REG_dual.v:L66-L72](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cmac/NV_NVDLA_CMAC_REG_dual.v#L66-L72) 地址译码：`MISC_CFG_0` 在偏移 `0x700c`、`OP_ENABLE_0` 在 `0x7008`；`op_en_trigger` 在写 `OP_ENABLE` 时拉高一拍，作为点火脉冲。

[NV_nvdla/cmac/NV_NVDLA_CMAC_REG_dual.v:L103-L111](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cmac/NV_NVDLA_CMAC_REG_dual.v#L103-L111) 字段定义：`conv_mode` 在 bit0、`proc_precision` 在 bit[13:12]，复位默认 `proc_precision=2'b01`（INT16）。

single_reg 装指针与状态：

[NV_nvdla/cmac/NV_NVDLA_CMAC_REG_single.v:L65-L69](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cmac/NV_NVDLA_CMAC_REG_single.v#L65-L69) `POINTER_0` 在 `0x7004`（bit0=producer 可写、bit16=consumer 只读），`STATUS_0` 在 `0x7000`。

再看手写的 reg.v 大脑。影偶选组与写保护：

[NV_nvdla/cmac/NV_NVDLA_CMAC_reg.v:L336-L342](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cmac/NV_NVDLA_CMAC_reg.v#L336-L342) 地址 < `0x7008` 走 single 组；≥ `0x7008` 按 `producer` 选 d0/d1，且写使能再 `& ~op_en`——正在运行的组禁止写：

```verilog
assign select_d0 = (reg_offset[11:0] >= (32'h7008 & 32'hfff)) & (reg2dp_producer == 1'h0);
assign d0_reg_wr_en = reg_wr_en & select_d0 & ~reg2dp_d0_op_en;
```

consumer 在 done 时翻转：

[NV_nvdla/cmac/NV_NVDLA_CMAC_reg.v:L150-L156](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cmac/NV_NVDLA_CMAC_reg.v#L150-L156) `dp2reg_done` 来时 `consumer <= ~consumer`。

op_en 状态机（点火置位、done 且本组为 consumer 时清零）：

[NV_nvdla/cmac/NV_NVDLA_CMAC_reg.v:L248-L250](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cmac/NV_NVDLA_CMAC_reg.v#L248-L250) `reg2dp_d0_op_en_w`：空闲且收到点火脉冲 → 置为写入值；done 且本组是 consumer → 清零；否则保持。

按 consumer 选出送给 datapath 的配置：

[NV_nvdla/cmac/NV_NVDLA_CMAC_reg.v:L591-L605](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cmac/NV_NVDLA_CMAC_reg.v#L591-L605) `reg2dp_conv_mode`/`reg2dp_proc_precision` 按 `consumer` 从 d0/d1 里二选一输出——引擎始终消费 consumer 指向的那组。

状态三态译码（空闲/运行/待命）：

[NV_nvdla/cmac/NV_NVDLA_CMAC_reg.v:L218-L234](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cmac/NV_NVDLA_CMAC_reg.v#L218-L234) `dp2reg_status_0`：op_en=0 → 0(空闲)；op_en=1 且 consumer 指向另一组 → 1(运行)；op_en=1 且 consumer 指向本组 → 2(待命)。

SLCG 时钟门控控制信号：

[NV_nvdla/cmac/NV_NVDLA_CMAC_reg.v:L300-L328](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cmac/NV_NVDLA_CMAC_reg.v#L300-L328) `op_en` 先展成 11 位、再经 3 级寄存得到 `slcg_op_en[10:0]`，送给 core 的 11 个操作域时钟门——空闲时关钟省电。

精度译码（在 cfg 模块里，配合 reg 使用）：

[NV_nvdla/cmac/NV_NVDLA_CMAC_CORE_cfg.v:L83-L92](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cmac/NV_NVDLA_CMAC_CORE_cfg.v#L83-L92) `proc_precision==2'h0→int8`、`2'h1→int16`、`2'h2→fp16`；`conv_mode==1→winograd`。

[NV_nvdla/cmac/NV_NVDLA_CMAC_CORE_cfg.v:L72-L78](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/cmac/NV_NVDLA_CMAC_CORE_cfg.v#L72-L78) `cfg_reg_en = (~op_en_d1 | op_done_d1) & reg2dp_op_en`：只在操作跳变沿允许配置生效，保证新影偶组干净接管。

#### 4.4.4 代码实践：画一张影偶切换时序图

1. **实践目标**：把“写 d0 → 点火 → done 翻转 consumer → 写 d1”的影偶切换，对应到 reg.v 的具体信号。
2. **操作步骤**：
   - 假设初始 `producer=0, consumer=0`，两组都空闲。
   - CPU 写 `MISC_CFG`（设 INT8）+ `OP_ENABLE`：由 `select_d0`（producer=0）路由进 d0，`op_en_trigger` 把 `reg2dp_d0_op_en` 置 1。
   - 此时 `consumer=0`，所以 `reg2dp_proc_precision` 选 d0 的 INT8 送给 core，引擎开跑。
   - 引擎跑期间，CPU 把下一层参数（设 FP16）写进 d1：`select_d1`（producer 此时切到 1）路由，但若 d1 还没点火则可写。
   - 引擎 done：`dp2reg_done` → `consumer` 翻为 1 → `reg2dp_proc_precision` 改选 d1 的 FP16 → `reg2dp_d0_op_en` 因“done 且 consumer≠0”而保持/清零，引擎无缝接跑 FP16 层。
3. **需要观察的现象**：`consumer` 翻转那一拍，`reg2dp_proc_precision` 立刻切到另一组，但流水里正在算的 INT8 结果不会被破坏（因为它已经在 cell 的压缩树/CACC 里）。
4. **预期结果**：能画出 producer/consumer/op_en/done 四条线在两层交替时的波形，并标注 STATUS 三态变化。
5. 待本地验证：建议用一个 sanity trace（见 u1-l4）在 Verdi 里观察 `u_partition_ma.u_NV_NVDLA_cmac.u_reg.dp2reg_consumer` 与 `reg2dp_proc_precision` 的跳变（待本地验证）。

#### 4.4.5 小练习与答案

- **练习 1**：为什么写使能要 `& ~op_en`？
  - **答案**：防止 CPU 覆盖正在被引擎使用的配置。一个组一旦点火（op_en=1），就锁住直到它 done；CPU 只能写另一空闲组。
- **练习 2**：CMAC 的影偶组里只放了 `conv_mode` 和 `proc_precision` 两个会变的字段，为什么也要用 dual_reg？
  - **答案**：精度/模式必须在“上一层算完、下一层开算”的那个边界无缝切换，不能在跑的中途被改。影偶机制保证了切换瞬间的原子性，是整个 NVDLA 各引擎统一的配置更新范式。

## 5. 综合实践：给一个 INT8 卷积层“指认”CMAC 内部的数据通路

把本讲知识串起来，做一次端到端的“源码指认”：

**任务设定**：假设 CSC 已把一个 INT8 卷积层的 dat 与 wt 按 64 输入通道、某 8 个输出通道的节拍送进 cmac_a。请你在源码里指出：这批数据从进 CMAC 到产出 8 路部分和，依次经过哪些模块、哪些关键信号变化，最终如何交到 CACC 手里。

**建议步骤**：

1. **入口**：在 `NV_NVDLA_cmac.v` 找到 `sc2mac_dat_data0..127`（128 字节 INT8 数据）与 `sc2mac_wt_data0..127`（权重）端口，确认它们进 `u_core`。
2. **配置**：确认 `u_reg` 输出 `reg2dp_proc_precision=2'h0`（INT8），经 `u_cfg` 译出 `cfg_is_int8=1`。
3. **激活**：dat/wt 经 `u_rt_in`、`u_active` 产生 `dat_actv_*`/`wt_actv_*`（含 nz 非零标志）。INT8 下 exp/nan 通路不启用。
4. **8 个 cell 并行**：进入 `u_mac_0..7`，每个 cell 内 64 个 `u_mul_*` 以 `cfg_is_int8=1` 做“双 8×8”乘法。
5. **叶子乘法**：在 `NV_NVDLA_CMAC_CORE_MAC_mul` 里，确认 `res_a_ori` 走 `{pp_out_l0n1_0[15:0], pp_out_l0n0_0[15:0]}` 分支——两个 8×8 积各占 16 位。
6. **压缩**：每个 cell 的 CSA level0 因 `cfg_is_int8` 把 `res_a` 拆成 `{2'b0, [31:16], 2'b0, [15:0]}` 进树，64 路（INT8 下等效 128 个 8×8 积）压成 `mac_out_data[175:0]`。
7. **出口**：8 个 cell 的 `mac_out_data` 经 `u_rt_out` 成为 `mac2accu_data0..7`，连同 `mask`/`mode`/`pvld` 送给 CACC。
8. **对照算力**：回顾 4.1，确认本层动用了 8 cell × 64 mul × 2（INT8 双积）= 1024 个 INT8 MAC（单半），两半同时算另一组输出通道 → 全阵列 2048 INT8 MAC/拍。

**交付物**：一张标注了模块名、关键信号、`cfg_is_int8` 分支选择的 CMAC 内部框图（手画即可），并能用一句话向同伴解释“为什么 INT8 下算力是 INT16 的两倍”。

> 说明：本实践为源码阅读型实践，不要求跑仿真；若你已按 u1-l4 配好 VCS/Verdi，可在波形里用层次化路径核对上述信号（待本地验证）。

## 6. 本讲小结

- CMAC 是卷积主流水线第四级，只做“并行乘 + 压缩成部分和”，不做最终累加；最终累加在 CACC。
- 全阵列 = 两半（cmac_a/b，共享 dat、异权重）× 8 个输出通道 cell × 64 个叶子乘法器 = 1024 个乘法器；INT8 下每个乘法器算双 8×8，故 2048 INT8 MAC。规模由 `MAC_ATOMIC_C_SIZE_64`/`MAC_ATOMIC_K_SIZE_32` 锁定。
- 一个 cell = 64 个 `MAC_mul` + `exp`（FP16 指数对齐）+ `nan`（异常传播）+ CSA 压缩树，产出 176 位冗余部分和。
- 叶子乘法器用 **Booth 基 4 编码 + `DW02_tree` 压缩**，靠 `cfg_is_int8`/`cfg_is_fp16` 在编码、压缩、输出三处一致分流，实现 INT8/INT16/FP16 三精度复用同一套硬件。
- CMAC 用 dual_reg/single_reg 影偶寄存器 + 手写 reg.v 大脑实现 producer/consumer 无缝切换；写保护与 STATUS 三态保证配置安全。
- `op_en` 经 SLCG 广播成时钟门控使能，空闲时关阵列时钟省电。

## 7. 下一步学习建议

- **向下游**：进入 **u3-l6（CACC 累加器）**，看 CMAC 交出的 8 路 176 位冗余部分和如何在 CACC 里做进位传播加法、叠加偏置、按 INT8/INT16/FP16 装配交付 SDP。这是理解“为什么 CMAC 只压不算”的闭环。
- **向横切**：若对 Booth/CSA 这类算术原语感兴趣，可结合 **u6-l4（浮点运算单元）** 对比 NVDLA 在 vlibs 里另一套 `HLS_fp*` 浮点实现，体会两套 FPU 的差异。
- **向验证**：想看 CMAC 在真实激励下的波形，可回到 **u7-l1/u7-l2**，用 trace-player 跑一个卷积 trace，在 Verdi 里沿 `u_partition_ma.u_NV_NVDLA_cmac` 层次观察 `cfg_is_int8`、`mac2accu_data*` 等信号。
- **源码延伸阅读**：`NV_NVDLA_CMAC_CORE_active.v`（输入激活与稀疏处理）和 `NV_NVDLA_CMAC_CORE_slcg.v`（时钟门控单元）是本讲未深入的两个支撑模块，值得在掌握主干后单独细读。

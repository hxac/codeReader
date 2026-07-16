# 杂项 base：CAM/PRBS/复位生成/动态移位/首位解码/流控处理器

## 1. 本讲目标

本讲把 Open Logic base 区域里几个「不好归到前面某一类、但又非常实用」的实体集中讲清楚。学完本讲，读者应当能够：

- 理解 **内容寻址存储 CAM**（`olo_base_cam`）「用内容查地址」的反向查找机制，以及它如何被映射到普通块 RAM。
- 掌握 **PRBS 伪随机序列**（`olo_base_prbs`）的 LFSR 原理与状态注入，以及 **复位生成器**（`olo_base_reset_gen`）为什么是整个设计的「上电第一拍」。
- 学会使用 **动态桶形移位**（`olo_base_dyn_sft`）与 **首位解码器**（`olo_base_decode_firstbit`），理解它们为什么都要做成多级流水线。
- 学会用 **流控处理器**（`olo_base_flowctrl_handler`）给一个「天生不支持反压」的处理模块补上完整的 AXI-S 流控。

本讲是 u5 单元（时序、仲裁、CRC 与杂项 base）的收尾，承接 u2-l2 的两进程法与握手约定、u2-l1 的 base 包体系。

## 2. 前置知识

阅读本讲前，请确保已掌握以下概念（在 u1-l5、u2-l1、u2-l2、u2-l3 中建立）：

- **AXI-S 握手与反压**：`Valid`/`Ready` 配对，下游不收时上游必须停住（参见 u2-l2）。
- **两进程法（two-process method）**：用 record 收纳所有寄存器，组合进程 `p_comb` 算下一拍 `r_next`，时序进程 `p_seq` 只打拍与复位（参见 u2-l2）。
- **同步高有效复位**：复位写在进程末尾作为覆盖，只复位状态位（参见 u1-l5）。
- **块 RAM 的 RBW/WBR 行为**：读时写歧义——同地址同周期读写返回旧值还是新值（参见 u2-l3）。
- **`olo_base_ram_sdp` 与 `olo_base_fifo_sync`**：CAM 复用前者存匹配位、流控处理器复用后者做缓冲（参见 u2-l3、u2-l4）。
- **LFSR**（线性反馈移位寄存器）与 **GF(2) 多项式**：u5-l3 讲 CRC 时已引入，本讲 PRBS 再次用到。

> 术语提示：本讲出现 **CAM**（Content Addressable Memory，内容寻址存储）、**PRBS**（Pseudo-Random Binary Sequence，伪随机二进制序列）、**LFSR**、**barrel shifter**（桶形移位器）、**priority encoder**（优先级编码器，即首位解码）等术语，下文首次出现时会展开解释。

## 3. 本讲源码地图

| 文件 | 作用 |
| :--- | :--- |
| [src/base/vhdl/olo_base_cam.vhd](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_cam.vhd) | 内容寻址存储：用内容查地址，映射到块 RAM |
| [src/base/vhdl/olo_base_prbs.vhd](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_prbs.vhd) | 基于 LFSR 的伪随机序列发生器 |
| [src/base/vhdl/olo_base_reset_gen.vhd](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_reset_gen.vhd) | 上电/外部复位脉冲生成器，保证同步释放 |
| [src/base/vhdl/olo_base_dyn_sft.vhd](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_dyn_sft.vhd) | 动态（运行时可选移位量）桶形移位，多级流水线 |
| [src/base/vhdl/olo_base_decode_firstbit.vhd](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_decode_firstbit.vhd) | 首位解码器（求最低置位 bit 的索引），可流水线 |
| [src/base/vhdl/olo_base_flowctrl_handler.vhd](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_flowctrl_handler.vhd) | 为不支持反压的处理模块补全 AXI-S 流控 |
| [test/base/olo_base_cam/olo_base_cam_tb.vhd](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/test/base/olo_base_cam/olo_base_cam_tb.vhd) | CAM 的 VUnit 测试台（实践任务依据） |

辅助依赖：CAM 内部实例化 `olo_base_ram_sdp` 与 `olo_base_decode_firstbit`；流控处理器实例化 `olo_base_fifo_sync`；PRBS 的多项式常量来自 `olo_base_pkg_logic`。

## 4. 核心概念与源码讲解

### 4.1 内容寻址存储 CAM（olo_base_cam）

#### 4.1.1 概念说明

普通 RAM 是「给我地址，我给你数据」；**CAM（内容寻址存储）正好反过来**：「给我一段内容，我告诉你这段内容存在哪个地址里」。CAM 常用于需要极快查找的场景：CPU 的 TLB/缓存、网络路由表、模式匹配等。

`olo_base_cam` 的接口就体现了这种反向查找：

- **写侧 `Wr_...`**：把一个「地址↔内容」对写入 CAM（`Wr_Addr` + `Wr_Content`，`Wr_Write` 拉高表示写入）。
- **读侧 `Rd_...`**：给出 `Rd_Content`，CAM 返回这段内容存在哪些地址。
- **两类读响应**：`Match_Match` 是 one-hot 向量（每位对应一个地址，命中则置 1）；`Addr_Addr` 是二进制编码的「最低命中地址」，并配一个 `Addr_Found` 表示是否命中。

> 注意：用户必须保证「写入某地址时该地址是空的」，否则行为未定义。这正是 CAM 能用廉价 RAM 实现的前提——下文会解释。

#### 4.1.2 核心流程

CAM 最难的工程问题是：**怎么用普通块 RAM 实现「按内容寻址」？** 直接拿 18 位内容当地址，需要 \(2^{18}=262144 \) 深度的 RAM，完全不可行。`olo_base_cam` 采用 AMD 应用笔记 XAPP 1151 的思路，核心是把内容**切片后并行喂给多块 RAM，再把各块输出按位与**：

1. 把内容宽度 `ContentWidth_g` 按 `RamBlockDepth_g`（一块 RAM 的地址位宽，如 512 即 9 位地址）切成若干「并行块」`BlocksParallel_c`，每个块单独是一块 RAM。
2. 每块 RAM 的**地址**是内容的一段，**数据宽度**是 `Addresses_g` 位——即「每个 CAM 地址占 1 个 bit」。
3. 读时各块并行用各自的内容片段作地址读出向量，把所有块的向量**按位与**，结果就是 one-hot 命中向量：只有在所有片段上都同时命中同一个地址时，该位才为 1。

之所以「按位与」能成立，是因为每个 CAM 地址只存一份内容（用户保证写入时地址为空）。

并行块数的计算（[olo_base_cam.vhd:88-90](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_cam.vhd#L88-L90)）：

\[
\text{BlocksParallel} = \left\lceil \frac{\text{ContentWidth}}{\log_2(\text{RamBlockDepth})} \right\rceil
\]

其余流程：读占 1 拍、写占 2 拍、读写不能同拍进行；复位后 CAM 会自动清空自身（见 4.1.3）。读/写优先级与顺序由 `ReadPriority_g`、`StrictOrdering_g` 两个泛型控制——CAM 常用于「读多写少且对读延迟敏感」的场景，故默认读优先、非严格顺序以保证恒定读延迟。

#### 4.1.3 源码精读

**实体与泛型**：`Addresses_g`（CAM 容纳多少地址）、`ContentWidth_g`（内容位宽）是必填项；`RamBlockDepth_g` 决定每块 RAM 的深度，必须为 2 的幂（有断言检查）。

[olo_base_cam.vhd:33-79](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_cam.vhd#L33-L79) —— 实体声明，注意读侧给 `Rd_Content`、写侧给 `Wr_Addr`+`Wr_Content`+`Wr_Write`，并输出 one-hot（`Match_...`）与二进制（`Addr_...`）两套响应。

**两进程法 record**：把三级流水线（Stage 0 输入、Stage 1 RAM 读、Stage 2 匹配）的状态都收进 `TwoProcess_r`：

[olo_base_cam.vhd:93-115](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_cam.vhd#L93-L115) —— record 定义，含复位清空计数器 `RstClearCounter`/`RstClearDone`。

**按位与得到命中向量**：核心就在这两行——把第 0 块的读出作为初值，再循环与其余各块相与：

```vhdl
v.Match_2 := RamRead_1(0);
for i in 1 to BlocksParallel_c-1 loop
    v.Match_2 := v.Match_2 and RamRead_1(i);
end loop;
```

这就是「各内容片段并行查找后取交集」。见 [olo_base_cam.vhd:221-227](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_cam.vhd#L221-L227)。

**写掩码（写/清/全清）**：写时构造 `SetMask_v`（置 1 指定位）、清时构造 `ClearMask_v`（清指定位），写回 RAM：

[olo_base_cam.vhd:230-249](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_cam.vhd#L230-L249) —— 用 `fromUslv(to01(r.Addr_1))` 把二进制地址转成 one-hot 掩码位。

**RAM 阵列实例化**：用 `for generate` 按 `BlocksParallel_c` 并行例化 `olo_base_ram_sdp`，每块 RAM 的数据宽度是 `Addresses_g`（每个 CAM 地址 1 bit）：

[olo_base_cam.vhd:326-354](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_cam.vhd#L326-L354) —— 注意 `Wr_Data => RamWrite_1(i)`、`Rd_Data => RamRead_1(i)`，地址来自内容的对应片段。

**二进制地址输出**：one-hot 命中向量要转成「最低命中地址」的二进制编码，这正是 4.3 节的 `olo_base_decode_firstbit` 的用途——CAM 内部直接例化了它：

[olo_base_cam.vhd:357-376](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_cam.vhd#L357-L376) —— `In_Data => MatchInt`，输出 `Addr_Addr`/`Addr_Found`。

**复位后自动清空**：RAM 内容不会被 `Rst` 清掉（这是 RAM 的通性，见 u2-l3），而 CAM 内容很宽，没法靠遍历所有可能内容来清空。于是 CAM 在复位后用一个计数器遍历每块 RAM 的所有地址、写 0，这要花 `RamBlockDepth_g` 拍，期间 `Rd/Wr_Ready` 保持低：

[olo_base_cam.vhd:261-289](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_cam.vhd#L261-L289) —— `RstClearCounter` 计满 `2**BlockAddrBits_c-1` 后置 `RstClearDone`，清空期间把写数据强制为全 0。

#### 4.1.4 代码实践

仓库已为 CAM 提供完整的 VUnit 测试台（`olo_base_cam_tb.vhd`），本实践用「运行现成测试 + 阅读激励」的方式理解命中/未命中行为。

1. **实践目标**：验证 CAM 写入若干键后，查询命中返回正确地址、查询未命中返回 `Addr_Found='0'`。
2. **操作步骤**：
   - 进入 `sim/` 目录，按 u1-l4 介绍的方式运行 CAM 测试：`python run.py --ghdl -- "olo_base_cam_tb"`（具体仿真器开关以本地环境为准）。
   - 打开 [olo_base_cam_tb.vhd:27-42](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/test/base/olo_base_cam/olo_base_cam_tb.vhd#L27-L42)，观察测试台如何用 generic（`Addresses_g`、`ContentWidth_g`、`ReadPriority_g` 等）参数化不同配置。
   - 阅读 `pushConfigIn` 过程（[olo_base_cam_tb.vhd:79-90](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/test/base/olo_base_cam/olo_base_cam_tb.vhd#L79-L90)），理解它如何把 `Content/Addr/Write/Clear/ClearAll` 打包成一拍写命令。
3. **需要观察的现象**：复位释放后，CAM 先花若干拍清空（`Rd_Ready`/`Wr_Ready` 为低）；之后写入键再查询，命中时 `Match_Match` 对应位拉高、`Addr_Addr` 给出地址；查询不存在的内容时 `Addr_Found='0'`。
4. **预期结果**：所有 CAM 测试用例通过（VUnit 报告 `pass`）。
5. 若本地未配置仿真器，结果标注「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 CAM 能把内容切片后「按位与」各块输出，就能得到正确命中向量？需要什么前提？
> **答案**：因为每个 CAM 地址只存一份唯一内容（用户保证写入时地址为空）。把内容切成 N 段分别作地址查 N 块 RAM，每块给出「该片段命中的地址集合」；只有真正存有该完整内容的地址会同时在所有片段上命中，故交集（按位与）即为最终命中。前提是「同一地址不存多份内容」。

**练习 2**：`ReadPriority_g=true` 且 `StrictOrdering_g=false` 时，写后立即读会读到新值还是旧值（假设 `RamBehavior_g="RBW"`）？
> **答案**：读到旧值。`StrictOrdering_g=false` 允许读紧跟写、不插入等待拍，而 RBW（读前写）RAM 在这种情况下返回的是写入前的旧内容；这也是默认配置以换取恒定读延迟的代价。

---

### 4.2 PRBS 伪随机序列与复位生成（olo_base_prbs / olo_base_reset_gen）

#### 4.2.1 概念说明

**PRBS（伪随机二进制序列）** 是看似随机、实则由确定性电路产生的比特流，用于数据通路自检（BIST）、加扰/解扰、测试图案等。Open Logic 用 **LFSR（线性反馈移位寄存器）** 实现：一个移位寄存器，每拍把若干个「抽头」异或后作为新的最低位反馈回来。抽头位置由多项式决定。

`olo_base_prbs` 把这套机制封装成标准 AXI-S 输出（`Out_Valid` 恒为 1），并提供状态注入接口（`State_Set`/`State_New`）用于运行时重置序列。

**复位生成器 `olo_base_reset_gen`** 解决另一个根本问题：FPGA 上电后，谁来产生「第一个」复位脉冲？它在上电（或外部 `RstIn` 触发）后产生一个不短于 `RstPulseCycles_g` 拍的高有效复位，并保证复位**同步释放**（避免释放瞬间落在不同寄存器的不同拍上导致亚稳态）。它通常是一个设计中所有 `Rst` 的最终来源。

#### 4.2.2 核心流程

**PRBS（LFSR）更新**：设多项式为 \(P\)（位向量，置 1 处代表用到 \(x^n\) 项），LFSR 状态为 \(S\)，则每拍新反馈位为：

\[
b_{\text{new}} = \bigoplus_{i} \big( S_i \wedge P_i \big)
\]

即「状态与多项式逐位与，再全部异或」。然后把寄存器移位、把 \(b_{\text{new}}\) 塞进最低位。`BitsPerSymbol_g>1` 时一拍移多位、输出多位。

> 关键约束：LFSR 状态**永不能为全 0**——全 0 时反馈位恒为 0，序列会永远停在 0。故 `Seed_g` 必须非零（有断言）。

**复位生成器**采用经典的「异步置位、同步释放」复位同步器：

1. `RstIn`（或上电时的 FF 初值）异步地把同步链全部置 1（复位有效）。
2. 时钟到来后，一串 0 沿着同步链逐拍移入（同步释放）。
3. 若 `RstPulseCycles_g>3`，再用一个计数器把复位脉冲展宽到要求的拍数。

#### 4.2.3 源码精读

**PRBS 实体**：多项式与种子以 `std_logic_vector` 给出，`BitsPerSymbol_g` 控制输出宽度。

[olo_base_prbs.vhd:35-54](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_prbs.vhd#L35-L54) —— 注意 `Out_Ready` 有默认值 `'1'`、`State_Set` 默认 `'0'`，不用的接口可悬空。

**LFSR 更新函数**：循环 `bits` 次，每次「与多项式 → 异或归约 → 移位」。`xor_reduce` 来自 `ieee.std_logic_misc`：

```vhdl
LfsrMasked_v := Lfsr_v(Polynomial_g'length-1 downto 0) and Polynomial_g;
NextBit_v    := xor_reduce(LfsrMasked_v);
Lfsr_v       := Lfsr_v(Lfsr_v'high-1 downto 0) & NextBit_v;
```

见 [olo_base_prbs.vhd:70-87](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_prbs.vhd#L70-L87)。

**时序进程**：`Out_Ready='1'` 时才推进 LFSR（支持反压暂停）；`State_Set` 优先载入新状态；复位载入 `Seed_g`：

[olo_base_prbs.vhd:123-151](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_prbs.vhd#L123-L151) —— 三段优先级：正常推进 < 状态载入 < 复位。

**多项式常量从哪来**：`olo_base_pkg_logic` 预定义了 `Polynomial_Prbs2_c` … `Polynomial_Prbs32_c`（[olo_base_pkg_logic.vhd:79-109](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_pkg_logic.vhd#L79-L109)），直接当 `Polynomial_g` 传入即可，例如 7 位 PRBS 用 `Polynomial_Prbs7_c => "1100000"`（\(x^7+x^6+1\)）。

**复位生成器实体**：

[olo_base_reset_gen.vhd:34-46](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_reset_gen.vhd#L34-L46) —— `RstPulseCycles_g` 最小 3、`RstIn` 可选（靠 FF 初值实现纯上电复位）、`RstOut` 恒高有效。

**异步置位、同步释放**：`RstIn` 命中极性时异步置链为全 1，否则时钟沿把 0 移入链中：

[olo_base_reset_gen.vhd:79-86](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_reset_gen.vhd#L79-L86) —— 注意这是 `process (RstIn, Clk)`，对 `RstIn` 敏感，体现「异步置位」。

**同步链与综合属性**：同步释放链 `DsSync` 必须被综合成真实的、互相级联的触发器，不能被优化、合并或抽成移位寄存器 SRL。因此挂了一组跨厂商属性：

[olo_base_reset_gen.vhd:63-74](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_reset_gen.vhd#L63-L74) —— `shreg_extract=suppress`、`async_reg`、`dont_merge`/`preserve` 等，含义与 u2-l2、u4-l1 一致。

**脉冲展宽**：`RstPulseCycles_g>3` 时启用计数器；`<=3` 时直接输出同步链结果：

[olo_base_reset_gen.vhd:108-144](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_reset_gen.vhd#L108-L144) —— 异步输出模式下 `RstOut <= RstPulse or RstSync`（保证即使无时钟也能保持复位）。

#### 4.2.4 代码实践

1. **实践目标**：实例化一个 7 位 PRBS，观察其输出序列并验证「载入全 0 状态会被断言拦截」。
2. **操作步骤**：
   - 仿照 `test/base/olo_base_prbs4_tb.vhd` 写一个最小顶层，例化：
     ```vhdl
     -- 示例代码：仅演示实例化，非仓库既有文件
     i_prbs : entity work.olo_base_prbs
         generic map (
             Polynomial_g    => Polynomial_Prbs7_c,  -- 来自 olo_base_pkg_logic
             Seed_g          => "0000001",
             BitsPerSymbol_g => 1)
         port map (
             Clk       => Clk,
             Rst       => Rst,
             Out_Data  => PrbsBit,
             Out_Valid => open);
     ```
   - 仿真数拍，记录 `Out_Data` 序列。
3. **需要观察的现象**：复位后从种子开始按 LFSR 规律演化；若试图把 `State_New` 设为全 0 并拉高 `State_Set`，仿真应在断言处报错（`Seed_g MUST NOT be zero` 类检查对运行时状态虽不强制，但全 0 会导致输出此后恒 0）。
4. **预期结果**：得到一段确定性比特序列；同一种子每次仿真结果完全一致（伪随机特性）。运行时设全 0 后序列卡死在 0。
5. 若本地无仿真器，标注「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：为什么复位生成器对同步释放链 `DsSync` 要加 `async_reg`、`shreg_extract=suppress` 等属性？
> **答案**：同步释放链的作用是让复位释放信号经过若干级触发器、确定性地在某个时钟拍生效，从而消除亚稳态。若工具把这些触发器优化合并、或抽成移位寄存器（SRL）、或放到不同位置，就会破坏「级联独立 FF」的结构，失去去亚稳态能力。这些属性强制它们保持为独立的、位置相邻的真实 FF。

**练习 2**：PRBS 的 `Seed_g` 为什么禁止为 0？
> **答案**：LFSR 反馈位是「状态与多项式逐位与再异或」；若状态全 0，反馈位恒为 0，移位后状态仍全 0，序列将永远停在 0 无法跳出，故种子必须非零。

---

### 4.3 动态桶形移位与首位解码（olo_base_dyn_sft / olo_base_decode_firstbit）

#### 4.3.1 概念说明

**动态桶形移位（barrel shifter）** `olo_base_dyn_sft`：移位量不是编译期固定的，而是**每个数据样本在运行时单独指定**（`In_Shift`）。它用于数据对齐、浮点对阶、可变字段的提取/拼接等。

**首位解码器（priority encoder / first-bit decoder）** `olo_base_decode_firstbit`：给定一个向量，找出「最低（或某个方向）置 1 bit 的索引」。这在仲裁（找最高优先级请求者）、CAM 把 one-hot 转二进制地址等场景必不可少。

两者有一个共同的工程难点：**对宽输入做这类「大树状归约」运算，单拍组合逻辑路径太长，时序跑不上去**。所以 Open Logic 都把它们做成「多级流水线」——把一个大运算拆成若干级小运算，中间插寄存器。

#### 4.3.2 核心流程

**桶形移位分级**：把移位量按 `SelBitsPerStage_g` 位一级地拆。每级只根据自己那几位选择「不移」或「移 \(2^{\text{基}}\) 位」。例如 `MaxShift_g=15`（移位量 4 位）、`SelBitsPerStage_g=2`，则分 2 级，移 7 = 第 1 级移 3 + 第 2 级移 4。级数为：

\[
\text{Stages} = \left\lceil \frac{\lceil \log_2(\text{MaxShift}+1) \rceil}{\text{SelBitsPerStage}} \right\rceil
\]

右移实现技巧：把数据放进一个 2 倍宽的临时向量，按移位量放在高位区域，再取高半部分作为输出（相当于零填充右移）；`SignExtend_g=true` 时用符号位填充而非 0。

**首位解码分级**：第一级把输入分成若干小段，**并行**地找每段内的最低置位 bit 及其段内索引；后续每级从上一级若干个「候选」中选出索引最小的那个，并把更多地址位补全。级数由 `PlRegs_g` 控制（`PlRegs_g=0` 表示纯单拍组合）。

#### 4.3.3 源码精读

**dyn_sft 实体**：`Direction_g`（"LEFT"/"RIGHT"）、`MaxShift_g`、`SignExtend_g`、`SelBitsPerStage_g` 是关键旋钮：

[olo_base_dyn_sft.vhd:34-51](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_dyn_sft.vhd#L34-L51) —— 注意断言要求 `MaxShift_g <= Width_g`、`Direction_g` 必须是 LEFT/RIGHT。

**级数与每级步长**：

[olo_base_dyn_sft.vhd:58-61](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_dyn_sft.vhd#L58-L61) —— `Stages_c = ceil(In_Shift'length / SelBitsPerStage_g)`。

**右移实现**（核心几行）：用 `TempData_v`（2 倍宽）承载移位后的数据，取高半部分：

```vhdl
if SignExtend_g then
    TempData_v := (others => r.Data(stg)(Width_g - 1));  -- 符号位填充
else
    TempData_v := (others => '0');
end if;
TempData_v(2*Width_g-1-Select_v*StepSize_v downto Width_g-Select_v*StepSize_v) := r.Data(stg);
v.Data(stg+1) := TempData_v(2*Width_g-1 downto Width_g);
```

见 [olo_base_dyn_sft.vhd:108-119](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_dyn_sft.vhd#L108-L119)。每级结束后用 `shiftRight(...)` 把剩余的移位量下移到下一级（[olo_base_dyn_sft.vhd:126](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_dyn_sft.vhd#L126)），该函数来自 `olo_base_pkg_logic`（[olo_base_pkg_logic.vhd:150-164](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_pkg_logic.vhd#L150-L164)）。

**decode_firstbit 实体**：

[olo_base_decode_firstbit.vhd:34-55](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_decode_firstbit.vhd#L34-L55) —— 输出 `Out_FirstBit`（索引）+ `Out_Found`（是否有任何 bit 置 1）；断言要求 `PlRegs_g < log2(InWidth_g)/2`。

**第一级并行分段查找**：把输入切成多段，每段独立找最低置位 bit，找到就 `exit`（保证取最低）：

[olo_base_decode_firstbit.vhd:160-177](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_decode_firstbit.vhd#L160-L177) —— 嵌套 for 循环，外层遍历并行实例、内层遍历段内 bit。

**后续级归约**：从上一级的多个候选中选 `Found=1` 的最低者，并补上更高位的索引位：

[olo_base_decode_firstbit.vhd:180-198](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_decode_firstbit.vhd#L180-L198) —— 把段号 `bit` 写入地址的高位字段。

**未找到时输出 0**：便于测试：

[olo_base_decode_firstbit.vhd:208-213](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_decode_firstbit.vhd#L208-L213) —— `FoundOut='0'` 时 `FirstBit` 强制为全 0。

#### 4.3.4 代码实践

1. **实践目标**：理解 `SelBitsPerStage_g` 如何在「时序」与「级数（延迟）」之间取舍。
2. **操作步骤**：
   - 阅读文档图（`doc/base/olo_base_dyn_sft.md`）中 `MaxShift_g=15, SelBitsPerStage_g=2` 的两级示例。
   - 推算：同一 `MaxShift_g=15`（移位量 4 位）下，`SelBitsPerStage_g=4` 时几级？`SelBitsPerStage_g=1` 时几级？
3. **需要观察的现象**：`SelBitsPerStage_g` 越小，级数越多、延迟越大，但每级逻辑越浅、时序越好；越大则级数少、延迟小，但单级 MUX 更宽。
4. **预期结果**：`SelBitsPerStage_g=4` → 1 级（1 拍延迟）；`=2` → 2 级；`=1` → 4 级。可对照 [olo_base_dyn_sft.vhd:60](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_dyn_sft.vhd#L60) 的 `Stages_c` 公式手算验证。
5. 若在仿真中实测延迟，标注「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：为什么桶形移位不直接写 `Out_Data <= std_logic_vector(shift_right(...))` 一行搞定，而要拆成多级？
> **答案**：一行写法是单拍纯组合的大 MUX 树，对宽数据 + 大移位量，组合路径极长，会成为关键路径、压低时钟频率。拆成多级、每级只移 \(2^{k}\) 位并在中间插寄存器，把长路径切成若干短路径，时序大幅改善，代价是增加几拍延迟。

**练习 2**：`olo_base_decode_firstbit` 的第一级为什么要「并行分多段、每段独立找最低位」，而不是从头到尾一个循环找？
> **答案**：并行分段让每段在「自己的局部范围」内同时完成查找，后续再归约选最小者。这样把一条很长的串行优先级链拆成宽而浅的并行结构，便于流水线化、提升时序；串行单循环则是一条长组合链。

---

### 4.4 流控处理器（olo_base_flowctrl_handler）

#### 4.4.1 概念说明

很多处理模块（比如一个纯组合的乘法器、或一个第三方 IP）**只认 `Valid`、不认 `Ready`**——也就是说它们没有反压能力：上游来数据就处理、处理完就吐出去，不管下游收不收。一旦下游拉低 `Out_Ready`，吐出的样本就会丢失。

`olo_base_flowctrl_handler` 就是一个**流控适配器**：包在这种模块外面，给它「补上」完整的 AXI-S 流控（含 Ready/反压）。它内部放一个小 FIFO 缓冲处理模块的输出，并通过提前压低 `In_Ready` 来保证「处理模块吐出的样本永远有地方放」。

#### 4.4.2 核心流程

设处理模块最多会在 `ToProc_Valid` 拉低后还继续吐 `SamplesToAbsorb_g` 个样本（例如一个 N 级流水线，停掉输入后还会涌出 N 个结果）。策略是：

1. **输入侧**：`In_Ready <= Fifo_HalfEmpty`——只要 FIFO「过半空」就继续收，否则停。
2. **喂给处理模块**：`ToProc_Valid <= In_Valid and Fifo_HalfEmpty`——只有 FIFO 能保证吸收结果时，才把数据真正喂进处理模块。
3. **输出侧**：处理模块的输出 `FromProc_...` 写入 FIFO，FIFO 的输出就是对外接口 `Out_...`，由下游 `Out_Ready` 正常消费。

FIFO 深度取 \(2 \times (\text{SamplesToAbsorb}+2)\)，留足余量保证「全吞吐、不丢数据」。其本质是：**用 FIFO 的「半空」标志提前一个处理延迟去关断输入**，使处理模块涌出的尾部样本刚好被 FIFO 接住。

#### 4.4.3 源码精读

**实体**：两侧位宽可不同（`InWidth_g`/`OutWidth_g`），`SamplesToAbsorb_g` 是核心参数：

[olo_base_flowctrl_handler.vhd:31-58](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_flowctrl_handler.vhd#L31-L58) —— 对外是标准 `In_...`/`Out_...`，对内是 `ToProc_...`（给处理模块的输入）和 `FromProc_...`（处理模块的输出）。

**输入与喂入逻辑**：核心就三行赋值——`In_Ready` 跟 FIFO 半空、`ToProc_Valid` 是「输入有效 且 FIFO 半空」：

[olo_base_flowctrl_handler.vhd:74-77](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_flowctrl_handler.vhd#L74-L77) —— `ToProc_Data <= In_Data;`（数据直通，只控有效）。

**FIFO 例化**：用 `olo_base_fifo_sync`，关键在把「几乎空」阈值设为深度的一半（`AlmEmptyLevel_g => FifoDepth_c/2`），并把 `AlmEmpty` 当作 `Fifo_HalfEmpty` 用：

[olo_base_flowctrl_handler.vhd:80-100](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_flowctrl_handler.vhd#L80-L100) —— 回顾 u2-l4：`AlmEmpty` 基于 `RdLevel` 用 `<=` 判断，是「几乎空」提前预警，正好充当这里需要的「半空」信号。

**深度公式**：

[olo_base_flowctrl_handler.vhd:67](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_flowctrl_handler.vhd#L67) —— `FifoDepth_c := 2*(SamplesToAbsorb_g+2)`。

**仿真期断言**：确保处理模块在 `FromProc_Valid='1'` 时 FIFO 一定没满（即流控设计正确、永不丢数据），仅在仿真期生效：

[olo_base_flowctrl_handler.vhd:103-113](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_flowctrl_handler.vhd#L103-L113) —— 用 `-- synthesis translate_off/on` 包裹，综合时被忽略。

#### 4.4.4 代码实践

1. **实践目标**：把一个「不支持反压」的处理（例如一个固定 2 拍延迟的乘法）包进 `olo_base_flowctrl_handler`，验证下游反压时不丢数据。
2. **操作步骤**：
   - 设处理模块为 2 级流水线（无 Ready），故 `SamplesToAbsorb_g => 2`（即停输入后还会涌出 2 个结果）。
   - 实例化 `olo_base_flowctrl_handler`，把 `ToProc_...` 接到处理模块输入、`FromProc_...` 接到处理模块输出；外部 `In_...`/`Out_...` 暴露给测试台。
   - 仿真时让下游 `Out_Ready` 在数据流中途拉低若干拍。
3. **需要观察的现象**：下游拉低时，`In_Ready` 随之拉低（FIFO 不再半空），处理模块的尾部输出落入 FIFO 而不丢失；下游恢复后 FIFO 把缓冲的数据依次吐出。
4. **预期结果**：输入序列与输出序列逐样本一致（顺序不变、无丢失）；仿真期断言不报错。
5. 若本地未搭建该仿真，标注「待本地验证」。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `ToProc_Valid` 要写成 `In_Valid and Fifo_HalfEmpty`，而不是直接 `In_Valid`？
> **答案**：处理模块不认反压，一旦喂入就会在若干拍后吐出结果。若 FIFO 已经较满（不「半空」）还继续喂，吐出的结果可能无处安放而丢失。用 `and Fifo_HalfEmpty` 保证「只在 FIFO 确定能吸收未来结果时」才喂入，从源头杜绝溢出。

**练习 2**：`SamplesToAbsorb_g` 取得偏大会有什么后果？偏小呢？
> **答案**：偏大只是 FIFO 比必要的更深、浪费一些 RAM 资源，但功能正确（文档明确说明「宁大勿小」）；偏小则 FIFO 吸收不下处理模块涌出的尾部样本，会丢数据、触发仿真断言。

---

## 5. 综合实践

把本讲两条主线串起来：**用 `olo_base_reset_gen` 产生上电复位，驱动一个由 `olo_base_cam` 实现的 4 项查找表**。

任务：

1. **顶层时钟与复位**：例化 `olo_base_reset_gen`（`RstPulseCycles_g => 10`），用它的 `RstOut` 作为全设计的 `Rst`（模拟「上电第一拍」）。
2. **4 项 CAM**：例化 `olo_base_cam`，`Addresses_g => 4`、`ContentWidth_g => 16`、`RamBlockDepth_g => 512`，复位来自上一步。
3. **写入查找表**：复位释放后（注意 `ClearAfterReset_g=true` 会先清空 512 拍，期间 `Wr_Ready` 为低），写入 3 个键值对，例如地址 0↔`0x1000`、地址 1↔`0x2000`、地址 2↔`0x3000`（确保每个地址写入前为空）。
4. **查询**：
   - 查 `0x2000`：期望 `Match_Match = "0010"`、`Addr_Addr = 1`、`Addr_Found = '1'`。
   - 查 `0x9999`（未写入）：期望 `Addr_Found = '0'`、`Match_Match = "0000"`。
5. **观察重点**：
   - 复位后 CAM 的 `Wr_Ready`/`Rd_Ready` 先保持低若干拍（清空 RAM），再变高。
   - 读响应延迟：读占 1 拍、加上 `RegisterInput_g`/`RegisterMatch_g`/首位解码的寄存级数。
6. **验证方式**：可参照仓库 [olo_base_cam_tb.vhd](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/test/base/olo_base_cam/olo_base_cam_tb.vhd) 的写法（它用 VUnit 的 AXI-S master/slave 验证组件驱动读/写/响应），把上述键值对与查询作为测试用例加入。若本地无仿真器，标注「待本地验证」。

> 这个综合实践同时检验了：复位生成器的「同步释放 + 脉冲展宽」、CAM 的「清空-写入-查询」时序、以及 one-hot↔二进制地址输出的对应关系。

## 6. 本讲小结

- **CAM（`olo_base_cam`）** 把「按内容查地址」映射到块 RAM：内容切片并行喂多块 RAM、输出按位与得到 one-hot 命中向量，前提是每个地址只存一份内容；复位后自动遍历清空 RAM。
- **PRBS（`olo_base_prbs`）** 用 LFSR 产生确定性伪随机序列，多项式决定反馈抽头、种子禁止为 0；`olo_base_pkg_logic` 预置了 PRBS2…32 的多项式常量。
- **复位生成器（`olo_base_reset_gen`）** 是设计的「上电第一拍」，异步置位、同步释放、可展宽脉冲，靠一组综合属性保持同步链为独立 FF。
- **动态桶形移位（`olo_base_dyn_sft`）** 把运行时可变移位拆成多级、每级移 \(2^{k}\) 位，以时序换延迟；`SelBitsPerStage_g` 调节平衡点。
- **首位解码器（`olo_base_decode_firstbit`）** 求最低置位 bit 索引，第一级并行分段、后续级归约，可流水线化以支持宽输入；CAM 用它把 one-hot 转二进制地址。
- **流控处理器（`olo_base_flowctrl_handler`）** 用一个小 FIFO + 「半空」提前关断输入，给不支持反压的处理模块补全 AXI-S 流控，深度公式 \(2\times(\text{SamplesToAbsorb}+2)\)。

## 7. 下一步学习建议

- 本讲讲完了 base 区域的「杂项」实体，base 区域至此基本覆盖完毕。下一步可进入 **第 6 单元 AXI 区域**，从 `olo_axi_pl_stage`（[u6-l1](u6-l1-axi-pipeline-stage.md)）开始，把 u2-l2 学到的握手与流水线推广到完整 AXI4 接口。
- 若对 **跨时钟域** 的复位穿越（本讲 `reset_gen` 产生的复位如何安全地送到另一时钟域）感兴趣，可回顾 [u4-l1](u4-l1-clock-crossing-principles.md) 的复位穿越约定。
- 想进一步理解 CAM 的来源，可阅读 AMD 应用笔记 [XAPP 1151](https://docs.amd.com/v/u/en-US/xapp1151_Param_CAM)（文档 `doc/base/olo_base_cam.md` 中有链接）。
- 想看这些实体如何在真实测试中被驱动，可继续阅读 [u10-l1（VUnit 测试台与验证组件）](u10-l1-vunit-tb-and-vcs.md)，理解 CAM/PRBS 测试台里用到的 AXI-S master/slave 验证组件（VC）。

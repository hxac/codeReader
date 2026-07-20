# 异步 FIFO：async_fifo 与格雷码指针

## 1. 本讲目标

本讲在 u4-l1（同步 FIFO）的基础上，进入 psi_common 的「双时钟域 FIFO」组件 `psi_common_async_fifo`。读完本讲你应当能够：

- 说清楚为什么跨时钟域传递「读写指针」必须用**格雷码**，而满空判别却是在**二进制**下完成的；
- 解释为什么指针比 RAM 地址多一位（\(n+1\) 位），以及这一位如何让「满」和「空」可区分；
- 读懂本组件采用的**两进程 record 设计法**（`ri/ri_next`、`ro/ro_next` 双记录），并指出哪些字段分别由 `in_clk_i` / `out_clk_i` 驱动；
- 把异步 FIFO 与同步 FIFO（`sync_fifo`）逐点对比，知道在什么场景下选哪一个。

---

## 2. 前置知识

本讲假设你已经掌握以下内容（前序讲义已建立）：

- **AXI-S 握手（u1-l4）**：传输只在 `vld=1` 且 `rdy=1` 的同一拍发生；`in_rdy_o` 在 FIFO 里语义就是「not full」，`out_vld_o` 语义就是「not empty」。
- **格雷码（u2-l2）**：`binary_to_gray` 是一次移位异或（廉价），`gray_to_binary` 是一条累积异或链（较深）；相邻格雷码值**只有 1 比特不同**，这是它用于 CDC 的根本原因。
- **简单双口 RAM（u3-l1）**：`psi_common_sdp_ram` 用 `is_async_g` 在「同步读」与「独立异步读时钟」间切换；异步模式下读写各有自己的时钟进程，共享同一个 `shared variable` 存储体。
- **真双口 RAM（u3-l2）**：`tdp_ram` 的 A/B 两端口完全对称、可双向读写；u3-l2 已指出一个常见误解——**异步 FIFO 实际上用的是 `sdp_ram` 而不是 `tdp_ram`**，本讲会把这件事讲透。
- **同步 FIFO（u4-l1）**：单时钟域、`fall-through`、AXI-S 接口，内部实例化 `sdp_ram`（同步模式）。

此外需要一点直觉：

> **什么是异步 FIFO？** 写端口和读端口跑在**两个互不相关**的时钟上（频率、相位都独立）。数据要从一个时钟域安全搬到另一个时钟域，既不能丢数据（写满了还硬写），也不能读出垃圾（把还没写完的多比特指针当成合法值）。

> **为什么不能直接把二进制指针拉到对侧？** 二进制计数器自增时常常**多位同时翻转**（例如 `011 → 100` 一次翻 3 位）。对侧时钟如果恰好在这一瞬采样，由于各比特走线延时不同，同步器可能采到「半新半旧」的非法中间值（如 `000` 或 `111`），FIFO 就会误判满空。

---

## 3. 本讲源码地图

| 文件 | 角色 |
|:--|:--|
| [hdl/psi_common_async_fifo.vhd](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_async_fifo.vhd) | **本讲主角**。双时钟 FIFO 实体，含两进程 record、格雷码同步、满空判别、RAM 与复位实例化。 |
| [hdl/psi_common_logic_pkg.vhd](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_logic_pkg.vhd) | 提供 `binary_to_gray` / `gray_to_binary` 两个函数，是指针跨时钟域的核心工具。 |
| [hdl/psi_common_sdp_ram.vhd](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_sdp_ram.vhd) | **async_fifo 真正实例化的底层存储**（`is_async_g => true`，独立读写时钟）。 |
| [hdl/psi_common_tdp_ram.vhd](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_tdp_ram.vhd) | 真双口 RAM，**本组件并未使用**。放在地图里是为了对比、解释「为何 FIFO 选 `sdp_ram`」。 |
| [testbench/psi_common_async_fifo_tb/psi_common_async_fifo_tb.vhd](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/testbench/psi_common_async_fifo_tb/psi_common_async_fifo_tb.vhd) | 自校验测试平台，两个时钟分别 100 MHz 与 83.333 MHz（完全异步），覆盖复位、满、空、几乎满/空、占空比变化等场景。 |
| [sim/config.tcl](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/sim/config.tcl) | 回归测试注册表，登记了 `async_fifo_tb` 的 5 组 generic 组合。 |
| [doc/files/psi_common_async_fifo.md](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/doc/files/psi_common_async_fifo.md) | 官方组件说明，含 CDC 时序约束示例（`set_max_delay`）。 |

---

## 4. 核心概念与源码讲解

### 4.1 双时钟与指针同步

#### 4.1.1 概念说明

异步 FIFO 的写侧（`in_clk_i`）维护写指针 `WrAddr`，读侧（`out_clk_i`）维护读指针 `RdAddr`，二者各自只在自己的时钟域里自增，互不干扰。

问题来了：写侧要知道「还剩多少空间」（用来判断满），就必须知道读指针走到了哪里；读侧要知道「还有多少数据」（用来判断空），就必须知道写指针走到了哪里。也就是说，**每个时钟域都需要看到对侧的指针**。

把一个多位计数值从 A 时钟域搬到 B 时钟域，标准做法是 Cliff Cummings 提出的「**格雷码 + 两级同步器**」：

1. 在源域把二进制指针转成格雷码（`binary_to_gray`）；
2. 用**两级触发器**在目的域采样这个格雷码（第一级可能亚稳态，第二级大概率稳定）；
3. 在目的域把格雷码还原为二进制（`gray_to_binary`），再用于计算。

之所以必须用格雷码，核心在于：**格雷码相邻值只差 1 比特**。即使目的域的同步器恰好在指针翻转的那一拍采样，它也只能采到「旧值」或「新值」中的一个，绝不会采到多位同时翻转造成的非法中间值。最坏情况只是指针晚到一拍，这对 FIFO 是**安全的保守偏差**（只会让满标志早一点拉高、空标志早一点拉高），永远不会丢数据或读出垃圾。

对比一下二进制的危险：

| 计数自增 | 二进制 | 同时翻转位数 | 格雷码 | 同时翻转位数 |
|:--|:--|:--|:--|:--|
| 3 → 4 | `011 → 100` | **3 位** | `010 → 110` | **1 位** |
| 7 → 8 | `0111 → 1000` | **4 位** | `0100 → 1100` | **1 位** |

#### 4.1.2 核心流程

指针从读域跨到写域的完整链路（写域据此判满）：

```
[读域] RdAddr(二进制)
   │  binary_to_gray（廉价：移位+异或）
   ▼
[读域] RdAddrGray          ── 寄存，准备跨域
   │  ═══ 跨时钟域（亚稳态风险点）═══
   ▼
[写域] RdAddrGraySync      ── 第 1 级同步 FF（可能亚稳态）
   ▼
[写域] RdAddrGray          ── 第 2 级同步 FF（已稳定）
   │  gray_to_binary（较深：累积异或链）
   ▼
[写域] RdAddr(二进制)       ── 用于写侧满/电平计算
```

写指针跨到读域的链路完全对称（`WrAddrGray → WrAddrGraySync → WrAddrGray → WrAddr`）。

注意：`binary_to_gray` 只有一层异或，逻辑很浅，**可以和指针寄存器合并**；而 `gray_to_binary` 是一条从最高位累加下来的异或链，逻辑较深，**必须单独用一拍寄存器收住**，否则时序会爆。源码注释把这点写得很清楚（见 4.1.3）。

#### 4.1.3 源码精读

先把两个 record 的字段看清——写侧记录 `two_process_in_r` 和读侧记录 `two_process_out_r` 各自持有一套指针和同步链：

写侧记录里，`RdAddrGraySync` / `RdAddrGray` / `RdAddr` 就是「读指针跨到写域」的三个落点：

[hdl/psi_common_async_fifo.vhd:59-65](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_async_fifo.vhd#L59-L65) —— 写侧 record，`WrAddr*` 是本地指针，`RdAddr*` 是跨域过来的读指针。

读侧记录对称地持有 `WrAddrGraySync` / `WrAddrGray` / `WrAddr`：

[hdl/psi_common_async_fifo.vhd:67-74](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_async_fifo.vhd#L67-L74) —— 读侧 record，`RdAddr*` 是本地指针，`WrAddr*` 是跨域过来的写指针。

真正的同步发生在组合进程 `p_comb` 的「Address Clock domain crossings」段。先做「二进制→格雷」（浅逻辑，直接给本地指针编码）：

[hdl/psi_common_async_fifo.vhd:201-204](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_async_fifo.vhd#L201-L204) —— `vi.WrAddrGray := binary_to_gray(std_logic_vector(vi.WrAddr))`，读侧同理。注释明确：Bin→Gray 很简单，不需要额外触发器。

然后是两级同步器（注意四行赋值的「源/目的」配对）：

[hdl/psi_common_async_fifo.vhd:206-210](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_async_fifo.vhd#L206-L210) ——
- `vi.RdAddrGraySync := ro.RdAddrGray`：写域从读域抓取读指针格雷码（**跨域第 1 级**）；
- `vi.RdAddrGray := ri.RdAddrGraySync`：写域第 2 级；
- `vo.WrAddrGraySync := ri.WrAddrGray`：读域从写域抓取写指针格雷码（**跨域第 1 级**）；
- `vo.WrAddrGray := ro.WrAddrGraySync`：读域第 2 级。

最后「格雷→二进制」（深逻辑，结果落入 `RdAddr`/`WrAddr` 寄存器字段）：

[hdl/psi_common_async_fifo.vhd:212-214](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_async_fifo.vhd#L212-L214) —— `vi.RdAddr := unsigned(gray_to_binary(ri.RdAddrGray))`，注释说明 Gray→Bin 逻辑较深、需要额外触发器收住。

这两条同步链分别由各自域的时钟进程寄存：写侧字段（含 `RdAddrGraySync/RdAddrGray/RdAddr`）由 `p_seq_in` 在 `in_clk_i` 上跳变时打入：

[hdl/psi_common_async_fifo.vhd:222-234](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_async_fifo.vhd#L222-L234) —— `ri <= ri_next`，复位时把写侧指针清零。

读侧字段由 `p_seq_out` 在 `out_clk_i` 上跳变时打入：

[hdl/psi_common_async_fifo.vhd:236-249](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_async_fifo.vhd#L236-L249) —— `ro <= ro_next`。

为了让综合工具正确处理这些跨域寄存器，源码贴了三类属性：

[hdl/psi_common_async_fifo.vhd:94-104](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_async_fifo.vhd#L94-L104) ——
- `syn_srlstyle = "registers"` 与 `shreg_extract = "no"`：禁止工具把同步器塞进 SRL（移位寄存器查找表），强制用真正的触发器，保证两级同步的时序/布局可控；
- `ASYNC_REG = "TRUE"`：告诉工具这些寄存器处于异步路径，应把两级 FF 摆得尽量靠近，最大化 MTBF（平均无故障时间）。

底层两个函数本身（u2-l2 已逐行讲过，这里只贴关键行）：

[hdl/psi_common_logic_pkg.vhd:141-147](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_logic_pkg.vhd#L141-L147) —— `binary_to_gray`：`Gray := binary xor ('0' & binary(high downto low+1))`，即 \(g = b \oplus (b \gg 1)\)。

[hdl/psi_common_logic_pkg.vhd:150-159](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_logic_pkg.vhd#L150-L159) —— `gray_to_binary`：最高位不变，其余 \(b_i = g_i \oplus b_{i+1}\)，自上而下累积异或（逻辑深度 \(O(n)\)）。

#### 4.1.4 代码实践

> **实践目标**：亲手把两条格雷码指针同步路径走一遍，能在波形/源码里指出「源域编码 → 跨域第 1 级 → 第 2 级 → 目的域解码」四级，并解释每一级的作用。

操作步骤（源码阅读型 + 可选仿真）：

1. 打开 [hdl/psi_common_async_fifo.vhd](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_async_fifo.vhd)，定位到 [L206-L210](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_async_fifo.vhd#L206-L210)。
2. 跟踪**读指针 → 写域**的四级：
   - 源域编码：`ro.RdAddr` →（`binary_to_gray`，L204）→ `ro.RdAddrGray`（读域寄存）；
   - 跨域第 1 级：`vi.RdAddrGraySync := ro.RdAddrGray`（L207，**这是真正跨越时钟域的那一跳**，由 `p_seq_in` 在 `in_clk_i` 打入 `ri.RdAddrGraySync`）；
   - 第 2 级：`vi.RdAddrGray := ri.RdAddrGraySync`（L208，同域、干净）；
   - 目的域解码：`vi.RdAddr := unsigned(gray_to_binary(ri.RdAddrGray))`（L213，落入写域 `ri.RdAddr`，写域满判据用它）。
3. 对称地跟踪**写指针 → 读域**：`ri.WrAddrGray`（L203）→ `ro.WrAddrGraySync`（L209）→ `ro.WrAddrGray`（L210）→ `ro.WrAddr`（L214）。
4. 可选仿真：按 u1-l3 的方式跑 `psi_common_async_fifo_tb`，在波形里把 `in_clk_i`、`out_clk_i`、`i_dut/ri.RdAddrGray`、`i_dut/ri.RdAddrGraySync`、`i_dut/ro.RdAddrGray` 拉到一起，观察一次读指针自增后，格雷码逐级「跳」过同步器的过程。

需要观察的现象：

- 第 1 级同步 FF 偶尔会比源域晚一拍才更新（这就是 CDC 的固有延迟）；
- 任意一拍，`RdAddrGray` 与 `RdAddrGraySync` 的值要么完全等于旧值、要么完全等于新值，**绝不会出现多位「半翻转」**——这正是格雷码的功劳。

预期结果：两个方向的四级链路都能在源码里逐行对应；如运行仿真，能看到同步器逐级传播且无非法中间值。若环境无法运行仿真，标注「待本地验证」即可，源码阅读部分结论不变。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `binary_to_gray` / `gray_to_binary` 全部去掉，直接跨域传二进制指针，最坏会发生什么？

> **答案**：二进制自增常多位同翻，对侧同步器可能采到非法中间值（如 `011→100` 被采成 `000`/`111`/`101`）。FIFO 据此算出的电平会出错，可能在该满时判「没满」继续写（丢数据），或在该空时判「不空」继续读（读出垃圾）。

**练习 2**：源码注释说「Gray→Bin needs additional FF」，请结合 [L150-L159](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_logic_pkg.vhd#L150-L159) 解释为什么。

> **答案**：`gray_to_binary` 是从最高位往下累积异或（\(b_i = g_i \oplus b_{i+1}\)），组合逻辑深度随位宽线性增长，时序差；必须把结果寄存一拍（即源码里的 `ri.RdAddr`/`ro.WrAddr` 字段）才能跑高频。而 `binary_to_gray` 只有一层异或，可直接折进指针寄存器，不必单独占一拍。

---

### 4.2 格雷码满空判别

#### 4.2.1 概念说明

很多人误以为「异步 FIFO 用格雷码做满空比较」，于是去格雷码空间里找满/空的特征码。本组件**不是这么做的**：

- 格雷码**只负责安全地把指针搬过时钟域**；
- 搬到对侧后，**先还原成二进制，再用二进制减法算「电平」，再判满空**。

这种方法干净、直观，但有一个前提：**指针必须比 RAM 地址多一位**。

#### 4.2.2 核心流程

设深度 \(D = 2^n\)（本组件要求深度为 2 的幂，见 [L108](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_async_fifo.vhd#L108) 的 assert）。

- RAM 地址宽度 = \(n\) 位，取值 \(0 \ldots 2^n-1\)；
- 指针宽度 = \(n+1\) 位，取值 \(0 \ldots 2^{n+1}-1\)，**最高位是「圈数位」**。

写侧电平定义为：

\[
L_{wr} = W - R \pmod{2^{n+1}}
\]

其中 \(W\) 是本地写指针，\(R\) 是跨域过来的读指针（都已还原为二进制）。由于指针会自然回绕（取低 \(n\) 位作 RAM 地址），减法结果落在 \([0, 2^n]\)：

\[
\text{满} \iff L_{wr} = D = 2^n,\qquad \text{空} \iff L_{wr} = 0
\]

**「多一位」的作用**：让「写指针正好比读指针多走一整圈」与「两者完全相等」这两种情形在二进制下可区分。

- 两者相等 → \(L_{wr}=0\) → **空**（全部位相同）；
- 写比读多走一圈 → 最低 \(n\) 位相同、仅最高位不同 → \(L_{wr}=2^n=D\) → **满**。

如果没有那一位，这两种情形的低 \(n\) 位都相同，满与空就无法区分了。

举一个 \(D=8\)（\(n=3\)，指针 4 位）的例子：

| 状态 | WrAddr | RdAddr | W−R | 判定 |
|:--|:--|:--|:--|:--|
| 空 | `0000` (0) | `0000` (0) | 0 | **empty** |
| 写入 3 个 | `0011` (3) | `0000` (0) | 3 | level=3 |
| 写满 8 个 | `1000` (8) | `0000` (0) | 8 | **full**（最高位不同，低位全同）|
| 读空 8 个 | `1000` (8) | `1000` (8) | 0 | **empty** |

注意「写满」和「空」两种情形，指针的低 3 位都是 `000`——正是那一位最高位把它们分开。

读侧电平 \(L_{rd}\) 用本地读指针减去跨域过来的写指针，逻辑完全对称，本组件还额外把 \(L_{rd}\) 寄存了一拍（`out_lvl_o` 字段）。

#### 4.2.3 源码精读

指针比 RAM 地址多一位，体现在 record 字段的位宽声明上：

[hdl/psi_common_async_fifo.vhd:59-65](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_async_fifo.vhd#L59-L65) —— `WrAddr : unsigned(log2ceil(depth_g) downto 0)`，注释直说「One additional bit for full/empty detection」。RAM 地址是 `log2ceil(depth_g)-1 downto 0`，正好少一位。

写侧的电平、满、写执行：

[hdl/psi_common_async_fifo.vhd:128-146](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_async_fifo.vhd#L128-L146) ——
- `InLevel_v := ri.WrAddr - ri.RdAddr;`（L129）就是 \(L_{wr}\)；
- `if InLevel_v = depth_g then in_full_o <= '1'`（L133）判满；
- 否则 `in_rdy_o <= '1'`（即 not full），并在 `in_vld_i='1'` 时执行写：`vi.WrAddr := ri.WrAddr + 1`（L139）、拉高 `RamWr`（L140）。

写侧的空/几乎满/几乎空标志：

[hdl/psi_common_async_fifo.vhd:148-157](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_async_fifo.vhd#L148-L157) —— `InLevel_v = 0` 判空（L149），`InLevel_v >= afull_lvl_g` 判几乎满（L152），`InLevel_v <= aempty_level_g` 判几乎空（L155）。

读侧电平的算略有不同：先算 `ro.WrAddr - ro.RdAddr`，若本拍发生读则再减 1（这样 `out_lvl_o` 反映「读出之后」的余量，符合 fall-through 语义）：

[hdl/psi_common_async_fifo.vhd:167-188](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_async_fifo.vhd#L167-L188) —— `vo.out_lvl_o := ro.WrAddr - ro.RdAddr`，`out_rdy_i='1'` 时减 1（L172-L174）；随后 `out_lvl_o=0` 判空（L179），否则拉 `out_vld_o` 并在读握手时 `vo.RdAddr := ro.RdAddr + 1`（L185）。

RAM 地址由指针**剥掉最高位**得到，让低 \(n\) 位自然回绕：

[hdl/psi_common_async_fifo.vhd:251](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_async_fifo.vhd#L251) —— `RamWrAddr <= std_logic_vector(ri.WrAddr(log2ceil(depth_g)-1 downto 0))`；读侧同理在 [L188](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_async_fifo.vhd#L188) 剥位。

正因为依赖「低 \(n\) 位自然回绕」，深度必须是 2 的幂，源码用 assert 强制：

[hdl/psi_common_async_fifo.vhd:108](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_async_fifo.vhd#L108) —— `assert log2(depth_g) = log2ceil(depth_g) report "###ERROR###: ... only power of two depth_g is allowed"`。

#### 4.2.4 代码实践

> **实践目标**：手算一个 \(D=8\) 的微型 FIFO，验证「多一位」如何区分满与空，并在源码里确认判满/判空用的是二进制减法。

操作步骤：

1. 取 `depth_g = 8`。计算 `log2ceil(8) = 3`，所以指针宽 `3 downto 0` = 4 位，RAM 地址宽 `2 downto 0` = 3 位。
2. 假设写侧连续写入 8 个字、读侧不读：手算 `ri.WrAddr` 从 `0000` 每拍 +1，写到第 8 个后变为 `1000`；此时 `ri.RdAddr`（跨域过来的读指针）仍为 `0000`。`InLevel_v = 1000 − 0000 = 8 = depth_g` → 对照 [L133](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_async_fifo.vhd#L133)，`in_full_o='1'`。
3. 现在让读侧把 8 个字全读走：`ro.RdAddr` 走到 `1000`，跨到写域后 `ri.RdAddr=1000`。`InLevel_v = 1000 − 1000 = 0` → 对照 [L149](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_async_fifo.vhd#L149)，`in_empty_o='1'`。
4. 在 [L251](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_async_fifo.vhd#L251) 确认 RAM 写地址是 `WrAddr(2 downto 0)`，即 `1000` 的低 3 位 `000`——写满时它正好回到地址 0。

需要观察的现象 / 预期结果：满（`1000` vs `0000`）与空（`1000` vs `1000`）的低 3 位都是 `000`，唯一区别是最高位；若把指针改成 3 位（去掉最高位），满和空就都变成「低 3 位相等」无法区分——这就是「多一位」的必要性。

#### 4.2.5 小练习与答案

**练习 1**：为什么本组件要求 `depth_g` 是 2 的幂？给一个非 2 幂深度会出什么问题？

> **答案**：RAM 地址取指针低 \(n\) 位、依赖「自然二进制回绕」覆盖 \(0 \ldots 2^n-1\) 个地址。若深度不是 2 的幂（如 10），低 \(n\) 位回绕会覆盖 16 个地址而 RAM 只有 10 个，地址越界；且「多一位」的满判据 \(L=D\) 也只在 \(D=2^n\) 时与「最高位翻转」严格对应。[L108](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_async_fifo.vhd#L108) 的 assert 就是拦截这种情况（对比：同步 FIFO `sync_fifo` 用显式回绕，可支持任意深度）。

**练习 2**：满判据是「保守」的吗？也就是说，写侧看到的读指针可能滞后，这会让 FIFO 提前报满还是延后报满？

> **答案**：保守、提前报满（safe）。跨域读指针最多滞后真实值一拍，于是 \(L_{wr}\) 可能被高估，满标志可能比「真实满」更早拉高、`in_rdy_o` 更早拉低——结果只是写侧暂时少写一两拍，绝不丢数据。反之读侧看到的写指针也可能滞后，使空标志提前拉高、读侧暂时少读，同样安全。

**练习 3**：读侧 `out_lvl_o` 在发生读的那一拍为什么还要再减 1？

> **答案**：见 [L172-L174](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_async_fifo.vhd#L172-L174)。`out_lvl_o` 是寄存输出，若不减 1，它会比「读出后真实余量」多 1；减 1 后它反映当拍读走一个字之后的余量，与 fall-through FIFO 对外承诺的 `out_lvl_o` 语义一致（u4-l1 同步 FIFO 也是这个约定）。

---

### 4.3 两进程 record 架构

#### 4.3.1 概念说明

`async_fifo` 的 RTL 主体只有三个进程：一个组合进程 `p_comb`、两个时钟进程 `p_seq_in` / `p_seq_out`。所有状态被收进两个 record：

- `two_process_in_r`（写侧，实例化为信号 `ri` / `ri_next`）；
- `two_process_out_r`（读侧，实例化为信号 `ro` / `ro_next`）。

这就是 PSI 库常用的「**两进程 record 设计法**」：每个时钟域一对「现态 `r` / 次态 `r_next`」，组合进程算 `r_next`，时钟进程把 `r_next` 打回 `r`。它的好处是：状态显式集中、复位只写现态、跨域信号可以「跨 record 赋值」一目了然。`async_fifo` 把它用到了极致——**两条跨域同步链就是两个 record 之间的字段搬运**。

> 术语：**fall-through FIFO** 指读侧 `out_dat_o` 上常驻队首字，`out_vld_o='1'` 即可读、`out_rdy_i` 一拉高就取走（u4-l1）。本组件也是 fall-through。

#### 4.3.2 核心流程

```
        ┌─────────────── in_clk_i 域 ───────────────┐ ┌── out_clk_i 域 ──┐
        │  p_comb(组合) 算 ri_next / ro_next         │ │                  │
        │   ├─ 写侧：算 InLevel、满/空、执行写        │ │  读侧：算 out_lvl│
        │   ├─ 读侧：算 out_lvl、空、执行读           │ │  空、执行读       │
        │   └─ 跨域：ro.RdAddrGray→ri.RdAddrGraySync  │◄┤  ro.RdAddr→Gray  │
        │             ri.WrAddrGray→ro.WrAddrGraySync │ └────────┬─────────┘
        │             gray_to_binary 收尾             │          │
        └──────────┬──────────────────────────────────┘          │
                   ▼                                              ▼
            p_seq_in: ri<=ri_next (in_clk)              p_seq_out: ro<=ro_next (out_clk)
```

要点：

1. **「保持变量稳定」开场**：`p_comb` 一进来就 `vi := ri; vo := ro;`，把现态拷给变量，之后只改需要改的字段，未改字段自然保持——这是两进程法避免「锁存器/漏赋值」的标准手法。
2. **跨域即跨 record**：`vi.RdAddrGraySync := ro.RdAddrGray` 这种写法清楚显示了「读域 record 的字段 → 写域 record 的字段」的跨越。
3. **复位只动现态**：`p_seq_in` / `p_seq_out` 在各自复位有效时把本域 record 清零，不动对侧。
4. **存储与复位 CDC 单独实例化**：RAM 用 `sdp_ram`，复位跨越用 `pulse_cc`，都不挤进主进程。

#### 4.3.3 源码精读

组合进程开头「保持稳定」与写侧默认值：

[hdl/psi_common_async_fifo.vhd:110-126](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_async_fifo.vhd#L110-L126) —— `vi := ri; vo := ro;` 之后给输出与 `RamWr` 赋默认 `'0'`，避免锁存。

把算好的次态交回信号：

[hdl/psi_common_async_fifo.vhd:216-218](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_async_fifo.vhd#L216-L218) —— `ri_next <= vi; ro_next <= vo;`。

两个时钟进程只做「打回 + 复位」，不含业务逻辑：

[hdl/psi_common_async_fifo.vhd:222-234](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_async_fifo.vhd#L222-L234) 与 [hdl/psi_common_async_fifo.vhd:236-249](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_async_fifo.vhd#L236-L249) —— `ri <= ri_next` / `ro <= ro_next`，复位时清本域字段。

底层存储：`async_fifo` 实例化的是 `psi_common_sdp_ram`，并开 `is_async_g => true`（独立读写时钟）：

[hdl/psi_common_async_fifo.vhd:251-271](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_async_fifo.vhd#L251-L271) —— 写口接 `in_clk_i`，读口接 `out_clk_i`，读使能固定 `'1'`。

`is_async_g => true` 让 `sdp_ram` 内部走「写进程跑写时钟、读进程跑读时钟」的异步双进程实现：

[hdl/psi_common_sdp_ram.vhd:66-86](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_sdp_ram.vhd#L66-L86) —— `write_p` 用 `wr_clk_i`，`read_p` 用 `rd_clk_i`，共享同一个 `shared variable mem`。

> **关于 spec 列出的 `tdp_ram`**：`psi_common_tdp_ram` 的 A/B 两端口是**对称、可双向读写**的（见 [hdl/psi_common_tdp_ram.vhd:19-33](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_tdp_ram.vhd#L19-L33)）。FIFO 是**有方向**的（一端只写、一端只读），用 `sdp_ram` 这种「一写口一读口」的结构刚好够用、资源更省，所以库作者选 `sdp_ram` 而非 `tdp_ram`。`tdp_ram` 留给真正需要双向读写的场合（如 `ping_pong` 乒乓缓冲，见 u7-l4）。这印证了 u3-l2 的提醒：**「双时钟」不等于「必须用真双口 RAM」**。

复位也要跨时钟域：`async_fifo` 用一个 `psi_common_pulse_cc` 把两侧复位同步/合成：

[hdl/psi_common_async_fifo.vhd:273-290](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_async_fifo.vhd#L273-L290) —— 注释直说「only used for reset crossing and oring」；产出 `RstInInt` / `RstOutInt` 喂给两个时钟进程。`pulse_cc` 的细节见 u5-l1。

最后，写侧 `rdy_rst_state_g` 控制复位期间 `in_rdy_o` 的电平：

[hdl/psi_common_async_fifo.vhd:143-146](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_async_fifo.vhd#L143-L146) —— 默认 `'1'`（ready 路径逻辑最少），取 `'0'` 时在复位期间强行拉低 `in_rdy_o` 挡住上游，代价是 FMAX 略差。语义与 u4-l1 同步 FIFO 完全一致。

#### 4.3.4 代码实践

> **实践目标**：跑官方自校验 TB，确认两进程架构在两个无关时钟下行为正确；并在源码里指出每个 record 字段归哪个时钟进程管。

操作步骤：

1. 在 [sim/config.tcl:261-268](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/sim/config.tcl#L261-L268) 查看已注册的 5 组 generic（覆盖 `afull_on` 开关、`rdy_rst_state_g` 0/1、`depth_g` 32/128、`ram_behavior_g` RBW/WBR、复位极性）。
2. 按 u1-l3 流程跑 Modelsim 回归（`run.tcl`）或 GHDL（`runGhdl.tcl`）。成败判据是 TB 是否打印 `###ERROR###`。
3. 阅读 [testbench/psi_common_async_fifo_tb/psi_common_async_fifo_tb.vhd:42-45](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/testbench/psi_common_async_fifo_tb/psi_common_async_fifo_tb.vhd#L42-L45)：写时钟 100 MHz、读时钟 83.333 MHz——二者无整数倍关系，是**真正完全异步**的激励，能压出 CDC 问题。
4. 在源码里为 `two_process_in_r` 的每个字段标注「由 `p_seq_in`（`in_clk_i`）寄存」，为 `two_process_out_r` 的每个字段标注「由 `p_seq_out`（`out_clk_i`）寄存」。

需要观察的现象：

- TB 依次跑过 `>> Reset`、`>> Two words write then read`、`>> Write into Full FIFO`、`>> Read from Empty FIFO`、`>> Almost full/almost empty`、`>> Different Duty Cycles`、`>> Output Ready before data available` 各阶段；
- 「Write into Full FIFO」阶段先填满 `depth_g` 个字、再硬写两个（应被丢弃），随后读回全部，数据应严格等于写入序号（[L267-L297](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/testbench/psi_common_async_fifo_tb/psi_common_async_fifo_tb.vhd#L267-L297)）。

预期结果：5 组 generic 全部不打印 `###ERROR###`。若本地无仿真器，标注「待本地验证」，源码阅读结论不变。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `p_comb` 开头要写 `vi := ri; vo := ro;`？删掉会怎样？

> **答案**：这是「保持变量稳定」手法，让所有未显式赋值的字段保持原值。删掉后，变量未赋值字段会变成不确定值/触发意外锁存，组合逻辑输出紊乱。两进程 record 法几乎都以此开场（u7-l1 的 `pl_stage` 也是）。

**练习 2**：`async_fifo` 为什么不用一个 record、一个时钟进程，而要拆成 `ri`/`ro` 两套？

> **答案**：因为有两个时钟域。同一个 record 不能被两个不同时钟的进程同时驱动（多驱动冲突）。把写侧状态归 `ri`（`in_clk_i` 驱动）、读侧状态归 `ro`（`out_clk_i` 驱动），跨域信息通过「组合进程里跨 record 赋值 + 同步器」传递，才是合法且可综合的写法。

**练习 3**：`sdp_ram` 的 `shared variable mem` 为什么能被两个不同时钟的进程共同读写而不冲突？

> **答案**：`shared variable` 在 VHDL 里可被多进程访问（普通 `signal` 不能被多驱动）。综合时它被映射成一块真正的双口 RAM 存储体：写进程连写口、读进程连读口。逻辑上「冲突」由设计保证不存在——FIFO 的写地址与读地址不会同拍写同一格（满/空判据已拦住），且此处只关心数据搬运会否丢/错，由 CDC 同步保证。详见 u3-l1。

---

### 4.4 与 sync_fifo 对比

#### 4.4.1 概念说明

`psi_common_sync_fifo`（u4-l1）与 `psi_common_async_fifo`（本讲）都是 fall-through、AXI-S、内部都用 `sdp_ram`。区别全在「单时钟 vs 双时钟」带来的连锁后果。把两者放在一起对比，能巩固「为什么异步 FIFO 要做这些额外的事」。

#### 4.4.2 核心流程（对比表）

| 维度 | sync_fifo（u4-l1） | async_fifo（本讲） |
|:--|:--|:--|
| 时钟 | 单一 `clk_i` | 写 `in_clk_i` / 读 `out_clk_i`，完全无关 |
| 底层 RAM | `sdp_ram`，`is_async_g => false`（同步读） | `sdp_ram`，`is_async_g => true`（独立读时钟） |
| 指针宽度 | `log2ceil(depth)` 位，显式回绕到 `depth-1` | `log2ceil(depth)+1` 位，多一位用于满空区分 |
| 深度约束 | **任意正整数**（显式回绕） | **必须 2 的幂**（assert 强制，[L108](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_async_fifo.vhd#L108)）|
| 跨域指针 | 不需要 | 格雷码 + 两级同步器（4.1）|
| 电平算法 | 双计数器 `WrLevel`/`RdLevel`，互报增减 | 指针二进制相减 `WrAddr - RdAddr`（4.2）|
| 进程结构 | 单进程为主 | 两进程 record（`ri`/`ro`，4.3）|
| 复位 | 单时钟复位即可 | 用 `pulse_cc` 做复位 CDC（[L273-L290](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_async_fifo.vhd#L273-L290)）|
| 综合属性 | 无特殊 | `ASYNC_REG` / `syn_srlstyle` / `shreg_extract`（[L94-L104](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_async_fifo.vhd#L94-L104)）|
| 额外时序约束 | 不需要 | CDC 路径需 `set_max_delay`（见下） |
| 共同点 | fall-through、AXI-S、`alm_full/alm_empty/level`、`rdy_rst_state_g` 同语义 | 同左 |

#### 4.4.3 源码精读

选型相关的两个关键差异点：

1. **深度约束**：`sync_fifo` 接受任意深度（u4-l1 的显式回绕 `if WrAddr = depth_g-1 then ...`）；`async_fifo` 强制 2 的幂——[hdl/psi_common_async_fifo.vhd:108](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_async_fifo.vhd#L108)。
2. **CDC 时序约束**：异步 FIFO 必须在约束文件里给跨域路径设上界。官方文档给了 Vivado 示例：

[doc/files/psi_common_async_fifo.md:74-83](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/doc/files/psi_common_async_fifo.md#L74-L83) —— 以 100 MHz / 33.33 MHz 为例，`set_max_delay --datapath_only --from <ClkA> -to <ClkB> 10.0`（取较快时钟周期为双向上限）。`sync_fifo` 单时钟，不需要这一步。

#### 4.4.4 代码实践

> **实践目标**：用对比加深选型直觉，并亲手触发 `async_fifo` 的「非 2 幂深度」断言。

操作步骤（源码阅读型，无需仿真器即可完成前两步）：

1. 打开 [hdl/psi_common_async_fifo.vhd:108](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_async_fifo.vhd#L108)，确认 `depth_g` 必须是 2 的幂。
2. 在 [sim/config.tcl:261-268](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/sim/config.tcl#L261-L268) 复制一行 run，把 `-gdepth_g=32` 改成 `-gdepth_g=10`（非 2 幂）。**注意：这是临时实验，不要提交。**
3. 跑该 run（Modelsim 或 GHDL）。elaborate/仿真启动时应看到 `###ERROR###: psi_common_async_fifo: only power of two depth_g is allowed`。

需要观察的现象：断言在 elaboration 阶段就触发并报 `severity error`。

预期结果：`depth_g=10` 报错；改回 `32`/`128`（config.tcl 原值）恢复正常。若本地无仿真器，可只做源码阅读：对照 `sync_fifo`（u4-l1，无此 assert）确认这是异步 FIFO 独有的约束。完成实验后**务必撤销**对 config.tcl 的临时改动（本讲禁止改源码与回归脚本，此处仅作只读观察，正式实验请在副本里做）。

#### 4.4.5 小练习与答案

**练习 1**：某设计要把 12 位 ADC 采样（写时钟 80 MHz）送给 50 MHz 的下游，深度需求恰好是 1000。能直接用 `async_fifo` 吗？

> **答案**：时钟无关，确实需要异步 FIFO；但 `async_fifo` 要求深度为 2 的幂，1000 不行。应把深度取整到 **1024**（\(\ge 1000\) 的最小 2 幂），用 `depth_g => 1024`。若对深度有严格非 2 幂要求，需自行扩展或改用厂商 FIFO IP。

**练习 2**：为什么 `sync_fifo` 能支持任意深度，`async_fifo` 却不能？

> **答案**：`sync_fifo` 单时钟，可在同一进程里写 `if 指针 = depth-1 then 指针 := 0` 做显式回绕，深度任意。`async_fifo` 依赖「指针低 \(n\) 位自然回绕」覆盖整个 RAM、并用「多一位」的二进制减法判满空，这都只在 \(depth = 2^n\) 时成立，故强制 2 幂。

**练习 3**：两个 FIFO 的 `in_lvl_o` / `out_lvl_o` 端口宽度公式分别是什么？一致吗？

> **答案**：一致，都是 `log2ceil(depth_g+1)-1 downto 0`（见 [async_fifo 端口 L48/L53](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_async_fifo.vhd#L48-L53)）。注意官方文档表格里写成 `log2(depth_g)`，**以源码为准**（u2-l1 已强调 `log2ceil` 是推导位宽的标准工具）。

---

## 5. 综合实践

把本讲四块串起来的小任务：**复现一次「写快读慢」的压满过程，验证反压、几乎满与电平的时序关系，并解释满判据的保守延迟**。

1. **读 TB 已有激励**：[testbench/psi_common_async_fifo_tb/psi_common_async_fifo_tb.vhd:263-303](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/testbench/psi_common_async_fifo_tb/psi_common_async_fifo_tb.vhd#L263-L303)（「Write into Full FIFO」段）。它先连续写满 `depth_g` 个、再硬写两个（应被丢），再连续读回校验。
2. **跑 config.tcl 的第 1 组 run**：`-gafull_on_g=true -gaempty_on_g=true -gdepth_g=32 ...`（[L263](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/sim/config.tcl#L263)）。此时几乎满阈值 `afull_lvl_g = depth_g-3 = 29`、几乎空阈值 `aempty_level_g = 5`（[TB L36-L37](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/testbench/psi_common_async_fifo_tb/psi_common_async_fifo_tb.vhd#L36-L37)）。
3. **在波形里同时观察**：`in_clk_i`、`out_clk_i`、`in_lvl_o`、`out_lvl_o`、`in_full_o`、`in_afull_o`、`in_rdy_o`、`i_dut/ri.RdAddr`、`i_dut/ri.RdAddrGray`。
4. **回答三个问题**（把 4.1–4.4 串起来）：
   - 当 `in_lvl_o` 到达 32 时，`out_lvl_o` 是否**同时**到达 32？为什么可能有几拍差异？（提示：写指针跨到读域需要两级同步 + 一拍 gray_to_binary。）
   - `in_afull_o` 在电平到 29 时拉高，但若此刻读侧其实已经取走了数据，写侧会不会「冤枉地」多拉一拍几乎满？（提示：读指针跨到写域有延迟，满/几乎满判据是**保守**的。）
   - 把 `out_clk_i` 频率改成与 `in_clk_i` 成**整数比**（例如 50 MHz / 100 MHz），满空行为会不会因此变准？（提示：整数比同步跨越有专门组件 `sync_cc_*`，见 u5-l3；`async_fifo` 面向的是**完全无关**时钟，整数比只是它的一个特例。）
5. **预期结果**：数据完整性校验全部通过（`out_dat_o` 严格等于写入序号），`in_full_o` 拉高期间 `in_rdy_o='0'`（反压生效），硬写的两个字被丢弃。若本地无仿真器，第 3–4 步改为「在源码里逐行论证」，并标注「待本地验证」。

---

## 6. 本讲小结

- 异步 FIFO 用「**格雷码 + 两级同步器**」把读写指针安全搬过时钟域；格雷码相邻值只差 1 位，同步器只会采到旧值或新值，不会采到非法中间值。
- **满空判别在二进制下完成**：指针比 RAM 地址多一位（\(n+1\) 位），用 \(L = W - R\) 直接得到电平，满对应 \(L=D\)、空对应 \(L=0\)，那一位最高位专门用来区分「多走一圈」与「相等」。
- 本组件是 PSI 库「**两进程 record 设计法**」的典型样本：写侧 `ri/ri_next`、读侧 `ro/ro_next`，组合进程算次态、两个时钟进程分别打回，跨域即跨 record 赋值。
- 底层存储用的是 **`psi_common_sdp_ram`（`is_async_g => true`）**，而非 `tdp_ram`——FIFO 有方向，一写口一读口刚好够用；复位跨越另外用 `pulse_cc`。
- 与 `sync_fifo` 的关键差异：异步 FIFO **要求深度为 2 的幂**、需要 `ASYNC_REG` 等综合属性与 `set_max_delay` 时序约束、满空判据因 CDC 延迟而**保守**；两者共享 fall-through、AXI-S、`alm_*`/`level`/`rdy_rst_state_g` 等接口约定。

---

## 7. 下一步学习建议

- **u5-l1（pulse_cc 与复位同步）**：本讲 [L273-L290](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_async_fifo.vhd#L273-L290) 用的 `pulse_cc` 会在那里展开「翻转-同步-异或」手法，正好与本讲的指针同步互为印证。
- **u5-l2（simple_cc/status_cc/bit_cc）**：本讲的「多 bit 指针用格雷码跨域」是「数据/状态跨越」的特例；学完 u5-l2 可对照理解「为什么单 bit / 慢变状态可以不用格雷码」。
- **u5-l3（sync_cc_n2xn / sync_cc_xn2n）**：综合实践第 4 问提到的「整数比同步时钟」有专门组件，若你的两个时钟其实是同步整数倍，那比 `async_fifo` 更省资源。
- **u7-l1（pl_stage 与二进程设计法）**：本讲的两进程 record 法是全库通用风格，`pl_stage` 是最小、最干净的样本，建议紧接着读。
- **继续读源码**：建议再翻一遍 [hdl/psi_common_async_fifo.vhd](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_async_fifo.vhd) 的 `p_comb`（[L110-L220](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_async_fifo.vhd#L110-L220)），把「写侧—读侧—跨域」三段的赋值顺序在脑子里跑一遍，这是掌握异步 FIFO 最有效的练习。

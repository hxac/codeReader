# FIFO 进阶：FWFT、预读与组合

## 1. 本讲目标

上一讲（u4-l2）我们拆解了 `fifo_single_clock_ram`——一个**只支持 normal 模式**、基于双口块 RAM 的标准单时钟 FIFO。我们留下了一个尾巴：块 RAM 的同步读会让 `r_data` 比读请求**晚一拍**出现，这在很多数据通路里不够顺手。

本讲专门解决"FIFO 读侧时序"与"FIFO 之间如何组合"这两类进阶问题。学完后你应当掌握：

1. **FWFT（First-Word Fall-Through，首字直通）模式**与 normal 模式在时序上的根本差异，并能用 `fifo_single_clock_reg_v2` 在两种模式间切换。
2. **预读 / 提前缓冲**的两种实现：`preview_fifo` 能一次性"预览"未来 2 个字，`read_ahead_buf` 则用一个"幽灵读"把队头提前取进缓冲，让用户在**同一个 `always_ff` 里既发读请求又取数据**。
3. **多 FIFO 聚合**的两种思路：`fifo_combiner`（任一非空即读，仲裁后多路选一）与 `fifo_operator`（全部非空才读，对多路数据施加一个运算）。

本讲是「存储器与 FIFO」单元的收尾，也是后续 u7-l4 综合实战里"数据缓冲"环节的直接前置。

## 2. 前置知识

在进入进阶内容前，请确认你已经理解下面几个概念（u4-l1、u4-l2 已建立）：

- **FIFO 的环形指针与满空判断**：写指针 `w_ptr`、读指针 `r_ptr` 各自回绕递增；用元素计数 `cnt` 译码出 `empty=(cnt==0)`、`full=(cnt==DEPTH)`，计数位宽比地址位宽多一位。
- **normal 模式的读延迟**：`fifo_single_clock_ram` 把存储体委托给双口块 RAM，块 RAM 是**同步读**——`r_req` 当拍给出地址，数据在**下一拍**才出现在 `r_data` 上。这就是"读延迟一拍"。
- **组合读 vs 寄存读**：组合读（`always_comb` / `assign`）当拍即出数据但可能引入组合环路压力；寄存读（`always_ff` 里用 `<=`）数据干净但要等一拍。
- **one-hot（独热）编码**：N 位向量里最多只有 1 位为 1。本讲 `preview_fifo` 的 `wrreq`/`rdreq` 用一种类似独热的 3 位编码表示"不读 / 读 1 个 / 读 2 个"。
- **仲裁器 / 编码器**：把一个多位请求向量压缩成"选中其中一路"的二进制索引。本讲 `fifo_combiner` 会复用 `priority_enc`、`round_robin_enc` 等模块（它们在 u6-l2 详讲，本讲只需把它们当成"从多位请求里选一路的黑盒"）。

一个贯穿全讲的关键直觉：

> **队列的"队头"什么时候对用户可见？** normal 模式说"你先发读请求，我下拍给你"；FWFT 模式说"队头一直摆在你面前，你发读请求只是为了让我把它弹掉、露出下一个"。本讲所有进阶技巧都是在围绕这句话做文章。

## 3. 本讲源码地图

本讲涉及的关键文件如下表。它们都遵循仓库统一的"头注释 / INFO / 例化模板 / module 实现"四段式结构（见 u1-l2）。

| 文件 | 作用 | 存储体 | 是否依赖厂商 IP |
| --- | --- | --- | --- |
| `fifo_single_clock_reg_v2.sv` | 寄存器实现的单时钟 FIFO，**可在 FWFT 与 normal 间切换** | 触发器阵列 `data[]` | 否（纯 SV） |
| `preview_fifo.sv` | show-ahead（FWFT）FIFO，可一次写 / 读 / 预览 **2 个字** | 2 个内部 scfifo | **是**（Altera `scfifo`） |
| `read_ahead_buf.sv` | 套在 FWFT FIFO 读口外的**提前读缓冲**，把队头预先取进缓冲 | 1 个 `soft_latch` | 否（纯 SV，复用 `edge_detect`、`soft_latch`） |
| `fifo_combiner.sv` | 把**多个**输入 FIFO 的数据汇集到**单个**输出 FIFO（任一非空即读） | 无（纯选通逻辑） | 否 |
| `fifo_operator.sv` | 对**多个**输入 FIFO 的数据施加运算后写入输出 FIFO（全部非空才读） | 无（纯运算逻辑） | 否 |

辅助依赖：`read_ahead_buf` 复用了 `edge_detect.sv`（边沿检测，u2-l2）和 `soft_latch.sv`（组合数据保持，u3-l3）；`fifo_combiner` 复用了 `reverse_vector.sv` 和三种编码器；二者都用 `clogb2.svh`（u2-l4）。

## 4. 核心概念与源码讲解

### 4.1 FWFT 模式：让队头数据"自己掉出来"

#### 4.1.1 概念说明

FWFT（First-Word Fall-Through，也叫 show-ahead）模式的字面意思就是"第一个字自动掉出来"。它和 normal 模式的差别完全在读侧时序上：

- **normal 模式**：`r_data` 是**寄存输出**。你必须先拉高 `r_req`，数据在**下一个时钟沿**之后才出现在 `r_data`。读延迟 = 1 拍。这就是 u4-l2 `fifo_single_clock_ram` 的行为（块 RAM 同步读）。
- **FWFT 模式**：`r_data` 是**组合输出**，它**永远等于当前队头** `data[r_ptr]`（只要 FIFO 非空）。你不需要发读请求就能"看到"队头；`r_req` 的作用只是"把队头弹掉、让下一个字掉出来"。读延迟 = 0 拍。

一句话对比：

| | normal | FWFT |
| --- | --- | --- |
| `r_data` 何时有效 | `r_req` 拉高后的**下一拍** | **一直有效**（显示当前队头） |
| `r_req` 的含义 | "我要读，下拍给我数据" | "我消费了当前队头，弹出下一个" |
| 典型实现 | 块 RAM 同步读 / `data_buf` 寄存器 | 组合 `assign r_data = data[r_ptr]` |

为什么需要 FWFT？因为很多控制逻辑希望"先看一眼队头，再决定要不要读"。比如一个状态机要先判断队头是不是某个同步字再决定消费——normal 模式下你得先读出来才能判断，读错了还得塞回去；FWFT 模式下队头一直在眼前，判断完再决定是否弹掉，干净得多。

#### 4.1.2 核心流程

下面用一个最小例子对比两种模式。假设 FIFO 里依次存着 `A, B, C`（`r_ptr` 初始指向 `A`），我们在第 1、2、3 拍连续拉高 `r_req`：

```
normal 模式 (r_data = data_buf, 寄存):
cycle   :   0     1     2     3     4
r_req   :   0     1     1     1     0
r_ptr   :   A     A     B     C     (回绕)
r_data  :   ?     ?     A     B     C      ← 数据比请求晚 1 拍
                                         (cycle1 发请求, cycle3 才看到 A? )
```

> 说明：normal 模式下 `data_buf <= data[r_ptr]` 在 `r_req=1` 的**那个 `always_ff` 沿**采样，所以 cycle1 拉高 `r_req` 时，`data_buf` 在 cycle1 末被赋成 `A`，cycle2 起才在 `r_data` 上看到 `A`。也就是"请求→下一拍出数据"。

```
FWFT 模式 (r_data = data[r_ptr], 组合):
cycle   :   0     1     2     3     4
r_req   :   0     1     1     1     0
r_ptr   :   A     A→B   B→C   C→.. 
r_data  :   A     A     B     C     ..     ← 队头一直可见, r_req 当拍数据就在
```

关键差异：FWFT 下，**即便 `r_req=0`（cycle 0），只要 FIFO 非空，`r_data` 上就已经摆着队头 `A`**。这就是"首字直通"。

#### 4.1.3 源码精读

`fifo_single_clock_reg_v2` 的存储体不是块 RAM，而是一个触发器阵列（u4-l2 的 `fifo_single_clock_ram` 才是块 RAM）：

[fifo_single_clock_reg_v2.sv:90-90](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/fifo_single_clock_reg_v2.sv#L90) —— `logic [DEPTH-1:0][DATA_W-1:0] data;`，一个 DEPTH×DATA_W 的寄存器数组。因为是普通触发器，对它做组合读不会冒犯块 RAM 的时序模型，所以 FWFT 才能放心实现。

模式切换的**核心只有 6 行**，藏在一个 `always_comb` 里：

[fifo_single_clock_reg_v2.sv:178-194](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/fifo_single_clock_reg_v2.sv#L178-L194) —— 这是整个模块的"灵魂"。我们逐段看：

[fifo_single_clock_reg_v2.sv:182-190](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/fifo_single_clock_reg_v2.sv#L182-L190) —— `if (FWFT_MODE == "TRUE")` 时 `r_data = data[r_ptr]`（**组合读**，队头直通）；`else`（normal）时 `r_data = data_buf`（**寄存读**，晚一拍）。`FWFT_MODE` 是字符串参数，编译期二选一，综合后不会留下多余的判断逻辑。

normal 模式用的那个 `data_buf` 是什么？它是一个输出缓冲寄存器：

[fifo_single_clock_reg_v2.sv:117-117](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/fifo_single_clock_reg_v2.sv#L117) —— `logic [DATA_W-1:0] data_buf = '0;`。

它在 `always_ff` 的读分支里被赋值。以"只读"分支为例：

[fifo_single_clock_reg_v2.sv:138-144](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/fifo_single_clock_reg_v2.sv#L138-L144) —— `2'b01`（只读）分支里，`data_buf <= data[r_ptr]` 与 `r_ptr <= inc_ptr(r_ptr)` 在**同一个时钟沿**更新。所以 `data_buf` 拿到的是**更新前**的 `r_ptr` 所指的数据（队头），但要在**下一拍**才出现在 `r_data`——这就是 normal 模式"晚一拍"的物理来源。同时读写分支 `2'b11` 的三处 `data_buf` 赋值（[L142](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/fifo_single_clock_reg_v2.sv#L142)、[L163](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/fifo_single_clock_reg_v2.sv#L163)、[L171](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/fifo_single_clock_reg_v2.sv#L171)）同理。

> 与 u4-l2 的对照：`fifo_single_clock_ram` 的 [INFO 第 17 行](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/fifo_single_clock_ram.sv#L17) 明确写"only 'normal' mode is supported here, no FWFT mode"，它的 `r_data` 直接来自双口 RAM 的 `doutb`（[fifo_single_clock_ram.sv:120-120](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/fifo_single_clock_ram.sv#L120)），块 RAM 的同步读特性天然就是 normal。所以"要 FWFT 就别用块 RAM 那版，用 reg_v2 这版"。

`FWFT_MODE` 参数本身的定义和注释：

[fifo_single_clock_reg_v2.sv:55-68](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/fifo_single_clock_reg_v2.sv#L55-L68) —— 注意它还顺带提供了可选的初始化（`USE_INIT_FILE` / `INIT_CNT`），这是 reg_v2 相对 v1 的"new!"特性，可以在上电时预装若干个字并设好 `cnt`，常用于"开机即有默认配置"的场景。

#### 4.1.4 代码实践

**实践目标**：用同一份激励，肉眼对比 FWFT 与 normal 两种模式下 `r_data` 的时序差。

**操作步骤**：

1. 复制仓库自带的 [fifo_single_clock_reg_v2_tb.sv](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/fifo_single_clock_reg_v2_tb.sv)，改写一个最小版：例化**两个** `fifo_single_clock_reg_v2`，参数完全相同（`DEPTH=8, DATA_W=16`），唯一区别是一个 `.FWFT_MODE("TRUE")`、另一个 `.FWFT_MODE("FALSE")`。
2. 给两者施加**完全相同**的 `w_req`/`w_data`/`r_req`（先写满 8 个递增值，再连续读）。
3. 用 iverilog 编译（需 `-g2012`，并把 `clogb2.svh` 放进 include 路径），dump 出 VCD，用 GTKWave 观察。

**需要观察的现象**：

- normal 那个的 `r_data` 比 `r_req` **晚 1 拍**变化；
- FWFT 那个的 `r_data` 在 `r_req` 拉高**之前**就已经等于队头，`r_req` 拉高当拍数据也立刻有效。

**预期结果**：两者的 `empty`/`full`/`cnt` 行为一致（因为 INFO 声明 v1/v2"operate identically from an outside observer's view"，FWFT 与 normal 的差别仅在读数据通路），但 `r_data` 的对齐相差一拍。**波形确切对齐请待本地验证**。

> 提示：仓库自带的 tb 用 `c_rand` 和 `clk_divider` 做随机激励，依赖较多；最小实践里用确定性的 `initial` 序列即可，更易读。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `fifo_single_clock_reg_v2` 的存储体从触发器阵列换成块 RAM，FWFT 模式还能像现在这样"零延迟"吗？为什么？

> **答案**：不能保持完全相同的零延迟组合读。块 RAM 的读端口是**同步**的（地址当拍给出、数据下拍才出），即便你写 `assign r_data = ram[r_ptr]`，综合工具也会把它推断成带输出寄存的同步读，`r_data` 至少晚一拍——退化成 normal 模式。这正是仓库保留两套 FIFO（块 RAM 版只做 normal，触发器版才做 FWFT）的根本原因。

**练习 2**：`fifo_single_clock_reg_v2` 的 `cnt` 位宽是 `clogb2(DEPTH)+1`（[L60](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/fifo_single_clock_reg_v2.sv#L60)）。结合 u2-l4，为什么必须 +1？

> **答案**：`cnt` 要能表示"0 到 DEPTH"这 DEPTH+1 个值（含"满"状态）。地址位宽 `$clog2(DEPTH)` 只能区分 DEPTH 个表项，装不下 DEPTH 这个值本身；按 u2-l4 的等式，计数位宽应取 `clogb2(DEPTH) == $clog2(DEPTH+1)`，即多一位。

---

### 4.2 预读缓冲：preview_fifo 与 read_ahead_buf

FWFT 解决了"队头一直可见"，但实际工程里我们常常还想更进一步：

- 能不能**一次看 2 个字**，好让下游做"成对处理"或"提前 2 拍决策"？（→ `preview_fifo`）
- 能不能让用户在**同一个 `always_ff` 里既判断 `empty`、又拉 `r_req`、又把 `r_data` 存下来**，而不用拆成两段状态机？（→ `read_ahead_buf`）

这两个模块是"预读 / 提前缓冲"的两种不同实现思路。

#### 4.2.1 概念说明

**`preview_fifo`：一次预览 2 个字**

它对外表现得像一个 **show-ahead（=FWFT）FIFO**，但独特之处在于：每次可以**写 0/1/2 个字**，也可以**读 0/1/2 个字**，并且**输出端同时摆着连续 2 个字**（`od0` 是当前队头，`od1` 是下一个）。读者因此可以"预览"未来两个字而不必先弹出它们。INFO 原文（[preview_fifo.sv:6-13](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/preview_fifo.sv#L6-L13)）就说："The module performs just like an ordinary FIFO in show-ahead mode... 0, 1 or 2 words may be written at once... gives an opportunity for the reader to 'preview' up to 2 future fifo words without actually fetching it yet."

实现秘诀是**把一个 FIFO 拆成两个半容量的子 FIFO**，写和读都在两者之间交替（`wr_ptr`/`rd_ptr` 各 1 位，来回切）。这样 `od0` 来自一个子 FIFO、`od1` 来自另一个，自然就能同时露出相邻两字。

> ⚠️ **厂商依赖**：`preview_fifo` 内部例化了两个 Altera 的 `scfifo` 原语（带 `LPM_*` 参数），只能在 Quartus 里综合；iverilog/Vivado 里没有 `scfifo`，仿真需替换成等价模型。这是它和 `fifo_single_clock_ram`（跨厂商纯 SV）最大的区别。

**`read_ahead_buf：幽灵读缓冲**

它不是 FIFO，而是一个**套在 FWFT FIFO 读口外面的薄壳**。它接管"向底层 FIFO 发 `fifo_r_req`、取 `fifo_r_data`"的全部细节，对用户暴露一组更顺手的读接口。核心机制是一个**"幽灵读"（fantom read）**：当它发现底层 FIFO 刚从空变为非空（`fifo_empty` 下降沿），就**抢在用户开口之前**主动读一次，把队头预先取进自己的缓冲。这样用户看到的 `empty` 一旦落下，`r_data` 上就已经有有效数据了。

INFO（[read_ahead_buf.sv:7-37](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/read_ahead_buf.sv#L7-L37)）总结了它的代价与收益：

- **+1 个字的有效深度**（缓冲里多囤了一个字）；
- **`empty` 撤销多 1 拍延迟**（要等幽灵读把数据取回来才撤空标志）；
- **不动写口和 `full`**（只插在读侧）；
- **把所有组合小动作藏进模块内部**，用户只需面对一组干净的接口；
- 让用户能像下面这样，在**单个 `always_ff`** 里同时控制读请求和接收数据：

```
always_ff @(posedge clk) begin
  if( ~empty )  r_req <= 1'b1;     // 判空 + 发请求
  else          r_req <= 1'b0;
  if( r_req )   new_data <= r_data; // 同一拍就能拿到数据
end
```

#### 4.2.2 核心流程

**`preview_fifo` 的读写交替流程**（以"写一个字"为例）：

```
wrreq=3'b010 (写 1 字):
  若 wr_ptr=0: 写进 internal_fifo0 (若 w0_valid, 即 fifo0 未满)
  若 wr_ptr=1: 写进 internal_fifo1 (若 w1_valid)
  每成功写 1 次, wr_ptr 翻转 → 下次写到另一个子 FIFO
wrreq=3'b100 (写 2 字): 同时写 fifo0 和 fifo1 (若 w2_valid)
wrreq=3'b001 (不写):   啥也不干
```

读侧（`rdreq`）逻辑对称。因为两个子 FIFO 都开 `LPM_SHOWAHEAD("ON")`（show-ahead），`od0`/`od1` 始终组合可见，`rd_ptr` 决定哪个子 FIFO 的数据是"第一个"、哪个是"第二个"。`empty[1:0]` 是 2 位：`2'b11`=空、`2'b01`/`2'b10`=只有 1 个字可预览、`2'b00`=有 ≥2 个字可预览。

**`read_ahead_buf` 的幽灵读流程**：

```
1. edge_detect 监视 fifo_empty 的下降沿 → fifo_empty_fall
2. fantom_read = fifo_empty_fall && buf_empty      // 缓冲空且底层刚来数据 → 偷偷读
3. normal_read = r_req && ~fifo_empty               // 用户正常读
4. fifo_r_req  = anrst && (fantom_read || normal_read)  // 汇总成对底层 FIFO 的读请求
5. soft_latch 在 (fantom_read || normal_read || 缓冲耗尽) 时锁存 fifo_r_data → r_data
6. 对外 empty   = buf_empty || (r_req && fifo_empty)    // 提前判断空
```

#### 4.2.3 源码精读

**先看 `preview_fifo`。** 两个子 FIFO 的实例和交替指针是它的骨架：

[preview_fifo.sv:81-82](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/preview_fifo.sv#L81-L82) —— `wr_ptr`/`rd_ptr` 各 1 位，注释说"`*_ptr=0` 下次读写走 FIFO0，`=1` 走 FIFO1"。

[preview_fifo.sv:91-100](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/preview_fifo.sv#L91-L100) —— 溢出 / 下溢保护标志。`w2_valid = ~|full[1:0]`（两个子 FIFO 都没满才能一次写 2 字），`r2_valid = ~|empty[1:0]`（两个都非空才能一次读 2 字）。这正是上一讲 u4-l2"overflow/underflow 保护"思想在"批量读写"场景的推广。

[preview_fifo.sv:104-145](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/preview_fifo.sv#L104-L145) —— 写侧组合逻辑：按 `wrreq` 解码，结合 `wr_ptr` 把数据路由到 `f_wrreq[0]`/`f_wrreq[1]` 和 `f_wrdata[]`，并用 `w*_valid` 过滤非法写。

[preview_fifo.sv:229-277](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/preview_fifo.sv#L229-L277) —— 两个 `scfifo` 实例（`internal_fifo0` / `internal_fifo1`），各自容量 `DEPTH/2`，关键参数 `LPM_SHOWAHEAD("ON")`（首字直通）、`UNDERFLOW_CHECKING/OVERFLOW_CHECKING("ON")`、`USE_EAB("ON")`（用嵌入式阵列块）。

[preview_fifo.sv:279-287](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/preview_fifo.sv#L279-L287) —— `usedw`（已用字数）= 两个子 FIFO 的 `usedw` 之和，注意 `full` 时要把子 FIFO 的 usedw 钳位到 `1<<(USED_W-1)`，并把最高位拼回去，所以端口注释特意提醒"attention to the additional MSB"。

**再看 `read_ahead_buf`。** 它复用了 u2-l2 的 `edge_detect` 来抓 `fifo_empty` 的下降沿：

[read_ahead_buf.sv:83-91](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/read_ahead_buf.sv#L83-L91) —— 例化 `edge_detect` 监视 `fifo_empty`，只取 `falling` 输出（即 `fifo_empty_fall`：FIFO 刚刚从空变为非空）。

[read_ahead_buf.sv:97-107](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/read_ahead_buf.sv#L97-L107) —— 缓冲空标志 `buf_empty` 的状态机：幽灵读 (`fantom_read`) 把它清 0（缓冲里囤进了一个字）；当"底层已空且用户还在读" (`fifo_empty && r_req`) 时把它置 1（缓冲被耗尽）。注意这里混用了非阻塞 `<=` 和阻塞 `=`，是为了精细控制当拍/次拍可见性。

[read_ahead_buf.sv:109-117](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/read_ahead_buf.sv#L109-L117) —— 三条核心 `assign`：`fantom_read`（抢先读）、`normal_read`（正常读）、对外 `empty`（`buf_empty || (r_req && fifo_empty)`，提前感知"再读就真没了"）、汇总的 `fifo_r_req`。这 4 行就是整个"提前读"思想的全部。

[read_ahead_buf.sv:133-141](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/read_ahead_buf.sv#L133-L141) —— 数据保持委托给 u3-l3 讲过的 `soft_latch`。`soft_latch` 是"组合数据保持电路"：`latch` 有效时输出直通输入（零延迟），无效时保持上次的值，且**不推断硬件 latch**（[soft_latch.sv:76-84](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/soft_latch.sv#L76-L84) 的 `always_comb` + 一个 `in_buf` 触发器共同实现）。用它来锁存 `fifo_r_data`，既保证了组合可见，又避免了被综合成时序 latch 的警告。

[read_ahead_buf.sv:120-131](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/read_ahead_buf.sv#L120-L131) —— `latch_req` 的产生：幽灵读、正常读、以及"缓冲耗尽"(`fifo_empty && r_req`) 三种情况都要重新锁存数据。

> 一个有趣的细节：仓库自带的 [read_ahead_buf_tb.sv:195-227](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/read_ahead_buf_tb.sv#L195-L227) 用了一个**深度 +1** 的参考 FIFO (`DEPTH(33)`) 来做黄金比对，并在注释里写"buffer adds effective +1 depth"——这正是 INFO 所说"+1 有效深度"的实证。

#### 4.2.4 代码实践

**实践目标**：感受 `preview_fifo` 的"一次预览 2 字"能力（源码阅读型，因依赖 `scfifo`，建议在 Quartus 内完成）。

**操作步骤**：

1. 打开 [preview_fifo_tb.sv](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/preview_fifo_tb.sv)，重点读 [L107-L125](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/preview_fifo_tb.sv#L107-L125)（写请求 `wrreq` 的随机产生：`3'b010` 写 1 字、`3'b100` 写 2 字、`3'b001` 不写）和 [L135-L156](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/preview_fifo_tb.sv#L135-L156)（读请求 `rdreq` 的产生）。
2. 在 Quartus 里编译 `preview_fifo` + 这个 tb，跑仿真，把 `od0`、`od1`、`empty[1:0]`、`usedw` 一起拉进波形。
3. 人眼追踪：当 `usedw >= 2`（即 `empty == 2'b00`）时，确认 `od0` 和 `od1` 恰好是队列里相邻的两个字，且**在没有发 `rdreq` 的拍上它们保持不变**。

**需要观察的现象**：

- `empty` 从 `2'b11` → `2'b10`/`2'b01`（来 1 个字）→ `2'b00`（来 ≥2 个字）的演变；
- `od0` 始终是当前队头；只有发 `rdreq=3'b010` 后下一拍，`od0` 才变成原 `od1`（队头被弹掉，第二个字"掉"到第一位）。

**预期结果**：`od0`/`od1` 像"两扇并排的窗户"，让你同时看到队头和下一个字。**精确波形待本地验证。**

#### 4.2.5 小练习与答案

**练习 1**：`preview_fifo` 为什么要用**两个**子 FIFO 而不是一个？

> **答案**：为了在**同一拍**同时露出两个**连续**的字。单个 show-ahead FIFO 同一拍只能露出队头 1 个字；用两个子 FIFO 交替存放奇偶位置的元素（`wr_ptr` 来回切），就能让 `od0`、`od1` 分别指向相邻两字并组合可见。代价是控制逻辑（交替指针、批量读写保护）变复杂。

**练习 2**：`read_ahead_buf` 的 INFO 说它"adds one cycle latency for empty flag deassertion"。结合源码解释这个延迟从哪来。

> **答案**：底层 FIFO 从空变非空时，`fifo_empty` 下降沿触发 `fantom_read`（[L109](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/read_ahead_buf.sv#L109)），`soft_latch` 要等到底层 FIFO 把数据送到 `fifo_r_data` 才能锁住，`buf_empty` 也才随之清 0（[L97-L107](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/read_ahead_buf.sv#L97-L107)）。对外 `empty` 依赖 `buf_empty`，所以"非空"的可见比底层 FIFO 自身的 `fifo_empty` 晚 1 拍——这就是用"+1 拍空标志延迟"换"+1 字有效深度 + 同拍取数"的代价。

---

### 4.3 多 FIFO 聚合：fifo_combiner 与 fifo_operator

实际系统里很少只有一个 FIFO。典型场景：多路传感器各自往自己的 FIFO 里塞数据，后端要把它们汇成一路；或者两路数据流要做逐元素运算（加、或、拼接）再送进下游。仓库提供了两个对称的"多入一出"模块。

#### 4.3.1 概念说明

两者端口几乎一模一样（多路输入 `r_empty`/`r_req`/`r_data`，单路输出 `w_full`/`w_req`/`w_data`），但**读触发条件**和**数据处理方式**恰好相反：

| | `fifo_combiner`（合并器） | `fifo_operator`（运算器） |
| --- | --- | --- |
| 何时读输入 | **任一**输入非空就读 | **全部**输入非空才读 |
| 选中策略 | 经仲裁器选**一路** | 同时读**所有**路 |
| 输出数据 | 选中路的原始数据（多路选一） | 对所有路数据施加一个运算（默认按位或） |
| 语义 | "OR"：谁有数据先搬谁 | "AND"：大家到齐了一起算 |
| 典型场景 | 多路数据汇聚到一条总线 | 双流逐元素运算（如 `a+b`、`a|b`） |

INFO 对比着看最清楚：
- `fifo_combiner`（[fifo_combiner.sv:7-13](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/fifo_combiner.sv#L7-L13)）："Combines / accumulates data words from multiple FIFOs to a single output FIFO... **Reads if ANY** input FIFO has data."
- `fifo_operator`（[fifo_operator.sv:7-13](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/fifo_operator.sv#L7-L13)）："Performs custom operation on data words from multiple FIFOs... **Reads only if ALL** input FIFOs have data."

二者都支持 `FWFT_MODE` 参数，用来适配上游 FIFO 是 FWFT 还是 normal（决定输出数据要当拍取还是打一拍取）。

#### 4.3.2 核心流程

**`fifo_combiner` 流程**：

```
1. 把各路 r_empty 取反得到"有数据"请求向量 req = ~r_empty
2. 仲裁器 (ROUND_ROBIN / ROUND_ROBIN_PERFORMANCE / PRIORITY) 从 req 里选一路
   → enc_valid (有没有选中), enc_filt (one-hot 选中位), enc_bin (选中路索引)
3. r_valid = enc_valid && ~w_full              // 选中且下游输出 FIFO 没满
4. r_req = {WIDTH{r_valid}} & enc_filt         // 只向选中那一路发读请求
5. FWFT 模式: w_data = r_data[enc_bin], w_req = r_valid        (当拍出)
   normal 模式: 用 r_valid_d1/r_data_d1 打一拍后写到输出 FIFO
```

**`fifo_operator` 流程**（更简单，没有仲裁）：

```
1. r_valid = ~|r_empty && ~w_full              // 所有路都非空且下游没满 (~|r_empty 即"没有一位为1")
2. r_req = {WIDTH{r_valid}}                    // 同时向所有路发读请求
3. FWFT 模式: w_data = operator(r_data), w_req = r_valid        (当拍出)
   normal 模式: 用 r_valid_d1/r_data_d1 打一拍
4. operator() 默认是把所有路按位或; 用户可改成任意运算
```

#### 4.3.3 源码精读

**`fifo_combiner` 的仲裁器选择**是它的核心，用一个 `generate` 在编译期三选一：

[fifo_combiner.sv:74-107](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/fifo_combiner.sv#L74-L107) —— 按 `ENCODER_MODE` 字符串例化 `round_robin_enc`、`round_robin_performance_enc` 或 `priority_enc`（这三个模块在 u6-l2 详讲）。它们的输入都是 `~r_empty`（"哪几路有数据"），输出 `enc_valid`（是否选中了某路）、`enc_filt`（one-hot 选中向量）、`enc_bin`（选中路的二进制索引）。

[fifo_combiner.sv:110-114](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/fifo_combiner.sv#L110-L114) —— 汇聚读请求：`r_valid = enc_valid && ~w_full`（选中且下游不满），`r_req = {WIDTH{r_valid}} && enc_filt`（只向选中路发读）。`{WIDTH{r_valid}}` 是把 `r_valid` 广播成 WIDTH 位再和 one-hot 相与——一个经典的可综合"条件广播"写法。

[fifo_combiner.sv:117-130](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/fifo_combiner.sv#L117-L130) —— normal 模式需要的打一拍缓冲（`r_valid_d1`、`enc_bin_d1`、`r_data_d1`）。因为 normal 模式下上游数据比请求晚一拍，所以索引和数据都要一起延迟一拍才能对齐。

[fifo_combiner.sv:133-169](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/fifo_combiner.sv#L133-L169) —— `generate` 二选一：FWFT 模式用 `r_data[enc_bin]`（当拍多路选一）写输出；normal 模式用 `r_data_d1[enc_bin_d1]`（打一拍）写输出。注意例化模板里那句"`w_full` connect to 'almost_full' if FWFT_MODE='FALSE'"（[L30](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/fifo_combiner.sv#L30)）——normal 模式因为有 1 拍延迟，下游 FIFO 用"将满"阈值更稳妥，避免在响应到达前就溢出。

**`fifo_operator` 的"全部到齐"判断**只有一行，但极其精炼：

[fifo_operator.sv:61-64](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/fifo_operator.sv#L61-L64) —— `r_valid = ~|r_empty && ~w_full`。`|r_empty` 是"归约或"（r_empty 里只要有一位为 1 就为 1），`~|r_empty` 就是"r_empty 全 0 = 所有路都非空"。再用 `r_req = {WIDTH{r_valid}}` 同时读所有路。这是与 combiner "`& enc_filt` 选一路"最本质的区别。

[fifo_operator.sv:119-127](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/fifo_operator.sv#L119-L127) —— `operator` 函数：默认实现是把 WIDTH 路数据**按位或**起来。注释明说"bitwise OR operator, **as an example**"——这是留给你替换的钩子。想算加法？把 `|` 换成 `+`（注意位宽进位）；想算拼接？换成 `{data[0], data[1]}`。整个模块的"运算语义"全由这个函数决定，这也是 INFO 所说"Source code could be easily adapted to apply any operator"的含义。

[fifo_operator.sv:80-116](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/fifo_operator.sv#L80-L116) —— 与 combiner 完全同构的 FWFT/normal 二选一输出路由，只是 `w_data` 换成了 `operator(...)` 的结果。

#### 4.3.4 代码实践

**实践目标**：通过源码阅读 + 改一行，体会 combiner 与 operator 的对称性。

**操作步骤**：

1. 把 [fifo_combiner.sv](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/fifo_combiner.sv) 和 [fifo_operator.sv](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/fifo_operator.sv) 并排打开，逐行对比端口声明——你会发现两者**完全一致**。
2. 对比 [fifo_combiner.sv:110-114](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/fifo_combiner.sv#L110-L114) 与 [fifo_operator.sv:61-64](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/fifo_operator.sv#L61-L64)：前者依赖仲裁器选出 `enc_filt` 再 `&` 上去（选一路），后者用 `~|r_empty` 直接判断全体到齐（读全部）。
3. **改一行实验**：复制 `fifo_operator.sv` 为 `fifo_adder.sv`（仅作练习，不修改原文件），把 [L125](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/fifo_operator.sv#L125) 的 `operator[DATA_W-1:0] = operator[DATA_W-1:0] | data[i];` 改成累加 `operator[DATA_W-1:0] = operator[DATA_W-1:0] + data[i];`，于是它变成"2 路输入逐元素相加"的流式加法器。

**需要观察的现象**：combiner 的输出是"某一路的原值"（多路选一）；改成加法后的 operator 输出是"两路之和"（逐元素运算）。

**预期结果**：两路输入分别给 `3` 和 `5`，combiner 输出要么是 `3` 要么是 `5`（看仲裁选中谁），而改后的 operator 输出恒为 `8`。**待本地验证。**

#### 4.3.5 小练习与答案

**练习 1**：`fifo_combiner` 例化模板建议 normal 模式下把 `w_full` 接到下游 FIFO 的 `almost_full` 而不是 `full`（[L30](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/fifo_combiner.sv#L30)）。为什么？

> **答案**：normal 模式下读数据比读请求晚 1 拍（[L117-L130](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/fifo_combiner.sv#L117-L130) 的 `r_valid_d1`/`r_data_d1`）。如果等下游 FIFO 真正 `full` 才停止读，那么已经在飞行中的这一拍数据写到下游时就会溢出。接到 `almost_full`（提前一两拍预警）可以覆盖这 1 拍的"在途数据"，避免溢出。FWFT 模式没有这 1 拍延迟，所以可以直接接 `full`。

**练习 2**：`fifo_operator` 里 `r_valid = ~|r_empty && ~w_full`（[L62](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/fifo_operator.sv#L62)）。如果有一路输入长期没数据，会出现什么现象？怎么解决？

> **答案**：只要有一路空，`r_valid` 就一直为 0，**所有路都不会被读**，其他路的数据会积压在各自 FIFO 里甚至填满（"木桶效应"——最慢的一路决定整体吞吐）。解决思路：保证各路数据速率匹配，或在 `operator` 里为缺失的那一路提供默认值（如 `0`）并放宽 `r_valid` 判据——但那会改变"全部到齐才算"的语义，需根据业务权衡。

---

## 5. 综合实践

把本讲三个模块串起来，完成规格里给定的核心任务：**用 `read_ahead_buf` 包裹 `fifo_single_clock_ram`，造一个"读请求当拍即出数据"的 FWFT 风格 FIFO，并用 testbench 验证时序。**

### 5.1 实践目标

- 体会 `read_ahead_buf` 是怎样"插在读侧"把一个 normal 模式 FIFO（u4-l2 的 `fifo_single_clock_ram`，块 RAM 同步读、数据晚一拍）包装成"用户侧同拍取数"的接口的；
- 完成一次端到端的仿真，写出观察到的时序。

### 5.2 系统连线

数据流方向：**用户 → `read_ahead_buf`（缓冲壳）→ `fifo_single_clock_ram`（底层存储）**。

```
              ┌─────────────────────────────┐
   user  ────►│ read_ahead_buf              │
  r_req       │  fifo_r_req  ────────► w_req│──► (内部) fifo 单 clock RAM 的写口由用户直接驱动
  r_data ◄────│  r_data     ◄──── r_data    │
  empty ◄────│  empty                       │
              └─────────────────────────────┘
                        │ fifo_r_req / fifo_r_data / fifo_empty
                        ▼
              ┌─────────────────────────────┐
              │ fifo_single_clock_ram        │  (写口: 用户直接 w_req/w_data)
              │  读口 r_req ◄── fifo_r_req   │
              │       r_data ──► fifo_r_data │
              │       empty  ──► fifo_empty  │
              └─────────────────────────────┘
```

注意分工：`read_ahead_buf` 只接管**读侧**（`fifo_r_req`/`fifo_r_data`/`fifo_empty`），**写侧**（`w_req`/`w_data`）和 `full` 由用户直接连到 `fifo_single_clock_ram`，缓冲不介入（这与 INFO 的"does not touch fifo write port and full flag"一致）。

### 5.3 操作步骤

1. **准备源文件**（均为纯 SV，iverilog 可跑）：`clogb2.svh`、`true_dual_port_write_first_2_clock_ram.sv`、`fifo_single_clock_ram.sv`、`edge_detect.sv`、`soft_latch.sv`、`read_ahead_buf.sv`，外加你新写的 `fwft_wrap_tb.sv`。
2. **写一个最小 testbench**（示例代码，非仓库原有）：

```systemverilog
`timescale 1ns / 1ps
module fwft_wrap_tb();
  logic clk = 0;  always #2.5 clk = ~clk;
  logic anrst = 1'b1;

  // ---- 底层 normal-mode FIFO 的写侧 (用户直接驱动) ----
  logic w_req;  logic [15:0] w_data;
  // ---- 底层 FIFO 的读侧 (交给 read_ahead_buf) ----
  logic fifo_r_req;  logic [15:0] fifo_r_data;  logic fifo_empty;
  // ---- 对外 (用户侧) 读接口 ----
  logic r_req = 1'b0;  logic [15:0] r_data;  logic empty;

  fifo_single_clock_ram #(
    .DEPTH(8), .DATA_W(16)
  ) ff (
    .clk(clk), .nrst(anrst),
    .w_req(w_req),  .w_data(w_data),
    .r_req(fifo_r_req), .r_data(fifo_r_data),
    .cnt(), .empty(fifo_empty), .full(), .fail()
  );

  read_ahead_buf #(.DATA_W(16)) rb (
    .clk(clk), .anrst(anrst),
    .fifo_r_req(fifo_r_req), .fifo_r_data(fifo_r_data), .fifo_empty(fifo_empty),
    .r_req(r_req), .r_data(r_data), .empty(empty)
  );

  // ---- 激励: 先写 4 个字, 再读 4 个字 ----
  initial begin
    w_req = 0;
    // 写 4 个字
    for (int i = 1; i <= 4; i = i + 1) begin
      @(posedge clk); w_req = 1; w_data = i * 16'h11;
    end
    @(posedge clk); w_req = 0;
    // 等缓冲完成幽灵读
    repeat (3) @(posedge clk);
    // 读 4 个字 (用户侧: 看到非空就读, 当拍就拿数据)
    for (int i = 0; i < 4; i = i + 1) begin
      @(posedge clk); r_req = ~empty;
      if (r_req) $display("[%0t] r_data = %h", $time, r_data);
    end
    @(posedge clk); r_req = 0;
    repeat (5) @(posedge clk);
    $finish;
  end
endmodule
```

3. **编译运行**：`iverilog -g2012 -o sim -I . clogb2.svh true_dual_port_write_first_2_clock_ram.sv fifo_single_clock_ram.sv edge_detect.sv soft_latch.sv read_ahead_buf.sv fwft_wrap_tb.sv && vvp sim`（include 路径按实际位置调整）。加 `$dumpfile/$dumpvars` 可在 GTKWave 里看波形。

### 5.4 需要观察的现象

- 写入 `0x11, 0x22, 0x33, 0x44` 后，底层 `fifo_empty` 一旦落下，`read_ahead_buf` 会自动发一次"幽灵读"，随后对外 `empty` 也落下，`r_data` 上已经摆好队头 `0x11`。
- 用户拉高 `r_req` 的**当拍**，`r_data` 就是有效数据（FWFT 风格），而不是像裸 `fifo_single_clock_ram` 那样要等下一拍。
- 整条链的有效深度比裸 FIFO 多 1 个字（缓冲里囤了一个）。

### 5.5 预期结果

`$display` 应按顺序打印 `0x11 → 0x22 → 0x33 → 0x44`。若顺序错乱或读出为 `0`，说明读侧握手时序未对齐——回头检查 `r_req` 与 `empty` 的相对节拍。

> ⚠️ **重要说明（待本地验证）**：`read_ahead_buf` 的 INFO 与官方 testbench（[read_ahead_buf_tb.sv:124-132](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/read_ahead_buf_tb.sv#L124-L132)）都是把它接在 **FWFT 模式**的 FIFO（`fifo_single_clock_reg_v1, FWFT_MODE("TRUE")`）上。本实践按要求接在 **normal 模式**的 `fifo_single_clock_ram` 上，由于底层读数据本身有 1 拍延迟，幽灵读的捕获节拍会与官方配置不同——这**正是本实践要你亲手仿真确认的部分**。如果发现 normal 模式下首字对齐差一拍，可改用 `fifo_single_clock_reg_v2` 的 `FWFT_MODE("TRUE")` 作为底层重做一遍对照，体会两者差异。

## 6. 本讲小结

- **FWFT（首字直通）= 组合读队头**：`fifo_single_clock_reg_v2` 用 `always_comb` 里 `FWFT_MODE` 字符串参数在"组合读 `data[r_ptr]`"与"寄存读 `data_buf`"间二选一；要 FWFT 就用触发器存储版，块 RAM 版（u4-l2）只能 normal。
- **`preview_fifo` 用两个子 FIFO 实现"一次预览 2 字"**：靠 `wr_ptr`/`rd_ptr` 交替、`LPM_SHOWAHEAD` 的两个 `scfifo`，把相邻两字同时露出；依赖 Altera IP，仅限 Quartus。
- **`read_ahead_buf` 用"幽灵读"把队头提前取进缓冲**：监听 `fifo_empty` 下降沿抢先读一次，配合 `soft_latch` 锁存，让用户能在单个 `always_ff` 里同拍发请求 + 取数据，代价是 +1 字深度、+1 拍空标志延迟。
- **`fifo_combiner` = 任一非空即读 + 仲裁选一路**：通过 `ENCODER_MODE` 在轮询 / 轮询性能版 / 固定优先级间切换，把多路数据汇聚到一条输出。
- **`fifo_operator` = 全部非空才读 + 施加运算**：`~|r_empty` 判断全体到齐，`operator` 函数（默认按位或，可改）决定运算语义，是流式逐元素运算的骨架。
- 二者都靠 `FWFT_MODE` 参数适配上游 FIFO 的读时序：normal 模式要把索引/数据打一拍对齐，且下游宜用 `almost_full` 防溢出。

## 7. 下一步学习建议

- **横向对比**：把本讲的 `fifo_single_clock_reg_v2`（FWFT/normal 可切）、u4-l2 的 `fifo_single_clock_ram`（块 RAM、仅 normal）、以及仓库里的 `fifo_single_clock_reg_v1`、`lifo.sv`（栈）放在一起读，建立"同一类数据结构在不同存储体/不同模式下的取舍"的全景。
- **进入协议层**：FIFO 是几乎所有串行通信的缓冲基础。下一单元 u5（通信协议 IP）里的 UART、SPI、AXI-Stream 都会用到本讲的概念——尤其 `axis_if` 的 `tvalid/tready` 握手本质上就是一个"FWFT 风格"的流接口，`fifo_combiner` 的仲裁思想会在多主 AXI 互联里再次出现。
- **仲裁器深读**：本讲把 `priority_enc`/`round_robin_enc` 当黑盒用了，它们的实现细节（固定优先级编码、轮询仲裁、性能优化版）在 u6-l2 专门讲解，学完后回头重读 `fifo_combiner` 的 [L74-L107](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/fifo_combiner.sv#L74-L107) 会有更深的体会。
- **综合实战**：u7-l4 会把 `fifo_single_clock_ram`、`debounce_v2`、`edge_detect`、`uart_tx` 串成一个完整的"按键 → FIFO → 串口上报"系统，本讲的 FIFO 知识是那条数据通路的核心缓存环节。

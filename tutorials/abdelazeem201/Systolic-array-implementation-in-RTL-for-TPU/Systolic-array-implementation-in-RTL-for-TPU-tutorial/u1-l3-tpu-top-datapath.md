# 顶层模块 tpu_top 与系统级数据流

> 承接上一篇《u1-l2 仓库目录结构与源码组织》：我们已经知道 `rtl/tpu_top.v` 是可综合的权威顶层，它例化了 `systolic`、`systolic_controll`、`addr_sel`、`quantize`、`write_out` 五个子模块。本篇就把这层"外壳"彻底拆开——看清楚每个端口、每根内部连线到底把谁和谁接起来，数据从 SRAM 读入到结果写回经历了怎样一条完整通路。

## 1. 本讲目标

学完本讲，你应当能够：

- 准确说出 `tpu_top` 的全部端口（控制、读数据、读地址、三组写回、完成信号）及其位宽；
- 解释"8 路 weight / data"在物理上其实是 **4 个 32bit SRAM 读端口、每侧 8 个 8bit 字节通道**这一事实；
- 把 `ORI_WIDTH = DATA_WIDTH+DATA_WIDTH+5` 这条 localparam 推导清楚；
- 画出五个子模块之间的 wire 连接图，指出每根信号由谁产生、由谁消费；
- 用"控制器 → 地址 → 阵列 → 量化 → 写回"这条主线描述 TPU 的系统级数据流。

## 2. 前置知识

在进入源码前，先建立三个直觉：

1. **结构化建模（structural modeling）**：Verilog 模块可以不写任何 `always`、不做任何运算，只负责"把别的模块像搭积木一样连起来"。这种模块本身不含逻辑，它的价值在于**声明端口 + 连线 + 例化**。`tpu_top` 就是这种纯结构化顶层——它一个 `assign`、一个 `always` 都没有。

2. **端口（port）与连线（wire）的区别**：`input`/`output` 是模块**对外**的边界；`wire` 是模块**内部**把各个子模块端口连起来的导线。读源码时，先认清"这根信号是穿墙进来的端口，还是只在屋里走的连线"。

3. **位宽会传染**：参数 `ARRAY_SIZE=8`、`DATA_WIDTH=8` 会被传给子模块，也会参与计算顶层端口与连线的位宽（如 `ARRAY_SIZE*OUTPUT_DATA_WIDTH`）。改一个参数，一串位宽会跟着变——这就是参数化设计的好处。

> 一个容易踩的坑：源码注释里写着 `//input data for (data, weight) from eight SRAM`，看起来像"8 块 weight SRAM + 8 块 data SRAM"。但实际端口只有 **2 路 weight（w0/w1）+ 2 路 data（d0/d1）= 4 个读端口**。原因在 [addr_sel.v](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/addr_sel.v#L9-L15) 与 [systolic.v](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/systolic.v#L56-L59) 中揭晓：每路 32bit SRAM 装 4 个 8bit 字节通道，2 路 × 4 = 8 通道。所以"eight"指的是**8 个字节通道（8 列）**，不是 8 块物理 SRAM。文档与代码冲突时，以代码为准。

## 3. 本讲源码地图

本讲只精读一个文件，但会引用另外两个文件来佐证数据流方向：

| 文件 | 作用 | 本讲用到什么 |
|------|------|--------------|
| [rtl/tpu_top.v](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/tpu_top.v) | 顶层模块，纯结构化连线 | 全部内容（端口、localparam、wire、五处例化） |
| [rtl/addr_sel.v](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/addr_sel.v) | 读地址解码（下一篇层精讲） | 仅看其端口注释，佐证 w0/w1 对应 queue 0~3 / 4~7 |
| [rtl/systolic.v](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/systolic.v) | 8×8 脉动阵列本体（u2 精讲） | 仅看它如何把 32bit 读数据切成 8 个 8bit 通道 |

## 4. 核心概念与源码讲解

### 4.1 tpu_top 的参数与端口声明

#### 4.1.1 概念说明

`tpu_top` 是整个加速器对外的"黑盒接口"：外部（通常是 testbench 或 SoC 总线）只看得到一组控制信号、读数据/读地址端口和写回端口。它对外隐藏了内部五个子模块的复杂性。模块头用 4 个 `parameter` 把设计参数化，让同一个 RTL 既能配成 8×8，也能放大到 32×32（详见 u1-l4）。

#### 4.1.2 核心流程

端口按职责分成五组：

1. **控制组**：`clk`、`srstn`（低有效同步复位）、`tpu_start`（启动一次矩阵乘）；
2. **读数据组**：4 路 32bit SRAM 读数据（weight 用 w0/w1，data 用 d0/d1）；
3. **读地址组**：4 路 10bit SRAM 读地址（由内部 `addr_sel` 产生，送出给外部 SRAM）；
4. **写回组**：a/b/c 三组输出 SRAM，每组含 1bit 写使能、128bit 写数据、6bit 写地址；
5. **完成信号**：`tpu_done`。

注意一个关键时序关系：**读地址是 TPU 输出给 SRAM 的，读数据是 SRAM 返回给 TPU 的**——二者方向相反，构成一次完整的"地址→数据"SRAM 读事务。

#### 4.1.3 源码精读

模块头与 4 个参数：

[rtl/tpu_top.v:1-6](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/tpu_top.v#L1-L6) —— 声明 `ARRAY_SIZE=8`、`SRAM_DATA_WIDTH=32`、`DATA_WIDTH=8`、`OUTPUT_DATA_WIDTH=16`，这 4 个值贯穿全设计。

控制与读数据端口：

[rtl/tpu_top.v:8-17](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/tpu_top.v#L8-L17) —— `clk/srstn/tpu_start` 是控制三件套；`sram_rdata_w0/w1/d0/d1` 各 32bit，是 4 块外部 SRAM 返回的读数据。

读地址端口（注意方向是 `output`）：

[rtl/tpu_top.v:19-24](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/tpu_top.v#L19-L24) —— 4 路 10bit 读地址，送给外部 SRAM 去取数。

写回端口（a/b/c 三组结构完全相同）：

[rtl/tpu_top.v:26-37](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/tpu_top.v#L26-L37) —— 每组含写使能（1bit）、写数据（`ARRAY_SIZE*OUTPUT_DATA_WIDTH = 8*16 = 128bit`）、写地址（6bit）。注释 `//write to three SRAN for comparison`（SRAN 是 SRAM 的笔误，"for comparison" 指供 testbench 与 golden 比对）。

完成信号：

[rtl/tpu_top.v:39](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/tpu_top.v#L39) —— `tpu_done` 在一批矩阵乘全部写回后拉高。

> **位宽速查**：读数据每路 32bit；写数据每组 128bit（=8×16）；读地址 10bit；写地址 6bit。

#### 4.1.4 代码实践

**实践目标**：把抽象的端口表变成一张可核对的"端口清单"。

**操作步骤**：

1. 打开 [rtl/tpu_top.v:1-39](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/tpu_top.v#L1-L39)；
2. 用一张表逐行抄录每个端口的：名称、方向（input/output）、位宽、所属分组（控制/读数据/读地址/写回/完成）；
3. 对位宽含表达式的端口（如 `ARRAY_SIZE*OUTPUT_DATA_WIDTH-1:0`），代入默认参数手算出具体位数。

**需要观察的现象**：你会发现 4 个读数据端口与 4 个读地址端口一一对应（w0↔w0、w1↔w1、d0↔d0、d1↔d1），而写回端口的命名却带了个"0"后缀（`sram_write_enable_a0`）。

**预期结果**：得到一张 16 行左右的端口表；其中写数据位宽代入后应为 128bit。

**待本地验证**：若你用 Verilog 工具（如 `iverilog` 或 verdi）打开，可对照端口面板确认位宽与上表一致。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `OUTPUT_DATA_WIDTH` 从 16 改成 8，`sram_wdata_a/b/c` 的位宽会变成多少？

> **参考答案**：`ARRAY_SIZE*OUTPUT_DATA_WIDTH = 8*8 = 64bit`。

**练习 2**：端口注释写"from eight SRAM"，但代码里只有 4 个读数据端口。请用一句话解释这个矛盾。

> **参考答案**：每路 32bit SRAM 含 4 个 8bit 字节通道，weight 侧 w0/w1 共 8 个通道（data 同理），所以"eight"指 8 个字节通道而非 8 块物理 SRAM。

---

### 4.2 内部连线 wire 与 localparam ORI_WIDTH

#### 4.2.1 概念说明

`tpu_top` 内部没有任何运算逻辑，它的"灵魂"是那些 `wire`。这些 wire 是五个子模块之间通信的总线：控制器发的命令、阵列算出的结果、量化后的数据，全靠这些连线传递。理解 `tpu_top` 的本质，就是搞清楚**每根 wire 由谁驱动（生产者）、被谁采样（消费者）**。

唯一的一处"计算"是 `localparam ORI_WIDTH`，它把乘加中间结果的位宽固定下来，供 `ori_data` 连线使用。

#### 4.2.2 核心流程

把内部连线按数据流方向归类，可以得到下面这张"信号总线表"：

| 内部 wire | 位宽 | 生产者（谁输出） | 消费者（谁接收） | 含义 |
|-----------|------|------------------|------------------|------|
| `addr_serial_num` | 7bit | `systolic_controll` | `addr_sel` | 单一地址序号，解码出 4 路读地址 |
| `alu_start` | 1bit | `systolic_controll` | `systolic` | 启动 MAC 与移位的使能 |
| `cycle_num` | 9bit | `systolic_controll` | `systolic` | 当前计算节拍，控制累加分支 |
| `matrix_index` | 6bit | `systolic_controll` | `systolic`、`write_out` | 当前输出矩阵索引，决定取哪条反对角线、写哪个地址 |
| `data_set` | 2bit | `systolic_controll` | `write_out` | 当前是第几批数据，决定写 a 还是 b/c |
| `sram_write_enable` | 1bit | `systolic_controll` | `write_out` | 结果有效的写窗口 |
| `ori_data` | 168bit | `systolic`（`mul_outcome`） | `quantize` | 8 个 21bit 乘加结果打包 |
| `quantized_data` | 128bit | `quantize` | `write_out` | 8 个 16bit 量化结果打包 |

位宽推导（默认参数）：

\[
\text{ORI\_WIDTH} = \text{DATA\_WIDTH} + \text{DATA\_WIDTH} + 5 = 8 + 8 + 5 = 21
\]

\[
\text{ori\_data 宽度} = \text{ARRAY\_SIZE} \times \text{ORI\_WIDTH} = 8 \times 21 = 168\text{bit}
\]

\[
\text{quantized\_data 宽度} = \text{ARRAY\_SIZE} \times \text{OUTPUT\_DATA\_WIDTH} = 8 \times 16 = 128\text{bit}
\]

#### 4.2.3 源码精读

唯一的 localparam：

[rtl/tpu_top.v:41](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/tpu_top.v#L41) —— `ORI_WIDTH = DATA_WIDTH+DATA_WIDTH+5`。两个 8bit 相乘得 16bit，再留 5bit 保护位用于累加不溢出，共 21bit。

控制器侧命令总线（`addr_serial_num` 等）：

[rtl/tpu_top.v:43-57](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/tpu_top.v#L43-L57) —— 这一段集中声明了由 `systolic_controll` 驱动、供其它模块消费的命令信号，注释里把它们按"谁的 parameter"分组（addr_sel / systolic / systolic_controll / write_out）。

计算结果总线（`ori_data`、`quantized_data`）：

[rtl/tpu_top.v:46-48](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/tpu_top.v#L46-L48) —— 注意二者都声明为 `wire signed`，因为定点乘加结果与量化结果都是有符号数。

#### 4.2.4 代码实践

**实践目标**：验证"位宽会传染"——改参数后连线位宽是否真的跟随。

**操作步骤**：

1. 在 [rtl/tpu_top.v:41](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/tpu_top.v#L41) 处把 `ORI_WIDTH` 公式中的 `5` 想象成 `7`（仅心算，不修改源码）；
2. 推算新的 `ORI_WIDTH`、`ori_data` 总宽；
3. 再设想 `ARRAY_SIZE` 改为 16，推算 `ori_data`、`quantized_data`、`sram_wdata_a` 的新位宽。

**需要观察的现象**：哪些连线位宽依赖 `ARRAY_SIZE`、哪些依赖 `DATA_WIDTH`、哪些同时依赖二者。

**预期结果**：`ORI_WIDTH` 仅依赖 `DATA_WIDTH`；`ori_data` 同时依赖 `ARRAY_SIZE` 与 `DATA_WIDTH`；`quantized_data` 与 `sram_wdata_a/b/c` 依赖 `ARRAY_SIZE` 与 `OUTPUT_DATA_WIDTH`。

> 本实践为源码阅读型，无需运行；若你想跑通，可在 u1-l5 的仿真环境里改参数后观察综合/仿真是否报位宽不匹配。

#### 4.2.5 小练习与答案

**练习 1**：`cycle_num` 是 9bit，它最多能表示多少个节拍？为什么需要这么多？

> **参考答案**：9bit 最多表示 512 个节拍。脉动阵列在进入、稳态、排空阶段需要逐拍推进，加上多批矩阵乘的回绕，节拍数远大于阵列尺寸 8，因此预留了较宽的计数器（具体用法见 u2-l2、u3-l1）。

**练习 2**：`ori_data` 为什么必须是 `signed`？如果去掉 `signed` 会怎样？

> **参考答案**：输入是 8bit 有符号定点，乘加结果可能为负。`signed` 让后续 `quantize`（u3-l4）能正确做符号扩展与饱和判断；若去掉，负数会被当成大正数，量化结果全错。

---

### 4.3 五处模块例化与系统级数据流

#### 4.3.1 概念说明

有了端口和连线，最后一步就是"把子模块接上去"。`tpu_top` 例化了 5 个子模块，按例化顺序是 `addr_sel`、`quantize`、`systolic`、`systolic_controll`、`write_out`。例化的本质是：**把内部 wire 与子模块的端口一一对接**（Verilog 里叫命名端口连接 `.port_name(wire_name)`）。

把五处例化连起来，就得到了 TPU 的系统级数据流主线：

```
systolic_controll (主控状态机)
   │  发出: addr_serial_num, alu_start, cycle_num,
   │        matrix_index, data_set, sram_write_enable, tpu_done
   ▼
addr_sel  ──► (4 路 sram_raddr 送出) ──► 外部 SRAM
                                            │ 读数据返回
                                            ▼
systolic  ◄── (4 路 sram_rdata_w0/w1/d0/d1)
   │  产出: mul_outcome (= ori_data, 168bit)
   ▼
quantize  ──► quantized_data (128bit)
   │
   ▼
write_out ──► (a/b/c 三组写使能/写数据/写地址) ──► 外部输出 SRAM
```

一句话总结：**控制器指挥一切 → 地址喂入 → 阵列计算 → 量化 → 写回**。

#### 4.3.2 核心流程

五处例化与它们各自的端口对接关系：

1. **`addr_sel`**（[rtl/tpu_top.v:64-77](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/tpu_top.v#L64-L77)）：输入 `clk`、`addr_serial_num`（来自控制器）；输出 4 路读地址 `sram_raddr_w0/w1/d0/d1`（直通到顶层 output 端口）。
2. **`quantize`**（[rtl/tpu_top.v:79-92](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/tpu_top.v#L79-L92)）：接收 4 个参数；输入 `ori_data`（来自 systolic）；输出 `quantized_data`。纯组合，无 clk。
3. **`systolic`**（[rtl/tpu_top.v:94-117](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/tpu_top.v#L94-L117)）：接收 3 个参数；输入 `clk/srstn/alu_start/cycle_num`（控制器）、4 路读数据（顶层 input）、`matrix_index`（控制器）；输出 `mul_outcome`，对接到内部 wire `ori_data`。
4. **`systolic_controll`**（[rtl/tpu_top.v:119-137](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/tpu_top.v#L119-L137)）：接收 `ARRAY_SIZE`；输入 `clk/srstn/tpu_start`；输出全部控制命令与 `tpu_done`。它是整条通路的"总指挥"。
5. **`write_out`**（[rtl/tpu_top.v:139-165](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/tpu_top.v#L139-L165)）：接收 2 个参数；输入 `clk/srstn`、`sram_write_enable/data_set/matrix_index`（控制器）、`quantized_data`（quantize）；输出 a/b/c 三组写回信号（直通到顶层 output 端口）。

#### 4.3.3 源码精读

**例化 1：addr_sel**——把单一序号解码成 4 路读地址：

[rtl/tpu_top.v:64-77](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/tpu_top.v#L64-L77) —— `.addr_serial_num(addr_serial_num)` 接收控制器发来的序号；`.sram_raddr_*` 直接接到顶层 output，送给外部 SRAM。

**例化 2：quantize**——把 21bit 饱和量化成 16bit：

[rtl/tpu_top.v:79-92](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/tpu_top.v#L79-L92) —— 注意 4 个参数都被显式传递，保持位宽一致；`.ori_data(ori_data)` 与 `.quantized_data(quantized_data)` 把计算与后处理串起来。

**例化 3：systolic**——消费读数据、产出乘加结果：

[rtl/tpu_top.v:94-117](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/tpu_top.v#L94-L117) —— 这里把顶层 input 的 4 路读数据 `.sram_rdata_w0/w1/d0/d1(...)` 喂给阵列；输出端口名是 `mul_outcome`，但顶层用 `.mul_outcome(ori_data)` 把它接到内部 wire `ori_data` 上——**子模块端口名与连线名可以不同，靠 `.port(wire)` 对接**。

> 佐证"8 通道"：[rtl/systolic.v:56-59](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/systolic.v#L56-L59) 中，`weight_queue[0][i]`（i=0~3）取自 w0 的 4 个字节，`weight_queue[0][i+4]`（i=0~3）取自 w1 的 4 个字节，共填满第 0 行的 8 个 weight 通道。

**例化 4：systolic_controll**——总指挥，输出全套控制信号：

[rtl/tpu_top.v:119-137](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/tpu_top.v#L119-L137) —— 它是 `addr_serial_num/alu_start/cycle_num/matrix_index/data_set/sram_write_enable` 这 6 条命令的唯一生产者，并产出对外的 `tpu_done`。

**例化 5：write_out**——把量化结果按反对角线重排写回：

[rtl/tpu_top.v:139-165](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/tpu_top.v#L139-L165) —— 输入端把控制器命令（`sram_write_enable/data_set/matrix_index`）与量化数据（`quantized_data`）一并接上；输出端 a/b/c 三组写回信号直通顶层 output。

#### 4.3.4 代码实践（本讲主实践）

**实践目标**：依据 `tpu_top.v` 画出一张完整的顶层框图，把控制、读入、写回三条路径与每根 wire 的位宽都标清楚。

**操作步骤**：

1. 在纸上画 6 个方框：1 个 `tpu_top`（外壳，画成大框，内部含 5 个小框）；
2. 在 `tpu_top` 内部画出 5 个子模块方框：`systolic_controll`（放最上，标注"总指挥"）、`addr_sel`、`systolic`、`quantize`、`write_out`；
3. **标控制路径**：从外部画 `clk`/`srstn` 进入并连到所有时序模块；`tpu_start` 只进 `systolic_controll`；`tpu_done` 从 `systolic_controll` 出去；
4. **标读入路径**：`systolic_controll →(addr_serial_num[7])→ addr_sel →(sram_raddr_*[10])→` 穿出顶层到外部 SRAM；外部 SRAM `→(sram_rdata_w0/w1/d0/d1[32])→` 穿进顶层到 `systolic`；别忘了 `alu_start`、`cycle_num[9]`、`matrix_index[6]` 从控制器到 `systolic`；
5. **标计算路径**：`systolic →(ori_data[168])→ quantize →(quantized_data[128])→ write_out`；
6. **标写回路径**：`systolic_controll →(sram_write_enable/data_set[2]/matrix_index[6])→ write_out →(sram_wdata_*[128]/sram_waddr_*[6]/sram_write_enable_*[1])→` 穿出顶层到外部输出 SRAM；
7. 在每根线上标注位宽。

**需要观察的现象**：画完你会发现——`matrix_index` 这根线**同时**连到 `systolic` 和 `write_out`（一生产者两消费者）；而 4 路读数据与 4 路读地址构成"出去取地址、进来拿数据"的闭环。

**预期结果**：一张标注完整位宽的框图，清晰呈现"控制器 → 地址 → SRAM → 阵列 → 量化 → 写回"的主线，与 4.3.2 的文字流程一致。

**待本地验证**：若你用 draw.io / Excalidraw 画好，可对照 [rtl/tpu_top.v:64-165](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/tpu_top.v#L64-L165) 逐根线核对接线是否遗漏。

#### 4.3.5 小练习与答案

**练习 1**：`matrix_index` 由谁驱动？被谁消费？为什么它要同时送到两个模块？

> **参考答案**：由 `systolic_controll` 驱动，同时被 `systolic` 和 `write_out` 消费。`systolic` 用它决定从 8×8 结果矩阵里取哪条反对角线拼成 `mul_outcome`（见 u2-l3）；`write_out` 用它决定把量化结果写进哪一行/哪个地址（见 u3-l3）。二者必须用同一个索引，才能保证"算出来的"和"写出去的"是同一批结果。

**练习 2**：如果把 `addr_sel` 这处例化整段删掉（假设），顶层哪些 output 端口会变成悬空（无驱动）？

> **参考答案**：`sram_raddr_w0/w1/d0/d1` 这 4 路 10bit 读地址会悬空，外部 SRAM 收不到读地址，整条读入通路瘫痪，`systolic` 拿不到有效的 `sram_rdata`，结果全错。

**练习 3**：`systolic` 的输出端口叫 `mul_outcome`，而顶层内部连线叫 `ori_data`，二者名字不同却连在一起，这合法吗？

> **参考答案**：合法。Verilog 命名端口连接 `.mul_outcome(ori_data)` 表示"把子模块的 `mul_outcome` 端口接到模块内的 `ori_data` 这根 wire 上"，端口名与连线名无需相同。

## 5. 综合实践

**任务**：扮演一次"顶层集成者"——在不改动任何子模块的前提下，仅依据 [rtl/tpu_top.v](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/tpu_top.v) 写一份《tpu_top 集成说明》文档，要求包含：

1. **端口规格表**：列出全部 input/output，含方向、位宽（代入默认参数后的具体位数）、分组、一句话作用；
2. **内部总线表**：列出 8 条核心内部 wire，含位宽、生产者、消费者；
3. **数据流时序叙述**：用一段话描述从 `tpu_start` 拉高到 `tpu_done` 拉高之间，控制信号、读地址、读数据、计算结果、量化结果、写回信号是如何依次流动的（可参考 4.3.2 的流程图，但要用你自己的话写）；
4. **一处矛盾澄清**：用你的话解释"eight SRAM 注释 vs 4 个物理读端口"这一矛盾。

**验收标准**：把你的文档拿给一个没读过源码的同学，他能否仅凭这份文档就画出与本讲 4.3.4 一致的框图。如果能，说明你真正理解了 `tpu_top` 的接线。

> 本实践不需要运行任何工具，是典型的源码阅读 + 文档化实践。完成后可对照 u1-l5 的 testbench 看这些端口在实际仿真中如何被驱动。

## 6. 本讲小结

- `tpu_top` 是**纯结构化顶层**：没有 `always`/`assign` 逻辑，只做参数声明、端口声明、wire 声明与五处模块例化。
- 对外端口分 5 组：控制（clk/srstn/tpu_start）、读数据（4 路 32bit）、读地址（4 路 10bit，注意方向是输出）、写回（a/b/c 三组，各含写使能/128bit 数据/6bit 地址）、完成（tpu_done）。
- "eight SRAM"是注释误导：物理上只有 4 个读端口，每路 32bit 含 4 个 8bit 通道，2 路 × 4 = 8 通道——这才是"8"的真实含义。
- 唯一的 localparam `ORI_WIDTH = DATA_WIDTH+DATA_WIDTH+5 = 21`，决定了乘加中间结果位宽，`ori_data` 共 168bit。
- 五处例化构成主线：**控制器（systolic_controll）指挥 → addr_sel 出读地址 → 外部 SRAM 返回读数据 → systolic 计算 → quantize 量化 → write_out 写回 a/b/c**。
- `matrix_index` 是关键的"一生产者两消费者"信号，同时连到 `systolic` 和 `write_out`，保证算出来的与写出去的是同一批结果。

## 7. 下一步学习建议

本讲只画出了"接线图"，还没有进入任何一个子模块的内部。接下来按依赖关系建议：

- **u1-l4（参数化与定点数）**：先把 `ARRAY_SIZE`、定点格式（8bit 输入 / 21bit 中间 / 16bit 输出）彻底搞懂，这是理解后续所有位宽的基础；
- **u1-l5（仿真环境）**：看 testbench 如何驱动本讲的这些端口（`tpu_start` 怎么给、SRAM 怎么接），把静态接线变成动态运行；
- 之后进入 **u2（脉动阵列数据通路）**，精读 `systolic.v` 内部——本讲里那个"黑盒 `systolic`"将第一次被打开，你会看到 `sram_rdata_w0/w1` 如何被切成 8 个通道、如何做 MAC 累加；
- 再进入 **u3（控制器、地址与写回）**，分别精读 `systolic_controll`、`addr_sel`、`write_out`、`quantize`，把本讲里那些"命令总线"的来源彻底讲清楚。

一句话：本讲给你的是**地图**，u1-l4/u1-l5 是**坐标与交通**，u2/u3 才是**走进每栋楼**。

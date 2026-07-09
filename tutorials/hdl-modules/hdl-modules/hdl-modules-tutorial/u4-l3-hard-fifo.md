# hard_fifo：硬核 FIFO 原语封装

## 1. 本讲目标

本讲聚焦 `hard_fifo` 模块。读完本讲，你应当能够：

- 说清「硬核 FIFO 原语（hard FIFO primitive）」与 u4-l1 讲过的「推断式 FIFO（inferred RAM FIFO）」的本质差别，以及各自的优势与局限。
- 读懂 `hard_fifo`（单时钟）与 `asynchronous_hard_fifo`（双时钟）这两层薄封装，理解它们如何把 AXI-Stream 式 ready/valid 握手翻译给底层 Xilinx `FIFO36E2` 原语的 `full/empty/wren/rden` 标志位接口。
- 掌握 `hard_fifo_pkg` 如何用一个枚举和两个函数，把任意用户位宽量化为原语支持的标准位宽/深度。
- 看懂 `fifo36e2_wrapper` 与原语对接的全部细节：奇偶端口拼位、SRL 自复位、FWFT 模式下的握手映射。
- 能够借助 `tools/synthesize.py` 对 `hard_fifo` 与推断式 `fifo` 做一次资源/时序对比，并据此判断硬核 FIFO 的适用场景。

## 2. 前置知识

本讲默认你已经学完：

- **u2-l1（握手约定）**：ready/valid、beat、packet、组合路径与 skid buffer 的概念。
- **u2-l2（common 基础包）**：`ram_style_t` 枚举与综合属性的含义。
- **u4-l1（同步 FIFO）**：推断式 `fifo` 的环形 RAM、指针、`level`/`almost_full` 以及 packet/last/drop/peek 一族 generic 的作用。
- **u4-l2（异步 FIFO）**：异步 FIFO 用格雷码指针 + `resync_counter` 跨域，以及配套 `scoped_constraints` 里 `set_max_delay` / `set_false_path` 的必要性。

几个本讲要用到的术语，先用一句话铺垫：

- **BRAM（Block RAM）**：FPGA 芯片内嵌的专用存储块。UltraScale+ 系列的一块 RAMB36 提供 36 Kbit 容量，可配置成多种「宽度 × 深度」组合。
- **硬核原语（primitive）**：器件厂商在硅片上固化的、有固定行为的功能单元。`FIFO36E2` 就是 UltraScale+ 中一块 RAMB36 的「FIFO 化」使用方式——它把 FIFO 需要的读写指针、满/空比较、可选输出寄存器全部做进了硅片，用户只看到 `full/empty/wren/rden` 这样的标志位接口。
- **推断（inference）**：与之相对，u4-l1 的 `fifo` 是用可综合 VHDL（数组 + 指针寄存器）描述行为，再让综合工具「推断」出应当使用 BRAM 来实现存储。推断式 FIFO 的指针和控制逻辑最终会占用 LUT/FF。
- **FWFT（First-Word Fall-Through，首字直通）**：FIFO 的一种读模式。使能后，写入的第一个数据会自动「冒」到输出端口，读侧用 `empty=0` 就能直接看到数据，无需先发起一次读。本讲的握手映射严重依赖这一点。
- **unisim**：Xilinx 提供的原语仿真库。要仿真 `FIFO36E2` 这类原语，必须先编译并加载 unisim；而综合时 Vivado 自带原语，不需要它。

## 3. 本讲源码地图

本讲涉及的关键文件都位于 `modules/hard_fifo/` 下：

| 文件 | 作用 |
| --- | --- |
| [modules/hard_fifo/src/hard_fifo_pkg.vhd](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/hard_fifo/src/hard_fifo_pkg.vhd) | 公共包：定义原语类型枚举 `fifo_primitive_t`，以及把用户位宽量化为原语位宽/深度的 `get_fifo_width` / `get_fifo_depth`。 |
| [modules/hard_fifo/src/fifo36e2_wrapper.vhd](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/hard_fifo/src/fifo36e2_wrapper.vhd) | 真正与 `FIFO36E2` 原语对接的实体：拼位、自复位、握手映射全在这里。 |
| [modules/hard_fifo/src/hard_fifo.vhd](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/hard_fifo/src/hard_fifo.vhd) | 单时钟（同步）封装：把统一的时钟接到读写两侧，委托给 `fifo36e2_wrapper`。 |
| [modules/hard_fifo/src/asynchronous_hard_fifo.vhd](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/hard_fifo/src/asynchronous_hard_fifo.vhd) | 双时钟（异步）封装：暴露独立 `clk_read` / `clk_write`，跨域交给原语内部处理。 |
| [modules/hard_fifo/module_hard_fifo.py](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/hard_fifo/module_hard_fifo.py) | tsfpga Module：登记仿真（按 generic 矩阵跑 `tb_hard_fifo`）、登记 netlist 资源回归（断言 1 块 BRAM、3 LUT、1 FF）。 |
| [modules/hard_fifo/test/tb_hard_fifo.vhd](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/hard_fifo/test/tb_hard_fifo.vhd) | 仿真测试台：用随机数据 + 随机 stall 验证同步/异步两种硬核 FIFO 的功能与背压鲁棒性。 |
| [tools/synthesize.py](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/tools/synthesize.py) | 快速综合脚本，本讲代码实践用它做资源对比。 |

值得注意的一个目录约定差异：与 `fifo` 模块相比，`hard_fifo` **没有** `scoped_constraints/` 子目录，也**没有** `rtl/` 子目录（可用 `find modules/hard_fifo -maxdepth 1 -type d` 与 `fifo` 对照确认）。原因随后解释——这正是硬核 FIFO 的一个重要优势。

## 4. 核心概念与源码讲解

### 4.1 硬核 FIFO 原语 vs 推断式 FIFO：为什么需要 hard_fifo

#### 4.1.1 概念说明

u4-l1 的推断式 `fifo` 用「数组 + 读写指针寄存器」描述一个 FIFO，综合后存储部分会被推断成 BRAM，但**指针递增、满/空比较、`level` 计算、almost 阈值比较、packet 计数**这些控制逻辑统统要用普通 LUT 和 FF 实现。当 FIFO 很深（例如 1024 以上）时，这些控制逻辑虽然不算多，却是真实可见的资源开销，而且会贡献组合逻辑级数。

Xilinx UltraScale+ 器件给了另一条路：直接例化 `FIFO36E2` 原语。这块硅片**把指针、满空标志、可选输出寄存器全部固化**，用户只看到一个标志位接口（`full`/`empty`/`wren`/`rden`），几乎不需要任何额外控制逻辑。`hard_fifo` 模块的存在，就是为了给这个原语套上一层干净的 AXI-Stream 式 ready/valid 外壳，让全项目的流式模块能即插即用地使用它。

源码头注释就把定位说得很清楚——这是一个「围绕 Xilinx 硬 FIFO 原语的封装，只能在特定器件上使用」：[modules/hard_fifo/src/hard_fifo.vhd:9-L12](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/hard_fifo/src/hard_fifo.vhd#L9-L12)。

#### 4.1.2 核心流程：两条路线的取舍

```text
推断式 fifo（u4-l1）               硬核 hard_fifo（本讲）
─────────────────────             ─────────────────────
VHDL 数组 + 指针寄存器              例化 FIFO36E2 原语
  │ 综合推断为 BRAM                  │ 存储与控制都在硅片内
  │ 控制逻辑 → LUT/FF                │ 控制逻辑 ≈ 0（3 LUT, 1 FF）
  │                                  │
任意 depth / 任意特性               固定深度档位 / 纯数据缓存
(packet/last/drop/peek/level)      （不暴露 level/almost）
跨厂商可移植                        仅 UltraScale+
异步 CDC：自己用 resync_counter     异步 CDC：原语内部双时钟
需配套 scoped_constraints 约束      无需用户侧时序约束
```

一句话总结：**推断式 FIFO 胜在灵活与可移植，硬核 FIFO 胜在控制逻辑近乎为零、且无需写 CDC 约束**。两者的存储部分最终都是 BRAM，差别在「控制逻辑住在哪里」。

### 4.2 hard_fifo_pkg：位宽/深度量化与原语类型枚举

#### 4.2.1 概念说明

`FIFO36E2` 是一块 36 Kbit 的存储，但它只接受若干**固定的端口宽度**（4/9/18/36/72 位）。用户给一个任意位宽（比如 32），必须先「向上取整」到原语支持的标准宽度。这个量化关系由 Xilinx 用户手册 UG573 的 table 1-21 规定，`hard_fifo_pkg` 把它编码成两个纯函数。包里还声明了一个枚举 `fifo_primitive_t`，目前只有一种取值 `primitive_fifo36e2`——它是为「将来可能支持更多原语」预留的扩展点。

#### 4.2.2 核心流程

量化规则是阶梯函数：按用户位宽落入哪一档，决定原语宽度；再用「总容量 ÷ 宽度」算出深度。原语总容量为 \(1024 \times 36\) bit，但 4 位宽是个特例。

\[ \text{fifo\_width} = \min\{ w \in \{4,9,18,36,72\} \mid w \geq \text{target\_width} \} \]

\[ \text{fifo\_depth} = \begin{cases} 8192, & \text{fifo\_width}=4 \\ \left\lfloor \dfrac{1024 \times 36}{\text{fifo\_width}} \right\rfloor, & \text{否则} \end{cases} \]

由此得到的宽度/深度对照表：

| 用户 `target_width` 落入 | `fifo_width` | `fifo_depth` |
| --- | --- | --- |
| ≤ 4 | 4 | 8192（特例） |
| 5–9 | 9 | 4096 |
| 10–18 | 18 | 2048 |
| 19–36 | 36 | 1024 |
| 37–72 | 72 | 512 |

#### 4.2.3 源码精读

枚举定义，目前唯一取值就是 `primitive_fifo36e2`：[modules/hard_fifo/src/hard_fifo_pkg.vhd:19-L19](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/hard_fifo/src/hard_fifo_pkg.vhd#L19)。

`get_fifo_width` 用一串 `if/elsif` 实现阶梯量化，超过 72 位则 `assert ... severity failure` 直接报错（综合期就会失败，而不是静默截断）：[modules/hard_fifo/src/hard_fifo_pkg.vhd:29-L48](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/hard_fifo/src/hard_fifo_pkg.vhd#L29-L48)。

`get_fifo_depth` 先复用 `get_fifo_width` 算出宽度，再套容量公式；4 位宽走 `1024 * 36 / 4.5 = 8192` 的特例：[modules/hard_fifo/src/hard_fifo_pkg.vhd:50-L61](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/hard_fifo/src/hard_fifo_pkg.vhd#L50-L61)。

> 这两个函数是精化期（elaboration）纯函数，会在综合前折叠成常量，本身不占任何电路资源——和 u2-l3 讲过的 `math_pkg` 同属一类「工具函数」。

#### 4.2.4 代码实践

1. **目标**：在脑中（或纸上）跑一遍量化函数，确认你能预测任意位宽对应的原语宽度与深度。
2. **步骤**：对 `data_width = 8, 16, 21, 32, 40` 五个值，分别用上表预测 `fifo_width` 与 `fifo_depth`。
3. **预期结果**：8→(9, 4096)、16→(18, 2048)、21→(36, 1024)、32→(36, 1024)、40→(72, 512)。
4. **观察重点**：注意 21 和 32 都会落到 36 位档、深度同为 1024——这正是测试台里特意加入「介于标准档位之间的 21」的原因（见 4.6.3）。

#### 4.2.5 小练习与答案

**练习 1**：用户位宽为 33 时，`get_fifo_width` 返回多少？实际占用原语的多少位？

> **答案**：返回 36（落入 19–36 档）。但 33 位用户数据真正用到的只是其中 33 位，多出的 3 位原语容量被浪费——这是硬核 FIFO「粒度固定」带来的代价。

**练习 2**：为什么 4 位宽的深度公式要单独写一个特例，而不是直接 `1024 * 36 / 4`？

> **答案**：因为 4 位宽无法有效利用原语每字节 1 位的奇偶端口（见 4.5.3），实际可用容量下降到 32 Kbit，所以用 `1024 * 36 / 4.5 = 8192` 而不是 `1024 * 36 / 4 = 9216`。

### 4.3 hard_fifo：同步封装与原语选择

#### 4.3.1 概念说明

`hard_fifo` 是面向单时钟场景的薄封装。它的职责只有两件：把读写两侧的端口共享同一个 `clk`；通过 `primitive_type` generic 选择底层原语（目前只有 `FIFO36E2`）。它本身不做任何数据或时序处理，真正的活儿全在 `fifo36e2_wrapper` 里。

#### 4.3.2 核心流程

```text
generic: data_width, enable_output_register, primitive_type(默认 fifo36e2)
   │
   ▼
if primitive_type = primitive_fifo36e2 generate
   例化 fifo36e2_wrapper
     is_asynchronous => false        ← 同步模式
     clk_read  => clk   ┐ 同一个时钟
     clk_write => clk   ┘
else generate
   assert false severity failure      ← 未知原语：编译期失败
```

#### 4.3.3 源码精读

实体声明。generic 只有三个：`data_width`、`enable_output_register`，以及带默认值的 `primitive_type`；端口是标准的 AXI-Stream 式 ready/valid：[modules/hard_fifo/src/hard_fifo.vhd:21-L38](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/hard_fifo/src/hard_fifo.vhd#L21-L38)。注意读侧是 `read_ready`（in）/`read_valid`（out），写侧是 `write_ready`（out）/`write_valid`（in）——方向和 u4-l1 的 `fifo` 完全一致，可以无缝替换。

`select_primitive` generate 块：匹配到 `primitive_fifo36e2` 就例化 wrapper，并把 `is_asynchronous => false`、读写时钟都接到 `clk`：[modules/hard_fifo/src/hard_fifo.vhd:44-L66](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/hard_fifo/src/hard_fifo.vhd#L44-L66)。

`else generate` 分支是一个 `assert false ... severity failure`：[modules/hard_fifo/src/hard_fifo.vhd:69-L74](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/hard_fifo/src/hard_fifo.vhd#L69-L74)。这是 hdl-modules 里反复出现的「编译期硬约束」写法——一旦将来加入新原语却忘了写分支，综合会立刻失败，而不是悄悄选错实现。和 u4-l1 里固化 generic 依赖的 `assert severity failure` 是同一种防御式风格。

#### 4.3.4 代码实践

1. **目标**：确认 `hard_fifo` 与推断式 `fifo` 的端口可以互换。
2. **步骤**：对照 [modules/fifo/src/fifo.vhd:93-L104](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/fifo/src/fifo.vhd#L93-L104)（推断式 fifo 的 `clk`/`read_ready`/`read_valid`/`read_data`/`write_ready`/`write_valid`/`write_data`），逐个比对 `hard_fifo` 的端口名与方向。
3. **预期结果**：核心 ready/valid/data 信号一一对应、方向一致；唯一差别是推断式 fifo 多出 `level`/`almost_full`/`almost_empty` 等状态口，而 `hard_fifo` 没有（见 4.5.1 的限制说明）。
4. **结论**：在不需要 `level`/packet 等高级特性的纯数据缓冲场景，`hard_fifo` 可作为 `fifo` 的「drop-in（直接替换）」候选。

#### 4.3.5 小练习与答案

**练习**：`primitive_type` 这个 generic 现在只有一个取值，为什么还要做成 generic 而不是写死？

> **答案**：为了将来扩展（比如支持 `FIFO18E2` 或更高端器件的原语）预留统一的开关点；同时配合 `assert` 把「未实现的原语」变成显式的编译期错误，而不是隐藏的运行期 bug。

### 4.4 asynchronous_hard_fifo：双时钟封装

#### 4.4.1 概念说明

`asynchronous_hard_fifo` 面向读写时钟独立的场景。它和 `hard_fifo` 几乎是镜像的，唯一差别是读写各有独立时钟端口。**关键点**：它**不使用** u4-l2 讲过的 `resync_counter` 格雷码方案——双时钟跨越完全由 `FIFO36E2` 原语**内部**完成（通过 `CLOCK_DOMAINS => "INDEPENDENT"` 配置）。这也解释了为什么本模块没有 `scoped_constraints/` 目录：CDC 路径在硅片内部，用户侧无需也无法对其施加 `set_max_delay`/`set_false_path`。

#### 4.4.2 核心流程

```text
端口: clk_read, clk_write（独立）
   │
   ▼
例化 fifo36e2_wrapper
   is_asynchronous => true           ← 异步模式
   clk_read  => clk_read   ┐ 两个独立时钟
   clk_write => clk_write  ┘
（原语内部用双时钟指针完成 CDC，无需 resync_counter、无需用户约束）
```

#### 4.4.3 源码精读

实体声明，注意 `clk_read` 与 `clk_write` 分属两个时钟域：[modules/hard_fifo/src/asynchronous_hard_fifo.vhd:21-L38](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/hard_fifo/src/asynchronous_hard_fifo.vhd#L21-L38)。

generate 块结构与同步版完全对称，差别仅是 `is_asynchronous => true` 和时钟分别接线：[modules/hard_fifo/src/asynchronous_hard_fifo.vhd:44-L64](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/hard_fifo/src/asynchronous_hard_fifo.vhd#L44-L64)。

同样有未知原语的 `assert ... severity failure` 兜底：[modules/hard_fifo/src/asynchronous_hard_fifo.vhd:67-L72](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/hard_fifo/src/asynchronous_hard_fifo.vhd#L67-L72)。

> **对比 u4-l2**：推断式 `asynchronous_fifo` 必须自己用 `resync_counter` 同步格雷码指针，并配套 `.tcl` 里的 `set_bus_skew`/`set_max_delay`；而 `asynchronous_hard_fifo` 把这一切都「外包」给了原语。这是硬核 FIFO 在异步场景下最显著的省心之处——但代价是锁死在 Xilinx UltraScale+ 器件上。

#### 4.4.4 代码实践

1. **目标**：体会「异步硬核 FIFO 把 CDC 藏进原语」这一设计取舍。
2. **步骤**：在仓库根目录执行 `find modules/hard_fifo -name '*.tcl'` 与 `find modules/fifo -name '*.tcl'`，对比两个模块的约束文件情况。
3. **预期结果**：`hard_fifo` 下没有任何 `.tcl`；`fifo` 下有 `scoped_constraints/asynchronous_fifo.tcl` 等约束。
4. **思考**：如果要把一个 `asynchronous_hard_fifo` 移植到非 Xilinx 器件，你会遇到什么障碍？（提示：`FIFO36E2` 原语不存在、unisim 不可用、CDC 行为需自行重建。）

#### 4.4.5 小练习与答案

**练习**：`asynchronous_hard_fifo` 的端口里有几个时钟？为什么 `hard_fifo` 只有一个？

> **答案**：`asynchronous_hard_fifo` 有 `clk_read` 和 `clk_write` 两个时钟，因为读写分属独立时钟域；`hard_fifo` 只有一个 `clk`，因为它把同一个时钟同时接到 wrapper 的 `clk_read` 和 `clk_write`，并设 `is_asynchronous => false` 让原语进入「COMMON 时钟域」模式。

### 4.5 fifo36e2_wrapper：与 Xilinx FIFO36E2 原语对接

这是整个模块的核心，也是最难的一节。它做四件事：拼位、自复位、例化原语、把标志位接口翻译成 ready/valid。

#### 4.5.1 概念说明：被「砍掉」的信号

在精读之前，先看 wrapper 头注释里的两个重要 note/warning——它们说明了为什么这个 FIFO 比 u4-l1 的推断式 fifo「功能少」：

- `almost_full`/`almost_empty` 原语本来提供，但「没有仿真用例，暂不引出」。
- `level`（读写计数）**不引出**，因为 unisim 仿真模型里这些计数信号有毛刺（见注释引用的 `fifo_glitches.png`），作者怀疑只是仿真模型问题、硬件上正常，但出于严谨选择不暴露。

这两段说明在：[modules/hard_fifo/src/fifo36e2_wrapper.vhd:12-L24](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/hard_fifo/src/fifo36e2_wrapper.vhd#L12-L24)。这就是 hard_fifo 没有 `level`/`almost_*` 端口的根因，也是它与推断式 fifo 功能差异的来源。

#### 4.5.2 核心流程：四件事

```text
1. 量化     get_fifo_width / get_fifo_depth（来自 hard_fifo_pkg）
2. 拼位     把用户 data 拆成「数据口 DIN」+「奇偶口 DINP」两段
3. 自复位   16 级 SRL 移位寄存器产生足够长的复位脉冲（写时钟域）
4. 例化+映射 FIFO36E2
            CLOCK_DOMAINS / REGISTER_MODE 用 impure 函数按 generic 算
            FWFT=TRUE
            把 full/empty/wren/rden 翻译成 ready/valid
```

#### 4.5.3 源码精读之一：拼位（奇偶端口）

`FIFO36E2` 的存储被组织成「每 9 位一组（8 数据 + 1 奇偶）」。当用户位宽大于「纯数据口」容量时，多出来的位可以借用奇偶端口 `DINP/DOUTP`，从而不浪费原语容量。wrapper 用两个常量刻画这件事：

```vhdl
constant parity_port_width : natural := fifo_width / 9;
constant data_port_width : positive := write_data'length - parity_port_width;
```

即：奇偶口宽度 = 原语宽度除以 9（等于「9 位组」的个数）；数据口宽度 = 用户位宽减去奇偶口宽度。这两行在 [modules/hard_fifo/src/fifo36e2_wrapper.vhd:80-L82](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/hard_fifo/src/fifo36e2_wrapper.vhd#L80-L82)。

> 举例：`data_width=32` 时 `fifo_width=36`，于是 `parity_port_width = 36/9 = 4`，`data_port_width = 32-4 = 28`。也就是 28 位走 DIN、4 位走 DINP，合计正好 32 位用户数据，没有一位浪费。

实际的拆分与重组在 `assign_data` 进程里：写入时把 `write_data` 的低 `data_port_width` 位送 `din`、高 `parity_port_width` 位送 `dinp`；读出时反向拼回 `read_data`：[modules/hard_fifo/src/fifo36e2_wrapper.vhd:210-L220](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/hard_fifo/src/fifo36e2_wrapper.vhd#L210-L220)。

#### 4.5.4 源码精读之二：自复位 SRL

`FIFO36E2` 要求复位脉冲持续足够长（参考 Xilinx UG473 图 2-2、UG974 第 293 页），且**双时钟模式下复位必须由写时钟驱动**。wrapper 用一个 16 级移位寄存器（初值全 `'1'`）在 `clk_write` 上移位，取最末位作为 `reset`，从而在开机后产生约 16 个写时钟周期的复位脉冲：

```vhdl
signal reset_pipe : std_ulogic_vector(0 to 16 - 1) := (others => '1');
...
reset_proc : process is
begin
  wait until rising_edge(clk_write);
  reset_pipe <= '0' & reset_pipe(reset_pipe'left to reset_pipe'right - 1);
end process;
reset <= reset_pipe(reset_pipe'right);
```

复位信号定义与进程见 [modules/hard_fifo/src/fifo36e2_wrapper.vhd:84-L86](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/hard_fifo/src/fifo36e2_wrapper.vhd#L84-L86) 与 [modules/hard_fifo/src/fifo36e2_wrapper.vhd:116-L126](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/hard_fifo/src/fifo36e2_wrapper.vhd#L116-L126)。注释里点明了「双时钟下复位由写时钟驱动」的依据。

> 这个复位脉冲会通过 `write_ready` 暴露出来——复位期间 `write_ready` 恒为 0，所以测试台 `test_init_state` 要等到 `write_ready` 变 1 才认为 DUT 就绪（见 4.6.3）。

#### 4.5.5 源码精读之三：例化原语与两个 impure 函数

原语的两个关键 generic 用 `impure function` 按 `is_asynchronous` / `enable_output_register` 现场计算：

- `get_clock_domains`：异步返回 `"INDEPENDENT"`，同步返回 `"COMMON"`。
- `get_register_mode`：使能输出寄存器返回 `"REGISTERED"`，否则 `"UNREGISTERED"`（注释提到还有 `"DO_PIPELINE"` 选项但未使用）。

这两个函数定义在 [modules/hard_fifo/src/fifo36e2_wrapper.vhd:60-L78](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/hard_fifo/src/fifo36e2_wrapper.vhd#L60-L78)。

`FIFO36E2` 的例化与 generic map：注意 `FIRST_WORD_FALL_THROUGH => "TRUE"`（FWFT，握手映射的前提）、`READ_WIDTH`/`WRITE_WIDTH` 用量化后的 `fifo_width`、`REGISTER_MODE` 与 `CLOCK_DOMAINS` 用上面的函数：[modules/hard_fifo/src/fifo36e2_wrapper.vhd:148-L160](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/hard_fifo/src/fifo36e2_wrapper.vhd#L148-L160)。完整的端口映射（含一堆级联用不到、固定接死的 `CAS*` 信号）在 [modules/hard_fifo/src/fifo36e2_wrapper.vhd:161-L196](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/hard_fifo/src/fifo36e2_wrapper.vhd#L161-L196)。

#### 4.5.6 源码精读之四：标志位 → ready/valid 的握手映射

这是整个 wrapper 最精妙的四行，它把原语的「标志位接口」翻译成全项目统一的 ready/valid：

```vhdl
rden       <= read_ready and not empty;   -- 只在「想读且非空」时真正读
read_valid <= not empty;                   -- FWFT：非空即有数据
write_ready <= not (full or reset);        -- 非满且已退出复位才能写
wren       <= write_ready and write_valid; -- 写握手成立才写
```

这四行在 [modules/hard_fifo/src/fifo36e2_wrapper.vhd:199-L207](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/hard_fifo/src/fifo36e2_wrapper.vhd#L199-L207)。逐条理解：

1. **`read_valid <= not empty`**：因为开了 FWFT，数据在 `empty=0` 时已经出现在 `dout`，所以「非空」就等价于「读数据有效」。这条是**纯组合**的，不引入额外寄存器延迟——FWFT 的红利。
2. **`write_ready <= not (full or reset)`**：能接受写入当且仅当「没满」。额外 or 上 `reset`，是为了在开机复位期间强制 `write_ready=0`，避免上游误写入。
3. **`wren <= write_ready and write_valid`**：标准写握手——双方都就绪才算一次写入。
4. **`rden <= read_ready and not empty`**：标准读握手，但**额外用 `not empty` 保护**。源码上一行注释解释：在 AXI-Stream 里，消费方完全可以在没有数据时把 `read_ready` 拉高（「我准备好了，但你不给数据也行」）；但作者不确定原语在这种「对空 FIFO 发起读」时会发生什么，于是宁可花「每个控制信号 1 个 LUT」来挡住这种情况。这就是 hard_fifo 资源回归里那 3 个 LUT 的主要来源。

> 这四行是「把任意标志位式存储原语接入 AXI-Stream 流水线」的通用范式：满/空标志天然对应 ready 的反/正，而 valid 用 FWFT 的 `not empty` 表达。记住这个套路，以后看任何厂商的硬 FIFO 封装都能迅速对上号。

#### 4.5.7 关于 level 信号（未引出）

虽然 wrapper 内部算出了 `read_level`/`write_level`（用项目内的转换函数把 `rdcount`/`wrcount` 转成 `natural`），但如 4.5.1 所述，因为仿真毛刺问题**没有把它们连到端口**：[modules/hard_fifo/src/fifo36e2_wrapper.vhd:223-L226](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/hard_fifo/src/fifo36e2_wrapper.vhd#L223-L226)。这两个信号只服务于 wrapper 内部的两处 `assert`（断言 almost 阈值合法）。

#### 4.5.8 代码实践

1. **目标**：在测试台里观察这四行握手映射的真实行为，尤其是 FWFT 与复位期间 `write_ready=0`。
2. **步骤**：阅读 `tb_hard_fifo.vhd` 的 `test_init_state` 分支，它先断言复位前 `read_valid='0'` 且 `write_ready='0'`，然后等待并断言复位后 `write_ready='1'`：[modules/hard_fifo/test/tb_hard_fifo.vhd:161-L176](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/hard_fifo/test/tb_hard_fifo.vhd#L161-L176)。
3. **预期现象**：仿真启动后约 16 个写时钟周期内 `write_ready` 保持 0；之后跳变为 1 并稳定，`read_valid` 在收到首个数据前一直为 0。
4. **运行方式（待本地验证）**：需先按 u1-l3 配置好 Vivado 仿真库（unisim），再在仓库根目录运行
   `python tools/simulate.py hard_fifo --vivado-simlib-path <path> --include-unisim`
   （具体参数名以本地 tsfpga/VUnit 版本为准，若环境不支持编译 unisim，则改为「源码阅读型实践」——只对照上面的四行映射画出时序波形示意图）。

#### 4.5.9 小练习与答案

**练习 1**：为什么 `read_valid` 可以用 `not empty` 直接表达，而 `write_ready` 却是 `not (full or reset)`、多了一个 `reset`？

> **答案**：`read_valid` 表达「现在有没有数据可读」，复位期间 `empty` 本来就是 1（FIFO 空），所以 `not empty` 自然给出 0，无需额外处理；`write_ready` 表达「现在能不能接受写」，复位期间原语尚未就绪、贸然写入会触发 `wrerr`，所以必须用 `reset` 显式把 `write_ready` 钳到 0。

**练习 2**：作者说 `rden <= read_ready and not empty` 里的 `and not empty`「每个控制信号花掉 1 个 LUT」。如果不加这个保护、直接写 `rden <= read_ready`，可能会出什么问题？

> **答案**：AXI-Stream 允许消费方在无数据时拉高 `read_ready`；若直接 `rden <= read_ready`，就会在 FIFO 空时对原语发起一次读。原语在「读空」时的行为未被作者确认（可能触发 `rderr`、可能错乱计数），所以宁可多花 1 个 LUT 把它挡住。这是「正确性优先于极省资源」的典型工程取舍。

### 4.6 仿真、资源回归与 unisim 依赖

#### 4.6.1 概念说明

`hard_fifo` 因为依赖 Xilinx 原语，在仿真和构建上有一套和其它模块不同的约定：仿真必须有 unisim，否则要能把整个模块排除掉；构建则用 netlist 资源回归把「硬核 FIFO 真的只用了 1 块 BRAM」这件事固化进 CI。

#### 4.6.2 核心流程

```text
仿真侧（module_hard_fifo.py）
  get_simulation_files(include_unisim=False) → 排除 4 个原语相关文件
  setup_vunit(include_unisim=True)           → 才登记 tb_hard_fifo
  测试矩阵: data_width × 同步/异步 × 读快/写快 × output_register

构建侧（module_hard_fifo.py）
  get_build_projects → 对 hard_fifo / asynchronous_hard_fifo
                       在 xcku5p 上做 netlist build
  build_result_checkers:
    TotalLuts(EqualTo(3))        ← 控制逻辑极省
    Ffs(EqualTo(1))
    Ramb36(EqualTo(1))           ← 正好用 1 块 BRAM
    MaximumLogicLevel(EqualTo(2)) ← 关键路径逻辑级数很低
```

#### 4.6.3 源码精读

**仿真排除逻辑**：`get_simulation_files` 在 `include_unisim=False` 时把 `asynchronous_hard_fifo.vhd`、`fifo36e2_wrapper.vhd`、`hard_fifo.vhd`、`tb_hard_fifo.vhd` 这四个依赖原语/unisim 的文件加入 `files_avoid`，使整个模块在纯 VHDL 仿真流里也能安全编译：[modules/hard_fifo/module_hard_fifo.py:25-L44](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/hard_fifo/module_hard_fifo.py#L25-L44)。`setup_vunit` 只在 `include_unisim=True` 时才调用 `_setup_hard_fifo_test`：[modules/hard_fifo/module_hard_fifo.py:46-L53](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/hard_fifo/module_hard_fifo.py#L46-L53)。

**测试矩阵**：`data_width` 取 `[4, 9, 16, 21, 36]`（含一个「介于标准档位之间」的 21），`is_asynchronous` 与 `read_clock_is_faster` 笛卡尔积，`enable_output_register` 按 `data_width` 奇偶轮换；`test_fifo_full` 给 95% 读侧 stall，`test_fifo_empty` 给 95% 写侧 stall：[modules/hard_fifo/module_hard_fifo.py:55-L81](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/hard_fifo/module_hard_fifo.py#L55-L81)。

**容量常量**：测试台用一个公式算 FIFO 容量，注意同步比异步多装一个字、开输出寄存器再多一个字——这是硬核 FIFO 的固有行为，测试台必须精确建模：

```vhdl
constant fifo_capacity : positive :=
  depth + to_int(enable_output_register) + to_int(not is_asynchronous);
```

见 [modules/hard_fifo/test/tb_hard_fifo.vhd:47-L51](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/hard_fifo/test/tb_hard_fifo.vhd#L47-L51)。

**资源回归**：`get_build_projects` 对 `data_widths=[18,32]` 各做一个同步、一个异步 netlist build，并用四个 checker 断言资源——这就是「硬核 FIFO 极省资源」的量化承诺，一旦某次改动让 LUT 涨到 4 或 BRAM 变成 2，CI 立刻失败：[modules/hard_fifo/module_hard_fifo.py:83-L113](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/hard_fifo/module_hard_fifo.py#L83-L113)。（netlist 资源回归的方法论详见 u8-l3。）

#### 4.6.4 代码实践

1. **目标**：理解「没有 Vivado 仿真库也能排除整个 hard_fifo 模块」的机制。
2. **步骤**：阅读 [modules/hard_fifo/module_hard_fifo.py:25-L44](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/hard_fifo/module_hard_fifo.py#L25-L44)，找到被排除的四个文件；再查 doc 里关于 `names_avoid` / `include_unisim` 的说明：[modules/hard_fifo/doc/hard_fifo.rst:1-L9](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/hard_fifo/doc/hard_fifo.rst#L1-L9)。
3. **预期结果**：在没有 unisim 的环境里，整个 `hard_fifo` 模块的 src/test 文件会被跳过，不会导致仿真编译失败。
4. **结论**：这就是 hdl-modules「器件相关模块可选可关」的统一模式——通过 `include_unisim` 开关 + `files_avoid` 实现。

#### 4.6.5 小练习与答案

**练习**：资源回归断言 `Ramb36(EqualTo(1))`。如果有人把 `data_width` 从 32 改成 64，这个断言还成立吗？

> **答案**：成立。`data_width=64` 落入 `fifo_width=72` 档，仍然只用一块 RAMB36（一块 36Kb BRAM 配成 72 位宽、512 深）。资源回归对 18 和 32 都断言 1 块 BRAM；64 位虽未被 CI 覆盖，但由 `get_fifo_width` 的量化逻辑可知仍映射到单块原语。这也是硬核 FIFO「深度/宽度变了但 BRAM 数不变」的特性——与推断式 fifo 不同。

## 5. 综合实践

**任务**：用 `tools/synthesize.py` 把 `hard_fifo` 与推断式 `fifo` 各综合一次，对比资源与逻辑级数，并写出硬核 FIFO 的适用场景结论。

### 背景与依据

- `tools/synthesize.py` 接收一个顶层实体名和若干 `--generic name=v1,v2`，对每组 generic 各建一个 netlist 工程（默认器件 `xcku5p`，属 UltraScale+，能识别 `FIFO36E2`）。generic 解析只支持布尔和整数：[tools/synthesize.py:110-L167](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/tools/synthesize.py#L110-L167)。
- `hard_fifo` 的 `primitive_type` 是枚举、无法通过命令行传入，但它有默认值 `primitive_fifo36e2`，所以不传也能综合。
- 推断式 `fifo` 的 generic 名为 `width`/`depth`/`enable_output_register` 等（见 [modules/fifo/src/fifo.vhd:66-L92](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/fifo/src/fifo.vhd#L66-L92)）；其 `ram_type` 是枚举（`ram_style_t`），无法命令行传入，但默认 `ram_style_auto` 会让 Vivado 自动选 BRAM。

### 操作步骤

在仓库根目录依次执行（以下命令待本地验证——需已安装 Vivado 与 tsfpga 工具链，详见 u1-l3）：

```bash
# 1) 硬核 FIFO：32 位用户数据 → 量化为 fifo_width=36, depth=1024
python tools/synthesize.py hard_fifo \
  --generic data_width=32 \
  --generic enable_output_register=false \
  --analyze-timing

# 2) 推断式 FIFO：32 位宽、1024 深，默认 ram_type=auto（推断为 BRAM）
python tools/synthesize.py fifo \
  --generic width=32 \
  --generic depth=1024 \
  --generic enable_output_register=false \
  --analyze-timing
```

综合完成后，在生成的工程目录（默认位于 `tools_env.HDL_MODULES_GENERATED/projects` 下）里查看每个工程的**资源利用报告（utilization report）**与**时序报告（逻辑级数 / logic level）**。

### 需要观察与记录的量

| 观察项 | hard_fifo（文档值） | fifo（待本地验证） |
| --- | --- | --- |
| `TotalLuts` | 3（CI 回归断言，见 4.6.3） | 预期明显更高（指针、满空比较、`level` 计算） |
| `Ffs` | 1 | 预期更高（指针寄存器等） |
| `RAMB36` | 1 | 预期 1（存储同为 BRAM） |
| 最大逻辑级数 | 2 | 预期更高（组合比较路径） |

> 注：`hard_fifo` 的 3 LUT / 1 FF / 1 RAMB36 / 逻辑级数 2 来自 `module_hard_fifo.py` 的 CI 资源回归（覆盖 `data_width` 为 18、32），是已验证的文档值；`fifo` 一栏请填入你本地综合得到的真实数字，不要照抄。

### 预期结论（综合实践要回答的问题）

1. **资源**：两者存储都是 1 块 BRAM，但推断式 `fifo` 在 LUT/FF 上显著多于 `hard_fifo`——差距来自推断式 fifo 必须用普通逻辑实现指针与控制。
2. **时序**：`hard_fifo` 关键路径逻辑级数更低（文档值 2），因为控制逻辑在硅片内、握手映射只有一两级组合。
3. **适用场景**：
   - **硬核 FIFO 更优**：UltraScale+ 上、需要大而深的纯数据缓冲（深度恰好落在 512/1024/2048/4096/8192 档）、不需要 `level`/packet/last 等高级特性、想极致省 LUT/FF 与逻辑级数、且不想写异步 CDC 约束时。
   - **推断式 fifo 更优**：需要跨厂商可移植、需要任意深度或 packet/drop/peek/level 等特性、或深度很小（用 LUTRAM 实现比整块 BRAM 更划算）时。

把你的本地实测数字与上述结论整理成一小段对比说明，即完成本实践。

## 6. 本讲小结

- `hard_fifo` 把 Xilinx UltraScale+ 的 `FIFO36E2` 硬原语封装成 AXI-Stream 式 ready/valid 接口，与 u4-l1 的推断式 `fifo` 端口一致、可作纯数据缓冲的直接替换。
- `hard_fifo_pkg` 用 `get_fifo_width`/`get_fifo_depth` 把任意用户位宽量化到原语支持的 {4,9,18,36,72} 档，深度由容量公式 \(1024\times36/\text{width}\)（4 位宽特例 8192）决定。
- 三层结构：`hard_fifo`/`asynchronous_hard_fifo` 是选择原语、区分单/双时钟的薄壳；`fifo36e2_wrapper` 才是真正与原语对接的核心。
- `fifo36e2_wrapper` 的四件事：奇偶端口拼位、16 级 SRL 自复位（写时钟域）、按 generic 用 impure 函数配置 `CLOCK_DOMAINS`/`REGISTER_MODE`、用四行组合逻辑把 `full/empty/wren/rden` 翻译成 ready/valid（FWFT 让 `read_valid = not empty`）。
- 异步版**不使用** `resync_counter`——CDC 由原语内部完成，故本模块**没有** `scoped_constraints/`，也无需用户侧 CDC 约束；代价是锁死在 Xilinx UltraScale+。
- 局限：器件绑定、需 unisim 才能仿真（可用 `include_unisim`/`files_avoid` 整体排除）、宽度/深度粒度固定、不暴露 `level`/`almost_*`（仿真毛刺）、无 packet/last/drop/peek。CI 资源回归断言它只用 1 块 BRAM + 3 LUT + 1 FF、逻辑级数 2。

## 7. 下一步学习建议

- **横向对比**：回看 u4-l1 的推断式 `fifo` 与 u4-l2 的 `asynchronous_fifo`，把「控制逻辑在 LUT 里 + 自带 resync_counter + 需要 scoped_constraints」与本讲的「控制逻辑在硅片里 + 无 resync + 无约束」列成对照表，巩固取舍判断。
- **进入 AXI 总线**：FIFO 是 AXI 各通道缓冲的基石。下一单元 u5（AXI/AXI-Stream/AXI-Lite）会大量复用本讲和前两讲建立的握手与缓冲概念，例如 `axi_stream_fifo`、各通道 FIFO 与 CDC。
- **验证方法论**：本讲出现的 netlist 资源回归（`EqualTo`/`Ramb36`/`MaximumLogicLevel`）在 u8-l3 会系统讲解；`tb_hard_fifo` 的随机 stall 矩阵写法在 u8-l1/u8-l2 展开。
- **继续阅读源码**：想加深理解可精读 `FIFO36E2` 原语的 Xilinx 官方手册（UG573 table 1-21、UG473 图 2-2、UG973），对照本讲的量化函数与复位时序，体会「封装层如何把器件手册的约束编码进 VHDL」。

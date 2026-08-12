# 设计规范、流片检查与二次开发

> 本讲是 OH! 学习手册的收官篇。前面 8 个单元我们一直在「读」——读原语、读协议、读链路、读物理实现。本讲把视角拉高到「写」与「交付」：OH! 用哪些规范约束它的代码？一颗芯片流片（tape-out）前要过哪些关？以及最重要的——你如何按 OH! 的约定**亲手新增一个 IP** 并把它接入仿真平台。这三件事分别对应三个最小模块：**设计规范、流片检查、二次开发流程**。

---

## 1. 本讲目标

学完本讲，你应当能够：

- 说出 OH! 的 Design / Coding / Documentation 三套 Guide 各自管什么，并能在一段陌生 Verilog 上「肉眼 lint」出违规点；
- 用 `docs/tapeout_checklist.md` 的分类对一个模块做流片前自检，并能把清单里的条目对应回本手册讲过的具体原语（如 `oh_rsync`、`oh_fifo_cdc`、`asic_clkicgand`）；
- 独立按 OH! 约定新建一个最小 emesh 外设 IP：写 RTL + `regmap.vh` + dut 包装 + `test_basic.emf`，并用 `build.sh` / `sim.sh` 走通编译与仿真流程（并知道在仓库当前状态下哪里会卡住、怎么修）。

## 2. 前置知识

本讲是「综合应用」级别，会频繁调用前 8 个单元的结论。开讲前请确认你熟悉下面这些概念（不熟悉可回查对应讲义）：

- **emesh 104 位事务包**与 `access` / `wait` 握手（u5-l1）：外设的「公共语言」。
- **`.vh` 寄存器映射模式**与地址译码、写选通（u6-l1）：每个 IP 的「软件接口」是怎么定的。
- **`emesh_unpack` / `emesh_readback`**（u5-l3）：读寄存器时数据如何回送。
- **GPIO 模块全流程**（u6-l2）：本讲把它当作「标准外设样板」反复对照。
- **仿真平台 `dv_top` 三段式骨架与 `.emf` 激励格式**（u4-l1、u4-l2、u4-l3）：二次开发最后要接进去的「插槽」。
- **soft / hard 双实现**（u9-l1）与 **asiclib 标准单元**（u9-l2）：规范里「只用可综合关键字」「无 timescale / 无延时」的根因。
- **CDC 同步原语**（u2-l4）与 **FIFO**（u3-l2）：流片清单里「跨时钟域」条目的落点。

一个贯穿全讲的阅读原则（从 u1-l1 起反复强调）：**代码与 LICENSE 是事实，README / 脚本 / 清单可能滞后**。本讲的「二次开发」环节会把这条原则用到极致——你会亲手撞上文档与实际目录不一致的地方，并学会以源码为准去修复。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲用它做什么 |
|------|------|----------------|
| `README.md` | 项目总览，含 Philosophy、Modules、Design/Coding/Documentation Guide、Tapeout Checklist 链接 | 设计规范的「单一事实源」 |
| `docs/tapeout_checklist.md` | 流片前自检清单（14 大类、上百条） | 流片检查模块的核心材料 |
| `docs/chip_glossary.md` | 芯片设计术语表 | 查阅清单里不懂的缩写（CDC、BIST、DFT、LVS……） |
| `docs/verilog_faq.md` | Verilog 问答与代码片段 | 二次开发时的速查（复位、dump 波形、emacs 关键字） |
| `gpio/hdl/gpio.v` | 一个完整的 emesh 外设样板 | 二次开发的「抄写模板」 |
| `gpio/hdl/gpio_regmap.vh` | GPIO 寄存器映射宏 | `regmap.vh` 的最小范例 |
| `gpio/dv/dut_gpio.v` | GPIO 的 dut 包装 | dut 端口契约的范例 |
| `gpio/dv/tests/test_basic.emf` | GPIO 的激励文件 | `.emf` 写法的范例 |
| `scripts/build.sh` | iverilog 编译脚本 | 二次开发的「编译入口」 |
| `scripts/sim.sh` | 仿真运行脚本 | 二次开发的「运行入口」 |
| `stdlib/testbench/libs.cmd` | iverilog 的库搜索路径配置 | 理解编译为何能自动找到分散的源码 |
| `stdlib/rtl/oh_counter.v` | 一个无 emesh 接口的纯原语计数器 | 对比「原语」与「外设 IP」两种风格的差异 |

---

## 4. 核心概念与源码讲解

### 4.1 设计规范

#### 4.1.1 概念说明

「设计规范」回答的是一个问题：**为什么 OH! 的几百个文件读起来长得那么像？**

答案不在某个工具里，而在 `README.md` 里用纯文本写下的三段约定——**Design Guide**（怎么切分模块）、**Coding Guide**（怎么写每一行 Verilog）、**Documentation Guide**（怎么写文档）。它们不是 EDA 工具的配置，而是给人看的「家规」；是否遵守全靠 code review 与自律，但正因如此，全库风格高度统一，任何一个模块拿过来都能秒懂。

这三套 Guide 与本手册前面的内容是**互为表里**的：Coding Guide 里「No timescales in design files」「Only synthesizable constructs」对应 u9-l1 讲的「设计文件不含 timescale 与延时，以保证 soft / hard 可替换」；「Use active low reset」「If async reset, use oh_rsync」直接指向 u2-l2、u2-l4 的 `nreset` 与 `oh_rsync`；Design Guide 里「Separate configuration from design」对应 u7-l4 的 elink 配置子系统。

#### 4.1.2 核心流程：三套 Guide 各管一段

把三套 Guide 拆成可操作的检查清单：

**Design Guide（架构层「怎么切」）**——核心动词是 *Separate*（解耦）：

| 条目 | 一句话理解 |
|------|-----------|
| Separate circuit from logic | 把「电路结构」（如 hard 宏、IO 单元）与「逻辑功能」分开 |
| Separate control from the datapath | 控制路径与数据通路分文件（参考 elink 的 `etx_protocol` vs `etx_io`） |
| Separate configuration from design | 配置寄存器与业务逻辑解耦（u7-l4） |
| Separate design from testbench / test | 设计 (`hdl/`)、测试台 (`dv/`)、测试数据 (`tests/*.emf`) 三者分目录 |
| Use 64b / nibble boundaries | 寄存器字段按 64 位 / 半字节对齐，便于扩展 |
| Make reset values 0；only reset if necessary | 复位值默认 0，能不复位就不复位（u2-l2 的「默认 `oh_dffq`」即此条产物） |

**Coding Guide（代码层「怎么写」）**——条目极多，可归并为 6 组记忆：

1. **格式**：每行 ≤80 字符；一行只写一个 input/output；用 `//` 单行注释，禁 `/*..*/`；信号名小写、参数/宏大写；多余 4 位的常量加 `_`（如 `8'h1100_1100`）。
2. **命名**：一文件一 module；通用名（`nreset`/`clk`/`din`/`dout`/`en`/`rd`/`wr`/`addr`）；名字「尽量短但不更短」；generate 块用 `g0/g1`，块内实例用 `i<name>`。
3. **可综合性**：只用一组白名单关键字（见下方源码精读）；禁 `casex`（可用 `casez`）；只用可综合结构；算术用 `$signed()`。
4. **时序**：时序逻辑一律非阻塞 `<=`；低有效复位 `nreset`；`case` 必带 `default`；混用时钟沿要隔离到独立模块；异步复位走 `oh_rsync`。
5. **参数与例化**：参数化但「适可而止」；**禁 `defparam`、禁位置式例化**，一律 `#(.DW(DW))` 按名传参、按名连接；设计文件不写 timescale、不写延时。
6. **头文件**：`.vh` 放常量，用 `` `include `` 引入，用 `` `ifndef _CONSTANTS_V `` 守卫只包含一次。

**Documentation Guide（文档层「怎么记」）**——核心是「**每个信号都要进表、都要有波形**」：用 Markdown 写；指明哪些寄存器会复位；标明读写类型；信号总表 + 波形（wavedrom）；寄存器在表里按地址排序、在描述里按字母排序；给出基址、中断表、编译/仿真/综合/使用方法。

#### 4.1.3 源码精读

**三套 Guide 的原文**都集中在 README 的一个区段。先看 Design Guide：

[README.md:L97-L108](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/README.md#L97-L108) — OH! 的架构层家规：前 4 条全是 `Separate ... from ...`，把「解耦」作为第一设计原则；最后两条「复位值默认 0、能不复位就不复位」是 u2-l2 触发器家族设计取舍的直接源头。

再看 Coding Guide 与它末尾那份**白名单关键字**——这是全库「可综合性」的硬约束：

[README.md:L110-L162](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/README.md#L110-L162) — 全部编码家规。最后一行（L162）是关键：`Allowed keywords: assign, always, input, output, wire, reg, module, endmodule, if/else, case, casez, ~,|,&,^,==, >>, <<, >, <,?,posedge, negedge, generate, for(...), begin, end, $signed`。这意味着 OH! 设计文件里**只会出现这一小撮关键字**——没有 `initial`、没有 `fork`、没有 `task`、没有 `casex`。正是这份克制，让同一份代码既能被综合成 ASIC 标准单元（u9-l1/u9-l2），也能在 iverilog 里用 `-g2005` 直接仿真（u1-l3）。

Documentation Guide：

[README.md:L165-L185](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/README.md#L165-L185) — 文档家规。注意「All signals should have waveforms (wavedrom)」与「internal register map」「base address」「table of interrupts」——这些恰好是 `gpio/README.md` 里那张寄存器表的来由。

**一份「完全合规」的样板**——GPIO 模块。`gpio.v` 几乎是 Coding Guide 的活体注释，本讲后续要反复抄它：

[gpio/hdl/gpio.v:L9-L27](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/gpio/hdl/gpio.v#L9-L27) — `` `include `` 引入 regmap、`#(parameter integer N=24, AW=32, PW=104)` 按名声明参数、端口每行一个并逐行注释、`nreset` 低有效。这是「端口列表该怎么写」的范本。

[gpio/hdl/gpio.v:L91-L109](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/gpio/hdl/gpio.v#L91-L109) — 解码段：`assign` + 位切片 + 宏比较产生 one-hot 写选通。每条 `assign` 都标了显式位宽 `[N-1:0]`（Coding Guide「Use vector sizes in every statement」），地址比较统一用 `dstaddr_in[6:3]` 切片（Documentation Guide「Place multi bit fields on nibble boundaries」的体现）。

[gpio/hdl/gpio.v:L213-L223](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/gpio/hdl/gpio.v#L213-L223) — 读回 `case`：**带了 `default`**（Coding Guide「Use default statements in all case statements」），读地址未命中时返回 0（Design Guide「Make reset values 0」的精神）。

[gpio/hdl/gpio.v:L117-L121](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/gpio/hdl/gpio.v#L117-L121) — 时序块：`posedge clk or negedge nreset`（时钟上升沿 + 异步低有效复位）、非阻塞 `<=`、复位写 0。这是 Coding Guide 时序组的教科书写法。

> ⚠️ 一个**违反**规范的活例子，提醒「规范是目标，仓库是现实」：`gpio.v` 第 77 行实例化的是 `enoc_unpack`（旧名），而当前 emesh 库里模块实际叫 `emesh_unpack`（见 `emesh/hdl/emesh_unpack.v`）。这正是 u5-l3、u6-l2 反复提到的「接口迁移未对齐」。读源码以 `emesh/hdl/` 的实际模块名为准；二次开发时你要用的是 `emesh_unpack`，不是 `enoc_unpack`。

#### 4.1.4 代码实践：肉眼 lint 一段 Verilog

**实践目标**：不动用任何 EDA 工具，仅凭 Coding Guide 找出代码违规。

**操作步骤**：

1. 打开 [stdlib/rtl/oh_counter.v:L8-L25](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/rtl/oh_counter.v#L8-L25)（`oh_counter` 的端口段）。
2. 逐条对照 4.1.2 的 6 组规则，列出它**符合**的点（如：参数 `SYN`/`TYPE` 大写、端口逐行注释、信号小写）。
3. 再看它的 always 块 [oh_counter.v:L35-L42](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/rtl/oh_counter.v#L35-L42)，注意 `count` 是 `output reg` 且**没有复位**——结合 Design Guide「Only reset register if absolutely necessary」思考：一个自由运行的计数器是否需要复位？OH! 的选择是「不需要」。

**需要观察的现象**：你会发现 `oh_counter` 没有 `nreset` 端口（对照 `gpio.v` 有）。这不是疏忽，而是规范的有意产物——能不复位就不复位。

**预期结果**：你能用 ≤6 条 bullet 说明 `oh_counter` 如何契合 Design/Coding Guide，并解释它为何省去复位。

**待本地验证**：无（纯阅读型实践）。

#### 4.1.5 小练习与答案

**练习 1**：Coding Guide 禁止两 种例化写法（位置式、`defparam`），请说出「正确的例化三件套」。

**参考答案**：① 按名传参 `#(.DW(DW), .AW(AW))`；② 按名连接 `.clk(clk), .din(din)`；③ 绝不写 `mux3 #(32) U2(...)` 这种位置式参数。

**练习 2**：为什么 Design Guide 要求「Separate design from testbench」且设计文件「No timescales」？

**参考答案**：设计文件要能原样进入 ASIC 综合流程（u9-l1），timescale 与延时语句属于仿真专用，写在设计里会污染综合结果；把 timescale 限制在 testbench（如 `stdlib/testbench/timescale.v`）、把测试台与测试数据分目录，才能让同一份 RTL 既仿真又综合而互不干扰。

**练习 3**：下面这行违反了 Coding Guide 的哪几条？`mux3 #(32) U2 (a, b, sel, z); /*select*/`

**参考答案**：违反了「禁位置式参数（`#(32)`）」「禁位置式连接（`a,b,sel,z` 顺序连接）」「禁 `/*..*/` 多行注释」三条。

---

### 4.2 流片检查

#### 4.2.1 概念说明

「流片（tape-out）」是把版图数据库（GDSII）发给晶圆厂制造的动作——**极其昂贵且几乎不可逆**。一颗芯片从 RTL 到能送厂的版图，要跨越逻辑综合、布局布线、时序收敛、物理验证（DRC/LVS）、可测性设计（DFT）、功耗与信号完整性等十几道关。任何一道关漏掉一个检查项，轻则返工数周，重则整批芯片失效。

`docs/tapeout_checklist.md` 就是 OH! 给出的一份**流片前自检表**：它把上述每一道关拆成一个个 yes/no 问题，按大类列成 Markdown 表格。它的价值不在于「填了就安全」，而在于**强制你逐项想过**——这正是工程上对付「忘记检查」的标准武器。

> 重要心态：清单本身也不完美——例如 `tapeout_checklist.md` 里「Is Verilog 2005 used?」出现了两次（L24 和 L48），「All paths constrained?」也重复了。把它当**起手模板**，而不是金科玉律；遇到本项目不涉及的条目（如「Wirebond pad/pitch」）直接标注 N/A。

#### 4.2.2 核心流程：14 大类与 OH! 的对应

清单把检查项分成 14 大类。我们不必全背，重点掌握那些**与本手册讲过的内容直接挂钩**的类：

```
项目管理 → 规格 → 设计 → 验证 → 时序 → 时钟 → 复位 → 功耗 → IO → IP → 综合 → 版图/PNR → DFM → 测试 → 电路检查
```

其中，**设计 / 时钟 / 复位 / 验证**四类与本手册关系最紧密：

- **设计类**：Verilog 2005、零 `casex`、列出用到的 latch / negedge flop、命名规范、非阻塞赋值、按名例化、无悬浮输入。→ 对应 u1-l4、u2-l2、本讲 4.1。
- **时钟类**：多大比例寄存器做了门控、是否用了集成门控单元（ICG）、列出所有时钟域穿越、**所有 CDC 是否都用了 `oh_fifo_cdc`**。→ 对应 u2-l3（`oh_clockgate`/`asic_clkicgand`）、u3-l2（`oh_fifo_cdc`）、u7-l2/u7-l3（elink 多时钟域）。
- **复位类**：低有效、**异步进入同步退出**、每个时钟域是否都用了 `oh_rsync`、复位扇出。→ 对应 u2-l4（`oh_rsync`「异步生效、同步释放」）。
- **验证类**：100% 代码覆盖率、>24 小时随机向量、随机化时钟频率、FPGA 仿真、形式等价性（HDL ↔ gate level）。→ 对应第 4 单元仿真平台、u4-l2 的 `egen.pl` 随机激励。

> 一个关键认知：**清单里的条目不是抽象口号，而是 OH! 早就给你备好了原语**。「Use of oh_fifo_cdc on all CDCs?」——`oh_fifo_cdc` 在 u3-l2 已讲；「Was oh_rsync used for every clk domain?」——`oh_rsync` 在 u2-l4 已讲。规范、原语、清单三者构成闭环。

#### 4.2.3 源码精读

清单是一张大表，我们挑四个最有代表性的区段精读：

**设计类**——这是 Coding Guide 的「流片版验收单」：

[docs/tapeout_checklist.md:L22-L48](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/docs/tapeout_checklist.md#L22-L48) — 注意条目与 4.1 的 Coding Guide 几乎一一对应：「In Verilog 2005 used?」「Is there zero use of 'casex'?」「Non-blocking used for all states?」「Was instantiation by name used?」「Does each file contain one module」。这说明**Coding Guide 不是写在 README 里就完了，它在流片前要被逐条复核**。条目「Were Latches used? (If so list)」「Were negedge flops used? (If so list)」要求你显式列出例外，因为 latch 与下降沿触发器在时序与测试上都是「异类」，必须单独说明。

**时钟类**——CDC 安全性的验收：

[docs/tapeout_checklist.md:L85-L97](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/docs/tapeout_checklist.md#L85-L97) — 末两条「List of all clock domain crossings?」「Use of oh_fifo_cdc on all CDCs?」「Were custom CDCs used? (if so list)」把跨时钟域当成头等大事：先**穷举**所有跨域点，再**默认**用 `oh_fifo_cdc`，**自定义 CDC 要单独申报**。这正是 u2-l4、u3-l2、u7 的 elink 多域设计的纪律来源。

**复位类**——「异步进入、同步退出」的验收：

[docs/tapeout_checklist.md:L99-L106](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/docs/tapeout_checklist.md#L99-L106) — 「Is reset active low used?」「Is reset of type async entry, sync exit?」「Was oh_rsync used for every clk domain」「Is use of reset minimized?」。前三条对应 u2-l4 的 `oh_rsync` 设计哲学，最后一条对应 Design Guide「Only reset register if absolutely necessary」。复位扇出（fanout）被单列，是因为复位网络巨大时布线与延迟会成为瓶颈。

**验证类**——随机验证与等价性：

[docs/tapeout_checklist.md:L50-L66](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/docs/tapeout_checklist.md#L50-L66) — 「100% HDL Code Coverage?」「>24hrs of random vectors?」「Randomized clock frequencies?」「Was formal equivalence run between HDL/GL?」。「随机化时钟频率」一条尤其值得记——它要求仿真时故意抖动时钟周期，以暴露亚稳态与时序裕量问题，与 u2-l4 讲的「注入仿真随机延迟」是同一思想。

遇到清单里不认识的缩写（如 ERC、EMI、SEU、LATCHUP、ATPG），查术语表：

[docs/chip_glossary.md:L91-L170](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/docs/chip_glossary.md#L91-L170) — Chip Design 段。例如 [L135](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/docs/chip_glossary.md#L135) 把 Metastability 解释为「在不稳定平衡上停留任意长时间的能力」——这正是 u2-l4 同步器要解决的问题。

#### 4.2.4 代码实践：拿 GPIO 过一遍清单

**实践目标**：把流片清单的「设计 / 时钟 / 复位」三类，逐条套到 GPIO 模块上，体会「自检」是怎么操作的。

**操作步骤**：

1. 打开 [gpio/hdl/gpio.v](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/gpio/hdl/gpio.v) 全文。
2. 建一张三列表：`清单条目 | 在 gpio.v 的证据 | 结论（Pass / 需注意）`。
3. 至少填这几行：
   - 「Is Verilog 2005 used?」→ 看 L162 白名单关键字，gpio.v 只用了 `assign`/`always`/`case`/`posedge`/`negedge` 等 → Pass。
   - 「Is reset active low used?」→ 看 [gpio.v:L117](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/gpio/hdl/gpio.v#L117) `negedge nreset` → Pass。
   - 「Use default in all case?」→ 看 [gpio.v:L222](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/gpio/hdl/gpio.v#L222) 读回 case 的 `default` → Pass；但注意：gpio.v 内部并无显式 casez/case 写法（mux 用 `oh_mux4`），所以本条主要落在 stdlib 原语上。
   - 「Were Latches used?」→ gpio.v 用的是 `always @(posedge clk ...)` 边沿触发，无 latch → Pass（latch 仅在 `oh_latq` 等 stdlib 原语里出现，需在 IP 自检时列出）。
   - 「Were custom CDCs used?」→ gpio 用 [gpio.v:L127-L130](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/gpio/hdl/gpio.v#L127-L130) 的 `oh_dsync` 同步输入引脚，这是 stdlib 标准同步器（非 custom CDC），但若 `gpio_in` 与 `clk` 不同域，应申报。

**需要观察的现象**：你会发现 GPIO 几乎全绿——这正是「规范在先、清单在后」的好处：按规范写出来的代码，过清单时几乎不用返工。

**预期结果**：得到一张 ≥6 行的三列自检表。

**待本地验证**：无（阅读 + 判断型实践）。

#### 4.2.5 小练习与答案

**练习 1**：清单「Clock」类要求「List of all clock domain crossings」。一个 elink 模块（u7）至少有哪些时钟域穿越点？

**参考答案**：至少包括：① TX 侧 `sys_clk` → `tx_lclk_div4`（`etx_fifo`，u7-l2）；② RX 侧对端 `LCLK` 恢复出的 `rx_lclk` → `sys_clk`（`erx_fifo`，u7-l3）；③ TX/RX 之间经 `oh_fifo_cdc` 的配置通路（u7-l4）。每一点都应在清单里列名并注明所用 CDC 原语。

**练习 2**：清单「Reset」类要求「async entry, sync exit」。请用一句话解释，并指出 OH! 的对应原语。

**参考答案**：复位信号异步地立即把寄存器清零（entry，不等时钟），但释放时必须等目标时钟同步后再撤（exit），以免释放瞬间卡在亚稳态；对应原语是 `oh_rsync`（u2-l4）。

**练习 3**：清单「Verification」类为何强调「Randomized clock frequencies」？

**参考答案**：固定周期时钟会掩盖 setup/hold 裕量不足与亚稳态问题；故意抖动周期能在仿真阶段暴露这些只在真实硅片上才出现的时序失效，与 u2-l4「同步器注入随机延迟」同源。

---

### 4.3 二次开发流程

#### 4.3.1 概念说明

前两个模块讲的都是「如何评价已有代码」。本模块讲「**如何新增代码**」——这是本讲、也是整本手册的终极目标。

OH! 的二次开发有一套固定套路：**新增一个 emesh 外设 IP，就是复制 GPIO 的骨架，换掉中间的「内核」**。这套套路之所以成立，是因为前 8 个单元建立的所有公共约定——emesh 104 位包、`access`/`wait` 握手、`.vh` regmap、地址译码写选通、`emesh_unpack`/`emesh_readback`、dut 端口契约、`.emf` 激励——把「外设长什么样」完全模板化了。你要做的只是：

1. 想清楚这个 IP 暴露几个寄存器（regmap.vh）；
2. 写出「地址 → 写选通 / 读数据」的组合与时序逻辑（RTL）；
3. 套上 unpack / readback 的 emesh 外壳（RTL）；
4. 套上 dut 端口契约（dut 包装）；
5. 写几行 `.emf` 激励；
6. 用 `build.sh` + `sim.sh` 跑通。

整个流程的「不变外壳」与「可换内核」如下图：

```
              ┌────────────── 仿真平台 dv_top（不变） ──────────────┐
激励 .emf ──► dv_driver ──► access_in/packet_in ──┐
                                                   ▼
                                         ┌─── dut 包装（端口契约不变）
                                         │      │
                                         │      ▼
                                         │  emesh_unpack  ──► 字段
                                         │      │            │
                                         │      ▼            ▼
                                         │  地址译码 ──► 写选通 / 读数据  ◄── 你的「内核」（可换）
                                         │      │
                                         │      ▼
                                         │  emesh_readback ──► packet_out ──► dv_driver(monitor)
                                         └──────────────────────────────────────────┘
```

#### 4.3.2 核心流程：新增 IP 六步法

把上面的思路落成可执行步骤（每步都给出 OH! 的「抄写对象」）：

| 步骤 | 产物 | 抄写对象 | 关键约定 |
|------|------|----------|----------|
| 1. 定寄存器表 | `xxx_regmap.vh` | `gpio_regmap.vh` | 4 位宏映射 `addr[6:3]`，带 include 守卫 |
| 2. 写 RTL 内核 | `xxx/hdl/xxx.v` | `gpio.v` | 端口含 emesh 五件套 + 业务 IO；`emesh_unpack` 解包、`emesh_readback` 回送 |
| 3. 写 dut 包装 | `xxx/dv/dut_xxx.v` | `dut_gpio.v` | 固定端口契约；兜底 `dut_active=1`、`wait_out=0`、`clkout=clk1` |
| 4. 写激励 | `xxx/dv/tests/test_basic.emf` | `gpio/.../test_basic.emf` | 一行一个事务：`datahi_datalo_dstaddr_ctrlmode_access` |
| 5. 编译 | `dut.bin` | `scripts/build.sh` | `iverilog -g2005 -DTARGET_SIM=1 -DCFG_ASIC=0 -f libs.cmd -o dut.bin dut_xxx.v` |
| 6. 运行 | `waveform.vcd` | `scripts/sim.sh` | 软链 `test_0.emf` → 你的 emf，跑 `./dut.bin` |

#### 4.3.3 源码精读：GPIO 样板逐层拆解

**第 1 层：regmap.vh**——软硬件共用的「地址事实源」：

[gpio/hdl/gpio_regmap.vh:L1-L16](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/gpio/hdl/gpio_regmap.vh#L1-L16) — 用 `` `ifndef GPIO_REGMAP_VH_ `` / `` `define `` / `` `endif `` 守卫（Coding Guide「include 只包含一次」），每个寄存器一个 4 位宏对应 `addr[6:3]`（注释写明 `//64 bit registers, maps to addr[6:3]`）。注意宏的右值是寄存器**编号**而非绝对地址——绝对地址由 emesh 系统级路由决定（u6-l1）。

**第 2 层：RTL 内核**——emesh 外壳 + 业务逻辑：

[gpio/hdl/gpio.v:L77-L109](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/gpio/hdl/gpio.v#L77-L109) — 这是「外壳」的标准长相：`emesh_unpack`（gpio 里写作旧名 `enoc_unpack`，见 4.1.3 警示）把 104 位包拆成 `write_in`/`dstaddr_in`/`data_in` 等字段；随后一组 `assign` 用 `reg_write & (dstaddr_in[6:3]==`宏)` 产生各路写选通。**你新增 IP 时，这一段几乎可以照抄，只改宏名。**

[gpio/hdl/gpio.v:L213-L238](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/gpio/hdl/gpio.v#L213-L238) — 读回段：`case(dstaddr_in[6:3])` 选出 `read_data`，再交 `emesh_readback` 装配响应包（它会把 `read_data` 塞进响应包的 data 字段，u5-l3）。**这一段也可以照抄。**真正「IP 特有」的业务逻辑夹在这两段之间（方向控制、边沿中断等，u6-l2）。

**第 3 层：dut 包装**——固定端口契约的适配器：

[gpio/dv/dut_gpio.v:L1-L40](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/gpio/dv/dut_gpio.v#L1-L40) — `dut` 模块名与端口（`dut_active`/`clkout`/`wait_out`/`access_out`/`packet_out` 出，`clk1`/`clk2`/`nreset`/`vdd`/`vss`/`access_in`/`packet_in`/`wait_in` 入）由 `dv_top` 在编译期写死（u4-l3），参数 `N` 是通道数、`PW=2*AW+40`。

[gpio/dv/dut_gpio.v:L57-L83](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/gpio/dv/dut_gpio.v#L57-L83) — 兜底赋值（`dut_active=1`、`wait_out=0`、`clkout=clk1`）+ 实例化真正的 IP 并桥接事务字段。**新增 IP 时，把这里的 `gpio` 换成你的模块名即可。**

> ⚠️ 端口契约的**空模板**在 [stdlib/testbench/dut_template.v:L1-L35](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/testbench/dut_template.v#L1-L35)，但它写得更简略（且把 `clk` 误写成 `clkin1`），实际抄写以 `dut_gpio.v` 为准。

**第 4 层：激励文件**：

[gpio/dv/tests/test_basic.emf:L1-L11](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/gpio/dv/tests/test_basic.emf#L1-L11) — 每行一个事务，五段下划线分隔：`srcaddr_datahi`、`data_datalo`、`dstaddr`、`ctrlmode`、`access/timing`（u4-l2）。第 1–8 行是写事务（`ctrlmode=05`，bit0=1 表写），第 9–11 行是读事务（`ctrlmode=04`，bit0=0 表读）。注意读 GPIO_IN 时 `data_datalo` 是 `DEADBEEF`（占位），真正返回值由 `emesh_readback` 填。

**第 5 层：编译脚本**：

[scripts/build.sh:L15-L19](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/scripts/build.sh#L15-L19) — `iverilog -g2005 -DTARGET_SIM=1 -DCFG_ASIC=0 -f $OH_HOME/scripts/libs.cmd -o dut.bin $1`。`$1` 就是你的 `dut_xxx.v`。`-g2005` 锁标准（u1-l3），两个 `-D` 切换仿真分支与 soft 实现（u9-l1）。

> ⚠️ **现实的坑（必须知道）**：`build.sh` 引用的是 `$OH_HOME/scripts/libs.cmd`，但仓库里**这个文件不存在**——真正的库搜索配置在 [stdlib/testbench/libs.cmd:L1-L32](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/testbench/libs.cmd#L1-L32)。而且该 `libs.cmd` 还列了 `common/`、`memory/`、`accelerator/hdl`、`xilibs/ip` 等**已不存在的目录**（u1-l2、u1-l3）。所以 `build.sh` 不能开箱即跑——你需要把它指向 `stdlib/testbench/libs.cmd`，并删掉其中失效的 `-y` 行。`libs.cmd` 的作用机制：`-y <dir>` 让 iverilog 在例化一个未定义模块时，到该目录按**模块名找同名 .v 文件**（u9-l3）；`+incdir+ <dir>` 补 `` `include `` 头文件的搜索路径。

**第 6 层：运行脚本**：

[scripts/sim.sh:L1-L7](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/scripts/sim.sh#L1-L7) — 把你指定的 emf 软链成 `test_0.emf`（平台固定读这个名字，u4-l2），然后执行 `./dut.bin`，产出 `waveform.vcd`。

#### 4.3.4 代码实践：新建一个「8 位只读计数器」IP（写文件）

> 这是本讲核心实践的**第一半**——把文件写出来。下一节（综合实践）再把它跑起来。

**实践目标**：按 OH! 约定，新建一个最小 emesh 外设 `mycounter`：它内部有一个自由运行的 8 位计数器，软件可以通过读一个寄存器 `MYCOUNTER_VALUE` 取回当前计数值。没有可写寄存器——所以叫「只读计数器」。它麻雀虽小，却完整包含 regmap.vh、emesh 外壳、dut 包装、emf 激励四件套。

**操作步骤**：在仓库根新建目录 `mycounter/`，按下面四个文件创建（以下均为**示例代码，由你新建，仓库中原本不存在**）。骨架完全照抄 4.3.3 的 GPIO 样板。

**文件 1：`mycounter/hdl/mycounter_regmap.vh`**（照抄 [gpio_regmap.vh](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/gpio/hdl/gpio_regmap.vh#L1-L16) 的守卫与宏风格）：

```verilog
// 示例代码（读者新建）
// maps to addr[6:3]
`ifndef MYCOUNTER_REGMAP_VH_
 `define MYCOUNTER_REGMAP_VH_
 `define MYCOUNTER_VALUE 4'h0  // free-running 8-bit counter (read only)
`endif
```

**文件 2：`mycounter/hdl/mycounter.v`**（外壳照抄 [gpio.v:L77-L109](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/gpio/hdl/gpio.v#L77-L109) 与 [L213-L238](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/gpio/hdl/gpio.v#L213-L238)，注意用**正确的** `emesh_unpack` 而非 gpio.v 里的旧名 `enoc_unpack`）：

```verilog
// 示例代码（读者新建）
`include "mycounter_regmap.vh"
module mycounter #(parameter integer AW = 32, // architecture address width
                   parameter integer PW = 104)// packet width
   (
    input            nreset,     // asynchronous active low reset
    input            clk,        // clock
    input            access_in,  // register access
    input [PW-1:0]   packet_in,  // emesh packet in
    output           wait_out,   // pushback from mesh
    output           access_out, // register access
    output [PW-1:0]  packet_out, // emesh packet out
    input            wait_in     // pushback from mesh
   );

   reg  [63:0] read_data;        // readback payload (64b boundary)
   reg  [7:0]  counter;          // free-running 8-bit counter
   wire        reg_read;
   wire        write_in;
   wire [1:0]  datamode_in;
   wire [4:0]  ctrlmode_in;
   wire [AW-1:0] dstaddr_in;
   wire [AW-1:0] srcaddr_in;
   wire [AW-1:0] data_in;

   //---------- emesh unpack (外壳，照抄 gpio.v) ----------
   emesh_unpack #(.AW(AW), .PW(PW)) p2e (
      .write_in   (write_in),
      .datamode_in(datamode_in[1:0]),
      .ctrlmode_in(ctrlmode_in[4:0]),
      .dstaddr_in (dstaddr_in[AW-1:0]),
      .srcaddr_in (srcaddr_in[AW-1:0]),
      .data_in    (data_in[AW-1:0]),
      .packet_in  (packet_in[PW-1:0]));

   assign reg_read = access_in & ~write_in;

   //---------- 内核：自由运行计数器（无复位，合 Design Guide） ----------
   always @(posedge clk)
     counter[7:0] <= counter[7:0] + 8'h1;

   //---------- 读回 mux（外壳，照抄 gpio.v 的 case + default） ----------
   always @(posedge clk)
     if(reg_read)
       case(dstaddr_in[6:3])
          `MYCOUNTER_VALUE : read_data[7:0] <= counter[7:0];
          default          : read_data[7:0] <= 8'h0;
       endcase // case (dstaddr_in[6:3])

   //---------- emesh readback（外壳，照抄 gpio.v） ----------
   emesh_readback #(.AW(AW), .PW(PW)) rback (
      .wait_out  (wait_out),
      .access_out(access_out),
      .packet_out(packet_out[PW-1:0]),
      .nreset    (nreset),
      .clk       (clk),
      .access_in (access_in),
      .packet_in (packet_in[PW-1:0]),
      .read_data (read_data[63:0]),
      .wait_in   (wait_in));

endmodule // mycounter
```

**文件 3：`mycounter/dv/dut_mycounter.v`**（照抄 [dut_gpio.v:L57-L83](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/gpio/dv/dut_gpio.v#L57-L83)，单通道 `N=1`）：

```verilog
// 示例代码（读者新建）
module dut(/*AUTOARG*/
   dut_active, clkout, wait_out, access_out, packet_out,
   clk1, clk2, nreset, vdd, vss, access_in, packet_in, wait_in);

   parameter AW = 32;
   parameter PW = 104;
   parameter N  = 1;            // single emesh channel

   input            clk1;
   input            clk2;
   input            nreset;
   input [N*N-1:0]  vdd;
   input            vss;
   output           dut_active;
   output           clkout;

   input [N-1:0]     access_in;
   input [N*PW-1:0]  packet_in;
   output [N-1:0]    wait_out;
   output [N-1:0]    access_out;
   output [N*PW-1:0] packet_out;
   input [N-1:0]     wait_in;

   wire clk;

   //---------- 兜底 + 时钟（端口契约要求） ----------
   assign dut_active       = 1'b1;
   assign clkout           = clk1;
   assign clk              = clk1;
   assign wait_out[N-1:0]  = 'b0;

   //---------- DUT ----------
   mycounter #(.AW(AW), .PW(PW)) mycounter (
      .wait_out  (wait_out[0]),
      .access_out(access_out[0]),
      .packet_out(packet_out[PW-1:0]),
      .nreset    (nreset),
      .clk       (clk),
      .access_in (access_in[0]),
      .packet_in (packet_in[PW-1:0]),
      .wait_in   (wait_in[0]));

endmodule // dut
```

**文件 4：`mycounter/dv/tests/test_basic.emf`**（照抄 [test_basic.emf](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/gpio/dv/tests/test_basic.emf#L1-L11) 的五段格式，两次读、第二次延迟更久以观察计数增长）：

```
# 示例代码（读者新建）
DEADBEEF_DEADBEEF_00000000_04_0010 // read MYCOUNTER_VALUE (sample 1)
DEADBEEF_DEADBEEF_00000000_04_0040 // read MYCOUNTER_VALUE (sample 2, later)
```

字段含义（u4-l2/u5-l1）：`DEADBEEF`=srcaddr、`DEADBEEF`=data（读时占位）、`00000000`=dstaddr（`addr[6:3]=0` 即 `MYCOUNTER_VALUE`）、`04`=ctrlmode（bit0=0 读，bits[2:1]=10 即 datamode=2 字宽）、`0010`/`0040`=事务间时序延迟。

**需要观察的现象**（写文件阶段）：四个文件的端口名、宏名、地址切片位宽（`[6:3]`）应前后一致；`emesh_unpack`/`emesh_readback` 的连接与 `gpio.v` 完全同构。

**预期结果**：得到一个目录结构 `mycounter/{hdl,dv/tests}/` 与 4 个文件，与 GPIO 的布局一一对应。

**待本地验证**：这些文件能否被综合/仿真，取决于第 5 步（见综合实践）。

#### 4.3.5 小练习与答案

**练习 1**：如果想让 `mycounter` 支持软件写一个「计数使能」寄存器，需要改哪几个文件、各加什么？

**参考答案**：① `mycounter_regmap.vh` 加一个宏 `` `define MYCOUNTER_CTRL 4'h1 ``；② `mycounter.v` 加 `ctrl_write = reg_write & (dstaddr_in[6:3]==`MYCOUNTER_CTRL)` 写选通与一个 `reg en` 的 always 块，并把计数器改为 `if(en) counter <= counter + 1`；③ `test_basic.emf` 加一行写事务 `...._00000001_00000008_05_0010`（dstaddr=8 即 `addr[6:3]=1`，data=1 开使能）。注意 ctrlmode `05` 的 bit0=1 表写。

**练习 2**：为什么 `mycounter.v` 里 `read_data` 声明成 64 位、而计数器只有 8 位？

**参考答案**：因为 `emesh_readback` 的 `read_data` 端口固定取 `[63:0]`（见 [gpio.v:L237](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/gpio/hdl/gpio.v#L237)），且 Design Guide 要求「Use 64b boundaries for scalable registers」。把 8 位计数器放在低字节、高位保持 0，既满足端口宽度又满足对齐约定（这同时修正了 u6-l2 提到的 `gpio.v` 中 `read_data[N-1:0]` 在 N<64 时越界的问题）。

**练习 3**：`build.sh` 里 `-DTARGET_SIM=1` 与 `-DCFG_ASIC=0` 各自触发什么？（提示：u1-l3、u9-l1）

**参考答案**：`-DTARGET_SIM=1` 让带 `` `ifdef TARGET_SIM `` 的仿真专用代码（如某些模块里的仿真断言、`initial` 注入）生效；`-DCFG_ASIC=0` 定义了 `CFG_ASIC` 宏，按 OH! 的 `ifdef` 约定走 soft（RTL）分支而非 hard（ASIC 宏）分支——但注意 u9-l1 指出的陷阱：`ifdef` 只判断宏「是否定义」而不看其值，所以这里 `-DCFG_ASIC=0` 实际上仍令 hard 分支为真，学习时要以源码实际 `ifdef` 写法为准。

---

## 5. 综合实践

> 本实践是整本手册的毕业设计：**把 4.3.4 写出的 `mycounter` 用 `build.sh` / `sim.sh` 跑通，并在波形里看到计数器值被读回**。它会逼你直面「文档与代码漂移」的真实工程场景。

### 5.1 实践目标

端到端完成一次 OH! 风格的「新增 IP → 编译 → 仿真 → 看波形」闭环，并在过程中亲手修复仓库脚本的历史路径问题。

### 5.2 操作步骤

1. **建环境**（u1-l3）：在仓库根 `source setenv.sh`，确认 `echo $OH_HOME` 指向仓库根目录。
2. **写文件**：完成 4.3.4 的四个文件。
3. **修 libs.cmd 路径**（关键）：`build.sh` 找的是 `$OH_HOME/scripts/libs.cmd`，但该文件不存在。两种修法任选其一：
   - 复制一份：`cp stdlib/testbench/libs.cmd scripts/libs.cmd`，然后编辑它，**删掉指向已不存在目录的行**（`common/dv`、`common/hdl`、`memory/dv`、`memory/hdl`、`accelerator/hdl`、`xilibs/ip`），并补一行 `-y ../mycounter/hdl` 让 iverilog 找到 `mycounter.v`；
   - 或临时改 `build.sh` 的 `-f` 参数指向 `stdlib/testbench/libs.cmd`，再在其中加 `-y ../mycounter/hdl` 与 `+incdir+ ../mycounter/hdl`（后者让 `` `include "mycounter_regmap.vh" `` 能被找到）。
4. **编译**：`./scripts/build.sh mycounter/dv/dut_mycounter.v`。这会产出 `dut.bin`。
5. **运行**：`./scripts/sim.sh mycounter/dv/tests/test_basic.emf`。这会软链 `test_0.emf` 并执行 `./dut.bin`，产出 `waveform.vcd`。
6. **看波形**：`./scripts/view.sh`（或 `gtkwave waveform.vcd`）。

### 5.3 需要观察的现象

- **编译期**：iverilog 的报错（若出现）大多来自 `libs.cmd` 里失效目录（提示 `cannot find ...`）或 `dv_top` 平台的接口漂移（u4-l1/u4-l2 指出 `dv_ctrl` 缺失、`stimulus`/`ememory` 对不上）。逐条修。
- **运行期**：若 `dut.bin` 能跑起来，在波形里定位 `packet_out` 信号。两次读事务的响应包里，data 字段（包内 [71:40] 这一段，u5-l1）应当是递增的两个值（第二次 > 第一次），证明计数器在自由运行且被正确读回。
- **判结论**：`oh_simctrl` 会打印 PASSED/FAILED（u4-l2）。读回值递增即功能正确。

### 5.4 预期结果

- 修复后的 `libs.cmd` 不再报缺失目录；
- `dut.bin` 生成成功；
- 波形中两次读响应的 data 字段递增。

### 5.5 待本地验证（重要）

**本仓库的仿真平台无法开箱即跑**，这是全手册反复印证的现实（u1-l3、u4-l1、u4-l2、u6-l2）。你极可能遇到的卡点：

1. `scripts/libs.cmd` 不存在 → 按 5.2 第 3 步修复。
2. `libs.cmd` 列了已删除的目录 → 删行。
3. `dv_top`/`dv_driver`/`stimulus` 之间的接口漂移 → 这是平台层的历史遗留，超出单个 IP 的修复范围；若你只是想验证 `mycounter.v` **自身**的语法与可综合性，可以**绕开 dv_top**，写一个最小的自校验 testbench（参考 `docs/verilog_faq.md` 的复位触发器与 `$dumpfile`/`$dumpvars` 写法），直接例化 `mycounter`、喂一个读事务、检查 `packet_out`。
4. 若只是想做语法检查，`iverilog -g2005 -DTARGET_SIM=1 mycounter/hdl/mycounter.v`（配上 `emesh/hdl` 的 `-y`/`+incdir+`）能快速发现拼写与端口错误，无需整个平台。

> 因此，本实践的**确定可达**部分是：写出 4 个文件、修复 `libs.cmd` 路径、用 `iverilog` 做语法检查。**端到端跑通 dv_top** 标记为「待本地验证」——能否成功取决于你愿意花多少力气修复平台层漂移。这本身就是 OH! 这类开源硬件项目的真实工作方式：**以源码为准，用双手弥合文档与现实的缝隙**。

---

## 6. 本讲小结

- OH! 的设计规范由 README 的 **Design / Coding / Documentation 三套 Guide** 构成，核心分别是「解耦」「只用一份白名单关键字的家规」「每个信号都进表带波形」；它们不是工具配置，而是靠 review 落地的家规。
- Coding Guide 末尾的**白名单关键字**（`assign`/`always`/`case`/`posedge`/`generate`/`$signed` 等）是全库可综合性的硬约束，也是 soft / hard 双实现能成立的根因。
- `docs/tapeout_checklist.md` 把流片前的检查分成 14 大类；其中**设计 / 时钟 / 复位 / 验证**四类与本手册直接挂钩，且每条都对应一个已讲过的 OH! 原语（`oh_rsync`、`oh_fifo_cdc`、`asic_clkicgand`……），规范—原语—清单三者闭环。
- 二次开发有固定套路：**新增一个 emesh 外设 = 复制 GPIO 骨架、换掉中间内核**；六步法为 regmap.vh → RTL（unpack + 内核 + readback）→ dut 包装 → emf → build → sim。
- 新增 IP 的「不变外壳」是 emesh 104 位包 + `access`/`wait` 握手 + dut 端口契约，「可换内核」是地址译码与业务逻辑。
- 现实落差：`build.sh` 指向不存在的 `scripts/libs.cmd`、`libs.cmd` 列了已删除目录、`gpio.v` 用了旧名 `enoc_unpack`、`dv_top` 平台存在接口漂移——**一律以源码实际文本为准**，遇到报错先怀疑这些历史路径问题。

## 7. 下一步学习建议

到这里，你已经读完了 OH! 的全部 9 个单元、从晶体管级（u9-l3）到板级（u9-l4）、从单个触发器（u2-l2）到片上网络（u5）与高速链路（u7）。建议的后续方向：

1. **把综合实践真正跑起来**：以本讲的 `mycounter` 为起点，逐步加上「写使能寄存器」「中断输出」「双时钟域 CDC」，每加一个特性就回查 `tapeout_checklist.md` 对应条目，体会规范如何约束演进。
2. **横向对照真实外设**：重读 `emailbox`、`emmu`、`etrace`（u6-l4）与 `edma`（u8-l3）的 RTL，用本讲的「外壳 + 内核」视角识别它们各自换了哪个内核，巩固模板思维。
3. **进入物理实现**：沿 u9-l1 → u9-l2 → u9-l4 的路线，尝试把一个 soft 模块（如 `oh_fifo_sync`）替换成它的 hard 分支，理解 `SYN`/`TYPE`/`SHAPE` 参数与 `asiclib` 黄金模型的配合。
4. **工具链外延**：README「Recommended Reading」指向了 Verilator（比 iverilog 更快的仿真器）、FuseSoc（EDA 构建管理）、OpenROAD（开源物理实现流程）——当你想把 OH! 模块推进到真正的综合与布局布线时，从这些工具入手。
5. **回归源头**：重读 `README.md` 的 Philosophy——*Make it work, make it simple, make it modular*。本手册的全部细节，最终都是在解释这三句话如何落地成 700+ 个文件。

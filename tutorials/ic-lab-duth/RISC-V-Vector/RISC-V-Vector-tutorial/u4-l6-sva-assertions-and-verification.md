# SVA 断言与验证

## 1. 本讲目标

读完本讲，你应当能够：

- 说清 `sva/` 目录下的断言文件是**如何被"长进"RTL 模块里的**（`` `include `` + `` `ifdef MODEL_TECH `` 注入机制），以及为什么它们只在仿真里存在、综合时消失。
- 看懂并会写 SystemVerilog 断言（SVA, SystemVerilog Assertions）的两类典型写法：用 `$isunknown` 做 **X 检查（不定态检查）**、用 `inside { ... }` 白名单做**操作码合法性检查**。
- 理解 vis / vmu / vrrm / vex 各单元上挂的**属性断言**在保护什么协议不变量（如握手稳定、不重复解锁、不超过最大重映射数）。
- 明确这套断言当前的设计意图与边界：**只用于仿真，尚未进入任何形式验证流程**。

本讲对应专家层（advanced），依赖你已经掌握 u2-l1（`vector_top` 顶层通路）以及前面几讲建立的信号命名（`valid_in`、`instr_in.fu`、`microop`、`reconfigure`、`locked`、`unlock_en` 等）。

## 2. 前置知识

### 2.1 什么是断言（Assertion）

在硬件验证里，**断言**就是一句"声明式"的、由工具持续监视的规则：你描述"在某条件下，某件事必须成立"，仿真器每个时钟沿都会替你检查；一旦违反，就立刻报错并指出在哪一拍、哪一条断言上翻车。它比"用 `if (...) $display` 在 `always` 块里手工查"更省事、更不容易漏，因为：

- 它是**并发（concurrent）**的，挂在模块作用域，与设计逻辑解耦；
- 它有时序算子（`|->`、`|=>`），可以跨周期表达"这一拍成立则下一拍必须……"。

本项目的断言几乎都是**并发断言**，写法模板是：

```systemverilog
assert property (@(posedge clk) disable iff(!rst_n)  <前提> |-> <必须成立>)
    else $error("<模块>:<出错了>");
```

四个关键零件：

| 片段 | 含义 |
|---|---|
| `@(posedge clk)` | 时钟事件，每个上升沿采样一次 |
| `disable iff(!rst_n)` | 复位期间（`rst_n` 为 0）**暂停检查**，避免复位态误报 |
| `\|->` | 非重叠蕴含：**当拍**前提成立，则**当拍**结论必须成立 |
| `\|=>` | 重叠蕴含：**当拍**前提成立，则**下一拍**结论必须成立 |
| `else $error(...)` / `else $fatal(...)` | 失败动作：`$error` 报错继续跑，`$fatal` 报错并终止仿真 |

### 2.2 `$isunknown` 与归约运算符

`$isunknown(expr)` 在 `expr` 含有任何 `x`/`z` 位时返回 1。设计信号里出现 `x`，通常意味着：没初始化、多驱动冲突、或者从非法状态读出——这些在真实硅片上是灾难，必须尽早抓出。

本项目大量使用"归约或" `|signal` 把一个多位/结构体压成 1 位再做 `$isunknown`，例如 `~$isunknown(|instr_in)`：对 packed 结构体 `instr_in` 做按位或归约，只要任一位是 `x`，归约结果就是 `x`，`$isunknown` 即命中。

### 2.3 `MODEL_TECH` 是什么

`MODEL_TECH` 是 **QuestaSim / ModelSim** 在编译时自动定义的宏。它成了一个天然的"是不是在仿真"开关：用 `` `ifdef MODEL_TECH `` 把只在仿真里才有意义的东西（断言、`$error`、`$readmemb`、用于仿真的 `` `include ``）包起来，综合工具（如 Design Compiler / Genus）看不到它们，自然不会被带进网表。这套机制在 u1-l2 已经提过，本讲是它的"主战场"。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [sva/vector_top_sva.sv](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/sva/vector_top_sva.sv) | 顶层断言：入口信号的 X 检查 + 三种 FU 的 microop 白名单 + FXP/bubble 禁用规则 |
| [sva/vex_sva.sv](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/sva/vex_sva.sv) | 执行级 X 检查，用 `generate` 按 lane 展开转发/写回地址与 ticket 的 X 检查 |
| [sva/vis_sva.sv](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/sva/vis_sva.sv) | 发射级 X 检查 + ready/valid 握手"输入必须稳定"的属性断言 |
| [sva/vmu_sva.sv](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/sva/vmu_sva.sv) | 存储单元 X 检查 + 三引擎互斥/仲裁不变量，违规用 `$fatal` |
| [sva/vrrm_sva.sv](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/sva/vrrm_sva.sv) | 重映射级 X 检查 + "重映射次数不超过上限"的致命断言 |
| [sva/vex_pipe_sva.sv](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/sva/vex_pipe_sva.sv) / [sva/vmu_ld_eng_sva.sv](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/sva/vmu_ld_eng_sva.sv) / [sva/vmu_st_eng_sva.sv](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/sva/vmu_st_eng_sva.sv) / [sva/vmu_tp_eng_sva.sv](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/sva/vmu_tp_eng_sva.sv) | 子模块的 X 检查与数据宽度/寻址模式/跟踪状态不变量 |
| 注入点：[rtl/vector/vector_top.sv:380-382](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vector_top.sv#L380-L382) 等 | 每个 RTL 模块在 `endmodule` 前用 `` `ifdef MODEL_TECH `` 包住 `` `include `` 把对应 SVA 文件纳入 |
| [vector_simulator/compile_vector_simulator.do:11](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/vector_simulator/compile_vector_simulator.do#L11) | `+incdir+../sva/` 让 `` `include "xxx_sva.sv" `` 能被找到 |

## 4. 核心概念与源码讲解

### 4.1 断言注入机制：SVA 如何"长进"RTL 模块

#### 4.1.1 概念说明

`sva/` 目录下的每个文件**本身不是一个完整的模块**，它只是一串裸的 `assert property (...)` 语句。这些语句要生效，必须被放进某个 `module ... endmodule` 的内部，因为：

- 并发断言 `assert property` 只能出现在模块/块作用域；
- 断言里直接引用的 `clk`、`rst_n`、`valid_in`、`instr_in` 等名字，必须能在作用域里可见。

本项目用一个极简而统一的手法完成这件事：**在每个 RTL 模块的 `endmodule` 之前，用 `` `include `` 把对应的 SVA 文件文本插入进来**，再用 `` `ifdef MODEL_TECH `` 把这次插入包成"仿真专用"。这样 SVA 文件就**共享了模块作用域里所有的端口与内部信号**，无需额外传参。

#### 4.1.2 核心流程

注入由 RTL 侧的"三行咒语"和编译侧的"一个加号"配合完成：

1. **RTL 侧**（每个被检查的模块都一样）：在 `endmodule` 前写

   ```systemverilog
   `ifdef MODEL_TECH
       `include "vector_top_sva.sv"
   `endif
   ```

   以顶层为例，见 [rtl/vector/vector_top.sv:380-382](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vector_top.sv#L380-L382)。其余模块完全同构：[rtl/vector/vis.sv:424-426](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vis.sv#L424-L426)、[rtl/vector/vex.sv:262-264](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vex.sv#L262-L264)、[rtl/vector/vmu.sv:435-437](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vmu.sv#L435-L437)，以及 vrrm / vex_pipe / vmu_ld_eng / vmu_st_eng / vmu_tp_eng 各自的 `endmodule` 前。

2. **编译侧**：`vlog` 必须知道去哪里找这些被 include 的文件，这就是 [vector_simulator/compile_vector_simulator.do:11](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/vector_simulator/compile_vector_simulator.do#L11) 里三个 `+incdir` 中的 `+incdir+../sva/`（另两个 `rtl/shared/`、`rtl/vector/` 负责 params/structs/vmacros，见 u1-l2）。

3. **运行时**：QuestaSim 自动定义 `MODEL_TECH`，于是 `` `ifdef `` 分支被编译，SVA 语句进入模块作用域，仿真器开始持续监视。综合时没有 `MODEL_TECH`，整个分支被剔除，断言对网表零影响。

一句话总结这条链路：

```
RTL endmodule 前  →  `ifdef MODEL_TECH / `include "x_sva.sv" / `endif
                       （文本展开，断言进入模块作用域，共享信号）
compile_vector_simulator.do  →  +incdir+../sva/  （告诉 vlog 去哪找 SVA 文件）
QuestaSim 运行时  →  自动定义 MODEL_TECH  →  断言生效；综合时无此宏  →  断言消失
```

#### 4.1.3 源码精读

看注入点的上下文，能直观体会"断言长在模块里、看得见模块信号"：

[rtl/vector/vector_top.sv:380-382](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vector_top.sv#L380-L382) —— 顶层模块末尾：

```systemverilog
`ifdef MODEL_TECH
    `include "vector_top_sva.sv"
`endif

endmodule
```

而 `vector_top_sva.sv` 第一条断言就直接用了模块端口 `valid_in`（该端口声明在 [rtl/vector/vector_top.sv:30-32](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vector_top.sv#L30-L32)）：

[sva/vector_top_sva.sv:4-5](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/sva/vector_top_sva.sv#L4-L5)

```systemverilog
assert property (@(posedge clk) disable iff(!rst_n) ~$isunknown(valid_in))
    else $error("x-check:TOP: valid_in");
```

注意它既没有重新声明 `valid_in`，也没有通过参数传入——它"免费"看到了模块端口。这正是 `` `include `` 注入相对于"例化一个独立 check 模块"的最大好处：**零端口映射成本，断言与设计信号一一对应**。

#### 4.1.4 代码实践

1. **实践目标**：亲手确认"没有 `MODEL_TECH` 就没有断言"。
2. **操作步骤**：
   - 打开 [vector_simulator/compile_vector_simulator.do:11](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/vector_simulator/compile_vector_simulator.do#L11) 的 `vlog` 命令，临时给 `vlog` 加一个 `+define+MODEL_TECH`（其实 QuestaSim 默认就有，这里只是显式确认）。
   - 再尝试用 `vlog -filelist ...`（不带 QuestaSim）或想象综合场景：没有 `MODEL_TECH` 时，[rtl/vector/vector_top.sv:380-382](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vector_top.sv#L380-L382) 的 `` `include `` 整段被跳过。
3. **需要观察的现象**：在 QuestaSim 里编译后，用 `vsim -assertdebug` 加载设计，对象窗口里能在 `vector_top` 下看到名为 `assert_...` 的并发断言对象；若无 `MODEL_TECH`，这些对象不存在。
4. **预期结果**：`MODEL_TECH` 在 → 断言对象可见、可命中；`MODEL_TECH` 不在 → 断言对象不存在，综合可干净通过。
5. **待本地验证**：具体断言对象的命名与是否需要 `-assertdebug`，以本地 QuestaSim 版本行为为准。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `` `include "vector_top_sva.sv" `` 放在 `endmodule` **之后**会怎样？

**答案**：SVA 文件里的 `assert property` 会出现在模块作用域之外，编译报错（并发断言必须在模块/过程块内）。这就是为什么注入点必须在 `endmodule` **之前**。

**练习 2**：为什么 SVA 文件自己不写 `module ... endmodule` 包裹？

**答案**：因为它要共享目标 RTL 模块的作用域（直接用 `valid_in`、`clk` 等）。若再裹一层 module，就成了独立模块，必须把所有待检查信号通过端口传进去——几百个端口不可行，违背了"零映射"初衷。

---

### 4.2 X 检查：用 `$isunknown` 守住不定态

#### 4.2.1 概念说明

**X 检查**是最基础也最值钱的一类断言：它不验证"逻辑对不对"，只验证"信号是不是确定值"。仿真里 `x` 的来源很多——寄存器没复位、case 没覆盖全产生锁存、总线多驱动、从无效内存读数——而 `x` 在真实芯片上会随机变成 0 或 1，导致"仿真能过、流片出错"的玄学 bug。X 检查把这些隐患在仿真期就炸出来。

本项目几乎每个 SVA 文件的前半段都是 X 检查，模板高度统一：

```systemverilog
assert property (@(posedge clk) disable iff(!rst_n) ~$isunknown(<信号>))
    else $error("x-check:<模块>: <信号>");
```

含义：除了复位期，`<信号>` 在任何时钟沿都不能含 `x`/`z`，否则报错。对多位信号常先归约：`~$isunknown(|frw_a_addr)`。

#### 4.2.2 核心流程

X 检查在本项目里分三个层次：

1. **顶层入口把关**：检查进入数据通路的指令与握手信号是否干净。
2. **关键控制信号把关**：如 vis 的 `pending`/`locked` 计分板位矩阵、`unlock_en`、转发使能等——这些一旦为 `x`，整条冒险/转发逻辑都会失控。
3. **条件性 X 检查**：用 `en |-> ~$isunknown(...)`，只在"这个信号本拍有意义"时才查它的值，避免对无关拍的空闲端口误报。例如 vex 里"只有转发使能拉高时，转发地址才不能是 `x`"。

#### 4.2.3 源码精读

**顶层入口三连检**——`valid_in`、`instr_in`、`pop` 任何一个为 `x` 都说明上游驱动有问题：

[sva/vector_top_sva.sv:4-11](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/sva/vector_top_sva.sv#L4-L11)

```systemverilog
assert property (@(posedge clk) disable iff(!rst_n) ~$isunknown(valid_in))
    else $error("x-check:TOP: valid_in");

assert property (@(posedge clk) disable iff(!rst_n) valid_in |-> ~$isunknown(|instr_in))
    else $error("x-check:TOP: instr_in");

assert property (@(posedge clk) disable iff(!rst_n) ~$isunknown(pop))
    else $error("x-check:TOP: pop");
```

第二条用了**条件蕴含** `valid_in |-> ...`：只有当指令有效时，才要求整条 `instr_in` 结构体没有 `x`；空闲拍允许它保持旧值或为 `x`。`|instr_in` 把 packed 结构体归约成 1 位再判 unknown，是处理"整条指令"的惯用法。

**条件性、按 lane 展开的 X 检查**——vex 对每条 lane 的转发/写回地址与 ticket 都查，但只在对应使能有效时才查：

[sva/vex_sva.sv:13-28](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/sva/vex_sva.sv#L13-L28)

```systemverilog
generate
    for (genvar k = 0; k < VECTOR_LANES; k++) begin
        assert property (@(posedge clk) disable iff(!rst_n) frw_a_en[k] |-> ~$isunknown(|frw_a_addr))
            else $error("x-check:vex: frw_a_addr");
        ...
        assert property (@(posedge clk) disable iff(!rst_n) wr_en[k] |-> ~$isunknown(|wr_ticket))
            else $error("x-check:vex: wr_ticket");
    end
endgenerate
```

注意三个细节：① `frw_a_en[k]` 是逐 lane 的位，所以用 `generate for` 展开，每条 lane 一组断言；② 地址 `frw_a_addr` 是全 lane 共享的，所以仍对整体 `|frw_a_addr` 归约；③ 条件 `frw_a_en[k] |-> ...` 保证只在"本 lane 本拍真的在转发"时才要求地址干净。

**计分板核心位的 X 检查**——vis 直接盯住 `pending` 和 `locked` 两个位矩阵（它们的语义见 u2-l5）：

[sva/vis_sva.sv:49-53](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/sva/vis_sva.sv#L49-L53)

```systemverilog
assert property (@(posedge clk) disable iff(!rst_n) ~$isunknown(|pending))
    else $error("x-check:vis: pending");

assert property (@(posedge clk) disable iff(!rst_n) ~$isunknown(|locked))
    else $error("x-check:vis: locked");
```

这两个矩阵是发射闸门的数据来源，一旦某位变 `x`，`no_hazards` 就会算出不可预测的值，整条流水可能乱发或不发——所以必须保证它们干净。

**写回值的强约束**——vis 要求"只要声明本拍有写回，地址、ticket、每个活跃 lane 的数据都不能是 `x`"：

[sva/vis_sva.sv:55-64](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/sva/vis_sva.sv#L55-L64)

```systemverilog
assert property (@(posedge clk) disable iff(!rst_n) |wr_en |-> ~$isunknown(|wr_addr))
    else $error("x-check:vis: Address must not be X when a valid writeback is indicated");
...
generate for (genvar k = 0; k < VECTOR_LANES; k++) begin: g_vis_sva_writeback
    assert property (@(posedge clk) disable iff(!rst_n) wr_en[k] |-> ~$isunknown(|wr_data[k]))
        else $error("x-check:vis: writeback data must not be X for active pipes");
end endgenerate
```

这是一类很有价值的"**带前提的强约束**"：`|wr_en`（任一 lane 在写）当拍，地址/ticket 必须确定；具体到某条 lane `wr_en[k]` 时，该 lane 的 `wr_data[k]` 也必须确定。

#### 4.2.4 代码实践

1. **实践目标**：体会"条件性 X 检查"为何比"无条件 X 检查"更少误报。
2. **操作步骤**：
   - 在 [sva/vis_sva.sv:55-59](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/sva/vis_sva.sv#L55-L59) 现有断言下方，**临时**加一条**无条件**版本做对比：
     ```systemverilog
     // 仅用于对比，验证完请删除
     assert property (@(posedge clk) disable iff(!rst_n) ~$isunknown(|wr_addr))
         else $error("x-check:vis(对比): wr_addr 无条件也不能为 X");
     ```
   - 按 u1-l5 的流程跑一遍 vvadd 示例仿真。
3. **需要观察的现象**：对比版本很可能在空闲拍（无写回）命中报错，而原版（带 `|wr_en |->`）不报。
4. **预期结果**：空闲拍 `wr_addr` 可能保留 `x`，无条件断言会误报；带前提的断言只在真写回时查，不误报。**结论：对"仅在有效时才有意义"的信号，必须用 `en |-> ...` 加前提**。验证后请删除对比断言，不要把实验代码留在仓库里。
5. **待本地验证**：空闲拍 `wr_addr` 的实际取值取决于 vis 内部是否给寄存器复位，若 vis 已对 `wr_addr` 复位则两者都不报，需以本地波形为准。

#### 4.2.5 小练习与答案

**练习 1**：为什么所有 X 检查都带 `disable iff(!rst_n)`？

**答案**：复位期间寄存器尚未装载确定值，大量信号合法地为 `x`；若不 disable，复位期会狂报误错。`disable iff(!rst_n)` 让检查只在复位释放后生效。

**练习 2**：`~$isunknown(|wr_data[k])` 里去掉归约符 `|` 写成 `$isunknown(wr_data[k])` 行不行？

**答案**：行，语义等价——`$isunknown` 对多位向量本身就会逐位检查、任一位为 `x` 即返回 1。本项目用 `|signal` 是一种**显式归约风格**，强调"先把向量压成一位再判"，可读性偏好，不是功能必需。

---

### 4.3 操作码合法性：`inside` 白名单断言

#### 4.3.1 概念说明

X 检查保证"信号是确定值"，但确定值也可能是**非法编码**——比如一条 `INT_FU` 指令带了一个保留的、ALU 根本没实现的操作码。这类错误不会产生 `x`，却会让 `case` 落入默认分支，输出垃圾。

**操作码合法性断言**用 `inside { ... }` 运算符给每种功能单元（FU）开一张"合法 microop 白名单"，凡不在表里的编码一律判非法。它是把 u1-l4 讲的操作码枚举 / u4-l5 讲的"译码层映射"反向落到硬件验证层：**译码器可能产生的编码**必须 **⊆ ALU/cache 真正实现的编码**，断言守住这个子集关系。

回顾必要的编码事实（来自 u1-l4、u4-l5）：

- `to_vector.fu` 是 2 位功能单元字段，取自 [rtl/vector/vmacros.sv:7-10](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vmacros.sv#L7-L10)：`MEM_FU=00`、`FP_FU=01`、`INT_FU=10`、`FXP_FU=11`。
- `to_vector.microop` 是 7 位操作码。整数枚举见 [rtl/vector/vstructs.sv:124-155](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vstructs.sv#L124-L155)，其中 `7'b0000000`（`0000000`）是**保留的非法码**（`VADD` 从 `7'b0000001` 起）。
- 访存 microop 的位域含义见 [rtl/vector/vmacros.sv:13-27](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vmacros.sv#L13-L27)：`[6]` 是 `LD_BIT` 区分 load/store，`[5:4]` 是寻址模式，`[3:2]` 是元素宽度。

#### 4.3.2 核心流程

`vector_top_sva.sv` 在顶层入口为四种 FU 各写了一条"前提 → 白名单"断言，结构一致：

```
当 valid_in & 非reconfigure & fu==某FU  →  microop 必须在该 FU 的白名单内
```

- **MEM_FU**：白名单最大，枚举所有合法 load/store/toeplitz 编码（含 `LD_BIT`、寻址模式、宽度的合法组合）。
- **FP_FU**：白名单很小（仅 3 条伪浮点指令），因为浮点 lane 目前是占位实现（见 u2-l7）。
- **INT_FU**：白名单枚举所有已实现的整数操作（与 `v_int_op_t` 枚举对应）。
- **FXP_FU**：直接禁止——定点当前不支持；另外特别禁止 `7'b1111111`，因为它是仿真器专用的"气泡/bubble"哨兵编码，**绝不应出现在真实数据通路上**（气泡应在 driver 就被弹出，见 u4-l4、u4-l5）。

白名单存在一个值得注意的**已知小瑕疵**：MEM_FU 白名单里 `7'b0000000` 等编码被重复列出（[sva/vector_top_sva.sv:16-19](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/sva/vector_top_sva.sv#L16-L19) 等处出现重复项）。`inside` 对重复项无影响（集合语义），但这说明该表可能是手工/脚本生成、未去重，维护时需留意。

#### 4.3.3 源码精读

**MEM_FU 白名单**（节选首尾，完整表见源文件）——顶层断言一上来就给所有访存指令立规矩：

[sva/vector_top_sva.sv:15-66](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/sva/vector_top_sva.sv#L15-L66)

```systemverilog
assert property (@(posedge clk) disable iff(!rst_n)
    (valid_in & ~instr_in.reconfigure & instr_in.fu == `MEM_FU)  |->
    instr_in.microop inside {
                     7'b0000000,
                     ...
                     7'b1000000,
                     7'b1000001,
                     ...
                     7'b1111010
    }
) else $error("Assertion:TOP: invalid microop for MEM_FU");
```

注意前提里 `~instr_in.reconfigure`：`reconfigure` 是特殊的重配指令（见 u2-l3），它的 microop 不服从常规白名单，故排除。

**FP_FU 白名单**很小，印证了"浮点是占位、只够做性能评估"：

[sva/vector_top_sva.sv:69-71](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/sva/vector_top_sva.sv#L69-L71)

```systemverilog
// Only a sample of pseudo FPU instructions are present, mainly used for performance evaluations
assert property (@(posedge clk) disable iff(!rst_n)
    (valid_in & ~instr_in.reconfigure & instr_in.fu == `FP_FU)  |->
    instr_in.microop inside {7'b0000001, 7'b0000010, 7'b0000011}
) else $error("Assertion:TOP: invalid microop for FP_FU");
```

**INT_FU 白名单**覆盖枚举出的全部整数操作（`7'b0000001` 起到 `7'b0100011`，外加归约码 `7'b1000000..7'b1000011`）：

[sva/vector_top_sva.sv:73-114](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/sva/vector_top_sva.sv#L73-L114)（结构同上，枚举更长）。

**FXP 禁用与 bubble 哨兵**——两条收尾断言：

[sva/vector_top_sva.sv:116-120](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/sva/vector_top_sva.sv#L116-L120)

```systemverilog
assert property (@(posedge clk) disable iff(!rst_n) valid_in |-> (instr_in.fu != `FXP_FU))
) else $error("Assertion:TOP: Fixed Point is current not supported");

assert property (@(posedge clk) disable iff(!rst_n)
    (valid_in & (instr_in.fu == `FXP_FU)) |-> (instr_in.microop != 7'b1111111)
) else $error("Assertion:TOP: Illegal encoding: This is used to denote a bubble cycle in the simulator, should not have reached the datapath");
```

第二条尤其重要：`7'b1111111` 是 u4-l4/u4-l5 讲的**气泡指令哨兵**（`reconfigure=1 & fu=11 & microop=1111111 & vl=0 & maxvl=0`），它应当**只在 driver 里被识别并吞掉**，绝不该流到数据通路上。这条断言就是这层"防线"的守护者。

#### 4.3.4 代码实践

> 本节即本讲的主实践任务：**在 `vector_top_sva.sv` 中为一个尚未被 `inside` 列表覆盖的 microop 编码新增一条断言，验证非法编码会在仿真中被捕获并报错。**

1. **实践目标**：亲手新增一条操作码合法性断言，并通过注入非法编码确认它能被仿真器抓到。

2. **操作步骤**：

   a. 先读懂现状：[sva/vector_top_sva.sv:73-114](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/sva/vector_top_sva.sv#L73-L114) 的 INT_FU 白名单**不包含** `7'b0000000`（枚举从 `7'b0000001` 起，`7'b0000000` 是保留非法码，见 [rtl/vector/vstructs.sv:124-155](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vstructs.sv#L124-L155)）。

   b. 在 [sva/vector_top_sva.sv](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/sva/vector_top_sva.sv) 末尾新增一条**显式禁用 `7'b0000000`** 的断言（这是 INT_FU 白名单在逻辑上已经隐含禁止的编码，这里把它写成一条单独、可读的明令禁止）：

   ```systemverilog
   // 新增：显式禁止 INT_FU 上的保留非法码 7'b0000000
   assert property (@(posedge clk) disable iff(!rst_n)
       (valid_in & ~instr_in.reconfigure & instr_in.fu == `INT_FU) |->
       (instr_in.microop != 7'b0000000))
       else $error("Assertion:TOP: reserved illegal microop 7'b0000000 for INT_FU");
   ```

   c. **先验"不误报"**：按 u1-l5 流程正常跑 vvadd/saxpy 示例，预期仿真安静通过——因为合法程序里 INT_FU 不会出现 `7'b0000000`，新断言不假火。

   d. **再验"能抓到"**：在 QuestaSim 里加载设计后，用 `force` 短暂注入非法编码触发它（层次路径以本地 `find` 为准，下面是典型写法）：

   ```tcl
   vsim -assertdebug work.vector_sim_top
   run 2000
   ;# 短暂把入口指令强制成非法组合
   force -deposit /vector_sim_top/dut/instr_in.fu       = 2'b10
   force -deposit /vector_sim_top/dut/instr_in.microop  = 7'b0000000
   force -deposit /vector_sim_top/dut/instr_in.reconfigure = 1'b0
   run 5
   noforce /vector_sim_top/dut/instr_in.fu
   noforce /vector_sim_top/dut/instr_in.microop
   noforce /vector_sim_top/dut/instr_in.reconfigure
   ```

3. **需要观察的现象**：步骤 c 仿真安静通过；步骤 d 在 `run 5` 之后的几个时钟沿，QuestaSim 控制台打印类似 `** Error (suppressible): ... "Assertion:TOP: reserved illegal microop 7'b0000000 for INT_FU"`，同时**已有的** INT_FU 白名单断言（[sva/vector_top_sva.sv:73-114](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/sva/vector_top_sva.sv#L73-L114)）也会一起报 `invalid microop for INT_FU`——两条断言同时命中，证明"新增的明令禁止"与"原白名单"逻辑一致。

4. **预期结果**：注入 `7'b0000000` 后，新断言与白名单断言**都**报错；不注入时**都**安静。由此验证非法编码确实被捕获并报错。

5. **待本地验证**：`force` 的层次路径（`/vector_sim_top/dut/...`）取决于 TB 例化实例名，请先用 `find /vector_sim_top -recursive instr_in` 确认真实路径；QuestaSim 对 `assert ... else $error` 的默认严重级别（Error，可 suppress）与是否 `-assertdebug` 有关，以本地版本为准。

> 注意：本实践会**临时**修改 `sva/vector_top_sva.sv`（这是验证读者的学习练习）。验证完毕请还原该文件，不要把实验断言留在仓库里。**不修改任何 RTL 源码。**

#### 4.3.5 小练习与答案

**练习 1**：白名单断言的前提里为什么有 `~instr_in.reconfigure`？去掉会怎样？

**答案**：`reconfigure` 是重配指令，它复用了 `fu`/`microop` 字段但语义不同，microop 不服从常规白名单。若不排除，重配指令会被误判为非法编码而报错。

**练习 2**：新增的"显式禁止 `7'b0000000`"断言与原 INT_FU 白名单断言是什么关系？

**答案**：**逻辑冗余但表达更显式**。白名单用"穷举合法集"间接排除了 `7'b0000000`；新断言用"直接点名非法集"表达同一约束。前者维护成本高（加新指令要改表），后者针对性强（对重点非法码给独立、可读的错误信息）。两者并存 = 双保险。

**练习 3**：为什么 `7'b1111111` 要单独用一条断言禁止，而不是写进各 FU 的白名单外？

**答案**：因为它是**仿真器专用的气泡哨兵**，跨所有 FU 都应被禁止流到数据通路（见 u4-l4/u4-l5）。把它单独成条、错误信息写明"this is used to denote a bubble cycle"，能让一旦 driver 的气泡过滤逻辑失效时，立刻被人读懂根因。

---

### 4.4 单元属性断言：握手稳定与协议不变量

#### 4.4.1 概念说明

X 检查与白名单检查之外，本项目还有一类**属性断言（property assertion）**，它们表达的不是"信号取值"，而是"**时序协议不变量**"——设计在跨周期交互时必须遵守的契约。典型例子：

- **ready/valid 握手稳定**：一旦 `valid` 拉高但下游 `ready` 未就绪，上游必须**保持** `valid` 与数据不变，直到下游接收。这是所有握手接口的根本约定。
- **资源互斥**：某些资源同一拍只能被一个引擎释放，不能"双解锁"。
- **容量不变量**：队列不能在已满时再接新指令。
- **配额不变量**：动态分配不能超过上限。

这类断言一旦失败，往往意味着设计在罕见场景下违反协议，是 bug 的强信号。本项目对这类断言的**失败动作分两档**：普通违约用 `$error`（如 vis 的握手稳定）；**不可恢复的结构违约**用 `$fatal`（如 vmu/vrrm，一旦命中直接停仿真，因为继续跑结果已不可信）。

#### 4.4.2 核心流程

属性断言按单元分布，各自盯紧本单元最关键的协议：

- **vis**：盯 ready/valid 入口稳定性。
- **vmu**：盯三引擎写回互斥、写回必获授权、容量上限。
- **vrrm**：盯重映射次数不超过物理寄存器能支撑的上限。
- **vmu_ld_eng / vmu_tp_eng**：盯逐元素跟踪状态机不会进入"待办却未激活"的非法态。

#### 4.4.3 源码精读

**vis 入口握手稳定**——`|=>` 跨周期表达"本拍没被接收，下一拍输入不得变"：

[sva/vis_sva.sv:74-78](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/sva/vis_sva.sv#L74-L78)

```systemverilog
assert property (@(posedge clk) disable iff(!rst_n) valid_in & !ready_o |=> valid_in)
    else $error("Assertion:vis: valid_in must stay stable");

assert property (@(posedge clk) disable iff(!rst_n) valid_in & !ready_o |=> $stable(instr_in))
    else $error("Assertion:vis: input data must remain stable");
```

读法：当拍 `valid_in` 有效但 `ready_o` 为 0（下游没接），则**下一拍** `|=>` `valid_in` 必须仍有效、`instr_in` 必须与当拍相同（`$stable`）。这是标准 AXI-like valid/valid+data 稳定协议。vis 这里用 `$error` 而非 `$fatal`——偶尔抖动虽是 bug，但仿真可继续以便收集更多现场。

**vmu 三引擎写回互斥与授权**——违规用 `$fatal`，因为这是"结构上不可恢复"的情景：

[sva/vmu_sva.sv:66-76](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/sva/vmu_sva.sv#L66-L76)

```systemverilog
assert property (@(posedge clk) disable iff(!rst_n) !(load_unlock_en & store_unlock_en))
    else $fatal("vmu: unsupported scenario yet");
...
assert property (@(posedge clk) disable iff(!rst_n) |wb_request |-> |wb_grant)
    else $fatal("vmu: no writeback grant was given");

assert property (@(posedge clk) disable iff(!rst_n) !fifo_ready |-> !ready_o)
    else $fatal("vmu: no new instr can be processed when activity matrix is full");
```

三条分别守住：① load 与 store 不能同拍都解锁（解锁端口时分复用，见 u3-l1）；② 只要三引擎中任一请求写回，就必须有且仅有一个写回授权（`|wb_request |-> |wb_grant`）；③ 在途顺序 FIFO 已满（`!fifo_ready`）时，绝不能再向上游宣称可接新指令（`!ready_o`）。注意 `vmu_sva` 的 X 检查也统一用 `$fatal`（如 [sva/vmu_sva.sv:4-5](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/sva/vmu_sva.sv#L4-L5)），与 vis/vex 的 `$error` 形成对比——这是因为 vmu 一旦信号异常，后续仲裁/unlock 全盘不可信。

**vrrm 重映射配额**——致命断言，防止硬件循环展开耗尽物理寄存器：

[sva/vrrm_sva.sv:37-38](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/sva/vrrm_sva.sv#L37-L38)

```systemverilog
//In the future, an exception could be used to capture this violation. For now assume illegal
assert property (@(posedge clk) disable iff(~rst_n) do_remap |-> (current_remaps < max_remaps))
    else $fatal("Did more remaps than the Max allowed. Use less rdsts or reconfigure");
```

含义：每次做重映射（`do_remap`）时，当前已重映射次数 `current_remaps` 必须小于上限 `max_remaps`（两者在 [rtl/vector/vrrm.sv:190-198](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vrrm.sv#L190-L198) 维护）。违反即说明程序在两次 `reconfigure` 之间用了过多目的寄存器、物理块不够分——错误信息直接给出修复建议（少用 rdst 或插入 reconfigure）。注释也透露了设计意图：未来计划把它做成可恢复的异常，当前先当致命错误。

**vmu_ld_eng 跟踪态不变量**——用一个 `always_comb` 辅助变量聚合"非法态"，再断言其恒为 0：

[sva/vmu_ld_eng_sva.sv:28-46](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/sva/vmu_ld_eng_sva.sv#L28-L46)

```systemverilog
logic flag_illegal;
always_comb begin
    flag_illegal = 0;
    for (int i = 0; i < VECTOR_LANES; i++) begin
        if(pending_elem[0][i] && !active_elem[0][i]) flag_illegal = 1;
        if(pending_elem[1][i] && !active_elem[1][i]) flag_illegal = 1;
    end
end
...
assert property (@(posedge clk) disable iff(!rst_n) !flag_illegal)
    else $error("vmu_ld_eng: Illegal state in the tracking");
```

这是"复杂不变量"的典型写法：把"待办元素 `pending_elem` 已置位但其 `active_elem` 未激活"这种不该出现的状态，用组合逻辑聚合成 `flag_illegal`，再断言它永远为 0。`vmu_tp_eng_sva` 用同样的模式（[sva/vmu_tp_eng_sva.sv:29-39](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/sva/vmu_tp_eng_sva.sv#L29-L39)）。注意这类 SVA 文件里除了 `assert` 还含有 `logic`/`always_comb`——它们之所以合法，正是因为被 `` `include `` 进了模块作用域，相当于在模块里多了几个用于检查的辅助信号。

#### 4.4.4 代码实践

1. **实践目标**：学会用"辅助变量 + 单条断言"表达复杂不变量。
2. **操作步骤**：
   - 仿照 [sva/vmu_ld_eng_sva.sv:28-46](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/sva/vmu_ld_eng_sva.sv#L28-L46) 的写法，在 [sva/vmu_sva.sv](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/sva/vmu_sva.sv) 里新增一个辅助变量 `logic flag_toepl_without_grant`，在 `always_comb` 里检查"toeplitz 引擎请求写回却长期无授权"等你想监视的非法态，再写一条 `assert property ... !flag_xxx else $fatal(...)`。
   - 重新编译运行（`vlog` + `vsim`），跑 vvadd/saxpy。
3. **需要观察的现象**：正常示例下新断言不命中；若你把判定条件写错（过严），会立刻命中 `$fatal` 并停仿真。
4. **预期结果**：能复现"辅助变量 + 断言"模式；过严条件会停仿真，过宽条件不报——以此体会断言**强度**的取舍。验证后还原文件。
5. **待本地验证**：`vmu_sva` 内能否直接新增 `always_comb` 取决于是否与模块内已有同名块冲突，以本地编译为准。

#### 4.4.5 小练习与答案

**练习 1**：vis 的握手稳定断言用 `$error`，vmu 的互斥断言用 `$fatal`，为什么不同？

**答案**：`$error` 报错但仿真继续，适合"违约但现场仍有价值、想多收集几拍"的场景；`$fatal` 报错即停，适合"结构上不可恢复、继续跑结果全不可信"的场景。vmu 的双解锁/无授权意味着仲裁逻辑已坏，继续跑毫无意义，故用 `$fatal`。

**练习 2**：`valid_in & !ready_o |=> valid_in` 用的是 `|=>` 而不是 `|->`，能否对调？

**答案**：不能。`|=>` 表示"当拍未被接收 → **下一拍** valid 仍有效"，是跨周期约束；`|->` 表示"当拍未被接收 → **当拍** valid 有效"，是恒真句（前提已含 `valid_in`），写出来等于废话。握手稳定天然是跨周期性质，必须用 `|=>`。

---

## 5. 综合实践

把本讲三类断言串起来，做一次"**给 vis 加一道完整防线**"的综合练习：

1. **X 检查**：阅读 [sva/vis_sva.sv:1-70](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/sva/vis_sva.sv#L1-L70)，列出它已经为哪些信号做了 X 检查。挑一个**尚未**被检查、但你认为关键的信号（例如某个内部计分板中间信号，需先用 `find`/波形确认其存在与位宽），仿照 [sva/vis_sva.sv:49-53](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/sva/vis_sva.sv#L49-L53) 的写法补一条 X 检查。

2. **操作码合法性**：确认 vis **没有**自己单独的操作码白名单（白名单集中在 `vector_top_sva`）。思考：为什么把白名单放在顶层入口而不是每个子模块各放一份？（提示：顶层是 microop 流入数据通路的**唯一入口**，在这把关一次即可，子模块拿到的都是已过滤的合法码。）

3. **属性断言**：仿照 [sva/vis_sva.sv:74-78](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/sva/vis_sva.sv#L74-L78)，为 vis 的**出口**写一条握手稳定断言（`valid_o & !ready_i |=> valid_o` 与 `|=> $stable(...)`），并决定该用 `$error` 还是 `$fatal`。

4. **验证**：编译运行四个示例（vvadd/saxpy/dot_product/fir），确认你新增的三类断言都不误报；再用 `force` 注入一次非法状态，确认它们能命中。

5. **还原**：练习结束后还原所有 SVA 文件，**不修改任何 RTL**。

通过这次综合练习，你将完整经历"读懂注入机制 → 补 X 检查 → 写协议不变量 → 验证能抓 bug → 还原"的真实验证工程闭环。

## 6. 本讲小结

- `sva/` 下的断言文件不是独立模块，而是靠 `` `ifdef MODEL_TECH `` + `` `include "x_sva.sv" `` 在每个 RTL 模块 `endmodule` 前被**文本插入**，从而共享模块作用域的全部端口与信号；`+incdir+../sva/` 让 include 可被找到（注入点见 [vector_top.sv:380-382](https://github.com/ic-lab-duth-RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vector_top.sv#L380-L382) 等）。
- **X 检查**用 `~$isunknown(...)` 守住"信号不得为不定态"，常配 `en |-> ...` 做条件性检查、配 `generate` 按 lane 展开；复位期一律 `disable iff(!rst_n)` 暂停。
- **操作码合法性**用 `inside { ... }` 白名单给每种 FU 列出合法 microop 集（[vector_top_sva.sv:15-120](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/sva/vector_top_sva.sv#L15-L120)），FXP 整体禁用、`7'b1111111` 气泡哨兵严禁流入数据通路。
- **属性断言**表达跨周期协议不变量：vis 的 ready/valid 稳定（`|=> $stable`）、vmu 的三引擎互斥与授权、vrrm 的重映射配额、load/tp 引擎的跟踪态合法；复杂不变量用"辅助 `always_comb` 变量 + 单条断言"表达。
- 失败动作分两档：普通违约 `$error`（继续跑），结构不可恢复 `$fatal`（停仿真）；vmu/vrrm 因后续结果不可信而偏用 `$fatal`。
- **边界**：正如 [sva/README.md](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/sva/README.md) 所述，这些断言**迄今仅用于仿真，未进入任何形式验证流程**——它们抓"运行时违约"，不证明"所有输入下恒成立"。

## 7. 下一步学习建议

- **性能调优视角**：断言告诉你"没错"，性能指标告诉你"快不快"。建议接着读 u4-l7（性能指标与参数调优），把 `results.log` 里的 `stall_locked`/`stall_pending` 与本讲 vis 的 `locked`/`pending` 计分板对应起来——你会发现**计分板的位**正是**stall 的来源**。
- **验证流程深化**：本项目断言只跑仿真。若想把 `assert property` 推到**形式验证**（如 JasperGold/SymbiYosys，对所有输入数学证明），下一步可研究：哪些断言适合形式化（如 vis 的握手稳定、vmu 的互斥）、哪些因状态空间爆炸不适合——这是把项目从"仿真验证"升级到"形式验证"的关键路径。
- **扩展新指令时同步断言**：参考 u4-l8（示例实战与项目扩展）的"新增一条整数指令"方案，记得在改完 ALU、`v_int_op_t` 枚举、`sim_generator.py` 映射后，**同步把新操作码加进** [sva/vector_top_sva.sv:73-114](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/sva/vector_top_sva.sv#L73-L114) 的 INT_FU 白名单，否则新指令会被自己的断言判非法——这是 u4-l5 强调的"译码层可用 ≠ 执行层可用"在验证层的回响。

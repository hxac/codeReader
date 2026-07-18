# 赋值处理与位掩码

## 1. 本讲目标

本讲承接 u5-l2（`StatementExecutor` 把过程块语句长成 HDL 意图树），放大其中最关键的一类语句——**赋值**——看它如何从 slang 的 `AssignmentExpression` 一路落地成 `Case::Action` 与 `VariableState` 的更新。

读完本讲，你应该能够：

- 说出 `assign_rvalue`、`assign_to_lvalue_with_masking`、`update_variable_state` 三者各自负责什么、调用顺序如何。
- 解释阻塞（`=`）与非阻塞（`<=`）赋值在本仓库里被**差别处理**的位置，以及为什么同一变量混用两种赋值会触发诊断。
- 理解 `mask`（位掩码）在「部分位赋值」（如 `a[i] = x`）中的作用：它如何与 `unmasked_rvalue` 一起被压进 `Case::Action`，又如何用 `$bwmux` 与「背景值」混合。
- 自己跟踪一条赋值语句的完整调用链，并预测它产生的 RTLIL 单元。

> 前置认知提醒：本讲默认你已经读过 u5-l1（`ProceduralContext`/`VariableState` 的可回滚账本）、u5-l2（`StatementExecutor` 与 HDL 意图树）、u4-l2（`LValue` 的五种 descriptor 与 `is_static`）、u3-l4（`Case::Action` 的 `lvalue/mask/unmasked_rvalue` 三件套）以及 u3-l2（`RTLILBuilder::Bwmux`）。

## 2. 前置知识

### 2.1 阻塞赋值与非阻塞赋值

SystemVerilog 过程块里有两种赋值：

| 写法 | 名称 | 语义 |
|------|------|------|
| `a = expr;` | 阻塞赋值（blocking） | 在本过程块的执行流里「立即」生效，后续语句读 `a` 拿到新值。 |
| `a <= expr;` | 非阻塞赋值（nonblocking） | 右值立即求值，但对 `a` 的更新推迟到过程块结束时统一生效（时钟沿采样）。 |

仿真器靠这两条语义实现组合逻辑与触发器。本仓库**不直接仿真**，而是把过程块翻译成 RTLIL `Process`，最后由 Yosys 的 `proc_*` 把 `Process` 降级成门级。正因如此，`=` 与 `<=` 在翻译期就需要走不同的记账路径——这正是本讲的重点。

### 2.2 为什么需要「位掩码」

考虑 `always @* begin a = b; a[1] = x; end`。第二条语句只改写 `a` 的第 1 位，其余位应保持上一条赋值的结果。我们没有为「部分位」单独建信号，而是把整条赋值描述成三件套（见 u3-l4）：

- `lvalue`：被写的「HDL 意图」位（用 `VariableBits` 表达，见 u3-l3）。
- `unmasked_rvalue`：新值。
- `mask`：逐位的「写使能」，`1` 表示这一位真要写新值，`0` 表示保持背景值。

最终落到 RTLIL 的混合用一个 `$bwmux`（按位选择）完成：`结果 = mask ? unmasked_rvalue : 背景值`。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `src/procedural.cc` | 本讲主战场：`assign_rvalue`、`assign_to_lvalue_with_masking`、`update_variable_state`、`do_simple_assign` 与掩码裁剪工具都在这里。 |
| `src/slang_frontend.cc` | `EvalContext` 遇到 `Assignment` 表达式时的入口；`VariableState::set/evaluate/restore` 与 `convert_static` 的实现。 |
| `src/cases.h` | `Case::Action` 三件套结构定义。 |
| `src/builder.cc` | `RTLILBuilder::Bwmux`（按位混合）的实现。 |
| `src/diag.cc` / `src/diag.h` | 阻塞/非阻塞顺序检测诊断的文案与注册。 |
| `tests/various/assign_mixing.ys` | 真实的「混用赋值」负向测试。 |
| `tests/unit/partsel_down.sv` | 真实的「动态部分位写入」用例（`data[sel-:2] = i2`）。 |

## 4. 核心概念与源码讲解

赋值从被求值到改写变量状态，依次经过三层：入口 `assign_rvalue` → 递归摊平左值 `assign_to_lvalue_with_masking` → 状态更新 `update_variable_state`。下面三个最小模块分别拆解这三层。

### 4.1 `assign_rvalue`：赋值的总入口

#### 4.1.1 概念说明

`assign_rvalue` 是 sv-elab 处理一条赋值语句的「门面」。它的输入是一条已经经过 slang 语义分析的 `AssignmentExpression`，外加**已经求值好的右值** `RTLIL::SigSpec rvalue`。它的职责不是求值右值，而是：判断这条赋值是不是可综合、是阻塞还是非阻塞、左值是不是特殊形态（流拼接、赋值模式），最后把普通情况交给 `assign_to_lvalue_with_masking`。

注意它本身**不直接建 RTLIL 单元**，真正建单元（如 `$bwmux`、`$memwr_v2`）的工作下沉到了后两层。

#### 4.1.2 核心流程

```text
assign_rvalue(assign, rvalue):
  1. 若赋值带 intra-assignment 时序控制（如 a <= #3 b）且未开 ignore_timing
     → 报 diag::GenericTimingUnsyn（不可综合）
  2. blocking = !assign.isNonBlocked()        # 区分 = 与 <=
  3. 按左值形态分派：
     - Streaming（{>>{}} 左值）       → streaming_lhs 取位 + do_simple_assign
     - SimpleAssignmentPattern 左值   → 逐元素递归 assign_rvalue
     - 普通左值                       → LValue::analyze + assign_to_lvalue_with_masking（mask 全 1）
```

右值的求值发生在这之前——由 `EvalContext::operator()` 的 `Assignment` 分支先 `(*this)(assign.right())` 求出 `ret`，再调用 `assign_rvalue`。

#### 4.1.3 源码精读

调用入口在 `EvalContext` 遇到 `ExpressionKind::Assignment` 时：先把 `lvalue` 指针指向赋值左值（供下游左值分析取上下文），求值右值，再调用 `assign_rvalue`。

[src/slang_frontend.cc:1243-1263](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L1243-L1263) —— `EvalContext` 把 `Assignment` 表达式翻译为「求值右值 + 调用 `assign_rvalue`」。`ast_invariant(expr, procedural != nullptr)` 强制赋值只能出现在过程块内（连续赋值另走 `connection_lhs`，见 u4-l2）。

`assign_rvalue` 本体：

[src/procedural.cc:439-486](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/procedural.cc#L439-L486) —— 赋值总入口。要点：

- 第 442-443 行：带 intra-assignment 时序控制（`assign.timingControl`）且未开 `ignore_timing` 时报 `GenericTimingUnsyn`。
- 第 445 行：`bool blocking = !assign.isNonBlocked();`——这是全仓库**唯一**判定 `=` 与 `<=` 的位置，这个布尔值会一路传到 `update_variable_state`。
- 第 448-454 行：流拼接左值（`{<<n{...}}` 作左值）走 `eval.streaming_lhs` 取位并 `do_simple_assign`。
- 第 455-480 行：`SimpleAssignmentPattern` 左值（如 `'{a, b, c}` 整体赋值）被拆成对每个元素的递归 `assign_rvalue`，按 bitstream 宽度切片右值。
- 第 481-485 行：普通左值——`LValue::analyze`（u4-l2）拿到左值描述树后，以**全 1 掩码** `RTLIL::SigSpec(RTLIL::S1, rvalue.size())` 调用 `assign_to_lvalue_with_masking`。

注意：进入 `assign_to_lvalue_with_masking` 时 mask 一律是「全 1」。真正出现非平凡掩码（逐位写使能信号）是在递归摊平动态左值时由 `AddressingResolver` 生成的，见 4.2。

#### 4.1.4 代码实践

**实践目标**：确认 `assign.isNonBlocked()` 是 `<=` 的唯一判定点，并观察赋值带时序控制时的诊断。

**操作步骤**：

1. 打开 [src/procedural.cc:439-486](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/procedural.cc#L439-L486)，确认 `blocking = !assign.isNonBlocked()`（第 445 行）。
2. 阅读 `tests/various/delays.ys`（若存在）或自行构造一个带 intra-assignment 延时的过程块：

```systemverilog
// 示例代码：仅供阅读，触发 GenericTimingUnsyn
module top(input clk, output reg q);
    always @(posedge clk)
        q <= #1 1'b1;   // intra-assignment delay
endmodule
```

3. 用 `read_slang` 读取该设计（需已按 u8-l3 构建 `slang.so` 并 `yosys -m slang.so`）。

**需要观察的现象**：应在日志里看到 `GenericTimingUnsyn` 对应的错误信息；若加 `--ignore-timing` 选项则不再报错。

**预期结果**：带 `#N` 的赋值默认不可综合，报错位置即第 442-443 行；该诊断码在 `diag.h` 中声明。若无法本地构建运行，明确标注「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：函数调用里 `output`/`inout` 形参的回写也走 `assign_rvalue`，请找出这个调用点。
**答案**：在 `StatementExecutor::handle_call` 里，对方向为 `Out`/`InOut` 的形参调用 `context.assign_rvalue(arg->as<ast::AssignmentExpression>(), arg_out[i])`，见 [src/statements.h:311-315](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/statements.h#L311-L315)。

**练习 2**：为什么 `assign_rvalue` 接收的 `rvalue` 是**已经求好**的 `SigSpec`，而不是 slang 表达式节点？
**答案**：因为右值求值复用 u4-l1 的 `EvalContext::operator()`，它统一处理二元运算、类型转换、寻址等所有表达式；`assign_rvalue` 只关心「把右值写到左值」，左值才需要特殊分析（u4-l2）。职责分离让两套逻辑互不污染。

---

### 4.2 `assign_to_lvalue_with_masking`：递归摊平左值

#### 4.2.1 概念说明

这个函数解决一个问题：**左值的形态千变万化**（整个变量、拼接、范围选择、成员访问、存储器写），但最终都要改写「某些变量的某些位」。它的做法是**递归**：拿到一棵 `LValue` 描述树（u4-l2 的五种 descriptor），把右值和掩码按结构切分，一路下钻到「单个静态变量」为止，再交给 `update_variable_state`。

它也是 `mask` 真正变得「非平凡」的地方：对动态索引的部分位写，`AddressingResolver`（u4-l3）会把一个标量写使能展开成逐位掩码信号。

#### 4.2.2 核心流程

```text
assign_to_lvalue_with_masking(assign, context, lvalue, rvalue, mask, blocking):
  if lvalue.is_static():                       # 静态捷径
    update_variable_state(lvalue.evaluate_vbits(), rvalue, mask, blocking)
    return
  按 lvalue.descriptor 的种类分派：
    Concatenation  → 按各元素 bitsize 切分 rvalue/mask，逐元素递归
    RangeSelect    → 若 stride==bitsize（单元素）:
                        递归 inner，rvalue=rvalue.repeat(width),
                        mask=resolver.demux(mask, inner_bitsize)
                     否则（一段）:
                        递归 inner，rvalue=resolver.shift_up(rvalue,...),
                        mask=resolver.shift_up(mask,...)
    MemberAccess   → 用 Sx/S0 补齐父变量里「非成员」位，递归 inner
    MemoryWrite    → 直接发 $memwr_v2 单元（存储器路径，见 u7-l1）
```

两条「动态部分位写」的关键变换：

- 单元素动态写 `a[i] = x`：把 1 比特的新值 `x` **复制铺满**整个 `a` 的宽度（`rvalue.repeat`），同时用 `resolver.demux(mask)` 把「写第 i 位」展开成「逐位写使能」。于是下游 `update_variable_state` 收到的是「整变量 `a`、逐位掩码」——只有掩码为 1 的位才真正写入。
- 一段动态写 `a[i+:w] = x`：用 `shift_up` 把右值和掩码按地址移位嵌入正确位置。

#### 4.2.3 源码精读

[src/procedural.cc:354-437](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/procedural.cc#L354-L437) —— 递归摊平左值的核心函数。逐段看：

- 第 358-362 行：静态捷径。`is_static()` 为真时（左值编译期可定，见 u4-l2），直接 `evaluate_vbits()` 折叠成 `VariableBits` 调 `update_variable_state`，不再下钻。
- 第 364-371 行：`Concatenation`，从高位到低位按各元素宽度 `extract` 切片，递归。
- 第 372-383 行：`RangeSelect`，区分「单元素」（`stride == bitsize`，走 `demux` + `repeat`）与「一段」（走 `shift_up`）。`demux`/`shift_up` 来自 u4-l3 的 `AddressingResolver`。
- 第 384-392 行：`MemberAccess`，把成员在父变量里的位段用 `Sx`（右值）/`S0`（掩码）补齐，再递归到父变量。
- 第 393-432 行：`MemoryWrite`，**不再递归**，直接在画布上 `addCell($memwr_v2)`——这是存储器写端口单元，端口优先级靠 `PRIORITY_MASK` 与 `preceding_memwr` 链推导（详见 u7-l1）。其 `EN` 端口正是「掩码按位或」后与 `case_enable()`、`timing.background_enable` 相与的结果（第 426-428 行）。

掩码的「按位或归一」在第 427 行可见一斑：`netlist.Mux(RTLIL::SigSpec(RTLIL::S0, mask.size()), mask, ...)` 把逐位掩码压成单比特写使能。

#### 4.2.4 代码实践

**实践目标**：在一个真实的动态部分位写用例上，看清 `demux` 如何把单比特写使能展开成逐位掩码。

**操作步骤**：

1. 打开真实测试 [tests/unit/partsel_down.sv:67-71](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/tests/unit/partsel_down.sv#L67-L71)，其中 `base2` 模块的过程块是：

```systemverilog
reg [MSB:LSB] data;
always @* begin
    data = i1;          // 整体赋值：mask 全 1
    data[sel-:2] = i2;  // 动态两段写：走 RangeSelect + shift_up
end
```

2. 在 [src/procedural.cc:372-383](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/procedural.cc#L372-L383) 的 `shift_up` 分支下断点（或加一条 `log("masking range-select write\n");`）。
3. 运行该测试（见 [tests/CMakeLists.txt:80-90](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/tests/CMakeLists.txt#L80-L90) 的 `foreach` 注册方式，命令形如 `yosys -m slang.so tests/unit/partsel_down.sv`，待本地验证）。

**需要观察的现象**：`data[sel-:2] = i2` 进入 `RangeSelect` 分支，`stride` 与 `bitsize` 不等，走 `shift_up`，掩码变成一个随 `sel` 移位的逐位信号。

**预期结果**：第二条赋值的 `mask` 不再是全 1，而是一个宽度等于 `data` 的逐位写使能；最终 `update_variable_state` 会用 `$bwmux` 把它和 `data = i1` 的背景值混合。

#### 4.2.5 小练习与答案

**练习 1**：对于 `a[1] = x`（`1` 是静态常量），会走 `assign_to_lvalue_with_masking` 的哪条分支？mask 会是非平凡的吗？
**答案**：`LValue::analyze` 对静态下标判定 `is_static()==true`，直接走第 358-362 行的静态捷径，`evaluate_vbits()` 返回 1 比特 `{a[1]}`，mask 仍是调用方传入的全 1（即 `1'b1`）。**这种情况下 mask 是平凡的**（`is_fully_ones()`）。要看到非平凡掩码，下标必须是动态的（如 `a[sel]`），才会走 `RangeSelect` 的 `demux` 分支。

**练习 2**：为什么 `MemberAccess` 分支里用 `Sx` 补右值、用 `S0` 补掩码？
**答案**：被访问成员之外的位「不关心新值」（`Sx` = don't-care），且「不应被写入」（`S0` 掩码）。这样 `Bwmux(背景, 含 Sx 的右值, 含 S0 的掩码)` 在这些位上保留背景值，符合「只改成员位」的语义。

---

### 4.3 `update_variable_state`：状态更新与顺序检测

#### 4.3.1 概念说明

`update_variable_state` 是赋值链的**终点**，也是最复杂的一层。它做四件事：

1. **裁剪掩码**：把 `mask==0` 的位整体剔除（这些位根本不写）。
2. **阻塞/非阻塞顺序检测**：对静态变量，禁止「先 `<=` 后 `=`」或「先 `=` 后 `<=`」混用。
3. **Initial 过程特殊处理**：`initial` 块里的赋值必须折叠成常量初值。
4. **更新意图树与变量状态**：压一条 `Case::Action`，并按 mask 是否全 1 选择「直接 set」或「`$bwmux` 混合后 set」。

前三件是「正确性护栏」，第四件才是「真正改写状态」。

#### 4.3.2 核心流程

```text
update_variable_state(loc, lvalue, unmasked_rvalue, mask, blocking):
  1. 断言三者等宽
  2. crop_zero_mask(mask, lvalue/rvalue/mask)   # 删掉 mask==0 的位
  3. for 每个 chunk:
       若 Static 变量:
         blocking 且已见过 nonblocking → BlockingAssignmentAfterNonblocking
         blocking 否则               → 记 seen_blocking_assignment
         nonblocking 且 Initial       → NonblockingAssignInInitialUnsupported (之后按 blocking 处理)
         nonblocking 且已见过 blocking → NonblockingAssignmentAfterBlocking
         nonblocking 否则             → 记 seen_nonblocking_assignment
       若 Dummy                      → 跳过（错误兜底）
       其他(Local/EscapeFlag)         → 断言 blocking（AST 不变量）
  4. 若 Initial timing:
       crop_undef_mask → 必须全常量 → 写入 initial_state / initial_locals_state，返回
  5. current_case->actions.push_back(Action{loc, lvalue, mask, unmasked_rvalue})
  6. 若 mask.is_fully_ones():
       vstate.set(lvalue, unmasked_rvalue)              # 捷径：整变量首赋值
     否则:
       background = vstate.evaluate(lvalue)
       mixed = Bwmux(background, unmasked_rvalue, mask) # 按位混合
       vstate.set(lvalue, mixed)
```

**掩码的数学含义**：对每一位 \(i\)，

\[
\text{result}_i = \text{mask}_i \,?\, \text{unmasked\_rvalue}_i : \text{background}_i
\]

这正是 `$bwmux` 的语义（`Y = S ? B : A`，按位）。当 `mask` 全 1 时退化为「直接取新值」，无需评估背景值——这条捷径对 automatic/local 变量的首次赋值尤为关键，因为此时背景值尚不可用。

**关于第 5 步的 `Case::Action`**：它记录的是 **HDL 意图**（左值用 `VariableBits`，而非真实线），不直接进 RTLIL。它的主要消费者是 u6-l3 的锁存器分析：`detect_possibly_unassigned_subset` 读 `actions` 找「从未被写到的位」，`insert_latch_signaling` 再据 `mask` 注入使能/暂存信号。

#### 4.3.3 源码精读

[src/procedural.cc:202-305](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/procedural.cc#L202-L305) —— `update_variable_state` 全函数。逐段：

- 第 205-210 行：宽度断言 + `crop_zero_mask`。两个重载分别在 [src/procedural.cc:170-176](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/procedural.cc#L170-L176)（对 `RTLIL::SigSpec`）与 [src/procedural.cc:178-184](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/procedural.cc#L178-L184)（对 `VariableBits`），从高位往低位删 `mask==S0` 的位，三者同步收缩。
- 第 212-248 行：**顺序检测**。注意它按 `chunk`（同变量连续段）遍历，用 `seen_blocking_assignment` / `seen_nonblocking_assignment` 两张 `Variable→SourceLocation` 映射记录「这个变量之前被哪种赋值写过」。这两张表是 `ProceduralContext` 的私有成员，见 [src/slang_frontend.h:271-272](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.h#L271-L272)。诊断码文案见 [src/diag.cc:283-288](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/diag.cc#L283-L288) 与 [src/diag.cc:317-318](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/diag.cc#L317-L318)。
- 第 250-291 行：**Initial 路径**。`crop_undef_mask` 删掉 `mask==Sx` 的位；右值必须全常量（否则 `ErrorNonconstantInitialEval`）；随后按变量种类写入 `netlist.initial_state`（Static）或 `initial_locals_state`（Local/EscapeFlag），对推断存储器还会触发 `add_memory_init`。**这条路径提前 `return`，不压 `Case::Action`**。
- 第 293 行：压 `Case::Action`——`mask` 与 `unmasked_rvalue` 原样存入，结构定义见 [src/cases.h:56-63](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/cases.h#L56-L63)。
- 第 295-304 行：**掩码混合**。`mask.is_fully_ones()` 走捷径直接 `vstate.set`；否则 `vstate.evaluate` 取背景值（[src/slang_frontend.cc:529-543](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L529-L543)），`netlist.Bwmux(background, unmasked_rvalue, mask)` 按位混合，再 `vstate.set`。

`$bwmux` 的实现：[src/builder.cc:188-205](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/builder.cc#L188-L205) —— 常量掩码时直接按位挑选不建单元，否则 `canvas->addBwmux`。

`do_simple_assign` 是 `update_variable_state` 的「全 1 掩码」便捷封装，常用于逃逸标志等内部赋值：

[src/procedural.cc:307-311](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/procedural.cc#L307-L311) —— 构造 `SigSpec(S1, rvalue.size())` 的全 1 掩码后转调 `update_variable_state`。

#### 4.3.4 代码实践

**实践目标**：用一个真实的负向测试，验证阻塞/非阻塞混用诊断；再用一个真实用例看清 mask 与 unmasked_rvalue 如何压进 `Case::Action`。

**操作步骤（诊断路径）**：

1. 打开真实测试 [tests/various/assign_mixing.ys:1-23](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/tests/various/assign_mixing.ys#L1-L23)。它用 `test_slangdiag -expect "..."` 断言两条错误：
   - 先 `x = 0;` 再 `x <= 1;` → 期望 `non-blocking assignment to variable 'x' is not supported after previous blocking assignment`（对应第 230-234 行的 `NonblockingAssignmentAfterBlocking`）。
   - 先 `y <= 0;` 再 `y = 1;` → 期望 `blocking assignment to variable 'y' is not supported after previous non-blocking assignment`（对应第 215-219 行的 `BlockingAssignmentAfterNonblocking`）。
2. 运行：`yosys -m slang.so tests/various/assign_mixing.ys`（命令待本地验证，构建方式见 u8-l3）。

**操作步骤（掩码路径）**：

3. 对 `always @* begin data = i1; data[sel] = x; end`（动态单比特写），在 [src/procedural.cc:293](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/procedural.cc#L293) 处观察压入的 `Action`：
   - `lvalue` = 整个 `data` 的 `VariableBits`（来自 4.2 的 `demux` 递归展开到 inner）；
   - `mask` = 由 `demux` 生成的逐位写使能（仅 `sel` 指向的那一位为 1）；
   - `unmasked_rvalue` = `x` 复制铺满 `data` 宽度。

**需要观察的现象**：

- `assign_mixing.ys` 两次都「带错通过」——因为 `test_slangdiag -expect` 命中预期诊断后置 `in_succesful_failtest`（机制见 u2-l4）。
- 掩码路径里，第 295 行 `mask.is_fully_ones()` 为假，走第 300-304 行：`Bwmux(data 的背景值, 铺满的 x, 逐位 mask)`，结果是「除 `sel` 位外保持 `i1`、`sel` 位改为 `x`」。

**预期结果**：诊断测试输出两条与 `assign_mixing.ys` 中 `-expect` 字面一致的消息；掩码用例最终在 RTLIL 里生成一个 `$bwmux` 单元，其 `S` 端口连到 `demux` 产生的逐位使能。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `mask.is_fully_ones()` 时可以直接 `vstate.set`，不必调 `vstate.evaluate` 取背景值？
**答案**：全 1 掩码意味着「每一位都写新值」，背景值会被完全覆盖，`Bwmux(background, rvalue, 全1) == rvalue`，所以省掉背景值评估与 `$bwmux` 单元。函数注释（第 296-298 行）还指出：automatic 变量的首次赋值时背景值根本不可用，这条捷径正是为此而设。

**练习 2**：`initial` 块里写 `x <= 1;` 会怎样？
**答案**：进入第 224-229 行：报 `NonblockingAssignInInitialUnsupported`（外加 `NoteIgnoreInitial` 提示用 `--ignore-initial`），然后把 `blocking` 强制改写为 `true` 继续按阻塞处理。文案见 [src/diag.cc:317-318](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/diag.cc#L317-L318)。

**练习 3**：`Dummy` 变量为什么跳过顺序检测？
**答案**：`Dummy` 是错误兜底用的占位变量（见 u3-l3），它不代表真实硬件，对它做阻塞/非阻塞一致性检查没有意义，故第 239-241 行直接跳过。

## 5. 综合实践

把本讲三层串起来，跟踪下面这段真实风格的过程块，画出它的「调用链 + 产物」：

```systemverilog
// 示例代码
module top(input clk, input [1:0] sel, input d, output reg [3:0] q);
    initial q = 4'b0000;          // (A) 初值
    always @(posedge clk) begin
        q[sel] <= d;              // (B) 动态单比特非阻塞写
    end
endmodule
```

请完成：

1. **入口层**：`(B)` 经 `EvalContext::Assignment` → 求 `d` → `assign_rvalue`。指出 `blocking` 的取值，并说明它如何传递到 `update_variable_state`。
2. **摊平层**：`q[sel]` 是动态下标，`LValue::analyze` 得到 `RangeSelect`（`stride==bitsize`）。说明 `assign_to_lvalue_with_masking` 第 372-377 行如何把 `d` 变成「铺满 4 位」、把 mask 变成「`demux` 逐位使能」。
3. **状态层**：进入 `update_variable_state`——`q` 是 Static 且非 Initial，先过顺序检测（`<=` 首次出现，记入 `seen_nonblocking_assignment`），再压 `Case::Action`（mask 非全 1），最后 `Bwmux(背景, 铺满的 d, 逐位 mask)` 后 `vstate.set`。
4. **对比 (A)**：`(A)` 走的是第 250-291 行 Initial 路径，折叠成 `initial_state` 常量，**不压 `Case::Action`、不建 `$bwmux`**。请解释为什么 `initial` 与 `always` 对同一变量的处理如此不同（提示：一个求常量初值、一个建时序电路）。
5. **预言产物**：`(B)` 最终会经过 u6-l1/u6-l2 的时序识别，变成一个带使能的 `$dffe`（或类似）触发器，其 D 输入是 `Bwmux` 的结果。请说明 `mask` 信号如何与 `posedge clk` 一起决定哪些位在时钟沿更新。

> 若本地已构建 `slang.so`（见 u8-l3），可用 `read_slang` 读取上例并 `show` 生成的模块，与你的预言对照（待本地验证）。

## 6. 本讲小结

- 赋值链分三层：**入口** `assign_rvalue`（判定可综合性、区分 `=`/`<=`、分派特殊左值）→ **摊平** `assign_to_lvalue_with_masking`（按 `LValue` descriptor 递归，动态部分位写在此生成非平凡掩码）→ **状态** `update_variable_state`（裁掩码、顺序检测、Initial 折叠、压 `Case::Action` 与 `$bwmux` 混合）。
- `blocking = !assign.isNonBlocked()` 是全仓库唯一的 `=`/`<=` 判定点，这个布尔值随调用链传到终点。
- 对静态变量，`update_variable_state` 用 `seen_blocking_assignment` / `seen_nonblocking_assignment` 两张表禁止混用阻塞与非阻塞，触发 `BlockingAssignmentAfterNonblocking` 或 `NonblockingAssignmentAfterBlocking`；`initial` 块里的 `<=` 单独报 `NonblockingAssignInInitialUnsupported`。
- `mask` 三件套（`lvalue` / `mask` / `unmasked_rvalue`）记录的是 HDL 意图：mask 全 1 时直接 `set`，否则用 `$bwmux` 与背景值按位混合（`Y_i = mask_i ? new_i : bg_i`）。
- `Case::Action` 不直接进 RTLIL，它的消费者是下游锁存器分析（u6-l3）；`MemoryWrite` 分支则直接发 `$memwr_v2`，衔接 u7-l1 的存储器推断。

## 7. 下一步学习建议

- **u6-l1（时序模式识别）**：本讲的 `mask` 与 `unmasked_rvalue` 在 `always` 块里如何被 `TimingPatternInterpretor` 重新解读为触发器/锁存器，是自然的下一站。
- **u6-l3（锁存器推断）**：`detect_possibly_unassigned_subset` 与 `insert_latch_signaling` 正是本讲 `Case::Action` 的直接消费者，读完会明白「为什么要把 mask 存进 HDL 意图树」。
- **u7-l1（存储器推断）**：本讲 `assign_to_lvalue_with_masking` 的 `MemoryWrite` 分支发出的 `$memwr_v2`，在那里与 `InferredMemoryDetector` 串成完整的存储器写端口语义。
- 进阶阅读：[src/procedural.cc:313-352](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/procedural.cc#L313-L352) 的 `substitute_rvalue`，它展示了 `seen_blocking_assignment` 如何反过来决定读一个变量时走 `vstate.evaluate` 还是 `convert_static`（非阻塞变量读旧值）。

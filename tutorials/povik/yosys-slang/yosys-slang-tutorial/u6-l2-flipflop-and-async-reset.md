# 触发器发射与异步复位

## 1. 本讲目标

本讲承接 u6-l1（时序模式分类器 `TimingPatternInterpretor`）。在上一讲里我们已经知道：一个 `always` / `always_ff` 块会被 `interpret` → `handle_always` → `interpret_async_pattern` 「剥洋葱」式地拆成一个**时钟触发**加上零或多个**异步分支**，最后把残骸交给 `handle_ff_process` 落地。本讲就放大这个「落地」环节，回答三个问题：

1. **分类结果怎么变成 RTLIL 单元？** 一个被识别为「触发器型」的 always 块，最终会发出哪些 `$dff` / `$dffe` / `$aldff` / `$aldffe` 单元？
2. **同步复位与异步复位的单元选择有何不同？** 为什么有时用 `$dffe`、有时用 `$aldffe`？
3. **双沿触发和 `iff` 条件的支持边界在哪里？** 哪些写法会被接受、哪些会触发诊断？

学完后你应该能够：拿到一段含时钟与复位的 `always_ff`，预言它会生成哪种触发器单元、各端口连什么信号；并能在源码里定位 `handle_ff_process` 的三段式结构与 `RTLILBuilder` 的触发器封装。

## 2. 前置知识

本讲假设你已经读过 u6-l1，熟悉下列概念。为完整性这里再用一句话复述：

- **边沿触发器（flip-flop, FF）**：在时钟信号指定边沿（上升沿 `posedge` / 下降沿 `negedge`）采样输入 `D`、更新输出 `Q` 的存储元件。Yosys RTLIL 里对应 `$dff`。
- **使能端（enable）**：带使能的触发器 `$dffe` 只在时钟边沿**且**使能 `EN` 有效时采样，等价于 `if (en) Q <= D;` 的时钟块。`@(posedge clk iff en)` 里的 `iff en` 就是这种使能。
- **异步载入（asynchronous load, aload）**：`$aldff` / `$aldffe` 多一个 `ALOAD` 信号与 `AD` 数据端，当 `ALOAD` 有效时寄存器**立刻**（与 clock 无关）载入 `AD`，正好用来建模异步复位/置位。
- **`ProcessTiming`**：u6-l1 引入的时序描述结构体（`src/slang_frontend.h:224`），含 `kind`（`Initial` / `Implicit` / `EdgeTriggered`）、`background_enable`（背景使能位）、`triggers`（敏感边沿列表）。本讲会大量用到它。
- **`AsyncBranch{trigger, polarity, body}`**：u6-l1 「剥洋葱」算法从 `if-else` 里剥出来的异步分支（见 [src/async_pattern.h:25-30](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/async_pattern.h#L25-L30)），`trigger` 用 `VariableBit` 描述以便在建线前匹配。

如果以上概念还模糊，请先回到 u6-l1 复习 `interpret_async_pattern` 的剥洋葱流程。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [src/slang_frontend.cc](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc) | `PopulateNetlist::handle_ff_process`（本讲主角）与 `ProcessTiming::extract_trigger`；也是 `$past` 发射 `$dffe` 的位置 |
| [src/builder.cc](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/builder.cc) | `RTLILBuilder::add_dff` / `add_dffe` / `add_aldff` / `add_aldffe` / `add_dual_edge_aldff` 五个触发器封装 |
| [src/async_pattern.h](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/async_pattern.h) | `TimingPatternInterpretor` 抽象基类，声明 `handle_ff_process` 纯虚方法与 `AsyncBranch` |
| [src/async_pattern.cc](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/async_pattern.cc) | `interpret_async_pattern` 把多触发块剥成「1 个时钟 + N 个异步」，然后调用 `handle_ff_process` |
| [tests/unit/dff.ys](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/tests/unit/dff.ys) | 等价性测试，覆盖 `iff` 使能、异步复位、`$past`，是本讲实践的样本 |

## 4. 核心概念与源码讲解

### 4.1 `handle_ff_process`：把 always_ff 拆成 prologue / 异步 / 同步三段

#### 4.1.1 概念说明

`handle_ff_process` 是抽象基类 `TimingPatternInterpretor` 声明的纯虚方法（[src/async_pattern.h:35-38](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/async_pattern.h#L35-L38)），其唯一实现位于 `PopulateNetlist`（在 `src/slang_frontend.cc` 中）。它接收 `interpret_async_pattern` 剥完后剩下的五样东西：

- `clock`：唯一的时钟 `SignalEventControl`（含 `edge` 与可选 `iffCondition`）；
- `prologue_block` / `prologue_statements`：剥洋葱过程中收集到的、`if-else` **之前**的局部声明/表达式语句（共享给所有分支）；
- `sync_body`：`if-else` 链最末尾的同步语句（剥掉所有异步 `if` 后剩下的 `else` 体）；
- `async`：零或多个 `AsyncBranch`，每个对应一个异步复位/置位。

为什么是「三段」？因为带异步复位的 `always_ff` 在硬件上遵循**复位优先于时钟**的语义：异步复位一到来，寄存器立刻载入复位值、与时钟无关；只有所有异步复位都无效时，时钟边沿才采样同步值。源码据此建三个 `ProceduralContext`（u5-l1 的「工作台」）：

1. **prologue 段**（`EdgeTriggered`，挂在时钟 + 所有异步触发上）——跑共享语句；
2. **每个异步分支**（`Implicit` 即组合上下文）——算出「复位时应载入的值」；
3. **同步段**（`EdgeTriggered`，只挂时钟）——算出「时钟边沿时应采样到的值」（即 D 输入）。

三个段都把各自的 HDL 意图 case 树 `copy_case_tree_into` 到**同一个** `RTLIL::Process` 容器里，最后由第 4 步「驱动发射」为每个被赋值的变量位发出触发器单元。

#### 4.1.2 核心流程

下面是 `handle_ff_process` 的伪代码骨架（省略命名/属性细节）：

```
proc = canvas->addProcess()                              # 一个 RTLIL::Process 容器

# ---- 段 1: prologue（共享语句，边沿触发：时钟 + 所有异步触发）----
prologue_timing.triggers = [clock] + [async.trigger for each async]
跑 prologue_statements  →  copy_case_tree_into(proc)

# ---- 段 2: 每个异步分支（组合上下文 Implicit）----
prior_branch_taken = []
aloads = []
for abranch in async:
    sig_depol = abranch.polarity ? trigger : NOT(trigger)      # 归一为「高有效」
    branch.background_enable = AND(NOT(prior_branch_taken), sig_depol)
    prior_branch_taken.append(sig_depol)                       # 累积互斥条件
    跑 abranch.body  →  copy_case_tree_into(proc)
    aloads.append({trigger, polarity, branch.vstate})          # 记下复位值

if len(aloads) > 1: 报 AloadOne, return                        # 只支持 1 个异步复位

# ---- 段 3: 同步体（边沿触发：仅时钟）----
event_guard = clock.iff ? ReduceBool(iff) : 1
timing.background_enable = AND(NOT(prior_branch_taken), event_guard)
跑 sync_body  →  copy_case_tree_into(proc)

# ---- 段 4: 为每个被同步体驱动的位，发射触发器 ----
for chunk in sync_branch.all_driven():
    assigned = sync_branch.vstate.evaluate(chunk)              # D 输入（同步值）
    if 无 aload:
        双沿 → add_dual_edge_aldff(...) ; 否则 → add_dffe(clk, en, D=assigned, Q=...)
    elif 1 aload:
        按位拆 aldff_q（异步分支赋值过）/ dffe_q（没赋值过）
        aldff_q → add_aldffe(clk, en, aload, D=assigned, Q, AD=aload 复位值)
        dffe_q → 报 MissingAload + add_dffe 兜底（EN 门控为「复位无效时」）
```

这里有一个贯穿全函数的关键变量 `prior_branch_taken`：它把「到目前为止已经点亮过哪些异步条件」累积起来，使每条分支的使能都是「前面的分支都没命中 且 本分支命中」。在只剩 1 个异步分支时，它的作用是让**同步段**的 `background_enable = NOT(异步复位有效) AND iff条件`，从而保证「复位期间不采样」。

#### 4.1.3 源码精读

- **函数签名与 Process 容器创建**：[src/slang_frontend.cc:1850-1861](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L1850-L1861) — `handle_ff_process` 先断言块体是 `TimedStatement`，创建一个 `RTLIL::Process`，并用 `transfer_attrs` 把源码属性挂上去。注意它**只创建一个 Process**，三段都往这一个容器里塞 case 树。

- **prologue 段（边沿触发 = 时钟 + 所有异步触发）**：[src/slang_frontend.cc:1863-1879](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L1863-L1879) — `prologue_timing` 的 `triggers` 同时包含时钟和所有异步触发信号，`convert_static` 把 `VariableBit` 形式的异步触发物化成 1 位线。

- **异步分支循环与 `prior_branch_taken`**：[src/slang_frontend.cc:1881-1910](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L1881-L1910) — `sig_depol` 把触发信号归一为「高有效」（negedge 取反），`branch_timing.background_enable` 用 `LogicAnd(LogicNot(prior_branch_taken), sig_depol)` 算出本分支使能，随后把 `sig_depol` 追加进 `prior_branch_taken`。每个分支体跑完后，其 `vstate`（变量状态账本，u5-l1）被存进 `aloads` 作为「复位值表」。注意 `branch.inherit_state(prologue)` 让分支继承 prologue 的局部变量状态。

- **「只支持 1 个异步复位」限制**：[src/slang_frontend.cc:1912-1915](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L1912-L1915) — 若 `aloads.size() > 1`，发 `diag::AloadOne` 诊断并直接 `return`（不发射任何 FF）。所以本实现只支持**单个**异步复位/置位，多个会报错。

- **同步段与 `background_enable`**：[src/slang_frontend.cc:1917-1932](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L1917-L1932) — `event_guard` 来自时钟的 `iffCondition`（无 iff 则为常量 1）；`timing.background_enable = LogicAnd(LogicNot(prior_branch_taken), event_guard)` 即「无异步复位命中 且 iff 成立」。注意 `sync_branch.timing_matches_process = false`：一旦存在 aload，同步段的时序不再「忠实匹配」原 always 块的敏感列表（因为复位被单独抽走了）。

- **驱动发射主循环**：[src/slang_frontend.cc:1934-2044](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L1934-L2044) — 对 `sync_branch.all_driven()` 的每个被驱动位块，`assigned = sync_branch.vstate.evaluate(...)` 得到同步 D 值，然后按「无 aload / 1 个 aload」分两路选单元（详见 4.2、4.3）。循环末尾把每个被驱动位登记进 `netlist.driven_variables`（[src/slang_frontend.cc:2046-2049](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L2046-L2049)），供锁存器分析等下游消费。

> 旁支：`ProcessTiming::extract_trigger`（[src/slang_frontend.cc:407-444](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L407-L444)）是给 `$check` / `$print` 这类「副作用单元」写 `EN` 与 `TRG*` 触发参数的，与本讲的寄存器驱动单元是两套机制，不要混淆。

#### 4.1.4 代码实践

**目标**：在脑子里把一个真实 always_ff 映射成 `handle_ff_process` 的三段。

**操作步骤**：

1. 打开 [tests/unit/dff.ys](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/tests/unit/dff.ys)，定位第三个用例 `dff_iff03_gate`（第 81-91 行）：

   ```systemverilog
   always_ff @(posedge clk iff en or posedge rst) begin
       if (rst) q <= 4'b0;
       else     q <= d;
   end
   ```

2. 用 u6-l1 的剥洋葱规则手算：敏感列表有两个边沿 `clk`、`rst` → 进入 `interpret_async_pattern`。`if (rst)` 的条件 `rst` 命中触发 `rst`，剥成一个 `AsyncBranch{trigger=rst, polarity=true, body=q<=0}`，剩下 `triggers=[clk]`，同步体 `sync_body = q <= d`。
3. 把结果填进 `handle_ff_process` 的三段骨架：
   - prologue 段：无共享语句；
   - 异步段（rst）：`aloads[0].values` 里 `q=0`，`prior_branch_taken=[rst 高有效]`；
   - 同步段：`event_guard=en`，`background_enable = NOT(rst) AND en`，`assigned = d`。

**需要观察的现象**：手算完毕后，你应该预言出「同步体驱动了 `q` 的全部 4 位，且这 4 位在异步分支里都被赋值（`q<=0`），因此走 `aldff_q` 一路，发出 1 个 `$aldffe`」。具体单元长什么样，留到 4.3 的实践去验证。

**预期结果**：理解「always_ff → 三段 → 驱动发射」这条主线，并能说出 `prior_branch_taken` 在本例中等于「rst 是否有效」。

### 4.2 同步寄存器封装：`add_dff` / `add_dffe` 与降级

#### 4.2.1 概念说明

`RTLILBuilder`（u3-l2 的「画笔」）把 Yosys `canvas->addDff*` 包成语义化方法。本模块关注最朴素的两个：

- **`$dff`**：边沿触发器，端口 `CLK` / `D` / `Q`，参数 `CLK_POLARITY`（1=上升沿）、`WIDTH`。对应 `add_dff`。
- **`$dffe`**：带使能的 `$dff`，多一个 `EN` 端口与 `EN_POLARITY` 参数。对应 `add_dffe`。`EN` 无效时寄存器保持，语义等价于 `if (en) Q <= D;`。

这两个单元覆盖了「无异步复位」的全部场景：`iff` 条件、`$past` 延迟链、纯时钟寄存器都靠它们落地。

#### 4.2.2 核心流程

`add_dffe` 的核心是一条「使能恒有效就降级」的捷径：

```
add_dffe(name, clk, en, d, q, clk_polarity, en_polarity):
    if en 恒为常量 且 等于 en_polarity:      # 使能永远有效 → 不需要 EN
        return add_dff(name, clk, d, q, clk_polarity)
    cell = canvas->addDffe(... clk, en, d, q, clk_polarity, en_polarity ...)
    bless_cell(cell)                          # 盖印 staged_attributes（u3-l2）
```

这条降级是 u3-l2 讲过的「五步模式」里「特化优化」的体现：既然使能永远开着，`$dffe` 就退化为更简单的 `$dff`，少一个端口、下游 `proc` 也更轻松。

#### 4.2.3 源码精读

- **`add_dff`**：[src/builder.cc:515-520](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/builder.cc#L515-L520) — 直接调 `canvas->addDff` 并 `bless_cell`，是最薄的封装。

- **`add_dffe` 与降级**：[src/builder.cc:522-534](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/builder.cc#L522-L534) — 注意第 526 行的判断 `en.is_fully_def() && en.as_bool() == en_polarity`：当 `EN` 是编译期常量且正好等于有效极性时，直接转调 `add_dff`（第 527-528 行）。

- **`handle_ff_process` 里「无 aload」一路调用 `add_dffe`**：[src/slang_frontend.cc:1960-1968](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L1960-L1968) — 这是没有异步复位时的主路径：`CLK=时钟`、`EN=event_guard`（即 `iff` 条件，无 iff 时为常量 1 → 触发上面的降级成 `$dff`）、`D=assigned`、`Q=变量`。

- **`$past` 也复用 `add_dffe`**：[src/slang_frontend.cc:854-864](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L854-L864) — `$past(d, n)` 用 `n` 个首尾相接的 `$dffe` 做延迟链（`EN` 接 `procedural->timing.background_enable`），是 `add_dffe` 的另一个调用点。`tests/unit/dff.ys` 的 `dff_past01`（无 iff）正好命中降级成 `$dff`（对照其 gold 网表第 127-142 行的两个 `$dff`），而 `dff_past02`（带 `iff en`）保持 `$dffe`（gold 第 167-186 行）。

#### 4.2.4 代码实践

**目标**：看懂 `iff` 使能如何变成 `$dffe` 的 `EN` 端口，并验证降级。

**操作步骤**：

1. 读 `tests/unit/dff.ys` 的 `dff_iff01_gate`（第 1-8 行）：`always_ff @(posedge clk iff en) q <= d + 1;`。
2. 对照它手写的 gold 网表（第 10-39 行）：一个 `$add`（`d + 1`）加一个 `$dffe`，`$dffe` 的 `connect \EN \en`、`connect \CLK \clk`、`connect \D $1`（加法结果）、`connect \Q \q`。
3. 在源码里确认这条链：`q <= d + 1` 的右值 `d + 1` 经 `EvalContext` → `RTLILBuilder::Biop($add, ...)` 产生 `$add`（u3-l2/u4-l1）；`iff en` 经 `handle_ff_process` 第 1919-1920 行变成 `event_guard = ReduceBool(en)`，作为 `add_dffe` 的 `EN`（第 1963 行）。

**需要观察的现象**：因为 `en` 是真实输入信号（非常量），`add_dffe` 第 526 行的 `en.is_fully_def()` 为假，**不**降级，发出完整的 `$dffe`。

**预期结果**：你能解释 gold 网表里每个 `connect` 来自源码哪一行。

> 是否真发出这些单元需在本地跑 `read_slang` 验证（待本地验证），但根据源码路径，gold 网表与 gate 应严格一致——这正是等价性测试 `equiv_status -assert` 所断言的。

#### 4.2.5 小练习与答案

**练习 1**：如果把 `dff_iff01` 的 `iff en` 去掉，写成 `always_ff @(posedge clk) q <= d + 1;`，`add_dffe` 会发出什么单元？为什么？

**参考答案**：会降级成 `$dff`。因为去掉 `iff` 后 `event_guard` 为常量 `RTLIL::S1`（[src/slang_frontend.cc:1918](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L1918)），传入 `add_dffe` 的 `en` 满足 `is_fully_def() && as_bool() == en_polarity`，触发第 526-528 行的 `add_dff` 分支。

**练习 2**：`add_dffe` 的 `en_polarity` 参数在 `handle_ff_process` 的「无 aload」路径里固定传 `true`（[src/slang_frontend.cc:1967](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L1967)）。这代表什么？

**参考答案**：代表 `EN` 高有效——`event_guard`（iff 条件归约结果）为 1 时才采样。这与 SystemVerilog `iff` 的语义一致：条件为真时该时钟边沿才生效。

### 4.3 异步复位寄存器：aload 分支与 `add_aldffe`

#### 4.3.1 概念说明

当 always 块有异步复位时，复位值必须**与时钟无关地**载入寄存器，这超出了 `$dffe` 的表达能力。Yosys 提供：

- **`$aldff`**：`$dff` + 异步载入。多端口 `ALOAD`（异步载入使能）与 `AD`（异步载入数据）。当 `ALOAD` 有效，`Q` 立刻变成 `AD`，不等时钟。对应 `add_aldff`。
- **`$aldffe`**：`$aldff` 再加同步使能 `EN`。对应 `add_aldffe`。

在 `handle_ff_process` 里，每个异步分支的变量状态 `aloads[i].values`（一个 `VariableState`）提供「复位时应载入的值」——即 `AD`；同步段的 `vstate` 提供 `D`。但这里有一个**逐位判定**：某个被同步体驱动的位，在异步分支里到底有没有被赋值？

- **被异步分支赋值过** → 归入 `aldff_q`：用 `$aldffe`，`AD` 取异步分支算出的复位值；
- **没被异步分支赋值过** → 归入 `dffe_q`：该位在复位期间「无定义」（仿真里保持、综合里可能保持也可能被优化），触发 `MissingAload` 诊断，并用 `$dffe` 兜底，其 `EN` 被门控为「异步复位无效时」才采样。

#### 4.3.2 核心流程

「1 个 aload」路径的逐位拆分与发射：

```
for 每个被同步驱动的位 b in driven_chunk:
    if b 在 aloads[0].values 里有赋值: aldff_q.append(b)    # 复位有定义
    else:                                  dffe_q.append(b)    # 复位无定义

for named_chunk in aldff_q 拆成的连续段:
    双沿 → add_dual_edge_aldff(...)
    否则 → add_aldffe(clk, EN=event_guard, ALOAD=aloads[0].trigger,
                      D=assigned(同步值), Q=变量位, AD=aloads[0].values.evaluate(复位值),
                      clk_polarity, en_polarity=true, aload_polarity)

for named_chunk in dffe_q: 报 MissingAload
for named_chunk in dffe_q:
    has_event_guard = (iff 存在)
    add_dffe(clk,
             EN = has_event_guard ? timing.background_enable : aloads[0].trigger,
             D=assigned, Q=变量位, clk_polarity,
             en_polarity = has_event_guard ? true : !aloads[0].trigger_polarity)
```

`dffe_q` 兜底分支的 `EN` 选择是本模块最精妙之处：

- **有 `iff`（`has_event_guard`）**：`EN = timing.background_enable = NOT(异步复位) AND iff`，高有效。复位期间不采样，与 aldffe 部分行为对齐。
- **无 `iff`**：`EN = aloads[0].trigger`，极性 `!aloads[0].trigger_polarity`，即「复位**无**效时」才允许采样。等价于「复位期间寄存器保持」，正好弥补该位缺少复位值的缺口。

#### 4.3.3 源码精读

- **`aldff_q` / `dffe_q` 逐位拆分**：[src/slang_frontend.cc:1970-1981](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L1970-L1981) — 遍历 `driven_chunk` 每一位，查 `aloads[0].values.visible_assignments` 决定归类。注意这里用的是 u3-l3 的 `VariableBit` 作键。

- **`aldff_q` 一路发射 `add_aldffe`**：[src/slang_frontend.cc:1983-2016](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L1983-L2016) — `D` 取 `assigned.extract(...)`（同步值的对应位段），`AD` 取 `aloads[0].values.evaluate(netlist, named_chunk)`（异步分支算出的复位值），`ALOAD` 取 `aloads[0].trigger`。双沿则改走 `add_dual_edge_aldff`（4.4）。

- **`dffe_q` 报 `MissingAload` 后兜底**：[src/slang_frontend.cc:2018-2040](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L2018-L2040) — 先对每个 `dffe_q` 段发 `MissingAload` 诊断并附 `NoteDuplicateEdgeSense`（提示该位在另一个边沿敏感下没有对应赋值），再用 `add_dffe` 兜底。第 2031-2038 行的 `has_event_guard` 三元表达式正是上面流程里描述的 `EN` 选择。

- **`add_aldff` / `add_aldffe` 封装与降级**：[src/builder.cc:536-557](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/builder.cc#L536-L557) — `add_aldffe` 同样有「使能恒有效 → 降级为 `add_aldff`」的捷径（第 550-552 行），结构与 `add_dffe` 完全平行。

> 结构体字段会被拆成多个独立 FF：`generate_subfield_names`（u7-l5）把每个 `named_chunk` 配上字段名，于是 `struct` 的不同字段各自得到一个 `$aldffe`，命名形如 `$driver$<var>$<field>$<n>`（[src/slang_frontend.cc:1988](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L1988)）。

#### 4.3.4 代码实践

**目标**：验证含异步复位的 `dff_iff03` 最终发出 `$aldffe`，并与 gold 比对。

**操作步骤**：

1. 回到 `tests/unit/dff.ys` 的 `dff_iff03_gate`（第 81-91 行）。按 4.1.4 的手算，`q` 的 4 位全部在异步分支（`q<=0`）里赋值过，因此全部归入 `aldff_q`，`dffe_q` 为空。
2. 预言单元：1 个 `$aldffe`，参数 `CLK_POLARITY=1`（posedge）、`EN_POLARITY=1`、`ALOAD_POLARITY=1`（rst 上升沿）、`WIDTH=4`；端口 `CLK=clk`、`EN=en`（来自 `iff en`）、`D=d`、`Q=q`、`ALOAD=rst`、`AD=4'b0`。
3. 注意该用例的 gold 用的是 `read_verilog`（第 93-103 行）而非手写 RTLIL，且测试多跑了 `proc; async2sync`（第 105-106 行）再做等价。这是因为在 Yosys 里 `$aldffe` 与 Verilog 前端产出的形式需要先经 `proc` 规整、`async2sync` 把异步逻辑转同步后再比。

**需要观察的现象**：跑 `read_slang` 后用 `show` 或 `dump` 查看顶层模块，应看到一个 `$aldffe` 单元（待本地验证）。注意第 1031-1038 行 `EN` 的取值：本例 `has_event_guard`（iff en 存在）为真，但 `q` 走的是 `aldff_q` 一路，`add_aldffe` 的 `EN` 直接用 `event_guard=en`（第 2005 行），而不是 `background_enable`——因为异步载入本身已经处理了复位，`EN` 只需表达 iff 使能即可。

**预期结果**：理解「异步复位 → `$aldffe`，`ALOAD`/`AD` 来自异步分支，`D`/`EN` 来自同步段」这条对应关系，并能解释为什么 `dff_iff03` 不需要 `MissingAload` 诊断（因为 `q` 所有位在异步分支都有定义）。

#### 4.3.5 小练习与答案

**练习 1**：若把 `dff_iff03` 的异步分支改成 `if (rst) q[3:2] <= 2'b0;`（只复位高 2 位），低 2 位会怎样？

**参考答案**：低 2 位 `[1:0]` 在异步分支里没有赋值，归入 `dffe_q`，触发 `MissingAload` 诊断（[src/slang_frontend.cc:2020-2023](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L2020-L2023)）。最终 `q[3:2]` 用 `$aldffe`，`q[1:0]` 用 `$dffe` 兜底，`EN = NOT(rst) AND en`（因为本例有 iff，`has_event_guard` 为真），即复位期间低 2 位保持不采样。

**练习 2**：为什么 `add_aldffe` 在 `EN` 恒有效时要降级成 `add_aldff`？

**参考答案**：与 `add_dffe` 降级同理——若使能永远开着，`EN` 端口就是冗余的，去掉它可以减少端口、简化下游 `proc` 处理（[src/builder.cc:550-552](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/builder.cc#L550-L552)）。`$aldff` 比 `$aldffe` 少一个 `EN` 端口与 `EN_POLARITY` 参数。

### 4.4 双沿触发与 `iff` 条件：`add_dual_edge_aldff`

#### 4.4.1 概念说明

`EdgeKind::BothEdges`（时钟上升、下降沿都触发，如 `always @(edge clk)`）在真实硬件里几乎没有对应器件，Yosys 也没有原生双沿 FF。sv-elab 用**软件模拟**：「一个 posedge FF + 一个 negedge FF + 一个 mux」，靠 mux 在时钟高低电平之间选通对应 FF 的输出。

因为这种结构代价大且容易引入仿真/综合不一致，双沿触发默认**关闭**：需要命令行 `--allow-dual-edge-ff`（对应 `SynthesisSettings::allow_dual_edge_ff`，[src/slang_frontend.h:507](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.h#L507)）才会被接受，否则在分类阶段就发 `BothEdgesUnsupported`（[src/async_pattern.cc:85-91](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/async_pattern.cc#L85-L91)）。

`iff` 条件的支持边界需要特别记住：

| 触发器位置 | `iff` 支持？ | 依据 |
|------------|-------------|------|
| 单沿**时钟**触发（`posedge clk iff en`） | ✅ 支持，变成 `EN` | [src/slang_frontend.cc:1919-1920](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L1919-L1920)，`dff_iff01/02` 即此例 |
| **异步复位**触发带 `iff` | ❌ `IffUnsupported` | [src/async_pattern.cc:242-243](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/async_pattern.cc#L242-L243) |
| **双沿时钟**带 `iff` | ❌ `IffUnsupported` | [src/slang_frontend.cc:1950-1951](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L1950-L1951)、[src/slang_frontend.cc:1991-1992](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L1991-L1992) |
| 无边沿（电平）带 `iff` | ❌ `IffUnsupported` | [src/async_pattern.cc:66-67](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/async_pattern.cc#L66-L67) |

#### 4.4.2 核心流程

`add_dual_edge_aldff(base_name, clk, aload, d, q, ad, aload_polarity)` 的模拟思路：让两个 FF 都吃同一个 `D`，分别锁存上升沿/下降沿的采样值，再用 `clk` 本身做选择信号。

\[ \text{pos\_q} = \text{采样于 } \uparrow\text{clk},\qquad \text{neg\_q} = \text{采样于 } \downarrow\text{clk} \]

\[ q = \text{clk}\ ?\ \text{pos\_q} : \text{neg\_q} \]

直觉：`clk=1` 时（刚发生过上升沿），输出 `pos_q`；`clk=0` 时（刚发生过下降沿），输出 `neg_q`。代码注释把这条选择规则写得很清楚（见下）。

#### 4.4.3 源码精读

- **`add_dual_edge_aldff` 实现**：[src/builder.cc:471-513](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/builder.cc#L471-L513) —
  - 第 477-481 行建两根内部线 `pos_q` / `neg_q`；
  - 第 483-506 行按「`aload` 是否需要」分两支：`aload` 恒为相反极性（无异步载入）时建两个普通 `$dff`（一个 `edge_polarity=true`、一个 `false`），否则建两个 `$aldff`；
  - 第 510-512 行建 `$mux`：`A=neg_q`、`B=pos_q`、`S=clk`、`Y=q`，即 `Y = clk ? pos_q : neg_q`。注释（第 508-509 行）说明「clk=0 选 neg_q，clk=1 选 pos_q」。

- **`handle_ff_process` 里双沿分支调用它**：[src/slang_frontend.cc:1949-1959](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L1949-L1959)（无 aload 一路）与 [src/slang_frontend.cc:1990-2000](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L1990-L2000)（1 aload 一路）。两处在调用前都先检查 `clock.iffCondition`，若有 `iff` 就发 `IffUnsupported`，再继续——也就是说双沿 + iff 会同时产生一条诊断，但仍会发射双沿模拟电路（诊断只是警告/错误，不中断发射）。

- **默认关闭与 `--allow-dual-edge-ff`**：[src/async_pattern.cc:85-91](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/async_pattern.cc#L85-L91) — `BothEdges` 默认发 `BothEdgesUnsupported` 且 `break`（不加入 `triggers`），只有显式开启才会把它当触发器推下去。

#### 4.4.4 代码实践

**目标**：观察双沿触发的软件模拟结构。

**操作步骤**：

1. 自构一个最小用例（**示例代码**，非项目原有测试）：

   ```systemverilog
   module dual_edge(input clk, input [3:0] d, output logic [3:0] q);
       always @(edge clk) q <= d;
   endmodule
   ```

2. 用 `read_slang --allow-dual-edge-ff` 读入（命令效果待本地验证，因为仓库测试里没有现成的双沿用例）。
3. 用 `show` 或 `dump` 查看生成的模块。

**需要观察的现象**：应看到两个 FF（一个 posedge、一个 negedge）共用 `D=d`，输出经一个 `$mux` 由 `clk` 选择后送 `q`，命名带 `$pos$q` / `$neg$q` / `$mux` 后缀（来自 [src/builder.cc:478-481](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/builder.cc#L478-L481) 与 [src/builder.cc:510](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/builder.cc#L510)）。若不加 `--allow-dual-edge-ff`，应只看到一条 `BothEdgesUnsupported` 诊断、没有 FF。

**预期结果**：直观理解「双沿 = 2× 单沿 FF + mux」，并记住默认禁用、需开关开启。

> 本地验证提示：仓库 `tests/` 目前没有双沿等价性测试，故生成结果需自行跑 `read_slang` 确认（待本地验证）。

#### 4.4.5 小练习与答案

**练习 1**：为什么双沿 FF 要用两个 FF 而不是一个？

**参考答案**：一个物理 FF 只能锁存一个边沿。要在两个边沿都「采样」，就得用两个分别锁存上升/下降沿的 FF，再用 mux 在 `clk` 高低电平期间选通对应那个的输出（[src/builder.cc:471-513](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/builder.cc#L471-L513)）。这种结构功耗/面积代价大且易产生仿真与综合不一致，所以默认禁用。

**练习 2**：`always_ff @(posedge clk iff en or posedge rst)` 里，`iff en` 和 `posedge rst` 分别落到 `$aldffe` 的哪个端口？

**参考答案**：`iff en` 是时钟触发上的 iff → 落到同步使能端口 `EN`（[src/slang_frontend.cc:2005](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L2005)）；`posedge rst` 经剥洋葱成为异步分支 → 落到 `ALOAD`，其复位值 `q<=0` 落到 `AD`（[src/slang_frontend.cc:2007-2010](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L2007-L2010)）。

## 5. 综合实践

把本讲三个模块串起来，做一个「读 always_ff，画电路」的小任务。

**任务**：阅读下面这段 SystemVerilog（**示例代码**），按本讲学到的流程，手写预言它生成的 RTLIL 单元，再用 `read_slang` 验证。

```systemverilog
module ex(input logic clk, input logic rst_n, input logic en,
          input logic [7:0] d, output logic [7:0] q);
    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n)
            q <= 8'hFF;
        else if (en)
            q <= d + 8'd2;
    end
endmodule
```

**操作步骤**：

1. **分类**（u6-l1）：两个边沿 `clk`（posedge）、`rst_n`（negedge）→ `interpret_async_pattern`。`if (!rst_n)` 条件归一后命中 `rst_n`，极性为 negedge → 剥成 `AsyncBranch{trigger=rst_n, polarity=false, body=q<=8'hFF}`，剩下 `triggers=[clk]`，同步体为 `if (en) q <= d + 8'd2;`（注意：原 `else if` 在剥掉外层 `if` 后，`iff`/`en` 表现为同步段里的条件赋值）。
2. **三段**（4.1）：异步段算出复位值 `q=8'hFF`，`prior_branch_taken=[!rst_n 即 rst_n 无效]`（注意 negedge 归一：`sig_depol = NOT(rst_n)`）；同步段无 `iff`，`event_guard=1`，`background_enable = NOT(NOT(rst_n)) = rst_n`。
3. **驱动发射**（4.3）：`q` 的 8 位都在异步分支赋值 → 全部 `aldff_q`，发 1 个 `$aldffe`。`D = en ? (d+2) : q`（同步段里 `if(en)` 的条件由 `ProceduralContext` 的 case 树体现，最终 D 输入是一个 mux 结果），`ALOAD = rst_n`，`ALOAD_POLARITY = 0`（低有效），`AD = 8'hFF`，`EN = 1`（无 iff，可能降级成 `$aldff`）。
4. **验证**：用 `read_slang` 读入，`prep` 或 `proc` 后 `show`，比对是否为单个 `$aldffe`（或降级的 `$aldff`）+ 一个算 `d+2` 的 `$add` + 选择 mux（待本地验证）。

**需要观察的现象与预期结果**：你预言的单元种类（`$aldffe` / `$add` / `$mux`）与端口连接应与 `read_slang` 实际输出一致；复位极性参数 `ALOAD_POLARITY` 应为 `0`（因为 `rst_n` 低有效，`aloads[0].trigger_polarity=false`，见 [src/slang_frontend.cc:2013](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L2013)）。

## 6. 本讲小结

- `handle_ff_process` 把「触发器型」always 块拆成 **prologue（共享）/ 异步分支（组合）/ 同步（边沿）** 三段，三段都塞进**同一个** `RTLIL::Process`，最后为每个被同步驱动的位发射触发器单元。
- `prior_branch_taken` 累积「已点亮的异步条件」，使每条分支互斥，并让同步段在复位期间停止采样（`background_enable = NOT(复位) AND iff`）。
- 无异步复位时用 `add_dffe`（`iff` 落到 `EN`），且「使能恒有效」会降级为 `add_dff`；`add_dffe`/`add_aldffe` 都遵循这条降级捷径。
- 有异步复位时按位判定：异步分支赋值过的位走 `add_aldffe`（`ALOAD`/`AD` 取自异步段），没赋值的位走 `dffe_q` 兜底并报 `MissingAload`；本实现**只支持 1 个异步复位**，多于 1 个发 `AloadOne`。
- 双沿触发默认禁用（`--allow-dual-edge-ff` 开启），用「2× 单沿 FF + mux」软件模拟；`iff` 仅在单沿时钟触发上支持，异步复位触发与双沿触发带 `iff` 都会发 `IffUnsupported`。

## 7. 下一步学习建议

- **下一讲 u6-l3（锁存器推断）**：本讲只覆盖了「触发器型」与「组合型」里能稳定识别为 FF 的情形；当一个组合型 always 块里某些位在所有路径上都没被赋值，就会进入锁存器推断，由 `handle_comb_like_process` + `insert_latch_signaling` 发出 `$dlatch`。建议接着读 `src/slang_frontend.cc` 中 `handle_comb_like_process` 与 `detect_possibly_unassigned_subset`。
- **顺带复习**：u3-l2（`RTLILBuilder` 的五步模式与 `bless_cell`/`AttributeGuard`）、u5-l1（`ProceduralContext` / `VariableState`，理解 `inherit_state` 与 `vstate.evaluate` 如何给出 D 与 AD 值）。
- **源码延伸**：想了解副作用单元（`$check` / `$print`）如何拿时序触发，可读 `ProcessTiming::extract_trigger`（[src/slang_frontend.cc:407-444](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L407-L444)），与本讲寄存器驱动单元的发射对照。

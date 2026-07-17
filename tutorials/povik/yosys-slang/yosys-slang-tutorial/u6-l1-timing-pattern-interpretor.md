# TimingPatternInterpretor：识别 always 块的时序模式

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清楚 sv-elab 在拿到一个 `always` / `always_ff` / `always_comb` / `always_latch` / `initial` 过程块后，**第一步**做了什么——把它的「时序外形」归类为组合型、触发器型还是 initial 型。
- 读懂 `TimingPatternInterpretor` 这个类的骨架：它用纯虚函数把「分类」和「落地」拆成两层，子类 `PopulateNetlist` 只负责落地。
- 跟着 `interpret` → `handle_always` → `interpret_async_pattern` 的调用链，解释一段 `always_ff @(posedge clk or posedge rst)` 是如何被拆成「时钟分支」和「异步复位分支」的。
- 理解 `AsyncBranch` 这个数据结构是如何把一个 `if (rst)` 分支连同它的触发性极性打包起来的。
- 知道 `ProcessTiming`（`triggers` 与 `background_enable`）是如何把分类结果传递给下游的。

本讲是单元 6（时序逻辑）的总纲。它只解决「**这块 always 到底是什么类型的电路**」这一个分类问题，至于触发器单元怎么发（`$dffe`/`$aldffe`）、锁存器怎么推断，留给 u6-l2 与 u6-l3。

## 2. 前置知识

本讲假设你已经读过：

- **u5-l1 / u5-l2**：sv-elab 把一个过程块翻译成**一个** `RTLIL::Process`，过程中用 `ProceduralContext` 持有 HDL 意图 case 树和变量状态。本讲是 `ProceduralContext` 的**上游**——分类器先判断时序类型，再决定用什么 `ProcessTiming` 去喂给 `ProceduralContext`。
- **u3-l3**：`VariableBits` / `VariableBit` 这种「不指向真实线、只描述某变量的某些位」的轻量抽象。本讲里 `AsyncBranch::trigger` 就是一个 `VariableBit`，分类器拿它去和敏感列表里的信号做匹配。
- **u4-l1**：`EvalContext::lhs(...)` 能把一个表达式（这里是敏感列表里的信号或 `if` 条件）求值成静态 `VariableBits`，本讲靠它做「条件是不是等于某个触发信号」的比对。

几个术语先对齐：

- **过程块（procedural block）**：SystemVerilog 里的 `always`、`always_ff`、`always_comb`、`always_latch`、`initial`、`final`。slang 用 `ast::ProceduralBlockSymbol` 表示，`procedureKind` 字段区分这几种。
- **敏感列表（sensitivity list）**：`always @(...)` 括号里的内容，例如 `posedge clk or posedge rst`。slang 用 `ast::TimingControl` 家族（`EventListControl`、`SignalEventControl`、`ImplicitEventControl`、`DelayControl` 等）表示。
- **异步复位（asynchronous load / aload）**：触发器除了时钟边沿外，还被一个异步信号直接置位/复位。综合上它对应 `$aldff` / `$aldffe`（带异步加载端的触发器）。
- **可综合性**：只有「能映射成真实硬件」的 SV 写法才会被翻译，否则发诊断。本讲分类器的一大职责就是把不可综合的时序外形挑出来报错。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/async_pattern.h](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/async_pattern.h) | `TimingPatternInterpretor` 类声明、`AsyncBranch` 结构、三个纯虚 `handle_*` 接口与 `interpret` 入口。 |
| [src/async_pattern.cc](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/async_pattern.cc) | 分类器的全部实现：`interpret`、`handle_always`、`interpret_async_pattern`。 |
| [src/slang_frontend.h](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.h) | `ProcessTiming` 结构（分类结果的数据载体）、`ProceduralContext`（下游消费者）声明。 |
| [src/slang_frontend.cc](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc) | `PopulateNetlist`（`TimingPatternInterpretor` 的唯一子类）及其 `handle_comb_like_process` / `handle_ff_process` / `handle_initial_process` 实现。 |
| [src/diag.h](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/diag.cc) | 分类器发出的各类诊断码（`IffUnsupported`、`AlwaysFFBadTiming`、`ExpectingIfElseAload` 等）。 |
| [tests/unit/dff.ys](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/tests/unit/dff.ys) | 触发器等价性测试，含 `posedge clk or posedge rst` 的异步复位用例（`dff_iff03`）。 |

## 4. 核心概念与源码讲解

### 4.1 TimingPatternInterpretor：always 块的总分类器

#### 4.1.1 概念说明

面对一个 `always` 块，sv-elab 在真正翻译语句之前，必须先回答一个问题：**这块逻辑的时序外形是什么？**

- 是纯组合逻辑（输出只依赖当前输入，像 `assign`）？
- 是边沿触发的触发器（在时钟边沿更新）？
- 是只在仿真启动时跑一次的 `initial`？

这个判断至关重要，因为它决定了下游要生成哪一类 RTLIL 单元：组合逻辑生成连续驱动 / mux，触发器生成 `$dff`/`$dffe`/`$aldffe`，`initial` 则只产生上电初值。

`TimingPatternInterpretor` 就是承担这个「时序模式识别」任务的类。它的名字含义是「时序模式解释器（interpreter）」——注意它**不是**一个访问者（ASTVisitor），而是一个手写的、面向过程块的小状态机。它把识别结果通过三个纯虚函数交给子类去落地，自己只负责「看懂外形、做出分类」。

这是一种典型的「模板方法 + 策略」拆分：基类固化分类流程，子类提供落地策略。

#### 4.1.2 核心流程

```text
ProceduralBlockSymbol（一个 always/initial 块）
        │
        ▼
   interpret(symbol)            ← 按 procedureKind 分派
        │
        ├─ Always / AlwaysFF ───────► handle_always(symbol)
        │                                  │
        │                                  ├─ 解析敏感列表
        │                                  ├─ 隐式敏感(implicit) ──► handle_comb_like_process()  [组合型]
        │                                  └─ 有边沿触发     ──► interpret_async_pattern()    [触发器型]
        │                                                         │
        │                                                         └─ 剥出异步分支 ──► handle_ff_process()
        │
        ├─ AlwaysComb / AlwaysLatch ─► handle_comb_like_process()   [组合型 / 锁存器型]
        │
        ├─ Initial ───────────────────► handle_initial_process()    [initial 型]
        │
        └─ Final ─────────────────────► 忽略（综合不处理）
```

三个 `handle_*` 都是**纯虚函数**，由子类 `PopulateNetlist` 实现。基类只负责走到正确的那个 `handle_*`。

#### 4.1.3 源码精读

先看类的整体骨架。它持有三个引用：综合选项 `settings`、诊断中枢 `issuer`、表达式求值器 `eval`——这三样是做分类判断的「工具箱」。

> [src/async_pattern.h:L19-L30](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/async_pattern.h#L19-L30) —— `TimingPatternInterpretor` 的构造函数只接收三个引用；`AsyncBranch` 内嵌结构把「一个异步分支」打包成 `(trigger 触发位, polarity 极性, body 分支体)` 三元组。

> [src/async_pattern.h:L32-L43](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/async_pattern.h#L32-L43) —— 三个纯虚 `handle_*` 是子类必须实现的「落地接口」，唯一的非虚公共入口是 `interpret`。

注意 `AsyncBranch::trigger` 的类型是 `const VariableBit`（单个 HDL 意图位，来自 u3-l3）。这意味着分类器匹配异步信号时，用的是「位级抽象键」而不是已经物化的 `RTLIL::SigSpec`——这正是为了在还没真正建线之前就能做判断。

子类是谁？全仓库只有 `PopulateNetlist` 继承它：

> [src/slang_frontend.cc:L1766-L1777](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L1766-L1777) —— `PopulateNetlist` 多重继承 `TimingPatternInterpretor` 与 `ASTVisitor`，构造时把 `netlist.settings`、`netlist`（作为 `DiagnosticIssuer`）、`netlist.eval` 传给基类。这就是「分类器」与「画布」绑定的地方。

调用入口在 `PopulateNetlist::handle(ProceduralBlockSymbol)` 里，它对断言类块走 SVA 专用路径，其余的一律交给 `interpret`：

> [src/slang_frontend.cc:L2061-L2089](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L2061-L2089) —— 每访问到一个过程块符号，除了「来自断言的 always」单独处理外，都调用 `interpret(symbol)`（第 2087 行）进入分类器。

#### 4.1.4 代码实践

**实践目标**：确认 `TimingPatternInterpretor` 是一个被「唯一子类」实现的抽象骨架，并找到分类入口。

**操作步骤**：

1. 打开 [src/async_pattern.h:L19-L53](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/async_pattern.h#L19-L53)，确认三个 `handle_*` 都带 `= 0`（纯虚）。
2. 在仓库里搜索 `: public TimingPatternInterpretor`，确认只有 `PopulateNetlist` 一个子类。
3. 打开 [src/slang_frontend.cc:L2087](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L2087)，确认过程块访问最终汇聚到 `interpret(symbol)`。

**需要观察的现象**：基类不含任何 `RTLIL::` 类型的成员——它刻意不碰画布，把「分类」与「建网表」彻底解耦。

**预期结果**：你会看到分类器只依赖 `settings` / `issuer` / `eval`，所有 RTLIL 操作都在三个 `handle_*` 实现里。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `TimingPatternInterpretor` 要把 `handle_*` 设计成纯虚函数，而不是直接在基类里生成 RTLIL？

**参考答案**：为了让「时序模式识别」这一关注点独立可测、可复用，并与 RTLIL 画布解耦。基类固化了分类流程，子类只需提供「拿到分类结果后怎么建线」的策略。这种拆分使得分类逻辑不依赖 Yosys 数据结构，也方便将来换一种落地方式。

**练习 2**：`AsyncBranch::trigger` 为什么用 `VariableBit` 而不是 `RTLIL::SigBit`？

**参考答案**：因为分类发生在建线之前，此时信号可能还没物化成真实的 RTLIL 线。`VariableBit` 是 u3-l3 引入的轻量「HDL 意图位」抽象，可在建线前就用作稳定、可哈希的匹配键。

---

### 4.2 interpret：按 ProceduralBlockKind 分派

#### 4.2.1 概念说明

`interpret` 是分类器的总入口。它的逻辑非常直白：读 `symbol.procedureKind`，按 SystemVerilog 的六种过程块类型分流。这一步是「粗分类」——只看关键字（`always` 还是 `initial`），还没看敏感列表内容。

#### 4.2.2 核心流程

| `procedureKind` | 走向 | 含义 |
| --- | --- | --- |
| `Always` / `AlwaysFF` | `handle_always` | 需要进一步看敏感列表才能定组合型还是触发器型 |
| `AlwaysComb` / `AlwaysLatch` | `handle_comb_like_process` | 天然组合/锁存器型，无需看敏感列表 |
| `Initial` | `handle_initial_process` | 仅仿真启动，产生初值 |
| `Final` | 直接忽略 | 综合不处理 |

注意 `AlwaysComb` / `AlwaysLatch` 是 SV-2009 引入的「带语义」关键字——它们本身就声明了「我是组合 / 我是锁存器」，所以分类器不用再猜，直接当组合型处理。而老式的 `always` / `always_ff` 必须读敏感列表才能判断。

#### 4.2.3 源码精读

> [src/async_pattern.cc:L269-L289](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/async_pattern.cc#L269-L289) —— `interpret` 的全部实现就是一个 `switch(kind)`：`Always`/`AlwaysFF` 合并走 `handle_always`；`AlwaysComb`/`AlwaysLatch` 合并走 `handle_comb_like_process`；`Initial` 走 `handle_initial_process`；`Final` 空实现（注释说明「Final blocks are ignored by synthesis」）。

> [src/slang_frontend.cc:L2053-L2059](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L2053-L2059) —— 对应的子类落地：`handle_initial_process` 用 `ProcessTiming::initial` 建 `ProceduralContext` 跑语句；若开了 `--ignore-initial` 则直接 return（典型做法：综合时丢弃 `initial` 以匹配不带初值的 ASIC 流程）。

#### 4.2.4 代码实践

**实践目标**：验证六种过程块关键字的分流归宿。

**操作步骤**：

1. 读 [src/async_pattern.cc:L269-L289](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/async_pattern.cc#L269-L289)。
2. 自行预测：`always_comb`、`always_latch`、`initial`、`final` 各自走到哪个 `case`。
3. 对 `final` 块，确认它不会产生任何 RTLIL Process。

**需要观察的现象**：`AlwaysComb` 和 `AlwaysLatch` 共用同一个 `handle_comb_like_process`，区别只在下游（u6-l3 的锁存器推断会看 `procedureKind == AlwaysLatch` 决定是否发 `LatchNotInferred` 警告）。

**预期结果**：分流表与本节表格一致；`final` 无任何输出。

#### 4.2.5 小练习与答案

**练习 1**：`always_comb` 和 `always @*` 在 `interpret` 里的路径相同吗？

**参考答案**：不同。`always_comb` 在 `interpret` 里直接命中 `AlwaysComb` 分支，调用 `handle_comb_like_process`；而 `always @*` 的关键字是 `Always`，先走 `handle_always`，在 `handle_always` 里因为敏感列表是隐式（`ImplicitEvent`）才被标记为组合型，最终也调到 `handle_comb_like_process`。殊途同归，但 `always_comb` 少了一步敏感列表解析。

**练习 2**：为什么 `Final` 块是空实现而不是 `log_abort()`？

**参考答案**：`final` 块只在仿真结束时执行，没有对应硬件，综合工具应当静默忽略它而不是报错，所以是空实现。

---

### 4.3 handle_always：解析敏感列表，组合型还是触发型

#### 4.3.1 概念说明

`handle_always` 处理最棘手的情况：`always` / `always_ff` 的真实意图藏在敏感列表里。同一个 `always` 关键字，写出 `always @(*)` 就是组合逻辑，写出 `always @(posedge clk)` 就是触发器。这一节的目标就是把敏感列表「翻译」成两个布尔结论：

- `implicit`：是否是隐式/电平敏感（组合型）？
- `triggers`：收集到的边沿触发信号列表（触发器型）？

然后根据这两个结论做三分法：纯隐式 → 组合型；纯边沿 → 触发器型；两者混用 → 报诊断。

#### 4.3.2 核心流程

```text
always 块体
   │
   ├─ 是 Block/ConcurrentAssertion/ImmediateAssertion ──► handle_comb_like_process  (SVA 短路)
   ├─ 不是 TimedStatement ──────────────────────────────► UnsynthesizableFeature   (报错返回)
   │
   ▼  取 TimedStatement.timing
   展开 EventList（多个事件）或单事件
   │
   ▼  遍历每个事件 ev
   switch (ev->kind):
     SignalEvent:
        edge=None  ──► AlwaysFF 报错 / 否则 implicit=true（电平敏感）
        edge=Pos/Neg ──► 加入 triggers
        edge=Both   ──► 默认报 BothEdgesUnsupported，开了 --allow-dual-edge-ff 才加入
        （若带 iff 且 None 边沿 ──► IffUnsupported）
     ImplicitEvent (@*) ──► AlwaysFF 报错 / 否则 implicit=true
     EventList        ──► log_abort（不应出现，已在前面展开）
     Delay (#...)     ──► 默认报 GenericTimingUnsyn，开了 --ignore-timing 则 implicit=true
     default          ──► UnsynthesizableFeature
   │
   ▼
   if (implicit && !triggers.empty()) 报 EdgeImplicitMixing
   if (implicit)                       ──► handle_comb_like_process   [组合型]
   else if (!triggers.empty())         ──► interpret_async_pattern    [触发器型]
   (若两者皆空：什么都不做)
```

#### 4.3.3 源码精读

**短路判断**：块体不是 `TimedStatement` 的几种特例。

> [src/async_pattern.cc:L36-L45](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/async_pattern.cc#L36-L45) —— 如果块体本身是 `Block`/`ConcurrentAssertion`/`ImmediateAssertion`（典型是自由站立的 SVA），直接当组合型；如果不是 `TimedStatement`，则发 `UnsynthesizableFeature` 并返回。

**展开敏感列表**：`EventList`（多个 `or` 连接的事件）要展开成数组，单个事件则包成单元素数组。

> [src/async_pattern.cc:L47-L56](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/async_pattern.cc#L47-L56) —— 取 `TimedStatement.timing`，若是 `EventListControl` 取其 `events` 数组，否则用栈上单元素数组 `top_events` 包装。

**逐事件分类的大 switch**：这是本节核心。

> [src/async_pattern.cc:L61-L115](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/async_pattern.cc#L61-L115) —— 遍历每个事件并分类。注意几个关键诊断：`EdgeKind::None`（电平敏感）对 `always_ff` 是 `AlwaysFFBadTiming` 错误（因为 `always_ff` 必须建模触发器），对普通 `always` 则是 `SignalSensitivityAmbiguous` 警告并置 `implicit=true`；`BothEdges` 默认拒绝（`BothEdgesUnsupported`），需 `--allow-dual-edge-ff` 才放行。

> [src/async_pattern.cc:L106-L112](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/async_pattern.cc#L106-L112) —— 延迟控制 `#n` 本身不可综合，默认报 `GenericTimingUnsyn`，但开了 `--ignore-timing` 后降级为隐式组合，方便用户强行综合带延迟的测试代码。

**最终三分派**：

> [src/async_pattern.cc:L117-L124](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/async_pattern.cc#L117-L124) —— 隐式与边沿混用报 `EdgeImplicitMixing`；`implicit` 走组合型，有 `triggers` 走触发器型解析。

#### 4.3.4 代码实践

**实践目标**：亲手把几种常见 `always` 写法映射到 `handle_always` 的出口。

**操作步骤**：

1. 准备三段最小 SV：
   - `always @(*) q = a & b;`（隐式敏感）
   - `always_ff @(posedge clk) q <= d;`（单边沿）
   - `always @(posedge clk or posedge rst) ...`（多边沿）
2. 对每一段，在 [src/async_pattern.cc:L61-L124](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/async_pattern.cc#L61-L124) 里追踪 `implicit` 与 `triggers` 的最终取值。
3. 给出每段最终命中的 `handle_*`。

**需要观察的现象**：第一段 `implicit=true, triggers=[]`；第二段 `implicit=false, triggers=[clk]`；第三段 `implicit=false, triggers=[clk, rst]`。

**预期结果**：第一段 → `handle_comb_like_process`；后两段 → `interpret_async_pattern`。若想真机验证，可运行 `tests/unit/dff.ys`（见 4.4 节）。

#### 4.3.5 小练习与答案

**练习 1**：`always_ff @(*)` 会发生什么？

**参考答案**：`always_ff` 的语义要求它必须建模触发器，所以遇到隐式敏感（`@*`/电平敏感）会在 [src/async_pattern.cc:L71-L74](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/async_pattern.cc#L71-L74) 或 [L96-L98](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/async_pattern.cc#L96-L98) 发 `AlwaysFFBadTiming` 错误（"timing control does not model a flip-flop"），不会进组合路径。

**练习 2**：`always @(posedge clk or a)`（一个边沿 + 一个电平）会怎样？

**参考答案**：`posedge clk` 进入 `triggers`，`a`（无边沿）置 `implicit=true`。于是 `implicit && !triggers.empty()` 成立，在 [L117-L118](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/async_pattern.cc#L117-L118) 发 `EdgeImplicitMixing`（"mixing of implicit and edge sensitivity"），随后仍按组合型处理。

---

### 4.4 interpret_async_pattern 与 AsyncBranch：剥出异步复位

#### 4.4.1 概念说明

这是本讲最精巧的部分。当一个 `always` 块有**多个**边沿触发信号时（典型：`posedge clk or posedge rst`），sv-elab 需要区分出：

- 哪个是**真正的时钟**（同步分支，产生 `$dffe`）？
- 哪些是**异步加载信号**（异步分支，产生 `$aldffe` 的异步端）？

SystemVerilog 本身没有显式语法声明「rst 是异步复位」，这个意图完全靠**代码模式**表达：异步复位总是写成

```verilog
always_ff @(posedge clk or posedge rst) begin
    if (rst)        // 异步分支：rst 有效时立即生效
        q <= 0;
    else            // 同步分支：clk 边沿时更新
        q <= d;
end
```

`interpret_async_pattern` 的工作就是**识别这个 `if-else` 模式**，把 `if (rst)` 这一段连同它的触发信号 `rst` 和极性，打包成一个 `AsyncBranch`，然后层层剥离，直到只剩下一个触发信号——那就是时钟。

`AsyncBranch` 的定义非常紧凑，就是把剥出来的异步分支三要素钉在一起：

> [src/async_pattern.h:L25-L30](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/async_pattern.h#L25-L30) —— `AsyncBranch{ trigger: VariableBit, polarity: bool, body: Statement& }`：触发位（如 `rst`）、极性（高有效/低有效）、分支体（如 `q <= 0`）。

#### 4.4.2 核心流程

`interpret_async_pattern` 分两大阶段：

**阶段 A：剥离 prologue（前导语句）**

很多触发器写法会把局部声明或表达式语句放在 `if` 之前（包在 `begin...end` 或语句列表里）。这些不属于异步分支，需要先剥下来作为「prologue」（前导），它们在每个触发事件（时钟或异步）发生时都执行。

```text
while (did_something):
   若 stmt 是 begin...end(Sequential) ──► 记录 blockSymbol 为 prologue_block，下钻到 body
   若 stmt 是 List 且 triggers>1 ──► 把开头的 ExpressionStatement/VariableDeclaration
                                    移入 prologue，最后一个语句成为新 stmt
```

**阶段 B：逐个剥出异步分支**

```text
while (triggers.size() > 1):        # 只剩 1 个时，它就是时钟，停止
   要求 stmt 是「标准 if-else」：
      - Conditional 语句
      - 无 unique/priority 修饰
      - 恰好 1 个条件
      - 无 pattern
      - 有 ifFalse（else 分支）
     否则 ──► ExpectingIfElseAload 报错返回

   归一化条件表达式（剥壳，追踪极性 polarity）：
      !cond / ~cond     ──► polarity 翻转，取操作数
      cond == 1'b1      ──► 取 cond
      cond == 1'b0      ──► polarity 翻转，取 cond
      narrowing 转换    ──► 取被转换的操作数

   condition1 = eval.lhs(*condition)        # 求成 VariableBits

   若 condition1 是 1 位且无 dummy 位：
      在 triggers 里找匹配 eval.lhs(trigger.expr) == condition1 的那个
      若找到：
         极性不匹配 ──► IfElseAloadPolarity 警告（仍继续）
         found_async.push_back({condition1[0], polarity, ifTrue 体})
         triggers.erase(该触发)            # 从候选里移除
         stmt = ifFalse                     # 下钻到 else，继续剥
         continue
   否则 ──► IfElseAloadMismatch 报错返回

# 循环结束后：
handle_ff_process(symbol, clock=triggers[0], prologue_block, prologue, sync_body=stmt, found_async)
```

关键直觉：**剥洋葱**。每剥一层 `if-else`，就消耗一个异步信号、产出一条 `AsyncBranch`；剥到只剩一个信号时，它就是时钟，剩下的 `else` 体就是同步分支体。

**极性（polarity）的含义**：异步信号可能是高有效（`posedge rst`，polarity=true）或低有效（`negedge rst_n`，polarity=false）。归一化时，`if (rst)` 是高有效，`if (!rst_n)` 翻转一次极性后仍是匹配 `negedge rst_n`。

#### 4.4.3 源码精读

**阶段 A：剥 prologue**

> [src/async_pattern.cc:L134-L172](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/async_pattern.cc#L134-L172) —— 第一个 `while(did_something)` 循环：先把 `begin...end` 块符号记为 `prologue_block` 并下钻；再把语句列表里开头的声明/表达式语句移入 `prologue`。注意第 148 行的 `triggers.size() > 1` 守卫——只有多触发场景才尝试剥列表 prologue。

**阶段 B：校验标准 if-else 模式**

> [src/async_pattern.cc:L175-L184](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/async_pattern.cc#L175-L184) —— `while(triggers.size() > 1)` 循环开头，严格校验当前语句必须是「普通 if-else」：无 `unique`/`priority`、单条件、无 `pattern`、必须有 `else`。任何不符都发 `ExpectingIfElseAload`（"simple if-else pattern expected in modeling an asynchronous load on a flip-flop"）并附 `NoteDuplicateEdgeSense` 注释，然后 `return`。

**阶段 B：条件归一化（剥壳）**

> [src/async_pattern.cc:L189-L231](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/async_pattern.cc#L189-L231) —— 内层 `while(did_something)` 把条件表达式层层剥成「裸信号 + 极性」：逻辑/按位取反翻极性、`==1` 取左、`==0` 翻极性取左、窄化转换取操作数。这一段保证了 `if (!rst_n)`、`if (rst == 1'b1)`、`if ((rst))` 都能归一成对 `rst` 的匹配。

**阶段 B：与触发信号匹配**

> [src/async_pattern.cc:L233-L259](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/async_pattern.cc#L233-L259) —— `condition1 = eval.lhs(*condition)` 求成 `VariableBits`；若是 1 位无 dummy，就在 `triggers` 里用 `eval.lhs(trigger->expr)` 逐一比对，找到匹配后处理 `iff` 与极性（极性不符发 `IfElseAloadPolarity` 但仍推断），把 `{condition1[0], polarity, ifTrue}` 压入 `found_async`，`triggers.erase`，`stmt = ifFalse` 继续。

> [src/async_pattern.cc:L261-L266](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/async_pattern.cc#L261-L266) —— 若条件无法匹配任何触发信号，发 `IfElseAloadMismatch`（"condition cannot be matched to any signal from the event list"）返回；循环正常结束后，调用 `handle_ff_process`，把唯一的 `triggers[0]` 作为时钟、剩余 `stmt` 作为同步体、`found_async` 作为异步分支集合传下去。

#### 4.4.4 代码实践

**实践目标**：对 `always_ff @(posedge clk or posedge rst) if (rst) q <= 0; else q <= d;`，手动模拟 `interpret_async_pattern` 的剥离过程，并说出 `handle_ff_process` 收到的参数。

**操作步骤**：

1. 进入函数时 `triggers = [&clk_sigev, &rst_sigev]`（两个 `SignalEventControl`，都是 `PosEdge`），`body` 是 `if (rst) q<=0; else q<=d;`。
2. **阶段 A**：假设 `body` 被 `begin...end` 包裹，则 `prologue_block` 记为该块符号，`stmt` 下钻到 `if-else`；无声明语句，`prologue` 为空。
3. **阶段 B 第 1 轮**：`triggers.size()==2 > 1`。
   - 校验：`stmt` 是 `Conditional`，无 `unique/priority`，单条件 `rst`，无 pattern，有 `else` ✓。
   - 归一化：`rst` 是裸标识符，`polarity=true`，`condition=rst`。
   - `condition1 = eval.lhs(rst)` 得到 `rst` 的 1 位 `VariableBits`。
   - 在 `triggers` 中比对：`posedge rst` 的 `eval.lhs` 等于 `condition1`，命中。
   - 极性：`PosEdge == polarity?PosEdge:NegEdge` 即 `PosEdge==PosEdge` ✓。
   - `found_async.push_back({rst_bit, true, "q<=0"})`，`triggers.erase(rst)` → `triggers=[clk]`，`stmt = ifFalse = "q<=d"`，`continue`。
4. **阶段 B 第 2 轮**：`triggers.size()==1`，不大于 1，循环退出。
5. 调用 `handle_ff_process(symbol, clock=*triggers[0]=clk, prologue_block, prologue=[], sync_body="q<=d", found_async=[{rst,true,"q<=0"}])`。

**需要观察的现象**：`found_async` 恰好有一条，时钟是 `clk`，同步体是 `else` 分支。

**预期结果**：下游 `handle_ff_process` 会据此为 `q` 生成 `$aldffe`（带异步加载端的使能触发器）——时钟 `clk`、异步端 `rst` 高有效、同步 D 端 `d`、异步加载值 `0`。可用 `tests/unit/dff.ys` 的 `dff_iff03` 用例（[tests/unit/dff.ys:L81-L109](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/tests/unit/dff.ys#L81-L109)）做等价性验证；该用例的 gate 端正是 `posedge clk iff en or posedge rst` + `if(rst)…else…`。运行方式见综合实践。

#### 4.4.5 小练习与答案

**练习 1**：如果写成 `always_ff @(posedge clk or posedge rst) if (rst) q <= 0;`（**没有 else 分支**），会发生什么？

**参考答案**：阶段 B 校验失败——第 180 行要求 `!stmt->as<ConditionalStatement>().ifFalse`（即必须有 `else`），缺失 `else` 时该校验为假，于是发 `ExpectingIfElseAload` 并 `return`，不生成触发器。

**练习 2**：`always_ff @(posedge clk or negedge rst_n) if (!rst_n) q <= 0; else q <= d;` 的异步极性是什么？

**参考答案**：触发信号是 `negedge rst_n`。条件 `!rst_n` 经归一化翻转一次极性（`polarity` 从 `true` 变 `false`），匹配上 `negedge`，所以 `AsyncBranch.polarity=false`（低有效）。下游据此生成异步端低有效的 `$aldffe`。

**练习 3**：如果有两个异步信号（`posedge clk or posedge rst or posedge load`），剥完后会怎样？

**参考答案**：`found_async` 会有两条。但 [src/slang_frontend.cc:L1912-L1915](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L1912-L1915) 在 `handle_ff_process` 里检查 `aloads.size() > 1` 时会发 `AloadOne`（"multiple asynchronous loads unsupported"）错误并返回，即当前实现只支持单个异步加载。

---

### 4.5 ProcessTiming：分类结果如何传递给下游

#### 4.5.1 概念说明

`interpret_async_pattern` 的产出（时钟、同步体、异步分支列表）最终喂给 `handle_ff_process`。后者要做的事是：为**每个分支**（prologue、每个异步分支、同步分支）各建一个 `ProceduralContext`，而 `ProceduralContext` 需要一个 `ProcessTiming` 来描述「这个分支在什么条件下、由什么信号触发」。

`ProcessTiming` 就是「时序意图」的数据载体，它把分类结果编码成下游能直接消费的形式：

- `kind`：`Initial` / `Implicit`（组合）/ `EdgeTriggered`（边沿）。
- `background_enable`：「背景使能」信号，默认 `S1`（恒有效）。对于异步/同步分支，它是「在本分支未被更高优先级分支抢占时才激活」的组合条件。
- `triggers`：`Sensitivity{signal, edge_polarity, ast_node}` 列表，描述边沿触发信号。

#### 4.5.2 核心流程

`handle_ff_process` 把一个 `always_ff` 拆成三段，各配一个 `ProcessTiming`：

```text
always_ff @(posedge clk or posedge rst) ...   (found_async=[rst], clock=clk)
   │
   ├─ prologue 段
   │    ProcessTiming(EdgeTriggered)
   │      triggers = [clk(极性), rst(异步,极性)]   # 时钟 + 所有异步信号
   │    # prologue 语句在每个触发事件都执行
   │
   ├─ 每个异步分支 (abranch in async)
   │    ProcessTiming(Implicit)                    # 异步分支是电平触发的「立即生效」
   │      background_enable = !prior_branch_taken & (rst 有效极性)
   │    # prior_branch_taken 累积已处理分支的信号，保证优先级
   │
   └─ 同步分支
        ProcessTiming(EdgeTriggered)
          triggers = [clk]
          background_enable = !prior_branch_taken & event_guard
        # event_guard 来自 iff 条件（若存在）
        # 最终为每个被驱动变量发 $aldffe（有异步）或 $dffe（无异步）
```

`background_enable` 是理解优先级的关键：异步分支的使能是「`rst` 有效 **且** 之前更优先的分支没被命中」。这正确表达了「异步复位优先于同步更新」的硬件语义。

#### 4.5.3 源码精读

**ProcessTiming 结构**

> [src/slang_frontend.h:L224-L245](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.h#L224-L245) —— `ProcessTiming`：`kind` 默认 `Implicit`；`background_enable` 默认 `S1`；`Sensitivity{signal, edge_polarity, ast_node}`；`triggers` 向量；`extract_trigger` 把触发信息写进 `$print`/`$check` 这类副作用单元的 `EN`/`TRG` 端口；另有 `implicit`/`initial` 两个静态单例。

> [src/procedural.cc:L80-L84](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/procedural.cc#L80-L84) —— 两个全局静态实例 `ProcessTiming::implicit`（组合）与 `ProcessTiming::initial`（初值），分别供 `handle_comb_like_process` 与 `handle_initial_process` 复用。

**handle_ff_process 如何拼装三段 ProcessTiming**

> [src/slang_frontend.cc:L1863-L1879](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L1863-L1879) —— **prologue 段**：`ProcessTiming(EdgeTriggered)`，`triggers` = 时钟 + 所有异步触发信号，跑 prologue 语句后 `copy_case_tree_into` 灌进同一个 `Process`。

> [src/slang_frontend.cc:L1894-L1910](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L1894-L1910) —— **异步分支段**：`ProcessTiming(Implicit)`，`background_enable = LogicAnd(LogicNot(prior_branch_taken), sig_depol)`，其中 `sig_depol` 是把异步信号转成「有效电平」（高有效直通、低有效取反）。`prior_branch_taken` 累积各分支信号以保证优先级。`inherit_state(prologue)` 继承 prologue 的变量状态。

> [src/slang_frontend.cc:L1917-L1932](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L1917-L1932) —— **同步分支段**：`ProcessTiming(EdgeTriggered)`，`triggers=[clk]`，`background_enable = LogicAnd(LogicNot(prior_branch_taken), event_guard)`（`event_guard` 来自 `iff` 条件）。随后据 `aloads` 是否为空，为每个被驱动变量发 `$dffe` 或 `$aldffe`（具体单元选择是 u6-l2 的内容）。

**background_enable 如何进入 RTLIL**

> [src/slang_frontend.cc:L407-L411](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L407-L411) —— `ProcessTiming::extract_trigger` 把 `LogicAnd(background_enable, enable)` 接到副作用单元的 `EN` 端口，是 `background_enable` 影响网表的一个出口。

#### 4.5.4 代码实践

**实践目标**：把 `always_ff @(posedge clk or posedge rst) if(rst) q<=0; else q<=d;` 的分类结果，对应到三段 `ProcessTiming`。

**操作步骤**：

1. 在 [src/slang_frontend.cc:L1863-L1932](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L1863-L1932) 分别定位 prologue / 异步 / 同步三段 `ProcessTiming` 的构造。
2. 对这个例子，`prior_branch_taken` 在异步段后等于 `rst`（高有效）。
3. 写出同步段的 `background_enable = !rst & 1'b1 = !rst`，体会「异步复位生效时同步分支被关门」。

**需要观察的现象**：异步段 `background_enable = rst`，同步段 `background_enable = !rst`，两者互斥——这正是「同一时刻只允许一个分支驱动 `q`」的硬件语义。

**预期结果**：三段时序在同一个 `RTLIL::Process` 里以 `prior_branch_taken` 串联，最终 `q` 由一个 `$aldffe` 驱动（异步端 `rst` 高有效、时钟 `clk`、同步 D=`d`、异步值=`0`）。该结论可用 `tests/unit/dff.ys` 的 `dff_iff03` 验证。

#### 4.5.5 小练习与答案

**练习 1**：为什么 prologue 段的 `triggers` 要包含时钟**和**所有异步信号？

**参考答案**：因为 prologue 语句（如局部变量赋值）需要在**每个**触发事件——无论是时钟边沿还是异步信号有效——发生时都执行，作为后续分支的公共前置。所以它的 `EdgeTriggered` 时序要把所有触发信号都列入。

**练习 2**：`background_enable` 默认为什么是 `S1`？

**参考答案**：`S1` 表示恒为 1（恒有效）。对组合型（`Implicit`）和无抢占的简单时序，分支总是激活的，默认 `S1` 让下游逻辑「不被额外关门」。只有异步/同步多分支需要互相抢占时，才会改写 `background_enable` 加入 `prior_branch_taken` 条件。

---

## 5. 综合实践

**任务**：写一个最小的异步复位触发器，用 `read_slang` 综合，追踪它从「分类」到「单元发射」的完整路径，并用 Yosys 内置的 `read_verilog` 做等价性证明。

**步骤**：

1. 准备测试脚本 `my_dff.ys`（示例代码，基于 [tests/unit/dff.ys](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/tests/unit/dff.ys) 的范式）：

   ```tcl
   # 示例代码：异步复位 D 触发器的等价性测试
   read_slang <<EOF
   module my_dff_gate(input logic clk, input logic rst, input logic d, output logic q);
       always_ff @(posedge clk or posedge rst) begin
           if (rst) q <= 1'b0;
           else     q <= d;
       end
   endmodule
   EOF

   read_verilog <<EOF
   module my_dff_gold(input clk, input rst, input d, output reg q);
       always @(posedge clk or posedge rst) begin
           if (rst) q <= 1'b0;
           else     q <= d;
       end
   endmodule
   EOF

   proc
   async2sync
   equiv_make my_dff_gold my_dff_gate my_dff_equiv
   equiv_induct my_dff_equiv
   equiv_status -assert
   ```

2. 运行（待本地验证，需先按 u8-l3 构建 `slang.so` 插件并安装到 Yosys）：

   ```bash
   yosys -m slang.so -s my_dff.ys
   ```

3. 若想看分类器实际产出的网表，在 `read_slang` 之后加一行 `show -format dot` 或 `write_rtlil`，检查 `my_dff_gate` 里 `q` 是否由 `$aldffe`（或经 `proc` 后的等价单元）驱动。

4. **源码追踪**（不依赖运行）：对照本讲 4.4.4，把这个设计的 `if-else` 经 `interpret_async_pattern` 的剥离过程在纸上走一遍，确认 `found_async=[{rst,true,"q<=0"}]`、`clock=clk`、`sync_body="q<=d"`。

**预期结果**：`equiv_status -assert` 输出 `Equivalence successfully proven!`，证明 sv-elab 的分类与翻译与 Yosys 内置 `read_verilog` 语义一致。

**若无法运行**：标记「待本地验证」，把重点放在步骤 4 的源码追踪上——这已能完整覆盖本讲的三个最小模块。

## 6. 本讲小结

- `TimingPatternInterpretor` 是 sv-elab 的「时序模式分类器」，用三个纯虚 `handle_*` 把**分类**（基类）与**落地**（子类 `PopulateNetlist`）解耦，自身不碰 RTLIL 画布。
- `interpret` 按 `ProceduralBlockKind` 粗分派：`Always`/`AlwaysFF` 交 `handle_always`；`AlwaysComb`/`AlwaysLatch` 直接当组合型；`Initial` 交 `handle_initial_process`；`Final` 忽略。
- `handle_always` 解析敏感列表，产出 `implicit`（组合型）与 `triggers`（边沿触发信号列表）两个结论，据此三分派到组合型或触发器型路径，并在此拦截不可综合的时序外形（`IffUnsupported`、`AlwaysFFBadTiming`、`BothEdgesUnsupported`、`EdgeImplicitMixing` 等）。
- `interpret_async_pattern` 用「剥洋葱」算法处理多触发信号：校验标准 `if-else` 模式 → 归一化条件并匹配触发信号 → 把每个异步分支打包成 `AsyncBranch{trigger, polarity, body}`，直到剩下一个信号即为时钟。
- `AsyncBranch` 是「一个异步复位分支」的三元组，`trigger` 用 `VariableBit`（HDL 意图位）以便在建线前做匹配。
- 分类结果经 `ProcessTiming`（`kind` / `triggers` / `background_enable`）传递给下游：`handle_ff_process` 为 prologue / 异步 / 同步三段各配一个 `ProcessTiming`，用 `prior_branch_taken` 编码分支优先级，最终由 u6-l2 发射 `$dffe`/`$aldffe`。

## 7. 下一步学习建议

- **u6-l2 触发器发射与异步复位**：紧接本讲，细讲 `handle_ff_process` 如何根据 `ProcessTiming` 与 `aloads` 选择 `$dffe` / `$aldffe` / `$aldffe` / `add_dual_edge_aldff`，以及 `MissingAload` 警告的含义。建议先读 [src/slang_frontend.cc:L1934-L2044](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L1934-L2044)。
- **u6-l3 锁存器推断**：讲 `handle_comb_like_process` 如何用 `detect_possibly_unassigned_subset` 找悬空位并注入 `$dlatch`，是组合型路径的下游。
- **延伸阅读**：想加深对触发器单元封装的理解，回看 u3-l2 的 `RTLILBuilder::add_dff/add_dffe/add_aldff`；想理解 `eval.lhs` 如何把信号求值成 `VariableBits`，回看 u4-l1 / u3-l3。

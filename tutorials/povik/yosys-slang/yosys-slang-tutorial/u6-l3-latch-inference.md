# 锁存器推断

## 1. 本讲目标

本讲承接 u6-l1（时序模式分类）与 u6-l2（触发器发射），解决时序逻辑的最后一种落地形态：**锁存器（latch）**。

读完本讲你应当能够：

- 说清 sv-elab 在什么条件下把一个过程块翻译成锁存器，而不是连续驱动或触发器。
- 读懂 `detect_possibly_unassigned_subset` 的「悬空位」判定算法，并手动模拟它在小型 case 树上的执行。
- 解释 `handle_comb_like_process` 如何为每一个悬空位发射 `$dlatch` 单元，并用 `en`/`staging` 两个辅助信号构造「透明/保持」语义。
- 描述 `insert_latch_signaling` 如何把记录在 `Case::Action` 里的 HDL 意图改写成对 `en`/`staging` 的驱动。
- 知道 `always_comb`、`always_latch`、普通 `always @(*)` 三者在锁存器推断上的差别，以及 `LatchNotInferred` 诊断何时触发。

## 2. 前置知识

在进入源码前，先建立两条直觉。

**直觉一：硬件里没有「偶尔赋值」。** 一个 `always @(*)` 块描述的是组合逻辑：只要敏感列表里的信号变化，块体就「重新执行一次」。如果某条执行路径上某个位**没有被赋值**（例如 `if` 缺少 `else` 分支），那么这个位在该路径上就「保持原值」。组合逻辑要保持值，唯一的物理实现就是**锁存器**——一个电平敏感的存储元件。这种「因为某条路径没赋值而被迫引入存储」的现象，就是「锁存器推断（latch inference）」。

**直觉二：sv-elab 不直接发单元，先建意图树。** 回顾 u3-l4 与 u5-l1：过程块里的每条赋值都被压成一个 `Case::Action`（含 `lvalue`/`mask`/`unmasked_rvalue` 三件套），挂在一棵 `Case`/`Switch` 意图树上。这棵树记录的是「HDL 意图」，左值用 `VariableBits`（见 u3-l3）而非真实线。锁存器分析正是消费这棵意图树：它要回答「在所有可能执行路径上，哪些位**保证**被赋值、哪些位**可能**漏掉」。

> 关键术语复习：
> - `VariableBit`：某变量某一位的轻量抽象键（u3-l3），是悬空判定的最小单位。
> - `Case::Action`：意图树上的一条赋值记录，`mask` 全 1 表示无条件整位赋值（u3-l4、u5-l3）。
> - `$dlatch`：Yosys RTLIL 的电平敏感锁存器单元，端口为 `EN`（使能）、`D`（数据）、`Q`（输出），`EN_POLARITY` 参数指定使能极性。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [src/slang_frontend.cc](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc) | 包含 `detect_possibly_unassigned_subset`（悬空位判定）、`handle_comb_like_process`（锁存器发射主函数）、`add_continuous_driver`（组合路径的连续驱动） |
| [src/cases.h](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/cases.h) | `Case::Action` 意图结构与 `Case::insert_latch_signaling`（把意图改写成 enable/staging 驱动） |
| [src/async_pattern.cc](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/async_pattern.cc) | `TimingPatternInterpretor::interpret` 与 `handle_always`，把过程块分派到组合/触发器/initial 三条路径 |
| [src/procedural.cc](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/procedural.cc) | `ProceduralContext::all_driven`，汇总本过程块驱动的全部静态变量位 |
| [src/builder.cc](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/builder.cc) | `RTLILBuilder::add_placeholder_signal`，创建待连接的占位线（en/staging 信号由此产生） |
| [tests/unit/latch.ys](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/tests/unit/latch.ys) | 锁存器等价性测试，是本讲实践的活样本 |

---

## 4. 核心概念与源码讲解

### 4.1 从过程块到组合/锁存器路径的分派

#### 4.1.1 概念说明

一个 `always` 系列过程块到底是组合逻辑、触发器、还是锁存器？这是 u6-l1 讲过的 `TimingPatternInterpretor` 的工作。本讲关心的只是它的「组合型」出口：当分类器判定一个块**没有边沿敏感**（即电平敏感、等价于 `always @(*)`），就会调用 `handle_comb_like_process`。

`handle_comb_like_process` 同时服务三类过程块：

- `always_comb`：纯组合，按 SV 语义**不应**推断锁存器。
- `always_latch`：明确声明「我想要锁存器」。
- 普通 `always @(*)`（隐式敏感列表）：可能推断出锁存器，也可能纯粹组合——取决于块体里有没有「漏赋值的路径」。

区分这三者的关键，不是块的关键字本身，而是**块体里是否存在悬空位**。

#### 4.1.2 核心流程

```
interpret(kind)
  ├─ Always / AlwaysFF  → handle_always
  │     ├─ 有边沿触发    → interpret_async_pattern → (u6-l2 触发器)
  │     └─ 隐式/电平敏感  → handle_comb_like_process  ← 本讲
  ├─ AlwaysComb / AlwaysLatch → handle_comb_like_process  ← 本讲
  ├─ Initial            → handle_initial_process
  └─ Final              → 忽略
```

进入 `handle_comb_like_process` 后，再由悬空位判定决定每个位走「连续驱动」（组合）还是「锁存器」。

#### 4.1.3 源码精读

分派入口在 `interpret` 中，`AlwaysComb`/`AlwaysLatch` 直接进组合路径：

[src/async_pattern.cc:276-279](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/async_pattern.cc#L276-L279) — `AlwaysComb`/`AlwaysLatch` 直接调用 `handle_comb_like_process`。

对于普通 `always`，`handle_always` 先解析敏感列表。当敏感列表里**没有任何边沿事件**（全是电平敏感或隐式 `@*`）时，`implicit` 为真，于是也走组合路径：

[src/async_pattern.cc:120-124](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/async_pattern.cc#L120-L124) — `implicit` 为真时调用 `handle_comb_like_process`，否则有边沿触发才走异步模式（u6-l2）。

注意 [src/async_pattern.cc:40](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/async_pattern.cc#L40) 这条短路：当 `always` 块体是一个自由站立断言（SVA）时，也借道 `handle_comb_like_process` 处理（断言见 u7-l4）。

#### 4.1.4 代码实践

1. **实践目标**：确认「同一个 `handle_comb_like_process` 服务三种关键字」。
2. **操作步骤**：在 src/async_pattern.cc 中分别搜索 `AlwaysComb`、`AlwaysLatch` 与 `handle_comb_like_process`，统计后者被调用的三处位置。
3. **观察现象**：你会看到三处调用——`interpret` 里 `AlwaysComb/AlwaysLatch` 分支一处、`handle_always` 里 SVA 短路一处、`handle_always` 里 `implicit` 分支一处。
4. **预期结果**：理解「锁存器是否推断」与关键字无强绑定，真正决定因素是下一步的悬空位判定。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `always_ff` 永远不会进入 `handle_comb_like_process`？
> **答**：`always_ff` 只走 `handle_always`，且其敏感列表必须是边沿事件；若出现电平敏感事件会触发 `AlwaysFFBadTiming` 诊断（[src/async_pattern.cc:71-74](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/async_pattern.cc#L71-L74)），不会被当作 `implicit` 组合块。

**练习 2**：`always_comb` 块如果存在漏赋值的位，sv-elab 会推断锁存器吗？
> **答**：不会。后面 4.3 节会看到，`handle_comb_like_process` 对 `always_comb` **跳过**悬空位判定，所有驱动位一律走连续驱动（组合）路径。

---

### 4.2 detect_possibly_unassigned_subset：悬空位判定

#### 4.2.1 概念说明

这是本讲最核心的算法。给定一棵 `Case`/`Switch` 意图树和一组「我们关心的位」`signals`，它要回答一个问题：

> **在这些位的所有可能执行路径上，哪些位「可能」没有被赋值？**

这些「可能没赋值」的位就是悬空位（dangling），也就是锁存器的候选。

为什么说「可能」？因为 case 树的分支是否可达、是否覆盖了 switch 信号的所有取值，取决于运行时的信号值。sv-elab 用一个保守的、编译期的可达性分析来逼近：只要存在**任何一条可达且不赋值该位的路径**，该位就算「可能悬空」。

#### 4.2.2 核心流程

算法本质是一棵带「模式池」剪枝的 DFS。设输入关心的位集合为 \( S \)，规则（Case 节点）为 \( r \)：

```
detect(S, r):
    remaining ← S
    # 1. 本节点直接赋值的位：mask 全 1 才算「确定赋值」
    for action in r.actions:
        if action.mask 全 1:
            remaining ← remaining − action.lvalue 的所有位

    # 2. 逐个下钻 switch
    for sw in r.switches:
        if remaining 空: 跳出
        pool ← BitPatternPool(sw.signal)   # 还能取哪些值
        new_remaining ← {}
        for case_ in sw.cases:
            if pool 空: 跳出              # 后续 case 不可达
            selectable ← case_ 是否仍可达
            if selectable:
                new_remaining ← new_remaining ∪ detect(remaining, case_)
        # 3. 若 switch 覆盖全部取值(full_case 或 pool 空)，
        #    则 remaining = 各可达 case 悬空位的「并集」;
        #    否则存在隐式默认路径(什么都不做)，原 remaining 全部保留
        if sw.full_case 或 pool 空:
            remaining ← new_remaining
    return remaining
```

两个关键不变量：

1. **并集语义**：一个位要被「摘除」（视为安全），必须在**每一个**可达 case 里都被赋值。只要有一个可达 case 没赋它，它就出现在 `new_remaining` 里。
2. **隐式默认**：若一个 switch 既不是 `full_case`、模式池也没被耗尽（`pool` 非空），说明存在输入取值落不到任何 case——等价于一条「什么都不做」的默认路径，此时**所有** `remaining` 位都视为悬空，保留不动。

`BitPatternPool`（Yosys 内建）是一个「尚待匹配的信号取值集合」：`pool.take(pattern)` 尝试移除一个取值，成功（是新取值）返回真；`pool.take_all()` 清空；`pool.empty()` 表示 switch 信号已无未覆盖取值。它正确处理了重叠/冗余的 case 标签——只有「新覆盖」的取值对应的 case 才算 `selectable`。

#### 4.2.3 源码精读

函数签名与起点：`signals` 是输入输出兼用的「关心的位」，`remaining` 是其拷贝，逐层剥减。

[src/slang_frontend.cc:340-354](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L340-L354) — 遍历当前 Case 的 `actions`：仅当 `mask.is_fully_ones()`（整位无条件赋值）时，才把对应位从 `remaining` 擦除。部分位赋值（mask 含 0）不会摘除该位，因为那些位在该 action 下并未被全部赋值。

接着下钻 switch，用模式池判断每个 case 的可达性：

[src/slang_frontend.cc:364-397](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L364-L397) — `pool` 初值为 switch 信号的全部取值空间；每个 case 用 `pool.take`（常量标签）或「pool 非空」（非常量标签）判定 `selectable`；默认 case（`compare` 空）用 `pool.take_all()`。可达 case 的悬空位递归并入 `new_remaining`。

最后决定是否替换 `remaining`：

[src/slang_frontend.cc:399-401](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L399-L401) — 仅当 `full_case` 或模式池耗尽（覆盖完备）时，才把 `remaining` 换成 `new_remaining`；否则保留原值（隐式默认路径使全部位悬空）。

#### 4.2.4 代码实践

1. **实践目标**：手算一个小例子，验证对算法的理解。
2. **操作步骤**：考虑下面这段 `always @(*)`：
   ```verilog
   always @(*) begin
       if (en)
           q = d;      // q 在 en=1 分支被赋值，mask 全 1
       // 没有 else
   end
   ```
   把 `q`（4 位）的所有位作为 `signals` 传入 `detect(root_case)`。root_case 的 switches 里有一个 `signal=en` 的 Switch，含两个 case：`compare={S1}`（en=1，其 actions 里赋了 q，递归返回空集）与隐式默认（en=0，无 case 覆盖）。
3. **观察现象**：`en` 的 switch 没有 `full_case` 标记，且模式池在处理完 `compare={S1}` 后仍非空（还剩 `en=0`），所以 [src/slang_frontend.cc:399](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L399) 的条件不成立，`remaining` 保留 q 的全部 4 位。
4. **预期结果**：`detect` 返回 q 的全部 4 位 → 它们都是悬空位 → 推断为 4 个锁存器。这正对应 tests/unit/latch.ys 里 `latch01_gate` 的预期（一个 WIDTH=4 的 `$dlatch`）。

#### 4.2.5 小练习与答案

**练习 1**：如果把上面的例子补全 `else q = 0;`，`detect` 会返回什么？
> **答**：此时 switch 仍非 `full_case`，但两个 case 分别覆盖 `en=1` 与 `en=0`，模式池最终被耗尽（`pool.empty()` 真），故 [src/slang_frontend.cc:399](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L399) 条件成立，`remaining` 换成两个 case 悬空位的并集——两个 case 都赋了 q，并集为空。返回空集 → 无锁存器，纯组合。

**练习 2**：为什么 `detect` 里只有 `mask.is_fully_ones()` 的 action 才摘除位？部分位赋值（如 `q[1] = x`）为什么不算？
> **答**：部分位赋值的 mask 含 0 位，表示该 action 只赋了 q 的某些位，其余位在本 action 下并未赋值。摘除一个位的前提是「本 action 确定赋了它」，故只有 mask 全 1 才满足。部分位赋值的悬空处理留给 `insert_latch_signaling` 用掩码 switch 解决（见 4.4）。

---

### 4.3 handle_comb_like_process：锁存器的发射

#### 4.3.1 概念说明

`handle_comb_like_process` 是组合路径的落地主函数。它做三件事：

1. **遍历块体**，长出 HDL 意图树，并取出本块驱动的全部静态变量位 `all_driven`。
2. **判定悬空位**：调 `detect_possibly_unassigned_subset`，把 `all_driven` 拆成「连续驱动位 `cl`」与「锁存器位 `latch_driven`」。
3. **分两路落地**：连续驱动位走 `add_continuous_driver`（组合）；锁存器位每个发一个 `$dlatch`，并配 `en`/`staging` 两个辅助信号，再用 `insert_latch_signaling` 把意图树改写。

锁存器的核心技巧是**一对辅助信号**：

- `en[i]`：第 i 位的「使能」。某条执行路径赋了该位 → 该路径把 `en[i]` 拉高；没有任何路径赋值 → `en[i]` 保持 0（默认）。
- `staging[i]`：第 i 位的「暂存数据」。某条路径赋了该位 → 该路径把 `staging[i]` 接到右值；没有路径赋值 → `staging[i]` 保持 `x`（默认）。

`$dlatch(EN=en[i], D=staging[i], Q=真实变量位[i], EN_POLARITY=1)` 的语义是：

\[ Q = \begin{cases} \text{staging}[i] & \text{if } en[i]=1 \text{（透明：加载新值）} \\ Q_{\text{prev}} & \text{if } en[i]=0 \text{（保持：锁存旧值）} \end{cases} \]

也就是说，当代码路径要赋值时，把 `en` 拉高、`staging` 给新值，锁存器透明地输出新值；当路径不赋值时，`en` 为 0，锁存器保持。这正是「电平敏感存储」的建模。

#### 4.3.2 核心流程

```
handle_comb_like_process(symbol, body):
    proc ← 新建 RTLIL::Process
    procedure ← 新 ProceduralContext(implicit 时序)
    body.visit(StatementExecutor)          # 长出意图树 root_case + 更新 vstate

    all_driven ← procedure.all_driven()    # 本块驱动的全部静态位

    dangling ← {}
    if 关键字 != AlwaysComb:               # always_comb 不判悬空
        dangling ← detect(all_driven, root_case)

    for bit in all_driven:
        if bit 不在 dangling:  cl/cr ← 位 + vstate 当前值   # 组合路径
        else:                   latch_driven ← 位           # 锁存路径

    if AlwaysLatch 且 cl 非空:  报 LatchNotInferred 警告     # 用户想锁存却没锁存到

    for chunk in latch_driven.chunks():
        en[i], staging[i] ← add_placeholder_signal           # 建辅助线
        addDlatch(EN=en[i], D=staging[i], Q=真实位[i])        # 发锁存器
    root_case 追加默认 aux_actions: en=0, staging=x          # 「不赋值」的默认
    root_case.insert_latch_signaling(signaling)             # 改写意图树

    copy_case_trees_into(proc)                               # 降级成 RTLIL::Process
    add_continuous_driver(cl, cr)                            # 组合位连续驱动
```

#### 4.3.3 源码精读

先建 Process 与意图树，再取 `all_driven`：

[src/slang_frontend.cc:1781-1787](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L1781-L1787) — 新建 `RTLIL::Process`，用 `ProceduralContext`（隐式时序 `ProcessTiming::implicit`）跑 `StatementExecutor` 长出意图树，`all_driven()` 汇总本块驱动的全部静态位。

`all_driven` 的实现只保留 `Variable::Static` 的位（局部自动变量不在此列，因为它们不映射到网表线）：

[src/procedural.cc:141-156](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/procedural.cc#L141-L156) — 从 `vstate.visible_assignments` 取所有被赋值位，排序去重，过滤出 `Static` 变量的位。

然后判悬空——注意 `always_comb` 被排除在外：

[src/slang_frontend.cc:1788-1793](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L1788-L1793) — 仅当关键字不是 `AlwaysComb` 时才调用 `detect_possibly_unassigned_subset`，得到悬空位集合 `dangling`。

把驱动位一分为二（连续驱动 `cl/cr` vs 锁存 `latch_driven`）：

[src/slang_frontend.cc:1799-1807](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L1799-L1807) — 不在 `dangling` 的位取 `vstate.visible_assignments` 的当前值（已经过 `$mux` 合并的最终组合结果）进组合路径；在 `dangling` 的位进锁存路径。

`always_latch` 的特别诊断：用户声明了 `always_latch`，但有些位其实没产生锁存器（说明它们在所有路径都被赋值，等价组合），发警告：

[src/slang_frontend.cc:1809-1814](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L1809-L1814) — `always_latch` 且 `cl`（非锁存位）非空时，按 chunk 报 `LatchNotInferred` 警告。

锁存器的实际发射——逐 chunk 建 `en`/`staging` 占位线，逐位发 `$dlatch`：

[src/slang_frontend.cc:1816-1837](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L1816-L1837) — 对每个锁存 chunk 用 `add_placeholder_signal` 建等宽 `en` 与 `staging` 线；逐位调用 `addDlatch(EN=en[i], D=staging[i], Q=convert_static(chunk[i]), en_polarity=true)`，其中 `Q` 用 `convert_static` 取该位的真实网表线（见 [src/slang_frontend.cc:3394](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L3394)）。`signaling` 字典把每个锁存位映射到 `{en[i], staging[i]}`，供下一步改写。同时把这些位登记进 `driven_variables`/`register_driven_variables`，标记它们已被本块驱动。

接着给根 case 设两条「默认」连线——这是「没有路径赋值」时的 fallback：

[src/slang_frontend.cc:1839-1843](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L1839-L1843) — 在 `root_case->aux_actions` 追加：所有 `enables = 0`（默认不使能→保持）、所有 `staging = x`（默认数据无关）；然后调 `insert_latch_signaling` 把意图树里对锁存位的赋值改写成对 `en`/`staging` 的驱动。

最后降级意图树、处理组合位：

[src/slang_frontend.cc:1846-1847](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L1846-L1847) — `copy_case_tree_into` 把意图树降级成 `RTLIL::Process`（u3-l4 讲过）；`add_continuous_driver` 把组合位 `cl` 用右值 `cr` 连续驱动。

占位线 `add_placeholder_signal` 的实现就是建一根普通线，等待后续 `connect` 接驱动：

[src/builder.cc:704-717](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/builder.cc#L704-L717) — 建一根带暂存属性的 `RTLIL::Wire`，返回其 SigSpec，稍后由 `connect` 接到真实驱动。

连续驱动 `add_continuous_driver` 把 `VariableBits` 转成真实线后接到右值：

[src/slang_frontend.cc:654-672](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L654-L672) — 跳过特殊网（wand/wor），对普通静态位调 `register_driven` 登记、`convert_static` 取线、`connect` 接右值。

#### 4.3.4 代码实践

1. **实践目标**：追踪一个不完整 `if` 赋值，看哪些位被判为锁存、生成什么样的 `$dlatch`。
2. **操作步骤**：阅读 [tests/unit/latch.ys](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/tests/unit/latch.ys) 的 `latch01_gate`。其 `gold` 网表期望一个 `WIDTH=4` 的 `$dlatch`，端口 `EN=en, D=d, Q=q`。若你本地已构建 sv-elab（见 u8-l3），可运行：
   ```bash
   cd tests && bash run.sh unit/latch.ys
   ```
   若没有可执行环境，则为「源码阅读型实践」：在 [src/slang_frontend.cc:1816-1837](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L1816-L1837) 手动模拟：`q` 是 4 位全部悬空 → 1 个 chunk（4 位）→ 建 `en[3:0]`、`staging[3:0]` → 逐位发 4 个 `$dlatch`（综合后 Yosys 会把它们合并成一个 WIDTH=4 的 `$dlatch`）。
3. **观察现象**：`en` 在根 case 默认为 0；在 `en==1` 分支里（由 `insert_latch_signaling` 改写）`staging=d`、`en=1`，于是锁存器透明输出 `d`；其余情况保持。
4. **预期结果**：等价性测试 `equiv_induct` 通过，证明 `gate`（sv-elab 生成）与 `gold`（手写 `$dlatch`）行为一致。运行结果待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：`en`/`staging` 信号为什么要在 `root_case` 设默认值（`en=0`、`staging=x`），而不是在每个 case 分支里显式写「保持」？
> **答**：因为 RTLIL 的 Process 模型里，「某分支不写某信号」就等价于「该信号在该分支保持默认」。把默认设在根 case，让所有「不赋值」的路径自动落到默认（en=0→保持），无需为每条路径显式枚举「保持」动作，大幅减少连线数量。`staging=x` 是因为 en=0 时 D 端无关，给 x 最省事。

**练习 2**：为什么 `always_comb` 跳过 `detect`、绝不推断锁存器？
> **答**：[src/slang_frontend.cc:1789](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L1789) 的 `if` 把 `AlwaysComb` 排除。`dangling` 为空 → 所有位进 `cl/cr` → 全走 `add_continuous_driver` 组合路径。这符合 SV 语义：`always_comb` 漏赋值属于设计错误（应由仿真器警告），综合器按组合处理，不悄悄塞锁存器。

---

### 4.4 insert_latch_signaling：把 HDL 意图改写成 enable/staging

#### 4.4.1 概念说明

到上一节为止，锁存器的 `$dlatch` 单元、`en`/`staging` 线、以及「默认 en=0/staging=x」都已就位。但还差一环：意图树里那些原本对「真实变量位」的赋值（`Case::Action`），现在必须被**改写**成对 `en`/`staging` 的驱动——因为真实变量位已经由 `$dlatch` 的 `Q` 驱动了，不能再由过程块直接驱动，否则就是多驱动冲突。

这就是 `Case::insert_latch_signaling` 的工作：它带着上一步建好的 `signaling` 映射（锁存位 → `{en, staging}`）遍历整棵意图树，把每条触及锁存位的 `Case::Action` 翻译成对 `en`/`staging` 的 `aux_actions`（已物化的真实连线）。

它还要处理一个细节：**部分位赋值（带 mask）**。当一条赋值的 mask 不全为 1（例如 `q[i] = x` 只动了一位，或动态索引 `q[idx]=x` 经 u5-l3/u4-l3 展开成逐位掩码），「赋不赋该位」本身依赖运行时的 mask 位。此时不能简单地把 `en` 拉高，而要再套一层 Switch：仅当 mask 位为 1 时才驱动 `en`/`staging`。

#### 4.4.2 核心流程

```
insert_latch_signaling(issuer, map):     # map: 锁存位 -> {en, staging}
    prepended_switches ← {}
    has_mask_switches ← {}               # 记录哪些位已建过 mask switch

    for action in actions:               # 本 Case 的每条意图赋值
        for i, lbit in action.lvalue 的逐位:
            if lbit 在 map 中（它是锁存位）:
                {en, staging} ← map[lbit]
                if action.mask[i] == S1 且 该位尚未建过 mask switch:
                    # 整位无条件赋值：直接驱动
                    lstaging ← lstaging + staging
                    enables  ← enables  + en
                    rvalue   ← rvalue   + unmasked_rvalue[i]
                else:
                    # 部位赋值：新建一个 signal=mask[i] 的 Switch
                    # 仅在 mask[i]==1 时：staging=rvalue, en=1
                    sw ← new Switch(signal = action.mask[i])
                    case_ ← sw.add_case({S1})
                    case_.aux_actions += {staging, rvalue[i]}
                    case_.aux_actions += {en, S1}
        if lstaging 非空:
            aux_actions += {lstaging, rvalue}        # staging = rvalue
            aux_actions += {enables, S1...}          # en = 1

    # 递归所有子 switch
    for sw in switches:
        for case_ in sw.cases:
            case_.insert_latch_signaling(issuer, map)

    # 把新建的 mask switch 插到本 Case 最前
    switches ← prepended_switches + switches
```

改写后的结果：意图树里不再有「对锁存真实位的 action 驱动」（那些 action 的语义被翻译成了 en/staging 连线），降级时只有 `aux_actions` 会被 `copy_into` 复制进 `RTLIL::Process`（u3-l4 讲过 `copy_into` 只搬 `aux_actions`）。

#### 4.4.3 源码精读

`Case::Action` 的三件套结构——这是被改写的对象：

[src/cases.h:56-63](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/cases.h#L56-L63) — `Action` 含 `loc`（源码位置）、`lvalue`（`VariableBits` 意图左值）、`mask`（逐位掩码）、`unmasked_rvalue`（未掩码右值）。

`insert_latch_signaling` 的整位路径（mask 全 1）与部位路径（mask 含 0）：

[src/cases.h:128-162](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/cases.h#L128-L162) — 遍历 `actions`，逐位查 `map`：若该位是锁存位，按 mask 分两路。mask 位为 1 且尚未建过 mask switch 的位，累积进 `lstaging`/`enables`/`rvalue`，循环结束后一次性发出 `staging=rvalue` 与 `en=1` 两条 `aux_actions`；mask 位为 0（或已建过 switch）的位，新建一个 `signal=mask[i]` 的 Switch，仅 `mask[i]==1` 的 case 里驱动 `staging=rvalue[i]` 与 `en=1`。`has_mask_switches` 防止同一位被多个 action 重复建 switch。

递归子节点并把新建 switch 前插：

[src/cases.h:164-168](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/cases.h#L164-L168) — 递归所有子 switch 的所有 case；最后把本层新建的 mask switch 插到 `switches` 最前，保证 mask 判定先于既有分支结构。

`copy_into` 只搬 `aux_actions`、丢弃 `actions`——这正是改写后意图能落地的原因：

[src/cases.h:84-113](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/cases.h#L84-L113) — `copy_into` 把 `aux_actions` 复制进 `RTLIL::CaseRule`，`actions`（意图）从不被直接复制；它的语义已由 `insert_latch_signaling`（锁存）、`SwitchHelper::finish`（合并）、`add_continuous_driver`（组合）编译掉。

#### 4.4.4 代码实践

1. **实践目标**：理解部位赋值如何触发 mask switch。
2. **操作步骤**：看 tests/unit/latch.ys 的 `latch02_gate`：
   ```verilog
   always @(*) begin
       q[idx+:4] = d;   // 动态起始的范围选择赋值
   end
   ```
   这里 `idx` 是运行时变量，整段 `q` 都是「可能悬空」（任何具体 `idx` 只赋 4 位中的连续 4 位，但 q 是 4 位……实际取决于位宽）。sv-elab 经 u4-l3 的 `AddressingResolver` 把这个动态范围写展开成逐位掩码写入。每个有效位的 mask 由寻址电路动态产生。
3. **观察现象**：在 `insert_latch_signaling` 里，这些位的 `action.mask[i]` 不是常量 S1，而是一个动态信号。于是走 [src/cases.h:144-154](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/cases.h#L144-L154) 的 mask-switch 分支：建一个 `signal=mask[i]` 的 Switch，仅当该位被选中（mask=1）时才 `en=1`、`staging=d[i]`。
4. **预期结果**：每个锁存位都由其各自的 mask 信号门控 en，从而只在真正被赋值时透明。等价性测试用 `latch02_gold`（显式 `for` 循环逐位写）对照证明两者等价。运行结果待本地验证。

#### 4.4.5 小练习与答案

**练习 1**：为什么需要 `has_mask_switches` 这个集合？
> **答**：同一位可能出现在多条 `Action` 里（例如同一 case 里多次部分赋值），或一个 action 处理后该位的 mask switch 已建。`has_mask_switches` 记录「该位已建过 mask switch」，避免重复建 Switch 造成对同一 `en`/`staging` 的多重驱动。整位路径（mask==S1）用 `&& !has_mask_switches.count(lbit)` 守卫同理。

**练习 2**：改写后，原来的 `Case::Action`（意图）还会被复制进 RTLIL 吗？
> **答**：不会。`copy_into`（[src/cases.h:90](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/cases.h#L90)）只复制 `aux_actions`，`actions` 被彻底丢弃。锁存位的 action 语义已由 `insert_latch_signaling` 翻译成 en/staging 的 `aux_actions`；非锁存位的 action 语义由 4.3 节的 `add_continuous_driver` 单独处理。

---

## 5. 综合实践

把本讲三个模块串起来，做一个端到端的追踪任务。

**设计输入**（自行写入一个 `.sv` 文件或 heredoc）：

```verilog
module latch_demo(input logic en, input logic [3:0] d,
                  input logic [1:0] sel, output logic [3:0] q);
    always @(*) begin
        if (en) begin
            q = d;
            q[sel] = 1'b1;     // 在 en 之外再对选定位做部位赋值
        end
        // 没有 else：q 整体悬空
    end
endmodule
```

**任务**：

1. **悬空判定**：用 4.2 的算法手算 `detect(all_driven, root_case)` 的结果。提示：root_case 下有一个 `signal=en` 的 switch，en=1 分支内还有一个 `signal=...`（由 `q[sel]=1` 的部位 mask 产生的）结构。判定 q 的 4 个位是否都在 `dangling` 里。
2. **锁存发射**：根据 4.3，写出会生成几个 `$dlatch`、几个 `en`/`staging` 线，以及根 case 的默认 `aux_actions`。
3. **意图改写**：根据 4.4，说明 `q[sel]=1` 这条部位赋值的 `Action`（mask 含动态位）如何被改写成 mask switch，对 `en[sel]` 的门控如何产生。
4. **验证**：写一个等价的 `gold` 模块（例如用显式 `$dlatch` 加门控逻辑），用 tests/unit/latch.ys 的范式（`async2sync` → `equiv_make` → `equiv_induct` → `equiv_status -assert`）证明等价。

**提示**：第 1 步的关键是认识到 `en` 的 switch 没有覆盖 `en=0`（无 else、非 full_case），所以即使 en=1 分支内赋了 q，整体仍因隐式默认路径而全部悬空。第 3 步的关键是 `sel` 是动态的，`q[sel]=1` 经 AddressingResolver 展开后，每个有效位的 mask 是 `demux` 产生的动态信号，故走 mask-switch 分支。

> 若无本地构建环境，本实践为「源码阅读 + 手算」型：把每一步对应到本讲引用的具体源码行，画出意图树与改写后的 aux_actions 列表即可。运行结果待本地验证。

## 6. 本讲小结

- `handle_comb_like_process` 是 sv-elab 组合路径的落地主函数，服务 `always_comb`、`always_latch` 与隐式 `always @(*)` 三类块；它把驱动位一分为二——连续驱动位（组合）与悬空位（锁存）。
- `detect_possibly_unassigned_subset` 用带 `BitPatternPool` 剪枝的 DFS，按「并集 + 隐式默认」语义求出可能漏赋值的位；只有 `mask` 全 1 的 action 才算「确定赋值」，switch 不完备（非 full_case 且模式池未空）则所有位保留为悬空。
- 锁存器的核心是一对辅助信号：`en`（默认 0）与 `staging`（默认 x），配 `$dlatch(EN=en, D=staging, Q=真实位)` 实现「路径赋值则透明、否则保持」的电平敏感存储。
- `Case::insert_latch_signaling` 把意图树里对锁存位的 `Action` 改写成对 `en`/`staging` 的 `aux_actions`；整位赋值直接拉 en，部位赋值（mask 含动态位）再套一层 mask switch 门控 en。
- `always_comb` 跳过悬空判定、绝不推断锁存器；`always_latch` 若有位未变成锁存器则发 `LatchNotInferred` 警告，提示用户「声明了锁存却没锁存到」。
- 至此单元 6（时序逻辑）的三种形态——组合连续驱动（u6-l1 的组合型）、触发器（u6-l2）、锁存器（本讲）——全部讲完。

## 7. 下一步学习建议

- 进入单元 7 的**存储器推断**（u7-l1）：存储器的 `$memwr_v2`/`$memrd_v2` 与本讲的 `$dlatch` 同属「过程块驱动的存储类单元」，但走 `InferredMemoryDetector` 而非悬空判定。对比两者的判定条件能加深理解。
- 回顾 u4-l3（AddressingResolver）：本讲多处提到「动态索引/部位赋值展开成逐位掩码」，其电路来源就是 AddressingResolver 的 `demux`/`shift_up`。把它们串起来读，能形成「动态左值 → 掩码 → 锁存门控」的完整链条。
- 阅读 [tests/unit/latch_addressing.ys](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/tests/unit/latch_addressing.ys)：它用 `always_latch` + 动态数组索引 + `sat` 形式化验证，综合演示了本讲的 mask switch 与 u4-l3 寻址电路如何协作。

# 关键优化 Pass 详解

## 1. 本讲目标

本讲承接 u4-l1「优化 Pass 框架」，把抽象的 Pass 机制落到五个最具代表性的优化 Pass 上。读完本讲，你应当能够：

- 说清 `arith_simp`（算术化简）、`cse`（公共子表达式消除）、`dce`（死代码消除）、`const_fold`（常量折叠）、`inlining`（内联）各自做的图变换与收益。
- 掌握「一个 Pass 如何向上汇报自己是否改动过图」这一统一契约（`changed` 返回值），以及它如何驱动 fixedpoint 迭代。
- 理解这五个 Pass 在标准优化管线 `optimization_pass_pipeline.txtpb` 中的相对位置与编排逻辑。
- 能用 `opt_main --passes` 单独跑某一类 Pass，逐类对照优化前后的 IR 差异。

## 2. 前置知识

本讲默认你已经学过 u4-l1，熟悉下面几个概念（此处只做最简提示）：

- **Pass（优化遍）**：对 IR 数据流图做一次遍历与变换的独立单元，统一从 `PassBase::Run` 入口进入，子类实现 `RunInternal`。
- **fixedpoint（不动点）**：反复跑一组 Pass，直到没有任何 Pass 再改动图为止。
- **Node（节点）**：IR 图的顶点，靠 `operands`（入边）与 `users`（出边）表达数据依赖（见 u3-l1）。
- **FunctionBase**：`Function` / `Proc` / `Block` 的共同基类，是「持有一堆 Node」的容器。
- **Op 与类别位掩码**：每个运算符带 `kSideEffecting`、`kComparison`、`kAssociative` 等类别标志（见 u3-l2）。

本讲会反复用到两个底层机制，先建立直觉：

1. **替换语义**。优化 Pass 修改图的标准动作不是「原地改 Node」，而是「造一个等价的新表达式，然后把旧 Node 的所有使用点（users）改指向新表达式」。这一动作由 `Node::ReplaceUsesWith(...)` / `ReplaceUsesWithNew<T>(...)` 完成。被替换的旧 Node 暂时变成「无人引用」，留给 DCE 统一回收。
2. **查询引擎（QueryEngine）**。很多优化需要知道「某个 Node 的某些位是不是常数」。本讲的算术化简与常量折叠用的都是最轻量的 [StatelessQueryEngine](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/passes/stateless_query_engine.h#L36-L46)——它只看节点的「局部（peephole）上下文」，`Populate()` 是空操作，任何时候都能 O(1) 调用，代价是推断能力很弱，多数时候只能确认「直接是字面量」的情况。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `xls/passes/pass_base.h` | Pass 的统一 `Run` 入口、`changed` 契约、`IsIdempotent`、`RedundancyGuard`、`FixedPointCompoundPassBase`。 |
| `xls/passes/optimization_pass_pipeline.txtpb` | 标准优化管线的数据驱动定义，记录每个 Pass 的相对顺序。 |
| `xls/passes/arith_simplification_pass.{h,cc}` | 算术化简：除/模/乘/移位/比较的模式匹配与重写。 |
| `xls/passes/cse_pass.{h,cc}` | 公共子表达式消除：等价节点合并。 |
| `xls/passes/dce_pass.{h,cc}` | 死代码消除：从根反向标记存活节点，删除其余。 |
| `xls/passes/constant_folding_pass.{h,cc}` | 常量折叠：全字面量操作数求值替换为字面量。 |
| `xls/passes/inlining_pass.{h,cc}` | 内联：把 `invoke` 替换为被调函数的函数体。 |
| `xls/passes/stateless_query_engine.h` | 上述两个 Pass 依赖的轻量查询引擎。 |
| `xls/tools/opt_main.cc` / `xls/tools/opt_flags.cc` | `opt_main` 的入口与 `--passes`、`--list_passes`、`--skip_passes` 等标志。 |

## 4. 核心概念与源码讲解

### 4.1 公共契约：`changed` 返回值、fixedpoint 与管线编排

#### 4.1.1 概念说明

本讲的五个 Pass 表面上千差万别，但都遵守同一个契约，这个契约由 u4-l1 的 `PassBase` 框架强制执行：

> **每个 Pass 的唯一入口是 `Run`，它必须返回一个布尔值 `changed`——`true` 表示本轮改动了 IR 图，`false` 表示没改动。** 框架拿这个返回值来决定「是否再跑一轮」（fixedpoint）、是否触发不变式校验、以及能否跳过冗余 Pass。

换句话说，子类（如 `DcePass`）只需要在 `RunInternal` 里老实回答「我改没改」，而 bisect 截断、冗余跳过、计时、不变式校验这些横切关注点全部由基类 `Run` 包办。

#### 4.1.2 核心流程

`PassBase::Run` 的执行流程可以概括为：

1. **bisect 截断**：若已达到 `--passes_bisect_limit`，直接返回 `false`（用于二分定位是哪个 Pass 引入了问题）。
2. **黑名单**：若本 Pass 的 short_name 出现在 `--skip_passes`，返回 `false`。
3. **冗余跳过**：向 Pass 询问一个 `RedundancyGuard`（签名），若该签名被标记为「已知冗余」，就跳过真正执行。
4. **真正执行**：调用子类的 `RunInternal`，拿到 `changed`。
5. **不变式校验**：若 `changed` 为真，跑 invariant checker 校验图仍合法；若为假，框架会断言「节点数没变」（防止 Pass 撒谎说没改其实改了）。

冗余跳过是优化速度的关键手段之一。`RedundancyGuard` 有两档：

- `Never()`：永远不跳过；
- `CanSkip([config])`：如果上一轮以相同签名跑过且「没改动」，且此后 IR 再没变过，就跳过。

fixedpoint 的概念是：把若干 Pass 装进 `FixedPointCompoundPassBase`，它反复执行子 Pass，直到一轮下来没有任何子 Pass 返回 `changed`。标准管线里的 `simp`（化简）复合 Pass 就是这种结构。

#### 4.1.3 源码精读

`Run` 的入口与「唯一入口」的注释：

[xls/passes/pass_base.h:405-415](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/passes/pass_base.h#L405-L415)——`Run` 注释明确指出「Returns true if the graph was changed」，且这是 Pass 的唯一入口，子类的 `RunInternal` 才做实事。

`changed` 与 fixedpoint 的关系、不变式断言（防止 Pass「谎报军情」）：

[xls/passes/pass_base.h:483-487](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/passes/pass_base.h#L483-L487)——框架断言：如果 Pass 报告 `changed == false`，那么图的节点数必须保持不变。这是对「changed」语义的硬约束。

`RedundancyGuard` 两档语义：

[xls/passes/pass_base.h:238-268](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/passes/pass_base.h#L238-L268)——`Never()` 与 `CanSkip(config)`，`config` 用来区分同一 Pass 的不同配置（如不同 opt_level）。

冗余签名命中时的跳过逻辑：

[xls/passes/pass_base.h:456-466](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/passes/pass_base.h#L456-L466)——若签名已知冗余则跳过 `RunInternal`，否则真正执行。

这五个 Pass 对 `RedundancyGuard` 与 `IsIdempotent` 的选择各不相同，是一处很有意思的对比（见下表，行号均来自各自的 `.h`）：

| Pass | `IsIdempotent()` | `GetRedundancyGuard` |
| --- | --- | --- |
| arith_simp | `true` ([arith_simplification_pass.h:172](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/passes/arith_simplification_pass.h#L172)) | `CanSkip("O{opt_level}")` |
| cse | 默认 `false` | `CanSkip("literals"/"no_literals")` ([cse_pass.h:176-178](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/passes/cse_pass.h#L176-L178)) |
| dce | `true` ([dce_pass.h:149](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/passes/dce_pass.h#L149)) | `CanSkip()` ([dce_pass.h:151-155](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/passes/dce_pass.h#L151-L155)) |
| const_fold | 默认 `false` | `CanSkip()` ([constant_folding_pass.h:120-124](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/passes/constant_folding_pass.h#L120-L124)) |
| inlining | 默认 `false` | `CanSkip()` ([inlining_pass.h:177-181](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/passes/inlining_pass.h#L177-L181)) |

> 名词解释：
> - **幂等（idempotent）**：连跑两遍与跑一遍结果相同。arith_simp 与 dce 都是幂等的——再跑一遍不会再生效。
> - 注意 cse 没有声明幂等，但它的 `CanSkip` 用了 `literals`/`no_literals` 配置串来区分两种运行模式，避免误判。

管线编排：标准优化管线是数据驱动的，写在一个 textproto 里。下面是 `simp`（化简）复合 Pass 的开头一段，可以看到 `const_fold`、`dce`、`arith_simp`、`cse` 的密集穿插：

[xls/passes/optimization_pass_pipeline.txtpb:23-85](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/passes/optimization_pass_pipeline.txtpb#L23-L85)——`simp` 的 passes 列表里，几乎每跟一个变换 Pass 就跟一个 `dce`（如 `const_fold`(L28)、`dce`(L29)、`arith_simp`(L34)、`dce`(L35)、`cse`(L73)、`dce`(L74)）。这种「变换一次、清理一次」的编排是 XLS 的典型风格。

内联则分两个阶段独立编排：

[xls/passes/optimization_pass_pipeline.txtpb:185-194](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/passes/optimization_pass_pipeline.txtpb#L185-L194)——`full-inlining`（`inlining`，L187）与 `one-leaf-inlining`（`leaf-inlining`，L193），二者前面都先跑 `loop_unroll`、`map_inlining` 把循环和 map 展开成普通 `invoke`，再交给内联 Pass。

#### 4.1.4 代码实践

1. **实践目标**：亲眼看到「changed 契约」如何驱动 fixedpoint，并学会列出 / 单独运行某个 Pass。
2. **操作步骤**：
   - 列出全部可用 Pass：`opt_main --list_passes`（标志定义见 [opt_main.cc:77-78](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/tools/opt_main.cc#L77-L78)，处理逻辑见 [opt_main.cc:269-282](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/tools/opt_main.cc#L269-L282)）。在输出里找到 `arith_simp`、`cse`、`dce`、`const_fold`、`inlining`。
   - 选一个已有的 `.ir` 文件，单独运行某一类 Pass（`--passes` 语法见 [opt_flags.cc:88-100](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/tools/opt_flags.cc#L88-L100)）：
     ```bash
     opt_main --passes "dce"        input.ir > dce.ir
     opt_main --passes "const_fold dce" input.ir > fold.ir
     ```
     注意：一旦传了 `--passes`，**标准管线会被完全忽略**，只跑你指定的 Pass（opt_flags.cc:96-98）。
   - 用 `--skip_passes` 在标准管线上排除某个 Pass，对比差异（[opt_flags.cc:46-48](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/tools/opt_flags.cc#L46-L48)）。
3. **需要观察的现象**：`--passes "dce"` 单独跑时，若输入本就没有死代码，输出应与输入相同（changed=false，节点数不变）；若传了 `const_fold`，输出节点数通常更少。
4. **预期结果**：`--list_passes` 能列出五个目标 Pass 的 short_name；单独运行的输出 IR 语义不变但节点数可能减少。具体文本「待本地验证」。
5. 如果暂时没有 `.ir` 文件，可先用 `ir_converter_main` 把一个 `.x` 转成 `.ir`（见 u1-l5）。

#### 4.1.5 小练习与答案

1. **练习**：为什么标准管线里几乎每跟一个变换 Pass 就跟一个 `dce`？
   **答案**：因为大多数变换 Pass 用「替换 uses」而非「删除节点」来改图，替换后会留下无人引用的旧节点；`dce` 负责统一回收这些垃圾，保持图紧凑，也让后续 Pass 的分析更轻。
2. **练习**：`RedundancyGuard::CanSkip("O3")` 与 `CanSkip()` 的区别是什么？
   **答案**：前者带配置串 `O3`，只有当「上一轮以 `O3` 这个签名跑且没改动、且 IR 此后未变」时才跳过；后者签名只是 Pass 名本身。带 config 是为了区分同一 Pass 在不同 opt_level / 不同字面量策略下的不同运行实例。
3. **练习**：如果一个 Pass 把 `changed` 错误地报成 `false` 但其实删了一个节点，框架会发现吗？
   **答案**：会。见 [pass_base.h:483-487](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/passes/pass_base.h#L483-L487) 的断言：`changed == false` 时要求节点数不变。

---

### 4.2 算术化简（arith_simp）

#### 4.2.1 概念说明

`ArithSimplificationPass`（short_name `arith_simp`）是 XLS 里「重写规则最多」的 Pass。它做的事可以用一句话概括：**识别算术/位运算的特定形状（pattern），用更省硬件的等价形状替换掉**。

为什么需要它？因为硬件里不同的运算代价天差地别：

- 除法器（`udiv`/`sdiv`）面积大、延迟高，而「除以常量」可以用「乘法 + 移位」的 magic multiplication 等价替换，面积小得多。
- 移位（`shll`/`shrl`/`shra`）若移位量是常量，等价于 `bit_slice` + `concat`，不需要一个桶形移位器（barrel shifter）。
- `add(not(x), 1)` 在补码下就是 `neg(x)`，可以省掉一次按位取反加一。
- `(x + C0) == C1` 因为加法是单射（injective）运算，可以化简为 `x == (C1 - C0)`，少一个加法器。

它声明自己是幂等的（[arith_simplification_pass.h:172](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/passes/arith_simplification_pass.h#L172)），意思是跑到底后再跑也不会再变。

#### 4.2.2 核心流程

`arith_simp` 的主循环是一个**自带 fixedpoint 的 do-while**：

```
构造 StatelessQueryEngine
do:
    pass_changed = false
    按「逆拓扑序」遍历所有 Node：
        调用 MatchArithPatterns(opt_level, node, query_engine)
        若返回 true（本节点被重写）：
            pass_changed = true
while pass_changed
返回 changed
```

逆拓扑序（从结果往输入）遍历是有讲究的：先处理靠近输出的节点，能让靠近输入的节点在最后一轮拿到最新的常量信息。每个节点的判定交给一个巨大的函数 `MatchArithPatterns`，它内部是一长串 `if` 模式匹配——命中一个就 `ReplaceUsesWithNew<...>` 重写并返回 `true`，没命中返回 `false`。

部分典型重写规则（均来自 `MatchArithPatterns`）：

| 原形状 | 替换为 | 触发条件 |
| --- | --- | --- |
| `add(not(x), 1)` | `neg(x)` | 补码恒等式 |
| `[us]mul(x, 2^K)` | `shll(x, K)` | 乘以 2 的幂 |
| `udiv(x, 2^K)` | `shrl(x, K)` | 除以 2 的幂 |
| `umod(x, 2^K)` | 取低 K 位 | 模 2 的幂 |
| `udiv(x, K)`（K 非 2 的幂） | magic mul + 移位 | 常量除数 |
| `shll/shrl(x, K)`（K 常量） | `concat(slice(x), 0)` | 常量移位免去桶形移位器 |
| `ext(ext(x, w0), w1)` | `ext(x, w1)` | 嵌套扩展合并 |
| `not(cmp(x,y))` | 反向比较 | 仅当 cmp 只有 not 一个用户 |
| `(x + C0) == C1` | `x == (C1 - C0)` | 单射运算的逆 |
| `(decode(N)) - 1` | `not(all_ones << N)` | 省掉一个加法器 |

> 名词解释：
> - **magic multiplication（魔法乘法）**：把「除以常量 K」改写成「乘以一个定点常数 m 再右移」的算法，源自论文 *Division by Invariant Integers using Multiplication*。核心思想是寻找常数 \(m\) 与移位量，使得 \(\lfloor (m \cdot x) / 2^{\text{post\_shift}} \rfloor = \lfloor x / K \rfloor\) 对所有 \(x\) 成立。

#### 4.2.3 源码精读

主循环（自带 fixedpoint 的 do-while）：

[xls/passes/arith_simplification_pass.cc:2375-2399](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/passes/arith_simplification_pass.cc#L2375-L2399)——构造 `StatelessQueryEngine`，按逆拓扑序遍历，每个节点交给 `MatchArithPatterns`，命中则置 `pass_changed`，循环直到一轮无改动。注意它跳过 `IsDead()` 节点（避免对将被 DCE 清理的节点做无谓工作）。

`RedundancyGuard` 带 opt_level 配置：

[xls/passes/arith_simplification_pass.cc:2369-2373](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/passes/arith_simplification_pass.cc#L2369-L2373)——签名形如 `arith_simp<O3>`，不同 opt_level 视为不同运行实例。原因：很多规则（如 `NarrowingEnabled`、`SplitsEnabled` 控制的窄化/拆分）只在较高 opt_level 开启。

模式匹配的总入口 `MatchArithPatterns` 与第一条规则：

[xls/passes/arith_simplification_pass.cc:1220-1241](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/passes/arith_simplification_pass.cc#L1220-L1241)——`Add(Not(x), 1) => Neg(x)`，注释给出补码理由 `~x + 1 == -x (mod 2^N)`，并处理了交换律的另一种操作数顺序。

常量移位改写为 slice + concat：

[xls/passes/arith_simplification_pass.cc:1513-1544](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/passes/arith_simplification_pass.cc#L1513-L1544)——`(val << lit) -> concat(slice, 0)`、`(val >> lit) -> concat(0, slice)`，移位量 ≥ 位宽时直接替换为 0，移位量为 0 时是 no-op。注释解释：低层 IR 的移位隐含一个桶形移位器，常量移位不需要它。

单射运算比较的化简 `MatchComparisonOfInjectiveOp`：

[xls/passes/arith_simplification_pass.cc:307-326](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/passes/arith_simplification_pass.cc#L307-L326)——匹配 `(X + C0) cmp C1` 这类「单射运算的结果与常量比较」，支持 `kAdd`/`kSub`/`kUMul`，用逆运算把常量「搬到一边」，把依赖 X 的运算消掉。

常量无符号除法的 magic multiplication 实现：

[xls/passes/arith_simplification_pass.cc:576-693](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/passes/arith_simplification_pass.cc#L576-L693)——`MatchUnsignedDivideByConstant`，把 `udiv(x, K)` 替换为「乘以定点常数 m、取整数部分、再 post_shift」，核心表达式是 `SRL(mulhi(m, SRL(numerator, pre_shift)), post_shift)`。对「常量被除数 + 变量除数」的小除数情况，则改成查表（LUT）。

#### 4.2.4 代码实践

1. **实践目标**：观察 `arith_simp` 把「除以常量」和「常量移位」换成更省硬件的形状。
2. **操作步骤**：把下面这段手写 IR 存为 `arith_demo.ir`（**示例代码**，非项目自带）：
   ```ir
   package arith_demo

   fn demo(x: bits[8]) -> bits[8] {
     lit4: bits[8] = literal(value=4)
     ret div: bits[8] = udiv(x, lit4)
   }
   ```
   然后单独跑算术化简：
   ```bash
   opt_main --passes "arith_simp dce" arith_demo.ir
   ```
3. **需要观察的现象**：`udiv(x, 4)` 应被替换为右移两位（除以 4 是 2 的幂），进而右移常量又被改成 `concat` + `bit_slice`；最终 IR 里不应再有 `udiv` 节点。
4. **预期结果**：输出中 `udiv` 消失，出现 `bit_slice`/`concat`/`literal` 组合。具体节点名「待本地验证」。
5. 再把除数改成 3（非 2 的幂）重跑，对比输出里是否出现 `umul`（magic multiplication）。

#### 4.2.5 小练习与答案

1. **练习**：为什么 `arith_simp` 选择「逆拓扑序」遍历而不是正拓扑序？
   **答案**：逆拓扑序先处理靠近输出的节点；这样当处理靠近输入的节点时，其下游已被化简，查询引擎能看到更准确的常量信息，一轮内能传播更多化简，减少 fixedpoint 轮数。
2. **练习**：`umod(x, 1)` 会被化简成什么？
   **答案**：根据 [arith_simplification_pass.cc:1282-1287](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/passes/arith_simplification_pass.cc#L1282-L1287)，模 1 或模 0 直接替换为字面量 `0`。
3. **练习**：为什么 `arith_simp` 把 `RedundancyGuard` 写成 `CanSkip("O{opt_level}")` 而不是 `CanSkip()`？
   **答案**：因为不同 opt_level 下开启的规则集不同（窄化、拆分等依赖 `NarrowingEnabled`/`SplitsEnabled`），相同 IR 在 `O2` 下「无改动」不意味着在 `O3` 下也无改动，必须用 opt_level 区分签名。

---

### 4.3 公共子表达式消除（CSE）与死代码消除（DCE）

这两个 Pass 经常成对出现，因为 CSE 制造死代码、DCE 负责清扫，所以放在一起讲。

#### 4.3.1 概念说明

**CSE（Common Subexpression Elimination，公共子表达式消除）**：如果图里有两个**完全等价**的运算节点（同样的运算符、同样的操作数、同样的附加配置、同样的结果类型），它们在硬件上就是「重复计算同一份结果」，应当合并成一个、让两个使用点共享。在硬件里这意味着「共享一块组合逻辑」，直接省面积。

**DCE（Dead Code Elimination，死代码消除）**：从「必须保留」的根节点（函数返回值、有副作用的运算、Proc 的状态更新等）出发反向可达性分析，凡不可达又无副作用的节点都是「死代码」，可以删除。CSE 把某个重复节点替换成代表节点后，旧节点就失去了所有 user，正是靠 DCE 把它真正从图里移除。

二者的等价与存活判定都和「副作用」紧密相关（见 u3-l2 的 `kSideEffecting` 类别）：有副作用的运算（`assert`、`send`、`receive`、`trace` 等）既不能被 CSE 合并（合并会改变可观测的副作用次数），也不能被 DCE 删除。

#### 4.3.2 核心流程

**CSE 的流程**：

```
构造 CseNodeArena（带「是否合并 literal」开关）
按拓扑序遍历每个 Node n：
    为 n 求一个规范的 CseNode（op + 规范化的操作数 + misc_data + type）
    把 n 放进「CseNode -> [Node...]」的等价桶
（鸽巢原理短路：若桶数 + 死节点数 == 总节点数，说明无可合并，返回 false）
对每个大小 > 1 的等价桶：
    选一个名字最短的代表节点 representative
    把桶内其余节点用 representative 做 ReplaceUsesWith
返回 changed=true
```

「等价」的精确定义（CseNode 的相等）：

\[ \text{equiv}(a, b) \iff a.op = b.op \land a.type = b.type \land a.misc = b.misc \land a.operands = b.operands \]

其中对**可交换**运算（`kCommutative`，如 `add`/`and`）的操作数会先按 id 排序再比较，保证 `add(x,y)` 与 `add(y,x)` 被识别为同一个表达式。`misc_data` 装的是「不在 (op, operands, type) 三元组里的区分信息」，比如 `bit_slice` 的 `start`、`tuple_index` 的 `index`、`literal` 的值；对于永远不该合并的副作用/控制节点，则塞一个全局唯一 id 强制「不相等」。

**DCE 的流程**（工作表法）：

```
is_deletable(n) = 没有 implicit use 且 不是 invoke 且（无副作用 或 是 gate）
工作表初始化 = 所有「无 user 且可删除」的节点
while 工作表非空：
    取出 node
    对它的每个唯一操作数 operand：
        若 operand 只有这一个 user（HasSingleUse）且可删除：加入工作表
    RemoveNode(node)
返回 removed_count > 0
```

这是一种「自底向上」的传播：先删叶子级死节点，删完后它的操作数可能也变死，继续删。

> 名词解释：
> - **implicit use（隐式使用）**：某些节点即使没有显式 user 也必须保留，比如 Block 的输出端口、Proc 的 state read。`HasImplicitUse` 用来识别它们，使其免于被删。
> - **gate 是 DCE 的例外**：`gate` 虽带副作用类别，但 DCE 允许删除它（见下文源码）。

#### 4.3.3 源码精读

**CSE**：等价节点的表示 `CseNode` 与相等/哈希。

[xls/passes/cse_pass.cc:74-155](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/passes/cse_pass.cc#L74-L155)——`CseNode` 持有 `op_`、`operands_`、`misc_data_`、`type_`；`operator==` 逐一比较这四项，`AbslHashValue` 对它们求哈希。注意 `id_` 仅用于给可交换操作数排序，不参与相等与哈希。

「永远不合并」的副作用/控制节点名单：

[xls/passes/cse_pass.cc:300-321](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/passes/cse_pass.cc#L300-L321)——`assert`、`cover`、`param`、`receive`、`send`、`state_read`、`register_read/write`、`input/output_port` 等都塞入唯一 `non_cse_id_`，保证它们彼此不等价、不会被合并；而 `gate` 走的是普通路径（可合并）。

`RunCse` 主流程：拓扑序建桶、鸽巢短路、选代表、替换。

[xls/passes/cse_pass.cc:406-461](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/passes/cse_pass.cc#L406-L461)——按拓扑序遍历（[cse_pass.cc:416-417](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/passes/cse_pass.cc#L416-L417)），跳过 `IsDead()` 节点（避免对 invoke 等死节点误判为「改动」），鸽巢短路在 [cse_pass.cc:433-435](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/passes/cse_pass.cc#L433-L435)，选代表用 `CompareName`（名字最短、其次 id 最小）保证确定性，最后 `ReplaceUsesWith`（[cse_pass.cc:455](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/passes/cse_pass.cc#L455)）。

`CsePass` 还暴露了一个独立函数 `RunCse`（[cse_pass.h:35-37](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/passes/cse_pass.h#L35-L37)），供其他 Pass 在内部直接调用并收集替换映射 `replacements`。

**DCE**：可删除性判定与工作表传播。

[xls/passes/dce_pass.cc:35-81](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/passes/dce_pass.cc#L35-L81)——`is_deletable` 判定（[dce_pass.cc:38-46](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/passes/dce_pass.cc#L38-L46)）：排除有 implicit use 的、排除 `Invoke`（留给内联处理）、排除有副作用的（但 `gate` 例外）。工作表用 `ForEachUnique` 处理重复操作数，配合 `HasSingleUse` 决定是否把操作数加入待删队列，最后 `RemoveNode` 真正移除，返回 `removed_count > 0`。

`Invoke` 不被 DCE 删除的原因写在注释里：

[xls/passes/dce_pass.cc:39-43](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/passes/dce_pass.cc#L39-L43)——被调函数可能有副作用，所以 DCE 保守地不删 invoke，把它留给 4.4 的 `InliningPass`。

#### 4.3.4 代码实践

1. **实践目标**：验证 CSE 合并重复子表达式、DCE 回收旧节点。
2. **操作步骤**：把下面这段手写 IR 存为 `cse_demo.ir`（**示例代码**，注意 `and.1` 与 `and.2` 完全等价）：
   ```ir
   package cse_demo

   fn demo(x: bits[8], y: bits[8]) -> bits[8] {
     and.1: bits[8] = and(x, y)
     and.2: bits[8] = and(x, y)   // 与 and.1 等价
     ret or.3: bits[8] = or(and.1, and.2)
   }
   ```
   依次跑：
   ```bash
   opt_main --passes "cse"        cse_demo.ir   # 合并：and.2 的 use 指向 and.1
   opt_main --passes "cse dce"    cse_demo.ir   # 合并后清扫死节点 and.2
   ```
3. **需要观察的现象**：第一步输出里 `or` 的两个操作数应都指向同一个 `and` 节点（`and.2` 被 `and.1` 替换），但 `and.2` 可能仍在；第二步后 `and.2` 被删除，节点数减少。
4. **预期结果**：`cse` 单跑后 `or` 的两路相同；`cse dce` 后只剩一个 `and`。具体节点名「待本地验证」。
5. 思考：如果把 `and` 换成有副作用的 `assert`，CSE 还会合并吗？为什么？（见 4.3.5）

#### 4.3.5 小练习与答案

1. **练习**：为什么 CSE 对可交换运算要先排序操作数再比较？
   **答案**：否则 `add(x, y)` 与 `add(y, x)` 会因为操作数顺序不同被判为不等价而漏掉合并。排序后二者归一为同一 `CseNode`。（见 [cse_pass.cc:206-209](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/passes/cse_pass.cc#L206-L209)）
2. **练习**：DCE 为什么不删 `invoke` 节点？
   **答案**：被调函数可能有副作用，DCE 无法静态确认其可删，故保守保留，交给 `InliningPass` 把它内联展开后再由 DCE 处理（见 [dce_pass.cc:39-43](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/passes/dce_pass.cc#L39-L43)）。
3. **练习**：`literal` 节点会被 CSE 合并吗？
   **答案**：取决于 `common_literals` 开关。`CsePass` 默认构造时 `common_literals=true`（[cse_pass.h:171](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/passes/cse_pass.h#L171)），此时值相同的字面量会被合并；若设为 `false`，每个字面量被塞入唯一 id，互不合并（[cse_pass.cc:286-299](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/passes/cse_pass.cc#L286-L299)）。

---

### 4.4 常量折叠（const_fold）与内联（inlining）

#### 4.4.1 概念说明

**常量折叠（Constant Folding，`const_fold`）**：如果一个运算的所有操作数在编译期都是已知常量，那么这个运算的结果在编译期就能算出来——直接用一个 `literal` 节点替换整个运算。例如 `add(literal(2), literal(3))` 直接折叠成 `literal(5)`。这是最基础也最强大的优化之一：它能把一整片常量表达式塌缩成一个常量。

**内联（Inlining，`inlining`）**：把函数调用 `invoke(callee, args)` 替换为被调函数 `callee` 的函数体本身（参数用实参代入）。内联本身不一定让图变小，但它「拍平」了调用边界，让 CSE、DCE、常量折叠等 Pass 能跨函数边界生效——尤其当实参是常量时，内联后常量折叠能把整段被调函数算成常量。

内联有两个深度，由 `InliningPass::InlineDepth` 控制（[inlining_pass.h:169-172](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/passes/inlining_pass.h#L169-L172)）：

- `kFull`（名字 `inlining`）：把所有函数递归内联进 top，直到只剩 top 一个实体。
- `kLeafOnly`（名字 `leaf-inlining`）：只内联叶子函数（不调用别的函数的函数），以及「只有一个调用者且其被调都是叶子」的函数，给其它 Pass 在更小图上优化的机会。

注意：内联是 Package 级 Pass（继承 `OptimizationPass` 而非 `OptimizationFunctionBasePass`），因为它要跨函数操作调用图。它的 `RunInternal` 接收 `Package*` 而非 `FunctionBase*`（[inlining_pass.h:193-196](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/passes/inlining_pass.h#L193-L196)）。

#### 4.4.2 核心流程

**常量折叠流程**：

```
构造 StatelessQueryEngine
按拓扑序遍历每个 Node：
    若 NodeIsConstantFoldable(node)：
        收集各操作数的已知 Value
        用 InterpretNode(node, operand_values) 在编译期求值
        用 ReplaceUsesWithNew<Literal>(result) 替换
返回 changed
```

`NodeIsConstantFoldable` 同时满足以下条件才折叠：

1. 有 user 或有 implicit use（否则是死代码，折叠无意义）；
2. 自己不是 `Literal`（已是常量）；
3. 类型不含 token（token 表示顺序约束，不能折叠成字面量）；
4. 无副作用，或本身是 `gate`（gate 在条件与数据都常量时可折叠成直通或零）；
5. 所有操作数都被查询引擎判为「完全已知」。

折叠后旧的运算节点失去 user，由随后的 DCE 清扫（管线里 const_fold 后总跟一个 dce）。

**内联流程**：

```
构造调用图 CallGraph
按调用图后序（叶子先）确定待内联函数序列
for 每个函数 f（kFull 用全后序；kLeafOnly 用 GetFunctionsToInlineByLeaf）：
    for f 中的每个 invoke（且 IsInlineable，即非 foreign function）：
        InlineInvoke(invoke):
            建立 参数 -> 实参 的映射
            按拓扑序克隆被调函数的每个节点到调用方，
                引用参数处替换为实参
            传播名字（参数名前缀 -> 实参名前缀）
            对 cover/assert 去重标签（加调用方前缀，保证 Verilog 唯一）
            用被调函数返回值替换 invoke，再删除 invoke
返回 changed
```

后序处理（叶子先）是关键：当函数 Foo 被内联进调用方时，Foo 内部已不再残留可内联的 invoke，避免重复工作。

#### 4.4.3 源码精读

**常量折叠**：可折叠性判定。

[xls/passes/constant_folding_pass.cc:40-61](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/passes/constant_folding_pass.cc#L40-L61)——五条判定：无 user 且无 implicit use 直接 false（L41-44）、已是 Literal 跳过（L45-48）、含 token 跳过（L49-52）、有副作用且非 gate 跳过（L53-56）、最后要求所有操作数 `IsFullyKnown`（L58-60）。

折叠主循环：编译期求值并替换为字面量。

[xls/passes/constant_folding_pass.cc:65-90](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/passes/constant_folding_pass.cc#L65-L90)——按拓扑序遍历，命中可折叠节点后用 `InterpretNode`（复用 IR 解释器，见 u6-l1）在编译期求值，再 `ReplaceUsesWithNew<Literal>(result)`（[constant_folding_pass.cc:83-84](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/passes/constant_folding_pass.cc#L83-L84)）。这是一个「编译期复用运行期求值器」的设计。

**内联**：单次内联 `InlineInvoke`。

[xls/passes/inlining_pass.cc:108-217](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/passes/inlining_pass.cc#L108-L217)——先建「被调函数参数 → invoke 实参」映射（L111-115），再按拓扑序把被调函数每个节点 `CloneInNewFunction` 到调用方（L133-135），引用参数处透明替换为实参；最后处理名字传播与 cover/assert 标签去重（L165-212），用返回值替换 invoke 并删除 invoke（L214-216）。

内联主驱动：调用图后序、逐 invoke 内联。

[xls/passes/inlining_pass.cc:267-300](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/passes/inlining_pass.cc#L267-L300)——构造 `CallGraph`，`kFull` 用全后序 `FunctionsInPostOrder`、`kLeafOnly` 用 `GetFunctionsToInlineByLeaf`（L279-283），对每个函数的可内联 invoke 调 `InlineInvoke`。`IsInlineable` 排除 foreign function（[inlining_pass.cc:100-103](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/passes/inlining_pass.cc#L100-L103)）。

两种深度的注册：

[xls/passes/inlining_pass.cc:302-307](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/passes/inlining_pass.cc#L302-L307)——`REGISTER_OPT_PASS(InliningPass, kFull)` 注册为 `inlining`；`leaf-inlining` 在 module initializer 里单独注册（`ConfiguredName` 把两种深度映射到不同名字，见 [inlining_pass.h:203-210](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/passes/inlining_pass.h#L203-L210)）。

#### 4.4.4 代码实践

1. **实践目标**：观察常量折叠塌缩常量表达式、内联展开函数调用。
2. **操作步骤（常量折叠）**：把下面这段手写 IR 存为 `fold_demo.ir`（**示例代码**）：
   ```ir
   package fold_demo

   fn demo() -> bits[8] {
     l2: bits[8] = literal(value=2)
     l3: bits[8] = literal(value=3)
     ret add: bits[8] = add(l2, l3)
   }
   ```
   跑 `opt_main --passes "const_fold dce" fold_demo.ir`。
3. **需要观察的现象**：`add(2, 3)` 应被替换为 `literal(value=5)`，原 `l2`、`l3`、`add` 被删除，最终函数体只剩一个值为 5 的 literal。
4. **预期结果**：输出形如 `ret literal.4: bits[8] = literal(value=5)`（节点名「待本地验证」）。
5. **操作步骤（内联）**：项目测试里有一个标准范例，直接阅读即可。见 [inlining_pass_test.cc:71-77](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/passes/inlining_pass_test.cc#L71-L77)，`caller` 里的 `invoke(..., to_apply=callee)` 经 `inlining` 后被 `callee` 的函数体（`add(x, y)`）替换。把这段 IR 存盘后跑 `opt_main --passes "inlining dce" demo.ir` 验证。
6. 进阶：把内联与常量折叠串起来 `opt_main --passes "inlining const_fold dce" demo.ir`，当 `callee` 的实参都是常量时，内联 + 折叠可把整个调用算成一个常量。

#### 4.4.5 小练习与答案

1. **练习**：为什么常量折叠要求节点「有 user 或有 implicit use」才折叠？
   **答案**：没有 user 又没有 implicit use 的节点是死代码，折叠它毫无意义（结果没人用），应留给 DCE 删除（[constant_folding_pass.cc:41-44](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/passes/constant_folding_pass.cc#L41-L44)）。
2. **练习**：`kFull` 与 `kLeafOnly` 内联的区别是什么？为什么需要 `kLeafOnly`？
   **答案**：`kFull` 把所有函数递归内联进 top，最终只剩 top；`kLeafOnly` 只展开叶子函数和「单调用者链」，保留函数边界，让其它 Pass 能在较小的图上反复优化，再决定是否进一步内联（[inlining_pass.cc:279-283](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/passes/inlining_pass.cc#L279-L283)）。
3. **练习**：内联一个 `cover` 节点时，为什么要改它的标签？
   **答案**：内联可能让同一个 coverpoint 在不同调用点被复制多份，而 Verilog 里 cover property 的名字必须唯一，所以给标签加上「调用方名 + inline 计数 + 原标签」前缀去重（[inlining_pass.cc:181-194](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/passes/inlining_pass.cc#L181-L194)）。

## 5. 综合实践

把五个 Pass 串起来，亲手制造并消除三类冗余。把下面这段「同时含可折叠常量、重复子表达式、函数调用」的 IR 存为 `combo.ir`（**示例代码**）：

```ir
package combo

fn helper(a: bits[8], b: bits[8]) -> bits[8] {
  ret h: bits[8] = add(a, b)
}

fn top(x: bits[8], y: bits[8]) -> bits[8] {
  l2: bits[8] = literal(value=2)
  l3: bits[8] = literal(value=3)
  folded: bits[8] = add(l2, l3)          // 可常量折叠 -> 5
  a1: bits[8] = and(x, y)
  a2: bits[8] = and(x, y)                 // 与 a1 重复 -> CSE
  call: bits[8] = invoke(folded, a2, to_apply=helper)  // 可内联
  ret out: bits[8] = or(a1, call)
}
```

按下面顺序逐步运行，每步用 `diff` 对照上一步：

```bash
opt_main combo.ir > full.ir                       # 完整默认管线
opt_main --passes "inlining dce"      combo.ir > s1.ir   # 仅内联
opt_main --passes "const_fold dce"    combo.ir > s2.ir   # 仅折叠
opt_main --passes "cse dce"           combo.ir > s3.ir   # 仅 CSE
diff combo.ir s1.ir
diff s1.ir full.ir
```

要求你回答：

1. 单独跑 `inlining dce` 后，`invoke` 消失了吗？`helper` 的函数体去哪了？
2. 单独跑 `const_fold dce` 后，`folded` 变成了什么？`l2`/`l3` 还在吗？
3. 单独跑 `cse dce` 后，两个 `and` 还剩几个？
4. 完整管线下，最终 `top` 里还剩几个节点？为什么 `a1`（或其代表）一定不会被消除？
5. 结合本讲所学，解释「为什么标准管线要把这些 Pass 反复穿插」——哪些 Pass 制造了让另一些 Pass 生效的机会？

> 提示：内联把 `helper` 的 `add(folded, a2)` 暴露到 `top`；若先折叠则 `folded` 变成常量 5，内联后 `add(5, a1)` 又可被进一步分析。多个 Pass 互为「铺路者」，这正是 fixedpoint 复合 Pass 存在的意义。

## 6. 本讲小结

- 五个 Pass 都遵守同一契约：`Run` 返回布尔 `changed`，框架据此驱动 fixedpoint、做不变式校验与冗余跳过；`changed==false` 时节点数必须不变。
- **arith_simp** 用 `StatelessQueryEngine` 在逆拓扑序上做模式匹配重写，自带 fixedpoint；核心收益是把除/模/乘/移位/比较换成更省硬件的等价形状（magic multiplication、slice+concat 等）。
- **CSE** 用 `CseNode`（op + 规范化操作数 + misc_data + type）定义等价类，合并重复运算共享逻辑；副作用/控制节点强制唯一不合并。
- **DCE** 用工作表法自底向上删除无 user、无副作用（gate 除外）、非 invoke 的死节点，是 CSE/折叠/内联之后的「清道夫」。
- **const_fold** 在编译期用 `InterpretNode` 求值「全常量操作数」的运算并替换为字面量，是「编译期复用运行期」的典型设计。
- **inlining** 按调用图后序把 `invoke` 替换为被调函数体（`kFull` 全展开 / `kLeafOnly` 仅叶子），拍平边界以释放后续 Pass 的跨函数优化能力。

## 7. 下一步学习建议

- **下一讲 u4-l3「查询与分析引擎」**：本讲反复出现的 `StatelessQueryEngine` 其实是查询引擎家族里最弱的一个。下一讲会讲更强的 `bdd_query_engine`（基于 BDD 推断每个位的已知值/区间）与 `forwarding_query_engine`，解释它们如何让 arith_simp 等 Pass 看到更深层的常量信息（例如本讲的「mask 比较化简」就依赖更强的区间分析）。
- **延伸阅读源码**：把本讲的 `MatchArithPatterns` 当成练习场，试着读一两个你没见过的重写规则；再对照 `xls/passes/basic_simp`、`comparison_simp`、`select_simp` 等同族化简 Pass，体会「一个 Pass 只做一类变换」的拆分哲学。
- **与下游衔接**：这些 Pass 产出的精简 IR 是 u4-l4（延迟模型）与 u4-l5（流水线调度）的输入。建议在读完本讲后，回头看一眼 `optimization_pass_pipeline.txtpb` 的 `prepare-for-scheduling` 段，理解优化如何为调度做准备。

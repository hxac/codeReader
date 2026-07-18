# 层次管理与展平：hierarchy / flatten

## 1. 本讲目标

读完前面几讲，我们已经知道一次综合的数据流是「前端读 HDL → 一串 pass 变换 RTLIL → 后端写网表」。但 Verilog 设计往往不是「一个大模块」，而是「顶层模块层层例化子模块」的树状层次。本讲就回答三个问题：

1. **`hierarchy` 是怎么把这种例化树整理清楚的**——它如何确定顶层、如何为每个例化找到对应的模块定义、如何处理参数化模块、如何删掉用不到的模块。
2. **`flatten` 是怎么把这棵树「拍平」成一个模块的**——把子模块的内容直接内联进父模块。
3. **`submod` / `uniquify` 是反方向与正交方向的工具**——`submod` 把一个模块的一部分拆成新子模块（造层次），`uniquify` 给被多次例化的模块做「每人一份」的副本。

学完本讲你应当能够：

- 说清 `hierarchy -top`、`-auto-top`、`-check` 各自做什么，以及参数化模块在 `hierarchy` 中是如何被「派生（derive）」成具体模块的。
- 读懂 `hierarchy` 的主循环（`hierarchy_worker` → `expand_module` → `hierarchy_clean`）并解释它为什么要循环到不动点。
- 解释 `flatten` 用拓扑排序决定展平顺序、用名字拼接避免冲突、用 `$scopeinfo` 保留溯源信息。
- 会用 `stat` 在展平前后对比模块数与单元数，并用 `keep_hierarchy` 属性阻止某个子模块被展平。

## 2. 前置知识

在进入源码前，先用通俗语言澄清几个概念。

- **例化（instantiation）与层次（hierarchy）**：Verilog 里 `add4 u_add(.a(a), ...);` 表示「在当前模块里放一个 `add4` 类型的实例，起名 `u_add`」。综合后这对应 RTLIL 里一个 `Cell`，它的 `type` 是被例化模块的名字。多个模块互相例化就形成一棵「例化树」。
- **顶层（top module）**：例化树中不被任何模块例化的那个根节点。综合只关心从 top 出发「可达」的那部分设计，其余模块应当被丢弃。
- **参数化模块（parametric module）**：带 `parameter` 的模块，如 `module fa #(parameter W=8) (...)`。同一份源码对应不同 `W` 时，逻辑上是「不同的模块」。Yosys 用「派生（derive）」机制为每组参数生成一个具体副本。
- **黑盒（blackbox）**：只声明端口、不含实现的模块（如工艺库单元）。`hierarchy` 默认会保留它们，`flatten` 默认不会展平它们。
- **不动点（fixpoint）**：反复执行某操作直到「再做也不会改变」的状态。`hierarchy` 处理参数与接口时需要多轮迭代，直到一轮里什么都没改变为止。
- **拓扑排序（topological sort）**：把有向无环图的节点排成线性序列，使每条边 `u→v` 都满足 `u` 排在 `v` 前面。`flatten` 用它决定「先展平谁」。

本讲承接 u2（RTLIL 数据结构）、u3（Module/Cell/Wire/SigSpec 接口）、u4（Pass 系统、ScriptPass）的认知：你已经知道 `Cell` 用 `type` 引用模块、用 `connections_` 表达端口连接，也知道 `synth` 这种 ScriptPass 会按阶段调用一连串子 pass。`hierarchy` 正是 `synth` 在 `begin` 阶段调用的第一个子 pass。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [passes/hierarchy/hierarchy.cc](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/hierarchy/hierarchy.cc) | 本讲主角。注册 `hierarchy` 命令，确定 top、展开例化（含参数派生）、清理无用模块、处理位置参数/wand-wor/端口宽度等收尾。 |
| [passes/hierarchy/flatten.cc](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/hierarchy/flatten.cc) | 注册 `flatten` 命令，把子模块内容内联进父模块，靠拓扑排序定序、靠名字拼接防冲突。 |
| [passes/hierarchy/submod.cc](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/hierarchy/submod.cc) | 注册 `submod` 命令，把一组 cell 抽出来做成新子模块（「反向 flatten」）。 |
| [passes/hierarchy/uniquify.cc](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/hierarchy/uniquify.cc) | 注册 `uniquify` 命令，给被多次例化的模块做独立副本。 |
| [frontends/ast/ast.cc](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/ast/ast.cc) | `AstModule::derive` 真正实现「按参数克隆 AST 并重新生成 RTLIL」，是 `hierarchy` 派生参数化模块时的底层回调。 |
| [kernel/rtlil.cc](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/rtlil.cc) | `RTLIL::Module::derive`（基类，非参数化模块直接报错）与 `clone` 的默认实现。 |
| [techlibs/common/synth.cc](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/techlibs/common/synth.cc) | 通用综合脚本，在 `begin` 阶段调用 `hierarchy`、在 `coarse` 阶段（可选）调用 `flatten`，说明本讲两个 pass 在真实流程里的位置。 |

---

## 4. 核心概念与源码讲解

### 4.1 hierarchy：解析、确定顶层与参数化派生

#### 4.1.1 概念说明

`read_verilog` 只负责「把文本变成 RTLIL 模块」，它**并不会**去检查模块之间的例化关系，也不会决定哪个是顶层。这些工作全部交给 `hierarchy`。可以这样理解 `hierarchy` 的职责：

- **确定顶层**：根据 `-top <module>`、模块上的 `top` 属性、或 `-auto-top` 自动推断，挑出唯一的 top。
- **展开例化**：对每个 cell，确认它 `type` 指向的模块确实存在；若该模块是参数化模板（`$abstract...`），则按 cell 携带的参数「派生」出具体模块并把 cell 的 `type` 改指向派生结果。
- **清理**：从 top 出发做可达性分析，删掉任何 top 树之外、用不到的模块。
- **收尾改写**：把位置参数（`$1`/`$2`…）改成命名参数、解析 SV 隐式端口连接、处理 `wand`/`wor` 线逻辑、规整端口宽度、给含 `$print`/形式化属性的模块打上 `keep` 属性。

> 关键直觉：`hierarchy` 把「一堆互相引用、还可能带参数的模块集合」整理成「一棵以 top 为根、所有例化都已落实、无冗余的干净设计」。后续的 `proc`/`opt`/`techmap` 才能在这棵树上放心工作。

#### 4.1.2 核心流程

`hierarchy` 的 `execute` 大致按下面顺序进行（对应源码主循环）：

```text
1. 解析参数（-top / -auto-top / -check / -libdir / -generate ...）
2. 确定 top_mod：
     - 若给了 -top：按名查找；若是 $abstract 模板则 derive 成具体模块
     - 若没给 -top 但有模块带 'top' 属性：用它
     - 若开了 -auto-top：用 find_top_mod_score 选「例化链最深」的模块
     - 若 top 仍是 $abstract：派生它
3. 主循环（while did_something，跑到不动点）：
     a. hierarchy_worker：从 top 出发 DFS，收集「被使用」的模块集合 used
     b. 对 used 中每个模块调用 expand_module：
          - 为每个 cell 找到（或派生 / 从 libdir 加载）其模块定义
          - 处理 SV interface 连接、检查端口、必要时调用 mod->derive(...)
     c. 若本轮改动了什么（did_something=true），回到 3
4. hierarchy_clean：删掉 used 之外的模块（默认保留 blackbox）
5. 在 top 上设 'top' 属性；给含 $print / 形式化属性的模块设 'keep'
6. 收尾：位置参数→命名参数、默认值、wand/wor、端口宽度规整
```

为什么需要「跑到不动点」的循环？因为派生一个参数化模块可能会引入新的、之前不存在的模块，进而引入新的 cell 需要再展开；处理 SV interface 也可能让顶层模块本身改变。所以只要这一轮「做了点什么」，就再跑一轮，直到稳定。

#### 4.1.3 源码精读

**命令注册与入口。** `hierarchy` 是一个普通 `Pass`，构造时注册命令名与一句话描述：

[hierarchy.cc:756-757](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/hierarchy/hierarchy.cc#L756-L757) —— 定义 `HierarchyPass`，命令名 `hierarchy`。

[hierarchy.cc:847-849](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/hierarchy/hierarchy.cc#L847-L849) —— `execute` 入口，先打一条 `Executing HIERARCHY pass` 的标题（回顾 u4-l4：`log_header` 会自动编号）。

**确定顶层（含参数派生）。** 当用户给了 `-top` 时，下面的代码块负责把名字解析成真正的 `top_mod`，必要时派生参数化顶层：

[hierarchy.cc:976-1008](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/hierarchy/hierarchy.cc#L976-L1008) —— 若 `-top` 指向一个 `$abstract` 模板或带了 `-chparam` 参数，就调用 `derive(...)` 生成具体模块；若派生出的模块名与用户指定的名字不一致，还会 `clone` 一份并改名回用户期望的名字。

**自动推断顶层。** `-auto-top` 用「例化深度评分」挑 top：

[hierarchy.cc:707-727](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/hierarchy/hierarchy.cc#L707-L727) —— `find_top_mod_score` 对每个模块递归计算一个分数：每多例化一层更深模块，分数 +1。分数最高的模块（即「处于例化链最顶端」的）被选作 top。设某模块为 \(m\)，其分数定义为

\[
\text{score}(m) = \max_{c \in \text{cells}(m),\; m_c \neq \varnothing}\bigl(\text{score}(m_c) + 1\bigr)
\]

叶子模块分数为 0。这正是「最长例化链长度」。

[hierarchy.cc:1057-1068](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/hierarchy/hierarchy.cc#L1057-L1068) —— `-auto-top` 主流程，遍历选中模块取分数最大者。

**主循环：收集使用集 + 展开每个模块。**

[hierarchy.cc:1107-1124](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/hierarchy/hierarchy.cc#L1107-L1124) —— `while (did_something)` 主循环；先用 `hierarchy_worker` 从 top 收集 `used_modules`，再对其中每个模块调用 `expand_module`，只要有一个模块报告「做了改动」就继续循环。

[hierarchy.cc:629-647](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/hierarchy/hierarchy.cc#L629-L647) —— `hierarchy_worker`：从 top 做深度优先遍历，对每个 cell 的 `type`（含 `$array:` 前缀先剥离）递归，把可达模块加入 `used` 集合并打印缩进的层次树（你在日志里看到的 `Top module:` / `Used module:` 就是这里输出的）。

**`expand_module`：为每个 cell 落实模块定义。** 这是 `hierarchy` 真正「展开例化」的核心：

[hierarchy.cc:505-518](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/hierarchy/hierarchy.cc#L505-L518) —— 若 `design->module(cell->type)` 找不到，就调 `get_module` 尝试派生或从 libdir 加载；若仍找不到，按「黑盒」跳过（开了 `-check` 系列才会报错）。

[hierarchy.cc:370-419](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/hierarchy/hierarchy.cc#L370-L419) —— `get_module`：先看是否存在 `$abstract<type>` 模板，若有就 `derive` 出具体模块并把 cell 的 `type` 改过去、清空 cell 参数；否则按 `.v`/`.sv`/`.il` 顺序在 libdir 里找文件并调用前端读入。

[hierarchy.cc:557-562](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/hierarchy/hierarchy.cc#L557-L562) —— 当 cell 带了被覆盖的参数（`cell->parameters` 非空）或需要接 interface 时，调用 `mod->derive(design, cell->parameters, ...)` 生成具体模块，把 cell 指向它并清空参数，同时置 `did_something = true`（因为派生引入了新模块，需要再走一轮）。

**派生的底层实现：`AstModule::derive`。** 上面的 `mod->derive(...)` 对普通模块会直接报错（普通模块不是参数化的），只有 `AstModule`（由 `read_verilog` 产生的、留存了原始 AST 的模块，见 u5-l3）才真正实现了它：

[rtlil.cc:1601-1606](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/rtlil.cc#L1601-L1606) —— 基类 `RTLIL::Module::derive`：非参数化模块被 derive 时直接 `log_error`。

[ast.cc:1752-1768](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/ast/ast.cc#L1752-L1768) —— `AstModule::derive`：调 `derive_common` 得到派生模块名；若该名字还不存在，就用改写好参数的新 AST 重新跑一遍 `process_module`（即 AST→RTLIL 那条流水线）生成具体模块；若已存在则直接命中缓存。

[ast.cc:1787-1796](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/frontends/ast/ast.cc#L1787-L1796) —— `derived_module_name`：派生模块名由「参数序列化」拼成。序列化串短于 60 字符时形如 `$paramod\name W=8'...`；过长则用 sha1 哈希压缩成 `$paramod$<sha1>\name`，既保证「同参数同名」便于缓存命中，又避免名字无限增长。

> 把上面几段串起来：`hierarchy` 看到 cell 带参数 → 调 `Module::derive` →（对 Verilog 模块）落到 `AstModule::derive` → 克隆 AST、改写 parameter 节点、算出 `$paramod...` 名字、重新生成 RTLIL → cell 的 `type` 改指向这个新模块。这就是「参数派生」的完整闭环。

**清理无用模块。**

[hierarchy.cc:649-683](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/hierarchy/hierarchy.cc#L649-L683) —— `hierarchy_clean`：复用 `hierarchy_worker` 得到 `used` 集合，把不在集合里的模块删掉；除非开了 `-purge_lib`，否则保留 blackbox 模块（库单元可能后续还会被 `abc`/`techmap` 用到）。调用点在主循环之后：

[hierarchy.cc:1150-1153](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/hierarchy/hierarchy.cc#L1150-L1153) —— 展平例化、跑到不动点后，执行清理。

**收尾改写（位置参数→命名参数）。** 主循环之后还有一大段对每个 cell 的改写，这里以「位置参数转命名参数」为例：

[hierarchy.cc:1202-1264](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/hierarchy/hierarchy.cc#L1202-L1264) —— 默认把形如 `$1`/`$2` 的位置端口连接，按目标模块 `port_id` 映射成真正的端口名；`-keep_positionals` 可关闭。位置参数转命名是后续 pass 能稳定工作的前提。

#### 4.1.4 代码实践

**实践目标**：直观看到 `hierarchy` 如何「确定 top + 删掉无用模块 + 派生参数化子模块」。

**操作步骤**：

1. 准备一个两层级化的设计（示例代码，读者自行保存为 `hier_demo.v`）：

   ```verilog
   // 示例代码：两层级化 + 一个参数化子模块
   module fa #(parameter W = 1)(
     input  [W-1:0] a, b,
     output [W-1:0] s,
     output         co
   );
     assign {co, s} = a + b;
   endmodule

   module add4(
     input  [3:0] a, b,
     output [3:0] s,
     output       co
   );
   wire c0; fa #(.W(4)) u0(.a(a), .b(b), .s(s), .co(co));
   endmodule

   module top(input [3:0] a, b, output [3:0] s, output co);
   add4 u_add(.a(a), .b(b), .s(s), .co(co));
   endmodule
   ```

   结构是 `top → add4 → fa`，其中 `fa` 被参数化为 `W=4`。

2. 进入 yosys 交互 shell（或写 `.ys` 脚本），先只读入、不跑 `hierarchy`，观察「原始状态」：

   ```
   yosys> read_verilog hier_demo.v
   yosys> ls
   yosys> stat
   ```

3. 执行 `hierarchy -top top`，再观察：

   ```
   yosys> hierarchy -top top
   yosys> ls
   yosys> stat
   ```

**需要观察的现象**：

- `read_verilog` 之后 `ls` 应能看到 `top`、`add4`、`fa` 三个模块；`fa` 此时还是带参数的「模板」（在内部以 `$abstract\fa` 形式存在）。
- `hierarchy -top top` 之后：
  - 日志会打印 `Top module: \top` 以及缩进的 `Used module:` 列表。
  - `ls` 中应出现一个派生模块，名字形如 `$paramod\fa\W=4'...`（参数序列化结果）；`fa` 的实例被改指向它。
  - 若你在设计里额外加一个「谁也不例化、也不被例化」的孤立模块，`hierarchy -top top` 之后它会被删掉，日志里出现 `Removing unused module ...`。

**预期结果**：参数化模块被替换为具体的 `$paramod` 派生模块；顶层树之外的多余模块被移除；位置参数被改写为命名连接。**派生模块名的确切字符串（特别是 sha1 与否）以本地 `ls` 输出为准——待本地验证。**

#### 4.1.5 小练习与答案

**练习 1**：如果故意不给 `-top`、也不给任何模块打 `top` 属性、也不开 `-auto-top`，`hierarchy` 会怎样处理 top？

**参考答案**：`top_mod` 保持为 `nullptr`，主循环里会把 design 中**所有**模块都当作 `used`（见 [hierarchy.cc:1116-1119](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/hierarchy/hierarchy.cc#L1116-L1119)），仍然展开例化与派生参数，但**不会**删除任何模块，也不设 `top` 属性。这意味着「找不到唯一 top」时 `hierarchy` 退化为「只展开、不裁剪」。

**练习 2**：为什么 `hierarchy` 的主循环是 `while (did_something)` 而不是「每个模块只处理一次」？

**参考答案**：因为 `expand_module` 会 `derive` 出原本不存在的新模块（参数派生）、会因 SV interface 改变顶层模块本身。这些改动可能让此前「找不到模块定义」的 cell 现在变得可解析，或让顶层需要重新派生。只有跑到「一轮里什么都没改变」（`did_something=false`）才能保证整棵树已经完全展开，这是典型的不动点迭代。

**练习 3**：`-check`、`-simcheck`、`-smtcheck` 三者的区别是什么？

**参考答案**：三者都要求「例化的模块必须存在」（否则 `get_module` 在 `check=true` 时 `log_error`，见 [hierarchy.cc:414-416](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/hierarchy/hierarchy.cc#L414-L416)）。`-simcheck` 更严：还不允许例化 blackbox、且要求必须有 top（[hierarchy.cc:1096-1097](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/hierarchy/hierarchy.cc#L1096-L1097) 与 [hierarchy.cc:529-534](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/hierarchy/hierarchy.cc#L529-L534)）。`-smtcheck` 在 `-simcheck` 基础上额外放过带 `smtlib2_module` 属性的 blackbox（用于形式验证后端）。

---

### 4.2 flatten：把层次展平成单个模块

#### 4.2.1 概念说明

`hierarchy` 把例化树整理清楚，但**保留了层次**——`top` 仍通过一个 `add4` 类型的 cell 去引用 `add4` 模块。很多后续优化（如跨模块边界的 `opt`、`techmap`）在层次边界上会受阻。`flatten` 的任务就是**把子模块的实现直接内联进父模块**：删掉那个引用 cell，把子模块里的 wire/cell/process/connection 全部搬过来并重新接线。

`flatten` 的帮助文本里有一句关键说明：它「与 `techmap` 非常相似，区别在于 `flatten` 用当前设计自身作为映射库」([flatten.cc:339-344](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/hierarchy/flatten.cc#L339-L344))。也就是说，可以把 `flatten` 理解成「以设计自身为模板库的 techmap」——这正好预告了下一讲 u6-l5 的 techmap 机制。

#### 4.2.2 核心流程

```text
1. 解析选项（-wb / -noscopeinfo / -scopename / -separator / -nocleanup）
2. 确定 used_modules：
     - 若整设计全选且存在 top：从 top 开始
     - 否则：所有模块都参与
3. 拓扑排序：按「被依赖→依赖者」建边，排出顺序，保证先展平底层模块
   （若存在环 → 报错：无法展平递归例化）
4. 按拓扑顺序对每个模块调用 flatten_module：
     - worklist 里取出每个引用了「本设计中某模块」的 cell
     - 跳过 blackbox / 带 keep_hierarchy 的
     - flatten_cell：把子模块内容搬进来、重接端口、删 cell、（可选）建 $scopeinfo
5. 若 cleanup 且有 top：删除展平后不再被使用的子模块
```

拓扑排序是这里的设计精髓。考虑 `top → add4 → fa`：必须先把 `fa` 内联进 `add4`、再把（已含 fa 内容的）`add4` 内联进 `top`。拓扑序保证「被例化者排在例化者之前」，因此一趟扫描就能展平整棵树。`flatten_cell` 还会把新搬进来的 cell 推回 worklist（[flatten.cc:324-329](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/hierarchy/flatten.cc#L324-L329) 的注释解释了：全选+有 top 时一趟就够，单独展平某模块时可能要多趟）。

#### 4.2.3 源码精读

**命令注册与拓扑排序。**

[flatten.cc:333-334](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/hierarchy/flatten.cc#L333-L334) —— 注册 `flatten` 命令。

[flatten.cc:411-441](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/hierarchy/flatten.cc#L411-L441) —— 确定 `used_modules`，用 `TopoSort` 建立「模板模块 → 例化它的模块」的边（`topo_modules.edge(tpl, module)`），然后 `sort()`。注释里的拓扑方向意味着「叶子模块先排、顶层后排」。

[flatten.cc:437-438](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/hierarchy/flatten.cc#L437-L438) —— 若拓扑排序失败（存在环），直接 `log_error("Cannot flatten a design containing recursive instantiations.")`：Yosys 不支持递归例化（Verilog 本身也只允许有限展开）。

[flatten.cc:440-448](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/hierarchy/flatten.cc#L440-L448) —— 按拓扑序逐个 `flatten_module`；若开了 cleanup 且有 top，删掉不再使用的子模块。

**逐模块展平。**

[flatten.cc:299-330](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/hierarchy/flatten.cc#L299-L330) —— `flatten_module`：维护一个 worklist，取出每个引用了「本设计中存在模块」的 cell；跳过 blackbox 与带 `keep_hierarchy` 的（见下），然后调 `flatten_cell`。

[flatten.cc:318-322](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/hierarchy/flatten.cc#L318-L322) —— **`keep_hierarchy` 守卫**：只要 cell 或被例化模块任一方带 `keep_hierarchy` 属性，就不展平它，并把该子模块记入 `used_modules`（这样 cleanup 阶段不会误删它）。这是设计者保留某个层次边界的标准手段。

**搬运内容与改名防冲突。** `flatten_cell` 的核心难点是「子模块和父模块里的 wire/cell 名字可能撞车」，解决办法是给搬过来的对象加「例化实例名 + 分隔符」前缀：

[flatten.cc:45-63](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/hierarchy/flatten.cc#L45-L63) —— `concat_name`/`map_name`：把对象名拼成 `\cellname.objectname`（公有名）或 `$flatten\cellname.objectname`（内部名），再用 `module->uniquify` 保证全局唯一。

[flatten.cc:65-72](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/hierarchy/flatten.cc#L65-L72) —— `map_sigspec`：搬过来的 cell/process 里所有 SigSpec 引用的 wire，都要按 `wire_map`（旧 wire → 新 wire）重定向。

[flatten.cc:127-201](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/hierarchy/flatten.cc#L127-L201) —— `flatten_cell` 的「搬运」部分：依次把模板模块的 memories、wires、processes、cells、connections 复制进父模块（建好 wire_map），并对新对象的 SigSpec 调 `rewrite_sigspecs(map_sigspec)` 重定向。

**重接端口与删 cell。** 子模块搬进来后，原来那个「引用 cell」的端口连接（父模块一侧的信号）要接到搬过来的、对应端口 wire 上：

[flatten.cc:203-268](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/hierarchy/flatten.cc#L203-L268) —— 遍历 cell 的每个端口连接，按端口方向（输出/输入/inout）决定 `assign` 的左右方向，做宽度对齐后 `module->connect(...)`，并 `sigmap.add` 记录连接关系以检测「驱动常量位」的错误。

[flatten.cc:270-296](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/hierarchy/flatten.cc#L270-L296) —— （可选）建一个 `$scopeinfo` cell 把被删 cell 与模板模块的属性（含 `src` 溯源）保留下来，随后 `module->remove(cell)` 删掉引用 cell。

> 直觉总结：`flatten_cell` = **复制子模块全部内容（改名防冲突）+ 把父模块端口信号接到对应端口 wire 上 + 删掉引用 cell**。完成后该 cell 在父模块里就「展开」成了它内部的一堆门。

#### 4.2.4 代码实践

**实践目标**：用一个两层级化设计，对比 `hierarchy` 之后与 `flatten` 之后的模块数与单元数。

**操作步骤**：沿用 4.1.4 的 `hier_demo.v`（`top → add4 → fa(W=4)`）。

```
yosys> read_verilog hier_demo.v
yosys> hierarchy -top top
yosys> stat                      # 记录此刻：模块数、各模块 cell 数
yosys> flatten
yosys> stat                      # 再记录一次
yosys> ls
```

**需要观察的现象**：

- `hierarchy -top top` 之后：`stat` 会列出 `top`、`add4`、以及 `$paramod...fa...` 三个模块，单元（`$add` 等算术单元）分散在各自模块里。
- `flatten` 之后：`stat` 基本只剩 `top` 一个模块（若开了默认 cleanup，`add4` 和派生的 `fa` 会被删掉，日志出现 `Deleting now unused module ...`）；原先分布在子模块里的 `$add` 等单元现在全部出现在 `top` 中，且名字带上了实例前缀（如 `\u_add.\u0....`）。

**预期结果**：模块数从「3」降为「1」，而 `top` 内的单元数相应增加（等于原本各子模块单元数之和，忽略被合并/清理掉的中间连线）。**具体单元类型与确切数量以本地 `stat` 输出为准——待本地验证。**

**进阶观察（keep_hierarchy）**：给 `add4` 模块加属性 `(* keep_hierarchy *) module add4(...)`，重新 `read_verilog` + `hierarchy -top top` + `flatten`，观察 `add4` 是否仍被保留（应保留，且 `flatten` 日志打印 `Keeping ... (found keep_hierarchy attribute).`）。

#### 4.2.5 小练习与答案

**练习 1**：`flatten` 为什么要做拓扑排序？如果直接按 `design->modules()` 的任意顺序展平会出什么问题？

**参考答案**：因为「先展平谁」有依赖——必须先把底层模块内联进它的直接父模块，父模块才能带着这些内容继续被内联进更上层。拓扑序保证「被例化者先于例化者」处理。若乱序，可能先处理了 `top`，把 `add4` 内联进来时 `add4` 本身还没展平 `fa`，导致 `top` 里残留对 `add4` 内部 `fa` 实例的引用，需要多轮补救（代码里有 worklist 回推机制兜底，但效率低、且单独展平某模块时才需要）。

**练习 2**：`flatten` 如何避免「父模块和子模块都有名为 `\x 的 wire」造成的名字冲突？

**参考答案**：通过 `concat_name`/`map_name` 给搬过来的对象加上「实例名 + 分隔符」前缀（如 `\u_add.\x`），再用 `module->uniquify` 确保在父模块内唯一（[flatten.cc:45-63](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/hierarchy/flatten.cc#L45-L63)）。同时建立 `wire_map`（旧→新），在搬运后用 `rewrite_sigspecs` 把所有 SigSpec 引用重定向到新 wire。

**练习 3**：`flatten` 与 `techmap` 都做「用模板替换 cell」，本质区别是什么？

**参考答案**：`techmap`（下一讲）用**指定的外部模板文件**（如 `techmap.v`）替换特定类型的 cell；`flatten` 用**当前设计自身**作为模板库，把「引用了本设计中另一个模块」的 cell 一律替换为那个模块的内容。可以说 `flatten` 是「模板库 = 自身、目标 = 所有例化 cell」的特化版 techmap（[flatten.cc:339-344](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/hierarchy/flatten.cc#L339-L344)）。

---

### 4.3 submod / uniquify：拆分与唯一化

`hierarchy` 和 `flatten` 都是「自顶向下整理例化关系」。还有两个正交方向的小工具值得一提：`submod` 是「反向 flatten」（把一组 cell 抽成新子模块），`uniquify` 是「给共享模块做独立副本」。

#### 4.3.1 概念说明

- **submod**：在一个**已经展平**（或本来就没有层次）的模块里，把若干 cell 打包提取成一个新的子模块，并在原模块里用一个例化 cell 替换它们。用途：在逆向分析或人工划分时「重建层次」。它通过 `submod` 属性标记要分组的 cell（值相同者进同一组），或用 `-name` 配合选择。
- **uniquify**：默认情况下，被多个模块例化的子模块在设计中只保留一份（共享）。这节省内存、保留模块性，但**阻碍了针对某个特定例化点的优化**（比如某个例化点的输入恒为 0，本可简化，但共享副本不能单独改）。`uniquify` 为每个例化点 `clone` 出一份独立模块，打上 `unique` 属性，让后续 pass 能各自优化。

#### 4.3.2 核心流程

**submod 流程**：

```text
1. 若用属性模式：先 opt_clean，再遍历所有「整模块被选中」的模块
2. 对每个模块，按 cell 的 'submod' 属性值分组（同值 → 同一子模块）
   （或用 -name：把当前选中的 cell 全归入一个名为 <name> 的子模块）
3. handle_submodule(每组)：
   a. 分析这组 cell 用到的信号，推断哪些信号要变成新模块的「端口」
      （内部驱动+外部使用 → 输出；外部驱动+内部使用 → 输入）
   b. 新建子模块，把 cell 搬过去，重定向信号到新 wire
   c. 在原模块里放一个例化 cell，把端口接到原信号
4. opt_clean 收尾
```

**uniquify 流程**：

```text
1. 遍历「带 unique 属性 或 top」的模块（只有这些模块的例化才需要唯一化）
2. 对其每个例化 cell：clone 目标模块，改名为 "<父模块>.<cell名>"，
   把 cell->type 指向新模块，给新模块打 unique 属性
3. 循环到不动点（因为新模块也带了 unique，其内部的例化也要继续唯一化）
```

#### 4.3.3 源码精读

**submod 命令注册与分组。**

[submod.cc:321-338](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/hierarchy/submod.cc#L321-L338) —— 注册 `submod` 命令；帮助文本说明它「把带 `submod` 属性的 cell 移到新子模块」，可用于在平坦设计里重建层次。

[submod.cc:275-300](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/hierarchy/submod.cc#L275-L300) —— 属性模式下：遍历 cell，按 `submod` 属性值分桶；新子模块全名为 `<原模块名>_<属性值>`（重名则追加 `_`）。

**端口方向推断（submod 的核心难点）。** 要把一组 cell 抽成子模块，必须推断「哪些信号要变成端口、是输入还是输出」。`SubmodWorker` 用一套标志位（`is_int_driven`/`is_int_used`/`is_ext_driven`/`is_ext_used`）来表达「内部驱动/内部使用/外部驱动/外部使用」：

[submod.cc:146-155](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/hierarchy/submod.cc#L146-L155) —— 推断规则：内部驱动且外部使用 → 输出端口；外部驱动且内部使用 → 输入端口；既内部驱动又外部驱动 → inout。这正是「按数据流方向确定端口」的朴素逻辑。

[submod.cc:88-204](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/hierarchy/submod.cc#L88-L204) —— `handle_submodule`：先 `flag_signal` 标注每个信号的方向标志，据此为新模块创建带方向的端口 wire，把 cell 搬进新模块并把 SigSpec 重定向到新 wire，最后在原模块里放一个例化 cell 接好端口（[submod.cc:223-243](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/hierarchy/submod.cc#L223-L243)）。

> 注意 `submod` 的前置条件：模块必须**已无 process 和 memory**（[submod.cc:252-260](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/hierarchy/submod.cc#L252-L260)），即需要先跑过 `proc` 和 `memory`，否则直接跳过该模块。这是因为它要逐 cell 分析端口方向，而 process/memory 不是 cell 形式。

**uniquify 命令注册与克隆。**

[uniquify.cc:25-26](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/hierarchy/uniquify.cc#L25-L26) —— 注册 `uniquify` 命令。

[uniquify.cc:62-99](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/hierarchy/uniquify.cc#L62-L99) —— 主循环：只处理「带 `unique` 属性 或 是 top」的模块（[uniquify.cc:68](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/hierarchy/uniquify.cc#L68)）；对其每个例化 cell，`clone` 出新模块并改名、改 cell->type、打 `unique` 属性。

[uniquify.cc:87-93](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/hierarchy/uniquify.cc#L87-L93) —— 关键四步：`tmod->clone()` → 改名为 `<父模块>.<cell名>` → `cell->type = newname` → `smod->set_bool_attribute(ID::unique)`。底层的 `clone` 实现见 [rtlil.cc:2789-2795](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/rtlil.cc#L2789-L2795)，它深拷贝模块的全部内容。

#### 4.3.4 代码实践

**实践目标**：体验「先展平、再用 submod 重建层次」的往返，以及 uniquify 的多副本生成。

**操作步骤**（源码阅读型 + 可选运行）：

1. 在 4.2.4 的 `flatten` 之后，设计只剩一个平坦的 `top`。给其中的两个 cell 打上相同的 `submod` 属性，再用 `submod` 抽出来：

   ```
   yosys> select top u:/u_add/u0 %c                # 选某条 cell 链（示例）
   yosys> setattr -set submod alu_group            # 给选中 cell 打 submod 属性
   yosys> submod
   yosys> ls
   ```

   （`select`/`setattr` 的精确写法请用 `help select` / `help setattr` 查阅；本步主要看 `submod` 是否生成名为 `top_alu_group` 的新模块。）

2. 观察 `uniquify`：先把 `top` 设为 unique（或依赖其 `top` 属性隐式 unique），对一个被例化两次的子模块运行 `uniquify`：

   ```
   yosys> read_verilog <一个子模块被例化两次的设计>
   yosys> hierarchy -top top
   yosys> setattr -set unique -mod top
   yosys> uniquify
   yosys> ls
   ```

**需要观察的现象**：

- `submod` 后：原模块里对应 cell 被替换成一个例化 cell，同时多出一个 `<原模块名>_<组名>` 的新模块；新模块的端口由 `handle_submodule` 推断出的输入/输出构成。
- `uniquify` 后：每个原本共享的子模块，对应每个例化点都出现了一个独立副本，名字形如 `\top.u1`、`\top.u2`，且都带 `unique` 属性。

**预期结果**：`submod` 成功重建一层层次；`uniquify` 把共享模块复制成「每例化点一份」。**具体模块名与端口以本地 `ls` 输出为准——待本地验证。**

#### 4.3.5 小练习与答案

**练习 1**：`submod` 为什么要求模块里没有 process 和 memory？

**参考答案**：`submod` 靠逐 cell 分析端口方向（输入/输出）来决定新子模块的端口。process（来自 always）和 memory 不是 cell 形式，无法用 `CellTypes` 查端口方向去标注信号，所以 `SubmodWorker` 构造时直接跳过含 process/memory 的模块（[submod.cc:252-260](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/hierarchy/submod.cc#L252-L260)）。要先跑 `proc` 和 `memory` 把它们变成 cell 才能用 `submod`。

**练习 2**：`uniquify` 只对什么样的模块生效？为什么有这个限制？

**参考答案**：只对「带 `unique` 属性、或是 top」的模块生效（[uniquify.cc:68](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/hierarchy/uniquify.cc#L68)）。因为默认保留模块性、共享子模块是 Yosys 的优化默认值；只有当设计者明确（用 `unique` 属性）或隐式（top 模块）表示「我需要为每个例化点单独优化」时，才值得付出克隆的内存代价。这也让 `uniquify` 可以递归——新克隆的模块也带 `unique`，其内部例化会在下一轮被处理。

**练习 3**：`uniquify` 之后，对某个例化点做 `opt` 优化会不会影响其他例化点？

**参考答案**：不会。这正是 `uniquify` 的目的——每个例化点现在有独立的模块副本（带 `unique` 属性），针对其中一个副本的优化（比如某输入恒 0 而折叠掉一部分逻辑）只改这个副本，不会波及其他例化点。若没有 `uniquify`，所有例化点共享同一份模块，就无法做这种「例化点专属」的优化。

---

## 5. 综合实践

**任务**：用一个三模块的参数化设计，串起本讲的 `hierarchy` / `flatten` / `uniquify`，并用 `stat` 在每一步量化变化。

**准备**（示例代码，保存为 `lab.v`）：

```verilog
module leaf #(parameter W=4)(input [W-1:0] a, b, output [W-1:0] y);
  assign y = a & b;
endmodule

module mid(input [3:0] a, b, output [3:0] y);
  wire [3:0] y0, y1;
  leaf #(.W(4)) u0(.a(a), .b(b), .y(y0));   // leaf 被例化两次
  leaf #(.W(4)) u1(.a(y0), .b(a), .y(y1));
  assign y = y1;
endmodule

module top(input [3:0] a, b, output [3:0] y);
  mid u_mid(.a(a), .b(b), .y(y));
  mid u_mid2(.a(b), .b(a), .y());            // mid 也被例化两次（第二个输出悬空，仅演示）
endmodule
```

**步骤**：

1. `read_verilog lab.v` → `hierarchy -top top` → `stat`。记录：模块数（应含 top、mid、两个 mid 中引用的 leaf 派生模块——注意 leaf 两次例化参数相同，派生命中同一缓存）、`Used module` 列表。
2. `setattr -set unique -mod top` → `uniquify` → `stat`。观察：`mid` 因为被例化两次而被复制成两份独立模块，名字带 `top.u_mid` / `top.u_mid2`。
3. `flatten` → `stat`。观察：模块数降为 1（仅 `top`），所有 `leaf`/`mid` 的 `$and` 单元都内联进 `top`，且带实例前缀。
4. 重新开始：`read_verilog lab.v` → `hierarchy -top top` → 给 `mid` 加 `(* keep_hierarchy *)` → `flatten` → `stat`。观察 `mid` 是否被保留。
5. 在步骤 3 的平坦结果上，挑一条 cell 链 `setattr -set submod g` → `submod` → `ls`，确认「重建」出了一层 `top_g` 子模块。

**预期结果**：你能用 `stat` 的「Number of modules」与各模块 cell 数，定量画出 `hierarchy → uniquify → flatten` 三步对设计结构的影响曲线。**所有确切数字以本地 `stat` 输出为准——待本地验证。**

## 6. 本讲小结

- `hierarchy` 是 `synth` 的第一个子 pass，职责是「确定 top + 展开例化（含参数派生）+ 清理无用模块 + 收尾改写」；它用 `while (did_something)` 跑到不动点，因为派生参数/处理 interface 会引入新模块、需要再来一轮。
- 参数派生的闭环是：cell 带参数 → `Module::derive` →（Verilog 模块）`AstModule::derive` → 克隆 AST、改写 parameter、生成 `$paramod...` 名字（过长用 sha1）→ 重新生成 RTLIL。
- `find_top_mod_score` 用「最长例化链」评分在 `-auto-top` 时自动选 top；`hierarchy_worker` 做 DFS 得到「被使用」集合，`hierarchy_clean` 据此删除冗余模块（默认保留 blackbox）。
- `flatten` 把子模块内联进父模块：拓扑排序决定「先展平谁」（递归例化会报错），`flatten_cell` 复制内容（加实例名前缀防冲突）+ 重接端口 + 删引用 cell；`keep_hierarchy` 属性可阻止展平。它本质是「以设计自身为模板库的 techmap」。
- `submod` 是「反向 flatten」，按 `submod` 属性或选择把一组 cell 抽成新子模块，靠「内部/外部驱动-使用」标志推断端口方向；要求模块已无 process/memory。
- `uniquify` 给被多次例化的模块做「每例化点一份」的克隆（打 `unique` 属性），为针对单个例化点的优化扫清障碍；只对带 `unique` 或 top 的模块生效。

## 7. 下一步学习建议

本讲之后，你已经理解了「设计在进入核心综合前如何被整理成一棵干净的、可选展平的模块树」。建议接下来：

- **u6-l2 proc**：`hierarchy` 之后 `synth` 紧接着调用 `proc`，把 always 块（`RTLIL::Process`）翻译成 `$mux`/`$dff`。这是行为级到门级的第一步。
- **u6-l3 opt**：理解 `flatten` 之后为什么能做「跨模块边界」的优化——`opt` 系列正是在展平后的网表上做常量传播与合并。
- **u6-l5 techmap**：本讲多次提到 `flatten` 类似 `techmap`。学完 techmap 你会彻底理解「用模板模块替换 cell」这一通用机制。
- **延伸阅读**：对照 [passes/hierarchy/keep_hierarchy.cc](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/hierarchy/keep_hierarchy.cc)，了解「按规模自动决定是否保留层次」的策略；阅读 `tests/various/hierarchy_*.ys` 系列脚本，看 `hierarchy -generate`、参数派生、接口展开的真实测试用例。

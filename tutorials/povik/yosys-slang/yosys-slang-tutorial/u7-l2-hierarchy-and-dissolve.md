# 层次处理：展平、黑盒与 hierarchy 模式

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清楚 sv-elab 在翻译时如何决定一个子模块实例是「就地展平（dissolve）」还是「保留为独立的 RTLIL 模块」。
- 掌握 `should_dissolve`、`is_blackbox`、`hierarchy_mode()` 三者的判定顺序与各自的判定规则。
- 理解 `HierarchyQueue` 如何以「按需生成 + 队列动态生长」的方式延迟创建子模块的 `NetlistContext`。
- 理解 `realm`（实例体边界）与 `find_symbol_realm` 的概念，以及为什么跨保留边界的层次引用会被拒绝。
- 会用 `--keep-hierarchy`、`--best-effort-hierarchy` 两种开关分别跑同一个设计，并解释生成模块数量与边界的差异。

本讲是单元 7（高级主题）的第二篇，承接 u2-l2 讲过的「slang 顶层实例 → `NetlistContext` → `PopulateNetlist` 翻译」主线，把视角下沉到「一个设计里有多个模块时，sv-elab 怎么画模块边界」。黑盒的导入/导出细节（`import_blackboxes_from_rtlil` 等）留给 u7-l3，本讲只关心「边界判定」这一层。

## 2. 前置知识

在进入源码前，先建立三条直觉。

**直觉一：slang 给的是一棵实例树，sv-elab 要把它变成若干 RTLIL 模块。**
slang 精化后，顶层模块和它实例化的子模块构成一棵「实例树」，每个实例都有一个 `InstanceBodySymbol`（实例体，描述这个实例「体内」长什么样）。sv-elab 的产物是 Yosys 的 `RTLIL::Module` 集合。一个实例体既可以被「展平」——它体内的逻辑直接并入父模块；也可以被「保留」——它对应一个独立的 `RTLIL::Module`，父模块里只放一个 cell（单元）实例指向它。是否展平，由本讲的几个函数决定。

**直觉二：「realm」= 一个 `NetlistContext` 翻译的实例体边界。**
sv-elab 里每个 `NetlistContext` 对象（见 u3-l1）对应一个正在构建的 RTLIL 模块，它的 `realm` 成员指向该模块所属的那个 `InstanceBodySymbol`。当某个子实例被「保留」时，它就成为一个新的 realm 边界，会新建一个 `NetlistContext`。当它被「展平」时，它体内所有符号的 realm 仍是父模块的 realm。理解了 realm，就理解了「模块边界从哪来」。

**直觉三：黑盒是一种特殊的「保留」。**
黑盒（blackbox）是没有可综合内部实现的模块，sv-elab 不展平它，而是发一个 cell + 把它的定义导出成空 RTLIL 模块。无论层次模式怎么设，黑盒的处理方式都一样——所以黑盒判定在展平判定之前。

> 术语对照：slang 的 `InstanceSymbol`（实例）、`InstanceBodySymbol`（实例体）、`DefinitionSymbol`（模块定义）；Yosys 的 `RTLIL::Module`（模块）、`RTLIL::Cell`（单元实例）、`RTLIL::Wire`（线网）。本讲频繁在「slang 实例体」与「RTLIL 模块」之间做映射。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [src/slang_frontend.h](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.h) | `SynthesisSettings`（含 `HierMode` 枚举与 `hierarchy_mode()`）、`NetlistContext`（含 `realm` 与各判定方法）的声明 |
| [src/slang_frontend.cc](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc) | 全部实现：`HierarchyQueue`、`is_blackbox`、`should_dissolve`、`find_symbol_realm`、`PopulateNetlist::handle(InstanceSymbol)`、`execute()` 主循环 |
| [tests/various/intf_w_hierarchy.ys](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/tests/various/intf_w_hierarchy.ys) | 用 `--keep-hierarchy` 跑含接口（interface）层次设计的等价性测试，是本讲实践的参考 |

本讲引用的关键源码点集中在 `src/slang_frontend.cc` 的两个区域：判定函数群（3196–3384 行附近）与翻译驱动（1696–1718 行的 `HierarchyQueue`、2210–2450 行的实例处理、3756–3795 行的主循环）。

## 4. 核心概念与源码讲解

### 4.1 hierarchy_mode()：三种层次模式的换算

#### 4.1.1 概念说明

sv-elab 把「层次处理策略」抽象成三种模式，用一个枚举 `HierMode` 表示：

- **NONE（默认）**：尽可能展平，最终只保留顶层模块（以及黑盒、带 inout 端口无法展平的模块）。
- **BEST_EFFORT**：尽力保留层次，但对于「会带来麻烦」的端口（例如没有 modport 的接口端口）仍然展平。
- **ALL**：对应 `--keep-hierarchy`，保留所有能保留的模块边界（注释里标注为 experimental，可能崩溃）。

用户不直接给枚举，而是给两个布尔开关 `--keep-hierarchy` 与 `--best-effort-hierarchy`，由派生方法 `hierarchy_mode()` 换算成枚举。把「用户输入」与「下游实际使用的值」分开，是 sv-elab 选项系统的常见手法（见 u2-l3）。

#### 4.1.2 核心流程

换算规则很简单，`--keep-hierarchy` 优先级最高：

```
if keep_hierarchy 为真       → ALL
else if best_effort 为真     → BEST_EFFORT
else                         → NONE
```

#### 4.1.3 源码精读

枚举与换算方法都写在 `SynthesisSettings` 内部：

[src/slang_frontend.h:514-527](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.h#L514-L527) —— 定义 `HierMode{NONE, BEST_EFFORT, ALL}` 三态枚举，并提供 `hierarchy_mode()` 把两个 `optional<bool>` 字段换算成枚举。`keep_hierarchy` 优先于 `best_effort_hierarchy`。

这两个开关用 slang 的命令行框架注册，`help read_slang` 的输出就来自这里：

[src/slang_frontend.cc:92-95](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L92-L95) —— `--keep-hierarchy`（实验性，可能崩溃）与 `--best-effort-hierarchy`（尽力保留）两个开关的注册。注意帮助文案里 `--keep-hierarchy` 被明确标为 experimental。

#### 4.1.4 代码实践

**目标**：确认两种开关的优先级与默认模式。

**步骤**：
1. 在交互式 Yosys 里执行 `help read_slang`，找到 `--keep-hierarchy` 与 `--best-effort-hierarchy` 两行，阅读其帮助文案。
2. 构造一个「同时传两个开关」的命令：`read_slang --keep-hierarchy --best-effort-hierarchy top.sv --top top`。
3. 阅读上面的换算逻辑，预测实际生效的是哪种模式。

**需要观察的现象**：帮助文案里 `--keep-hierarchy` 是否带有 "experimental; may crash" 字样。

**预期结果**：根据 [src/slang_frontend.h:520-527](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.h#L520-L527) 的优先级，同时传两个开关时 `ALL` 胜出，`--best-effort-hierarchy` 被忽略。不传任何开关时为 `NONE`（默认全展平）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `hierarchy_mode()` 不直接把 `HierMode` 作为命令行参数，而要用两个布尔开关再换算？
**答案**：命令行体验上「保留层次」是两个独立的 Yes/No 意图（全部保留 vs 尽力保留），用两个布尔开关更自然；而下游代码（`should_dissolve`）需要的是「三选一」的决策，所以用派生方法把两个布尔折叠成枚举，把「用户表达」与「内部决策」解耦。

**练习 2**：如果不传任何层次开关，`hierarchy_mode()` 返回什么？这对一个含 3 个普通子模块的设计意味着什么？
**答案**：返回 `NONE`。意味着这 3 个普通子模块（只要不是黑盒、没有 inout 端口）都会被展平进顶层，最终网表里通常只剩顶层一个模块（外加任何黑盒）。

---

### 4.2 is_blackbox：黑盒判定

#### 4.2.1 概念说明

黑盒是「只有端口、没有可综合内部实现」的模块。sv-elab 遇到黑盒实例时不展平它，而是：(1) 在父模块里发一个指向黑盒类型的 cell；(2) 把黑盒定义导出成一个「空但带端口」的 RTLIL 模块（这部分导出逻辑在 u7-l3 讲）。

判定一个模块定义是不是黑盒，由 `NetlistContext::is_blackbox` 完成。它有四条独立的判据，任意一条命中即判定为黑盒。函数还有一个可选的出参 `why_blackbox`：当传入一个 `slang::Diagnostic *` 时，函数会把「为什么判定为黑盒」的备注（note）追加进去，用于在诊断里给用户解释原因。

#### 4.2.2 核心流程

四条判据按顺序短路求值：

```
1. 定义带 cellDefine 标记            → 黑盒
2. 模块名在 blackboxed_modules 集合  → 黑盒   （来自 --blackboxed-module）
3. 带 (* blackbox *) 属性且非假      → 黑盒
4. 若 --empty-blackboxes 开启        → 再检查是否为「空声明模块」→ 黑盒
否则                                  → 非黑盒
```

#### 4.2.3 源码精读

[src/slang_frontend.cc:3196-3224](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L3196-L3224) —— `is_blackbox` 全文。逐条对应：

- 第 3198–3199 行：`sym.cellDefine` 为真（对应源码里的 `(* celldefine *)` 属性）直接返回 true。
- 第 3201–3202 行：模块名命中 `settings.blackboxed_modules`（由命令行 `--blackboxed-module <name>` 逐个累积，见 [src/slang_frontend.cc:119-125](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L119-L125)）。
- 第 3204–3213 行：遍历模块上的属性，找到名为 `blackbox` 且值非假的属性；同时通过 `why_blackbox->addNote(...)` 追加 `NoteModuleBlackboxBecauseAttribute` 备注。
- 第 3215–3221 行：仅当 `--empty-blackboxes` 开启时，调用 `is_decl_empty_module(*sym.getSyntax())` 判断「声明体是否为空模块」，并追加 `NoteModuleBlackboxBecauseEmpty` 备注。

`why_blackbox` 出参的设计让「判定」与「解释」复用同一段逻辑：调用方想知道原因时传诊断指针，不想知道时传 `nullptr`（默认值）。

#### 4.2.4 代码实践

**目标**：用三种不同方式把同一个模块标记成黑盒，观察它们是否都命中 `is_blackbox`。

**步骤**：
1. 准备一个最小设计 `leaf.v`：定义一个 `leaf` 模块（带一个输入 `a`、一个输出 `y`），但不写实现，并在顶层 `top` 里实例化它。
2. 分别用三种方式让 `leaf` 成为黑盒：
   - 方式 A：在 `leaf` 模块声明前加 `(* blackbox *)`。
   - 方式 B：`read_slang leaf.v --top top --blackboxed-module leaf`。
   - 方式 C：让 `leaf` 模块体为空，并加 `--empty-blackboxes`。
3. 每种方式跑完后用 Yosys 的 `ls` 或 `show` 查看生成的模块列表。

**需要观察的现象**：三种方式下，`leaf` 是否都生成为一个独立的、体内无逻辑、仅声明端口的 RTLIL 模块；顶层 `top` 里是否都有一个指向 `leaf` 类型的 cell。

**预期结果**：三种方式都应让 `leaf` 成为黑盒。方式 A 命中第 3 条判据（属性），方式 B 命中第 2 条（名字集合），方式 C 命中第 4 条（空模块）。具体运行输出**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `--empty-blackboxes` 默认关闭，而前两条判据（`cellDefine`、`blackbox` 属性）总是生效？
**答案**：`cellDefine` 与 `(* blackbox *)` 是用户「显式声明」的黑盒意图，可信度高；而「模块体为空」可能只是因为模块还没写完或被工具链预处理过，把它默认当黑盒会改变网表语义，所以需要 `--empty-blackboxes` 显式开启才生效。

**练习 2**：`is_blackbox` 的 `why_blackbox` 出参有什么用？
**答案**：它让上层诊断能复用判定逻辑来「解释原因」。例如 `should_dissolve` 发现某黑盒不能展平时，可以把同一个诊断对象传进去，自动追加「因为它是黑盒、而黑盒是因为 X 属性」这样的备注链，避免在两处重复写解释逻辑。

---

### 4.3 should_dissolve：展平判定

#### 4.3.1 概念说明

`should_dissolve` 回答「这个实例要不要展平进父模块」。它是层次处理的核心决策点，被三处调用：`PopulateNetlist::handle(InstanceSymbol)`（决定实例怎么翻译）、变量声明的遍历（决定要不要下钻实例体收集线网）、以及 `InferredMemoryDetector`（决定存储器候选要不要下钻）。

它的判定**有严格的优先级顺序**：黑盒与接口的判定优先于层次模式，inout 端口的判定也优先于层次模式。也就是说，有些实例无论用户选哪种 `hierarchy_mode` 都会得到相同的处理。和 `is_blackbox` 一样，它也带一个 `why_not_dissolved` 出参用于解释。

#### 4.3.2 核心流程

```
1. 是模块且是黑盒          → 不展平（return false）   黑盒永远保留
2. 是接口（interface）      → 展平（return true）      接口永远展平
3. 是模块且有 inout 端口    → 不展平（return false）   inout 无法跨边界
4. 否则按 hierarchy_mode():
     NONE       → 展平（true）
     BEST_EFFORT→ 看端口：有「麻烦端口」则展平，干净模块则保留
     ALL        → 不展平（false）
```

BEST_EFFORT 模式下「麻烦端口」的判定见下方源码：任何非 `Port`/`MultiPort`/`InterfacePort` 的端口种类、或没有 modport 的接口端口，都会触发展平。

#### 4.3.3 源码精读

[src/slang_frontend.cc:3226-3302](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L3226-L3302) —— `should_dissolve` 全文。分段说明：

- 第 3228–3237 行：黑盒子句。先 `is_blackbox` 判定，命中则通过 `why_not_dissolved` 追加 `NoteModuleNotDissolvedBecauseBlackbox`，并再调一次 `is_blackbox(..., why_not_dissolved)` 把黑盒原因链补全。
- 第 3239–3241 行：接口恒展平（接口本身不构成可综合模块边界，它的信号会被提升到连接处）。
- 第 3243–3265 行：扫描端口连接，遇到任何方向为 `InOut` 的 `Port` 或 `MultiPort` 就不能展平（inout 端口跨边界展开会破坏驱动语义），追加 `NoteModuleNotDissolvedBecauseInOut`。
- 第 3268–3301 行：剩余情况交给 `settings.hierarchy_mode()`：
  - `NONE`（第 3270–3271 行）：恒展平。
  - `BEST_EFFORT`（第 3272–3292 行）：遍历端口连接，遇到 `InterfacePort` 且无 modport（`!conn->getIfaceConn().second`）则展平，遇到 `default`（非 Port/MultiPort/InterfacePort 的异常种类）也展平；若全部端口「干净」且是模块，则保留（return false）。
  - `ALL`（第 3293–3298 行）：恒不展平，追加 `NoteModuleNotDissolvedBecauseKeepHierarchy`。

可以看到，BEST_EFFORT 是 NONE 与 ALL 之间的折中：它愿意保留层次，但只对「端口干净、能干净地连成 cell 端口」的模块保留，其余仍展平，避免在保留边界上产生无法处理的连接。

#### 4.3.4 代码实践

**目标**：直观体会「同一设计在三种模式下展平与否的差异」。

**步骤**：
1. 准备 `hier.sv`：
   ```systemverilog
   module sub(input logic a, output logic y);
       assign y = ~a;
   endmodule
   module top(input logic a, output logic y);
       logic w;
       sub u(.a(a), .y(w));
       assign y = w & a;
   endmodule
   ```
2. 分别用三种命令读取（注意 `--keep-hierarchy` 标注为实验性）：
   - `read_slang hier.sv --top top`（NONE，默认）
   - `read_slang --best-effort-hierarchy hier.sv --top top`
   - `read_slang --keep-hierarchy hier.sv --top top`
3. 每次读取后执行 `ls` 查看模块数量，再 `select top; show` 查看顶层内部结构。

**需要观察的现象**：
- NONE 模式下，`sub` 被展平，顶层里直接出现 `~a` 的非门逻辑，`ls` 只列出一个模块 `top`。
- BEST_EFFORT / ALL 模式下，`sub` 被保留为独立模块，顶层里出现一个类型为 `sub`（或带层次后缀）的 cell，`ls` 列出两个模块。

**预期结果**：NONE 得到 1 个模块、顶层内含展开的与非逻辑；ALL/BEST_EFFORT 得到 2 个模块、顶层含一个 cell。具体模块命名（是否带 `$层次路径` 后缀）取决于实例缓存设置，见 4.4。运行输出**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：一个模块声明为 `module io_mod(inout wire b);`，在 `--keep-hierarchy`（ALL）模式下实例化它会怎样？
**答案**：仍然不展平会被 inout 卡住——但 ALL 模式返回 false 表示「想保留」。实际上 inout 端口的判定（第 3243–3265 行）在层次模式判定**之前**就返回 false 了，所以无论哪种模式它都不展平。换言之，inout 端口的实例永远被保留为独立模块（与黑盒一样，是「无条件保留」）。

**练习 2**：BEST_EFFORT 模式下，一个带「无 modport 的接口端口」的模块会怎样？
**答案**：第 3278–3281 行判定它为「麻烦端口」，返回 true（展平）。这是 BEST_EFFORT 与 ALL 的关键区别：ALL 会强行保留（哪怕接口端口没 modport，后续可能报错），BEST_EFFORT 则宁可展平以避免在边界上产生无法处理的连接。

---

### 4.4 HierarchyQueue：按需生成模块的队列

#### 4.4.1 概念说明

知道了「某个实例要不要保留」之后，下一个问题是：保留的子模块什么时候、以什么顺序被翻译？

sv-elab 用一个**按需生成（demand-driven）**的队列 `HierarchyQueue` 来管理。它维护一张「实例体 → `NetlistContext`」的映射和一个待处理队列。当 `PopulateNetlist` 在翻译某个模块时遇到一个「要保留」的子实例，就调用 `get_or_emplace`：如果这个子实例体已经建过 `NetlistContext` 就复用，否则**当场新建**一个并塞进队列尾部。主循环用一个「上界随队列增长」的 for 循环逐一消费队列——这样无需预先递归扫描整棵实例树，边翻译边发现新的子模块。

这正是 u2-l2 提到的「`HierarchyQueue` 以队列实时长度为上界，支持层次展平时动态扩张」的具体含义。

#### 4.4.2 核心流程

```
execute() 主循环:
  建 HierarchyQueue
  for 每个顶层实例: get_or_emplace 建顶层 NetlistContext（标 top 属性）
  for i = 0; i < queue.size(); i++:        ← 上界每次重新求值
      netlist = queue[i]
      PopulateNetlist 在 netlist.realm 上 visit
        └─ 遇到保留子实例 → get_or_emplace → 可能 push_back 到 queue
```

关键点：循环条件 `i < queue.size()` 的右端在每次迭代都被重新求值，所以 `PopulateNetlist` 在处理过程中往队列尾部追加的新模块，会被后续迭代自然消费到——这就是「动态扩张」。

#### 4.4.3 源码精读

[src/slang_frontend.cc:1696-1718](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L1696-L1718) —— `HierarchyQueue` 结构体。两个核心成员：`netlists`（`map<InstanceBodySymbol*, NetlistContext*>`，查重用）、`queue`（`vector<NetlistContext*>`，处理顺序用）。`get_or_emplace`（1698–1708 行）返回 `pair<NetlistContext&, bool>`——第二项 `bool` 表示「是否新建」（已存在则 false）。析构函数（1710–1714 行）负责 `delete` 所有 `NetlistContext`，因为它们都是 `new` 出来得。

新建子模块 `NetlistContext` 的现场在 `PopulateNetlist::handle(InstanceSymbol)` 的「不展平」分支：

[src/slang_frontend.cc:2380-2387](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L2380-L2387) —— 当 `should_dissolve` 返回 false 时，`get_instance_body` 取实例体，`queue.get_or_emplace(ref_body, netlist, *ref_body->parentInstance)` 按需建子模块 `NetlistContext`（注意它复用父 `netlist` 的 design/settings/compilation，仅以新实例构造），并在父画布上 `addCell` 指向子模块类型。

子模块的 RTLIL 模块名由 `module_type_id` 决定：

[src/slang_frontend.cc:210-218](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L210-L218) —— 若实例的层次路径等于模块名（即顶层或无歧义），模块名就是转义后的模块名；否则命名为 `模块名$层次路径`。这意味着在保留层次时，**每个实例通常对应一个特化的 RTLIL 模块**。

为什么是「通常每个实例一个模块」？因为 sv-elab 默认禁用了 slang 的实例缓存。`get_instance_body` 据此决定用哪个实例体作为 key：

[src/slang_frontend.cc:501-507](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L501-L507) —— 若 `disable_instance_caching` 为假且有 canonical body，则用 canonical body（参数相同的实例共享一个 body）；否则用 `instance.body`（每个实例各自的 body）。而 `fixup_options` 默认把 `DisableInstanceCaching` 设为 true：

[src/slang_frontend.cc:3506-3512](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L3506-L3512) —— 默认 `disable_inst_caching = true`，于是 `get_instance_body` 走 `instance.body` 分支，每个实例体都不同，保留层次时每个实例得到自己的 RTLIL 模块（命名带层次路径后缀）。

主消费循环：

[src/slang_frontend.cc:3756-3795](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L3756-L3795) —— 先为每个顶层实例建 `NetlistContext` 并标 `top` 属性（3756–3770 行）；再用 `for (int i = 0; i < (int) hqueue.queue.size(); i++)` 消费队列（3772 行），每次对一个 `NetlistContext` 跑 `PopulateNetlist`，处理中通过 `get_or_emplace` 追加的新模块会让 `queue.size()` 增长，从而被后续迭代消费。

`NetlistContext` 构造时建画布（RTLIL 模块）、析构时 `fixup_ports` + `check` 收尾：

[src/slang_frontend.cc:3420-3436](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L3420-L3436) —— 两个构造函数：顶层用 `(design, settings, compilation, instance)`，子模块用 `(other, instance)` 转调前者。构造时 `canvas = design->addModule(module_type_id(instance.body))` 建出对应的 RTLIL 模块，`realm` 设为 `instance.body`。

#### 4.4.4 代码实践

**目标**：验证「按需生成 + 队列动态生长」——保留层次时，子模块是边翻译边发现的，而非预先递归扫描。

**步骤**：
1. 准备一个三层设计 `chain.sv`：`top → mid → leaf`，每层各实例化下一层一次。
2. 用 `read_slang --keep-hierarchy chain.sv --top top` 读取。
3. 在 `src/slang_frontend.cc:3772` 的 for 循环里想象执行过程：初始 `queue` 只有 `top`（size=1）。
4. 用 Yosys `ls` 列出生成的模块，应该看到 `top`、`mid$...`、`leaf$...` 三个（命名带层次后缀）。

**需要观察的现象**：
- i=0 处理 `top` 时，发现 `mid` 要保留 → `get_or_emplace` 建 `mid` 的 NetlistContext，queue 变成 size=2。
- i=1 处理 `mid` 时，发现 `leaf` 要保留 → 建 `leaf`，queue 变成 size=3。
- i=2 处理 `leaf`，无子实例，queue 不再增长。
- 最终 `ls` 列出 3 个模块，命名都带 `$` 层次路径后缀（因为实例缓存被禁用）。

**预期结果**：生成 3 个独立模块，模块名形如 `\top`、`\mid$top.b`、`\leaf$top.b.c`（具体后缀取决于实例的层次路径）。运行输出**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：如果把主循环改成 `for (int i = 0; i < N; i++)`（其中 `N` 是循环开始时队列的长度，固定不变），会发生什么？
**答案**：只有初始的顶层模块会被翻译。处理顶层时新建的子模块虽然被 push 到队列尾部，但因为 `N` 固定，循环不会迭代到它们，子模块的 `NetlistContext` 永远不会被 `PopulateNetlist` 访问，最终网表里会有「只声明了端口、没有内部逻辑」的空子模块。这就是为什么循环上界必须每次重新求值。

**练习 2**：默认禁用实例缓存（`DisableInstanceCaching=true`）对保留层次有什么影响？
**答案**：它使 `get_instance_body` 返回每个实例各自的 body，于是 `HierarchyQueue::netlists` 以「实例体指针」为 key 时，同一模块的两个实例（哪怕参数完全相同）也会得到两个不同的 `NetlistContext`、两个不同的 RTLIL 模块（命名带各自层次路径）。这牺牲了模块共享以换取每个实例的独立特化，避免不同实例的上下文相互污染。

---

### 4.5 find_symbol_realm：符号归属与跨边界检查

#### 4.5.1 概念说明

层次被保留后，会出现一个新的问题：**层次引用**（hierarchical reference，如 `top.u.sub.sig`）可能跨越被保留的模块边界。sv-elab 在展平时，所有符号都在同一个 realm（同一个 `NetlistContext`）里，引用随便指；但保留层次后，一个 `NetlistContext` 只能翻译自己 realm 内的符号，跨边界的引用需要被检测并（通常）拒绝。

`find_symbol_realm` 用来回答「这个符号最终归属哪个 realm」。它从符号所在的作用域开始向上走，找到第一个「不会被展平」的实例体——那就是该符号的 realm。如果一路走到 `Root` 都没找到（例如包里的静态变量），返回 `nullptr`。

#### 4.5.2 核心流程

```
从 symbol.getParentScope() 开始:
  loop:
    若当前 scope 是 Root          → 返回 nullptr（符号在模块层次之外）
    若当前 scope 是某个 InstanceBody:
        取其 parentInstance
        若 parentInstance 在 Root 下，或 parentInstance 不应展平
            → 返回这个 InstanceBody（它就是 realm）
        否则继续向上（scope = parentInstance 所在 scope）
    否则（普通 scope，如 begin/end 块）
        → 继续向上（scope = scope.getParentScope()）
```

核心不变量：**返回的实例体是「向上第一个不会被展平的边界」**。在 NONE（全展平）模式下，所有非顶层实例都会被展平，所以任何符号的 realm 都是顶层模块体；在 ALL 模式下，每个实例都是边界，符号的 realm 就是它最近的那个实例体。

#### 4.5.3 源码精读

[src/slang_frontend.cc:3304-3329](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L3304-L3329) —— `find_symbol_realm` 全文。逐段：

- 第 3306 行：从符号的直接父作用域起步。
- 第 3310–3315 行：走到 `Root` 仍没遇到非展平边界，返回 `nullptr`（注释举的例子是「包里的静态变量」，它在模块层次之外）。
- 第 3316–3324 行：遇到 `InstanceBodySymbol` 时，看它的 `parentInstance` 是否在 `Root` 下（即顶层），或者 `should_dissolve(*parentInstance)` 是否为 false（即父实例被保留）。满足任一条件，当前 body 就是 realm；否则跳到父实例所在作用域继续向上。
- 第 3325–3327 行：普通作用域（命名块、generate 块等）直接继续向上。

`find_symbol_realm` 的主要消费者是 `check_hier_ref`，用来校验层次引用是否跨了保留边界：

[src/slang_frontend.cc:3361-3384](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L3361-L3384) —— `check_hier_ref`。先用 `find_symbol_realm` 算出被引用符号的 realm；若为 `nullptr` 报 `HierarchicalRefOutsideModulesUnsupported`；若与当前 `netlist.realm` 不同，说明跨了保留边界，报 `ReferenceAcrossKeptHierBoundary`，并通过 `should_dissolve(..., &diag)` 在诊断里指明是哪条边界挡住了引用。

`realm` 成员本身的定义与注释：

[src/slang_frontend.h:542-546](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.h#L542-L546) —— `realm` 是「当前 `NetlistContext` 对应的实例体」，注释说明它「可能是、也可能不是当前正在处理的直接包含体」——因为展平时，当前处理的符号可能来自已被展平的子实例，其 realm 仍是上层那个不被展平的边界。

#### 4.5.4 代码实践

**目标**：理解跨保留边界的层次引用为什么会被拒绝。

**步骤**：
1. 阅读测试 [tests/various/hierref_error.ys](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/tests/various/hierref_error.ys)，它专门测试层次引用的错误诊断。
2. 构造一个设计：顶层 `top` 保留子模块 `sub`（用 `--keep-hierarchy`），在 `top` 里用层次路径去引用 `sub` 内部的某个信号，例如 `assign y = u.inner_reg;`。
3. 预测 `check_hier_ref` 会怎么走：`find_symbol_realm(inner_reg)` 返回 `sub` 的实例体，与 `top` 的 realm 不同 → 触发 `ReferenceAcrossKeptHierBoundary`。

**需要观察的现象**：运行后是否报 `ReferenceAcrossKeptHierBoundary`（或对应中文环境的等价诊断），并附带一条指出边界所在的 note。

**预期结果**：在 `--keep-hierarchy` 下，跨边界的层次引用被拒；同样的引用在默认（NONE）模式下因为 `sub` 被展平、`inner_reg` 与顶层同 realm 而被接受。具体诊断文案**待本地验证**。

#### 4.5.5 小练习与答案

**练习 1**：在默认（NONE，全展平）模式下，`find_symbol_realm` 对一个三层设计 `top/mid/leaf` 里 `leaf` 的某个寄存器返回什么？
**答案**：返回顶层 `top` 的实例体。因为 `mid`、`leaf` 都会被展平，向上走时它们的 `should_dissolve(parentInstance)` 都为 true，循环会一路跳过，直到 `top`（其 parentInstance 在 Root 下）才停下。所以全展平模式下，所有符号的 realm 都是顶层。

**练习 2**：为什么 `find_symbol_realm` 返回 `nullptr` 时 `check_hier_ref` 要报 `HierarchicalRefOutsideModulesUnsupported`？
**答案**：`nullptr` 表示符号不在任何模块实例体内（典型是包里的静态变量）。sv-elab 的 `NetlistContext` 总是绑定到一个实例体 realm，无法翻译「游离于模块层次之外」的符号，所以这种引用不被支持，必须报错而非悄悄忽略。

---

## 5. 综合实践

把本讲的四个模块串起来，做一个「模式对比」小任务。

**设计**：写一个含普通子模块、一个黑盒、一个带 inout 端口模块的小设计 `mix.sv`：

```systemverilog
(* blackbox *)
module bb(input logic a, output logic y);  // 黑盒：命中 is_blackbox 第 3 条
endmodule

module iomod(inout wire b);                // inout：should_dissolve 第 3 条，恒不展平
endmodule

module sub(input logic a, output logic y); // 普通子模块：展平与否取决于模式
    assign y = ~a;
endmodule

module top(input logic a, inout wire bidir, output logic y);
    logic w;
    sub u(.a(a), .y(w));      // 普通子实例
    bb  v(.a(a), .y());       // 黑盒实例
    iomod io(.b(bidir));      // inout 实例
    assign y = w;
endmodule
```

**任务**：

1. 用三种模式分别读取：默认（NONE）、`--best-effort-hierarchy`、`--keep-hierarchy`。
2. 每次读取后用 `ls` 列模块，用 `select top; show` 看顶层结构，记录：
   - `sub` 实例：在 NONE 下应被展平（顶层直接有 `~a`），在 BEST_EFFORT/ALL 下应保留为 cell。
   - `bb` 实例：三种模式下都应是 cell（黑盒恒保留）。
   - `iomod` 实例：三种模式下都应是 cell（inout 恒不展平）。
3. 预测模块总数：NONE 下应为「top + bb + iomod 的特化模块」；ALL 下还应多出 `sub` 的特化模块。
4. 解释每一类实例为何如此处理，分别引用 `should_dissolve` 与 `is_blackbox` 的哪一条判据。

**预期结论**：
- 黑盒（`bb`）与 inout 模块（`iomod`）的边界与层次模式**无关**，由 `should_dissolve`/`is_blackbox` 的前置判据决定。
- 只有普通子模块（`sub`）的边界随 `hierarchy_mode()` 变化。
- 保留的模块通过 `HierarchyQueue::get_or_emplace` 按需生成，命名带层次路径后缀。
- 跨这些保留边界的层次引用会被 `find_symbol_realm` + `check_hier_ref` 拦下。

运行输出与具体模块命名**待本地验证**。

## 6. 本讲小结

- sv-elab 用「realm」表示一个 `NetlistContext` 对应的实例体边界；每个不被展平的实例体就是一个 realm，对应一个 RTLIL 模块。
- `is_blackbox` 用四条判据（`cellDefine`、`--blackboxed-module` 名字集合、`(* blackbox *)` 属性、`--empty-blackboxes` 下的空模块）判定黑盒，黑盒恒不展平。
- `should_dissolve` 按严格优先级判定：黑盒→不展平、接口→展平、inout 端口→不展平，其余才交给 `hierarchy_mode()`（NONE 展平 / BEST_EFFORT 看端口干净度 / ALL 不展平）。
- `hierarchy_mode()` 把 `--keep-hierarchy`（优先）与 `--best-effort-hierarchy` 两个布尔开关换算成 NONE/BEST_EFFORT/ALL 三态。
- `HierarchyQueue` 用「实例体→NetlistContext」映射 + 按需 `get_or_emplace` + 上界动态求值的消费循环，实现模块的延迟、按需生成；默认禁用实例缓存使每个保留实例得到特化模块。
- `find_symbol_realm` 向上找到第一个不展平的实例体作为符号的 realm，`check_hier_ref` 据此拦截跨保留边界的层次引用。

## 7. 下一步学习建议

- **u7-l3 黑盒的导入与导出**：本讲只讲了「为什么黑盒不展平」以及它在父模块里变成 cell；黑盒定义如何被导出成空 RTLIL 模块（`export_blackbox_to_rtlil`）、未定义模块如何从已有 RTLIL 设计补全（`import_blackboxes_from_rtlil`），都在 u7-l3。
- **重读 u2-l2 / u3-l1**：带着本讲对 realm 与 `HierarchyQueue` 的理解回去看 `execute()` 主循环和 `NetlistContext` 的构造/析构，会看到层次处理是如何嵌入整体翻译流水线的。
- **源码延伸阅读**：阅读 `src/blackboxes.cc`（黑盒桥接）、`tests/various/intf_w_hierarchy.ys` 与 `tests/various/hierref_error.ys`（接口层次与层次引用错误的实际测试），把本讲的判定规则映射到具体测试断言上。

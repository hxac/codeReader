# 黑盒的导入与导出

## 1. 本讲目标

sv-elab 的翻译主链路是把 SystemVerilog「精化」成 RTLIL 网表，但真实设计里几乎总会出现一些模块**不想、不能或不需要**被翻译：它们可能是用别的语言/工具已经综合好的 IP、是预先用 `read_verilog` 载入的网表、是只有端口声明的抽象接口，或者是特意留空的占位模块。这类模块统称为「黑盒（blackbox）」。

本讲聚焦 sv-elab 与黑盒打交道的双向桥梁：一边把**已存在于 RTLIL 设计里的模块**反向「导入」回 slang 的世界，让 SystemVerilog 源码能合法地例化它们；另一边把**在 SystemVerilog 里声明为黑盒的模块**「导出」成 RTLIL 黑盒模块，供下游 Yosys 流程引用。学完本讲你应当能够：

- 说清 `import_blackboxes_from_rtlil` 如何在不写文本解析器的前提下，手搓一棵 slang 语法树把 RTLIL 模块伪装成 SV 黑盒声明；
- 解释 `is_decl_empty_module` 配合 `--empty-blackboxes` 如何把「空体模块」判定为黑盒；
- 描述 `export_blackbox_to_rtlil` 如何把一个 SV 黑盒实例的端口与参数落到 RTLIL 模块上，以及它在端口宽度非常量时如何报 `BboxExportPortWidths`；
- 在遇到「未定义模块」报错时，判断 `extern_modules`（默认开启）会不会从已有 RTLIL 设计里把黑盒补全。

## 2. 前置知识

- **黑盒（blackbox）**：在 Yosys/RTLIL 的语境里，指一个只声明了端口、没有内部实现的模块，对应 `(* blackbox *)` 属性。综合时它被当作一个不透明单元原样保留，内部留给下游（技术映射、网表拼接）处理。
- **`RTLIL::escape_id` / `unescape_id`**：Yosys 的命名转义约定。RTLIL 内部用带前导反斜杠 `\` 的标识符表示「需要转义的名字」。`unescape_id` 把 `\foo` 还原成 `foo` 用来去 slang 里查定义；`escape_id` 则反向加回前缀。
- **slang 的两阶段**：slang 先把源码解析成**语法树（SyntaxTree）**，再做语义分析得到 **AST（`ast::Compilation`）**。本讲的「导入」走的是「直接构造语法树再喂给 Compilation」这条路，跳过了文本解析。
- **BumpAllocator**：slang 自带的「线性分配器」，一次性批量分配、整体释放，特别适合这种「构造一整棵一次性语法树」的场景。
- 本讲是 u7-l2（层次处理）的延续：`is_blackbox` 在层次判定里**优先级最高**——黑盒永不展平（`should_dissolve` 直接返回 false）。理解本讲后，你会明白黑盒模块的边界到底是怎么被「画」进 RTLIL 设计的。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [src/blackboxes.cc](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/blackboxes.cc) | 本讲主角，包含三个函数：导入、空模块判定、导出 |
| [src/slang_frontend.cc](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc) | 调用方：`execute()` 在何时调用导入、`handle(InstanceSymbol)` 在何时调用导出、`is_blackbox()` 如何用空模块判定 |
| [src/slang_frontend.h](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.h) | 三个函数的外部声明与 `SynthesisSettings::empty_blackboxes` 等字段 |
| [src/diag.h](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/diag.h) / [src/diag.cc](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/diag.cc) | 黑盒相关诊断码（`BboxExportPortWidths`、`UnsupportedPortDirection`、`BboxTypeParameter` 等）的声明与文案 |
| [tests/various/blackbox_scenarios.ys](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/tests/various/blackbox_scenarios.ys) | 三个端到端场景，覆盖「仅预载」「仅 SV 声明」「两者都有」三种组合 |

## 4. 核心概念与源码讲解

黑盒在 sv-elab 里有**两个相反的流向**，先建立这张总图：

```text
        ┌─────────────────────────┐   import_blackboxes_from_rtlil
RTLIL   │ 已载入的 RTLIL 模块      │ ──────────────────────────────┐  (RTLIL → slang)
设计    │ (read_verilog 等带入)    │                                ▼
        └─────────────────────────┘                   ┌────────────────────────┐
                                                      │ slang Compilation       │
        ┌─────────────────────────┐                   │ 有了黑盒的 module 定义  │
RTLIL   │ 新生成的 RTLIL 黑盒模块 │ ◀──────────────── │ 例化合法化              │
设计    │ (供下游引用)            │   export_blackbox └────────────────────────┘
        └─────────────────────────┘   _to_rtlil (slang → RTLIL)
```

- **导入** 解决的问题：「SV 源码里例化了一个模块 X，但 X 不在 SV 源码里定义，而在已经载入的 RTLIL 设计里。」此时 slang 的语义分析会因为「未知模块」而失败，导入就是在语义分析前把 X 作为黑盒声明塞进 Compilation。
- **导出** 解决的问题：「SV 源码里声明了一个 `(* blackbox *)` 模块 Y 并例化了它。」翻译时 sv-elab 要在当前模块里发一个指向 Y 的 cell，但 RTLIL 设计里还没有 Y 这个模块定义——导出就是顺便把 Y 作为黑盒模块补到 RTLIL 设计里。
- **空模块判定** 是导出的前置判断之一：决定一个 SV 模块到底「算不算黑盒」。

下面三个小节分别精读这三个最小模块。

### 4.1 import_blackboxes_from_rtlil：把 RTLIL 模块导入为 slang 黑盒

#### 4.1.1 概念说明

正常情况下，sv-elab 用 slang 的 driver 去读 SystemVerilog 源码。但 Yosys 是一个多语言前端共存的环境：在调用 `read_slang` 之前，用户可能已经用 `read_verilog`、`read_liberty`、`read_blif` 等命令把一批模块载入到当前的 RTLIL 设计（`RTLIL::Design`）里了。这些模块没有 SV 源码，slang 对它们一无所知。

如果 SV 源码里例化了这样一个模块，slang 语义分析会立刻报「unknown module」。`import_blackboxes_from_rtlil` 就是来打补丁的：它**反向**遍历 RTLIL 设计里已有的模块，为每一个在 slang 里「查无此名」的模块，手搓一份「只有端口、带 `(* blackbox *)` 属性」的 SV 模块语法树，塞进 Compilation。这样 slang 语义分析就能通过，sv-elab 后续翻译时也会把它当黑盒例化。

> 选项名叫 `--extern-modules`，但它已弃用、**恒为开启**（见 4.1.2）。换句话说，「从已有 RTLIL 设计补全黑盒」是默认行为。

#### 4.1.2 核心流程

`import_blackboxes_from_rtlil` 在 `execute()` 中的调用点非常关键，它卡在「源码已解析、Compilation 已创建」与「翻译开始」之间：

1. `driver.parseAllSources()` 解析 SV 源码得到语法树；
2. `driver.createCompilation()` 做语义分析、产出 `ast::Compilation`；
3. **`import_blackboxes_from_rtlil(driver.sourceManager, *compilation, design)`** —— 本节主角，把 RTLIL 模块注入 Compilation；
4. （可选）`dump_ast`、诊断检查；
5. `PopulateNetlist` 遍历，真正生成 RTLIL。

导入函数内部对一个 RTLIL 模块做这几件事：

- 用 `unescape_id` 取出裸名，先 `tryGetDefinition` 查 slang 是否已有同名定义——**有则跳过**（SV 源码里的定义优先）；
- 否则用 `BumpAllocator` 逐个 token 手搓 `ModuleDeclarationSyntax`：每个端口根据 `port_input`/`port_output` 标志决定 `input`/`output`/`inout`，维度写成 `[width-1:0]` 的常量范围；
- 给模块挂上 `(* blackbox *)` 属性；
- 把所有模块声明打包成一个 `CompilationUnitSyntax` → `SyntaxTree`，标记 `isLibraryUnit = true`，最后 `target.addSyntaxTree(tree)`。

一个重要约束：导入的黑盒**不带参数**（源码里写着 `// parameters: todo`）。因此对导入来的黑盒做 `#(.FF(8))` 参数覆盖，会被 slang 自己的语义分析拒绝（这类限制对应注册过的诊断 `NoParamsOnUnkBboxes`，文案为「parameters on unknown blackboxes unsupported」）。

#### 4.1.3 源码精读

先看 `execute()` 里唯一的调用点，注意它由 `extern_modules.value_or(true)` 守卫——默认就是开的：

[src/slang_frontend.cc:3722-3723](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L3722-L3723) —— `extern_modules` 默认为真时调用导入，把 RTLIL 设计 `design` 里的模块注入刚建好的 `compilation`。

`--extern-modules` 在选项注册里被标注为「已弃用、恒为开启」，其旧含义正好描述了本函数的用途：

[src/slang_frontend.cc:135-139](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L135-L139) —— 选项注册：把任何先前用 Yosys 命令载入的模块当作可例化黑盒导入。

现在看函数本体。函数签名确立了「源（RTLIL Design）→ 目标（slang Compilation）」的方向：

[src/blackboxes.cc:33-35](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/blackboxes.cc#L33-L35) —— 导入函数签名：从 `RTLIL::Design *source` 读，写进 `ast::Compilation &target`。

「SV 源码优先、已存在则跳过」的关键一行：

[src/blackboxes.cc:68-72](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/blackboxes.cc#L68-L72) —— 遍历 RTLIL 模块，若 slang 里已能查到同名定义就 `continue`，避免覆盖真实 SV 定义。

端口方向的换算——RTLIL 的两个布尔标志映射到 SV 的三种方向关键字：

[src/blackboxes.cc:79-85](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/blackboxes.cc#L79-L85) —— 据 `port_input`/`port_output` 选 `InputKeyword`/`OutputKeyword`/`InOutKeyword`。

整段手搓语法的产物是一个带 `(* blackbox *)` 属性、空体的模块声明；参数位置留着 `// parameters: todo`：

[src/blackboxes.cc:117-141](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/blackboxes.cc#L117-L141) —— 组装 `ModuleHeaderSyntax` + `blackbox` 属性 + 空成员列表，得到一个 `ModuleDeclarationSyntax`。

最后把所有手搓的模块声明包成一个库单元语法树，加入 Compilation：

[src/blackboxes.cc:144-150](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/blackboxes.cc#L144-L150) —— 打包成 `CompilationUnitSyntax` 与 `SyntaxTree`，置 `isLibraryUnit`，`addSyntaxTree` 注入。

#### 4.1.4 代码实践

实践目标：亲眼看到一个「只在 RTLIL 里有、SV 源码里没定义」的模块被自动当成黑盒例化。

操作步骤（这是 [tests/various/blackbox_scenarios.ys](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/tests/various/blackbox_scenarios.ys) 的 Scenario 1）：

1. 在已编译好 `slang.so`（或内置 read_slang）的 Yosys 里，先 `read_verilog` 一个带 `(* blackbox *)` 的模块 `foo`；
2. 再用 `read_slang` 读一段只例化 `foo`、不定义 `foo` 的 SV；
3. 跑 `hierarchy -check -top top`。

需要观察的现象：

- 若没有导入机制，第 2 步 slang 会报「unknown module foo」而失败；
- 开启默认 `extern_modules` 后，`foo` 被静默导入，`hierarchy -check` 通过，`top` 里出现一个类型为 `foo` 的 cell。

预期结果：流程成功，没有 unknown module 报错；`dump` 可见 `top` 例化了黑盒 `foo`。如果手头没有可运行的 Yosys+插件环境，标注「待本地验证」，可改为源码阅读型实践：在 `execute()` 里把第 3722 行的 `value_or(true)` 临时想成 `value_or(false)`，推演一下「SV 例化了 RTLIL 才有的模块」会发生什么。

#### 4.1.5 小练习与答案

**练习 1**：为什么导入函数要在 `addSyntaxTree` 之前把 `isLibraryUnit` 置为真？
**答案**：标记为库单元告诉 slang 这份语法树是「预定义/库代码」，不对应一份用户源文件，在错误归属、文件名展示等处理上区别对待；同时它强调这些黑盒声明是补全性质的，而非用户主输入。

**练习 2**：如果同一个模块名既出现在 SV 源码里、又出现在已载入的 RTLIL 设计里，导入函数会怎么做？
**答案**：会跳过。第 71 行的 `tryGetDefinition(...).definition` 命中即 `continue`，SV 源码里的定义优先，不会被 RTLIL 版本覆盖。

### 4.2 is_decl_empty_module：空模块的黑盒化判定

#### 4.2.1 概念说明

有时候设计里会出现「只有端口和参数、没有任何电路行为」的模块，比如只声明了 `parameter`、`typedef`、端口列表，body 是空的。默认情况下 sv-elab 会尝试把它当成普通模块去展平翻译，结果得到一个空模块。

`--empty-blackboxes` 选项改变了这个默认：开启后，凡是「声明层面为空」的模块都被当成黑盒。判定「声明层面为空」的工作就交给 `is_decl_empty_module`——它只看**语法节点**（SyntaxNode），枚举模块的成员，只要每一项都落在「无害声明」白名单里，就判定为空。

#### 4.2.2 核心流程

`is_decl_empty_module(syntax)` 的逻辑很简单：

1. 若 `syntax` 不是 `ModuleDeclaration`，直接返回 false；
2. 遍历模块的每个 member，用一个 switch 列举允许的种类：typedef、前向 typedef、参数声明、类型参数声明、端口声明、ANSI 端口、时间单位声明、函数声明、defparam、net alias；
3. 任一 member 不在白名单 → 返回 false（说明 body 里有真正的逻辑）；
4. 全部命中白名单 → 返回 true。

它唯一的调用点是 `NetlistContext::is_blackbox()`：当 `--empty-blackboxes` 开启时，用它来判定；命中则附加一条 `NoteModuleBlackboxBecauseEmpty` 说明「该模块因空体而成为黑盒」。

注意它工作在**语法层**而非语义层：它不关心参数被赋了什么值、端口实际多宽，只看源码里写了哪些 member 种类。

#### 4.2.3 源码精读

判定函数本体，关键是那个「白名单 switch」：

[src/blackboxes.cc:153-179](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/blackboxes.cc#L153-L179) —— 非 ModuleDeclaration 返回 false；逐个 member 检查，命中允许种类 `break` 继续，否则 `return false`；全部通过返回 true。

唯一的调用点，在 `is_blackbox()` 的末尾，受 `empty_blackboxes` 选项控制：

[src/slang_frontend.cc:3215-3221](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L3215-L3221) —— 仅当 `--empty-blackboxes` 开启时，用 `is_decl_empty_module` 判定，并按需附加「因空体而黑盒」的 note。

`--empty-blackboxes` 的注册文案直接点明了用途：

[src/slang_frontend.cc:109-110](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L109-L110) —— 选项注册：「Assume empty modules are blackboxes」。

`is_blackbox()` 本身按优先级串联了多种判黑盒途径，`empty_blackboxes` 是其中最后一条：

[src/slang_frontend.cc:3196-3224](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L3196-L3224) —— `is_blackbox` 全貌：`cellDefine` → `blackboxed_modules` 集合 → `(* blackbox *)` 属性 → （可选）空模块判定。

#### 4.2.4 代码实践

实践目标：观察同一个空体模块在不开/开 `--empty-blackboxes` 时的不同处理。

操作步骤：

1. 准备一个只含端口与参数、body 为空的 SV 模块（例如 `module emptymod(input [3:0] a, output b); endmodule`），在顶层例化它；
2. 分别用 `read_slang` 与 `read_slang --empty-blackboxes` 读入；
3. 用 `dump` 查看顶层对它的例化形态，并用 `show` 或检查模块列表看 `emptymod` 自身是否被当作黑盒。

需要观察的现象：

- 不开选项时，`emptymod` 被当普通模块展平，顶层看到的是展平后的连线（或一个空模块）；
- 开启选项后，`is_decl_empty_module` 返回 true，`emptymod` 成为黑盒，顶层里出现一个指向黑盒 `emptymod` 的 cell，`emptymod` 模块自身带 `blackbox` 属性。

预期结果：两次 `dump` 的模块边界不同。若无法运行，标注「待本地验证」，改为阅读型实践：对照 4.2.3 的白名单，判断 `module m; logic [3:0] x; endmodule`（含一个真正变量声明）会不会被判为空——答案是不会，`logic` 声明不在白名单。

#### 4.2.5 小练习与答案

**练习 1**：一个模块 body 里只有 `parameter WIDTH = 8;` 和一个 `typedef`，它会被 `is_decl_empty_module` 判为空吗？
**答案**：会。`ParameterDeclaration` 和 `TypedefDeclaration` 都在白名单里，全部 member 命中，返回 true。

**练习 2**：为什么 `is_decl_empty_module` 只看语法层，而不去查参数的实际值？
**答案**：因为判定目标只是「body 里有没有可综合逻辑」，参数与端口声明本身不构成电路行为；用语法层判定既足够又便宜，且能在语义分析之后、翻译之前的任意时机调用，不依赖具体参数取值。

### 4.3 export_blackbox_to_rtlil：把 slang 黑盒实例导出回 RTLIL

#### 4.3.1 概念说明

与导入相反的场景：SV 源码里**显式**声明了一个 `(* blackbox *)` 模块（带端口、可能带参数），并在某处例化了它。sv-elab 翻译到这个例化时要做两件事：

1. 在当前模块里发一个 RTLIL **cell**，类型指向该黑盒模块，并把端口连接和参数值接好（这部分在 `handle(InstanceSymbol)` 里）；
2. **在 RTLIL 设计里补出该黑盒模块的定义**——因为 RTLIL 设计原先并没有这个模块，下游 pass 需要一个带端口、带 `blackbox` 属性的模块壳子才能识别它。这第二件事就是 `export_blackbox_to_rtlil` 的职责。

与导入的黑盒不同，导出路径**支持参数**（参数值落在 cell 上，模块壳子只记录参数名），但要求**端口宽度是编译期常量**——如果端口宽度依赖参数（parametric widths），导出会失败并报 `BboxExportPortWidths`。

> 一个很有用的早退：如果 RTLIL 设计里已经有同名模块（比如它本来就是被 `read_verilog` 预载的，或被导入函数注入过），`export_blackbox_to_rtlil` 直接返回，什么都不做。这解释了 [tests/various/blackbox_scenarios.ys](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/tests/various/blackbox_scenarios.ys) 的 Scenario 3：预载的 RTLIL 黑盒 + SV 声明并存时，端口宽度可以参数化，因为真正的模块壳子来自预载侧，导出被跳过。

#### 4.3.2 核心流程

`export_blackbox_to_rtlil(netlist, inst, target)` 的流程：

1. 计算模块名 `name = escape_id(inst.body.name)`；
2. **早退 A**：`target->module(name)` 已存在 → 返回（模块壳子已就位）；
3. **早退 B**：存在同名顶层实例 → 返回（顶层模块稍后会自己生成）；
4. `mod = target->addModule(name)`，置 `blackbox` 属性，把定义上的属性 `transfer_attrs` 过去；
5. `inst.body.visit(...)` 遍历端口与参数：
   - **端口**：校验类型是 fixed-size、且类型语法的维度是「常量范围选择 / 常量位选择」；任一不满足就报 `BboxExportPortWidths`，但**仍会**按 bitstream 宽度建线、设方向、登记进 `mod->ports`；方向为 `Ref` 时报 `UnsupportedPortDirection`；
   - **参数**：`mod->avail_parameters(...)` 登记参数名（注意只是名，参数值由调用方在 cell 上 `setParam`）。

端口宽度校验是这个函数最容易踩坑的部分。它逐一检查「声明类型的语法维度」是否是「左右都是字面量」的范围/位选择。设端口位宽为 \(w\)，只有当 \(w\) 能在编译期确定为常量时，RTLIL 才能建出固定宽度的 wire；若 \(w\) 依赖参数，导出拒绝。可以用一个简单式子表达这一约束：设端口宽度 \(w = f(p)\)，其中 \(p\) 为参数，则导出要求

\[
\exists\, c \in \mathbb{N}\ .\ w = c \quad \text{(与参数取值无关)}
\]

否则发 `BboxExportPortWidths`。

#### 4.3.3 源码精读

调用点：在 `handle(InstanceSymbol)` 里，确认是黑盒后，先建 cell、接端口、设参数，最后调用导出把模块壳子补进设计：

[src/slang_frontend.cc:2218-2281](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L2218-L2281) —— 黑盒例化的完整处理：建 cell、连端口、设参数（含 `BboxTypeParameter` 诊断）、`transfer_attrs`、最后调 `export_blackbox_to_rtlil`。

具体地，cell 上的参数值在这里落下，类型参数会报错：

[src/slang_frontend.cc:2265-2278](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L2265-L2278) —— 遍历参数把解析值 `setParam` 到 cell，遇到 `TypeParameterSymbol` 发 `BboxTypeParameter`。

导出函数本体。先看两个早退，它们决定了「何时什么都不做」：

[src/blackboxes.cc:187-199](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/blackboxes.cc#L187-L199) —— 同名模块已存在则返回；同名顶层实例会稍后生成也返回。

创建模块壳子并搬运属性：

[src/blackboxes.cc:201-203](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/blackboxes.cc#L201-L203) —— `addModule` + 置 `blackbox` 属性 + `transfer_attrs`。

端口宽度校验是导出的核心难点，校验失败但**仍然建线**的逻辑在这里：

[src/blackboxes.cc:205-298](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/blackboxes.cc#L205-L298) —— 端口访问者：检查类型语法维度是否为常量范围/位选择，否则发 `BboxExportPortWidths`；无论是否拒绝都按 `getBitstreamWidth()` 建 wire、设方向、登记端口。

方向映射与 `Ref` 拒绝的一段：

[src/blackboxes.cc:283-294](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/blackboxes.cc#L283-L294) —— `In`/`Out`/`InOut` 映射到 `port_input`/`port_output`，`Ref` 发 `UnsupportedPortDirection`。

参数只登记名字：

[src/blackboxes.cc:299-301](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/blackboxes.cc#L299-L301) —— 参数访问者：仅 `avail_parameters` 登记参数名。

相关诊断的声明与文案：

[src/diag.h:41-42](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/diag.h#L41-L42) 与 [src/diag.h:48-49](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/diag.h#L48-L49) —— 黑盒相关诊断码声明；
[src/diag.cc:217-218](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/diag.cc#L217-L218)、[src/diag.cc:220-221](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/diag.cc#L220-L221)、[src/diag.cc:238-239](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/diag.cc#L238-L239)、[src/diag.cc:241-242](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/diag.cc#L241-L242) —— 对应文案与严重级别（`UnsupportedBlackboxConnection`、`UnsupportedPortDirection`、`BboxTypeParameter`、`BboxExportPortWidths`，均为 Error）。

#### 4.3.4 代码实践

实践目标：触发 `BboxExportPortWidths`，并对比「端口宽度常量 vs 参数化」两种情形。

操作步骤：

1. 参考 [tests/various/blackbox_scenarios.ys](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/tests/various/blackbox_scenarios.ys) 的 Scenario 2，写一个 SV 声明的黑盒，端口宽度**不**依赖参数（如 `input [2:0] a`），跑通导出；
2. 再把端口改成依赖参数（如 `input [WIDTH-1:0] a`，`parameter WIDTH = 3;`），**不**预载任何同名 RTLIL 模块，单独 `read_slang`；
3. 观察日志与 `dump`。

需要观察的现象：

- 第 1 步：导出成功，RTLIL 设计里出现带 `blackbox` 属性、端口宽度为 3 的 `foo` 模块，cell 参数 `ff=8` 正确落在例化 cell 上；
- 第 2 步：由于 RTLIL 侧没有同名模块、导出无法把参数化宽度物化为常量，会报 `cannot export a blackbox definition with non-constant port widths`（即 `BboxExportPortWidths`，Error）；
- 对照 Scenario 3：若先 `read_verilog` 一个同名黑盒（端口可参数化）再 `read_slang`，导出因早退 A（`target->module(name)` 已存在）而跳过，参数化宽度不再报错。

预期结果：第 2 步报错、第 1、3 步通过。若本地无法运行，标注「待本地验证」，可改为源码阅读型实践：在 [src/blackboxes.cc:205-298](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/blackboxes.cc#L205-L298) 里追踪「校验失败后仍执行 `addWire`」的路径，解释为什么即便报错也仍然建线（保证模块壳子结构完整、便于下游报错定位）。

#### 4.3.5 小练习与答案

**练习 1**：导出函数里，参数值到底存在哪里？模块壳子上记了什么？
**答案**：参数**值**存在例化 cell 上（`handle(InstanceSymbol)` 里 `cell->setParam`）；模块壳子只通过 `avail_parameters` 登记参数**名**，表示「这个黑盒模块接受这些参数」。

**练习 2**：为什么 Scenario 3（预载 + SV 声明并存）允许参数化端口宽度，而 Scenario 2（只有 SV 声明）不允许？
**答案**：Scenario 3 里 RTLIL 设计已有同名模块（预载带入），导出函数命中早退 A 直接返回，根本不去做端口宽度物化；Scenario 2 里 RTLIL 侧空缺，必须由导出函数建出固定宽度的 wire，参数化宽度无法物化，于是报 `BboxExportPortWidths`。

**练习 3**：导出函数对 `Ref` 方向的端口如何处理？
**答案**：在方向映射的 switch 里，`Ref` 不属于 `In`/`Out`/`InOut`，落到 default 分支发 `UnsupportedPortDirection` 诊断，并把方向名追加进诊断。

## 5. 综合实践

把本讲三个模块串起来，完成规格里指定的实践：**说明当 `read_slang` 遇到一个未定义模块时，`extern_modules` 如何从已有 RTLIL 设计中补全黑盒。**

请按下面的步骤梳理一遍数据流，并尽可能在本机验证：

1. **构造输入**：先 `read_verilog`（或任意 Yosys 前端）载入一个带 `(* blackbox *)` 的模块 `M`，它只在 RTLIL 设计里存在；SV 源码里只有顶层 `top` 例化 `M`，**不**定义 `M`。
2. **定位补全点**：在 [src/slang_frontend.cc:3722-3723](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L3722-L3723) 确认 `extern_modules.value_or(true)` 默认开启，于是调用 `import_blackboxes_from_rtlil`。
3. **追踪注入**：在 [src/blackboxes.cc:68-150](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/blackboxes.cc#L68-L150) 跟踪 `M`：`tryGetDefinition` 查不到（SV 没定义）→ 手搓一份带 `(* blackbox *)` 的语法树 → `addSyntaxTree` 注入 Compilation。现在 slang「认识」`M` 了。
4. **语义分析通过**：于是 `top` 里对 `M` 的例化在 slang 语义分析中合法。
5. **翻译阶段**：`PopulateNetlist` 走到该例化，`is_blackbox` 命中（注入时挂了 `blackbox` 属性），进入 [src/slang_frontend.cc:2218-2281](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L2218-L2281) 的黑盒分支建 cell；随后调 `export_blackbox_to_rtlil`，但因其早退 A（`target->module(name)` 命中最初预载的 `M`）而立即返回——RTLIL 侧沿用预载模块。
6. **产出**：最终 `top` 里有一个类型为 `M` 的 cell，`M` 模块壳子来自最初的 `read_verilog`，全链路打通。

请用一张时序图（文字版即可）把「`read_verilog` → `createCompilation` → 导入注入 → 语义分析 → 翻译建 cell → 导出早退」这六个时刻画出来，并标注每一步在哪个源码文件、哪几行。若本地有环境，用 Scenario 1 实跑一遍作为佐证。

## 6. 本讲小结

- sv-elab 与黑盒打交道有**两个相反方向**：`import_blackboxes_from_rtlil` 把 RTLIL 模块导入 slang，`export_blackbox_to_rtlil` 把 SV 黑盒导出回 RTLIL。
- 导入靠**手搓 slang 语法树**（`BumpAllocator` + `ModuleDeclarationSyntax`）实现，不写文本解析；它由默认开启的 `extern_modules` 触发，在 `createCompilation` 之后、翻译之前运行；SV 源码里的同名定义优先（`tryGetDefinition` 命中即跳过）。
- `is_decl_empty_module` 在语法层判定「空体模块」，配合 `--empty-blackboxes` 把这类模块当黑盒；它是 `is_blackbox()` 判定链的最后一条。
- 导出在 `handle(InstanceSymbol)` 的黑盒分支末尾被调用；它支持参数（值落 cell、名落 `avail_parameters`），但要求端口宽度为编译期常量，否则发 `BboxExportPortWidths`；若 RTLIL 侧已有同名模块则早退跳过。
- 三个场景（仅预载 / 仅 SV 声明 / 两者并存）的差别，本质是「模块壳子由谁提供」与「端口宽度能否物化为常量」的组合，[tests/various/blackbox_scenarios.ys](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/tests/various/blackbox_scenarios.ys) 完整覆盖了它们。

## 7. 下一步学习建议

- 回到 u7-l2，把本讲的黑盒判定与 `should_dissolve` / `hierarchy_mode` / `HierarchyQueue` 串起来看：黑盒是层次判定里「永不展平」的最高优先级情形，理解了导出，你就明白保留层级时黑盒模块的边界是如何被落地的。
- 阅读下一讲 u7-l4（SystemVerilog 断言），它同样依赖 `is_blackbox` 之外的「特殊例化处理」分支；本讲建立的 `handle(InstanceSymbol)` 分派直觉会直接复用。
- 想做二次开发的读者，可以尝试给 `import_blackboxes_from_rtlil` 补上「参数」支持（源码里那行 `// parameters: todo`）：在 [src/blackboxes.cc:117-125](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/blackboxes.cc#L117-L125) 的 header 里加入 `ParameterPortList`，并参考 u8-l4 的扩展流程补一个等价性测试。

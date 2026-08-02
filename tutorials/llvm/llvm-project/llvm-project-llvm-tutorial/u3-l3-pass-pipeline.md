# Pass 流水线与 PassBuilder

## 1. 本讲目标

学完本讲，你应该能够：

- 说清一条 `-passes=...` 文本字符串是如何被 `PassBuilder` 解析成一棵嵌套的流水线树，并最终变成一个 `ModulePassManager` 的；
- 解释 `-O2` 与 `-passes="default<O2>"` 为什么等价，以及「默认优化流水线」`buildPerModuleDefaultPipeline` 在源码里由哪几段拼装而成；
- 区分两类扩展机制——`registerPipelineParsingCallback`（自定义 pass 名字）与扩展点回调（EP，在默认流水线的固定位置注入 pass）——并知道它们各自的注册时机。

本讲承接 u3-l1（新 Pass 管理器的 `PassManager` / `AnalysisManager` 骨架）与 u3-l2（如何写一个 pass 并把它的名字注册进 `-passes=`）。这两讲回答的是「pass 是什么、怎么写」，本讲回答的是「**这些 pass 是怎么被串成一条流水线的**」。

## 2. 前置知识

阅读本讲前，请确认你已经理解：

- **IR 单元层级**：Module → CGSCC（调用图强连通分量）→ Function → Loop。新 Pass 管理器按这四层分别有对应的 `PassManager`，跨层靠 adaptor pass 桥接（u3-l1）。
- **pass 与 analysis 的区别**：pass 改 IR、返回 `PreservedAnalyses`；analysis 只读、结果被缓存（u3-l1）。
- **`PassBuilder` 的职责**：它是新 PM 的「装配车间」，既负责批量注册内置 pass/analysis，也负责把 `-passes=` 文本翻译成内存里的 `PassManager`（u3-l2 已接触它的注册回调入口）。
- **opt 是薄壳**：`opt` 只解析命令行，真正干活的逻辑来自 `lib/`（u1-l3）。

本讲会反复提到两个文件：

| 文件 | 作用 |
| --- | --- |
| `lib/Passes/PassBuilder.cpp` | 文本解析、单 pass 分派、分析注册、别名分析 pipeline |
| `lib/Passes/PassBuilderPipelines.cpp` | 默认优化流水线（O0/O1/O2/O3、LTO、ThinLTO 等）的拼装，以及扩展点回调的触发点 |

把它们理解为「翻译官」和「默认配方师」即可。

## 3. 本讲源码地图

| 关键点 | 文件 | 作用 |
| --- | --- | --- |
| 文本解析器 | [`lib/Passes/PassBuilder.cpp`](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/lib/Passes/PassBuilder.cpp) | `parsePipelineText` 把字符串切成嵌套树 |
| 顶层入口 | 同上 | `parsePassPipeline(ModulePassManager&, ...)` 自动包装非 module pass |
| 单层分派 | 同上 | `parseModulePass` / `parseFunctionPass` 按 IR 层级分发 |
| pass 名注册表 | `lib/Passes/PassRegistry.def` | X-Macro 列出所有内置 pass，含 `default<O#>` |
| 默认流水线 | `lib/Passes/PassBuilderPipelines.cpp` | `buildPerModuleDefaultPipeline` 等 |
| 扩展点声明 | `include/llvm/Passes/PassBuilder.h` | 一组 `register...EPCallback` |
| opt 入口 | `tools/opt/optdriver.cpp`、`tools/opt/NewPMDriver.cpp` | 把 `-O2` 映射成 `default<O2>` 并调用 `parsePassPipeline` |

> 提示：`PassRegistry.def` 是 X-Macro 文件（u2-l1 已介绍过 X-Macro 思想），它本身不是 C++ 源文件，而是被 `#include` 进来后由宿主文件提供的宏「展开」成不同代码。本讲的 `default<O#>` 就定义在这里。

## 4. 核心概念与源码讲解

### 4.1 文本 pipeline 的解析机制

#### 4.1.1 概念说明

用户在命令行写 `-passes="default<O2>,function(instcombine)"` 时，给的是一段**文本**；而新 PM 运行时需要的是一个**装填好 pass 的 `ModulePassManager` 对象**。把文本翻译成对象的工作，全由 `PassBuilder::parsePassPipeline` 一族函数完成。

理解这段的关键，是先建立一个心智模型：**流水线文本 = 嵌套的括号树**。例如：

```
module(function(instcombine,sroa),dce,cgscc(inliner,function(...)))
```

外层是一个 module 管理器，里面依次装了：一个 function 子管理器（又装了 instcombine、sroa）、一个 dce、一个 cgscc 子管理器（装了 inliner 和一个嵌套 function）。括号表达「包含关系」，逗号表达「同级顺序」。这与 u3-l1 讲的「按 IR 单元分层、跨层用 adaptor」完全对应——括号就是「新建一个下层 PassManager」。

#### 4.1.2 核心流程

解析分三步：

1. **切词建树**：`parsePipelineText` 扫描字符串里的 `,`、`(`、`)` 三个分隔符，用一个栈把括号层级还原成一棵 `PipelineElement` 树。每个 `PipelineElement` 有一个 `Name` 和一组 `InnerPipeline` 子节点。
2. **顶层自动包装**：`parsePassPipeline(ModulePassManager&, ...)` 看树的第一个名字属于哪一层；如果不是 module 层，就自动补一层包装（这是「便捷写法」的来源）。
3. **逐层分派**：对树的每一层，调用对应 IR 单元的 `parseXxxPass`，它先处理特殊关键字（`module`/`cgscc`/`function`/`loop`），再用 `PassRegistry.def` 展开内置 pass，最后问一遍用户注册的回调。

伪代码：

```
parsePassPipeline(MPM, text):
    tree = parsePipelineText(text)          # 字符串 → PipelineElement 树
    first = tree[0].Name
    if not isModulePassName(first):         # 第一层不是 module pass？
        if isFunctionPassName(first):       # 自动补一层 function(...)
            tree = [{Name:"function", Inner:tree}]
        elif isCGSCCPassName(first):        # 或补 cgscc(...)
            tree = [{Name:"cgscc", Inner:tree}]
        elif isLoopPassName(first):         # 或补 function(loop(...))
            tree = [{Name:"function", Inner:[{Name:"loop", Inner:tree}]}]
        ...                                 # machine-function 同理
    parseModulePassPipeline(MPM, tree)      # 递归分派
```

#### 4.1.3 源码精读

先看 `PipelineElement` 的定义，它就是树的一个节点：

```cpp
struct PipelineElement {
  StringRef Name;
  std::vector<PipelineElement> InnerPipeline;
};
```

> [`include/llvm/Passes/PassBuilder.h:130-133`](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/include/llvm/Passes/PassBuilder.h#L130-L133) —「名字 + 可选的内层流水线」。注释明确：如果名字是 pass（如 `instcombine`），`InnerPipeline` 为空；如果是 pipeline 类型（如 `cgscc`），`InnerPipeline` 装着它的子 pass。

`parsePipelineText` 是纯字符串处理，逻辑很短，关键是「遇到 `(` 压栈、遇到 `)` 弹栈」：

```cpp
SmallVector<std::vector<PipelineElement> *, 4> PipelineStack = {&ResultPipeline};
for (;;) {
  std::vector<PipelineElement> &Pipeline = *PipelineStack.back();
  size_t Pos = Text.find_first_of(",()");
  Pipeline.push_back({Text.substr(0, Pos), {}});
  if (Pos == Text.npos) break;          // 没有更多分隔符，结束
  char Sep = Text[Pos];
  Text = Text.substr(Pos + 1);
  if (Sep == ',') continue;             // 逗号：继续在当前层
  if (Sep == '(') {                     // 左括号：进入刚 push 的元素的 InnerPipeline
    PipelineStack.push_back(&Pipeline.back().InnerPipeline);
    continue;
  }
  // Sep == ')'：弹栈；括号不配对则返回 nullopt
  do {
    if (PipelineStack.size() == 1) return std::nullopt;
    PipelineStack.pop_back();
  } while (Text.consume_front(")"));
  ...
}
```

> [`lib/Passes/PassBuilder.cpp:2013-2067`](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/lib/Passes/PassBuilder.cpp#L2013-L2067) — 注意 `find_first_of(",()")` 一次定位三种分隔符；左括号时把栈顶指向「刚加入的那个元素的 `InnerPipeline`」，于是后续读到的名字就自动成为它的子节点。这种「边读边长树」的写法避免了递归。

建好树后，`parsePassPipeline(ModulePassManager&, ...)` 处理「自动包装」便捷写法：

```cpp
StringRef FirstName = Pipeline->front().Name;
if (!isModulePassName(FirstName, ModulePipelineParsingCallbacks)) {
  if (isCGSCCPassName(FirstName, ...))
    Pipeline = {{"cgscc", std::move(*Pipeline)}};            // 补 cgscc(...)
  else if (isFunctionPassName(FirstName, ...))
    Pipeline = {{"function", std::move(*Pipeline)}};          // 补 function(...)
  else if (isLoopPassName(FirstName, ..., UseMemorySSA))
    Pipeline = {{"function", {{UseMemorySSA ? "loop-mssa" : "loop",
                               std::move(*Pipeline)}}}};       // 补 function(loop(...))
  else if (isMachineFunctionPassName(...))
    Pipeline = {{"function", {{"machine-function", std::move(*Pipeline)}}}};
  else { ... return "unknown pass name"; }
}
return parseModulePassPipeline(MPM, *Pipeline);
```

> [`lib/Passes/PassBuilder.cpp:2711-2759`](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/lib/Passes/PassBuilder.cpp#L2711-L2759) — 这就是为什么 `-passes="instcombine,sroa"`（直接写函数级 pass）也能跑通：它被自动等价改写成 `-passes="function(instcombine,sroa)"`。该写法的文档见 [`include/llvm/Passes/PassBuilder.h:331-349`](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/include/llvm/Passes/PassBuilder.h#L331-L349)。

判定「一个名字属于哪一层」靠 `isModulePassName` 等模板函数。`function` 被显式认定为 module 层关键字（因为 module 层可以嵌套一个 function 管理器）：

```cpp
if (Name == "module")  return true;
if (Name == "cgscc")   return true;
if (NameNoBracket == "function") return true;   // 兼容 function<eager-inv>
if (Name == "coro-cond") return true;
// 再展开 PassRegistry.def 里的 MODULE_PASS / MODULE_PASS_WITH_PARAMS ...
```

> [`lib/Passes/PassBuilder.cpp:1870-1895`](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/lib/Passes/PassBuilder.cpp#L1870-L1895) — `NameNoBracket` 用 `take_until('<')` 去掉 `<...>` 参数后缀，因此 `function<eager-inv>` 也能识别。

最后看「逐层分派」的核心 `parseModulePass`。它先处理带内层流水线的关键字（`module`/`cgscc`/`function(...)`），其中 `function(...)` 由 `parseFunctionPipelineName` 识别，并打包成 `createModuleToFunctionPassAdaptor`——这正是 u3-l1 讲的「跨层 adaptor」：

```cpp
if (auto Params = parseFunctionPipelineName(Name)) {     // 识别 "function" / "function<...>"
  FunctionPassManager FPM;
  if (auto Err = parseFunctionPassPipeline(FPM, InnerPipeline)) return Err;
  MPM.addPass(createModuleToFunctionPassAdaptor(std::move(FPM), Params->first));
  return Error::success();
}
```

> [`lib/Passes/PassBuilder.cpp:2075-2125`](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/lib/Passes/PassBuilder.cpp#L2075-L2125) — 当 `InnerPipeline` 为空（即一个普通 pass 名），则走到函数末尾用 `#include "PassRegistry.def"` 展开内置 pass（见 4.1 末与 4.2 详述）。`parseFunctionPipelineName` 本身解析可选参数 `<eager-inv;no-rerun>`，见 [`lib/Passes/PassBuilder.cpp:779-798`](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/lib/Passes/PassBuilder.cpp#L779-L798)。

#### 4.1.4 代码实践

**目标**：亲手验证「自动包装」和 `function(...)` 的解析分支。

1. 准备一段带冗余计算的最小 IR（保存为 `t.ll`）：

   ```llvm
   define i32 @f(i32 %x) {
     %a = add i32 %x, 0        ; 加 0，冗余
     %b = mul i32 %a, 1        ; 乘 1，冗余
     ret i32 %b
   }
   ```

2. 用**便捷写法**（直接列函数级 pass，不带 `function(...)`）：

   ```bash
   opt -S -passes='instcombine' t.ll -o -
   ```

   预期 `%a`、`%b` 被折叠，函数体只剩 `ret i32 %x`。这验证了 4.1.2 的自动包装：`instcombine` 被 `parsePassPipeline` 自动包成 `function(instcombine)`。

3. 用**显式写法**（带 `function(...)`）得到完全一样的结果：

   ```bash
   opt -S -passes='function(instcombine)' t.ll -o -
   ```

4. **源码定位**：在 [`lib/Passes/PassBuilder.cpp:2075`](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/lib/Passes/PassBuilder.cpp#L2075) 的 `parseModulePass` 中找到 4.1.3 引用的 `parseFunctionPipelineName` 分支（约 2103 行），这就是 `-passes='function(...)'` 在 module 层命中的解析分支；其中 `instcombine` 作为内层名字，会进一步进入 [`parseFunctionPass`](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/lib/Passes/PassBuilder.cpp#L2364-L2493) 通过 `PassRegistry.def` 的 `FUNCTION_PASS` 宏匹配。

**需要观察的现象**：步骤 2 与步骤 3 输出完全一致 → 自动包装生效；删掉 `-S` 只保留 `-o out.bc` 也能跑 → 解析与输出格式无关。

> 待本地验证：若你的构建未带 assertions，行为相同；上述命令需要本地已编译 `opt`（参见 u1-l2、u1-l3）。

#### 4.1.5 小练习与答案

**练习 1**：下列三段 `-passes=` 文本，哪些等价？

- (a) `instcombine,sroa`
- (b) `function(instcombine,sroa)`
- (c) `function(instcombine),function(sroa)`

> **答案**：(a) 与 (b) 完全等价（自动包装）。(a)/(b) 与 (c) **通常不等价**：(a)/(b) 把两个 pass 放进**同一个** `FunctionPassManager`（共用一份分析缓存），(c) 创建**两个**独立的 function 管理器，中间会经历一次管理器边界，分析缓存行为不同。

**练习 2**：为什么 `parsePipelineText` 要在遇到 `)` 时用 `do { ... } while (Text.consume_front(")"))` 「贪婪消费」连续右括号？

> **答案**：避免在 `...))` 之间产生空名字节点。若不贪婪消费，连续右括号会被误解析出一个 `Name=""` 的空元素。贪婪地把多个 `)` 一次性弹栈，能保证每个 `PipelineElement` 都有非空名字（除非是合法的空内层）。

---

### 4.2 默认优化流水线（O0 / O1 / O2 / O3）

#### 4.2.1 概念说明

上一节讲的是「用户手写流水线文本」。但绝大多数用户不会逐个 pass 地拼流水线，而是直接写 `-O2`。`-O2` 本质上是一套**官方预置的、按经验调优过的 pass 序列**，称为「默认优化流水线」（default pipeline）。

这里有个漂亮的统一设计：**`-O2` 在 opt 内部就被改写成 `-passes="default<O2>"`**。也就是说，「优化级别」和「手写流水线」走的是**同一条 `parsePassPipeline` 管道**，`default<O2>` 只是一个特殊的、带参数的 pass 名字，它在 `PassRegistry.def` 里注册，参数 `<O2>` 表示优化级别。这样做的好处是：用户可以用 `default<O2>,my-pass` 在默认流水线后面追加自己的 pass，无需另设机制。

> 名词解释：
> - **优化级别（OptimizationLevel）**：枚举 `O0/O1/O2/O3`，决定开启哪些 pass、激进度如何。`O0` 几乎不优化，`O3` 最激进。
> - **默认流水线（default pipeline）**：`PassBuilder` 内置的、对应各优化级别的 pass 序列。
> - **`default<O#>`**：流水线文本里的「占位 pass」，解析时被展开成对应级别的默认流水线。

#### 4.2.2 核心流程

`-O2` 从命令行到运行的链路：

```
opt -O2 t.ll
   │
   ├─[optdriver.cpp]  把 -O2 映射成字符串 "default<O2>"
   │
   ├─[NewPMDriver.cpp] PB.parsePassPipeline(MPM, "default<O2>")
   │
   ├─[PassBuilder.cpp] parsePipelineText → [{Name:"default<O2>"}]
   │      └─ parseModulePass → 命中 PassRegistry.def 的 MODULE_PASS_WITH_PARAMS("default",...)
   │             └─ parseOptLevelParam("O2") → OptimizationLevel::O2
   │             └─ 展开动作：buildPerModuleDefaultPipeline(O2)
   │
   └─[PassBuilderPipelines.cpp] buildPerModuleDefaultPipeline 拼装 MPM 并返回
```

`buildPerModuleDefaultPipeline` 的宏观构成（O1/O2/O3 共用一套骨架，靠 `OptimizationLevel` 调节细节）：

1. `O0` 直接走 `buildO0DefaultPipeline`（只跑语义必需的 pass）。
2. 否则：先做一些 module 级准备（移除 MemProf 元数据、注解转 metadata、强制函数属性）。
3. **触发 PipelineStart 扩展点回调**（4.3 详述）。
4. 拼入 **`buildModuleSimplificationPipeline`**：模块级清理 + 函数级简化（含 SROA、InstCombine、早期内联等）。
5. 拼入 **`buildModuleOptimizationPipeline`**：主优化（含 CGSCC 内联、循环优化、向量化等）。
6. 收尾：注解 remarks、LTO pre-link 必需 pass 等。

#### 4.2.3 源码精读

**第一步：opt 把 `-O2` 映射成 `default<O2>`**。这是 opt 薄壳逻辑里很简单的一段：

```cpp
std::string Pipeline = PassPipeline;
if (OptLevelO0) Pipeline = "default<O0>";
if (OptLevelO1) Pipeline = "default<O1>";
if (OptLevelO2) Pipeline = "default<O2>";
if (OptLevelO3) Pipeline = "default<O3>";
```

> [`tools/opt/optdriver.cpp:776-789`](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/tools/opt/optdriver.cpp#L776-L789) — 注意上面这段也对 `-Os`/`-Oz` 设了 `default<Os>`/`default<Oz>`，但解析阶段（见下文 `parseOptLevel`）已把 `Os`/`Oz` 视为不再支持，会报致命错误并建议改用 `O2 + optsize/minsize` 属性；本讲聚焦 `O0`–`O3`。这段代码同时禁止 `-O#` 与 `--passes` 同时出现，提示用 `-passes='default<O#>,...'` 组合。

随后 opt 调用 `runPassPipeline`，它内部调用 `PB.parsePassPipeline`：

> [`tools/opt/NewPMDriver.cpp:250`](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/tools/opt/NewPMDriver.cpp#L250) — 把字符串交给 `PassBuilder`。至此 opt 的工作结束，后面全是 `PassBuilder` 的逻辑。

**第二步：`default<O2>` 在 `PassRegistry.def` 里注册**。它是一个带参数的 module pass：

```cpp
MODULE_PASS_WITH_PARAMS(
    "default", "", [&](OptimizationLevel L) {
      setupOptionsForPipelineAlias(PTO, L);
      return buildPerModuleDefaultPipeline(L);
    },
    parseOptLevelParam, "O0;O1;O2;O3")
```

> [`lib/Passes/PassRegistry.def:272-277`](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/lib/Passes/PassRegistry.def#L272-L277) — 解读四元组：名字 `default`、类名占位为空、构造 lambda（接收 `OptimizationLevel`，调用 `buildPerModuleDefaultPipeline`）、参数解析器 `parseOptLevelParam`、合法参数 `O0;O1;O2;O3`。紧随其后的 `thinlto`/`lto`/`thinlto-pre-link`/`lto-pre-link`/`fatlto-pre-link` 用同样方式注册，所以 `default<O2>`、`thinlto<O2>`、`lto<O3>` 都是同一套机制。

`parseOptLevelParam` 把字符串 `O2` 变成枚举：

```cpp
return StringSwitch<std::optional<OptimizationLevel>>(S)
    .Case("O0", OptimizationLevel::O0)
    .Case("O1", OptimizationLevel::O1)
    .Case("O2", OptimizationLevel::O2)
    .Case("O3", OptimizationLevel::O3)
    .Default(std::nullopt);
```

> [`lib/Passes/PassBuilder.cpp:560-575`](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/lib/Passes/PassBuilder.cpp#L560-L575) — 非法级别返回 `StringError`。上文提到的 `Os`/`Oz` 在此之前的 `parseOptLevel`（约 555-559 行）会被拦截并报「不再支持」。

`PassRegistry.def` 的展开发生在 `parseModulePass` 的 `MODULE_PASS_WITH_PARAMS` 宏里：当 `Name == "default<O2>"`，`checkParametrizedPassName` 匹配前缀 `default`，再用 `parsePassParameters(parseOptLevelParam, ...)` 取出 `O2`，调用上面的 lambda 构造 pass（这里「pass」其实是一整条流水线）：

> [`lib/Passes/PassBuilder.cpp:2133-2140`](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/lib/Passes/PassBuilder.cpp#L2133-L2140) — `MODULE_PASS_WITH_PARAMS` 宏的展开处。这就是 `-passes="default<O2>"` 在解析阶段最终命中的代码分支。

**第三步：`buildPerModuleDefaultPipeline` 拼装真正的流水线**：

```cpp
ModulePassManager PassBuilder::buildPerModuleDefaultPipeline(OptimizationLevel Level, ...) {
  if (Level == OptimizationLevel::O0)
    return buildO0DefaultPipeline(Level, Phase);

  ModulePassManager MPM;
  ...
  MPM.addPass(MemProfRemoveInfo());
  MPM.addPass(Annotation2MetadataPass());
  MPM.addPass(ForceFunctionAttrsPass());
  ...
  invokePipelineStartEPCallbacks(MPM, Level);            // ← 扩展点（见 4.3）
  MPM.addPass(buildModuleSimplificationPipeline(Level, Phase));  // 模块简化
  MPM.addPass(buildModuleOptimizationPipeline(Level, Phase));    // 主优化
  ...
  return MPM;
}
```

> [`lib/Passes/PassBuilderPipelines.cpp:1755-1805`](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/lib/Passes/PassBuilderPipelines.cpp#L1755-L1805) — 这就是 `-O2` 的「总配方」。其中两个子流水线：
> - [`buildModuleSimplificationPipeline`](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/lib/Passes/PassBuilderPipelines.cpp#L1115)（1115 行起）：模块级清理 + 经 `createModuleToFunctionPassAdaptor` 套一层 [`buildFunctionSimplificationPipeline`](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/lib/Passes/PassBuilderPipelines.cpp#L622)（含 SROA、InstCombine 等，具体 pass 留待 u4 各讲细讲）。
> - [`buildModuleOptimizationPipeline`](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/lib/Passes/PassBuilderPipelines.cpp#L1510)（1510 行起）：主优化，含 CGSCC 内联、循环、向量化等。

> 注意：`O1`/`O2`/`O3` 共用同一个 `buildPerModuleDefaultPipeline`，区别由 `Level` 在子流水线内部驱动（例如 [`setupOptionsForPipelineAlias`](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/lib/Passes/PassBuilder.cpp#L2069-L2073) 在 `O2` 及以上开启循环/SLP 向量化）。`O0` 走单独的极简流水线。

#### 4.2.4 代码实践

**目标**：验证 `-O2` 与 `default<O2>` 等价，并对比不同级别的输出。

1. 用 4.1.4 的 `t.ll`，分别跑三种写法并 diff：

   ```bash
   opt -S -O2                 t.ll -o a.ll
   opt -S -passes='default<O2>' t.ll -o b.ll
   diff a.ll b.ll             # 预期：无差异
   ```

2. 对比 `O0` 与 `O3` 的函数体差异，直观感受「优化级别」对流水线的影响：

   ```bash
   opt -S -O0 t.ll -o o0.ll
   opt -S -O3 t.ll -o o3.ll
   ```

   预期：`o0.ll` 几乎保留原始指令；`o3.ll` 经过折叠/简化后更短（本例因输入极简，差异主要在属性与元数据，可用更复杂的 IR 观察 pass 数量差别）。

3. **混合默认流水线与自定义 pass**（体现「统一管道」设计）：

   ```bash
   opt -S -passes='default<O2>,function(print<instcount>)' t.ll -o -
   ```

   预期：先跑完整 `-O2`，再在每个函数上打印指令计数统计。这证明 `default<O2>` 只是一个「占位 pass」，可以和别的 pass 自由串联。

**需要观察的现象**：步骤 1 的 `diff` 为空 → `-O2` 与 `default<O2>` 同构；步骤 3 能正常输出统计 → 默认流水线可被嵌入更大流水线。

> 待本地验证：`print<instcount>` 需内置，统计输出到 stderr。

#### 4.2.5 小练习与答案

**练习 1**：既然 `-O2` 会被改写成 `default<O2>`，那 `default<O2>` 在解析时被当作「一个 pass」还是「一串 pass」？

> **答案**：在 `parseModulePass` 里它命中 `MODULE_PASS_WITH_PARAMS("default",...)`，被当作**一个** module pass 加入 `MPM`；但这个 pass 的「构造动作」是调用 `buildPerModuleDefaultPipeline`，返回的是一个**已经装满 pass 的 `ModulePassManager`**，再用 `MPM.addPass(std::move(...))` 整体嵌套进去。所以从结构上是「一个嵌套子管理器」，从效果上是「一串 pass」。

**练习 2**：为什么 LLVM 要让 `-O2` 和手写流水线复用同一条 `parsePassPipeline` 通道，而不是给 `-O2` 单独写一条装配路径？

> **答案**：统一通道带来三点好处——(1) 用户可用 `default<O2>,my-pass` 在官方流水线上叠加自定义 pass，无需新机制；(2) EP 扩展点回调（4.3）对默认流水线和「含 default 的自定义流水线」一视同仁地生效；(3) 解析、校验、报错逻辑只维护一份，减少分歧。

---

### 4.3 扩展点（EP）回调与默认流水线的注入

#### 4.3.1 概念说明

新 PM 提供了**两种**让外部代码（插件、前端、target）介入流水线的方式，初学者很容易混淆，本节专门把它们区分清楚：

| 机制 | 何时触发 | 解决什么问题 | 注册函数 |
| --- | --- | --- | --- |
| **流水线解析回调** | 用户在 `-passes=` 里**显式写出**某 pass 名时 | 给自定义 pass 一个名字，让 `-passes=my-pass` 能解析 | `registerPipelineParsingCallback` |
| **扩展点回调（EP）** | 运行**默认流水线**（`default<O#>`）到固定位置时 | 在官方流水线的固定时机**自动注入** pass，无需用户改 `-passes` | `registerPipelineStartEPCallback` 等 |

u3-l2 已经讲过第一种（`registerPipelineParsingCallback`，让插件 pass 拥有名字）。本节聚焦第二种——**扩展点（Extension Point, EP）**。

EP 的存在动机：很多场景下，我们希望某个 pass **总是**在 `-O2` 的某个固定阶段运行，而不依赖用户记得在 `-passes=` 里写出它。例如 sanitizer 插件希望在流水线**最开头**插桩，target 后端希望在**向量化之前**注入特定 pass。LLVM 在默认流水线里预留了一组「钩子位置」，外部代码用 `register...EPCallback` 注册一个回调，`PassBuilder` 在拼装默认流水线走到该位置时就会调用它，把回调里 `addPass` 的 pass 插进去。

常见的 EP（按位置粗略排序）：

| EP | 注入位置 | 操作的 PassManager |
| --- | --- | --- |
| `PipelineStartEP` | 默认流水线最开头 | Module |
| `PipelineEarlySimplificationEP` | 早期简化之后 | Module |
| `OptimizerEarlyEP` | 主优化之前 | Module |
| `VectorizerStartEP` / `VectorizerEndEP` | 向量化前后 | Function |
| `OptimizerLastEP` | 主优化最末尾 | Module |
| `FullLinkTimeOptimizationEarlyEP` / `...LastEP` | 全量 LTO 流水线首尾 | Module |

#### 4.3.2 核心流程

EP 的工作模式是「**注册回调 → 默认流水线拼装时触发**」：

```
插件加载时：
  PB.registerPipelineStartEPCallback([](MPM, Level){ MPM.addPass(MyPass()); })

用户运行 opt -O2：
  parsePassPipeline("default<O2>")
    └─ buildPerModuleDefaultPipeline(O2)
          └─ ... 准备 pass ...
          └─ invokePipelineStartEPCallbacks(MPM, O2)   ← 遍历回调列表，逐个调用
                └─ 你的 lambda 被调用 → MPM 里多了一个 MyPass
          └─ buildModuleSimplificationPipeline(...)
          └─ buildModuleOptimizationPipeline(...)
```

要点：

- EP 回调**只在默认流水线里触发**。如果你写 `-passes='instcombine,sroa'`（不含 `default<>`），`PipelineStartEP` 等**不会**被调用，因为根本没有走 `buildPerModuleDefaultPipeline`。
- 一个 EP 可以注册**多个**回调，按注册顺序依次执行。
- EP 回调签名带 `OptimizationLevel`（有的还带 `ThinOrFullLTOPhase`），让插件能按级别决定是否注入。

#### 4.3.3 源码精读

EP 回调的**声明与存储**在 `PassBuilder.h`，每个 EP 对应一个 `std::vector` 成员和一个 `register...` 函数：

```cpp
void registerPipelineStartEPCallback(
    const std::function<void(ModulePassManager &, OptimizationLevel)> &C) {
  PipelineStartEPCallbacks.push_back(C);
}
...
void registerOptimizerLastEPCallback(
    const std::function<void(ModulePassManager &, OptimizationLevel,
                             ThinOrFullLTOPhase)> &C) {
  OptimizerLastEPCallbacks.push_back(C);
}
```

> [`include/llvm/Passes/PassBuilder.h:501-534`](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/include/llvm/Passes/PassBuilder.h#L501-L534) — 注意 `OptimizerLastEP` 的回调签名比 `PipelineStartEP` 多一个 `ThinOrFullLTOPhase` 参数，因为「优化末尾」要区分是否处于 LTO 阶段。完整 EP 列表见该文件 480–552 行（含 Vectorizer、FullLinkTimeOptimization 等）。

作为对照，**流水线解析回调**长这样（u3-l2 已用），它操作的是「名字」而非「时机」：

```cpp
void registerPipelineParsingCallback(
    const std::function<bool(StringRef Name, FunctionPassManager &,
                             ArrayRef<PipelineElement>)> &C) {
  FunctionPipelineParsingCallbacks.push_back(C);
}
```

> [`include/llvm/Passes/PassBuilder.h:590-614`](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/include/llvm/Passes/PassBuilder.h#L590-L614) — 五个重载分别对应 CGSCC/Function/Loop/Module/MachineFunction 五层。返回 `bool`：回调若识别该名字返回 `true`，`parseXxxPass` 就认为解析成功。

EP 回调的**触发**（`invoke...`）在 `PassBuilderPipelines.cpp`，实现极简——遍历列表逐个调用：

```cpp
void PassBuilder::invokePipelineStartEPCallbacks(ModulePassManager &MPM,
                                                 OptimizationLevel Level) {
  for (auto &C : PipelineStartEPCallbacks)
    C(MPM, Level);
}
```

> [`lib/Passes/PassBuilderPipelines.cpp:413-422`](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/lib/Passes/PassBuilderPipelines.cpp#L413-L422) — 所有 EP 的 `invoke...` 都是同样的「遍历回调列表」模式。

而**调用点**就嵌在默认流水线拼装函数里。`buildPerModuleDefaultPipeline` 在准备 pass 之后、`buildModuleSimplificationPipeline` 之前触发 `PipelineStartEP`：

```cpp
// Apply module pipeline start EP callback.
invokePipelineStartEPCallbacks(MPM, Level);
// Add the core simplification pipeline.
MPM.addPass(buildModuleSimplificationPipeline(Level, Phase));
// Now add the optimization pipeline.
MPM.addPass(buildModuleOptimizationPipeline(Level, Phase));
```

> [`lib/Passes/PassBuilderPipelines.cpp:1784-1791`](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/lib/Passes/PassBuilderPipelines.cpp#L1784-L1791) — `invokePipelineStartEPCallbacks` 的位置决定了「PipelineStart」注入的 pass 会跑在简化/优化之前。其他 EP 的调用点散布在各 `build...` 函数中，例如 `invokeOptimizerLastEPCallbacks` 在 [`PassBuilderPipelines.cpp:1687`](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/lib/Passes/PassBuilderPipelines.cpp#L1687) 与 [`2506`](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/lib/Passes/PassBuilderPipelines.cpp#L2506)，`invokeVectorizerStartEPCallbacks` 在 [`1606`](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/lib/Passes/PassBuilderPipelines.cpp#L1606)。**找 EP 注入点的诀窍**：在 `PassBuilderPipelines.cpp` 里搜索 `invoke...EPCallbacks`，每一处调用就是该 EP 在默认流水线里的精确坐标。

把整条链路串起来（结合 4.2）：`-O2` → `default<O2>` → `buildPerModuleDefaultPipeline` → 走到 `invokePipelineStartEPCallbacks` → 你注册的 EP 回调被调用 → 你的 pass 被加入 `MPM`。整个过程中用户无需在 `-passes=` 写任何额外内容。

#### 4.3.4 代码实践

**目标**：用 opt 自带的 `-passes-ep-*` 命令行选项（它们内部就是 EP 回调）直观感受「在默认流水线固定位置注入 pass」，并定位对应 EP 的调用点。

opt 的 `NewPMDriver` 把一组 `*-ep-pipeline` 命令行选项转成 EP 回调（参见 [`tools/opt/NewPMDriver.cpp:259-346`](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/tools/opt/NewPMDriver.cpp#L259-L346)）。其中 `passes-ep-pipeline-start` 对应 `PipelineStartEP`：

1. 对比「不加 EP」与「加 EP」：

   ```bash
   # 基线：默认 O2
   opt -S -O2 t.ll -o base.ll

   # 在流水线开头注入一个 instcombine
   opt -S -O2 -passes-ep-pipeline-start='instcombine' t.ll -o ep.ll

   diff base.ll ep.ll
   ```

   预期：本例输入极简可能无可见差异，但 `-passes-ep-pipeline-start` 选项被接受、不报错，说明 EP 注入路径生效。可换更复杂的 IR（含循环的函数）观察差异。

2. **源码定位**：

   - opt 把该选项注册成 EP 回调的位置：[`tools/opt/NewPMDriver.cpp:308-311`](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/tools/opt/NewPMDriver.cpp#L308-L311)（`PipelineStartEPPipeline` → `PB.registerPipelineStartEPCallback`）。
   - 该 EP 在默认流水线的触发点：[`PassBuilderPipelines.cpp:1785`](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/lib/Passes/PassBuilderPipelines.cpp#L1785)。

3. **对比 EP 与解析回调的差异**：EP 注入的 pass **不会**在你写 `-passes='instcombine'`（不含 `default<>`）时运行——因为后者不经过 `buildPerModuleDefaultPipeline`。你可以验证：

   ```bash
   opt -S -passes='instcombine' -passes-ep-pipeline-start='sroa' t.ll -o -
   ```

   观察 `sroa` 是否被额外触发（提示：它不会，因为没有走默认流水线，`PipelineStartEP` 不触发）。

**需要观察的现象**：步骤 1 选项被接受；步骤 3 验证「EP 仅在默认流水线里生效」这一关键性质。

> 待本地验证：`-passes-ep-*` 是 opt 的隐藏选项（`cl::Hidden`），`-help-hidden` 可见；行为依赖本地构建。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `registerPipelineParsingCallback` 的回调签名里有 `ArrayRef<PipelineElement>`，而 `registerPipelineStartEPCallback` 没有？

> **答案**：解析回调处理的是「用户写出的某个 pass 名」，该 pass 可能带内层流水线（如 `my-pass(loop(...))`），所以要把 `InnerPipeline` 作为 `ArrayRef<PipelineElement>` 传给回调以便它进一步解析。EP 回调处理的是「在固定时机注入 pass」，注入什么完全由回调代码决定，与用户写的 `-passes=` 文本无关，因此只需要 `(PassManager&, OptimizationLevel[, Phase])`。

**练习 2**：假设你写了一个 sanitizer 插件，希望它对**所有** `-O1`/`-O2`/`-O3` 编译都自动生效，但用户用 `-passes=` 手拼流水线时不强制运行。应该选哪种机制？

> **答案**：选 `registerPipelineStartEPCallback`（或更靠后的合适 EP）。因为 EP 只在默认流水线（`default<O#>`）里触发，恰好满足「`-O#` 时生效、手拼流水线时不强制」的需求。若用 `registerPipelineParsingCallback`，则必须用户显式写出该 pass 名才会运行，达不到「自动生效」的效果。

---

## 5. 综合实践

把本讲三个模块串起来，完成一次「**追一条完整链路**」的源码阅读实践，并辅以命令行验证。

**背景任务**：解释 `opt -O2 t.ll` 是如何一步步变成可运行的 pass 序列的，并在源码里标注每一跳的位置。

**操作步骤**：

1. **准备一个稍复杂的 IR**（含循环，便于观察默认流水线的循环优化），例如：

   ```llvm
   define i32 @sum(i32 %n) {
   entry:
     br label %loop
   loop:
     %i = phi i32 [0, %entry], [%next, %loop]
     %acc = phi i32 [0, %entry], [%new, %loop]
     %next = add i32 %i, 1
     %new = add i32 %acc, %i
     %cmp = icmp slt i32 %next, %n
     br i1 %cmp, label %loop, label %exit
   exit:
     ret i32 %new
   }
   ```

   存为 `sum.ll`。

2. **追链路并填表**（不写代码，只读源码 + 命令验证）：

   | 阶段 | 发生位置（文件:行） | 你的验证命令 |
   | --- | --- | --- |
   | `-O2` → `"default<O2>"` | `tools/opt/optdriver.cpp:782-783` | `opt -O2 sum.ll -o /dev/null` 能跑 |
   | 字符串 → 树 | `lib/Passes/PassBuilder.cpp:2013` (`parsePipelineText`) | — |
   | `default<O2>` 命中注册表 | `lib/Passes/PassRegistry.def:272` | — |
   | 解析级别参数 | `lib/Passes/PassBuilder.cpp:568` (`parseOptLevelParam`) | — |
   | 拼装默认流水线 | `lib/Passes/PassBuilderPipelines.cpp:1755` | `opt -S -O2 sum.ll -o out.ll` 看循环是否被优化 |
   | 触发 PipelineStart EP | `lib/Passes/PassBuilderPipelines.cpp:1785` | `opt -O2 -passes-ep-pipeline-start='instcombine' sum.ll` |

3. **对比手拼流水线**：用 `-passes='loop-unroll'` 单独跑循环展开，对比 `-O3`（默认会做循环优化）的输出，体会「单 pass」与「默认流水线」的差异：

   ```bash
   opt -S -passes='loop-unroll' sum.ll -o unroll.ll
   opt -S -O3                     sum.ll -o o3.ll
   ```

4. **小结**：用一段话写出「`-O2` 与 `default<O2>` 的等价性体现在哪一行代码」「EP 注入发生在哪一行」。如果你能在不看讲义的情况下复述这张表，说明你已掌握本讲。

> 待本地验证：循环相关优化的具体输出依赖 target 与编译选项；本实践重在「读懂链路 + 命令可运行」，而非比对具体指令。

## 6. 本讲小结

- **流水线文本 = 嵌套括号树**：`parsePipelineText` 用「遇到 `(` 压栈、`)` 弹栈」把 `-passes=...` 字符串切成 `PipelineElement` 树；括号表达 IR 单元的嵌套关系，逗号表达同级顺序。
- **自动包装**：`parsePassPipeline(ModulePassManager&, ...)` 检测首个 pass 名所属层级，若不是 module 层，自动补 `function(...)`/`cgscc(...)`/`function(loop(...))` 外壳，于是 `-passes='instcombine'` 与 `-passes='function(instcombine)'` 等价。
- **逐层分派**：`parseModulePass`/`parseFunctionPass` 先处理 `module`/`cgscc`/`function`/`loop` 等关键字（跨层靠 adaptor），再用 `PassRegistry.def` 的 X-Macro 宏匹配内置 pass，最后询问用户注册的解析回调。
- **`-O2` ≡ `default<O2>`**：opt 在 `optdriver.cpp` 把 `-O2` 改写成 `default<O2>`；`default` 在 `PassRegistry.def` 注册为带参 module pass，参数 `<O2>` 经 `parseOptLevelParam` 解析后调用 `buildPerModuleDefaultPipeline`，把一整条官方流水线作为一个嵌套 `ModulePassManager` 装入。
- **默认流水线骨架**：`buildPerModuleDefaultPipeline` = 准备 pass → 触发 `PipelineStartEP` → `buildModuleSimplificationPipeline` → `buildModuleOptimizationPipeline` → 收尾；`O0` 走单独极简流水线，`O1`/`O2`/`O3` 共用骨架、靠 `OptimizationLevel` 调节。
- **两类扩展机制要分清**：`registerPipelineParsingCallback` 给自定义 pass「起名字」（用户须显式写出，u3-l2）；EP 回调（`registerPipelineStartEPCallback` 等）在**默认流水线的固定位置**自动注入 pass（用户无需改 `-passes=`），其触发点就是 `PassBuilderPipelines.cpp` 里各 `invoke...EPCallbacks` 的调用处。

## 7. 下一步学习建议

本讲讲清了「pass 如何被串成流水线」。接下来：

- **进入具体优化**：u4 各讲将拆开默认流水线里的核心 pass——u4-l1（InstCombine/SCCP）、u4-l2（循环与 ScalarEvolution）、u4-l3（内联与 IPO）、u4-l4（别名分析）。阅读时可以回头对照本讲的「默认流水线骨架」，看看这些 pass 各自嵌在 `buildModuleSimplificationPipeline` / `buildModuleOptimizationPipeline` 的哪一段。
- **自己写一个 EP 注入**：基于 u3-l2 的插件骨架，把 `registerPipelineParsingCallback` 换成 `registerPipelineStartEPCallback`，用 `opt -O2 -load-pass-plugin` 验证你的 pass 在 `default<O2>` 里自动运行（而不必在 `-passes=` 写出它）。
- **延伸阅读源码**：通读 [`lib/Passes/PassBuilderPipelines.cpp`](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/lib/Passes/PassBuilderPipelines.cpp) 中 `buildModuleOptimizationPipeline` 的完整拼装，结合 `invoke...EPCallbacks` 的所有调用点，画一张「默认 O2 流水线 + EP 钩子位置」的地图——这是理解 LLVM 优化器全貌最直接的方式。

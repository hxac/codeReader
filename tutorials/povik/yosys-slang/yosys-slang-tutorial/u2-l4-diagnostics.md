# 诊断系统：从 slang Diagnostic 到 Yosys 日志

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清楚一条 sv-elab 自定义诊断（diagnostic）从「被代码触发」到「打印进 Yosys 日志」的完整链路。
- 区分 slang 原生诊断与 sv-elab 自定义诊断，理解为什么 sv-elab 要在 slang 的 `DiagnosticEngine` 上「叠加」一套自己的诊断码。
- 读懂 `DiagnosticIssuer` 这个混入类（mixin）如何让任意对象具备「累积诊断」的能力。
- 掌握 `diag::setup_messages` 如何为每个诊断码登记文案与严重级别（Error/Warning/Note），以及 `DiagGroup` 如何批量设置级别。
- 理解 `check_diagnostics` 与 `test_slangdiag -expect` 联手构成的「负向测试」机制：如何用一段 SV 源码断言「应当产生某条诊断」。

本讲只聚焦诊断系统本身，不展开每一条诊断码的具体语义（那是后续 always/存储器/SVA 各讲的任务）。

## 2. 前置知识

### 2.1 什么是「诊断」

在编译器/综合器语境里，「诊断」泛指工具向用户汇报的一条带位置信息的信息，分为三级：

- **Error（错误）**：无法继续，通常会中止综合。
- **Warning（警告）**：有问题但能继续，结果可能不符合预期。
- **Note（提示）**：补充说明，常附在 Error/Warning 后面指出相关位置。

一条诊断至少包含三要素：**诊断码**（唯一标识这一类问题）、**源码位置**（出问题的那一段 SV 代码）、**文案**（给用户看的话）。

### 2.2 slang 的诊断引擎

slang 库自带一套成熟的诊断系统，核心是 `slang::DiagnosticEngine`。它负责：

- 持有「诊断码 → 文案」「诊断码 → 严重级别」的映射；
- 把 `slang::Diagnostic` 对象格式化成带文件名、行号、源码上下划线的字符串；
- 统计错误数量（`getNumErrors()`）；
- 把格式化后的字符串写到标准输出/错误流。

sv-elab **不另起炉灶**，而是复用 `driver.diagEngine`（`slang::driver::Driver` 自带的引擎实例）来登记和输出自己的诊断码。这正是「从 slang Diagnostic 到 Yosys 日志」那座桥的起点。

### 2.3 Yosys 的日志体系

Yosys 用 `log()`、`log_warning()`、`log_error()` 等函数输出。sv-elab 是 Yosys 插件，最终所有信息都得进 Yosys 的日志通道。后面会看到，sv-elab 用一个 `captureOutput` 适配器把 slang 的输出「劫持」并转发进 `log()`。

### 2.4 前置讲义回顾

本讲承接 [u2-l2](u2-l2-slang-driver-pipeline.md)。那里讲到 `execute()` 的四段骨架，并提到 `diag::setup_messages` 在参数装配阶段初始化诊断、`check_diagnostics` 是测试模式的钩子。本讲把这两块放大讲透。你还需要知道 `NetlistContext` 这个中枢类（[u3-l1](u3-l1-netlist-context.md) 会细讲），这里只用到它的一个身份：它同时「是一个 `DiagnosticIssuer`」。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [src/diag.h](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/diag.h) | 声明 `diag` 命名空间下全部诊断码的 `extern` 变量，以及 `setup_messages` 原型。 |
| [src/diag.cc](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/diag.cc) | 定义每个诊断码的编号、`DiagnosticIssuer` 各方法的实现、两个 `DiagGroup`、以及 `setup_messages` 的全部文案与级别登记。 |
| [src/slang_frontend.h](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.h) | 定义 `DiagnosticIssuer` 混入类，以及 `NetlistContext` 对它的多重继承。 |
| [src/slang_frontend.cc](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc) | `execute()` 里调用 `setup_messages`、累积并上报诊断、调用 `check_diagnostics`；以及 `TestSlangDiagPass`（`test_slangdiag`）测试命令。 |

## 4. 核心概念与源码讲解

### 4.1 DiagnosticIssuer：让任意对象具备「累积诊断」的能力

#### 4.1.1 概念说明

翻译 AST 成 RTLIL 是个庞大过程，会同时遍历成百上千个节点。如果每发现一个问题就立刻 `log_error()`，会有两个麻烦：

1. **顺序混乱**：遍历顺序未必等于源码行号顺序，输出会跳来跳去。
2. **无法批量测试**：测试时希望「收集全部诊断后，再判断有没有预期的那一条」。

sv-elab 的做法是引入一个轻量混入类 `DiagnosticIssuer`：任何继承它的对象都自带一个 `std::vector<Diagnostic> issued_diagnostics` 成员，可以把诊断「先攒着」，等合适时机再一次性排序、上报。

谁继承它？主要是两个「会发现问题」的对象：

- `NetlistContext`（[src/slang_frontend.h:537](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.h#L537)）：网表构建中枢，端口、连线、过程块翻译中遇到的问题都攒在它身上。
- `InferredMemoryDetector`（[src/memory.h:33-36](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/memory.h#L33-L36)）：存储器推断器，单独攒自己发现的存储器相关诊断。

注意：`async_pattern.cc` 里也有局部 `issuer`，它走的是 `TimingPatternInterpretor` 自带的 `DiagnosticIssuer` 基类（时序模式识别的问题先攒着，最后并入 netlist）。

#### 4.1.2 核心流程

`DiagnosticIssuer` 提供的接口可以归纳为两类：**「写入」**与**「读出」**。

```
写入（翻译过程中持续调用）
  add_diag(code, location)        -> 返回 Diagnostic&，调用方可继续 << 填充 { } 占位参数
  add_diag(code, sourceRange)     -> 同上，并自动把这段源码范围标为高亮
  add_diag(Diagnostic)            -> 直接塞入一条已构造好的诊断
  add_diagnostics(diagnostics)    -> 批量塞入一组诊断（来自 slang 等）

读出（翻译结束后一次性处理）
  report_into(engine)             -> 把 issued_diagnostics 逐条交给 slang 引擎 issue
  （成员）issued_diagnostics      -> 外部也可直接读这个 vector
```

「占位参数」是 slang 的约定：诊断文案里写 `{}` 表示「这里等运行时填」。比如文案 `"failed to open file '{}'"` 里的 `{}`，会在触发处用 `diag << filename` 填上真实文件名。

#### 4.1.3 源码精读

`DiagnosticIssuer` 的声明非常精简，全部是公开成员：

[Diag] 类声明 [src/slang_frontend.h:473-486](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.h#L473-L486) — 定义了四个写入方法、一个读出方法 `report_into`，以及公开的 `issued_diagnostics` 容器。

实现集中在 [src/diag.cc:24-52](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/diag.cc#L24-L52)。重点看两个写入重载的区别：

```cpp
// 按位置写入：只记录起始位置
Diagnostic &DiagnosticIssuer::add_diag(DiagCode code, SourceLocation location) {
    issued_diagnostics.emplace_back(code, location);
    return issued_diagnostics.back();
}

// 按源码范围写入：记录起始位置，并把整段范围作为高亮
Diagnostic &DiagnosticIssuer::add_diag(DiagCode code, SourceRange sourceRange) {
    Diagnostic &diag = add_diag(code, sourceRange.start());
    diag << sourceRange;            // 把范围作为一个参数附加
    return diag;
}
```

差别在于「指向一个点」还是「圈住一段」。后者会让最终打印的诊断带上源码上下文并用 `~~~` 高亮那一整段表达式，对用户更友好。所以多数触发点用的是 `expr.sourceRange` 版本（例如 [src/slang_frontend.cc:950](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L950)），少数只知道一个点的用 `symbol.location` 版本（例如 [src/slang_frontend.cc:1811](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L1811)）。

`report_into` 则是把攒下来的诊断倒给 slang 引擎：

```cpp
void DiagnosticIssuer::report_into(DiagnosticEngine &engine) {
    for (auto &diag : issued_diagnostics)
        engine.issue(diag);
}
```

> 说明：`report_into` 是这个类提供的通用「倒出」助手。`execute()` 主流程目前没有直接调用它，而是用了等价的内联逻辑（见 4.3.3）：先排序、去重，再逐条 `engine.issue()`。两者做的是同一件事，`report_into` 更简洁但不排序去重。

#### 4.1.4 代码实践

**实践目标**：确认「累积」与「一次性读出」这对设计，并看清 `add_diag` 返回引用的妙用。

**操作步骤**（源码阅读型）：

1. 打开 [src/diag.cc:24-52](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/diag.cc#L24-L52)，确认四个写入方法都只是把诊断 `push` 进 `issued_diagnostics`，没有任何打印动作——印证「先攒着」。
2. 打开 [src/slang_frontend.cc:950-951](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L950-L951)，观察这种典型用法：
   ```cpp
   auto &diag = netlist.add_diag(diag::ArgumentTypeUnsupported, filename_arg->sourceRange);
   diag << filename_arg->type->toString();
   ```
   `add_diag` 返回 `Diagnostic&`，紧接着 `<<` 把类型名字填进文案 `{}`。
3. 全仓库搜索 `report_into`，确认它只在 `diag.cc` 定义、在头文件声明，主流程没直接调用。

**需要观察的现象**：`add_diag` 调用点遍布十几个文件（slang_frontend.cc、procedural.cc、async_pattern.cc、sva.cc、statements.h、lvalue.cc……），但没有任何一处直接 `log_error`，所有问题都默默进了 vector。

**预期结果**：你会真切看到「累积—读出」的分离——翻译阶段只写不报，报告阶段才统一输出。

**待本地验证**：如果你想看一条真实诊断的完整文本，可以运行（需要已构建的 slang.so）：
```
yosys -m slang.so -p "read_slang <<EOT
module m; initial begin $readmemh(\"no_such_file_anywhere.hex\", m); end endmodule
EOT"
```
（此命令的运行结果待本地验证；预期会打印 4.2 里讲到的 `ReadmemFileNotFound` 文案。）

#### 4.1.5 小练习与答案

**练习 1**：为什么 `add_diag` 要返回 `Diagnostic&` 而不是 `void`？

> **答案**：为了支持链式填充 `{}` 占位参数。调用方拿到引用后可以连续 `diag << arg1 << arg2`，把运行时才确定的值（如变量名、文件名、类型名）塞进固定模板文案，而不必为每种参数组合写一个新函数。

**练习 2**：`NetlistContext` 同时继承 `RTLILBuilder` 和 `DiagnosticIssuer`（[src/slang_frontend.h:537](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.h#L537)），这种多重继承有什么好处？会不会有「两个基类都有同名成员」的冲突风险？

> **答案**：好处是「一个对象兼具两种能力」——既能发 RTLIL 单元（`RTLILBuilder`），又能就地报告问题（`DiagnosticIssuer`），翻译代码里 `netlist.add_diag(...)` 与 `netlist.add_dff(...)` 写起来一样自然。由于两个基类的成员集合完全不重叠（一个管 cell/wire，一个管 diagnostic），不会产生歧义冲突。

---

### 4.2 diag 命名空间与 setup_messages：诊断码的「户口登记」

#### 4.2.1 概念说明

sv-elab 区分两类诊断：

- **slang 原生诊断**：如 `UnknownSystemName`（调用了未知的系统任务 `$foo`），由 slang 自己登记文案与级别。
- **sv-elab 自定义诊断**：如「锁存器未推断」「双沿触发不支持」——这些是 sv-elab 在把 AST 翻译成 RTLIL 时才产生的语义判断，slang 不可能预知。

为了让 slang 引擎能格式化、定级这些自定义诊断，sv-elab 必须为每一个自定义码做「户口登记」：

1. **分配唯一编号**：形如 `DiagCode(DiagSubsystem::Netlist, 1010)`，表示「Netlist 子系统第 1010 号」。所有 sv-elab 自定义码都挂在 `DiagSubsystem::Netlist` 下，编号从 1000 起。
2. **登记文案与级别**：在引擎上调用 `setMessage`（设文案）和 `setSeverity`（设级别）。

这套登记集中在一个函数 `diag::setup_messages` 里，在 `execute()` 早期被调用一次。

#### 4.2.2 核心流程

```
diag.h   : extern 声明每个 DiagCode 变量（供全仓库引用）
diag.cc  : 为每个 DiagCode 变量赋初值 (子系统, 编号)
           定义 DiagGroup「unsynthesizable」「sanity」把若干码归组
setup_messages(engine):
    对每个码:
        engine.setMessage(码, "文案带 {} 占位")
        engine.setSeverity(码, Error | Warning | Note)   // 多数码显式定级
    对 unsynthesizable / sanity 组里每个码:
        engine.setSeverity(码, Error)                      // 少数码靠组批量定级
execute() 早期:
    diag::setup_messages(driver.diagEngine)                // 一次性登记
```

为什么要有 `DiagGroup`？因为 slang 引擎支持按「组名」整体调整级别（用户可用 `-Wno-<group>` 之类选项）。sv-elab 把一组「不可综合」问题归到 `unsynthesizable` 组，既便于统一设为 Error，也把「能否被降级」的控制权暴露给了 slang 的选项机制（不过 sv-elab 又用 `catch_forbidden_options` 禁掉了某些降级，见 u2-l2/u2-l3）。

#### 4.2.3 源码精读

**第一步：诊断码的声明与定义。** [src/diag.h:13-92](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/diag.h#L13-L92) 用 `extern slang::DiagCode XXX;` 声明了近 80 个码，例如：

```cpp
extern slang::DiagCode LatchNotInferred;
extern slang::DiagCode ReadmemFileNotFound;
extern slang::DiagCode BothEdgesUnsupported;
```

对应的定义在 [src/diag.cc:55-138](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/diag.cc#L55-L138)，每个都分配了唯一编号：

```cpp
DiagCode LatchNotInferred(DiagSubsystem::Netlist, 1010);
...
DiagCode ReadmemFileNotFound(DiagSubsystem::Netlist, 1079);
```

（`BothEdgesUnsupported` 在 [src/diag.cc:59](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/diag.cc#L59)，编号 1004。）

**第二步：两个诊断组。** [src/diag.cc:140-143](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/diag.cc#L140-L143)：

```cpp
DiagGroup unsynthesizable("unsynthesizable",
        {IffUnsupported, GenericTimingUnsyn, BothEdgesUnsupported, ExpectingIfElseAload,
                IfElseAloadPolarity, IfElseAloadMismatch, UnsynthesizableFeature});
DiagGroup sanity("sanity", {EdgeImplicitMixing});
```

**第三步：`setup_messages` 登记文案与级别。** 这是本模块的重头戏，[src/diag.cc:145-365](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/diag.cc#L145-L365)。挑三段有代表性的看：

```cpp
// (a) 显式定级为 Error
engine.setMessage(ReadmemFileNotFound, "failed to open file '{}'");
engine.setSeverity(ReadmemFileNotFound, DiagnosticSeverity::Error);

// (b) 显式定级为 Warning
engine.setMessage(LatchNotInferred, "latch not inferred for variable '{}' driven from always_latch procedure");
engine.setSeverity(LatchNotInferred, DiagnosticSeverity::Warning);

// (c) 只设文案、不单独定级，靠 DiagGroup 批量定级
engine.setMessage(UnsynthesizableFeature, "unsynthesizable feature");
// ... 随后：
for (auto code : unsynthesizable.getDiags())
    engine.setSeverity(code, DiagnosticSeverity::Error);   // 把组内全部设为 Error
```

> 第 (c) 条 `UnsynthesizableFeature` 本身只调了 `setMessage`（[src/diag.cc:250](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/diag.cc#L250)），它的 Error 级别来自上面的组循环（[src/diag.cc:188-191](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/diag.cc#L188-L191)）。这是「显式定级」与「组定级」两种风格的对照。

Note 级别常用于「附注」，比如 `NotePreviousAssignment`（"previous assignment here"）会跟在 `BlockingAssignmentAfterNonblocking` 后面，指出之前那条赋值在哪——[src/diag.cc:283-288](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/diag.cc#L283-L288)。

#### 4.2.4 代码实践

**实践目标**：亲手把「声明—定义—登记」三处对上号。

**操作步骤**：

1. 在 [src/diag.h](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/diag.h) 任选三个码，记下名字，例如 `WaitStatementUnsupported`、`MemoryNotInferred`、`PrimTypeUnsupported`。
2. 在 [src/diag.cc:55-138](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/diag.cc#L55-L138) 找到它们的编号（应是 `1011`、`1020`、`1049`）。
3. 在 [src/diag.cc:145-365](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/diag.cc#L145-L365) 找到各自的 `setMessage`/`setSeverity`，记下文案和级别。

**需要观察的现象**：你会看到 `WaitStatementUnsupported` 是 Warning（[src/diag.cc:154-155](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/diag.cc#L154-L155)），`MemoryNotInferred` 与 `PrimTypeUnsupported` 是 Error，文案里 `{}` 的个数等于触发处 `<<` 的次数。

**预期结果**：三处一一对应，编号在头文件不出现、只在 `.cc` 定义处给出，印证「`extern` 声明 + 单点定义」的 C++ 惯例。

#### 4.2.5 小练习与答案

**练习 1**：为什么所有自定义码都用 `DiagSubsystem::Netlist`，而不为「过程块」「存储器」「SVA」各开一个子系统？

> **答案**：这是 sv-elab 的简化选择。`DiagSubsystem` 主要用来给编号分段，sv-elab 把全部翻译期诊断统一放进 `Netlist` 段（编号 1000–1085），靠**编号本身**和**诊断名**来区分语义类别，避免了为每类问题维护独立子系统表。代价是编号必须全局唯一、不能重复（注意 [src/diag.cc:110](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/diag.cc#L110) 与 [:137](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/diag.cc#L137) 都出现了 `1084`，这是个真实存在的重号瑕疵，新增码时要避开）。

**练习 2**：如果将来想新增一条「foo 不支持」的诊断，最少要改哪几个地方？

> **答案**：四处。(1) `diag.h` 加 `extern slang::DiagCode FooUnsupported;`；(2) `diag.cc` 顶部加 `DiagCode FooUnsupported(DiagSubsystem::Netlist, <新编号>);`；(3) `setup_messages` 里加 `setMessage` + `setSeverity`（或把它塞进 `unsynthesizable` 组）；(4) 在翻译代码的对应触发点 `netlist.add_diag(diag::FooUnsupported, range);`。

---

### 4.3 check_diagnostics 与 test_slangdiag：诊断的「上报」与「负向测试」

#### 4.3.1 概念说明

前两模块解决了「攒」和「登记」。本模块解决两件事：

1. **上报**：翻译结束后，把 `issued_diagnostics` 里攒下来的诊断，经 slang 引擎格式化后送进 Yosys 日志。这里有个关键适配：slang 默认往 `stdout/stderr` 写，而 Yosys 要走 `log()`，需要一个输出劫持器。
2. **负向测试**：很多 sv-elab 测试是「喂一段**故意有问题**的 SV，断言它产生了某条特定诊断」。这由 `test_slangdiag -expect "<文案>"` 命令配合 `check_diagnostics` 函数实现。

#### 4.3.2 核心流程

```
execute() 主流程（每个顶层实例的 netlist 翻译完成后）:
    diags = mem_detect.issued_diagnostics + netlist.issued_diagnostics
    diags.sort(sourceManager)                      # 按源码位置排序，输出才不乱
    check_diagnostics(engine, diags, last=false)   # 测试模式：在里面找预期文案
    for d in diags (去重):
        engine.issue(d)                            # 真正交给 slang 引擎
最后:
    check_diagnostics(engine, {}, last=true)       # 收尾：还没找到预期就报错
    driver.reportDiagnostics()                     # slang 引擎统一格式化输出
        └─ 输出被 captureOutput 劫持 -> log()       # 桥到 Yosys 日志
    若 getNumErrors()>0 且非成功失败测试:
        log_error("Design elaboration failed...")
```

测试侧：

```
TestSlangDiagPass (-expect "文案"):
    把文案写进文件级静态变量 expected_diagnostic
随后的 read_slang:
    check_diagnostics 逐条 formatMessage 比较
    命中 -> log("Expected diagnostic ... found")，清空，标记 in_succesful_failtest
    全程没命中且 last==true -> log_error("Expected diagnostic ... but none emitted")
```

#### 4.3.3 源码精读

**(a) 桥到 Yosys 日志：captureOutput。** [src/slang_frontend.cc:3680](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L3680)：

```cpp
auto guard = slang::OS::captureOutput([&](std::string_view text, bool) { log("%s", std::string(text).c_str()); });
```

这一行在 `execute()` 入口建立了一个 RAII 守卫：只要 slang 引擎（包括 `reportDiagnostics`）往标准输出写字符串，就会被这个回调捕获并转发给 Yosys 的 `log()`。这就是「从 slang Diagnostic 到 Yosys 日志」那座桥的物理实现。守卫在 `execute()` 结束时析构，自动恢复原输出。

**(b) 登记与上报。** [src/slang_frontend.cc:3687](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L3687) 调 `diag::setup_messages(driver.diagEngine)` 完成全部登记（4.2 模块）。翻译完成后，在 [src/slang_frontend.cc:3782-3794](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L3782-L3794) 收集并上报：

```cpp
slang::Diagnostics diags;
diags.append_range(populate.mem_detect.issued_diagnostics);   // 存储器推断器的诊断
diags.append_range(netlist.issued_diagnostics);               // 网表翻译的诊断
diags.sort(driver.sourceManager);                             // 按位置排序

if (check_diagnostics(driver.diagEngine, diags, /*last=*/false))
    in_succesful_failtest = true;

for (int i = 0; i < (int) diags.size(); i++) {
    if (i > 0 && diags[i] == diags[i - 1])
        continue;                                             // 去重：相邻完全相同的只报一次
    driver.diagEngine.issue(diags[i]);                        // 交给 slang 引擎
}
```

注意这里**没有**调用 4.1 里的 `report_into`，而是内联实现了「排序 + 去重 + 逐条 issue」——比 `report_into` 多了两步处理（排序让输出对齐源码行号、去重避免同一问题被多处触发而重复刷屏）。

**(c) check_diagnostics 的四种情形。** 注释把语义讲得很清楚，[src/slang_frontend.cc:3650-3672](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L3650-L3672)：

```cpp
// 1. 正常模式：expected_diagnostic 为空，直接返回 false 继续执行
// 2. 测试模式且命中：返回 true，提前以成功退出
// 3a. 测试模式未命中但后面还可能有：返回 false 继续
// 3b. 测试模式最终未命中（last==true）：log_error 退出
bool check_diagnostics(slang::DiagnosticEngine &diagEngine,
                       const slang::SmallVector<slang::Diagnostic> &diags, bool last) {
    if (expected_diagnostic.empty())
        return false;
    for (auto &diag : diags) {
        auto message = diagEngine.formatMessage(diag);        // 用引擎格式化成最终文案
        if (message == expected_diagnostic) {                 // 逐字比较
            log("Expected diagnostic `%s' found\n", expected_diagnostic.c_str());
            expected_diagnostic.clear();
            return true;
        }
    }
    if (last)
        log_error("Expected diagnostic `%s' but none emitted\n", expected_diagnostic.c_str());
    else
        return false;
}
```

关键点：比较的是 **`formatMessage` 之后的完整文案**，而不是诊断码。这就是为什么测试里 `-expect` 后面跟的是一整句人话（如 `"failed to open file 'no_such_file_anywhere.hex'"`），而不是码编号。

**(d) 测试命令 test_slangdiag。** [src/slang_frontend.cc:3892-3920](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L3892-L3920) 注册了 Yosys Pass `test_slangdiag`，它只做一件事——把 `-expect` 参数写进文件级静态变量 `expected_diagnostic`（[src/slang_frontend.cc:3524](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L3524)）：

```cpp
if (args[argidx] == "-expect" && argidx+1 < args.size()) {
    std::string message = args[++argidx];
    if (message.front() == '\"' && message.back() == '\"')
        message = message.substr(1, message.size() - 2);   // 去掉首尾引号
    expected_diagnostic = message;
    ...
}
```

随后紧跟的 `read_slang` 在翻译时，`check_diagnostics` 就会拿这个值去比对。一个真实测试用例 [tests/various/readmem_diag.ys:2](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/tests/various/readmem_diag.ys#L2)：

```
test_slangdiag -expect "failed to open file 'no_such_file_anywhere.hex'"
```

这句正是 4.2 里 `ReadmemFileNotFound` 的文案（`{}` 被填成了具体文件名）。

#### 4.3.4 代码实践（本讲的主实践）

**实践目标**：完整追踪三个诊断码「声明 → 定义 → 登记文案/级别 → 触发点 → 测试断言」五处，亲手走通诊断系统的全链路。这正是讲义规格里要求的核心实践。

**操作步骤**：按下表逐项定位（行号均已核对）。

| 诊断码 | 声明（diag.h） | 定义+编号（diag.cc） | 登记文案/级别（diag.cc） | 触发点（slang_frontend.cc） | 触发场景 | 测试断言 |
|--------|----------------|----------------------|--------------------------|------------------------------|----------|----------|
| `ReadmemFileNotFound` | [:86](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/diag.h#L86) | [:132](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/diag.cc#L132) (1079) | [:344-345](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/diag.cc#L344-L345) Error | [:976](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L976) | `$readmemh`/`$readmemb` 指定的文件打不开 | [tests/various/readmem_diag.ys:2](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/tests/various/readmem_diag.ys#L2) |
| `LatchNotInferred` | [:24](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/diag.h#L24) | [:66](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/diag.cc#L66) (1010) | [:166-167](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/diag.cc#L166-L167) Warning | [:1811](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L1811) | `always_latch` 块里的变量实际没构成锁存器（每条分支都赋值了） | （行为型，可自造） |
| `PrimTypeUnsupported` | [:58](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/diag.h#L58) | [:104](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/diag.cc#L104) (1049) | [:267-268](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/diag.cc#L267-L268) Error | [:2906](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L2906) | 遇到不支持的门级原语类型（如 `tranif1`） | [tests/various/tranif.ys:1](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/tests/various/tranif.ys#L1) |

逐行打开每个链接，确认五处确实串成一条因果链：

1. `ReadmemFileNotFound`：`extern` 声明 → 编号 1079 → 文案 `"failed to open file '{}'"` Error → 在 readmem 处理函数里，文件打开失败时 `netlist.add_diag(...)` → 测试用 `test_slangdiag -expect` 断言这句文案出现。
2. `LatchNotInferred`：注意它的 `add_diag` 用的是 `symbol.location`（一个点）而非 `sourceRange`（一段），且触发条件是 `procedureKind == AlwaysLatch && !cl.empty()`——即「声明成 always_latch 但其实没悬空位」。
3. `PrimTypeUnsupported`：触发处 `netlist.add_diag(diag::PrimTypeUnsupported, sym.location) << type;`，`<< type` 把原语类型名填进文案 `'{}'`，所以测试断言是 `"primitives of type 'tranif1' unsupported"`。

**需要观察的现象**：

- 每个码的编号在 `diag.cc` 定义段唯一（注意避开前面提到的 `1084` 重号瑕疵）。
- 文案里 `{}` 的个数 = 触发处 `<<` 的次数（`ReadmemFileNotFound` 一个、`PrimTypeUnsupported` 一个、`LatchNotInferred` 一个）。
- 测试 `-expect` 的字符串与 `setMessage` 文案填好参数后**逐字相等**。

**预期结果**：你能对着一张表，从任意一个诊断码出发，闭着眼睛找到它的「户口」和「触发现场」，并说出它会以 Error/Warning 哪种级别出现在 Yosys 日志里。

**待本地验证**：运行 `tests/various/readmem_diag.ys` 与 `tests/various/tranif.ys` 对应的等价性/诊断测试，观察日志里是否打印出表中文案（运行方式见 [u8-l1](u8-l1-test-infrastructure.md)；具体命令与输出待本地验证）。

#### 4.3.5 小练习与答案

**练习 1**：`check_diagnostics` 为什么比较 `formatMessage(diag)` 的字符串，而不是直接比较 `diag.code`？

> **答案**：因为同一条诊断码在不同上下文里，`{}` 占位会被填成不同值（如 `ReadmemFileNotFound` 的文件名因测试而异）。比较格式化后的完整文案，能让测试既验证「产生了正确的诊断码」，又验证「填入了正确的参数」，断言更精确。代价是测试对文案措辞敏感——改一个字就得同步更新所有 `-expect`。

**练习 2**：`in_succesful_failtest` 这个变量名暗示了什么？为什么有了错误（`getNumErrors()>0`）却还能算「成功」？

> **答案**：它表示「这是一次**预期内的失败测试**」——测试本意就是喂一段有问题的 SV，期待 sv-elab 报错。只要报出的错误里包含 `-expect` 指定的那条诊断，`check_diagnostics` 就返回 true 并置位 `in_succesful_failtest`，随后即便 `getNumErrors()>0`，[src/slang_frontend.cc:3743-3744](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L3743-L3744) 的 `log_error` 会被跳过，测试以「成功失败」收场。

**练习 3**：`catch_forbidden_options`（[src/slang_frontend.cc:3530-3547](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L3530-L3547)）里用到的 `diag::ForbiddenDemotion` 和 `diag::NoIgnoreUnknownModules` 是经哪条路径上报的？它们也走 `issued_diagnostics` 吗？

> **答案**：不走 `issued_diagnostics`。这两条诊断是用 `engine.issue({...})` 直接交给 slang 引擎的（位置是 `SourceLocation::NoLocation`，因为没有具体源码点）。它们对应的是「命令行/选项层面的违规」（用户试图降级不该降级的错误、或用了已废弃的 `--ignore-unknown-modules`），不属于某个 AST 节点的翻译问题，所以绕过了 `DiagnosticIssuer` 累积机制，直接进引擎。文案见 [src/diag.cc:261-262](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/diag.cc#L261-L262) 与 [:359-360](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/diag.cc#L359-L360)。

## 5. 综合实践

**任务**：为 sv-elab 新增一条**自定义诊断**，并配上一个「负向测试」用例，完整走一遍「登记—触发—断言」全流程。这会把本讲三个模块（`DiagnosticIssuer`、`setup_messages`、`check_diagnostics`）串起来用。

> 提示：本任务会修改源码，请先在本地副本上做，不要污染原仓库；且本任务**仅作学习设计**，不要求真正合并。

设计步骤（不要求运行，重在走通流程）：

1. **选一个触发点**。假设你想在遇到 `force` 语句时报警。在 [src/statements.h](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/statements.h) 里定位处理 `VariableDeassignmentStatement`/`Assign` 之类的分支，找到合适的位置（可参考 [:757](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/statements.h#L757) 处 `LangFeatureUnsupported` 的写法）。
2. **登记户口**。在 [src/diag.h](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/diag.h) 加 `extern slang::DiagCode ForceUnsupported;`；在 [src/diag.cc:55-138](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/diag.cc#L55-L138) 段选一个未用编号（避开 `1084` 重号），写 `DiagCode ForceUnsupported(DiagSubsystem::Netlist, 1090);`；在 `setup_messages` 里写：
   ```cpp
   engine.setMessage(ForceUnsupported, "force statement unsupported");
   engine.setSeverity(ForceUnsupported, DiagnosticSeverity::Error);
   ```
3. **触发**。在选定位置写 `netlist.add_diag(diag::ForceUnsupported, stmt.sourceRange);`（用 `sourceRange` 版本，让输出带高亮）。
4. **写负向测试**。仿照 [tests/various/tranif.ys](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/tests/various/tranif.ys)，新建一个 `.ys`：
   ```
   test_slangdiag -expect "force statement unsupported"
   read_slang <<EOT
   module m; wire a; initial force a = 1; endmodule
   EOT
   ```
5. **自检**：对照 4.3.4 的五列表格，确认你的新码五个落点齐全；确认 `-expect` 字符串与 `setMessage` 文案逐字一致（没有 `{}` 时无需填参数）。

**预期结果**（设计层面）：你能在不读后续讲义的前提下，独立把一条诊断从无到有接进系统，并解释它最终如何经由 `captureOutput` 出现在 Yosys 日志里、如何被 `check_diagnostics` 在测试中捕获。运行结果待本地验证。

## 6. 本讲小结

- sv-elab 复用 slang 的 `DiagnosticEngine` 来承载自己的诊断，不另造轮子；所有自定义码挂在 `DiagSubsystem::Netlist` 下，编号 1000 起。
- `DiagnosticIssuer` 是一个混入类，提供 `add_diag`（写入，返回引用以链式填 `{}`）、`report_into`（读出）；`NetlistContext` 和 `InferredMemoryDetector` 都继承它，实现「翻译期先攒、报告期再报」。
- `diag::setup_messages` 集中为每个码登记文案与严重级别；多数码显式 `setSeverity`，少数靠 `DiagGroup`（`unsynthesizable`/`sanity`）批量定级。
- 上报时 `execute()` 内联做了「排序 + 去重 + 逐条 `engine.issue`」，比 `report_into` 更完善；slang 引擎的输出经 `captureOutput` 守卫劫持转发进 Yosys `log()`，完成「slang Diagnostic → Yosys 日志」的桥接。
- `check_diagnostics` 比较**格式化后的完整文案**（而非诊断码），配合 `test_slangdiag -expect` 与文件级静态变量 `expected_diagnostic`，构成「喂问题 SV、断言产生某条诊断」的负向测试机制；命中则进入 `in_succesful_failtest`，让带错误的测试也算通过。
- 诊断码的「声明（diag.h）→ 定义（diag.cc）→ 登记文案/级别（setup_messages）→ 触发点（各 .cc）→ 测试断言（.ys）」五处必须一一对齐，新增码时缺一不可。

## 7. 下一步学习建议

- 想看 `DiagnosticIssuer` 如何被网表中枢使用，进入 [u3-l1 NetlistContext：网表构建的中枢](u3-l1-netlist-context.md)，理解 `NetlistContext` 同时身为 `RTLILBuilder` 与 `DiagnosticIssuer` 的设计。
- 想了解具体诊断码的语义来源，可按主题分头读：时序类（`AlwaysFFBadTiming`、`IffUnsupported`）在 [u6-l1](u6-l1-timing-pattern-interpretor.md)，锁存器类（`LatchNotInferred`）在 [u6-l3](u6-l3-latch-inference.md)，存储器类（`MemoryNotInferred`）在 [u7-l1](u7-l1-memory-inference.md)，SVA 类（`SVAUnsupported`）在 [u7-l4](u7-l4-systemverilog-assertions.md)。
- 想亲手用 `test_slangdiag` 跑一个诊断测试，进入 [u8-l1 测试体系：等价性检查与自测](u8-l1-test-infrastructure.md)，那里讲 `tests/` 目录的 CTest 集成与 `run.sh`。
- 直接读源码的话，建议顺序：[src/diag.h](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/diag.h) → [src/diag.cc](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/diag.cc) 的 `setup_messages` → [src/slang_frontend.cc](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc) 的 `check_diagnostics` 与 `execute` 上报段。

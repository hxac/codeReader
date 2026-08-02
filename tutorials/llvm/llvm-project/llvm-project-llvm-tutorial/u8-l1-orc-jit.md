# 执行引擎与 ORC JIT

> 本讲对应增量更新 `4e924a6 → 036af906`。本次 LLVM 版本演进在 ORC/JITLink 上新增了 `COFFAutoImportGenerator`（一种新的 `DefinitionGenerator`），因此本讲在讲解 ORC v2 层式架构之后，专门用一节讲清「`DefinitionGenerator` 扩展点」，并以这个新加入的生成器为真实案例。

## 1. 本讲目标

学完本讲，你应当能够：

1. 说清 LLVM 的 `ExecutionEngine` 抽象要解决什么问题，以及它与「解释器 / JIT」的关系。
2. 画出 ORC v2 的层式架构（ExecutionSession / JITDylib / 各 Layer），并解释「向下发射、向上查找」两条数据流。
3. 读懂 `examples/HowToUseLLJIT`，并能把其中的 `add1` 改成返回不同的值、验证执行结果。
4. 说清 `DefinitionGenerator` 这一扩展点的职责，并能以新加入的 `COFFAutoImportGenerator` 为例，解释它如何为一个动态库的导出符号按需合成 `__imp_X` 指针槽与跳转 thunk。

## 2. 前置知识

本讲假设你已经学过本手册第二单元（LLVM IR 基础），尤其是：

- **Module / Function / BasicBlock / Instruction** 四层归属结构（u2-l1）。
- 用 **IRBuilder** 构造算术与返回指令（u2-l3）。
- IR 的内存表示与磁盘格式（u2-l4）。

在概念层面，你只需要理解一件事：前几讲我们都在「造 IR」和「优化 IR」，而本讲要回答的是——**这些 IR 怎么变成机器码并真正跑起来**。这就需要一个「执行引擎」。

补充两个 JIT 领域的常用术语（本讲会用到）：

- **JIT 编译（Just-In-Time Compilation）**：不是一次性把整个程序编译成磁盘文件，而是在程序运行期间、按需地把某段代码翻译成机器码、放进可执行内存，然后直接跳过去执行。
- **符号查找（symbol lookup）**：当代码里引用了一个还没被定义的符号（比如调用了外部函数 `printf`），执行引擎需要在「某个地方」找到它的地址。静态链接器在链接期做这件事，JIT 则在运行期做——而且可以**动态生成**这些定义。`DefinitionGenerator` 就是 ORC v2 为「动态生成定义」预留的扩展点。

## 3. 本讲源码地图

本讲涉及的关键文件及其作用：

| 文件 | 作用 |
|------|------|
| `examples/HowToUseLLJIT/HowToUseLLJIT.cpp` | 最小可用示例：构造一个 `add1` IR 模块，交给 LLJIT 编译并执行 |
| `include/llvm/ExecutionEngine/ExecutionEngine.h` | 执行引擎的抽象基类，定义了 `runFunction` 等接口 |
| `lib/ExecutionEngine/ExecutionEngine.cpp` | 执行引擎基类的通用实现 |
| `include/llvm/ExecutionEngine/Orc/Core.h` | ORC v2 核心：`ExecutionSession`、`JITDylib`、`DefinitionGenerator`、`LookupState` |
| `include/llvm/ExecutionEngine/Orc/LLJIT.h` / `lib/ExecutionEngine/Orc/LLJIT.cpp` | 预制的 ORC JIT 栈 `LLJIT`，含各层（Layer）的组装 |
| `include/llvm/ExecutionEngine/Orc/COFFAutoImportGenerator.h` | **本次新增**：COFF dllimport 自动导入生成器声明 |
| `lib/ExecutionEngine/Orc/COFFAutoImportGenerator.cpp` | **本次新增**：上述生成器的实现 |
| `docs/ORCv2.md` | **本次更新**：ORC v2 设计文档，新增了 `COFFAutoImportGenerator` 用法说明 |
| `unittests/ExecutionEngine/Orc/COFFAutoImportGeneratorTest.cpp` | **本次新增**：生成器的单元测试，是最好的「用法示例」 |

## 4. 核心概念与源码讲解

本讲按四个最小模块展开：

1. **4.1 `ExecutionEngine` 抽象** —— 先建立「执行引擎」的高层心智模型。
2. **4.2 ORC v2 层式架构** —— 现代执行引擎的内部结构。
3. **4.3 HowToUseLLJIT 示例** —— 用最小程序把架构跑起来。
4. **4.4 `DefinitionGenerator` 扩展点与 COFF dllimport 自动导入** —— 架构的扩展点，以及本次新增的实例。

### 4.1 `ExecutionEngine` 抽象

#### 4.1.1 概念说明

前几讲里，我们用 `llvm-as` 把 IR 写成位码、用 `opt` 优化、用 `llc` 生成 `.s`/`.o`。这些都是**离线**工具：编译完得到一个文件，再交给别的程序去运行。

但很多时候我们希望**在同一个进程里**：构造好一棵 IR 树（或读入一个 `.bc`），立刻让它变成机器码并执行，拿到返回值。这种「在内存里编译并执行 IR」的能力，需要一个统一的抽象——这就是 `ExecutionEngine`。

LLVM 在源码里对它的定位非常直白：

> 抽象接口，用于执行 LLVM 模块，既支持解释器（interpreter）实现，也支持即时编译（JIT）实现。

也就是说，`ExecutionEngine` 是一个**接口基类**，它规定了「给我一个 `Module`、给我一个函数和参数，我就把执行结果还给你」这样的契约；至于底层是逐条解释 IR，还是先编译成机器码再跳转，是这个抽象的两种实现策略。历史上 LLVM 有过 `Interpreter`、`MCJIT`、`ORC v1`、`ORC v2` 等多种实现，本讲聚焦当代主流的 **ORC v2**（封装为 `LLJIT`）。

理解这一点后，有一条关键结论要记住：**`ExecutionEngine` 是「面向使用者」的薄抽象，真正的复杂度都在它的 JIT 实现里（ORC v2）。** 所以 4.2 我们就钻进 ORC v2 内部。

#### 4.1.2 核心流程

从使用者的视角，执行引擎的生命周期只有三步：

```
1. 装载：把一个或多个 Module 放进执行引擎（addModule / addIRModule）
2. 查找：按名字查到某个函数的地址（lookup / getPointerToFunction）
3. 执行：把地址转成函数指针，像普通 C 函数一样调用（runFunction / 直接函数指针）
```

对应到抽象基类，`runFunction` 就是那个「执行」接口的虚函数：

```text
runFunction(Function *F, 参数列表)  →  GenericValue 返回值
```

需要强调：现代用法（包括本讲的 `HowToUseLLJIT`）**通常不再调用 `runFunction`**，而是用 `lookup` 拿到地址、自己转型成函数指针后直接调用——这样更高效、类型也更清楚。`runFunction` 更多是老接口和历史代码在用。

#### 4.1.3 源码精读

`ExecutionEngine` 基类定义在这里，类注释点明了它的双重职责：

- [ExecutionEngine.h:97-100](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/llvm/include/llvm/ExecutionEngine/ExecutionEngine.h#L97-L100) —— 中文：定义执行引擎抽象基类，注释明确它「既支持解释器、也支持 JIT」。

最核心的虚函数 `runFunction`（子类必须实现，故为纯虚）：

- [ExecutionEngine.h:217-226](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/llvm/include/llvm/ExecutionEngine/ExecutionEngine.h#L217-L226) —— 中文：声明 `runFunction`，给定函数与参数执行并返回 `GenericValue`；注释也提示现代代码更倾向用 `GetFunctionAddress` 拿地址再自己调用。

> 说明：`ExecutionEngine` 是一个庞大且偏「遗留」的接口（还带着全局地址映射表 `GlobalAddressMap` 等）。本讲不展开它的每一处成员，只把它当作「通往 ORC v2 的入口」来理解。ORC v2 的 `LLJIT` 并不直接继承这个老 `ExecutionEngine`，而是自成一套更清晰的 API。

#### 4.1.4 代码实践

**实践目标**：在源码层面确认「执行引擎是一个抽象接口，有多种实现」。

**操作步骤**：

1. 打开 `include/llvm/ExecutionEngine/ExecutionEngine.h`，找到 `runFunction` 的声明，确认它是 `virtual ... = 0`（纯虚函数）。
2. 在 `include/llvm/ExecutionEngine/` 目录下浏览，看看有哪些与执行引擎相关的头文件（例如 `Orc/` 子目录、`MCJIT.h` 等），体会「同一抽象、多种实现」。

**需要观察的现象**：`runFunction` 是纯虚的，说明它只是一个约定；真正的行为由派生类决定。

**预期结果**：你会确认 `ExecutionEngine` 只定义接口，不提供 JIT 的具体逻辑。

#### 4.1.5 小练习与答案

**练习 1**：为什么 LLVM 要把「解释器」和「JIT」统一在同一个 `ExecutionEngine` 抽象下？
> **参考答案**：因为对使用者而言，二者对外契约相同——「给我模块和函数，我返回执行结果」。把它抽象成同一个基类，上层代码可以不关心底层是逐条解释还是先编译后跳转，从而在不同实现之间切换。

**练习 2**：现代示例（如 `HowToUseLLJIT`）为什么往往不调用 `runFunction`？
> **参考答案**：`runFunction` 走的是 `GenericValue` 这种类型擦除的通用返回值，开销大、类型不直观。现代做法是用 `lookup` 拿到函数地址、直接转型成具体签名的函数指针后调用，既高效又类型安全。

---

### 4.2 ORC v2 层式架构

#### 4.2.1 概念说明

ORC（On-Request Compilation）是 LLVM 自研的 JIT 框架，**v2 是当前版本**。它的核心设计思想是「**层（Layer）**」与「**惰性物化（lazy materialization）**」。

直觉上，可以这样理解 ORC v2 的三个核心概念：

- **ExecutionSession（执行会话）**：整个 JIT 的「中枢」与状态持有者。一次 JIT 运行就是一个 session，所有 JITDylib、符号表、查找调度都归它管。
- **JITDylib（JIT 动态库）**：模拟一个普通的 `.so`/`.dll`。它有自己的符号表和「链接顺序」（link order，即它可以链接到哪些其它 JITDylib 来解析外部符号）。你把 IR 模块、对象文件、生成器都加到某个 JITDylib 里。
- **Layer（层）**：把 IR 一路加工成可执行内存的「流水线工位」。每一层只干一件事：变换 IR、编译 IR→对象文件、把对象文件链接进内存。层与层之间层层包裹，组成一条管道。

「层式架构」的最大好处是**可组合**：你可以在管道中间插入自己的变换层（比如优化、插桩），而不必改动前后层。`LLJIT` 就是 ORC 官方预制好的一条「标准管道」，开箱即用。

#### 4.2.2 核心流程

ORC v2 有两条方向相反的数据流，必须分清：

**① 向下发射（Emit，加代码时）**——把高层表示一步步降级到底层：

```
IR Module
  → (InitHelper)IRTransformLayer   变换 IR（可插自定义 pass）
  → IRCompileLayer                  编译：IR → 对象文件
  → ObjectTransformLayer            变换对象文件
  → ObjectLinkingLayer              链接：对象文件 → 可执行内存（经 JITLink）
```

**② 向上查找（Lookup，取地址时）**——这是 ORC 的灵魂，体现「按需物化」：

```
你调用 lookup("add1")
  → ExecutionSession 在 JITDylib 的符号表里找 "add1"
  → 找到定义但还没物化？触发物化（materialization）
     → 物化会沿「发射」管道把 add1 真正编译+链接出来
  → 物化完成后，把最终地址返回给你
```

关键是第二步里的「**还没定义就去找 DefinitionGenerator**」：如果 JITDylib 里压根没有 `add1` 的定义，但又有人请求它，ORC 不会立刻报错，而是把这个请求交给挂在 JITDylib 上的**定义生成器**（`DefinitionGenerator`）——它们有机会「现场编造」一个定义。这正是 4.4 要讲的扩展点，也是本次新增 `COFFAutoImportGenerator` 的落脚点。

#### 4.2.3 源码精读

`JITDylib` 的设计意图在注释里写得很清楚——它就是一个「不用预先全部编译」的动态库：

- [Core.h:654-673](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/llvm/include/llvm/ExecutionEngine/Orc/Core.h#L654-L673) —— 中文：`JITDylib` 类注释，说明它「模拟普通 dylib 的行为，但不需要把内容预先全部编译」，内容通过加入 `MaterializationUnit` 来定义，靠链接顺序解析外部引用。

`LLJIT` 持有的各层成员（即「标准管道」的工位）：

- [LLJIT.h:280-284](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/llvm/include/llvm/ExecutionEngine/Orc/LLJIT.h#L280-L284) —— 中文：`LLJIT` 的私有成员，依次持有 `ObjLinkingLayer`、`ObjTransformLayer`、`CompileLayer`、`TransformLayer`（以及 `InitHelperTransformLayer`），这就是层式架构在数据结构上的体现。
- [LLJIT.h:318-319](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/llvm/include/llvm/ExecutionEngine/Orc/LLJIT.h#L318-L319) —— 中文：还有 `IPLayer`（`IRPartitionLayer`，IR 分区）与 `CODLayer`（`CompileOnDemandLayer`，按需编译），这两个用于「懒编译」的 `LLLazyJIT` 变体。

这些层在 `LLJIT` 构造时被**层层包裹**起来（注意包裹顺序：后构造的层把先构造的层作为下游）：

- [LLJIT.cpp:1043-1058](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/llvm/lib/ExecutionEngine/Orc/LLJIT.cpp#L1043-L1058) —— 中文：构造管道——`ObjTransformLayer` 包住 `ObjLinkingLayer`；`CompileLayer`(IRCompileLayer) 包住 `ObjTransformLayer` 并绑定编译函数；`TransformLayer` 包住 `CompileLayer`；`InitHelperTransformLayer` 又包住 `TransformLayer`。这段代码就是 4.2.2「向下发射」管道的真实接线。

向下的「加模块」入口：

- [LLJIT.cpp:911-919](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/llvm/lib/ExecutionEngine/Orc/LLJIT.cpp#L911-L919) —— 中文：`addIRModule` 先套上数据布局，再把 IR 模块交给**最顶层** `InitHelperTransformLayer->add`，由此开启逐层向下降级。注意此刻并不会真正编译——只是登记定义，编译推迟到查找时（惰性物化）。

向上的「查找」入口：

- [LLJIT.cpp:936-944](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/llvm/lib/ExecutionEngine/Orc/LLJIT.cpp#L936-L944) —— 中文：`lookupLinkerMangled` 把请求转给 `ES->lookup`，构造一条以目标 JITDylib 为唯一元素的查找顺序。这里触发的就是「向上查找 → 物化 → 返回地址」的全过程。

#### 4.2.4 代码实践

**实践目标**：在源码里走一遍「加模块向下、查找向上」的两条路径。

**操作步骤**：

1. 在 `LLJIT.cpp:911` 的 `addIRModule` 设一个心智断点：它把模块交给 `InitHelperTransformLayer`。沿 `IRTransformLayer → IRCompileLayer → ObjectTransformLayer → ObjectLinkingLayer` 的包含关系，对照 `LLJIT.cpp:1043-1058` 理解「层是层层包裹的」。
2. 在 `LLJIT.cpp:936` 的 `lookupLinkerMangled` 设第二个心智断点：它把查找交给 `ES->lookup`。理解「查找」与「加模块」是两个独立动作——加模块只登记，查找才真正编译。

**需要观察的现象**：加模块时并没有立即生成机器码；机器码是在第一次 `lookup` 触发物化时才生成的。

**预期结果**：你能用一句话说出「层式架构 = 向下降级管道 + 向上按需物化」。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `addIRModule` 之后程序并不会变慢（没有立即编译）？
> **参考答案**：ORC v2 是惰性物化的。`addIRModule` 只是把模块登记进 JITDylib 的符号表，标明「这些符号在这里有定义，但还没物化」。真正编译+链接推迟到第一次 `lookup` 触发物化时才发生。

**练习 2**：如果查找一个根本不存在的符号，ORC 会怎么做？
> **参考答案**：先查 JITDylib 的符号表；若无定义，依次询问挂在该 JITDylib 上的 `DefinitionGenerator`（见 4.4）；若所有生成器都未能给出定义，才最终以错误结束查找——这正是 4.4 中 `UnexportedSymbolFailsToLink` 测试验证的行为。

---

### 4.3 HowToUseLLJIT 示例

#### 4.3.1 概念说明

理论讲完，来看一个能跑的最小例子。`examples/HowToUseLLJIT` 构造了一个只有 `add1(int x){ return x+1; }` 的 IR 模块，交给 `LLJIT` 编译，然后调用 `add1(42)` 并打印结果。它把 4.2 的抽象全部落到了具体 API 上。

这个例子同时是「用 IRBuilder 生产 IR」（u2-l3）和「用 LLJIT 执行 IR」的结合点：前半段造 IR，后半段执行 IR。

#### 4.3.2 核心流程

整个程序分两段：

```text
【造 IR】createDemoModule()
  1. 建 LLVMContext + Module("test")
  2. 定义 add1 函数签名：int (int)
  3. 加一个 EntryBlock，用 IRBuilder 插入 `%add = add i32 1, %arg` 与 `ret i32 %add`
  4. 包装成 ThreadSafeModule 返回

【执行 IR】main()
  1. InitializeNativeTarget() / InitializeNativeTargetAsmPrinter()  ← 注册本机目标
  2. LLJITBuilder().create()            ← 拼装一条默认的 ORC 管道
  3. J->addIRModule(std::move(M))       ← 把模块塞进主 JITDylib（向下登记）
  4. J->lookup("add1")                  ← 查找并物化（向上触发编译）
  5. 地址 .toPtr<int(int)>()            ← 转成函数指针
  6. Add1(42)                           ← 像普通 C 函数一样调用
```

注意第 1 步的 `InitializeNativeTarget`：它只注册「当前正在运行的这台机器」对应的后端。JIT 默认只为本机生成代码（要把代码放进本机内存并跳转执行，当然得是本机能执行的指令）。

#### 4.3.3 源码精读

`createDemoModule` —— 用 IRBuilder 造出 `add1`：

- [HowToUseLLJIT.cpp:41-75](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/llvm/examples/HowToUseLLJIT/HowToUseLLJIT.cpp#L41-L75) —— 中文：创建 `LLVMContext` 与 `Module`，定义 `add1` 的 `FunctionType`，加 `EntryBlock`，用 `IRBuilder` 插入 `add` 与 `ret`，最后包成 `ThreadSafeModule`。注意这里用 `ThreadSafeModule` 而非裸 `Module`——因为 ORC 支持并发编译，IR 需要带锁保护。

`main` —— 注册目标、建 JIT、执行：

- [HowToUseLLJIT.cpp:81-82](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/llvm/examples/HowToUseLLJIT/HowToUseLLJIT.cpp#L81-L82) —— 中文：`InitializeNativeTarget()` 注册本机后端、`InitializeNativeTargetAsmPrinter()` 注册汇编打印后端，这是让 JIT 能为本机生成代码的前提。
- [HowToUseLLJIT.cpp:88](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/llvm/examples/HowToUseLLJIT/HowToUseLLJIT.cpp#L88) —— 中文：`LLJITBuilder().create()` 用建造者模式拼装一条默认 ORC 管道（即 4.2.3 看到的那套层），返回一个 `LLJIT` 实例。
- [HowToUseLLJIT.cpp:91](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/llvm/examples/HowToUseLLJIT/HowToUseLLJIT.cpp#L91) —— 中文：`J->addIRModule(std::move(M))` 把模块塞进主 JITDylib（向下登记，尚未编译）。
- [HowToUseLLJIT.cpp:94-98](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/llvm/examples/HowToUseLLJIT/HowToUseLLJIT.cpp#L94-L98) —— 中文：`J->lookup("add1")` 触发物化并返回地址；`.toPtr<int(int)>()` 转成函数指针；调用 `Add1(42)` 打印 `add1(42) = 43`。

还有一个细节值得注意：程序开头有 `ExitOnError ExitOnErr;`（[HowToUseLLJIT.cpp:39](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/llvm/examples/HowToUseLLJIT/HowToUseLLJIT.cpp#L39)）。ORC 的几乎所有 API 都返回 `Expected<T>`（成功带值、失败带 `Error`）。`ExitOnError` 是个便捷工具：把 `Expected<T>` 解开，出错就打印并退出，省去手写大段错误处理。

#### 4.3.4 代码实践

**实践目标**：亲手跑通 `HowToUseLLJIT`，并修改它让 JIT 模块返回不同的整数，验证「JIT 出来的代码真的被执行了」。

**操作步骤**：

1. 配置并构建 LLVM（至少包含 `examples`，见 u1-l2），用 CMake 目标 `HowToUseLLJIT` 单独构建它（具体命令视你的构建生成器而定）。
2. 运行可执行文件，观察输出。
3. 修改 `createDemoModule`：把 `builder.CreateAdd(One, ArgX)`（[HowToUseLLJIT.cpp:69](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/llvm/examples/HowToUseLLJIT/HowToUseLLJIT.cpp#L69)）改成例如乘法 `builder.CreateMul(One, ArgX)`，或把常量 `1` 改成别的值，或改成 `CreateAdd` 一个更大的常量。
4. 重新编译并运行，对照 `add1(42) = ...` 的输出验证改动生效。

**需要观察的现象**：改动 IR 构造代码后，输出结果随之改变，说明「被 JIT 编译并执行的，正是你刚构造的那段 IR」。

**预期结果**：例如把 `add` 改成「`x + 10`」，运行应输出 `add1(42) = 52`。

> ⚠️ 若你的环境尚未配置好构建，无法运行，请把这一步标记为「待本地验证」，仅完成源码阅读与改动设计即可。

#### 4.3.5 小练习与答案

**练习 1**：为什么模块要用 `ThreadSafeModule` 而不是直接传 `std::unique_ptr<Module>`？
> **参考答案**：ORC v2 支持并发编译，多个线程可能同时访问/编译模块。`ThreadSafeModule` 把 `Module` 和它所属的 `LLVMContext` 连同一个锁一起管理，保证并发下的安全访问。

**练习 2**：`lookup` 返回的地址，与进程里其它普通函数地址有何区别？
> **参考答案**：没有本质区别。JIT 链接后，`add1` 的机器码就放在本进程的可执行内存里，`lookup` 返回的就是那块内存的地址，可以像任何 C 函数指针一样调用。

---

### 4.4 `DefinitionGenerator` 扩展点与 COFF dllimport 自动导入

> 本节对应本次版本演进的核心变化（`4e924a6 → 036af906`）：ORC 新增了 `COFFAutoImportGenerator`。先讲通用的扩展点，再讲这个新实例。

#### 4.4.1 概念说明

回顾 4.2：当 `lookup` 查一个 JITDylib 里没有定义的符号时，ORC 不会立刻失败，而是会询问挂在该 JITDylib 上的**定义生成器**。`DefinitionGenerator` 就是这个「现场生成定义」的扩展点。

为什么需要它？因为 JIT 场景下，「定义」不一定非得来自你显式加进去的 IR/对象文件，还可以来自：

- 进程已加载的动态库（`dlsym` / `GetProcAddress`）—— `DynamicLibrarySearchGenerator`。
- 静态库（`.a`）里按需抽出某个成员对象文件 —— `StaticLibraryDefinitionGenerator`。
- 把别的 JITDylib 的符号「重导出」出来 —— `ReexportsGenerator`。
- COFF 平台 dllimport 约定下，为外部库函数合成导入桩 —— **本次新增的 `COFFAutoImportGenerator`**。

每个生成器只实现一个核心虚函数 `tryToGenerate`：拿到「当前还缺哪些符号」的集合，自行决定能否为其中一些造出定义，造好的定义（一个 `MaterializationUnit`，通常是一张可被 JITLink 的 `LinkGraph`）交还给 JITDylib。

**为什么 COFF 需要一个专门的生成器？** 这是理解本节新代码的关键背景：

在 Windows/COFF 目标上，调用一个 `dllimport` 函数，编译器产出的不是「直接跳转到函数地址」，而是「间接调用，跳转目标存在一个名为 `__imp_X` 的指针槽（Import Address Table，IAT）里」。即便你写的是「直接调用」`X`，链接期也期望绑定到一个由**导入库（import library）**提供的「跳转 thunk」（形如 `jmpq *__imp_X(%rip)`）。换句话说，COFF 对象要正确链接，光有函数地址不够，还得有 `__imp_X` 槽和 `X` 桩。

在普通静态链接里，这些桩由导入库（`.lib`）提供。但在 JIT 场景下，我们往往只有 DLL 本身、不想再额外构造导入库。`COFFAutoImportGenerator` 就是来「自动合成」这些桩的——这就是它的名字里 **Auto Import** 的含义。

#### 4.4.2 核心流程

**`DefinitionGenerator` 的通用机制**：

```text
lookup 缺符号 X
  → ExecutionSession 把 {X, ...} 交给某 JITDylib 的每个 DefinitionGenerator
  → 生成器.tryToGenerate(LookupState, K, JD, flags, {X, ...})
       · 可以为 X 造一个定义（返回 MaterializationUnit / 把 LinkGraph 加入 Layer）
       · 也可以「暂时挂起」查找（持有 LookupState，异步解析后再 continueLookup）
       · 不认识的符号就不管，留给下一个生成器
  → 全部生成器都给不出 X → 查找失败
```

注意一个细节：`tryToGenerate` 接收一个 `LookupState &`。生成器可以**异步**工作——先「接管」这次查找（把 `LookupState` move 走），去别处查地址，查完再调 `LookupState::continueLookup` 把查找重新启动。新加入的 `COFFAutoImportGenerator` 正是用了这套异步机制。

**`COFFAutoImportGenerator` 的具体流程**：

```text
Load(ES, ObjLinkingLayer, DylibMgr, "/path/to/lib.dll")
  1. 取目标三元组，查 JITLink 是否注册了「匿名指针创建器」「指针跳转桩创建器」
     （没有就 Load 失败 —— 不支持的架构会被提前拒绝）
  2. DylibMgr.loadDylib("/path/to/lib.dll") 在执行器侧加载该 DLL，拿到句柄
  3. 缓存创建器 + 库句柄，返回生成器实例

（之后某次 lookup 缺符号时）
tryToGenerate(..., Symbols={X, __imp_X, ...})
  1. 把每个符号去掉 "__imp_" 前缀、去重，得到「基础名」集合
  2. 异步向 DylibMgr 查询这些基础名（WeaklyReferencedSymbol：弱引用）
       → 库导出表里有的，返回真实地址；没有的，返回空（保留未解析）
  3. 对每个查到地址的 X，造一个 LinkGraph（createStubsGraph）：
       · 一个本地绝对符号，值为 X 的真实地址
       · __imp_X：指针槽，内容指向上面那个绝对符号
       · X：跳转桩，`jmpq *__imp_X`
     并把它们设为 Weak（让以后真实定义能覆盖）
  4. 把整张图交给 ObjectLinkingLayer（绑定到一个共享 ResourceTracker）
  5. continueLookup 继续这次查找
```

它最大的设计取舍（在头文件注释里写得很明确）：

- **绑定到单个动态库**：这个库的导出表就是「什么能被合成」的权威。库里不导出的符号，生成器绝不会合成——于是链接照常失败，行为跟「静态链接对应的导入库」一致。
- **「easy mode」**：假定每个 import 都是函数，不区分代码与数据，所以**数据导入不被支持**；而且 `&X` 解析到的是合成的桩，而非库里 `X` 的真实地址。
- **惰性 + 单一 ResourceTracker**：合成是惰性的（由 JITLink 的外部符号查找驱动），所有桩共享一个 `ResourceTracker`，调用其 `remove()` 可以一次性回收全部合成的桩。

与它相对的是更通用的 `DLLImportDefinitionGenerator`（见 `ExecutionUtils.h`）：后者通过 **JITDylib 的链接顺序**解析底层符号，而 `COFFAutoImportGenerator` 绑定**单个库**——这是两者最核心的区别。

#### 4.4.3 源码精读

先看通用扩展点的定义。`DefinitionGenerator` 是一个抽象基类，核心是一个纯虚 `tryToGenerate`：

- [Core.h:630-652](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/llvm/include/llvm/ExecutionEngine/Orc/Core.h#L630-L652) —— 中文：`DefinitionGenerator` 基类。注释说明「定义生成器可挂在 JITDylib 上，在查找期间为原本未解析的符号生成新定义」；唯一的纯虚方法 `tryToGenerate`（[Core.h:644-646](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/llvm/include/llvm/ExecutionEngine/Orc/Core.h#L644-L646)）接收 `LookupState`、查找种类、目标 JITDylib、查找标志以及「未解析符号集合」。

配合它的 `LookupState`，允许生成器异步挂起查找：

- [Core.h:604-628](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/llvm/include/llvm/ExecutionEngine/Orc/Core.h#L604-L628) —— 中文：`LookupState` 封装「一次进行中的查找」的状态；生成器可以接管它，等异步结果回来后再用 `continueLookup(Err)` 重启查找（[Core.h:617-619](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/llvm/include/llvm/ExecutionEngine/Orc/Core.h#L617-L619)）。

现在看本次**新增**的 `COFFAutoImportGenerator`。头文件的类注释把设计意图讲得非常完整（读源码注释本身就是学习 ORC 的好方法）：

- [COFFAutoImportGenerator.h:27-52](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/llvm/include/llvm/ExecutionEngine/Orc/COFFAutoImportGenerator.h#L27-L52) —— 中文：类文档。说明它为一个动态库导出的符号合成 COFF dllimport `__imp_` 符号与 PLT 桩；强调它「绑定单个库、库导出表为权威」「惰性合成、假定都是函数导入、数据导入不支持」「`&X` 解析到桩而非库内实现」「所有桩共享一个 ResourceTracker」「依赖 JITLink 注册的指针/桩创建器」。

它的三个公开接口：

- [COFFAutoImportGenerator.h:60-62](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/llvm/include/llvm/ExecutionEngine/Orc/COFFAutoImportGenerator.h#L60-L62) —— 中文：静态工厂 `Load`，加载库并返回生成器（失败返回原因）。强调通过 `DylibManager` 解析导入，故同时支持「进程内」与「进程外」执行。
- [COFFAutoImportGenerator.h:64-66](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/llvm/include/llvm/ExecutionEngine/Orc/COFFAutoImportGenerator.h#L64-L66) —— 中文：实现基类的 `tryToGenerate`，这是生成器的核心入口。
- [COFFAutoImportGenerator.h:74-76](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/llvm/include/llvm/ExecutionEngine/Orc/COFFAutoImportGenerator.h#L74-L76) —— 中文：`getImportStubsResourceTracker()`，返回持有全部已合成桩的 `ResourceTracker`，对其 `remove()` 可一次性回收。

再看实现。`Load` 提前校验架构并加载库：

- [COFFAutoImportGenerator.cpp:18-42](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/llvm/lib/ExecutionEngine/Orc/COFFAutoImportGenerator.cpp#L18-L42) —— 中文：取目标三元组，分别用 `getAnonymousPointerCreator`、`getPointerJumpStubCreator` 查 JITLink 是否注册了指针/桩创建器（缺失即报错，**把不支持的架构在 Load 时就拒掉**）；再 `DylibMgr.loadDylib` 在执行器侧加载库；最后构造对象并缓存这两个创建器。

`tryToGenerate` 用了异步查找：

- [COFFAutoImportGenerator.cpp:44-91](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/llvm/lib/ExecutionEngine/Orc/COFFAutoImportGenerator.cpp#L44-L91) —— 中文：先对每个请求符号去掉 `__imp_` 前缀并去重（[L53-62](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/llvm/lib/ExecutionEngine/Orc/COFFAutoImportGenerator.cpp#L53-L62)），以「弱引用」异步查这些基础名（[L64-65](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/llvm/lib/ExecutionEngine/Orc/COFFAutoImportGenerator.cpp#L64-L65)）；回调里只保留「库导出且地址非空」的结果（[L70-75](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/llvm/lib/ExecutionEngine/Orc/COFFAutoImportGenerator.cpp#L70-L75)）——**这就是「库导出表为权威」的落点**；然后用 `createStubsGraph` 造桩、绑定到一个（按需新建的）共享 `ResourceTracker`，再 `continueLookup`（[L80-88](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/llvm/lib/ExecutionEngine/Orc/COFFAutoImportGenerator.cpp#L80-L88)）。

`createStubsGraph` 是「合成 `__imp_X` + 桩」的核心：

- [COFFAutoImportGenerator.cpp:97-129](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/llvm/lib/ExecutionEngine/Orc/COFFAutoImportGenerator.cpp#L97-L129) —— 中文：对每个解析到的 `X`，先造一个值为 `X` 真实地址的本地绝对符号（[L110-112](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/llvm/lib/ExecutionEngine/Orc/COFFAutoImportGenerator.cpp#L110-L112)）；再用缓存的 `CreatePointer` 造出 `__imp_X` 指针槽指向它（[L114-119](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/llvm/lib/ExecutionEngine/Orc/COFFAutoImportGenerator.cpp#L114-L119)）；再用 `CreateStub` 造出 `X` 跳转桩（`jmpq *__imp_X`，[L121-125](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/llvm/lib/ExecutionEngine/Orc/COFFAutoImportGenerator.cpp#L121-L125)）。`__imp_X` 与 `X` 都设为 Weak，让日后真实定义能覆盖。注释里还有一句 FIXME：这套造桩逻辑与 `DLLImportDefinitionGenerator::createStubsGraph` 几乎相同，将来应抽成共享 helper。

文档（本次更新）给出了最简用法：

- [ORCv2.md:849-893](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/llvm/docs/ORCv2.md#L849-L893) —— 中文：本次新增的文档段落，解释 COFF/Windows 上 dllimport 走 `__imp_` IAT 槽、直接调用也期望绑定到导入库桩；并给出 `COFFAutoImportGenerator::Load(...)` + `JD.addGenerator(...)` 的用法片段；同样说明它是「easy mode」（假定函数导入、数据导入不支持、`&X` 是桩地址），并指向更通用的 `DLLImportDefinitionGenerator`。

最后，单元测试是最好的「用法 + 行为契约」说明书：

- [COFFAutoImportGeneratorTest.cpp:101-126](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/llvm/unittests/ExecutionEngine/Orc/COFFAutoImportGeneratorTest.cpp#L101-L126) —— 中文：`SynthesizesImpSlotAndThunk`。把生成器挂到 `main` JITDylib 后，查找 `__imp_X` 验证槽里存的就是 `X` 的真实地址（[L111-114](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/llvm/unittests/ExecutionEngine/Orc/COFFAutoImportGeneratorTest.cpp#L111-L114)）；查找 `X` 验证得到的是一个**独立的桩**（与 `__imp_X` 地址不同、与库内真实地址不同，[L118-121](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/llvm/unittests/ExecutionEngine/Orc/COFFAutoImportGeneratorTest.cpp#L118-L121)）；调用这个桩，验证它「跳过槽」到达真实实现（[L124-125](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/llvm/unittests/ExecutionEngine/Orc/COFFAutoImportGeneratorTest.cpp#L124-L125)）。
- [COFFAutoImportGeneratorTest.cpp:130-143](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/llvm/unittests/ExecutionEngine/Orc/COFFAutoImportGeneratorTest.cpp#L130-L143) —— 中文：`UnexportedSymbolFailsToLink`。库不导出的符号必须保持未解析、查找失败——这正是「库导出表为权威」的验证。
- [COFFAutoImportGeneratorTest.cpp:148-174](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/llvm/unittests/ExecutionEngine/Orc/COFFAutoImportGeneratorTest.cpp#L148-L174) —— 中文：`StubsResourceTrackerLifecycle`。验证所有桩共享一个 `ResourceTracker`，`remove()` 后变 defunct；再次导入会透明地新建一个 tracker。

#### 4.4.4 代码实践

**实践目标**：通过阅读头文件注释与测试，说清 `COFFAutoImportGenerator`「以单个动态库的导出表为权威，按需合成 `__imp_X` 槽与跳转 thunk」的工作方式。

**操作步骤（源码阅读型实践）**：

1. 打开 `include/llvm/ExecutionEngine/Orc/COFFAutoImportGenerator.h`，通读类注释（[L27-L52](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/llvm/include/llvm/ExecutionEngine/Orc/COFFAutoImportGenerator.h#L27-L52)）。
2. 对照实现 `lib/ExecutionEngine/Orc/COFFAutoImportGenerator.cpp` 的 `createStubsGraph`（[L97-129](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/llvm/lib/ExecutionEngine/Orc/COFFAutoImportGenerator.cpp#L97-L129)），在自己的笔记里画出「绝对符号 → `__imp_X` 槽 → `X` 桩」三者的指向关系。
3. 阅读三个单元测试，把它们各自验证的契约整理成一张表：

   | 测试 | 验证的契约 |
   |------|-----------|
   | `SynthesizesImpSlotAndThunk` | 槽存真实地址；`X` 是独立桩；调用桩能跳到真实实现 |
   | `UnexportedSymbolFailsToLink` | 库不导出的符号保持未解析 |
   | `StubsResourceTrackerLifecycle` | 桩共享一个 tracker；可整体回收；回收后再导入会新建 tracker |

4. 用一句话回答任务里的问题：**它如何以单个动态库的导出表为权威来按需合成 `__imp_X` 指针槽与跳转 thunk？**

**需要观察的现象**：你会看到「权威」体现在 `tryToGenerate` 的弱引用查询里——只有库导出表返回非空地址的符号，才会进入 `createStubsGraph` 被合成；其余一律留空，最终链接失败。

**预期结果**：你能独立复述「`__imp_X` 槽持有 `X` 真实地址、`X` 桩 `jmp` 过这个槽、二者都设为 Weak 以便被真实定义覆盖」这一机制，并能指出它与 `DLLImportDefinitionGenerator`（按链接顺序解析）的关键区别。

> ⚠️ 若想在进程内真正运行该生成器，需要 x86_64 宿主（测试用 `#if defined(__x86_64__)` 保护，见 [COFFAutoImportGeneratorTest.cpp:34](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/llvm/unittests/ExecutionEngine/Orc/COFFAutoImportGeneratorTest.cpp#L34)）。非该架构或未构建测试时，标记为「待本地验证」，仅完成源码阅读。

#### 4.4.5 小练习与答案

**练习 1**：`tryToGenerate` 接收的 `LookupState &` 有什么用？为什么 `COFFAutoImportGenerator` 需要它？
> **参考答案**：`LookupState` 代表「一次进行中的查找」，生成器可以接管它、把查找**异步挂起**，等结果回来再 `continueLookup` 重启。`COFFAutoImportGenerator` 需要异步地向 `DylibManager` 查询库的导出地址，所以必须 move 走 `LookupState`、在异步回调里再 `continueLookup`。

**练习 2**：为什么 `__imp_X` 和 `X` 桩都要设为 `Weak` 链接属性？
> **参考答案**：设为 Weak 表示「这是个兜底定义」。如果 JITDylib 后来加入了真正的 `X`（或 `__imp_X`）定义，链接器会让真实定义覆盖这个合成桩（模仿 `link.exe` 的行为），避免冲突。

**练习 3**：`COFFAutoImportGenerator` 与 `DLLImportDefinitionGenerator` 最核心的区别是什么？
> **参考答案**：底层符号的解析来源不同。`COFFAutoImportGenerator` **绑定单个动态库**，该库的导出表是唯一权威，不导出即失败；`DLLImportDefinitionGenerator` 则通过 **JITDylib 的链接顺序**解析底层符号，更通用。

---

## 5. 综合实践

把本讲的四个模块串起来，完成下面这个综合任务：

**任务**：基于 `HowToUseLLJIT`，搭建一个「会调用外部符号」的小 JIT。

1. **改造 IR（承接 4.3）**：在 `createDemoModule` 里，让 `add1` 之外再写一个函数 `double_add1`，它**调用** `add1` 并把结果乘 2。注意 `add1` 与 `double_add1` 在同一个模块里，互相调用会被 JIT 内部自动解析——这一步验证「同 JITDylib 内的符号互引」。
2. **理解外部符号解析（承接 4.4）**：在你的 `double_add1` 里再调用一个**不在模块里**的函数（例如 C 标准库的 `abs`）。运行后会失败，因为 JITDylib 里没有它的定义。
3. **挂一个生成器解决它（承接 4.4）**：仿照 ORCv2.md 的片段，用 `DynamicLibrarySearchGenerator::GetForCurrentProcess(...)`（见 `include/llvm/ExecutionEngine/Orc/ExecutionUtils.h`）给主 JITDylib 挂一个「从当前进程查符号」的生成器，再次运行，确认 `abs` 被正确解析。
4. **对照 COFFAutoImportGenerator（拓展）**：阅读 4.4 的 `COFFAutoImportGenerator`，说明如果你在 Windows/COFF 上 `dllimport` 一个库函数，为什么光有 `DynamicLibrarySearchGenerator` 还不够、还需要专门合成 `__imp_X` 槽与桩。

**验收标准**：

- 改造后 `double_add1(21)` 输出 `84`（`add1(21)=22`，再乘 2）。
- 第 2 步会失败、第 3 步挂上生成器后成功——你能用本讲的语言解释这一「失败→成功」的差异。
- 第 4 步你能用自己的话讲清 dllimport 的 `__imp_` 机制。

> ⚠️ 若本地未配置 LLVM 构建，第 1-3 步标记为「待本地验证」，但第 4 步的源码阅读与解释应能独立完成。

## 6. 本讲小结

- `ExecutionEngine` 是「执行 LLVM 模块」的抽象基类，统一了「解释器」与「JIT」两种实现；现代示例通常不再用它的 `runFunction`，而是 `lookup` 拿地址、直接调用。
- ORC v2 用「层式架构」组织 JIT：`ExecutionSession` 是中枢，`JITDylib` 模拟动态库，各 `Layer` 组成一条「向下降级」管道（IRTransformLayer → IRCompileLayer → ObjectTransformLayer → ObjectLinkingLayer）。
- ORC 的灵魂是「**向上查找 + 惰性物化**」：加模块只登记定义，第一次 `lookup` 才真正编译+链接。
- `HowToUseLLJIT` 把上述抽象落成最小可运行程序：`LLJITBuilder().create()` 建栈 → `addIRModule` 登记 → `lookup` 物化 → `.toPtr<...>()` 调用。
- `DefinitionGenerator` 是 ORC 的「现场生成定义」扩展点：查找缺符号时，生成器有机会造一个定义（可异步、可合成桩），造不出才失败。
- **本次新增的 `COFFAutoImportGenerator`** 是 `DefinitionGenerator` 的一个实例：绑定单个 DLL，以其导出表为权威，按需合成 COFF dllimport 所需的 `__imp_X` 指针槽与 `X` 跳转桩，所有桩共享一个可回收的 `ResourceTracker`。

## 7. 下一步学习建议

- **延续 ORC 主线**：本讲只讲了 `LLJIT` 这条「标准管道」。建议继续阅读 `docs/ORCv2.md` 全文，以及 `lib/ExecutionEngine/Orc/` 下的 `CompileOnDemandLayer`、`LazyReexports` 等，理解「懒编译」与「重导出」如何在不改主流程的前提下插进管道。
- **深入 JITLink**：4.4 反复提到 `ObjectLinkingLayer` 与 `LinkGraph`。JITLink 是 ORC 底层的链接器，建议阅读 `lib/ExecutionEngine/JITLink/`，理解 `LinkGraph` 如何被各 `*Creator`（指针创建器、桩创建器）操作——`COFFAutoImportGenerator` 的合成能力正是建立在它们之上。
- **测试与贡献闭环**：本讲的 `COFFAutoImportGeneratorTest.cpp` 是一份极好的「ORC 单测写法」范例。结合下一讲 u8-l2（lit 与 FileCheck）以及 u8-l3（扩展实践），你可以尝试为本讲的综合实践写一个最小回归测试。

# ExecutionEngine 与 ORC JIT

## 1. 本讲目标

到目前为止，我们走完了「源码 → IR → 优化 → 后端机器码 → 目标文件」这条**离线编译**主线：`clang` 生成 IR，`opt` 优化 IR，`llc` 把 IR 编译成 `.s`/`.o`，最终由链接器产出可执行文件。但还有另一条路：**不落盘成可执行文件，而是在程序运行过程中「现用现编译、立刻执行」**——这就是 JIT（Just-In-Time，即时编译）。

LLVM 的 JIT 能力由 `ExecutionEngine` 这套抽象承载，其现代实现叫 **ORC（On-Request Compilation，按需编译）**，目前是 **v2 版本（ORCv2）**。本讲学完后，你应该能够：

1. 理解 **JIT 执行模型**：为什么 IR 可以不经落盘直接在内存里被编译、链接、执行；以及「按需编译」中「按需」二字的真正含义——编译发生在你**查找（lookup）某个符号**的那一刻。
2. 掌握 **ORC v2 的分层架构**：`ExecutionSession` / `JITDylib`（核心层）+ 一摞可叠加的 **Layer（IRLayer、ObjectLayer）**，理解 `IRCompileLayer` 如何把 IR 编译成目标文件、对象链接层如何把它链进可执行内存。
3. 认识 **LLJIT** 这个「预制的 ORC 栈」：它的构造函数如何把 5 层叠起来、`LLJITBuilder` 如何配置它、`addIRModule` / `lookup` 如何驱动「添加模块 → 查找符号 → 触发编译 → 拿到地址调用」的完整闭环。

本讲是第 8 单元「执行引擎、JIT 与链接」的入口。它承接 u3-l1（IR 的 Module/Function/BasicBlock 层次）与 u4-l1（Pass 管理器），因为 JIT 内部同样要把 `Module` 喂给后端代码生成；后续 u8-l2（LTO）与 u8-l3（LLD 链接器）会复用本讲建立的「符号 / 链接 / 内存映像」概念。

## 2. 前置知识

在进入源码之前，先用通俗语言澄清几个反复出现的术语。

- **JIT（即时编译）**：程序运行期，把某段表示（这里是 LLVM IR）翻译成宿主机机器码、放入可执行内存、然后跳过去执行。与之相对的是 AOT（Ahead-Of-Time，提前编译），即传统的离线编译。
- **ExecutionEngine**：LLVM 里「执行 IR」的统一抽象基类。它有几个具体子类：古老的解释器 `Interpreter`、上一代 JIT `MCJIT`、以及本讲的 `ORC` 系。`lli` 工具（见 u1-l4）就是它们的命令行外壳。
- **ORC（On-Request Compilation）**：字面意思是「按需编译」。它的核心设计是：你往里 `addIRModule` 添加一个模块时，**并不立刻编译**；只有当你 `lookup("foo")` 去查某个符号时，ORC 才回溯找到定义该符号的模块，**现场把它编译、链接成可执行代码**。这就是「On-Request」的由来。
- **JITDylib**：「JIT 动态库」。可以类比成普通链接里的一个 `.so`/`.dll`/`.a`：它持有若干符号定义，并维护一个**链接顺序（link order）**——即「我引用的符号还可以去哪些 dylib 里找」。ORC 用它来组织符号与符号间的依赖。
- **符号（Symbol）**：一个有名字、有地址、有标志（是否导出、是否可调用）的实体。JIT 里一切工作的目标，就是「让某个名字解析到一个可调用的地址」。
- **物化（Materialization）**：把一个符号从「只有定义描述、还没产出实际代码/数据」的状态，推进到「代码已在内存、地址已确定、可以被调用」的过程。可以理解为「把抽象定义落实成具体的机器码」。
- **Layer（层）**：ORC 把「IR → 目标文件 → 链接进内存」这条流水线拆成若干层叠的组件，每一层只负责一段转换，并把结果交给下一层。层与层之间是「洋葱式」嵌套。
- **ThreadSafeModule**：一个 `Module` 连同它专属的 `LLVMContext`，外加一把锁，保证多线程下安全使用。ORC 在层之间传递的就是它，而不是裸 `Module`。

> 提示：如果你对 `Module`/`Function` 还不熟，先看 u3-l1；对「编译后端把 IR 变成目标文件」还不熟，可回顾 u6-l1（后端流水线）与 u6-l4（MC 层与目标文件）。本讲会直接复用这些概念。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
| --- | --- |
| `llvm/include/llvm/ExecutionEngine/Orc/Core.h` | ORC 核心头文件：定义 `ExecutionSession`、`JITDylib`、`ResourceTracker`、`MaterializationResponsibility`、`lookup` 等。 |
| `llvm/lib/ExecutionEngine/Orc/Core.cpp` | 核心实现：符号查找的状态机、物化任务的派发（`dispatchOutstandingMUs`）。 |
| `llvm/include/llvm/ExecutionEngine/Orc/Layer.h` | 「层」的抽象基类：`IRLayer`（吃 IR）、`ObjectLayer`（吃目标文件），二者都把工作浓缩成一个纯虚 `emit`。 |
| `llvm/include/llvm/ExecutionEngine/Orc/IRCompileLayer.h` / `llvm/lib/ExecutionEngine/Orc/IRCompileLayer.cpp` | `IRCompileLayer`：把 IR 编译成目标文件的层，含 `IRCompiler` 函子（封装 `TargetMachine`）。 |
| `llvm/include/llvm/ExecutionEngine/Orc/IRTransformLayer.h` | `IRTransformLayer`：对经过的 IR 跑一个用户自定义变换（透传默认）。 |
| `llvm/include/llvm/ExecutionEngine/Orc/LLJIT.h` / `llvm/lib/ExecutionEngine/Orc/LLJIT.cpp` | `LLJIT`：把上述组件拼成一个开箱即用的 JIT 栈；`LLJITBuilder` 负责配置；`LLLazyJIT` 在其上加惰性编译。 |
| `llvm/tools/lli/lli.cpp` | `lli` 工具：用 `LLLazyJITBuilder` 构造 JIT、加模块、查找 `main` 并执行。 |
| `llvm/examples/HowToUseLLJIT/HowToUseLLJIT.cpp` / `CMakeLists.txt` | 官方最小示例：用 `IRBuilder` 造一个 `add1` 模块，经 LLJIT 编译执行。 |
| `llvm/docs/ORCv2.md` | 官方「ORC 设计与实现」文档，本讲大量参考它。 |

## 4. 核心概念与源码讲解

本讲拆成两个最小模块：

- **4.1 ORC v2 分层架构**：`ExecutionSession` + `JITDylib` 的核心抽象、`lookup` 如何按需触发物化、`IRLayer`/`ObjectLayer` 两类层基类、`IRCompileLayer` 的「IR → 目标文件」转换。
- **4.2 LLJIT 组件**：`LLJIT` 如何把 5 层叠成栈、`LLJITBuilder` 的配置项、`addIRModule`/`lookup` 的完整闭环、`LLLazyJIT` 的惰性编译扩展。

---

### 4.1 ORC v2 分层架构

#### 4.1.1 概念说明

ORC 的设计哲学可以用三句话概括：

1. **一切围绕「符号」展开**。JIT 的终极目的只有一个：给定一个名字（如 `"main"`），返回一个可以跳转执行的地址。模块、IR、目标文件都只是「符号定义的来源」。
2. **按需编译（On-Request）**。添加模块时不编译；查找符号时才回溯到定义来源，现场物化。这让 JIT 天然支持「用到什么才编什么」。
3. **可组合的层（Composable Layers）**。「IR → 目标文件 → 可执行内存」被拆成若干层，每层只做一件事并把结果交给下一层。你像搭积木一样选层、叠层，就能拼出 eager / lazy、in-process / out-of-process、单线程 / 多线程的各种组合。

这套架构有两个正交的维度，初学者最容易混淆，务必分清：

- **核心层（Core）**：`ExecutionSession` + 一堆 `JITDylib`。它负责**符号的登记、查找、状态机推进与物化调度**——和「具体怎么编译」无关，完全是符号层面的簿记。
- **层（Layer）**：真正干「编译 / 链接」活的组件，挂在 `ExecutionSession` 上，由核心层在需要时回调。`IRLayer` 吃 IR、`ObjectLayer` 吃目标文件，二者都把工作浓缩成一个 `emit` 方法。

ORC v2 相比 v1 的关键进步是**并发安全**与**可移除性**：JIT 出来的代码可以被多个线程并发执行、并发请求编译，甚至可以在运行后安全移除（靠 `ResourceTracker` 追踪资源归属）。这些都是为了支撑 LLDB 表达式求值、Julia/JVM 这类高强度场景。

#### 4.1.2 核心流程

一次「添加模块 → 执行函数」的完整生命周期如下（伪代码）：

```text
# 1. 准备一个执行会话与一个动态库
ES   = ExecutionSession(EPC)          # EPC 描述「代码最终执行在哪个进程」
JD   = ES.createJITDylib("main")     # 创建一个 JITDylib，相当于一个 .so

# 2. 叠层：IR 层 -> 对象层，挂在 ES 上
ObjLayer    = RTDyldObjectLinkingLayer(ES, ...)   # 目标文件 -> 可执行内存
CompileLayer= IRCompileLayer(ES, ObjLayer, Compiler)  # IR -> 目标文件

# 3. 添加模块（此时【不编译】，只登记符号）
CompileLayer.add(JD, ThreadSafeModule(M, Ctx))
#   内部：扫描 M，把每个全局值的名字+标志打包成一个 MaterializationUnit(MU)
#         MU 被塞进 JD，但 MU.materialize() 还没被调用

# 4. 查找符号（此时才【触发编译】）
Addr = ES.lookup([JD], "foo")
#   4a. 在 JD 的符号表里找到 "foo" -> 它由某个 MU 定义，状态 = NotMaterialized
#   4b. 派发 MaterializationTask -> 调用 MU.materialize()
#   4c. MU.materialize() 调用所在层的 emit：
#         IRCompileLayer.emit  ->  Compiler(M) 把 IR 编译成目标文件(MemoryBuffer)
#                              ->  ObjLayer.emit 把目标文件链进内存、登记符号地址
#   4d. 符号状态推进到 Resolved -> Ready，查找查询完成
#   4e. 返回符号地址 Addr

# 5. 把地址转成函数指针并调用
fn = Addr.toPtr<int(int)>()
fn(42)
```

关键观察：

- **第 3 步「登记」与第 4 步「物化」是分离的**。这正是「On-Request」的内存体现：模块加入时只是把符号登记为「未物化」，真正产出机器码是 `lookup` 时才发生的。
- **物化沿层向下流动**。`IRCompileLayer.emit` 产出目标文件后，并不自己链接，而是交给下一层 `ObjectLayer.emit`。每层只懂自己那一段。
- **符号状态机**驱动整个推进：`NotMaterialized → Materializing → Resolved → Ready`。`lookup` 要求到达某个状态（默认 `Ready`），核心层负责把符号一路推进到该状态才返回。

#### 4.1.3 源码精读

**(1) 核心三件套：`ExecutionSession`、`JITDylib`、`ResourceTracker`**

`ExecutionSession` 是整个 JIT 会话的「司令部」，持有 `ExecutorProcessControl`（描述代码执行在哪个进程、如何派发任务）、符号字符串池、错误报告器等。它最重要的对外能力是创建 `JITDylib` 与查找符号：

[`llvm/include/llvm/ExecutionEngine/Orc/Core.h`](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/llvm/include/llvm/ExecutionEngine/Orc/Core.h#L1111-L1139) 定义了 `ExecutionSession` 类，其中 `createJITDylib` 创建一个挂了平台标准符号的动态库：

- [Core.h:L1227](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/llvm/include/llvm/ExecutionEngine/Orc/Core.h#L1227) `Expected<JITDylib &> createJITDylib(std::string Name)` —— 创建并（若挂了 `Platform`）安装平台标准符号。
- [Core.h:L1307-L1312](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/llvm/include/llvm/ExecutionEngine/Orc/Core.h#L1307-L1312) 阻塞版 `lookup`：在给定搜索顺序里找一组符号，默认等到它们到达 `Ready` 才返回——这是「按需编译」的对外入口。
- [Core.h:L1317-L1319](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/llvm/include/llvm/ExecutionEngine/Orc/Core.h#L1317-L1319) 单符号便捷版 `lookup(SearchOrder, Symbol)`：`lli` 与 `LLJIT::lookup` 最终都落到这里。

`JITDylib` 是「符号容器 + 链接顺序」的组合体，定义见 [Core.h:L674-L686](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/llvm/include/llvm/ExecutionEngine/Orc/Core.h#L674-L686)。它的链接顺序（`JITDylibSearchOrder`）是一串 `(JITDylib*, 标志)` 对：

- [Core.h:L148-L149](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/llvm/include/llvm/ExecutionEngine/Orc/Core.h#L148-L149) 定义了搜索顺序的类型，正是普通链接里「 `-la -lb` 顺序」的 JIT 翻版。
- `addToLinkOrder` / `setLinkOrder`（[Core.h:L767-L800](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/llvm/include/llvm/ExecutionEngine/Orc/Core.h#L767-L800)）配置「本 dylib 引用的符号还可去哪些 dylib 找」，决定了跨模块符号解析的范围。

`ResourceTracker`（[Core.h:L63-L108](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/llvm/include/llvm/ExecutionEngine/Orc/Core.h#L63-L108)）则负责**资源归属与可移除性**：每个被加入的模块/对象都绑定到一个 tracker，将来可以按 tracker 粒度 `remove()` 或 `transferTo()`，从而安全地「卸载」已 JIT 的代码——这是 ORC v2 的招牌能力之一。

**(2) 「按需」如何落地：物化任务的派发**

`ExecutionSession::lookup` 的真正魔力在于：当它发现目标符号还处于「未物化」状态时，会把对应的 `MaterializationUnit`（符号定义的载体）排进一个待派发队列，然后由 `dispatchOutstandingMUs` 逐个派发成 `MaterializationTask`：

[`llvm/lib/ExecutionEngine/Orc/Core.cpp:L2033-L2057`](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/llvm/lib/ExecutionEngine/Orc/Core.cpp#L2033-L2057) 这段代码就是「按需编译」的心跳——循环取出待物化的 MU，包成 `MaterializationTask` 交给任务派发器：

```cpp
void ExecutionSession::dispatchOutstandingMUs() {
  while (true) {
    /* 取出 (MU, MR) 对 */
    ...
    dispatchTask(std::make_unique<MaterializationTask>(
        std::move(JMU->first), std::move(JMU->second)));   // L2053-L2054
  }
}
```

`MaterializationTask` 跑起来后，最终回调到 MU 所属层的 `emit`——于是编译才真正开始。这正是「查找即编译」的实现：`lookup` 不只是查表，它还会**驱动整条物化流水线**。

**(3) 两类层基类：`IRLayer` 与 `ObjectLayer`**

ORC 把「转换器」抽象成层，每层只暴露一个核心动作 `emit`。 [`llvm/include/llvm/ExecutionEngine/Orc/Layer.h:L68-L117`](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/llvm/include/llvm/ExecutionEngine/Orc/Layer.h#L68-L117) 定义了 `IRLayer`（吃 IR）：

```cpp
class IRLayer {
  /// 把代表给定 IR 的 MaterializationUnit 加到目标 JITDylib
  virtual Error add(ResourceTrackerSP RT, ThreadSafeModule TSM);
  /// 物化给定 IR —— 纯虚，子类（如 IRCompileLayer）实现具体编译
  virtual void emit(std::unique_ptr<MaterializationResponsibility> R,
                    ThreadSafeModule TSM) = 0;          // L110-L111
};
```

[`Layer.h:L134-L172`](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/llvm/include/llvm/ExecutionEngine/Orc/Layer.h#L134-L172) 定义了 `ObjectLayer`（吃目标文件），同样把工作浓缩成：

```cpp
class ObjectLayer {
  virtual Error add(ResourceTrackerSP RT, std::unique_ptr<MemoryBuffer> O, ...);
  /// 物化给定目标文件 —— 纯虚，子类（如 RTDyldObjectLinkingLayer）实现链接
  virtual void emit(std::unique_ptr<MaterializationResponsibility> R,
                    std::unique_ptr<MemoryBuffer> O) = 0;   // L167-L168
};
```

注意两种层的 `emit` 接受的第二参数不同：`IRLayer` 吃 `ThreadSafeModule`，`ObjectLayer` 吃装着目标文件的 `MemoryBuffer`。一个 IR 层的 `emit` 在产出目标文件后，把结果**下放**给一个对象层的 `emit`——这就是「洋葱」的层叠方式。

**(4) `IRCompileLayer`：IR → 目标文件**

`IRCompileLayer` 是 `IRLayer` 的子类，它持有一个 `IRCompiler` 函子（封装了 `TargetMachine`），在 `emit` 里把 IR 编译成目标文件，再交给底下的 `ObjectLayer`：

[`llvm/include/llvm/ExecutionEngine/Orc/IRCompileLayer.h:L32-L69`](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/llvm/include/llvm/ExecutionEngine/Orc/IRCompileLayer.h#L32-L69) 给出其接口，关键三处：

- [IRCompileLayer.h:L41](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/llvm/include/llvm/ExecutionEngine/Orc/IRCompileLayer.h#L41) `IRCompiler::operator()(Module&)` —— 纯虚，把模块编译成 `MemoryBuffer`（即目标文件）。`TMOwningSimpleCompiler`、`ConcurrentIRCompiler` 是它的两个现成实现。
- [IRCompileLayer.h:L53-L54](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/llvm/include/llvm/ExecutionEngine/Orc/IRCompileLayer.h#L53-L54) 构造函数：`IRCompileLayer(ES, ObjectLayer &BaseLayer, IRCompiler)` —— 它**持有一个下层 ObjectLayer 引用**，编译完就交给它。
- [IRCompileLayer.h:L60-L61](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/llvm/include/llvm/ExecutionEngine/Orc/IRCompileLayer.h#L60-L61) `emit` 的声明。

`emit` 的实现极简，是理解整个 JIT 数据流的最佳入口。 [`llvm/lib/ExecutionEngine/Orc/IRCompileLayer.cpp:L28-L45`](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/llvm/lib/ExecutionEngine/Orc/IRCompileLayer.cpp#L28-L45)：

```cpp
void IRCompileLayer::emit(std::unique_ptr<MaterializationResponsibility> R,
                          ThreadSafeModule TSM) {
  if (auto Obj = TSM.withModuleDo(*Compile)) {      // ① 用 IRCompiler 编译 IR -> 目标文件
    { /* 可选：通知已编译 */ }
    BaseLayer.emit(std::move(R), std::move(*Obj));  // ② 把目标文件下放给对象层链接
  } else {
    R->failMaterialization();                        // ③ 编译失败，标记物化失败
    getExecutionSession().reportError(Obj.takeError());
  }
}
```

三步非常清晰：①编译、②下放、③失败处理。这正是「层」的本质——**只做自己的那一段，其余交给邻居**。

> 小提示：这里的 `Compile` 本质是调用 `TargetMachine` 把 IR 跑一遍后端代码生成（u6 讲的那套），产出 `.o` 字节流。所以 JIT 复用的就是离线编译的后端，区别只在于产物**不落盘**，而直接进内存交给对象层。

**(5) `IRTransformLayer`：可选的 IR 变换钩子**

有时你想在「加入的 IR」被编译前先插一道处理（比如 `lli` 用来做 IR 校验与转储）。`IRTransformLayer` 就是这样一个透传层：它本身不编译，只对经过的 IR 跑一个用户函数，再原样交给下层 `IRLayer`：

[`llvm/include/llvm/ExecutionEngine/Orc/IRTransformLayer.h:L28-L51`](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/llvm/include/llvm/ExecutionEngine/Orc/IRTransformLayer.h#L28-L51)，其中 `identityTransform`（[L43-L46](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/llvm/include/llvm/ExecutionEngine/Orc/IRTransformLayer.h#L43-L46)）是默认的「什么都不做」变换，`setTransform` 可换掉它。我们在 4.2 会看到 LLJIT 把它叠了两层。

#### 4.1.4 代码实践

**实践目标**：用最少的命令亲手触发一次「按需编译」，验证「查找即编译」。

1. 准备一段极简 IR（存为 `add.ll`）：

   ```llvm
   define i32 @add1(i32 %x) {
   entry:
     %r = add i32 %x, 1
     ret i32 %r
   }
   define i32 @main() {
   entry:
     %r = call i32 @add1(i32 41)
     ret i32 %r            ; main 返回 42，lli 会把它作为退出码
   }
   ```

2. 用 `lli` 直接运行（`lli` 默认用 ORC JIT）：

   ```bash
   lli add.ll
   echo $?    # 预期：42
   ```

3. 观察它何时编译：再加一个 `-debug-only=orc`（需要带断言的构建）：

   ```bash
   lli -debug-only=orc add.ll
   ```

**需要观察的现象**：第 2 步能正确打印退出码 42，说明 IR 被现场编译并执行；第 3 步的调试日志里应能看到类似 *Dispatching MaterializationUnits...* 与 *Dispatching "<module>"* 的字样（对应 [Core.cpp:L2033-L2057](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/llvm/lib/ExecutionEngine/Orc/Core.cpp#L2033-L2057)），证明编译确发生在运行期而非预先。

**预期结果**：退出码为 42；若开了调试日志，能看到物化派发记录。

> 若本地没有可运行的 `lli`，以上结果**待本地验证**；可退而求其次，阅读 `lli.cpp` 中 `lookup(EntryFunc)` 与 `runAsMain` 两行（见 4.2.3）来理解等价流程。

#### 4.1.5 小练习与答案

**练习 1**：为什么说 ORC 是「按需」编译？如果你只 `addIRModule` 而从不 `lookup`，那段 IR 会被编译吗？

> **答案**：不会被编译。`addIRModule` 只把模块扫描成 `MaterializationUnit` 并把符号登记为「未物化」状态；真正的编译发生在 `lookup` 触发 `MaterializationTask`、回调到层的 `emit` 时。没人查，就没人编——这就是 On-Request。

**练习 2**：`IRLayer::emit` 和 `ObjectLayer::emit` 各自的输入是什么？为什么不同？

> **答案**：`IRLayer::emit` 输入是 `ThreadSafeModule`（一段 IR），`ObjectLayer::emit` 输入是 `MemoryBuffer`（一段目标文件字节）。不同是因为它们处于流水线的不同阶段：IR 层在「编译之前」，对象层在「编译之后、链接之时」。一个 IR 层 emit 完产出目标文件，正好喂给对象层 emit。

**练习 3**：`ExecutionSession` 与 `JITDylib` 各自管什么？二者关系是什么？

> **答案**：`ExecutionSession` 管整个会话的全局簿记（进程控制、符号池、查找状态机、物化派发、错误报告）；`JITDylib` 管一组符号定义及其链接顺序。一个 `ExecutionSession` 下可以挂多个 `JITDylib`，查找时按「搜索顺序」依次在这些 dylib 里找。

---

### 4.2 LLJIT 组件

#### 4.2.1 概念说明

4.1 讲的是「零件」。如果每次用 JIT 都要手写 `ExecutionSession`、手叠 `IRCompileLayer` + `ObjectLayer`，门槛太高。于是 ORC 提供了一个**预制的、开箱即用的 JIT 栈——`LLJIT`**：

- `LLJIT` 把「对象链接层 + 对象变换层 + IR 编译层 + 两层 IR 变换层」按固定顺序叠好，并自动建好 `ExecutionSession`、内存管理器、平台支持、主 `JITDylib`。
- 它的定位是 **MCJIT 的现代替代品**：默认 eager 编译（一查找就编译），适合作为「内存里的即时编译器」。
- 它的兄弟 `LLLazyJIT` 继承自 `LLJIT`，再叠一层 `CompileOnDemandLayer`，支持**惰性编译**（函数第一次被调用时才编译）。
- 两者都通过各自的 **Builder**（`LLJITBuilder` / `LLLazyJITBuilder`）创建，Builder 提供一系列 `setXxx` 链式方法来替换默认组件（自定义对象层、自定义编译器、编译线程数、平台……）。

官方文档 [`llvm/docs/ORCv2.md:L77-L122`](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/llvm/docs/ORCv2.md#L77-L122) 对二者有一段权威概述：`LLJIT` 用 `IRCompileLayer` + 对象链接层支持 IR 编译与可重定位目标文件链接，所有操作在符号查找时 eager 进行；`LLLazyJIT` 加 `CompileOnDemandLayer` 实现惰性编译。

#### 4.2.2 核心流程

`LLJIT` 从构造到使用的整体流程：

```text
LLJITBuilder().create()
   │  prepareForConstruction(): 探测宿主三元组、定 DataLayout、按架构选 JITLink/RTDyld、
   │                            建默认进程符号 dylib 与平台
   ▼
LLJIT 构造函数 (LLJIT.cpp L1004-L1099):
   ① ExecutionSession  ← SelfExecutorProcessControl(本进程执行)        L1011-L1022
   ② MemMgr / DylibMgr                                                 L1024-L1036
   ③ ObjLinkingLayer       = ObjectLinkingLayer 或 RTDyldObjectLinkingLayer  L1038-L1043
   ④ ObjTransformLayer(ES, ObjLinkingLayer)                             L1044-L1045
   ⑤ CompileLayer  = IRCompileLayer(ES, ObjTransformLayer, Compiler)    L1047-L1054
   ⑥ TransformLayer     = IRTransformLayer(ES, CompileLayer)            L1055
   ⑦ InitHelperTransformLayer = IRTransformLayer(ES, TransformLayer)    L1056-L1057
   ⑧ ProcessSymbols / Platform JITDylib + DefaultLinks                  L1063-L1091
   ⑨ Main = createJITDylib("main")                                     L1093-L1098
   ▼
使用：
   J->addIRModule(ThreadSafeModule)   # 进 ⑦ InitHelperTransformLayer   (LLJIT.cpp L918)
   J->lookup("foo")                   # 经 mangle -> ES.lookup          (LLJIT.cpp L936-L944)
   addr.toPtr<int(int)>()             # 拿到地址 -> 转函数指针 -> 调用
```

层叠方向（自顶向下，`addIRModule` 从最顶层喂入）：

```text
InitHelperTransformLayer  (IRTransformLayer)  ← 平台初始化扫描（如 global_ctors）
   └─ TransformLayer      (IRTransformLayer)  ← 用户自定义 IR 变换（lli 在此校验/转储）
        └─ CompileLayer    (IRCompileLayer)    ← IR -> 目标文件
             └─ ObjTransformLayer (ObjectTransformLayer) ← 目标文件变换
                  └─ ObjLinkingLayer            ← 目标文件 -> 可执行内存（JITLink / RTDyld）
```

物化时数据**从顶层流向底层**：IR 经两层变换 → 编译成目标文件 → 变换 → 链进内存。这与 4.1 讲的「`IRCompileLayer.emit` 下放给 `ObjectLayer.emit`」完全一致，只是 LLJIT 把栈叠得更完整。

#### 4.2.3 源码精读

**(1) `LLJIT` 的成员与构造：5 层是怎么叠的**

`LLJIT` 类定义在 [`llvm/include/llvm/ExecutionEngine/Orc/LLJIT.h:L44-L285`](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/llvm/include/llvm/ExecutionEngine/Orc/LLJIT.h#L44-L285)，它把这摞层作为成员持有（[LLJIT.h:L280-L284](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/llvm/include/llvm/ExecutionEngine/Orc/LLJIT.h#L280-L284)）：

```cpp
std::unique_ptr<ObjectLayer>       ObjLinkingLayer;        // 最底层
std::unique_ptr<ObjectTransformLayer> ObjTransformLayer;
std::unique_ptr<IRCompileLayer>    CompileLayer;
std::unique_ptr<IRTransformLayer>  TransformLayer;
std::unique_ptr<IRTransformLayer>  InitHelperTransformLayer; // 最顶层
```

并且提供 getter 让使用者拿到任意一层做定制（[LLJIT.h:L230-L240](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/llvm/include/llvm/ExecutionEngine/Orc/LLJIT.h#L230-L240)），例如 `lli` 就用 `getIRTransformLayer()` 注入一个校验+转储钩子。

真正的「叠层」发生在构造函数。 [`llvm/lib/ExecutionEngine/Orc/LLJIT.cpp:L1004-L1099`](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/llvm/lib/ExecutionEngine/Orc/LLJIT.cpp#L1004-L1099) 是本讲最关键的一段，逐层 new 出来并嵌套：

```cpp
LLJIT::LLJIT(LLJITBuilderState &S, Error &Err)
    : DL(std::move(*S.DL)), TT(S.JTMB->getTargetTriple()) {
  /* ① ExecutionSession（本进程执行） */                       // L1011-L1022
  /* ② MemMgr / DylibMgr */                                    // L1024-L1036
  /* ③ ObjLinkingLayer（按架构选 JITLink 或 RTDyld） */        // L1038-L1043
  ObjTransformLayer =
      std::make_unique<ObjectTransformLayer>(*ES, *ObjLinkingLayer);   // L1044-L1045
  {
    CompileLayer = std::make_unique<IRCompileLayer>(
        *ES, *ObjTransformLayer, std::move(*CompileFunction));          // L1053-L1054
    TransformLayer =
        std::make_unique<IRTransformLayer>(*ES, *CompileLayer);         // L1055
    InitHelperTransformLayer =
        std::make_unique<IRTransformLayer>(*ES, *TransformLayer);       // L1056-L1057
  }
  /* ⑧ ProcessSymbols / Platform / DefaultLinks */             // L1063-L1091
  /* ⑨ Main = createJITDylib("main") */                        // L1093-L1098
}
```

读这段代码时请关注两件事：

- 每一层构造时都**传入下一层的引用**（`IRCompileLayer(... *ObjTransformLayer ...)`、`IRTransformLayer(... *CompileLayer ...)`），这正是「洋葱」的物理体现——构造期就把层与层「焊」在一起。
- 默认有两个 `IRTransformLayer`：`TransformLayer`（给用户的变换钩子）和 `InitHelperTransformLayer`（给平台用来扫 `llvm.global_ctors` 等初始化符号）。`addIRModule` 走的是最顶层的 `InitHelperTransformLayer`。

**(2) 默认组件的选择：`prepareForConstruction`**

构造前，`LLJITBuilderState::prepareForConstruction`（[`LLJIT.cpp:L679-L856`](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/llvm/lib/ExecutionEngine/Orc/LLJIT.cpp#L679-L856)）负责把所有「没显式设置」的组件补上默认值：

- 探测宿主 `JITTargetMachineBuilder`（[L683-L692](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/llvm/lib/ExecutionEngine/Orc/LLJIT.cpp#L683-L692)）与默认 `DataLayout`（[L755-L760](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/llvm/lib/ExecutionEngine/Orc/LLJIT.cpp#L755-L760)）。
- **按目标架构自动选链接器**（[L798-L837](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/llvm/lib/ExecutionEngine/Orc/LLJIT.cpp#L798-L837)）：对 `riscv64`/`loongarch64`/`aarch64(非COFF)`/`x86_64(非COFF)` 等用新一代 **JITLink**（`ObjectLinkingLayer`），其余回退到经典的 **RuntimeDyld**（`RTDyldObjectLinkingLayer`）。这解释了「为什么同一个 LLJIT 在不同机器上底层链接器不同」。
- 建默认的「进程符号」dylib（[L841-L853](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/llvm/lib/ExecutionEngine/Orc/LLJIT.cpp#L841-L853)），让你 JIT 出来的代码能直接调用宿主进程里（如 `libc`）的符号。

两个工厂方法决定默认的对象层与编译器： [`createObjectLinkingLayer`](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/llvm/lib/ExecutionEngine/Orc/LLJIT.cpp#L953-L983)（[LLJIT.cpp:L953-L983](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/llvm/lib/ExecutionEngine/Orc/LLJIT.cpp#L953-L983)，默认造 `RTDyldObjectLinkingLayer`）与 [`createCompileFunction`](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/llvm/lib/ExecutionEngine/Orc/LLJIT.cpp#L985-L1002)（[LLJIT.cpp:L985-L1002](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/llvm/lib/ExecutionEngine/Orc/LLJIT.cpp#L985-L1002)：单线程用 `TMOwningSimpleCompiler`，多线程用 `ConcurrentIRCompiler`）。

**(3) 用法闭环：`addIRModule` 与 `lookup`**

`addIRModule`（[`LLJIT.cpp:L911-L923`](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/llvm/lib/ExecutionEngine/Orc/LLJIT.cpp#L911-L923)）先把 `DataLayout` 贴到模块上（`applyDataLayout`，[L1110-L1122](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/llvm/lib/ExecutionEngine/Orc/LLJIT.cpp#L1110-L1122)），再交给最顶层 `InitHelperTransformLayer->add`：

```cpp
Error LLJIT::addIRModule(ResourceTrackerSP RT, ThreadSafeModule TSM) {
  if (auto Err = TSM.withModuleDo([&](Module &M) { return applyDataLayout(M); }))
    return Err;
  return InitHelperTransformLayer->add(std::move(RT), std::move(TSM));  // L918
}
```

`lookup`（头文件 [LLJIT.h:L181-L188](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/llvm/include/llvm/ExecutionEngine/Orc/LLJIT.h#L181-L188) 的便捷重载）先把 IR 名做 mangling（链接器视角的名字，比如 C++ 的 `_Z3foo`），再调用 `lookupLinkerMangled`（[`LLJIT.cpp:L936-L944`](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/llvm/lib/ExecutionEngine/Orc/LLJIT.cpp#L936-L944)），后者正是 4.1 讲的 `ExecutionSession::lookup`：

```cpp
Expected<ExecutorAddr> LLJIT::lookupLinkerMangled(JITDylib &JD, SymbolStringPtr Name) {
  if (auto Sym = ES->lookup(makeJITDylibSearchOrder(&JD, ...), Name))  // L938-L940
    return Sym->getAddress();
  else
    return Sym.takeError();
}
```

`mangle` 的实现在 [LLJIT.cpp:L1101-L1108](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/llvm/lib/ExecutionEngine/Orc/LLJIT.cpp#L1101-L1108)，用 `Mangler` 按 `DataLayout` 加前缀。这就是为什么 `lookup` 接收的是「IR 名」，而 ORC 内部按「链接器 mangled 名」匹配。

**(4) 完整范例：`HowToUseLLJIT`**

官方最小示例 [`llvm/examples/HowToUseLLJIT/HowToUseLLJIT.cpp`](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/llvm/examples/HowToUseLLJIT/HowToUseLLJIT.cpp) 把上述闭环串成一段可运行程序。它先在内存里用 `IRBuilder` 造一个 `add1(x)=x+1` 的模块（[`HowToUseLLJIT.cpp:L41-L75`](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/llvm/examples/HowToUseLLJIT/HowToUseLLJIT.cpp#L41-L75)），然后在 `main` 里四步走完整个 JIT 流程（[`HowToUseLLJIT.cpp:L77-L101`](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/llvm/examples/HowToUseLLJIT/HowToUseLLJIT.cpp#L77-L101)）：

```cpp
InitializeNativeTarget();                           // 初始化本机后端
InitializeNativeTargetAsmPrinter();
auto J = ExitOnErr(LLJITBuilder().create());        // ① 建 LLJIT
auto M = createDemoModule();
ExitOnErr(J->addIRModule(std::move(M)));            // ② 加模块（此时不编译）

auto Add1Addr = ExitOnErr(J->lookup("add1"));       // ③ 查找 -> 触发编译
int (*Add1)(int) = Add1Addr.toPtr<int(int)>();      //    地址转函数指针

int Result = Add1(42);                              // ④ 调用 JIT 出来的函数
outs() << "add1(42) = " << Result << "\n";          //    打印 add1(42) = 43
```

它的构建脚本 [`llvm/examples/HowToUseLLJIT/CMakeLists.txt:L1-L12`](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/llvm/examples/HowToUseLLJIT/CMakeLists.txt#L1-L12) 声明了它链接的组件：

```cmake
set(LLVM_LINK_COMPONENTS
  Core
  OrcJIT        # ← ORC JIT 库
  Support
  nativecodegen # ← 本机后端代码生成（让 IRCompiler 能造出 TargetMachine）
  )
add_llvm_example(HowToUseLLJIT HowToUseLLJIT.cpp EXPORT_SYMBOLS)
```

> 注意 `nativecodegen`：JIT 要能编译，就必须有后端代码生成器在场。这正是「JIT 复用离线后端」在构建层面的体现。

**(5) `lli` 是怎么用 LLJIT 的**

`lli` 工具（[`llvm/tools/lli/lli.cpp`](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/llvm/tools/lli/lli.cpp)）是 LLJIT 的「重型」用户。它默认用 ORC（[lli.cpp:L109-L115](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/llvm/tools/lli/lli.cpp#L109-L115) 的 `JITKind` 枚举，默认 `Orc`），用 `LLLazyJITBuilder` 构造（[lli.cpp:L935](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/llvm/tools/lli/lli.cpp#L935)），按命令行参数定制目标、链接器、平台、编译线程数（[lli.cpp:L937-L1043](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/llvm/tools/lli/lli.cpp#L937-L1043)），然后：

```cpp
auto J = ExitOnErr(Builder.create());                               // L1045
/* 注入 IR 校验/转储、对象转储等到两层 TransformLayer */             // L1068-L1088
ExitOnErr(AddModule(J->getMainJITDylib(), std::move(MainModule)));  // L1112 加主模块
ExitOnErr(J->initialize(J->getMainJITDylib()));                     // L1160 跑静态构造器
auto MainAddr = ExitOnErr(J->lookup(EntryFunc));                    // L1173 查找 main -> 编译
auto MainFn = MainAddr.toPtr<MainFnTy *>();
int Result = orc::runAsMain(MainFn, InputArgv, ...);                // L1175 执行 main
ExitOnErr(J->deinitialize(J->getMainJITDylib()));                   // L1182 跑静态析构器
```

这段代码几乎就是 `HowToUseLLJIT` 的「加强版」：多出 `initialize`/`deinitialize`（运行 `llvm.global_ctors`/`dtors`）与 `-thread-entry` 等高级特性，但核心闭环（建栈 → 加模块 → 查找 → 调用）完全一致。

**(6) 惰性编译：`LLLazyJIT`**

若希望「函数第一次被调用时才编译」，用 `LLLazyJIT`。它在 LLJIT 之上再加两层（[`LLJIT.cpp:L1323-L1369`](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/llvm/lib/ExecutionEngine/Orc/LLJIT.cpp#L1323-L1369)）：

```cpp
IPLayer  = std::make_unique<IRPartitionLayer>(*ES, *InitHelperTransformLayer); // L1361
CODLayer = std::make_unique<CompileOnDemandLayer>(
    *ES, *IPLayer, *LCTMgr, std::move(ISMBuilder));                            // L1364-L1365
```

`CompileOnDemandLayer` 会把模块里每个函数的对外调用替换成一个**跳板（call-through stub）**：调用跳板时，若该函数尚未编译，则现场编译再跳转。`addLazyIRModule`（[LLJIT.cpp:L1305-L1313](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/llvm/lib/ExecutionEngine/Orc/LLJIT.cpp#L1305-L1313)）就是把模块喂给 `CODLayer` 而非顶层 `InitHelperTransformLayer`。`lli -jit-kind=orc-lazy` 即走这条路径（[lli.cpp:L94](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/llvm/tools/lli/lli.cpp#L94)、[L1107](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/llvm/tools/lli/lli.cpp#L1107)）。

#### 4.2.4 代码实践

**实践目标**：把官方 `HowToUseLLJIT` 示例亲手构建运行一次，确认「用 LLJIT 把一个 IR 模块编译并执行得到结果」。

> 前提：你需要一个已配置好的 LLVM 构建目录（见 u1-l3），且配置时启用了 `LLVM_BUILD_EXAMPLES`。

1. 在构建目录下构建该示例（示例位于 `llvm/examples/HowToUseLLJIT`）：

   ```bash
   cmake --build <build-dir> --target HowToUseLLJIT
   ```

2. 运行它：

   ```bash
   <build-dir>/bin/HowToUseLLJIT
   ```

3.（进阶）对照源码改造：仿照 [HowToUseLLJIT.cpp:L41-L75](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/llvm/examples/HowToUseLLJIT/HowToUseLLJIT.cpp#L41-L75)，把 `add1(x)=x+1` 改成 `double_mul(a,b)=a*b*2.0`，重新构建运行。

**需要观察的现象**：第 2 步应打印 `add1(42) = 43`；第 3 步改造后调用 `mul(3.0, 5.0)` 应得到 `30.000000`。

**预期结果**：示例可成功编译 IR 并执行得到正确数值，证明 LLJIT 闭环可用。

> 若本地不便构建（`HowToUseLLJIT` 未编入），以上结果**待本地验证**。可退而采用**源码阅读型实践**：打开 [HowToUseLLJIT.cpp](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/llvm/examples/HowToUseLLJIT/HowToUseLLJIT.cpp)，逐行标注 `LLJITBuilder().create()`、`addIRModule`、`lookup`、`toPtr` 四步各自对应 4.2.2 流程图的哪个环节，以此替代实跑。

#### 4.2.5 小练习与答案

**练习 1**：`LLJIT` 构造函数里，5 个层是按什么顺序「焊」在一起的？`addIRModule` 把模块喂给哪一层？

> **答案**：从底向上焊——先 `ObjLinkingLayer`，再依次 `ObjTransformLayer(包ObjLinkingLayer)` → `IRCompileLayer(包ObjTransformLayer)` → `TransformLayer(包CompileLayer)` → `InitHelperTransformLayer(包TransformLayer)`。`addIRModule` 喂给最顶层的 `InitHelperTransformLayer`（见 [LLJIT.cpp:L918](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/llvm/lib/ExecutionEngine/Orc/LLJIT.cpp#L918)）。物化时数据从顶层一路下放到底层。

**练习 2**：`LLJIT::lookup("foo")` 为什么要先 `mangle`？直接用 `"foo"` 查不行吗？

> **答案**：ORC 内部按**链接器视角的 mangled 名**登记符号（C++ 会加 `_Z` 前缀等，C 在 Itanium ABI 下也会按 `DataLayout` 加前缀）。用户给的是 IR 源码名 `"foo"`，必须先经 `mangle`（[LLJIT.cpp:L1101-L1108](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/llvm/lib/ExecutionEngine/Orc/LLJIT.cpp#L1101-L1108)）转成 mangled 名再去匹配，否则查不到。`lookupLinkerMangled` 则假定调用者已自行 mangle。

**练习 3**：`LLJIT` 与 `LLLazyJIT` 的核心区别是什么？后者多出哪两层？

> **答案**：`LLJIT` 是 eager——一查找就把整个模块相关代码编译出来；`LLLazyJIT` 是 lazy——把对外调用换成跳板，函数首次被调用时才编译。后者在 LLJIT 之上多了 `IRPartitionLayer`（模块分区）与 `CompileOnDemandLayer`（按需编译 + 跳板），见 [LLJIT.cpp:L1361-L1365](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/llvm/lib/ExecutionEngine/Orc/LLJIT.cpp#L1361-L1365)。

---

## 5. 综合实践

把 4.1 的「按需编译」与 4.2 的「LLJIT 闭环」串起来，完成下面这个**端到端追踪任务**。

**任务**：以 `lli add.ll`（4.1.4 那份 IR）为对象，画出从「敲下回车」到「退出码 42」之间的完整调用链，要求标注每一步发生在哪一层、对应哪个源码位置。建议按下面的骨架补全：

```text
lli main()
  → LLLazyJITBuilder().create()        # 建 JIT 栈          lli.cpp:L1045
  → AddModule(main)                    # 加模块             lli.cpp:L1112
       └─ LLJIT::addIRModule           # 贴 DataLayout + 顶层 add   LLJIT.cpp:L911-L918
  → J->lookup("main")                  # 查找 -> 触发编译   lli.cpp:L1173
       └─ LLJIT::lookupLinkerMangled   # mangle + ES.lookup LLJIT.cpp:L936-L944
            └─ ExecutionSession::lookup # 派发物化任务       Core.cpp:L2033-L2057
                 └─ InitHelperTransformLayer.emit → TransformLayer.emit
                      └─ IRCompileLayer.emit  # IR -> 目标文件  IRCompileLayer.cpp:L28-L45
                           └─ ObjLinkingLayer.emit  # 目标文件 -> 内存
  → MainAddr.toPtr<MainFnTy*>()        # 地址转指针         lli.cpp:L1174
  → orc::runAsMain(MainFn, ...)        # 执行 main          lli.cpp:L1175
  → 退出码 42
```

**交付物**：

1. 一张标注了「层名 + 源码行号」的调用链图（可文字描述）。
2. 用一句话回答：在这条链里，**编译具体发生在哪一步**？为什么说前面的 `addIRModule` 步骤「其实没编译」？
3.（可选）开启 `-debug-only=orc` 再跑一次，把你日志里看到的 *Dispatching MaterializationUnits* 与上面调用链的对应步骤圈出来。

> 预期结论：编译发生在 `lookup` 触发的物化派发那一步（`ExecutionSession::lookup` → `dispatchOutstandingMUs` → `IRCompileLayer::emit`）；`addIRModule` 仅登记符号为「未物化」，所以没编译。

## 6. 本讲小结

- **JIT = 运行期现用现编译**：LLVM 的 JIT 把 IR 不落盘地编译成机器码、链进可执行内存并立刻执行，由 `ExecutionEngine` 抽象承载，现代实现是 ORC v2。
- **On-Request 的本质是「查找即编译」**：`addIRModule` 只把符号登记为未物化；`lookup` 才回溯定义、派发 `MaterializationTask`、回调层的 `emit` 真正产出代码（[Core.cpp:L2033-L2057](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/llvm/lib/ExecutionEngine/Orc/Core.cpp#L2033-L2057)）。
- **核心层与层分离**：`ExecutionSession` + `JITDylib` 管符号簿记与状态机；`IRLayer`/`ObjectLayer` 两类层管具体编译/链接，每层只暴露一个 `emit`，沿层向下流动（[Layer.h:L68-L172](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/llvm/include/llvm/ExecutionEngine/Orc/Layer.h#L68-L172)）。
- **`IRCompileLayer` 是数据流枢纽**：`emit` 三步——编译 IR、下放给对象层、失败处理（[IRCompileLayer.cpp:L28-L45](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/llvm/lib/ExecutionEngine/Orc/IRCompileLayer.cpp#L28-L45)），它复用的就是离线后端的 `TargetMachine`。
- **`LLJIT` 是预制栈**：构造函数把对象链接层、对象变换层、IR 编译层、两层 IR 变换层按固定顺序焊好（[LLJIT.cpp:L1004-L1099](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/llvm/lib/ExecutionEngine/Orc/LLJIT.cpp#L1004-L1099)），`LLJITBuilder` 负责配置，是 MCJIT 的现代替代。
- **用法四步闭环**：`LLJITBuilder().create()` → `addIRModule` → `lookup` → `toPtr` 调用；`lli` 与 `HowToUseLLJIT` 都遵循它，`LLLazyJIT` 在其上叠加按需编译。

## 7. 下一步学习建议

- **继续本单元**：u8-l2 讲 **LTO（链接时优化）**，它和 JIT 共享「跨模块分析、按需物化」的思想（ThinLTO 的 `ModuleSummaryIndex` 与 ORC 的 lazy materialization 异曲同工）；u8-l3 讲 **LLD 链接器**，本讲最底层的「对象链接层」正是把 LLD/JITLink 的链接能力搬进内存。
- **深入 ORC 细节**：若想自定义层或做跨进程 JIT，阅读 `llvm/docs/ORCv2.md` 全文，以及 `llvm/include/llvm/ExecutionEngine/Orc/` 下的 `ObjectLinkingLayer.h`、`CompileOnDemandLayer.h`、`ExecutorProcessControl.h`。
- **回看后端**：本讲的 `IRCompiler` 实际调用 `TargetMachine` 跑后端。建议结合 u6-l1（后端流水线）与 u6-l4（MC 层）理解「目标文件字节流」是怎么产出的。
- **动手方向**：仿照 `HowToUseLLJIT`，写一个读取 `.bc` 文件、用 LLJIT 执行其中指定函数的小工具，作为本讲的延伸实践。

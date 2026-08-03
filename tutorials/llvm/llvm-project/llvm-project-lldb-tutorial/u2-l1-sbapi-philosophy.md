# SBAPI 设计哲学

## 1. 本讲目标

学完本讲后，你应当能够：

- 说清楚 LLDB 的「公共 C++ API」指的是什么，以及它为什么必须保持 **ABI（二进制接口）稳定**。
- 复述 SB* 类的**五条设计规则**：不可继承、无虚函数、单一成员、可默认构造、便于 SWIG 绑定。
- 区分 `lldb`（公共 API 命名空间）与 `lldb_private`（内部实现命名空间），理解 SB 对象作为**轻量代理（proxy）**指向内部对象的关系。
- 能够打开任意一个 SB 类的头文件，验证它确实只有「一个成员变量」且「没有基类、没有虚函数」。

本讲只讲「设计哲学」，不深入任何具体 SB 类的业务方法。具体对象模型（SBTarget/SBProcess/SBThread/SBFrame/SBValue 的导航关系）留到 [u2-l3 核心对象模型](u2-l3-sb-object-model.md)。

---

## 2. 前置知识

阅读本讲前，建议你已了解（来自 [u1-l1](u1-l1-project-overview.md) 与 [u1-l2](u1-l2-directory-map.md)）：

- LLDB 既是一个**可执行程序**（命令行 `lldb`），也是一个**可复用的库**（`liblldb`）。
- 源码分两层：`include/lldb/`（头文件）与 `source/`（实现），二者目录结构高度镜像。
- `lldb` 命令行、Python 脚本、Lua 脚本、IDE 集成（lldb-dap）**共用同一套 API**。

为了让读者顺利理解本讲，先解释两个关键术语：

- **ABI（Application Binary Interface，二进制接口）**：决定「已编译好的库」与「使用它的程序」能否在二进制层面兼容。如果库的类布局（成员变量数量、大小、是否有虚表指针）发生变化，已经编译链接好的旧程序就可能崩溃。相比之下 **API（源码接口）** 只是头文件里的函数签名，改了 API 重新编译就行，而改了 ABI 会让旧二进制失效。
- **SWIG（Simplified Wrapper and Interface Generator）**：一个工具，能根据 C/C++ 头文件**自动生成** Python、Lua 等语言的绑定代码。LLDB 用 SWIG 把 C++ 的 SB API 翻译成 Python 里的 `import lldb` 模块。

理解了这两点，你就会明白为什么 LLDB 对 SB 类定下那么严格的规矩——它们都是为了「**让 `liblldb` 升级后，旧客户端（无论是 C++ 程序还是 Python 脚本）不必重新编译/重装也能继续工作**」。

---

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
| --- | --- |
| `docs/resources/sbapi.md` | SB API 的官方设计规范文档，五条规则与各种约束的权威出处。 |
| `docs/resources/overview.md` | LLDB 总体架构概述，其中「API」一节用列表重述了 SB 类的约束。 |
| `include/lldb/API/SBDebugger.h` | 公共类 `SBDebugger` 的头文件，用来验证「无基类、单成员」。 |
| `include/lldb/API/SBTarget.h` | 公共类 `SBTarget` 的头文件，本讲实践的主观查对象。 |
| `include/lldb/lldb-forward.h` | 集中存放所有 `lldb_private` 内部类的**前向声明**，是 SB 类能持有内部对象指针的关键。 |
| `source/API/SBTarget.cpp` | `SBTarget` 的实现文件，展示「薄壳代理」如何把调用转交给内部对象。 |

---

## 4. 核心概念与源码讲解

### 4.1 公共 API 与内部实现的分层

#### 4.1.1 概念说明

LLDB 把代码分成两个 C++ 命名空间：

- `lldb`：**公共 API**。所有以 `SB` 开头的类（`SBDebugger`、`SBTarget`、`SBProcess`…）都在这里，对应 `include/lldb/API/` 下的头文件。这些类承诺 **ABI 稳定**，外部客户端可以放心链接。
- `lldb_private`：**内部实现**。真正干活的对象（`Debugger`、`Target`、`Process`、`Thread`、`ValueObject`…）都在这里，对应 `source/` 下的各模块。它们**不保证**布局稳定，随时可能被重构。

打个比方：`SB*` 类像是银行的「营业柜台」，对外提供稳定的服务窗口；`lldb_private::*` 是柜台背后的「金库与业务系统」，可以不断升级改造，只要柜台接口不变，客户就感知不到。

#### 4.1.2 核心流程

一次典型的 SBAPI 调用链路是：

1. 客户端调用某个 SB 方法，例如 `sbTarget.LaunchSimple(...)`。
2. SB 方法在实现文件（`.cpp`）里，先做参数校验与「是否有效」判断。
3. 通过自己持有的内部对象指针，调用对应的 `lldb_private::Target` 方法。
4. 内部对象完成真正的逻辑（如启动进程）。
5. 结果被封装回 SB 对象（如 `SBProcess`）返回给客户端。

关键点：**SB 类不实现复杂算法，只做「转发 + 封装」**。官方文档明确建议：如果你发现自己在 SB 方法里写了大段算法，就应该把它挪到 `lldb_private` 里去。

#### 4.1.3 源码精读

`docs/resources/overview.md` 的「API」一节开宗明义地给出了约束动机：

[docs/resources/overview.md:9-13](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/docs/resources/overview.md#L9-L13) —— 说明 `API` 文件夹是 LLDB 对外的公共接口，并指出「为了能持续给 API 增加方法且不破坏已链接的程序」，必须遵守若干规则。

紧接着的列表就是规则本身：

[docs/resources/overview.md:17-24](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/docs/resources/overview.md#L17-L24) —— 公共 API 类的四条核心约束：不可继承、无虚函数、可被 SWIG 这类脚本桥接工具处理、轻量且由单一成员支撑（首选指针/shared_ptr）。

而 `sbTarget.cpp` 里就能看到这种「转发」的真实样子。比如附加进程时，SB 方法只是上锁、校验，然后把活交给内部 `target.Attach`：

[source/API/SBTarget.cpp:84-102](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/source/API/SBTarget.cpp#L84-L102) —— `AttachToProcess` 辅助函数：先取 `target.GetAPIMutex()` 加锁保证线程安全，校验已有进程状态，最后调用内部 `target.Attach(...)`。SB 层只负责「锁与转发」，真正的附加逻辑在内部类里。

> 小贴士：这里出现的 `GetAPIMutex()` 是 SBTarget 的公共方法（见 [SBTarget.h:1013](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/include/lldb/API/SBTarget.h#L1013)），它让外部客户端也能用同一把递归互斥锁串行化对某个 Target 的操作，避免多线程并发踩坏内部状态。

#### 4.1.4 代码实践

这是一个**源码阅读型实践**，无需运行 LLDB：

1. **目标**：亲身验证「SB 方法只做转发」的说法。
2. **步骤**：打开 `source/API/SBTarget.cpp`，找到 `SBTarget::LaunchSimple` 的实现，阅读它如何构造 `SBLaunchInfo` 并最终调用内部 `Target::Launch`。
3. **观察**：注意方法体内是否包含「真正启动一个 OS 进程」的系统调用——你会发现没有，那些都在内部类里。
4. **预期结果**：你应能说出 SBLaunchInfo 到内部 Launch 之间的那一层「封装/转发」。
5. 无法本地确认的部分请标注「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 LLDB 不直接让外部客户端使用 `lldb_private::Target`？

> **答案**：因为 `lldb_private::Target` 的成员布局不保证稳定，LLDB 内部重构会改变它；如果外部直接依赖，每次升级都要重新编译，且二进制层面会直接崩溃。用 `SBTarget` 做稳定代理，可以把内部变化隔离掉。

**练习 2**：`SBTarget::GetAPIMutex()` 的存在说明 SB 层关心什么问题？

> **答案**：说明 SB 层关心**线程安全**。多个客户端线程可能并发操作同一个 Target，公共 API 通过一把递归互斥锁把对这些内部状态的访问串行化，避免数据竞争。

---

### 4.2 SB 类的五条设计规则

#### 4.2.1 概念说明

官方在 `docs/resources/sbapi.md` 中为 SB 类立下了若干规矩。我们可以把它归纳成**五条核心规则**，它们共同服务于一个目标：**ABI 稳定 + 易于 SWIG 绑定**。

| 规则 | 内容 | 为什么 |
| --- | --- | --- |
| ① 命名 | 类名 `SB<Name>`，方法名首字母大写驼峰 | 统一风格，便于工具与文档识别 |
| ② 不可继承 | SB 类不能有基类 | 继承会引入基类布局，改变对象大小 |
| ③ 无虚函数 | 方法都不能是 `virtual` | 虚函数需要虚表指针（vtable），改虚函数会破坏 ABI |
| ④ 单一成员 | 类只有一个成员变量（指针类） | 成员数量/类型一变，对象大小就变，ABI 就破 |
| ⑤ 可默认构造 | 必须有无参默认构造函数 | SWIG 与脚本语言需要能构造「空」对象 |

此外还有两条辅助约束：头文件里**不放内联实现**（实现都在 `.cpp`），且**不直接访问成员变量**（通过方法）。

#### 4.2.2 核心流程

这几条规则协同作用的逻辑链是：

1. 因为「无基类 + 无虚函数 + 单一成员」，SB 类在内存里**就是一个指针大小**，布局永远不变。
2. 因为布局不变，给 SB 类**新增非虚方法**时，只是多了一个符号，旧的已编译客户端照常工作——这正是 overview.md 说的「只是动态加载器多一次符号查找，不影响类布局」。
3. 因为「可默认构造 + 单一指针成员」，所有 SB 对象都能以「空」状态存在，方便 SWIG 生成绑定。
4. 因为「实现都在 `.cpp`」，头文件只暴露声明，内部细节不泄漏到公共接口。

#### 4.2.3 源码精读

权威出处是 `docs/resources/sbapi.md`：

[docs/resources/sbapi.md:11-18](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/docs/resources/sbapi.md#L11-L18) —— 明确规定：所有 SB API 类都是「non-virtual, single inheritance classes」（非虚、单继承——这里「单继承」实际指不参与继承体系）；只应 include `SBDefines.h` 等头文件；头文件里不要内联实现；不要直接访问 ivar（成员变量）。

`overview.md` 用更直白的列表复述了这些规则，并解释了它们与「动态加载器符号查找」的关系：

[docs/resources/overview.md:26-29](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/docs/resources/overview.md#L26-L29) —— 总结：遵守这些规则后，新增方法对类布局无影响（非虚、无新成员），因此可以持续给 API 加方法而不破坏已链接的程序。

现在用真实类验证。看 `SBTarget` 的声明——注意它没有基类：

[include/lldb/API/SBTarget.h:38-39](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/include/lldb/API/SBTarget.h#L38-L39) —— `class LLDB_API SBTarget {`，紧跟花括号，没有任何 `: public ...` 基类，满足规则②。

再看它的唯一成员：

[include/lldb/API/SBTarget.h:1081-1083](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/include/lldb/API/SBTarget.h#L1081-L1083) —— `private:` 区域只有一个 `lldb::TargetSP m_opaque_sp;`，即一个指向内部 `Target` 的共享指针。整个类就这一个成员变量，满足规则④。

`SBDebugger` 同样如此：

[include/lldb/API/SBDebugger.h:45-46](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/include/lldb/API/SBDebugger.h#L45-L46) 与 [include/lldb/API/SBDebugger.h:708-708](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/include/lldb/API/SBDebugger.h#L708-L708) —— 无基类，唯一成员 `lldb::DebuggerSP m_opaque_sp;`。

> 关于「无虚函数」（规则③）：你可以在这两个头文件里全文搜索 `virtual`，会发现一个都没有。所有方法都是普通非虚成员函数。

#### 4.2.4 代码实践

1. **目标**：亲手核对 SBTarget 的五条规则。
2. **步骤**：
   - 打开 `include/lldb/API/SBTarget.h`。
   - 搜索 `virtual`（应为 0 处）。
   - 确认 `class LLDB_API SBTarget {` 后无基类。
   - 滚到文件末尾的 `private:` 段，数一数成员变量个数（应为 1 个 `m_opaque_sp`）。
   - 确认有无默认构造函数 `SBTarget();`（在第 52 行附近）。
3. **观察**：你会看到 `protected:` 与 `private:` 里大多是 `friend` 声明和取/设指针的辅助方法，真正的数据成员只有 `m_opaque_sp` 一个。
4. **预期结果**：用一句话总结「SBTarget 满足全部五条规则」。
5. 若你在改过的本地分支上观察，结果可能不同，请以实际为准并标注。

#### 4.2.5 小练习与答案

**练习 1**：如果有人给 `SBTarget` 加了一个 `virtual ~SBTarget();`，会破坏什么？

> **答案**：会引入虚表指针，改变对象内存布局（多出一个隐藏的 vtable 指针成员），从而破坏 ABI——已编译链接到旧布局的客户端会读错内存偏移。所以规则③禁止虚函数。

**练习 2**：为什么规则④要求「单一成员」而不是「少量成员」？

> **答案**：只要成员数量或类型可变，对象大小就可能变，ABI 就可能破。而「单一指针成员」把对象大小恒定钉死为一个指针，内部对象怎么变都通过指针间接访问，公共对象的布局永远不变。

---

### 4.3 单成员代理模式：以共享指针为后端

#### 4.3.1 概念说明

SB 类既然只有一个成员，那它怎么承载复杂功能？答案是**代理（proxy）模式**：

- SB 对象持有一个指向 `lldb_private` 内部对象的**指针**（`pointer`）、**共享指针**（`shared_ptr`）或**独占指针**（`unique_ptr`）。
- 所有 SB 方法都通过这个指针去操作内部对象。
- 由于指针大小固定，内部对象（真正的「重量级」对象，含大量字段）无论怎么变化，都不会影响 SB 对象的内存布局。

这就是 overview.md 所说的「Pointers (or shared pointers) are the preferred choice since they allow changing the contents of the backend without affecting the public object layout」。

#### 4.3.2 核心流程

SB 对象与其内部后端的关系：

```
┌─────────────┐      shared_ptr      ┌────────────────────────┐
│  SBTarget   │  ─────────────────►  │  lldb_private::Target  │
│ m_opaque_sp │   (唯一的成员)        │  (重量级，可任意演化)    │
└─────────────┘                      └────────────────────────┘
   公共、稳定                              内部、可变
```

复制一个 SB 对象，只是**复制了共享指针**（引用计数 +1），并不复制内部对象。因此 SB 对象拷贝代价极低，且多个 SB 对象可以指向同一个内部对象。这也解释了为什么 SB 类大量提供拷贝构造和赋值运算符。

#### 4.3.3 源码精读

SBTarget 的拷贝构造就是把内部的共享指针搬过来：

[source/API/SBTarget.cpp:107-109](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/source/API/SBTarget.cpp#L107-L109) —— 拷贝构造 `SBTarget::SBTarget(const SBTarget &rhs) : m_opaque_sp(rhs.m_opaque_sp)`，仅仅是共享指针的拷贝（引用计数自增），非常轻量。

赋值运算符同理，先判自赋值，再拷指针：

[source/API/SBTarget.cpp:115-120](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/source/API/SBTarget.cpp#L115-L120) —— `operator=` 仅在 `this != &rhs` 时执行 `m_opaque_sp = rhs.m_opaque_sp;`。

而要让 SB 类能持有内部对象的指针，这些内部类必须**只声明不定义**地出现在公共头里。LLDB 把所有这种前向声明集中放在 `lldb-forward.h`：

[include/lldb/lldb-forward.h:76-76](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/include/lldb/lldb-forward.h#L76-L76) —— `class Debugger;` 仅前向声明，不暴露任何成员，公共头因此不会泄漏内部布局。

[include/lldb/lldb-forward.h:248-248](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/include/lldb/lldb-forward.h#L248-L248) —— `class Target;` 同理。`sbapi.md` 明确要求：若 SB 类要包装一个不在 `lldb-forward.h` 里的内部类，应**加到那里**，而不是在 SB 头里自行声明。

**当 SB 类需要额外状态时**，用「Impl 类」技巧。`sbapi.md` 以 `SBValue` 为例：

[docs/resources/sbapi.md:30-38](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/docs/resources/sbapi.md#L30-L38) —— 若 SB 类除后端对象外还需要自己的状态，不要直接加成员，而是在 `.cpp` 里定义一个 `Impl` 类，让 SB 对象持有指向 Impl 的指针。这样 Impl 可以后续增加成员而不改变 SB 对象大小。文中点名 `SBValue` 就是这种做法。

在 `SBValue.h` 里能看到这个原则的落地——它依旧只有一个成员，类型是指向 `ValueImpl` 的共享指针：

[include/lldb/API/SBValue.h:538-539](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/include/lldb/API/SBValue.h#L538-L539) —— `typedef std::shared_ptr<lldb_private::ValueImpl> ValueImplSP; ValueImplSP m_opaque_sp;`。即便 SBValue 需要记录「是否使用动态类型」「是否使用合成器」等额外状态，这些都被收纳进 `ValueImpl` 内部，SBValue 对外仍只占一个指针。

#### 4.3.4 代码实践

1. **目标**：体会「拷贝 SB 对象 = 拷贝一个指针」。
2. **步骤**：阅读 `source/API/SBTarget.cpp` 的拷贝构造（L107-109）和赋值（L115-120），对比一下若 SBTarget 直接持有 `Target`（而非指针）时，拷贝会发生什么。
3. **观察**：当前实现只动了一行 `m_opaque_sp = ...`，没有任何深拷贝逻辑。
4. **预期结果**：你能解释为什么「多个 SBTarget 副本共享同一个内部 Target」既安全又高效。
5. 进阶可选：在 `lldb-forward.h` 里数一数有多少个内部类被前向声明，体会这套机制覆盖范围之广。

#### 4.3.5 小练习与答案

**练习 1**：为什么 SBValue 把额外状态放进 `ValueImpl` 而不是直接加成员？

> **答案**：直接加成员会改变 SBValue 的大小，破坏 ABI。把状态收进 `ValueImpl`（由 SBValue 以指针持有），SBValue 仍是一个指针大小，`ValueImpl` 内部如何演化都不影响公共对象的布局。

**练习 2**：`lldb-forward.h` 为什么集中做前向声明，而不是各 SB 头里各自声明？

> **答案**：集中声明可以避免重复与不一致，并保证公共头里出现的内部类都只用「不透明指针」引用——既然只前向声明，SB 头就不会意外泄漏内部类的成员定义，从而守住 ABI 边界。

---

### 4.4 默认构造与 IsValid 有效性约定

#### 4.4.1 概念说明

为了让 SWIG 能生成 Python/Lua 绑定，**每个 SB 类都必须能被默认构造（无参构造出对象）**。但默认构造出的对象没有真正的内部后端——它是一个「空」对象。

于是 LLDB 约定：

- 每个 SB 类都提供 `IsValid()` 方法（或 `explicit operator bool()`），用来判断对象是否真的指向了有效后端。
- 在调用任何业务方法前，应该先判断 `IsValid()`，否则要能优雅地处理「空指针后端」。

> 例外：`SBError` 即便为空也是「有效」的——空状态本身就表示「成功」（无错误），因此它不需要后端对象也能给出有意义信息。这是 `sbapi.md` 特别说明的细节。

#### 4.4.2 核心流程

空对象的生命周期：

1. 默认构造 → `m_opaque_sp` 为空 → `IsValid()` 返回 `false`。
2. 调用某个工厂方法（如 `SBDebugger::CreateTarget`）→ 得到一个 `m_opaque_sp` 非空的对象 → `IsValid()` 返回 `true`。
3. 客户端在任何方法里都要先检查后端指针是否为空，为空则返回合理默认值（如返回一个无效的 SB 对象或空字符串）。

#### 4.4.3 源码精读

`sbapi.md` 解释了为什么需要默认构造，以及 `IsValid` 的由来：

[docs/resources/sbapi.md:40-45](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/docs/resources/sbapi.md#L40-L45) —— 因为所有成员都是指针，可以轻易默认构造；但这要求所有方法都准备好处理后端指针为空的情况，并「做点合理的事」。因此每个 SB 类都有一个 `IsValid` 方法报告对象是否为空。

关于 `SBError` 的特殊语义：

[docs/resources/sbapi.md:47-57](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/docs/resources/sbapi.md#L47-L57) —— 提示框说明：对大多数类，空意味着不应再调用其它方法；但 `SBError` 例外，它无需后端对象即可表示「成功」。

在 `SBDebugger.h` 中能看到典型的 `IsValid` 声明与默认构造：

[include/lldb/API/SBDebugger.h:60-61](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/include/lldb/API/SBDebugger.h#L60-L61) —— 注释「Default constructor creates an invalid SBDebugger instance」明确说明默认构造得到的是一个**无效**实例。

[include/lldb/API/SBDebugger.h:180-180](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/include/lldb/API/SBDebugger.h#L180-L180) —— `bool IsValid() const;` 提供有效性查询。

#### 4.4.4 代码实践

1. **目标**：验证「默认构造 → 无效」的约定。
2. **步骤**：在 `include/lldb/API/SBDebugger.h` 中找到默认构造（约第 61 行）和 `IsValid`（第 180 行）；然后到 `source/API/SBDebugger.cpp` 看 `IsValid()` 的实现，确认它就是判断 `m_opaque_sp` 是否为空。
3. **观察**：你会发现 `IsValid()` 本质上是 `return (bool)m_opaque_sp;` 之类。
4. **预期结果**：你能说出「`SBDebugger dbg;` 之后 `dbg.IsValid()` 返回 false，必须 `dbg = SBDebugger::Create(true);` 才有效」。
5. 实际实现细节若与上述推断不符，请以源码为准并标注「待本地验证」。

#### 4.4.5 小练习与答案

**练习 1**：为什么 SWIG 要求 SB 类必须默认构造？

> **答案**：脚本语言在绑定层需要能「先创建一个占位对象、之后再绑定真实值」（例如返回值占位、变量声明）。没有无参构造就无法生成这种用法，因此 SB 类必须支持默认构造，代价是引入「空对象」概念，用 `IsValid` 区分。

**练习 2**：调用一个空 SBTarget 的 `GetProcess()` 会怎样？

> **答案**：按约定，方法应能处理后端为空的情况，返回一个无效的 `SBProcess`（其 `IsValid()` 为 false），而不是崩溃。这正是规则「做点合理的事」的体现。

---

### 4.5 SWIG 绑定与脚本语言适配

#### 4.5.1 概念说明

SB API 不只服务 C++。通过 SWIG，同一套 `SB*` 类被翻译成：

- **Python 绑定**：`import lldb` 后就能用 `lldb.SBTarget` 等。
- **Lua 绑定**：同样基于 SB API。

为了适配脚本语言的特性（属性访问、迭代器、文档字符串），LLDB 在 `SB<ClassName>.h` 之外，还配套了两个额外文件（位于 `bindings/interface/`）：

- `SB<ClassName>Extensions.i`：给类加属性访问器、迭代器等脚本侧扩展。
- `SB<ClassName>Docstrings.i`：给类加文档字符串。

也就是说，脚本用户看到的接口 = `SB<ClassName>.h` + `Extensions.i` + `Docstrings.i` 三者之和。

此外，有些方法**只想给 C++ 用，不想暴露给脚本**，就用 `#ifndef SWIG` 宏包起来；反之用 `#ifdef SWIG` 只给脚本用。

#### 4.5.2 核心流程

SWIG 绑定的生成流程：

1. SWIG 读入 `bindings/` 下的 `.swig` / `.i` 接口文件。
2. 这些文件 `#include` 了 `include/lldb/API/SB*.h` 公共头。
3. SWIG 解析类声明，为每个非 `#ifndef SWIG` 保护的方法生成对应语言的包装代码。
4. 生成的包装代码在脚本里调用 C++ SB 方法，SB 方法再转交给 `lldb_private`。
5. `Extensions.i` / `Docstrings.i` 中的扩展被合并进去，形成脚本侧的属性/迭代器/文档。

#### 4.5.3 源码精读

`sbapi.md` 描述了这套三件套接口：

[docs/resources/sbapi.md:59-66](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/docs/resources/sbapi.md#L59-L66) —— 说明 SWIG 允许添加属性访问、迭代器和文档；这些扩展放在 `bindings/interface/SB<ClassName>Extensions.i`，文档放在 `SB<ClassName>Docstrings.i`。这三个文件共同构成脚本语言看到的接口。

关于用宏控制方法是否暴露给脚本：

[docs/resources/sbapi.md:68-83](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/docs/resources/sbapi.md#L68-L83) —— 用 `#ifndef SWIG` 包住只想给 C++ 用的方法；构建 macOS framework 时还会用 `unifdef` 预处理头文件，移除涉及 SWIG 的宏。

`SBDebugger.h` 里到处能看到这个宏的真实用法。例如文件句柄相关方法只给 C++：

[include/lldb/API/SBDebugger.h:216-225](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/include/lldb/API/SBDebugger.h#L216-L225) —— `SetInputFileHandle` / `SetOutputFileHandle` / `SetErrorFileHandle` 被 `#ifndef SWIG ... #endif` 包住，因为它们用了脚本语言不便表达的 `FILE *`，所以只对 C++ 可见。

而 `GetProgressFromEvent` 则展示了「同一个方法对 C++ 和 SWIG 给出不同签名」：

[include/lldb/API/SBDebugger.h:104-114](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/include/lldb/API/SBDebugger.h#L104-L114) —— `#ifdef SWIG` 分支用 `uint64_t &OUTPUT` 这类 SWIG typemap 约定（让参数成为输出返回值），`#else` 分支则是普通 C++ 引用参数。同一逻辑，两套签名适配两种世界。

#### 4.5.4 代码实践

1. **目标**：体会「同一方法对 C++ 和脚本有不同签名」。
2. **步骤**：在 `include/lldb/API/SBDebugger.h` 里搜索所有 `#ifdef SWIG` 与 `#ifndef SWIG`，统计哪些方法被排除出脚本、哪些有脚本专用签名。
3. **观察**：被排除的方法往往涉及 `FILE *`、原始指针或与脚本内存模型冲突的类型。
4. **预期结果**：你能列举 2～3 个被 `#ifndef SWIG` 隐藏的方法，并解释原因。
5. 若想看 Python 侧最终长什么样，可阅读 `docs/use/python-reference.md`（本讲不展开）。

#### 4.5.5 小练习与答案

**练习 1**：`SBDebugger.h` 中 `SetInputFileHandle(FILE *, bool)` 为什么被 `#ifndef SWIG` 隐藏？

> **答案**：因为它直接使用 C 标准库的 `FILE *`，Python/Lua 没有直接对应物，强行绑定会引入内存所有权与类型转换难题。LLDB 改用 `SetInputFile(SBFile)` 等基于 SB 类的方法供脚本使用。

**练习 2**：脚本用户看到的 SB 接口由哪几个文件共同决定？

> **答案**：由公共头 `SB<ClassName>.h`、扩展文件 `bindings/interface/SB<ClassName>Extensions.i`、文档文件 `SB<ClassName>Docstrings.i` 三者共同决定。其中 `.h` 是主体，后两者补充属性、迭代器与文档。

---

## 5. 综合实践

把本讲五条规则串起来，完成一次完整的「**SB 类合规性审查**」：

1. **任务**：从 `include/lldb/API/` 中任选一个 SB 类（推荐 `SBTarget` 或 `SBDebugger`，本讲以 `SBTarget` 为例），按下表逐项核验并填写结论。

| 审查项 | 规则 | 在哪里看 | 你的结论 |
| --- | --- | --- | --- |
| ① 命名 | `SB<Name>`，方法首字母大写 | 类声明处 | 待填 |
| ② 无基类 | `class LLDB_API SBX {` 后无 `: public` | `SBTarget.h:38` | 待填 |
| ③ 无虚函数 | 全文搜 `virtual` 应为 0 | 整个头文件 | 待填 |
| ④ 单一成员 | `private:` 段只有一个指针成员 | `SBTarget.h:1081-1083` | 待填 |
| ⑤ 可默认构造 | 存在无参构造 `SBTarget();` | `SBTarget.h:52` 附近 | 待填 |
| 额外：转发而非实现 | `.cpp` 里业务方法是否只转发 | `SBTarget.cpp` 任意方法 | 待填 |
| 额外：脚本适配 | 有无 `#ifndef SWIG` / `#ifdef SWIG` | 头文件中 | 待填 |

2. **操作步骤**：
   - 用编辑器打开 `include/lldb/API/SBTarget.h` 与 `source/API/SBTarget.cpp`。
   - 逐行核对上表每一项，把「待填」替换成「✅ 符合 / ❌ 不符合（附说明）」。
   - 对于「转发而非实现」，挑一个具体方法（如 `LaunchSimple` 或 `BreakpointCreateByLocation`）说明它如何调用内部 `Target` 的对应方法。
3. **需要观察的现象**：理想情况下，所有规则项都应为「✅ 符合」。若发现任何「❌」，请仔细确认——那要么是你看错了，要么是该处有特殊豁免（如 `SBError` 的空对象语义）。
4. **预期结果**：产出一份「SBTarget SBAPI 合规报告」，并能用一两句话向同伴解释「为什么 LLDB 要这么严格」。
5. 如果你没有本地可浏览的环境，可只基于本讲给出的永久链接完成阅读型审查，并标注「待本地验证」。

---

## 6. 本讲小结

- LLDB 用两个命名空间分层：`lldb`（公共 SB API，承诺 ABI 稳定）与 `lldb_private`（内部实现，可自由重构）。
- SB 类遵守**五条核心规则**：命名规范、不可继承、无虚函数、单一成员、可默认构造——全部为了让 `liblldb` 升级不破坏旧客户端。
- SB 对象采用**单成员代理模式**：只持有一个指向内部对象的（共享）指针，拷贝 SB 对象等于拷贝指针，代价极低且不改变布局。
- 内部类以**不透明前向声明**形式集中放在 `lldb-forward.h`，公共头不泄漏内部布局；需要额外状态时用 **Impl 类**（如 `SBValue` → `ValueImpl`）保持 SB 对象大小恒定。
- 默认构造产生「空」对象，用 `IsValid()` 判定有效性；所有方法都要能优雅处理后端为空（`SBError` 是例外）。
- 同一套 SB 类经 **SWIG** 同时服务 C++、Python、Lua；用 `#ifndef SWIG` / `#ifdef SWIG` 控制方法是否暴露给脚本，并为脚本补充属性、迭代器与文档。

---

## 7. 下一步学习建议

- 掌握了「为什么这样设计」之后，下一步建议学习 **[u2-l2 SBDebugger 生命周期与初始化](u2-l2-sbdebugger-lifecycle.md)**，看 `SBDebugger::Initialize` 如何拉起整套 LLDB 并注册插件。
- 想看 SB 对象之间的导航关系（SBTarget→SBProcess→SBThread→SBFrame→SBValue），可继续读 **[u2-l3 核心 SB 对象模型](u2-l3-sb-object-model.md)**。
- 对脚本绑定感兴趣的同学，可预先浏览 `docs/use/python-reference.md`，本系列在 **u13（脚本与绑定）** 单元会深入 SWIG 细节。
- 建议同时对照阅读 `docs/resources/sbapi.md` 全文，本讲只抽取了与「设计哲学」最相关的部分，文档还涵盖了生命周期（`const char *` 所有权）与 API Instrumentation（reproducer 插桩）等进阶话题。

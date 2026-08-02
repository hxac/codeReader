# 核心 SB 对象模型

## 1. 本讲目标

本讲把上一讲（u2-l2）建立的「SBDebugger 是入口」继续往下延伸一层。读者学完后应该能够：

- 说清 `SBTarget → SBProcess → SBThread → SBFrame → SBValue` 这条「执行对象层级」每一层代表什么、如何用代码从上一层导航到下一层。
- 理解 SB 对象之间的导航在源码层面究竟做了什么（本质是「用共享指针构造一个新的轻量代理对象」）。
- 知道为什么读取线程、栈帧、变量之前，被调试进程必须处于「停止（stopped）」状态，以及 LLDB 用哪些锁和检查来保证这一点。
- 从一个 `SBValue` 出发，获取它的值、类型、地址，以及它的子成员（结构体字段、数组元素）。
- 区分两条不同的对象层级：一条是「执行层级」（Target/Process/Thread/Frame/Value），一条是「符号层级」（Module/Section/Symbol/CompileUnit）。
- 在 SB 对象图上完整走通一次「读出一个局部变量并展开它的字段」的操作。

## 2. 前置知识

在进入源码前，先用三个生活化的类比建立直觉。

**类比一：调试现场是一座「正在运行的剧场」。**
- 剧场本身是 `SBTarget`：它描述「要演哪一出戏」（程序文件、架构、断点、模块列表），但此刻台上未必有人。
- 演到一半、暂停的那一刻，台上有一群演员，这群演员的整体就是 `SBProcess`。
- 每个演员是一条 `SBThread`。
- 演员手里翻到的某一页剧本（某一层调用栈）是 `SBFrame`。
- 那一页上写到的某个具体道具（变量）是 `SBValue`。

**类比二：SB 对象是「取件码」，不是「货物本身」。**
在 u2-l1 里我们说过，每个 SB 类只有一个成员变量，且它是一个指向 `lldb_private::` 内部对象的共享指针（shared pointer）。这意味着：你手里拿着的 `SBThread` 不是线程的全部数据，而更像是一张「取件码」——拿到它就能去内部取到真正的 `Thread`。导航（从 Process 拿到 Thread）的过程，本质是「向内部要一个 Thread 的共享指针，再用它新包一个 `SBThread` 还给你」。

**类比三：读数据必须等演员「定住」。**
当进程在运行时，线程列表、寄存器、栈帧随时在变，此刻去读会读到撕裂的数据。所以 LLDB 规定：读线程/帧/变量之前，进程必须先「停下来」。源码里你会反复看到一个叫 `StopLocker`（停止锁）和 `GetStoppedExecutionContext`（获取「已停止」的执行上下文）的机制，它们就是这个「定住」动作的守卫。

> 术语提示：
> - **shared pointer（共享指针）**：`std::shared_ptr<T>`，一种可以被多方共同持有、引用计数归零才销毁对象的智能指针。LLDB 内部对象几乎都用 `XxxSP`（如 `TargetSP`、`ProcessSP`）来传递所有权。
> - **代理模式（proxy）**：SB 对象不自己存数据，只持有一个指向内部对象的指针并转发调用，故称代理。
> - **执行上下文（ExecutionContext）**：LLDB 内部把「调试器—目标—进程—线程—帧」五层打包在一起的一个结构，方便在调用链里一次传齐。

如果这些概念还略感抽象，没关系，下面逐层用源码展开。

## 3. 本讲源码地图

本讲围绕「公共 API（SBAPI）」里描述执行对象层级与符号层级的几个文件：

| 文件 | 作用 |
| --- | --- |
| `source/API/SBTarget.cpp` | `SBTarget` 的实现：从目标拿进程（`GetProcess`）、拿模块（`GetModuleAtIndex`）等导航入口。 |
| `source/API/SBProcess.cpp` | `SBProcess` 的实现：拿选中线程（`GetSelectedThread`）、按索引拿线程（`GetThreadAtIndex`）、查进程状态（`GetState`）。 |
| `source/API/SBThread.cpp` | `SBThread` 的实现：拿进程、拿帧数量、按索引/选中拿栈帧（`GetFrameAtIndex`/`GetSelectedFrame`）。 |
| `source/API/SBFrame.cpp` | `SBFrame` 的实现：拿所属线程、按名字找变量（`FindVariable`）、批量取变量（`GetVariables`）。 |
| `source/API/SBValue.cpp` | `SBValue` 的实现：取值（`GetValue`）、取类型（`GetType`）、取子成员（`GetChildMemberWithName`/`GetChildAtIndex`）。 |
| `include/lldb/API/SBTarget.h` | `SBTarget` 公共头：可看到它唯一的成员 `m_opaque_sp` 及大量 `friend` 声明。 |
| `include/lldb/API/SBValue.h` | `SBValue` 公共头：可见它用 `ValueImplSP` 而非直接用 `ValueObjectSP`。 |
| `source/API/SBModule.cpp` | `SBModule` 的实现：导航到符号表与编译单元（符号层级）。 |
| `examples/python/globals.py` | 一个用 Python 走 SB 对象图、导出全局变量的真实示例。 |

记住一条主线：这五个 `SBXxx.cpp` 文件几乎全是「转发壳」——每个方法先检查后端共享指针是否有效，有效就取出内部对象、调用同名方法，再把结果重新包成一个 SB 对象返回。

## 4. 核心概念与源码讲解

### 4.1 执行对象层级：Target → Process → Thread → Frame

#### 4.1.1 概念说明

「执行对象层级」描述的是**一次调试会话里，程序运行到某一刻所形成的对象树**。它的五层从上到下是：

```
SBDebugger          （会话根，上一讲已讲）
   └── SBTarget        一个待调试程序（可执行文件 + 断点 + 模块）
          └── SBProcess     被启动/附加的那个进程（一个 Target 同一时刻通常只有一个 Process）
                 └── SBThread    进程中的一条线程
                        └── SBFrame  调用栈中的某一帧
                               └── SBValue   这一帧可见的某个变量
```

两个要点：

1. **包含关系是「运行时」的，不是静态的。** `SBTarget` 在进程还没启动时就有效（你刚 `CreateTarget` 完，断点都还没设）；但 `SBProcess` 只有在 `Launch`/`Attach` 之后才存在；`SBThread`/`SBFrame`/`SBValue` 只有在进程**停下来**之后才有意义。
2. **导航是单向构造新对象。** 从 `SBTarget` 拿 `SBProcess`，并不会「移动」什么，而是**新构造一个 `SBProcess`，把内部 `ProcessSP` 塞进去**再返回。所以你可以同时持有多个指向同一内部对象的 SB 句柄，它们互不影响。

#### 4.1.2 核心流程

一次典型的「自顶向下导航」伪代码：

```
target   = debugger.CreateTarget("a.out")          # 有 Target，无 Process
bp       = target.BreakpointCreateByName("main")
process  = target.LaunchSimple(...)                  # 此刻才产生 Process
# ……等待进程命中断点、停下……
thread   = process.GetSelectedThread()               # Process → Thread
frame    = thread.GetSelectedFrame()                 # Thread   → Frame
value    = frame.FindVariable("argc")                # Frame    → Value
print(value.GetValue(), value.GetType())
```

每一步「→」在源码里都对应一个返回新 SB 对象的方法。下一节我们就把这些方法的真实实现拆开看。

#### 4.1.3 源码精读

**第一跳：`SBTarget::GetProcess`。** 它先创建一个空的 `SBProcess`，从自己的后端 `TargetSP` 取出 `ProcessSP`，再用 `SetSP` 装进新对象返回。

参考 [source/API/SBTarget.cpp:177-188](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/source/API/SBTarget.cpp#L177-L188)：取进程并封装为 `SBProcess`。

关键点：

- 若 `target_sp` 为空（这是个无效 `SBTarget`），`sb_process` 保持默认构造的「空对象」直接返回——**导航无效对象不会崩溃，只会得到另一个无效对象**。
- `target_sp->GetProcessSP()` 取出内部进程的共享指针；`sb_process.SetSP(process_sp)` 把它装好。整个过程没有拷贝进程的真实数据。

**第二跳：`SBProcess::GetSelectedThread`。** 它从进程里取「当前选中的线程」。

参考 [source/API/SBProcess.cpp:207-221](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/source/API/SBProcess.cpp#L207-L221)：取当前选中线程。

注意这里多了一把锁：`std::lock_guard<...> guard(process_sp->GetTarget().GetAPIMutex())`。`GetAPIMutex()` 是 Target 级别的递归互斥锁，保证同一时刻只有一个线程在通过公共 API 操作这个目标——这是 SB 对象导航线程安全的基石（4.2 节详述）。

**第三跳：`SBThread::GetSelectedFrame` / `GetFrameAtIndex`。**

参考 [source/API/SBThread.cpp:1144-1162](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/source/API/SBThread.cpp#L1144-L1162)：取当前选中帧。

参考 [source/API/SBThread.cpp:1105-1122](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/source/API/SBThread.cpp#L1105-L1122)：按索引取某一帧。

这里出现了一个贯穿 4.2 节的关键调用 `GetStoppedExecutionContext(m_opaque_sp)`：它要求进程必须处于停止状态，否则返回一个错误（`Expected`），代码用 `if (!exe_ctx)` 判断后记日志并返回空对象。只有 `exe_ctx->HasThreadScope()`（上下文里确实有线程）才会真正去取帧。

**反向导航也存在。** 比如 `SBFrame::GetThread` 可以从帧反查线程，`SBThread::GetProcess` 可以从线程反查进程：

参考 [source/API/SBFrame.cpp:608-622](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/source/API/SBFrame.cpp#L608-L622)：从帧反查线程。

参考 [source/API/SBThread.cpp:1069-1087](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/source/API/SBThread.cpp#L1069-L1087)：从线程反查进程。

这说明对象层级是一张**可双向走通的图**，而非单向链表。

**为什么不会循环依赖？** SB 类之间通过 `friend` 互相授权访问彼此私有的 `GetSP()/SetSP()`。看 `SBTarget` 头里的友元列表：

参考 [include/lldb/API/SBTarget.h:1044-1066](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/include/lldb/API/SBTarget.h#L1044-L1066)：`SBTarget` 把 `SBProcess`、`SBThread`、`SBFrame`、`SBValue` 等都列为友元。

这样 `SBProcess` 内部需要拿到 `TargetSP` 时就能直接访问，而不必在公共头里暴露内部类型的成员。

#### 4.1.4 代码实践（源码阅读型）

**目标**：亲手确认「导航 = 构造新代理对象」这一模式。

**步骤**：

1. 打开 [source/API/SBTarget.cpp:177-188](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/source/API/SBTarget.cpp#L177-L188)，确认 `GetProcess` 先 `SBProcess sb_process;`（默认空对象）再 `SetSP`。
2. 打开 [source/API/SBProcess.cpp:207-221](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/source/API/SBProcess.cpp#L207-L221)，确认 `GetSelectedThread` 同样是「构造空 `SBThread` → `SetThread`」。
3. 打开 [source/API/SBThread.cpp:1105-1122](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/source/API/SBThread.cpp#L1105-L1122)，确认 `GetFrameAtIndex` 是「构造空 `SBFrame` → `SetFrameSP`」。

**需要观察的现象**：三个方法的结构高度同构——都是「空对象 + 取后端指针 + Set 装填 + 返回」。

**预期结果**：你能用一句话概括这套模式：「SB 导航方法从不复制业务数据，只搬运共享指针并重新封装。」

#### 4.1.5 小练习与答案

**练习 1**：如果一个 `SBTarget` 是默认构造的空对象（`IsValid()` 返回 false），调用它的 `GetProcess()` 会发生什么？
**答案**：`GetSP()` 返回空的 `TargetSP`，`if (TargetSP target_sp = GetSP())` 条件不成立，函数直接返回一个默认构造的空 `SBProcess`。即「空进空出」，不会崩溃。

**练习 2**：`SBThread::GetFrameAtIndex(0)` 和 `SBThread::GetSelectedFrame()` 通常都返回第 0 帧，它们在实现上有何共同前置条件？
**答案**：都先调用 `GetStoppedExecutionContext(m_opaque_sp)`，要求进程处于停止状态且上下文具有线程作用域（`HasThreadScope()`），否则返回无效 `SBFrame`。

---

### 4.2 前置条件：进程必须「停止」才能读线程与帧

#### 4.2.1 概念说明

第 4.1 节里你已经看到 `GetStoppedExecutionContext` 反复出现。这一节专门讲它背后的两个机制：

1. **运行锁（Run Lock）与停止锁（StopLocker）。** 当进程正在跑（`continue` 之后、停下之前），它的线程列表、寄存器、栈都不稳定。LLDB 给每个进程配了一把「运行锁」：进程跑起来时这把锁被占用，任何试图读线程/帧的 API 会通过 `StopLocker::TryLock(&GetRunLock())` 抢锁，抢不到就放弃读取、返回空对象，从而**避免读到正在变化的数据**。
2. **API 互斥锁（APIMutex）。** 它是 Target 级的递归锁，保证多线程同时用 SBAPI 操作同一个目标时不会互相踩踏。

合起来一句话：**读线程/帧/变量前，进程必须是 stopped，且访问要加 API 锁。**

注意 `SBTarget::GetProcess`、`SBTarget::GetModuleAtIndex` 这类「目标级」导航**没有**这些锁——因为模块列表（`target_sp->GetImages()`）本身是线程安全的，源码注释直接写明 `// The module list is thread safe, no need to lock`。锁的粒度是精心选择的，不是处处都加。

#### 4.2.2 核心流程

「按索引取线程」是最能体现这套守卫的方法，它的流程：

```
取 process_sp
  ├─ 若空 → 返回空 SBThread
  ├─ StopLocker::TryLock(&GetRunLock())
  │     ├─ 抢到(can_update=true) → 继续
  │     └─ 没抢到(进程在跑) → 跳过实际读取
  ├─ lock_guard(GetAPIMutex())        # 串行化 API 访问
  └─ thread_sp = GetThreadList().GetThreadAtIndex(index, can_update)
     → SetThread(thread_sp) 装填返回
```

`can_update` 这个布尔很巧妙：只有「进程确实停着」时才允许线程列表在读取时做刷新更新；进程在跑时就老老实实返回空。

#### 4.2.3 源码精读

参考 [source/API/SBProcess.cpp:391-405](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/source/API/SBProcess.cpp#L391-L405)：按索引取线程，含 `StopLocker` 与 `APIMutex` 双重守卫。

可以看到三件事：

1. `Process::StopLocker stop_locker;` 声明停止锁；
2. `stop_locker.TryLock(&process_sp->GetRunLock())` 尝试占用运行锁，返回值赋给 `can_update`；
3. 只有 `TryLock` 成功（进程没在跑），才会进入 `GetThreadList().GetThreadAtIndex(index, false)` 真正取线程。

线程/帧层的统一入口则是 `GetStoppedExecutionContext`，它把「进程是否停止」做成一个返回 `Expected<StoppedExecutionContext>`（成功带值、失败带错误）的结果。`SBThread::GetProcess` 就是典型用法：

参考 [source/API/SBThread.cpp:1069-1087](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/source/API/SBThread.cpp#L1069-L1087)：用 `GetStoppedExecutionContext` 守卫后取进程。

`if (!exe_ctx)` 分支用 `LLDB_LOG_ERROR` 记录错误并返回空对象——这就是为什么运行中调用这些方法不会崩、只会得到无效句柄。

要确认进程到底处于什么状态，用 `GetState`：

参考 [source/API/SBProcess.cpp:487-499](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/source/API/SBProcess.cpp#L487-L499)：查询进程当前状态（如 `eStateStopped`、`eStateRunning`）。

#### 4.2.4 代码实践（命令行观察型）

**目标**：直观体会「进程必须停止」。

**步骤**：

1. 准备一个带 `-g` 的小程序 `a.out`（含 `main` 与一个局部变量即可）。
2. 在 lldb 中：
   ```
   (lldb) target create a.out
   (lldb) b main
   (lldb) process launch
   (lldb) script lldb.debugger.GetSelectedTarget().GetProcess().GetState()
   ```
3. 在命中断点（停止）时执行上面的 `script` 行；再尝试 `process continue` 后立刻执行同一行（注意时序）。

**需要观察的现象**：停止时返回 `eStateStopped`；运行中读到的是 `eStateRunning` 或在导航帧/变量时得到无效对象。

**预期结果**：你亲眼看到「状态非 stopped 时，线程/帧/变量的读取会失败或返回空」，从而理解 4.2 节这套守卫存在的必要性。若你的环境难以捕捉运行中的瞬间，**可标注「待本地验证」**并仅完成停止态的观察。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `SBTarget::GetModuleAtIndex` 不加 `StopLocker`，而 `SBProcess::GetThreadAtIndex` 要加？
**答案**：模块列表（`GetImages()`）是进程启动前就已建立、且自身线程安全的数据结构，与「进程是否在运行」无关；而线程列表会随进程运行实时变化，必须确保进程停止、数据稳定后才能安全读取。

**练习 2**：`can_update` 这个变量在 `GetThreadAtIndex` 里起什么作用？
**答案**：它来自 `StopLocker::TryLock` 的返回值，表示「进程当前确实停着」。只有为 true 时，才允许 `GetThreadList()` 在读取的同时刷新线程状态；为 false（进程在跑）时跳过实际读取以避免读到不稳定数据。

---

### 4.3 SBValue：从变量到它的值、类型与子成员

#### 4.3.1 概念说明

`SBValue` 表示「一个具体的值」——它可能是一个局部变量、一个全局变量、一个表达式结果，或另一个 `SBValue` 的子成员（结构体字段、数组元素）。

它是 SB 家族里**最特殊的一个**，原因有二：

1. **它包装的不是 `ValueObjectSP`，而是 `ValueImplSP`。** u2-l1 讲过 SB 类「单一成员」规则；但 `SBValue` 需要额外记住两件事——用户是否想要「动态类型值」（多态对象的运行时类型）、是否想要「合成（synthetic）展示」（如把 `std::vector` 显示成数组）。为了在不破坏 SB 对象内存布局的前提下携带这些偏好，LLDB 引入了一个中间层 `ValueImpl`，让 `SBValue` 只持有一个 `ValueImplSP`。这正是 u2-l1 提到的「需要额外状态时用 Impl 类」的真实例子。

参考 [include/lldb/API/SBValue.h:538-539](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/include/lldb/API/SBValue.h#L538-L539)：`SBValue` 的唯一成员是 `ValueImplSP m_opaque_sp`。

2. **它是「可递归展开」的。** 一个结构体类型的 `SBValue` 有若干子成员，每个子成员又是一个 `SBValue`，于是形成一棵值树。导航这棵树是读数据的核心动作。

#### 4.3.2 核心流程

读取并展开一个变量的典型流程：

```
frame.FindVariable("p")          # p 是 struct Point { int x; int y; }
   → SBValue(p)
      ├─ .GetName()      → "p"
      ├─ .GetType()      → SBType(Point)
      ├─ .GetValue()     → (结构体通常无标量值，可能为空)
      ├─ .GetNumChildren() → 2
      ├─ .GetChildMemberWithName("x") → SBValue(x), .GetValue() → "3"
      └─ .GetChildMemberWithName("y") → SBValue(y), .GetValue() → "4"
```

两个不同的「取子成员」API：

- `GetChildMemberWithName("x")`：**按字段名**直接定位（会沿基类、合成规则查找）。
- `GetChildAtIndex(i)`：**按下标**遍历所有子成员。

#### 4.3.3 源码精读

**取标量值**：`GetValue` 把内部 `ValueObject` 的字符串表示取出来，并用 `ConstString` 稳定其生命周期后返回 C 字符串。

参考 [source/API/SBValue.cpp:187-195](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/source/API/SBValue.cpp#L187-L195)：取值的字符串表示。

注意 `ValueLocker locker; lldb::ValueObjectSP value_sp(GetSP(locker));` 这对组合：`GetSP` 通过 `ValueImpl` 拿到真正的 `ValueObjectSP`，`ValueLocker` 负责在读取期间锁住值对象防止并发失效。这是 `SBValue` 几乎所有方法都重复的固定前奏。

**取类型**：`GetType` 把内部的 `TypeImpl` 包成 `SBType` 返回。

参考 [source/API/SBValue.cpp:225-238](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/source/API/SBValue.cpp#L225-L238)：取类型为 `SBType`。

**按名取子成员**：先从 `ValueImpl` 拿到目标偏好（是否动态值），再委托给 `ValueObject::GetChildMemberWithName`，最后把结果连同偏好一起 `SetSP` 装进新的 `SBValue`。

参考 [source/API/SBValue.cpp:607-637](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/source/API/SBValue.cpp#L607-L637)：按字段名取子成员。

注意无参重载（607 行）会先问 `target_sp->GetPreferDynamicValue()` 决定是否用动态类型，再转调带 `use_dynamic_value` 的重载——这就是 `ValueImpl` 携带「偏好」的价值。

**按下标取子成员 / 取子成员个数**：

参考 [source/API/SBValue.cpp:556-590](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/source/API/SBValue.cpp#L556-L590)：按下标取子成员，并对指针/数组做特化（`treat_as_array`）。

参考 [source/API/SBValue.cpp:896-913](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/source/API/SBValue.cpp#L896-L913)：取子成员数量，内部用 `GetNumChildrenIgnoringErrors(max)`。

`GetChildAtIndex` 里有个细节：当 `treat_as_array` 为真且值是指针或数组类型时，走 `GetSyntheticArrayMember`（把 `ptr[i]` 当数组下标处理）；否则走 `GetChildAtIndex`。这就是为什么在 Python 里你能对 `SBValue` 直接做 `val[i]`。

**变量从哪来**：值树的根通常来自 `SBFrame`。`FindVariable` 按名字在当前帧的作用域里查变量，找到后包成 `SBValue`。

参考 [source/API/SBFrame.cpp:436-459](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/source/API/SBFrame.cpp#L436-L459)：按名字查找帧内变量。

它同样以 `GetStoppedExecutionContext` 守卫，再委托 `frame->FindVariable(ConstString(name))`。如果想一次性拿到当前帧的全部变量（参数/局部/静态），用 `GetVariables`：

参考 [source/API/SBFrame.cpp:640-673](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/source/API/SBFrame.cpp#L640-L673)：批量获取当前帧的变量（参数/局部/静态）。

#### 4.3.4 代码实践（命令行 + 源码对照）

**目标**：在真实调试中走通一棵值树，并对照源码理解每一步。

**步骤**：

1. 编写并编译示例程序（示例代码，非项目原有）：
   ```c
   // point.c —— 示例代码
   struct Point { int x; int y; };
   int main(void) {
       struct Point p = { 3, 4 };
       return p.x + p.y;   // 在此行下断点
   }
   ```
   编译：`cc -g point.c -o point`（Windows/MSVC 用户用 `cl /Zi point.c`）。
2. 在 lldb 中：
   ```
   (lldb) target create point
   (lldb) b main
   (lldb) run
   (lldb) frame variable p        # 命令行直接看
   (lldb) script
   >>> f = lldb.debugger.GetSelectedTarget().GetProcess().GetSelectedThread().GetSelectedFrame()
   >>> p = f.FindVariable("p")
   >>> p.GetChildMemberWithName("x").GetValue()
   >>> p.GetChildMemberWithName("y").GetValue()
   >>> p.GetNumChildren()
   ```
3. 把上面每一步 Python 调用，对照 [source/API/SBValue.cpp:607-637](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/source/API/SBValue.cpp#L607-L637) 与 [source/API/SBFrame.cpp:436-459](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/source/API/SBFrame.cpp#L436-L459) 阅读其实现。

**需要观察的现象**：`GetChildMemberWithName("x")` 返回的 `SBValue`，其 `GetValue()` 为 `"3"`；`GetNumChildren()` 为 `2`。

**预期结果**：你能在脑子里把「Python 一行调用」对应到「源码里取后端 `ValueObjectSP` → 委托内部方法 → 重新封装 `SBValue`」的完整链路。如果本地没有可用编译器，**标注「待本地验证」**，仅完成源码阅读部分。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `SBValue` 的成员是 `ValueImplSP` 而不是直接 `ValueObjectSP`？
**答案**：为了在不改变 `SBValue` 内存布局（保持「单一成员」、保障 ABI 稳定）的前提下，额外携带「是否使用动态类型值」「是否使用合成展示」这两个偏好。`ValueImpl` 包住真正的 `ValueObjectSP` 加上这两个开关，`SBValue` 只持有一个 `ValueImplSP`。

**练习 2**：`GetChildMemberWithName("x")` 与 `GetChildAtIndex(0)` 有何区别？
**答案**：前者按字段名定位，会沿基类与合成规则查找名为 `x` 的成员，结果与位置无关；后者按下标遍历当前展开出的子成员序列，第 0 个不一定是 `x`（例如有虚表指针、合成子成员时会改变顺序）。需要精确字段时用前者，需要遍历全部子成员时用后者配合 `GetNumChildren()`。

---

### 4.4 符号层级：SBModule → Section / Symbol / CompileUnit

#### 4.4.1 概念说明

除了「执行层级」，还有一条**与进程是否运行无关**的层级——**符号层级**，它描述可执行文件本身的结构：

```
SBTarget
   └── SBModule        一个已加载的可执行文件/共享库（如 a.out、libc.so）
          ├── SBSection        文件里的段/节（.text、.data、.debug_info …）
          ├── SBSymbol         符号表里的一条符号（函数名、全局变量名 …）
          └── SBCompileUnit    一个编译单元（一个 .c/.cpp 源文件及其行号表）
                 └── SBLineEntry   一条「源文件行 ↔ 地址」映射
```

理解这条层级很重要：断点设置（按文件名+行号、按符号名）、行号回溯、类型查找，背后都要先把地址或名字「落」到某个 Module 的 Section/Symbol/CompileUnit 上。这条层级的导航**不需要进程停止**，因为模块信息在 `CreateTarget` 时就已从可执行文件解析好了。

#### 4.4.2 核心流程

从目标出发看符号层级的典型路径：

```
target.GetModuleAtIndex(0)        # 取主模块
   module.GetNumCompileUnits()    # 有多少个编译单元
   module.GetCompileUnitAtIndex(i)
   module.GetNumSymbols()         # 符号表里有多少条符号
   module.GetSymbolAtIndex(k) / module.FindSymbol("main")
   module.FindSection(".text")    # 按段名找节
```

#### 4.4.3 源码精读

**从目标取模块**：`GetModuleAtIndex` 直接从目标的模块镜像表 `GetImages()` 取，注释明确「无需加锁」。

参考 [source/API/SBTarget.cpp:1777-1789](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/source/API/SBTarget.cpp#L1777-L1789)：按索引取模块。

**模块 → 编译单元**：

参考 [source/API/SBModule.cpp:260-280](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/source/API/SBModule.cpp#L260-L280)：取编译单元数量与第 i 个编译单元。

**模块 → 符号**：符号来自「统一符号表」（`GetSymtab()`）。`GetSymbolAtIndex` 与 `FindSymbol` 都先拿到这张表再操作。

参考 [source/API/SBModule.cpp:299-333](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/source/API/SBModule.cpp#L299-L333)：取符号数量、按下标取符号、按名查找符号。

注意 `FindSymbol` 把名字转成 `ConstString` 后调用 `symtab->FindFirstSymbolWithNameAndType(...)`——`ConstString` 是 LLDB 里广泛使用的「去重字符串」，能加速符号比较（详见 Utility 模块，本讲不展开）。

**真实示例：`examples/python/globals.py`。** 这个脚本不启动进程，仅创建目标，遍历主模块的符号表，找出数据符号并查它们的全局变量：

参考 [examples/python/globals.py:23-68](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/examples/python/globals.py#L23-L68)：用 Python 走 Target → Module → Symbol → Value 的真实示例。

值得留意的是它用的 Python 便捷写法：`target.module[target.executable.basename]`（等价于 `FindModule`）、`module.symbols`（等价于遍历符号表）、`global_variable.value`/`.type`/`.addr`（等价于 `SBValue` 的各 getter）。这些是 SWIG 在 `SBModule`/`SBValue` 上生成的属性与 `__getitem__`，背后调用的仍是本节讲的那些 C++ 方法——**同一套 SB API，C++ 与 Python 走的是完全相同的导航路径**。

#### 4.4.4 代码实践（源码阅读 + 命令行）

**目标**：把一条符号层级的导航在命令行和源码两侧对上。

**步骤**：

1. 准备任意带调试信息的小程序 `a.out`。
2. 在 lldb（不必启动进程）：
   ```
   (lldb) target create a.out
   (lldb) image list                          # 列出模块（SBModule）
   (lldb) image dump sections a.out           # 看段/节（SBSection）
   (lldb) image lookup -n main                # 按符号名查（SBSymbol）
   (lldb) image dump line-table a.out         # 看行号表（SBCompileUnit/SBLineEntry）
   ```
3. 在源码中确认：`image list` 对应 `SBTarget::GetModuleAtIndex`（[SBTarget.cpp:1777-1789](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/source/API/SBTarget.cpp#L1777-L1789)），按名查符号对应 `SBModule::FindSymbol`（[SBModule.cpp:319-333](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/lldb/source/API/SBModule.cpp#L319-L333)）。

**需要观察的现象**：上述命令在进程未运行时全部可用，证明符号层级不依赖进程状态。

**预期结果**：你能说出「image 系列命令 = 在 SBModule/SBSection/SBSymbol/SBCompileUnit 这条符号层级上导航」。

#### 4.4.5 小练习与答案

**练习 1**：为什么遍历 `SBModule` 的符号和编译单元时不需要进程停止？
**答案**：模块信息来自可执行文件本身（ELF/Mach-O 的段、符号表、DWARF 调试信息），在 `CreateTarget` 时就已解析进 `Module` 对象与线程安全的 `GetImages()` 列表，与被调试进程是否运行无关，因此无需 `StopLocker`。

**练习 2**：`examples/python/globals.py` 里 `module.symbols` 和 `target.module[...]` 这些写法，最终调用的是哪两族 C++ 方法？
**答案**：`module.symbols` 背后是遍历 `SBModule::GetNumSymbols()`/`GetSymbolAtIndex()`；`target.module[...]` 背后是 `SBTarget::FindModule(...)`。它们都是 SWIG 在 SB 类上生成的属性/下标语法糖，转发到同一套 SBAPI。

---

## 5. 综合实践

把本讲四条主线串起来，完成下面这个端到端任务（即本讲规格里要求的实践）。

**任务**：用 Python 脚本（`import lldb`）加载一个带调试信息的程序，设置断点并运行，命中后打印**当前线程**、**栈帧**、以及**一个局部变量**的 SB 对象类型与值。

**参考脚本**（示例代码，非项目原有，可保存为 `walk_sb.py` 后用 `command script import` 导入，或在 `lldb` 的 `script` 交互里逐行执行）：

```python
# walk_sb.py —— 示例代码
import lldb

def walk(debugger, command, result, internal_dict):
    target = debugger.GetSelectedTarget()
    print("SBTarget  :", target, "| Valid =", target.IsValid())

    process = target.GetProcess()
    print("SBProcess :", process, "| state =", process.GetState())   # 应为 eStateStopped

    thread = process.GetSelectedThread()
    print("SBThread  :", thread, "| num frames =", thread.GetNumFrames())

    frame = thread.GetSelectedFrame()
    print("SBFrame   :", frame)

    # 假设被调试程序 main 里有个局部变量 argc
    argc = frame.FindVariable("argc")
    if argc.IsValid():
        print("SBValue   : name =", argc.GetName(),
              "| type =", argc.GetType(),
              "| value =", argc.GetValue())
    else:
        print("SBValue   : 未找到名为 argc 的变量，请按你的程序改成真实变量名")
```

**操作步骤**：

1. 用 4.3.4 的 `point.c`（或任何带 `-g`、`main` 里有局部变量的小程序）。
2. 启动 lldb：`lldb point`。
3. 导入脚本：`(lldb) command script import walk_sb.py`（若你把它注册成了 `walk` 命令）。
4. 设断点并运行：`(lldb) b main` `(lldb) run`。
5. 命中后执行你的脚本/命令，观察输出。

**串联要点**（把四个模块对应起来）：

| 输出行 | 体现的本讲模块 | 关键源码 |
| --- | --- | --- |
| `SBTarget` | 4.1 层级总览（根） | `SBTarget` 单一成员 `m_opaque_sp` |
| `SBProcess` + `state` | 4.2 停止前提 | `SBProcess::GetState` |
| `SBThread` + `num frames` | 4.1 / 4.2 导航与守卫 | `SBThread::GetSelectedFrame`、`GetStoppedExecutionContext` |
| `SBFrame` | 4.1 帧层 + 4.3 变量入口 | `SBFrame::FindVariable` |
| `SBValue` 的 name/type/value | 4.3 值树 | `SBValue::GetType`/`GetValue` |

**预期结果**：脚本输出能稳定显示 `state = eStateStopped`，并打印出栈帧行号与某个局部变量的类型和值。若你的 LLDB 未启用 Python 绑定，可改为纯命令行（`process status`、`frame variable`、`image lookup`）配合源码阅读完成，并**标注「待本地验证」**。

> 进阶（可选）：把脚本里取变量的部分换成遍历一个结构体——`FindVariable("p")` 后用 `GetChildMemberWithName` 展开字段，验证 4.3 节的值树导航。

## 6. 本讲小结

- LLDB 的执行对象层级是 `SBTarget → SBProcess → SBThread → SBFrame → SBValue`，每一层都代表「程序运行到某一刻」的一个粒度。
- SB 对象导航的本质是：取出内部对象的共享指针，**重新封装成一个新的轻量 SB 代理**返回；业务数据从不被复制，无效对象导航只会得到无效对象。
- 读取线程、帧、变量前**进程必须停止**：`StopLocker`（运行锁）保证数据稳定，`GetAPIMutex()` 串行化 API 访问，`GetStoppedExecutionContext` 把「是否停止」做成统一前置检查。
- 目标级导航（取模块）不需要这些锁，因为模块列表是线程安全的、且在进程启动前就建好——锁的粒度是按数据是否「会随运行变化」来设计的。
- `SBValue` 是特殊成员：用 `ValueImplSP` 而非直接 `ValueObjectSP`，以在不破坏 ABI 的前提下携带「动态值/合成」偏好；它可递归展开为值树（`GetChildMemberWithName`/`GetChildAtIndex`/`GetNumChildren`）。
- 还存在一条与进程状态无关的**符号层级** `SBModule → SBSection/SBSymbol/SBCompileUnit`，断点设置、行号回溯、`image` 系列命令都建基于此；C++ 与 Python 走的是同一套 SB API。

## 7. 下一步学习建议

- **向「内部实现」深入**：本讲只看了 SB 这层「转发壳」。下一阶段（第 4 单元 u4-l3、第 5 单元）会进入 `lldb_private::` 的 `Module`/`Address`、`Target`/`Process`/`Thread`/`StackFrame`，看那些被 SB 转发的真实逻辑。建议先读 `include/lldb/Core/Module.h` 与 `source/Target/ExecutionContext.cpp`。
- **理解执行上下文传递**：本讲多次出现的 `GetStoppedExecutionContext`/`StoppedExecutionContext` 是 `ExecutionContext` 机制在 API 层的投影；第 5 单元 u5-l1 会系统讲 `ExecutionContext`/`ExecutionContextRef` 如何在调用链里携带五层上下文。
- **接上命令系统**：第 3 单元（u3）会讲 `frame variable`、`expression`、`image lookup` 这些命令是如何经 `CommandInterpreter` 调用到本讲的 SB 方法的，把「命令行 → SBAPI → 内部对象」整条链补全。
- **动手习惯**：在阅读后续任何一篇源码时，先问自己「这段代码处在执行层级还是符号层级？是否需要进程停止？」——这个二分法能帮你快速定位大多数 LLDB 代码的语境。

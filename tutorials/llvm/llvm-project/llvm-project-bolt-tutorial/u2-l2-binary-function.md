# BinaryFunction 与状态机：一个函数在 BOLT 中的生命周期

## 1. 本讲目标

学完本讲，你应当能够：

- 说出 `BinaryFunction` 在 BOLT 里代表什么——为什么说它是「一个被处理的函数」的统一容器，以及它和上一讲的 `BinaryContext` 是什么关系。
- 完整复述 `BinaryFunction::State` 这个六态状态机（`Empty` → `Disassembled` → `CFG` → `CFG_Finalized` → `EmittedCFG` → `Emitted`），并能解释每两个相邻状态之间发生了什么。
- 看懂指令表 `InstrMapType`（offset → `MCInst`）与 `FunctionLayout` 之间的分工：一个是 CFG 构建前的临时容器，一个是输出顺序的最终载体。
- 解释 `isSimple()` 这个标志为什么是「函数能否被优化」的分水岭，并说清 `clearDisasmState()` / `resetState()` 在反汇编失败时如何把函数状态回退。

本讲是单元 2（核心数据结构）的第二篇，承接 [u2-l1](u2-l1-binary-context.md) 里「`BinaryContext` 是大柜子，里面装着所有 `BinaryFunction`」的结论——本讲就钻进柜子里，拆开「一个函数」这个对象，看它在 BOLT 整条管线里是怎么一步步被加工、状态又是怎么流转的。下一讲 [u2-l3](u2-l3-basic-block-and-cfg.md) 会进一步钻进函数内部的 `BinaryBasicBlock` 与 CFG。

## 2. 前置知识

- **状态机（State Machine）**：一个对象在不同时刻处于不同的「状态」，每种状态下只允许做特定操作，做完特定操作后转移到下一个状态。比如订单有「待付款→已付款→已发货→已完成」。BOLT 的每个函数也是一个状态机：它从「空的」开始，一步步走到「已发射」。理解状态机，关键是搞清「在什么状态下能做什么、做完转到哪」。
- **反汇编（Disassembly）**：把一串字节「翻译」回一条条机器指令。`56 41 89 e8` 这样的字节，在 x86 下会被翻译成 `pop rsi` 等指令。BOLT 不从源码出发，而是从输入二进制的字节流出发，所以第一步就是把函数体反汇编出来。
- **MCInst**：LLVM MC 层里「一条机器指令」的抽象，是 BOLT 操作指令的基本单位。本讲只把它当成「一条指令」理解即可，下一单元会讲 BOLT 如何在它上面挂元数据。
- **控制流图（CFG）**：把一个函数切成一个个「基本块」（basic block，一段没有内部跳转的连续指令），再用边表示块与块之间的跳转关系，就得到 CFG。CFG 是几乎所有优化（重排、分裂）的基础。
- **`std::map` 与 `SmallVector`**：C++ 容器。`std::map<Key,Value>` 是一棵按 key 排序的树，可以按 key 快速查找；`SmallVector<T>` 是 LLVM 自己的数组，大部分情况下数据就存在对象内部、避免堆分配。本讲会看到函数的指令用 `std::map` 存（要按 offset 查），基本块用 `SmallVector` 存（要按顺序遍历）。

如果你对这几样还比较陌生，先把它们当成「BOLT 加工一个函数时用的容器和阶段标签」即可。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `include/bolt/Core/BinaryFunction.h` | `BinaryFunction` 类的声明，是本讲的主战场：`State` 枚举、`Instructions`、`BasicBlocks`、`Layout`、`IsSimple`、以及 `clearDisasmState` / `resetState` 等接口都在这里。 |
| `lib/Core/BinaryFunction.cpp` | 上述接口的实现，重点看 `disassemble()`（Empty→Disassembled）、`buildCFG()`（Disassembled→CFG）、`clearDisasmState()` / `resetState()`（状态回退），以及那些会把 `IsSimple` 置为 `false` 的判定点。 |
| `include/bolt/Core/FunctionLayout.h` | `FunctionLayout`、`FunctionFragment`、`FragmentNum` 的声明，定义了「输出布局」和「热/冷/暖分片」的抽象。 |
| `lib/Core/FunctionLayout.cpp` | `FunctionLayout` 的实现，重点看 `isSplit()` 如何判定函数是否被拆分。 |
| `lib/Rewrite/RewriteInstance.cpp` | 主管线里调用 `disassemble()` / `buildCFG()` 的地方，能看到「非 simple 函数在 buildCFG 这一步被整体跳过」的关键 `SkipPredicate`。 |

## 4. 核心概念与源码讲解

### 4.1 State 状态机：一个函数的六态生命周期

#### 4.1.1 概念说明

`BinaryFunction` 是 BOLT 对「一个被处理的函数」的统一抽象。这个抽象既可以是输入二进制里真实存在的函数（绝大多数情况），也可以是 BOLT 自己「注入」进去的函数（比如插桩用的运行时代码）。你可以把它理解成一个**带阶段标签的加工件**：它从原料（一段字节）开始，在 BOLT 流水线上被一步步加工，每一道工序都会在它身上盖一个「状态章」，后面的工序只认这个章。

之所以要搞一个显式的状态机，是因为 BOLT 对同一个函数的操作非常多——反汇编、建 CFG、跑几十个优化 pass、发射指令、重写文件——这些操作有**严格的先后依赖**：

- 没反汇编完，不能建 CFG；
- 没建好 CFG，不能跑优化；
- 布局没定下来（`CFG_Finalized`），不能发射。

如果不用状态机约束，就很容易写出「在还没建 CFG 时就去访问基本块」这种 bug。`State` 枚举就是这套约束的载体：很多方法内部都用 `assert(CurrentState == ...)` 来断言「调用我之前你必须处于某个状态」。

#### 4.1.2 核心流程

一个函数在 BOLT 里的完整生命周期可以用下面这个状态机概括：

```
            (构造)
              │
              ▼
          ┌────────┐  disassemble() 成功       ┌──────────────┐
          │ Empty  │ ────────────────────────▶ │ Disassembled │
          └────────┘                            └──────┬───────┘
              ▲                                        │ buildCFG() 成功
              │ resetState()                           ▼
              │                                 ┌──────────────┐
              │                                 │     CFG      │  ← 优化 pass 都在这之后跑
              │                                 └──────┬───────┘
              │                                        │ setFinalized()（布局锁定，禁止再优化）
              │                                        ▼
              │                                 ┌──────────────┐
              │                                 │ CFG_Finalized│
              │                                 └──────┬───────┘
              │                                        │ setEmitted()（开始发射）
              │                                        ▼
              │                                 ┌──────────────┐  保留 CFG
              │                                 │  EmittedCFG  │ ──────────┐
              │                                 └──────┬───────┘           │
              │                                        │ 不保留 CFG         │
              │                                        ▼                    ▼
              │                                 ┌──────────────┐   （对外等同 Emitted）
              └─────────────────────────────────│    Emitted    │
                                                └──────────────┘
```

要点：

1. **构造即 `Empty`**：`BinaryFunction` 一被 `new` 出来，状态就是 `Empty`，函数体为空。
2. **`Empty → Disassembled`**：`disassemble()` 把字节流解码成一条条 `MCInst`，存进指令表。只有当函数「足够简单」（见 4.4）时，才会在结尾把状态推进到 `Disassembled`。
3. **`Disassembled → CFG`**：`buildCFG()` 把扁平的指令表切成基本块、连成 CFG，状态推进到 `CFG`。**绝大多数优化 pass 都要求函数处于 `CFG`（或更靠后的状态）。**
4. **`CFG → CFG_Finalized`**：当布局已经定死、不允许再改基本块顺序时，调用 `setFinalized()`，禁止后续优化。
5. **`CFG_Finalized → EmittedCFG / Emitted`**：发射阶段把指令写出去；如果还需要保留 CFG 信息就停在 `EmittedCFG`，否则释放 CFG 内存、进入 `Emitted`。
6. **任意状态 → `Empty`**：一旦发现这个函数处理不了（比如反汇编失败），`resetState()` 会把它打回 `Empty` 并标记为忽略。

除了「当前是什么状态」，BOLT 还提供了几个**状态查询谓词**，代码里到处在用：

| 谓词 | 为真时函数处于 | 典型用途 |
|------|----------------|----------|
| `hasInstructions()` | `Disassembled` 或任何有 CFG 的状态 | 判断「这个函数现在手里有没有指令」 |
| `hasCFG()` | `CFG` / `CFG_Finalized` / `EmittedCFG` | 判断「CFG 是否已建好」 |
| `isEmitted()` | `EmittedCFG` / `Emitted` | 判断「是否已经发射」 |

#### 4.1.3 源码精读

**状态枚举本身**——六个状态、一行一个注释，是本讲最重要的定义：

[include/bolt/Core/BinaryFunction.h:L136-L143](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/include/bolt/Core/BinaryFunction.h#L136-L143) —— 定义 `enum class State`：`Empty` / `Disassembled` / `CFG` / `CFG_Finalized` / `EmittedCFG` / `Emitted`，注释说明每个状态的含义。

当前状态保存在私有成员 `CurrentState`，默认初始化为 `Empty`：

[include/bolt/Core/BinaryFunction.h:L226-L228](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/include/bolt/Core/BinaryFunction.h#L226-L228) —— `State CurrentState{State::Empty};`，函数一出生就是 `Empty`。

三个查询谓词的实现在头文件里内联给出，逻辑就是对照 `getState()` 返回值：

[include/bolt/Core/BinaryFunction.h:L1161-L1174](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/include/bolt/Core/BinaryFunction.h#L1161-L1174) —— `hasCFG()`、`hasInstructions()`、`isEmitted()`，注意 `hasCFG()` 把 `CFG`、`CFG_Finalized`、`EmittedCFG` 三个状态都算作「有 CFG」。

推进状态的动作分散在 `.cpp` 里。`disassemble()` 的结尾是 `Empty → Disassembled` 的转折点（注意它**只有在函数仍然 simple 时**才会推进，见 4.4）：

[lib/Core/BinaryFunction.cpp:L1593-L1600](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/lib/Core/BinaryFunction.cpp#L1593-L1600) —— 若 `!IsSimple` 则清空指令表、返回非致命错误、**不推进状态**；否则 `updateState(State::Disassembled)`。

`buildCFG()` 的结尾是 `Disassembled → CFG` 的转折点——它在清理完中间结构后才把状态写成 `CFG`，随后还会做一次间接分支后处理：

[lib/Core/BinaryFunction.cpp:L2553-L2562](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/lib/Core/BinaryFunction.cpp#L2553-L2562) —— `CurrentState = State::CFG;`（注意是直接赋值，不走 `updateState`，因为这里要从 `Disassembled` 强制进入 `CFG`）。

布局锁定和发射是两个内联小函数，状态转移非常直白：

[include/bolt/Core/BinaryFunction.h:L2509-L2519](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/include/bolt/Core/BinaryFunction.h#L2509-L2519) —— `setFinalized()` 进入 `CFG_Finalized`；`setEmitted()` 进入 `EmittedCFG`，若不需要保留 CFG 则 `releaseCFG()` 后再进入 `Emitted`。

最后，把状态打成字符串的那个 `switch`（用于 `print()` 输出），方便你对照日志：

[lib/Core/BinaryFunction.cpp:L209-L221](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/lib/Core/BinaryFunction.cpp#L209-L221) —— 把每个 `State` 映射成 `empty` / `disassembled` / `CFG constructed` / `CFG finalized` / `emitted with CFG` / `emitted`。

#### 4.1.4 代码实践

1. **实践目标**：亲手列出 `State` 的全部取值，确认状态机的「节点」。
2. **操作步骤**：
   - 打开 [include/bolt/Core/BinaryFunction.h:L136-L143](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/include/bolt/Core/BinaryFunction.h#L136-L143)。
   - 把六个枚举值和它们的注释抄下来。
3. **需要观察的现象**：注释里写的正是「这个状态下函数手里有什么」——`Empty` 是空，`Disassembled` 有指令列表，`CFG` 有基本块组成的控制流图。
4. **预期结果**：你应该得到六行，对应本节状态机图里的六个方框。
5. **待本地验证**：可选——若你已按 [u1-l2](u1-l2-build-and-run.md) 编出 `llvm-bolt`，对一个简单二进制加 `-print-disasm`（或 `-print-all`）跑一次，在日志里搜索 `CFG constructed` / `CFG finalized` 字样，验证函数确实经过了这些状态。

#### 4.1.5 小练习与答案

**练习 1**：`hasCFG()` 在 `Emitted` 状态下返回 `true` 还是 `false`？为什么？

**参考答案**：返回 `false`。`hasCFG()` 只认 `CFG` / `CFG_Finalized` / `EmittedCFG` 三个状态（见 [L1162-L1165](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/include/bolt/Core/BinaryFunction.h#L1162-L1165)）。一旦进入 `Emitted`，说明 `setEmitted()` 里调用了 `releaseCFG()` 把 CFG 内存释放掉了，所以「CFG 已经不在了」，谓词自然返回 `false`。

**练习 2**：为什么 `buildCFG()` 直接用 `CurrentState = State::CFG` 赋值，而不是先 `assert` 当前处于 `Disassembled`？

**参考答案**：它其实在更前面就用返回值做了等价检查——`buildCFG()` 开头判断 `CurrentState != State::Disassembled` 就直接返回非致命错误（见 [L2321-L2322](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/lib/Core/BinaryFunction.cpp#L2321-L2322)）。能走到 `CurrentState = State::CFG` 这一行，说明前面的前置检查已经通过，状态必然是 `Disassembled`，所以无需再 `assert` 一次。

### 4.2 指令表 InstrMapType 与 addInstruction：CFG 构建前的临时容器

#### 4.2.1 概念说明

在函数还没有 CFG 之前，它手里的指令需要一个地方「暂存」。BOLT 的做法是用一个**按 offset 排序的映射**：`InstrMapType`，本质是 `std::map<uint32_t, MCInst>`——key 是这条指令在函数内部的偏移（从函数起始地址算起的字节数），value 是解码出来的那条指令。

为什么要用「按 offset 排序的 map」而不是一个数组？因为反汇编过程中要频繁地「在某个 offset 上回查指令」（比如判断一个跳转目标落在哪条指令上），`std::map` 天然支持按 key 快速定位，且保持有序，遍历时就是地址从低到高的顺序。

但这个指令表是**临时的**。一旦 `buildCFG()` 把指令划分进各个基本块，这张表就会被清空（指令的「归属」从「整张函数的大表」转移到了「一个个基本块」）。所以你可以把 `Instructions` 理解成「CFG 构建期间的脚手架」，楼（CFG）盖好之后就拆掉。

除了指令表，这个阶段还有两个相关的「表」：

- `Labels`：offset → 符号。反汇编时给每个潜在的基本块起点贴一个标签，建 CFG 时靠它切分基本块。
- `FrameInstructions`：CFI（Call Frame Information，调用栈帧信息）指令列表。异常处理、栈展开需要它，BOLT 重排基本块后要重放这些 CFI 来保证状态正确——这正是 `CFG_Finalized` 阶段的重要工作。

#### 4.2.2 核心流程

反汇编与建 CFG 这两步，对指令表的「填—用—清」可以概括为：

```
   字节流 (ArrayRef<uint8_t>)
        │
        │  disassemble(): for 每个 offset，解码一条 MCInst
        ▼
   ┌─────────────────────────────┐
   │  Instructions: map<offset,   │   ← addInstruction(offset, inst)
   │                    MCInst>   │
   └──────────────┬──────────────┘
                  │  buildCFG(): 遇到 label 就切出新基本块，
                  │             把指令挪进 BinaryBasicBlock
                  ▼
   ┌─────────────────────────────┐
   │  BasicBlocks: SmallVector<   │
   │     BinaryBasicBlock*>       │   ← 每个块内部持有自己的指令
   └──────────────┬──────────────┘
                  │  清空中间结构
                  ▼
            clearList(Instructions)   ← 指令表使命完成，被清空
```

换句话说：**同一份指令，先以 `offset→MCInst` 的形式躺在指令表里，建完 CFG 后改以「基本块里的指令序列」形式存在**。`hasInstructions()` 这个谓词之所以对 `Disassembled` 和「有 CFG 的状态」都返回 `true`（见 4.1.3），正是因为这两种状态下函数手里都「有指令」，只是存放形态不同。

#### 4.2.3 源码精读

**指令表的类型与成员声明**——注意注释里明确写了「Temporary holder of instructions before CFG is constructed」（CFG 构建前的临时容器）：

[include/bolt/Core/BinaryFunction.h:L530-L533](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/include/bolt/Core/BinaryFunction.h#L530-L533) —— `using InstrMapType = std::map<uint32_t, MCInst>;` 与成员 `InstrMapType Instructions;`。

往表里加指令的接口就一行，把指令用 `emplace` 按 offset 插进 map：

[include/bolt/Core/BinaryFunction.h:L723-L725](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/include/bolt/Core/BinaryFunction.h#L723-L725) —— `addInstruction(uint64_t Offset, MCInst &&Instruction)`，内部 `Instructions.emplace(Offset, ...)`。

真正反复调用它的地方，是 `disassemble()` 的主循环末尾——解码、符号化、补上调试行号信息之后，才把这条指令存进表：

[lib/Core/BinaryFunction.cpp:L1574-L1575](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/lib/Core/BinaryFunction.cpp#L1574-L1575) —— `addInstruction(Offset, std::move(Instruction));`，循环每解码一条指令就调用一次。

与指令表配套的 `Labels`（offset→符号）和 CFI 指令表 `FrameInstructions`：

[include/bolt/Core/BinaryFunction.h:L523-L528](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/include/bolt/Core/BinaryFunction.h#L523-L528) —— `LabelsMapType Labels`，注释点明它「用于为 simple 函数建 CFG；非 simple 函数在重定位模式下也要为重定位引用（如跳转表）发射它们」。

[include/bolt/Core/BinaryFunction.h:L535-L556](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/include/bolt/Core/BinaryFunction.h#L535-L556) —— `CFIInstrMapType FrameInstructions`，注释解释了 BOLT 为什么不直接解码 CFI 状态机，而是维护一个「CFI State」概念：基本块重排后要重放 CFI 才能到达正确的栈帧状态。

「填表—用表—清表」的闭环在 `buildCFG()` 末尾完成——建完基本块后，指令表和分支列表一起被清掉：

[lib/Core/BinaryFunction.cpp:L2553-L2562](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/lib/Core/BinaryFunction.cpp#L2553-L2562) —— `clearList(Instructions); clearList(OffsetToCFI); clearList(TakenBranches);`，随后才 `CurrentState = State::CFG;`。这里能看到 `Labels` 故意**没有**被清（注释说明：万一后续把函数标记为非 simple，可能还要用到它）。

#### 4.2.4 代码实践

1. **实践目标**：跟踪一条指令在反汇编阶段的「归宿」，理解指令表只是临时容器。
2. **操作步骤**：
   - 阅读 [lib/Core/BinaryFunction.cpp:L1340-L1575](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/lib/Core/BinaryFunction.cpp#L1340-L1575) 的 `disassemble()` 主循环。
   - 找到「解码 `MCInst` → 处理分支/CFI/调试行号 → `addInstruction`」这条路径。
   - 再跳到 [buildCFG 末尾的 clearList(Instructions)](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/lib/Core/BinaryFunction.cpp#L2553-L2562)。
3. **需要观察的现象**：同一条指令，先以 `(offset, MCInst)` 进表，建完 CFG 后这份副本被丢弃，指令本身被搬进某个 `BinaryBasicBlock`。
4. **预期结果**：你能用一句话回答「`Instructions` 这个表什么时候是空的」——答：函数处于 `Empty`、`CFG` 及之后的状态时（除非中间被回退）。
5. **待本地验证**：可选——在 `addInstruction` 处加一条 `LLVM_DEBUG` 日志打印 offset，用 `-debug` 跑一个小函数，观察指令确实是按 offset 递增被填入的。

#### 4.2.5 小练习与答案

**练习 1**：`Instructions` 用的是 `std::map` 而不是 `std::unordered_map`，为什么？

**参考答案**：因为反汇编和建 CFG 过程都需要**按 offset 顺序**处理指令（比如 `buildCFG()` 创建基本块时就依赖「指令按 offset 有序」这一前提，见 [L2328-L2342](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/lib/Core/BinaryFunction.cpp#L2328-L2342) 的注释）。`std::map` 按 key 排序，遍历即地址递增；`unordered_map` 不保证顺序，会破坏这个前提。

**练习 2**：为什么 `buildCFG()` 末尾清空了 `Instructions` 和 `TakenBranches`，却**保留**了 `Labels`？

**参考答案**：见 [L2553-L2557](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/lib/Core/BinaryFunction.cpp#L2553-L2557) 的注释——后续如果发现额外的入口点、或不得不把函数降级为非 simple，可能还要用到 `Labels`，所以故意不清。`Instructions` / `TakenBranches` 一旦搬进基本块就再无用处，于是清掉省内存。

### 4.3 FunctionLayout：输出顺序与热冷分片

#### 4.3.1 概念说明

`FunctionLayout` 回答的是一个完全不同的问题：**这个函数的基本块，在新二进制里要按什么顺序排列？**

注意它和指令表、和基本块列表（`BasicBlocks`）的区别：

- `BasicBlocks`：函数拥有的所有基本块的集合（包括被删除但还留着做地址映射的 `DeletedBasicBlocks`）。
- `FunctionLayout`：这些基本块在**输出**二进制里的**排列顺序**，而且是分「片」（fragment）组织的。

「分片」是 `FunctionLayout` 最核心的设计。一个函数的代码不一定要全部挤在一起——BOLT 可以把它拆成几段，每段叫一个 `FunctionFragment`，分别放进不同的 section：

- **main 片**（`FragmentNum::main()`，编号 0）：函数主体，包含所有入口块和不能被拆走的块。
- **cold 片**（`FragmentNum::cold()`，编号 1）：冷代码，运行时很少执行，拆出去放进 `.text.cold`，让出宝贵的指令缓存空间给热代码。
- **warm 片**（`FragmentNum::warm()`，编号 2）：介于热冷之间，配合 cdsplit 策略做更细的三级划分。

同一片内的块在输出里是**连续**的，但片与片之间是**分离**的（落在不同 section）。这就是 BOLT 「热冷分裂」（hot/cold split）的底层载体——没有 `FunctionLayout` 的分片抽象，就没法表达「这个块要挪到冷 section」。

`BinaryFunction::isSplit()` 这个谓词就是问「这个函数现在是不是被拆成了至少两片非空的片段」，它直接由 `FunctionLayout::isSplit()` 实现（而且前提是函数得是 simple 的，见 4.4）。

#### 4.3.2 核心流程

`FunctionLayout` 的内部结构可以画成：

```
   FunctionLayout
   ├── Blocks:  [ BB0 | BB1 | BB2 | BB3 | BB4 | BB5 ]   ← 所有块连续存放
   └── Fragments:
         ├── Fragment 0 (main):  [ BB0, BB1, BB2 ]      ← 连续一段
         ├── Fragment 1 (cold):  [ BB3, BB4 ]           ← 连续一段（.text.cold）
         └── Fragment 2 (warm):  [ BB5 ]                ← 可选

   排列顺序 = 输出顺序 = 优化 pass 重排的目标
```

关键操作：

1. **建 CFG 时初始化**：`buildCFG()` 把基本块按原始地址顺序加进 layout，此时通常只有 main 一片。
2. **优化 pass 重排**：各种重排 pass 调 `update(NewLayout)` 用一个新顺序替换旧顺序；`update()` 会根据每个块自带的 `FragmentNum` 自动推断它属于哪一片。
3. **热冷分裂 pass**：`SplitFunctions` 把冷块挪到 cold 片，于是 layout 出现第二片，`isSplit()` 变 `true`。
4. **发射时落地**：发射阶段（`BinaryEmitter`，见单元 7）按 `FunctionLayout` 遍历每一片，main 片进 `.text`，cold 片进 `.text.cold`。

#### 4.3.3 源码精读

**文件头注释**已经把 `FunctionLayout` 的定位讲得很清楚——「layout 就是基本块在新二进制里的排列顺序；可以拆成多个 fragment，片内连续、片间分离，用于热冷分离」：

[include/bolt/Core/FunctionLayout.h:L8-L16](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/include/bolt/Core/FunctionLayout.h#L8-L16) —— `FunctionLayout` 的设计意图说明。

**片号枚举 `FragmentNum`**，三个静态工厂方法定义了 main/cold/warm：

[include/bolt/Core/FunctionLayout.h:L63-L65](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/include/bolt/Core/FunctionLayout.h#L63-L65) —— `main()`=0、`cold()`=1、`warm()`=2。

**`FunctionFragment`** 是「一片连续的块」，除了块列表，还携带这片代码在输出里的地址（`Address`）、在 codegen 内存里的地址/大小（`ImageAddress`/`ImageSize`）、文件偏移（`FileOffset`）——这些都是发射和链接阶段要用的：

[include/bolt/Core/FunctionLayout.h:L68-L102](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/include/bolt/Core/FunctionLayout.h#L68-L102) —— `FunctionFragment` 类与它的地址/大小/偏移成员。

**`FunctionLayout` 类本身**，提供按片遍历、按块遍历两套接口，以及 `update()`（替换布局）、`addFragment()`（新增片）：

[include/bolt/Core/FunctionLayout.h:L132-L167](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/include/bolt/Core/FunctionLayout.h#L132-L167) —— 类的声明与内部两个成员 `Fragments`、`Blocks`。

`getMainFragment()` 永远返回第 0 片，`getSplitFragments()` 返回除 main 外的所有片（cold/warm）：

[include/bolt/Core/FunctionLayout.h:L177-L199](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/include/bolt/Core/FunctionLayout.h#L177-L199) —— `getMainFragment()` 与 `getSplitFragments()`。

`update()` 是重排 pass 的主入口——它接收一个新顺序，按块自带的片号重新分片，并返回「新顺序是否和旧顺序不同」：

[include/bolt/Core/FunctionLayout.h:L220-L224](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/include/bolt/Core/FunctionLayout.h#L220-L224) —— `bool update(ArrayRef<BinaryBasicBlock *> NewLayout);`。

`isSplit()` 与 `isHotColdSplit()` 两个判定：

[include/bolt/Core/FunctionLayout.h:L260-L271](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/include/bolt/Core/FunctionLayout.h#L260-L271) —— `isSplit()`（至少两片非空）与 `isHotColdSplit()`（至多两片）的声明。

`isSplit()` 的实现——统计非空片的个数，≥2 即为已拆分：

[lib/Core/FunctionLayout.cpp:L250-L254](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/lib/Core/FunctionLayout.cpp#L250-L254) —— `FunctionLayout::isSplit()` 统计 `!FF.empty()` 的片数。

回到 `BinaryFunction`：它持有一个 `FunctionLayout Layout` 成员，并提供 `getLayout()` 访问器；`isSplit()` 方法把这个查询**包了一层 isSimple 前置条件**——只有 simple 函数才谈得上「被拆分」：

[include/bolt/Core/BinaryFunction.h:L615-L619](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/include/bolt/Core/BinaryFunction.h#L615-L619) —— `BasicBlocks` / `DeletedBasicBlocks` / `Layout` 三个成员并排声明。

[include/bolt/Core/BinaryFunction.h:L1459-L1460](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/include/bolt/Core/BinaryFunction.h#L1459-L1460) —— `bool isSplit() const { return isSimple() && getLayout().isSplit(); }`，非 simple 函数直接返回 `false`。

#### 4.3.4 代码实践

1. **实践目标**：搞清「main 片」与「cold 片」如何在 layout 里被区分，以及 `isSplit()` 的判定依据。
2. **操作步骤**：
   - 阅读 [FragmentNum 的 main/cold/warm 定义](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/include/bolt/Core/FunctionLayout.h#L63-L65)。
   - 对照 [FunctionLayout::isSplit() 实现](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/lib/Core/FunctionLayout.cpp#L250-L254)。
   - 再看 [BinaryFunction::isSplit()](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/include/bolt/Core/BinaryFunction.h#L1459-L1460) 这层封装。
3. **需要观察的现象**：一个函数默认只有 main 一片（`isSplit()` 为 `false`）；只有当某个优化 pass（`SplitFunctions`）把块挪到 cold 片后，非空片数才变成 2，`isSplit()` 变 `true`。
4. **预期结果**：你能回答「`isSplit()` 为 `true` 需要满足哪两个条件」——①函数是 simple 的；②layout 里至少有两片非空。
5. **待本地验证**：可选——用 `-split-functions -print-only=你的函数名` 跑一次，在输出里确认函数多了 `.text.cold` 片段。

#### 4.3.5 小练习与答案

**练习 1**：`isHotColdSplit()` 和 `isSplit()` 有什么区别？

**参考答案**：`isSplit()` 问「现在是否真的被拆开了」（至少两片**非空**）；`isHotColdSplit()` 问「至多两片」（即 fragment 总数 ≤ 2，不关心是否非空）。后者是一个**能力上限**检查，用于「某些处理逻辑还不支持 ≥3 片（hot/warm/cold）」的地方做守卫（见 [L268-L271](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/include/bolt/Core/FunctionLayout.h#L268-L271) 注释）。

**练习 2**：为什么 `BinaryFunction::isSplit()` 要先判断 `isSimple()`？

**参考答案**：非 simple 函数连 CFG 都建不完整，更不会被任何重排/分裂优化 pass 处理，它的 `Layout` 也就不会被拆片。直接对非 simple 函数调用 `getLayout().isSplit()` 没有意义（甚至可能误导调用者以为它被优化过），所以用 `isSimple()` 短路返回 `false`。

### 4.4 isSimple：函数能否被优化的分水岭

#### 4.4.1 概念说明

并不是输入二进制里的每个函数都能被 BOLT 优化。有些函数太「古怪」——比如含有 BOLT 无法完全确定的间接跳转、含有无法求值的 PC 相对寻址、或者根本反汇编不出像样的 CFG——对这些函数，BOLT 采取的策略是「**看得懂就优化，看不懂就原样搬运**」。

这个「看得懂 / 看不懂」的开关，就是 `IsSimple` 标志：

- `isSimple() == true`：BOLT 能够完整重建这个函数的 CFG，可以放心地重排、分裂、改写。
- `isSimple() == false`：函数「太复杂」，**不参与任何优化**；但在有重定位（`--emit-relocs`，见 u1-l3）的「重定位模式」下，BOLT 仍然会把它反汇编再重新汇编一遍（搬运到新位置），只是不做任何布局优化。

所以 `isSimple()` 是「函数能否被常规优化」的**分水岭**。它和状态机的关系是：`disassemble()` 在结尾会**检查 `IsSimple` 是否还为 `true`**，只有为 `true` 才把状态推进到 `Disassembled`（见 4.1.3）；后续 `buildCFG` 阶段更是用 `SkipPredicate` 把所有非 simple 函数**整体跳过**。

那么什么时候 `IsSimple` 会被置为 `false`？主要在反汇编和建 CFG 过程中，遇到这些「拿不准」的情况：

- 间接分支被判定为**可能的跳转表 / PIC 跳转表 / PIC 定长分支**，而用户又用 `-jump-tables=none`（`JTS_NONE`）禁用了跳转表处理；
- 某条指令的 PC 相对寻址**无法求值**（BOLT 算不出它引用的地址）；
- 函数含有**不被支持的指令**；
- `buildCFG()` 切出来的基本块为空，或最终 CFG 校验失败（间接分支无法自洽）；
- 函数尺寸为 0，或管线决定不反汇编它（`shouldDisassemble` 为假）。

一旦命中任何一条，这个函数就被「降级」为非 simple，退出优化竞争。

#### 4.4.2 核心流程

`IsSimple` 的生命周期：

```
   构造函数: IsSimple = true  (默认)
              │
              ▼
   disassemble() / buildCFG() 过程中:
        ├─ 跳转表 + JTS_NONE ────────┐
        ├─ PC-rel 求值失败 ──────────┤
        ├─ 不支持的指令 ─────────────┼──▶ setSimple(false)
        ├─ 基本块为空 ───────────────┤      (或 IsSimple = false)
        └─ CFG 校验失败 ─────────────┘
              │
              ▼
   disassemble() 结尾:
        if (!IsSimple) { clearList(Instructions); return 非致命错误; }  ← 不推进状态
        else updateState(Disassembled);
              │
              ▼
   buildCFG 阶段 (并行, in RewriteInstance):
        SkipPredicate = !shouldDisassemble(BF) || !BF.isSimple()
        ── 非 simple 函数直接被跳过，不建 CFG、不进优化管线 ──
```

当反汇编彻底失败、需要把函数打回原形时，`clearDisasmState()` 和 `resetState()` 负责回退：

- `clearDisasmState()`：**轻量回退**——只清空反汇编阶段产生的中间数据（指令表、分支列表），不动 CFG，不改状态。用于「反汇编出来的中间结果作废，但函数状态本身不变」。
- `resetState()`：**重量回退**——`clearDisasmState()` + 如果已有 CFG 就 `releaseCFG()` 并删除所有基本块、清空 `Layout`，然后把 `IsSimple=false`、`IsIgnored=true`、`CurrentState=Empty`。用于「这个函数彻底处理不了，打回 `Empty` 并忽略它」。

#### 4.4.3 源码精读

**`IsSimple` 成员**——默认 `true`，注释说明「`false` 表示函数太复杂、无法重建 CFG；在重定位模式下仍会反汇编+重汇编」：

[include/bolt/Core/BinaryFunction.h:L315-L318](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/include/bolt/Core/BinaryFunction.h#L315-L318) —— `bool IsSimple{true};` 与注释。

访问与设置接口：

[include/bolt/Core/BinaryFunction.h:L1432-L1433](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/include/bolt/Core/BinaryFunction.h#L1432-L1433) —— `isSimple()` 返回 `IsSimple`。

[include/bolt/Core/BinaryFunction.h:L1886-L1889](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/include/bolt/Core/BinaryFunction.h#L1886-L1889) —— `setSimple(bool)` 设置它。

**`disassemble()` 结尾的 simple 门**——这是「非 simple 函数不会进入 `Disassembled` 状态」的关键：

[lib/Core/BinaryFunction.cpp:L1593-L1598](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/lib/Core/BinaryFunction.cpp#L1593-L1598) —— `if (!IsSimple) { clearList(Instructions); return createNonFatalBOLTError(""); }` 然后 `updateState(State::Disassembled);`。

**几处把函数降级为非 simple 的判定点**（都是反汇编/建 CFG 中的「拿不准」时刻）：

[lib/Core/BinaryFunction.cpp:L1131-L1134](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/lib/Core/BinaryFunction.cpp#L1131-L1134) —— PC 相对寻址无法求值时（非重定位模式下）置 `IsSimple = false`。

[lib/Core/BinaryFunction.cpp:L1216-L1217](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/lib/Core/BinaryFunction.cpp#L1216-L1217) —— 间接分支疑似跳转表/PIC 跳转表，且 `-jump-tables=none` 时置 `IsSimple = false`。

[lib/Core/BinaryFunction.cpp:L2457-L2459](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/lib/Core/BinaryFunction.cpp#L2457-L2459) —— `buildCFG()` 切出的基本块为空，置 `setSimple(false)`。

[lib/Core/BinaryFunction.cpp:L2576-L2583](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/lib/Core/BinaryFunction.cpp#L2576-L2583) —— CFG 间接分支后处理 / 内部引用校验失败，置 `setSimple(false)`。

**`buildCFG()` 开头的 simple 守卫**——非 simple 函数根本不让建 CFG：

[lib/Core/BinaryFunction.cpp:L2315-L2319](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/lib/Core/BinaryFunction.cpp#L2315-L2319) —— `if (!isSimple()) { assert(!BC.HasRelocations ...); return createNonFatalBOLTError(""); }`。

**主管线里跳过非 simple 函数的 `SkipPredicate`**——这是「非 simple 函数不进优化管线」的最直接证据：

[lib/Rewrite/RewriteInstance.cpp:L4059-L4066](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/lib/Rewrite/RewriteInstance.cpp#L4059-L4066) —— `SkipPredicate = [&](const BinaryFunction &BF) { return !shouldDisassemble(BF) || !BF.isSimple(); };`，并行建 CFG 时对每个函数判断是否跳过。

**状态回退的两个函数**。先看轻量的 `clearDisasmState()`：

[lib/Core/BinaryFunction.cpp:L3309-L3313](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/lib/Core/BinaryFunction.cpp#L3309-L3313) —— 只清 `Instructions` / `IgnoredBranches` / `TakenBranches` 三个中间列表，不碰 CFG，不改 `CurrentState`。

再看重量的 `resetState()`：

[lib/Core/BinaryFunction.cpp:L3315-L3337](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/lib/Core/BinaryFunction.cpp#L3315-L3337) —— 先 `clearDisasmState()`；若已有 CFG 则 `releaseCFG()`、`delete` 所有基本块、`Layout.clear()`；最后 `IsSimple=false; IsIgnored=true; CurrentState=State::Empty;`。

与之配套的 `setIgnored()`——当管线决定忽略一个函数（比如反汇编失败）时调用，内部就会触发 `resetState()`：

[lib/Core/BinaryFunction.cpp:L3352-L3380](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/lib/Core/BinaryFunction.cpp#L3352-L3380) —— `setIgnored()`：置 `IsSimple=false`，若函数已非 `Empty` 则 `resetState()` 打回原形，并在有重定位时 `scanExternalRefs()` 修复外部引用。

#### 4.4.4 代码实践

1. **实践目标**：说清反汇编失败时的状态回退路径，并解释「为什么非 simple 函数不能被常规优化」。
2. **操作步骤**：
   - 阅读 [`clearDisasmState()`](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/lib/Core/BinaryFunction.cpp#L3309-L3313) 与 [`resetState()`](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/lib/Core/BinaryFunction.cpp#L3315-L3337)。
   - 找到反汇编失败时谁调用它们：在 [`disassemble()` 主循环](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/lib/Core/BinaryFunction.cpp#L1350-L1372) 里，解码失败会调 `setIgnored()`，而 `setIgnored()` 内部触发 `resetState()`。
   - 再看主管线 [`SkipPredicate`](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/lib/Rewrite/RewriteInstance.cpp#L4059-L4066) 如何把非 simple 函数挡在优化管线外。
3. **需要观察的现象**：反汇编失败 → `setIgnored()` → `resetState()` → 状态回到 `Empty`、`IsSimple=false`、`IsIgnored=true` → 后续 `buildCFG` 的 `SkipPredicate` 因 `!BF.isSimple()` 直接跳过该函数。
4. **预期结果**：你能用两句话回答——①状态回退：`resetState()` 清空 CFG/指令表/布局并回到 `Empty`，同时把函数标记为忽略；②为什么不能优化：常规优化（重排、分裂、ICP 等）都建立在「CFG 完整可信」之上，非 simple 函数的 CFG 不完整或不可信，强行优化会改变语义、破坏正确性，所以 BOLT 选择只搬运不优化。
5. **待本地验证**：可选——构造一个含 `computed goto` 或不可解析跳转表的小函数，用 `-jump-tables=none` 跑 BOLT，观察日志里是否出现「could not disassemble / will ignore」并确认该函数在最终二进制里被原样保留。

#### 4.4.5 小练习与答案

**练习 1**：`clearDisasmState()` 和 `resetState()` 的区别是什么？各自用于什么场景？

**参考答案**：`clearDisasmState()` 是**轻量**回退，只清掉反汇编的三个中间列表（指令表 + 两个分支列表），**不改状态、不删基本块**，适合「中间结果作废但函数还要继续被处理」（比如 `setTrapOnEntry()` 重置后再塞 trap 指令）。`resetState()` 是**重量**回退，在 `clearDisasmState()` 基础上，若有 CFG 则释放并删除所有基本块、清空 `Layout`，并把状态打回 `Empty`、`IsSimple=false`、`IsIgnored=true`，适合「这个函数彻底处理不了，放弃」。

**练习 2**：假设 `disassemble()` 跑到一半发现一条无法解码的指令，函数会经历哪些状态变化？

**参考答案**：解码失败时（见 [L1358-L1372](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/lib/Core/BinaryFunction.cpp#L1358-L1372)），在重定位模式 + `TrapOnAVX512` 下可能调 `setTrapOnEntry()`（函数被改造成入口即 trap）；否则调 `setIgnored()`。`setIgnored()` 会（在非 `processAllFunctions` 模式下）置 `IsSimple=false`，由于当前状态已经不是 `Empty`，于是触发 `resetState()`，状态从 `Empty`（反汇编期间函数仍处于 `Empty`，因为 `disassemble()` 结尾才推进状态）回退——注意此时 `CurrentState` 仍是 `Empty`，`resetState()` 的 `if (hasCFG())` 分支不进入，主要生效的是 `IsSimple=false; IsIgnored=true; CurrentState=Empty;`。最终该函数因 `!isSimple()` 被 `buildCFG` 阶段的 `SkipPredicate` 跳过。

**练习 3**：为什么「在重定位模式下，非 simple 函数仍然会被反汇编+重汇编，而不被直接丢弃」？

**参考答案**：因为有 `--emit-relocs`，BOLT 拥有完整的重定位信息，能够安全地把任意函数（哪怕 CFG 不完整）的字节搬移到新地址并修正重定位。丢弃它会导致符号引用悬空；而原样搬运（反汇编再汇编）既保留了函数行为，又不依赖 CFG 的正确性。注释里 `IsSimple` 的说明（[L315-L318](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/include/bolt/Core/BinaryFunction.h#L315-L318)）正是这句：「In relocation mode we still disassemble and re-assemble such functions.」

## 5. 综合实践

把本讲四个最小模块串起来，完成下面这个贯穿任务（即本讲指定的代码实践）：

> **任务**：在 `BinaryFunction.h` 里找到 `enum class State`，列出全部状态；然后阅读 `clearDisasmState()` / `resetState()`，说明反汇编失败时函数如何回退状态；最后解释为什么非 simple 函数不能被常规优化。

**建议步骤**：

1. **列状态**：打开 [State 枚举](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/include/bolt/Core/BinaryFunction.h#L136-L143)，写出六个状态及其一句话含义，并画出它们的状态转移图（参考 4.1.2）。标注哪一步对应反汇编（`Empty→Disassembled`）、哪一步对应建 CFG（`Disassembled→CFG`）。

2. **追回退路径**：从 [`disassemble()` 主循环里的解码失败分支](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/lib/Core/BinaryFunction.cpp#L1350-L1372) 出发，跟踪 `setIgnored()` → [`resetState()`](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/lib/Core/BinaryFunction.cpp#L3315-L3337)。在一张表里对比 `clearDisasmState()`（轻量、不改状态）和 `resetState()`（重量、回 `Empty`）各自清掉了什么。

3. **解释优化禁令**：结合 [`buildCFG` 的 simple 守卫](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/lib/Core/BinaryFunction.cpp#L2315-L2319) 和 [主管线的 `SkipPredicate`](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/lib/Rewrite/RewriteInstance.cpp#L4059-L4066)，说明非 simple 函数被挡在优化管线之外的两层关卡。然后写出你的结论：常规优化依赖完整可信的 CFG；非 simple 函数 CFG 不完整，优化会破坏正确性，所以只搬运不优化。

4. **（可选）验证**：若已编出 `llvm-bolt`，挑一个含跳转表的小程序，用 `-jump-tables=none -print-all` 跑一次，在日志里找出被标记为 ignored / non-simple 的函数，确认它的状态轨迹与本讲描述一致。

**预期产出**：一张状态转移图 + 一张 `clearDisasmState`/`resetState` 对比表 + 一段「为什么非 simple 不能优化」的说明。完成后，你就把 State 状态机、指令表/布局、isSimple 这三件事在「反汇编失败」这个真实场景下打通了。

## 6. 本讲小结

- `BinaryFunction` 是 BOLT 对「一个被处理的函数」的统一抽象，它是一个**带阶段标签的加工件**，靠 `State` 状态机约束各操作的先后顺序。
- 六个状态 `Empty → Disassembled → CFG → CFG_Finalized → EmittedCFG → Emitted` 分别对应「空 → 反汇编出指令 → 建好 CFG → 布局锁定 → 发射（保留CFG）→ 发射（释放CFG）」；`hasCFG()` / `hasInstructions()` / `isEmitted()` 是常用的状态查询谓词。
- 指令表 `InstrMapType`（`offset→MCInst`）是 **CFG 构建前的临时容器**，由 `disassemble()` 填充、`buildCFG()` 消费后清空；指令随后以「基本块内的序列」形式存在。
- `FunctionLayout` 描述**输出顺序**与**热/冷/暖分片**，是热冷分裂的底层载体；`isSplit()` 判定函数是否被拆成至少两片非空，且前提是函数必须 simple。
- `isSimple()` 是「函数能否被常规优化」的分水岭：反汇编/建 CFG 中遇到「拿不准」的情况就 `setSimple(false)`，非 simple 函数在 `buildCFG` 阶段被 `SkipPredicate` 整体跳过，不进优化管线（但重定位模式下仍会被搬运）。
- `clearDisasmState()` 是轻量回退（只清中间列表），`resetState()` 是重量回退（清 CFG、回 `Empty`、标记忽略）；反汇编失败时经 `setIgnored()` → `resetState()` 把函数打回原形。

## 7. 下一步学习建议

- **下一讲 [u2-l3](u2-l3-basic-block-and-cfg.md)**：钻进函数内部，学习 `BinaryBasicBlock` 如何作为 CFG 节点、如何维护后继/前驱边，以及支配树（`BinaryDomTree`）与循环信息（`BinaryLoop`）。本讲里的 `BasicBlocks` 列表和 `FunctionLayout`，到了下一讲才会看到它们「内部装的是什么」。
- **单元 3**：如果你更想先看「这些数据结构是怎么被填起来的」，可以直接跳到 [u3-l1](u3-l1-run-pipeline.md) 跟着 `RewriteInstance::run()` 走一遍主管线，亲眼看到 `disassemble()` / `buildCFG()` 在哪一步被调用、状态机如何随管线推进。
- **延伸阅读源码**：在进入下一讲前，建议先扫一眼 [`BinaryFunction.h` 里 `State` 枚举下方的若干 `assert(CurrentState == ...)`](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/include/bolt/Core/BinaryFunction.h#L136-L143)，体会状态机是如何在 API 层面强制约束调用时机的——这是理解 BOLT 代码「为什么这么写」的一把钥匙。

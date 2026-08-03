# BinaryBasicBlock 与控制流图表示

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 `BinaryBasicBlock` 在 BOLT 里是什么——它是 CFG 的「节点」，同时装着这个节点的指令序列、出边/入边、分支信息、执行计数和若干定位/对齐属性。
- 解释 CFG 边的「双向一致性」：为什么 `Successors` 和 `Predecessors` 永远要成对维护，`addSuccessor` / `removeSuccessor` 内部是怎么做到这一点的。
- 读懂 BOLT 如何用并列数组 `Successors` + `BranchInfo` 表示「条件/无条件、taken/fall-through」这两类分支，以及间接分支（跳转表）如何折算成多个后继。
- 用 `BinaryDominatorTree` / `BinaryLoopInfo` 取出一个块的支配者和它所属的循环，并理解这两个结构在 BOLT 里是「目标无关」地复用 LLVM 通用模板的。

本讲是单元 2（核心数据结构）的第三篇，承接 [u2-l2](u2-l2-binary-function.md) 里「`BinaryFunction` 是带阶段标签的加工件，状态推进到 `CFG` 后指令就搬进基本块」的结论。上一讲把镜头停在了函数这一层；本讲把镜头推进到函数内部，拆开 CFG 的基本单元 `BinaryBasicBlock`。它既是后续所有优化 pass（重排、分裂、调用提升）的操作对象，也是 `MCPlus` 元数据（[u2-l4](u2-l4-mcplus-metadata.md)）挂载的载体。

## 2. 前置知识

- **基本块（Basic Block）**：一段「除了最后一条，中间没有跳转进来、也没有跳转出去」的连续指令。执行只能从块的第一条指令进入、从最后一条离开。把函数切成一个个基本块，再用箭头连起来，就是控制流图（CFG）。
- **前驱 / 后继（Predecessor / Successor）**：如果块 A 末尾可能跳到块 B，就说 B 是 A 的**后继**，A 是 B 的**前驱**。CFG 的边是有向的，A→B 这条边在 A 里叫「后继」，在 B 里叫「前驱」，是同一条边的两个视角。
- **fall-through 与 taken**：x86/AArch64 的条件跳转通常配一条「条件不成立时顺次往下走」的路径，叫 **fall-through**；「条件成立跳过去」的那条叫 **taken**。BOLT 用一个固定约定记录这两条边（下文 4.2 会讲）。
- **支配（Dominance）**：在 CFG 里，如果从函数入口到块 B 的**所有**路径都必须经过块 A，就说 A 支配 B。入口块支配所有块。「直接支配者（immediate dominator，IDom）」是离 B 最近的那个支配它的块。所有块的 IDom 关系构成一棵树，叫**支配树（Dominator Tree）**。
- **循环（Loop）与回边（Back Edge）**：CFG 里如果存在一条边 B→H，而 H 支配 B，这条边就是**回边**，它定义了一个以 H 为**循环头（header）**的循环。循环可以嵌套，有深度之分。
- **`SmallVector<T>` 与并列数组**：LLVM 自带的动态数组；本讲会看到 `Successors`（后继块指针）和 `BranchInfo`（分支计数）两个**等长、按下标一一对应**的并列数组，这是一种很常见的「把节点和边上的属性对齐存储」的写法。

如果这些名词还生疏，先把「基本块 = CFG 节点」「支配树/循环 = 在 CFG 上算出来的两种分析结构」记住即可。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `include/bolt/Core/BinaryBasicBlock.h` | `BinaryBasicBlock` 类的声明，是本讲主战场：`Instructions`、`Predecessors` / `Successors`、`BranchInfo`、`ExecutionCount`、`addSuccessor` / `removeSuccessor`、`getConditionalSuccessor`、`analyzeBranch`、`splitAt`、`validateSuccessorInvariants` 等都在这里；文件末尾还有让 CFG 能被当图用的 `GraphTraits` 特化。 |
| `lib/Core/BinaryBasicBlock.cpp` | 上述接口的实现，重点看 `addSuccessor` / `removeSuccessor` / `removePredecessor` 如何维护双向边，`validateSuccessorInvariants` 如何自检，以及 `splitAt` 如何在切块时迁移后继。 |
| `include/bolt/Core/BinaryDomTree.h` | 支配树的类型别名：`BinaryDominatorTree = DomTreeBase<BinaryBasicBlock>`、`BinaryDomTreeNode = DomTreeNodeBase<BinaryBasicBlock>`，几乎是「零代码」地复用 LLVM 通用支配树模板。 |
| `include/bolt/Core/BinaryLoop.h` | 循环抽象：`BinaryLoop`（带 `TotalBackEdgeCount` / `EntryCount` / `ExitCount` 三个 profile 字段）和 `BinaryLoopInfo`（带 `OuterLoops` / `TotalLoops` / `MaximumDepth` 统计）。 |
| `include/bolt/Core/BinaryFunction.h` | `BinaryFunction` 持有 `BDT`（支配树）和 `BLI`（循环信息）两个 `unique_ptr`，并提供 `getDomTree` / `constructDomTree` / `getLoopInfo` / `calculateLoopInfo` 接口——这是访问支配/循环信息的入口。 |
| `lib/Core/BinaryFunction.cpp` | `constructDomTree()` / `calculateLoopInfo()` 的实现：前者调 `recalculate(*this)`，后者在支配树之上 `analyze` 出循环并填入 profile 计数。 |

## 4. 核心概念与源码讲解

### 4.1 BasicBlock 的指令与边：CFG 节点是怎么拼出来的

#### 4.1.1 概念说明

`BinaryBasicBlock` 是 BOLT 对「CFG 里一个节点」的抽象。一个块身上同时挂着三类东西：

1. **一段指令序列** `Instructions`——注意是「序列」而非「集合」，因为指令有先后顺序；它用 `InstructionListType`（也就是 `std::vector<MCInst>`，定义在 [include/bolt/Core/MCPlus.h:25](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/include/bolt/Core/MCPlus.h#L25)）存储。
2. **CFG 的边**——出边 `Successors` 和入边 `Predecessors`，各自一个块指针数组；外加一个与 `Successors` **等长、按下标一一对应**的 `BranchInfo` 数组，存每条出边上的 profile 计数。
3. **一堆定位/属性**——标签 `Label`、输入/输出地址范围、对齐、CFI 状态、执行计数、片段号（热/冷）等。

这里有一个和上一讲（[u2-l2](u2-l2-binary-function.md)）紧密呼应的设计：`BinaryBasicBlock` **自己并不独立存在**，它必须从属于某个 `BinaryFunction`，构造函数要求传入非空的 `Function` 指针，并且是 `friend class BinaryFunction`、由函数独占管理的。它甚至把 `hasCFG()` / `hasInstructions()` 直接委托给父函数去判断——也就是说，「这个块能不能被当成 CFG 节点用」取决于它所在的函数已经走到了哪个状态，而不是块自己说了算。

#### 4.1.2 核心流程

一个块的三类成员可以画成下面这张「侧视图」：

```
            BinaryBasicBlock (BB)
   ┌───────────────────────────────────────────────┐
   │ Instructions : [ MCInst, MCInst, ..., MCInst ] │  ← 有序指令序列
   │                                                   │
   │ Successors  : [ Succ0      , Succ1      ]        │  ← 出边（块指针）
   │ BranchInfo  : [ {Count,Mis}, {Count,Mis} ]        │  ← 与 Successors 等长、下标对齐
   │                                                   │
   │ Predecessors: [ PredA, PredB, ... ]               │  ← 入边（块指针）
   │                                                   │
   │ ExecutionCount, Label, CFIState, FragmentNum ...  │  ← 属性
   └───────────────────────────────────────────────┘
```

理解这个类，关键是抓住两条「不变量（invariant）」：

1. **边的双向一致性**：`Successors[BB]` 里若有 S，则 S 的 `Predecessors` 里必有 BB，反之亦然。下文 4.1.3 会看到，所有公开的边操作都成对地写这两个数组，**绝不留单边**。
2. **`Successors` 与 `BranchInfo` 等长对齐**：第 i 个后继的分支信息就是 `BranchInfo[i]`。下文 4.2 讲条件分支时会反复用到这个下标约定。

> 小贴士：BOLT 把 CFI（调用栈帧展开）指令也塞进 `Instructions` 里一起存，文件头注释就点明了这一点（[include/bolt/Core/BinaryBasicBlock.h:9-12](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/include/bolt/Core/BinaryBasicBlock.h#L9-L12)）。所以遍历指令时要区分「真指令」和「伪指令/CFI」，类里为此提供了 `getFirstNonPseudo()` / `getNumNonPseudos()` 等辅助接口。

#### 4.1.3 源码精读

先看成员声明，确认上面那张图：

[include/bolt/Core/BinaryBasicBlock.h:66-75](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/include/bolt/Core/BinaryBasicBlock.h#L66-L75) 定义了 `Instructions`、`Predecessors`、`Successors`、`BranchInfo` 四个核心容器；它们都是 `SmallVector<..., 0>`（0 表示不在对象内预留槽位，全部走堆分配，因为块数量大、要控对象体积——文件末尾有 `static_assert(sizeof(BinaryBasicBlock) <= 256)` 的体积约束）。

**双向一致性的核心：`addSuccessor`**。它的实现只有四行，但每一行都重要：

[lib/Core/BinaryBasicBlock.cpp:269-274](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/lib/Core/BinaryBasicBlock.cpp#L269-L274)

```cpp
void BinaryBasicBlock::addSuccessor(BinaryBasicBlock *Succ, uint64_t Count,
                                    uint64_t MispredictedCount) {
  Successors.push_back(Succ);
  BranchInfo.push_back({Count, MispredictedCount});
  Succ->Predecessors.push_back(this);
}
```

一次调用同时改三处：自己的 `Successors`、对应的 `BranchInfo`、以及**对方的** `Predecessors`。这就是「双向」的由来——加一条出边，自动在目标块登记一条入边，调用者无需手动维护对侧。

**对称的 `removeSuccessor`**：

[lib/Core/BinaryBasicBlock.cpp:304-318](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/lib/Core/BinaryBasicBlock.cpp#L304-L318)

```cpp
void BinaryBasicBlock::removeSuccessor(BinaryBasicBlock *Succ) {
  Succ->removePredecessor(this, /*Multiple=*/false);
  auto I = succ_begin();
  auto BI = BranchInfo.begin();
  for (; I != succ_end(); ++I) {
    assert(BI != BranchInfo.end() && "missing BranchInfo entry");
    if (*I == Succ) break;
    ++BI;
  }
  assert(I != succ_end() && "no such successor!");
  Successors.erase(I);
  BranchInfo.erase(BI);
}
```

注意两点：第一，它先调对方的 `removePredecessor(this)` 把入边摘掉，再在自己这边同步删掉 `Successors` 和 `BranchInfo` 中的对应项——**两个数组同删**，保住「等长对齐」不变量；第二，`I` 和 `BI` 两个迭代器是**同步前进**的，找到后继的同时也就定位到了它对应的分支信息。

**私有的 `removePredecessor` 有个 `Multiple` 开关**：

[lib/Core/BinaryBasicBlock.cpp:324-340](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/lib/Core/BinaryBasicBlock.cpp#L324-L340)

它默认 `Multiple=true`，会把所有等于 `Pred` 的前驱都删掉。这是为了应对「同一个前驱可能出现多次」的奇怪 CFG（比如条件/无条件后继指向同一块）。注释也提醒：不要直接调 `removePredecessor`，要用 `removeSuccessor()`，因为只有后者会同时处理 `Successors`/`BranchInfo`。

**批量清空要先用去重集合**：

[lib/Core/BinaryBasicBlock.cpp:296-302](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/lib/Core/BinaryBasicBlock.cpp#L296-L302)

```cpp
void BinaryBasicBlock::removeAllSuccessors() {
  SmallPtrSet<BinaryBasicBlock *, 2> UniqSuccessors(succ_begin(), succ_end());
  for (BinaryBasicBlock *SuccessorBB : UniqSuccessors)
    SuccessorBB->removePredecessor(this);
  Successors.clear();
  BranchInfo.clear();
}
```

为什么这里要先用 `SmallPtrSet` 去重？因为「条件后继 == 无条件后继」时，同一个后继块会在 `Successors` 里出现两次。若直接遍历 `Successors` 逐个 `removePredecessor`，第二次就会触发 `removePredecessor` 里那条 `assert(Erased && "Pred is not a predecessor of this block!")`。先去重，每个对侧块只摘一次入边，就安全了。这是双向一致性在「重复边」场景下的一个细节。

**切块时把后继整体搬走**：`splitAt` 是个很好的综合例子，它展示了「改边」的标准套路：

[lib/Core/BinaryBasicBlock.cpp:565-583](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/lib/Core/BinaryBasicBlock.cpp#L565-L583)

```cpp
BinaryBasicBlock *BinaryBasicBlock::splitAt(iterator II) {
  BinaryBasicBlock *NewBlock = getFunction()->addBasicBlock();
  moveAllSuccessorsTo(NewBlock);              // ① 老后继全部转给新块
  addSuccessor(NewBlock, getExecutionCount(), 0); // ② 自己改成无条件跳到新块
  NewBlock->setCFIState(getCFIStateAtInstr(&*II));
  ...
  NewBlock->addInstructions(II, end());
  Instructions.erase(II, end());
  return NewBlock;
}
```

`moveAllSuccessorsTo`（[include/bolt/Core/BinaryBasicBlock.h:603-610](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/include/bolt/Core/BinaryBasicBlock.h#L603-L610)）会把当前块的全部后继（连同 `BranchInfo`）转到新块，再 `removeAllSuccessors()` 清空自己——全程靠 `addSuccessor`/`removeAllSuccessors` 完成，因此双向边始终一致。

#### 4.1.4 代码实践（源码阅读型）

> **实践目标**：亲手验证「`addSuccessor` / `removeSuccessor` 如何保证 `Successors` 与对侧 `Predecessors` 双向一致」。

操作步骤：

1. 打开 [lib/Core/BinaryBasicBlock.cpp:269-340](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/lib/Core/BinaryBasicBlock.cpp#L269-L340)，逐行对照 `addSuccessor`、`removeSuccessor`、`addPredecessor`、`removePredecessor` 四个函数。
2. 思考一个反例：如果 `addSuccessor` 漏写 `Succ->Predecessors.push_back(this);` 这一行，下游哪个接口会最先暴露问题？提示：看 [include/bolt/Core/BinaryBasicBlock.h:629-637](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/include/bolt/Core/BinaryBasicBlock.h#L629-L637) 的 `isPredecessor` / `isSuccessor`——这两个谓词本应「互为镜像」。
3. 再读 `removeAllSuccessors`，解释为什么它必须先 `SmallPtrSet` 去重再 `removePredecessor`。

需要观察的现象 / 预期结果：

- 你会发现**没有一个公开接口只改单侧数组**；所有改边入口（`addSuccessor`、`removeSuccessor`、`replaceSuccessor`、`removeAllSuccessors`、`moveAllSuccessorsTo`）都在内部成对维护 `Successors` 和对侧 `Predecessors`。
- 结论（请自己用一段话写下）：BOLT 的 CFG 边一致性靠「唯一改边入口 + 入口内部双向写入」来保证，调用方只要不绕开这些接口去直接戳 `Successors`/`Predecessors`，双向性就不会被破坏。

> 说明：本实践是源码阅读型，不要求运行；如需运行时验证，可看 4.1.5 的练习 3 提到的 `validateSuccessorInvariants()`。

#### 4.1.5 小练习与答案

**练习 1**：`replaceSuccessor(Succ, NewSucc)`（[lib/Core/BinaryBasicBlock.cpp:276-294](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/lib/Core/BinaryBasicBlock.cpp#L276-L294)）为什么不像 `removeAllSuccessors` 那样需要去重？

**参考答案**：`replaceSuccessor` 用 `Multiple=false` 调 `Succ->removePredecessor(this, false)`，它只删对侧 `Predecessors` 中**第一个**等于 `this` 的项就返回（[lib/Core/BinaryBasicBlock.cpp:332-333](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/lib/Core/BinaryBasicBlock.cpp#L332-L333)），并且在自己这边也只替换 `Successors` 里**第一个**匹配项。它是在「精确替换一条边」而不是「清空所有边」，所以不会出现「同一个对侧块被删两次」的问题，也就不需要去重。

**练习 2**：`getBranchInfo(const BinaryBasicBlock &Succ)`（[lib/Core/BinaryBasicBlock.cpp:556-563](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/lib/Core/BinaryBasicBlock.cpp#L556-L563)）依赖了 4.1.2 里哪条不变量？

**参考答案**：依赖「`Successors` 与 `BranchInfo` 等长、按下标一一对应」。它用 `llvm::zip(successors(), branch_info())` 把两个数组拉链配对，找到后继的同时就拿到了同下标的 `BranchInfo`。如果两条不变量被破坏（例如只 push 了 `Successors` 没 push `BranchInfo`），这里就会越界或断言失败。

**练习 3**：`validateSuccessorInvariants()`（[lib/Core/BinaryBasicBlock.cpp:70-160](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/lib/Core/BinaryBasicBlock.cpp#L70-L160)）主要在校验什么？它为什么对「跳转表」和「普通分支」走两条不同分支？

**参考答案**：它校验「块末尾的实际分支指令」与「CFG 里登记的后继」是否一致：有几个后继、后继标签是否对得上条件/无条件分支的目标。对跳转表（`JT` 非空），它把后继标签和跳转表项逐一比对（[lib/Core/BinaryBasicBlock.cpp:76-117](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/lib/Core/BinaryBasicBlock.cpp#L76-L117)）；对普通分支，它调 `analyzeBranch` 还原出条件/无条件目标，再按后继个数（0/1/2）分别断言（[lib/Core/BinaryBasicBlock.cpp:128-147](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/lib/Core/BinaryBasicBlock.cpp#L128-L147)）。两条分支走不同逻辑，是因为跳转表的后继来自「数据里的地址」，而普通分支的后继来自「指令里的目标符号」，校验方式天然不同。

### 4.2 分支类型与执行计数：CFG 边上挂了什么

#### 4.2.1 概念说明

CFG 的边不只是「A 可能跳到 B」，还承载两类信息：**这条边是什么类型的分支**，以及**这条边被执行了多少次、预测错了多少次**（来自 profile）。BOLT 把它们分别编码成：

- **分支类型**：通过 `Successors` 的**个数与顺序**隐式表达。约定如下——
  - 0 个后继：块是函数出口（没有出边）。
  - 1 个后继：无条件跳转 / 顺序 fall-through。
  - 2 个后继：条件跳转。下标 `[0]` 是 **taken**（条件成立跳过去的目标），下标 `[1]` 是 **fall-through**（条件不成立顺次走的目标）。
  - 跳转表 / 间接跳转：可以有多个后继，每个表项目标都是一个后继（见 4.2.3）。
- **执行计数**：每个块有一个 `ExecutionCount`（这块被执行了多少次），每条出边有一个 `BinaryBranchInfo`（这条边走了多少次、其中多少次被分支预测器猜错）。这些计数是 BOLT 重排代码、热冷分裂的**主要依据**（呼应 [u1-l3](u1-l3-end-to-end-workflow.md) 讲过的「没有 profile 就无法优化」）。

#### 4.2.2 核心流程

条件分支的两个后继与两条 `BranchInfo` 的对应关系：

```
   BB 末尾:   jcc L_taken        （条件跳转）
              jmp L_next         （可选：无条件跳转，补 fall-through 之外的出口）

   Successors[0] = L_taken   ──┐  taken       （getConditionalSuccessor(true)）
   Successors[1] = L_next   ──┘  fall-through （getConditionalSuccessor(false)）
        ▲                       ▲
        │  下标对齐              │
   BranchInfo[0] = {takenCount,  takenMis}   ← getTakenBranchInfo()
   BranchInfo[1] = {fallCount,   fallMis }   ← getFallthroughBranchInfo()
```

要点：

1. `getConditionalSuccessor(true)` 取 `Successors[0]`，`getConditionalSuccessor(false)` 取 `Successors[1]`（[include/bolt/Core/BinaryBasicBlock.h:379-383](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/include/bolt/Core/BinaryBasicBlock.h#L379-L383)）。这个 0/1 约定贯穿整个 BOLT。
2. `getTakenBranchInfo()` 返回 `BranchInfo[0]`，`getFallthroughBranchInfo()` 返回 `BranchInfo[1]`，且都断言 `BranchInfo.size() == 2`（[include/bolt/Core/BinaryBasicBlock.h:404-415](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/include/bolt/Core/BinaryBasicBlock.h#L404-L415)）——它们只在「真正的条件分支块」上能用。
3. `swapConditionalSuccessors()`（[lib/Core/BinaryBasicBlock.cpp:449-456](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/lib/Core/BinaryBasicBlock.cpp#L449-L456)）同时交换 `Successors[0↔1]` 和 `BranchInfo[0↔1]`，用于「翻转分支条件」的优化（比如 SCTC、把热的 taken 改成 fall-through），交换时两个数组必须一起动。
4. **profile 的两种来源**：`BinaryBranchInfo::MispredictedCount` 有个特殊值 `COUNT_INFERRED`，表示这条 fall-through 边的计数是 BOLT **内部推算**出来的，而非 profile 直接给的（[include/bolt/Core/BinaryBasicBlock.h:42-49](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/include/bolt/Core/BinaryBasicBlock.h#L42-L49)）；块计数 `ExecutionCount` 也有个 `COUNT_NO_PROFILE` 表示「没有 profile」。

#### 4.2.3 源码精读

**条件后继的取法**，注意 0/1 与 true/false 的映射：

[include/bolt/Core/BinaryBasicBlock.h:375-388](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/include/bolt/Core/BinaryBasicBlock.h#L375-L388)

```cpp
/// ... has 2 successors, return a successor corresponding to a jump
/// condition which could be true or false.
BinaryBasicBlock *getConditionalSuccessor(bool Condition) {
  if (succ_size() != 2)
    return nullptr;
  return Successors[Condition == true ? 0 : 1];
}
```

`true` → `[0]`（taken），`false` → `[1]`（fall-through）。`getFallthrough()`（[include/bolt/Core/BinaryBasicBlock.h:392-401](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/include/bolt/Core/BinaryBasicBlock.h#L392-L401)）正是据此定义：两后继时返回 `getConditionalSuccessor(false)`，单后继时返回那唯一的后继。

**分析末尾分支**：`analyzeBranch` 把「解读末尾是条件/无条件跳转」这件事委托给目标后端 `MIB`（`MCPlusBuilder`）：

[lib/Core/BinaryBasicBlock.cpp:409-415](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/lib/Core/BinaryBasicBlock.cpp#L409-L415)

```cpp
bool BinaryBasicBlock::analyzeBranch(const MCSymbol *&TBB, const MCSymbol *&FBB,
                                     MCInst *&CondBranch,
                                     MCInst *&UncondBranch) {
  auto &MIB = Function->getBinaryContext().MIB;
  return MIB->analyzeBranch(Instructions.begin(), Instructions.end(), TBB, FBB,
                            CondBranch, UncondBranch);
}
```

它返回四个出口：taken 目标 `TBB`、fall-through 目标 `FBB`、条件分支指令 `CondBranch`、无条件分支指令 `UncondBranch`。注意「分支语义」是**目标相关**的（x86 和 AArch64 解码方式不同），所以 `BinaryBasicBlock` 只做转发，真正干活的是后端——这为 [u7-l1](u7-l1-target-backends.md) 的 `MCPlusBuilder` 埋下伏笔。

**跳转表如何变成多个后继**：跳转表是一种「数据里存了一组目标地址」的间接跳转，BOLT 把每个目标登记成一个后继块。`updateJumpTableSuccessors` 展示了这套登记逻辑：

[lib/Core/BinaryBasicBlock.cpp:363-390](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/lib/Core/BinaryBasicBlock.cpp#L363-L390)

```cpp
void BinaryBasicBlock::updateJumpTableSuccessors() {
  const JumpTable *JT = getJumpTable();
  removeAllSuccessors();                              // 先清空旧后继
  SmallVector<BinaryBasicBlock *, 16> SuccessorBBs;
  for (const MCSymbol *Label : JT->Entries) {         // 遍历跳转表的每个表项
    BinaryBasicBlock *BB = getFunction()->getBasicBlockForLabel(Label);
    if (!BB) { ... continue; }                        // __builtin_unreachable 等
    SuccessorBBs.emplace_back(BB);
  }
  llvm::sort(SuccessorBBs, ...);                      // 按输入 offset 排序
  SuccessorBBs.erase(llvm::unique(SuccessorBBs), SuccessorBBs.end()); // 去重
  for (BinaryBasicBlock *BB : SuccessorBBs)
    addSuccessor(BB);                                 // 逐个加后继（自动维护双向边）
}
```

要点：跳转表项可能重复指向同一块，所以要先排序再 `unique` 去重；最后逐个 `addSuccessor`，复用 4.1 里那套双向维护机制。块是否含跳转表用 `hasJumpTable()`（[include/bolt/Core/BinaryBasicBlock.h:910-915](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/include/bolt/Core/BinaryBasicBlock.h#L910-L915)）判断，它本质是看末尾非伪指令是否关联了一个 `JumpTable`。

**执行计数与按比例缩放**：块的 `ExecutionCount` 初值是 `COUNT_NO_PROFILE`（无 profile 标记，[include/bolt/Core/BinaryBasicBlock.h:109-110](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/include/bolt/Core/BinaryBasicBlock.h#L109-L110) 与 [163-164](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/include/bolt/Core/BinaryBasicBlock.h#L163-L164)）；`hasProfile()` 据此判断块是否有计数（[include/bolt/Core/BinaryBasicBlock.h:640](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/include/bolt/Core/BinaryBasicBlock.h#L640)）。`adjustExecutionCount(Ratio)`（[lib/Core/BinaryBasicBlock.cpp:392-407](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/lib/Core/BinaryBasicBlock.cpp#L392-L407)）会**同时**按比例缩放块计数和每条出边的 `BranchInfo`，常用于「分裂后把计数分摊到新块」。

#### 4.2.4 代码实践（源码阅读型）

> **实践目标**：搞清条件分支块的两条边在 `Successors`/`BranchInfo` 里的固定位置，以及跳转表块的后继个数。

操作步骤：

1. 在头文件里找到 `getConditionalSuccessor`、`getTakenBranchInfo`、`getFallthroughBranchInfo`、`getFallthrough` 四个接口（[include/bolt/Core/BinaryBasicBlock.h:375-415](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/include/bolt/Core/BinaryBasicBlock.h#L375-L415)），确认它们都依赖「2 个后继」和「0=taken, 1=fall-through」约定。
2. 读 `swapConditionalSuccessors`（[lib/Core/BinaryBasicBlock.cpp:449-456](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/lib/Core/BinaryBasicBlock.cpp#L449-L456)），回答：为什么交换后继必须连 `BranchInfo` 一起交换？如果只交换 `Successors`，`getTakenBranchInfo()` 会返回什么？
3. 读 `updateJumpTableSuccessors`（[lib/Core/BinaryBasicBlock.cpp:363-390](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/lib/Core/BinaryBasicBlock.cpp#L363-L390)），数一下一个有 N 个表项（去重前）的跳转表最多会产生几个后继。

需要观察的现象 / 预期结果：

- 步骤 2 预期：若只交换 `Successors` 不交换 `BranchInfo`，`getTakenBranchInfo()` 返回的就不再是「真正的 taken 边」的计数，而是 fall-through 边的计数——分支预测率、热度判断都会错。这正是「两个数组必须同步」的实际后果。
- 步骤 3 预期：去重后，后继个数 = 跳转表中**不同的**目标块个数（最少 1 个，最多等于表项数）。

> 说明：本实践为源码阅读型；分支语义的运行时行为依赖具体目标后端（见 [u7-l1](u7-l1-target-backends.md)），此处不要求运行。

#### 4.2.5 小练习与答案

**练习 1**：一个块的 `succ_size() == 1`，它能是条件跳转吗？

**参考答案**：按 BOLT 的约定，条件跳转块必须有 2 个后继（taken + fall-through）。`succ_size() == 1` 表示无条件跳转或顺序 fall-through。`getConditionalSuccessor` 在 `succ_size() != 2` 时直接返回 `nullptr`（[include/bolt/Core/BinaryBasicBlock.h:380-381](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/include/bolt/Core/BinaryBasicBlock.h#L380-L381)），`getTakenBranchInfo` 则断言 `BranchInfo.size() == 2`。注意有一种退化情形：`removeDuplicateConditionalSuccessor` 会把「条件和无条件后继指向同一块」合并成单后继（[lib/Core/BinaryBasicBlock.cpp:342-361](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/lib/Core/BinaryBasicBlock.cpp#L342-L361)），此时块只剩 1 个后继、也不再是「条件分支块」。

**练习 2**：`BinaryBranchInfo::MispredictedCount == COUNT_INFERRED` 表示什么？为什么 BOLT 要专门区分它？

**参考答案**：它表示这条 fall-through 边的 `Count` 是 BOLT 根据块计数和其它边**推算**出来的，而不是 profile 直接采样到的（[include/bolt/Core/BinaryBasicBlock.h:42-49](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/include/bolt/Core/BinaryBasicBlock.h#L42-L49)）。区分它的意义在于：推算值没有「预测错误次数」这个语义（`MispredictedCount` 此时只是个标记），下游在统计分支预测率时要排除这类边，避免把推算值当成真实采样。在 `adjustExecutionCount` 里也能看到它对 `MispredictedCount == COUNT_INFERRED` 的特判（[lib/Core/BinaryBasicBlock.cpp:404-405](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/lib/Core/BinaryBasicBlock.cpp#L404-L405)）。

### 4.3 支配树与循环信息：在 CFG 之上算出来的分析结构

#### 4.3.1 概念说明

CFG 本身只是「节点 + 边」。很多优化还需要在 CFG 之上**进一步算出来的结构**，BOLT 用了两个：

- **支配树（Dominator Tree）**：回答「块 X 的直接支配者是谁」「块 X 是否支配块 Y」。它是栈帧优化、活性分析（[u6-l5](u6-l5-frame-and-reg-optimizations.md)）等数据流分析的基础。
- **循环信息（Loop Info）**：回答「块 X 在哪个循环里」「这个循环的入口/出口/回边计数是多少」。它是热冷分裂、循环相关优化的依据。

BOLT 在这里做了一个非常省事的决定：**它没有自己实现支配树和循环算法，而是直接复用 LLVM 的通用模板**。这两个结构是「目标无关」的，所以本讲要讲的不是算法本身，而是「BOLT 如何把 `BinaryBasicBlock` 喂给通用模板、再怎么取结果」。

#### 4.3.2 核心流程

整体关系：

```
   BinaryFunction
        │ 持有（unique_ptr）
        ├─ BDT : BinaryDominatorTree   =  DomTreeBase<BinaryBasicBlock>
        └─ BLI : BinaryLoopInfo        =  LoopInfoBase<BinaryBasicBlock, BinaryLoop>

   构造流程：
        constructDomTree()  →  BDT->recalculate(*this)        // 把整个函数当图喂进去
        calculateLoopInfo() →  BLI->analyze(getDomTree())     // 在支配树之上识别循环
                              + 用 profile 填 TotalBackEdgeCount/EntryCount/ExitCount
```

取结果的典型用法：

- 取块 `BB` 的**直接支配者**：`BF.getDomTree().getNode(BB)->getIDom()->getBlock()`。
- 取块 `BB` 所属的**循环**：`BF.getLoopInfo().getLoopFor(BB)`，返回 `BinaryLoop *`（不在任何循环里则返回 `nullptr`）；再可取 `getLoopDepth()`（嵌套深度）、`getHeader()`（循环头）。

`BinaryLoop` 还自带三个 profile 字段：`TotalBackEdgeCount`（所有回边计数之和）、`EntryCount`（从循环外进入的次数）、`ExitCount`（从循环退出次数）。它们在 `calculateLoopInfo()` 里被填上（下文 4.3.3），是评估「这个循环有多热」的直接数据。

#### 4.3.3 源码精读

**两个几乎「零代码」的类型别名**——这正是 BOLT 复用通用模板的关键证据：

[include/bolt/Core/BinaryDomTree.h:23-24](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/include/bolt/Core/BinaryDomTree.h#L23-L24)

```cpp
using BinaryDomTreeNode = DomTreeNodeBase<BinaryBasicBlock>;
using BinaryDominatorTree = DomTreeBase<BinaryBasicBlock>;
```

`DomTreeBase<BinaryBasicBlock>` 是 LLVM 的通用支配树模板，模板参数是「图的节点类型」。它之所以能直接接受 `BinaryBasicBlock`，是因为头文件末尾为 `BinaryBasicBlock *` 特化了 `GraphTraits`，告诉通用算法「后继用 `succ_begin/succ_end` 取、节点编号用 `getIndex()` 取」（[include/bolt/Core/BinaryBasicBlock.h:974-986](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/include/bolt/Core/BinaryBasicBlock.h#L974-L986)）。换句话说，4.1 里那套 `Successors`/`Predecessors` 接口，正是通用图算法眼里的「边」。

[include/bolt/Core/BinaryLoop.h:25-50](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/include/bolt/Core/BinaryLoop.h#L25-L50) 同理：

```cpp
class BinaryLoop : public LoopBase<BinaryBasicBlock, BinaryLoop> {
public:
  BinaryLoop() : LoopBase<BinaryBasicBlock, BinaryLoop>() {}
  uint64_t TotalBackEdgeCount{0};   // 所有回边计数之和
  uint64_t EntryCount{0};           // 从外部进入循环的次数
  uint64_t ExitCount{0};            // 退出循环的次数
  // 大部分公共接口由 LoopBase 提供
};

class BinaryLoopInfo : public LoopInfoBase<BinaryBasicBlock, BinaryLoop> {
public:
  unsigned OuterLoops{0};    // 顶层循环个数
  unsigned TotalLoops{0};    // 全部循环个数（含嵌套）
  unsigned MaximumDepth{0};  // 最大嵌套深度
};
```

注释明确写了「Most of the public interface is provided by LoopBase / LoopInfoBase」——BOLT 只是在通用模板上**加了三个 profile 计数字段和三个统计量**，循环识别算法本身完全复用 LLVM。

**构造入口在 `BinaryFunction`**：

[include/bolt/Core/BinaryFunction.h:949-966](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/include/bolt/Core/BinaryFunction.h#L949-L966)

```cpp
bool hasDomTree() const { return BDT != nullptr; }
BinaryDominatorTree &getDomTree() { return *BDT; }
void constructDomTree();
bool hasLoopInfo() const { return BLI != nullptr; }
const BinaryLoopInfo &getLoopInfo() { return *BLI; }
bool isLoopFree() { if (!hasLoopInfo()) calculateLoopInfo(); return BLI->empty(); }
```

两个 `unique_ptr` 成员见 [include/bolt/Core/BinaryFunction.h:280-281](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/include/bolt/Core/BinaryFunction.h#L280-L281)。注意它们是**惰性构造**的：不调 `constructDomTree` / `calculateLoopInfo`，`BDT`/`BLI` 就是空指针。

**实现：支配树一行，循环识别两行**：

[lib/Core/BinaryFunction.cpp:4449-4459](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/lib/Core/BinaryFunction.cpp#L4449-L4459)

```cpp
void BinaryFunction::constructDomTree() {
  BDT.reset(new BinaryDominatorTree);
  BDT->recalculate(*this);          // 通用支配树算法，把整个函数当图
}

void BinaryFunction::calculateLoopInfo() {
  if (!hasDomTree())
    constructDomTree();             // 循环识别依赖支配树
  BLI.reset(new BinaryLoopInfo());
  BLI->analyze(getDomTree());       // 通用循环识别，输入是支配树
  ...
}
```

`recalculate(*this)` 之所以能接受一个 `BinaryFunction`，是因为 `BinaryFunction` 也提供了 `GraphTraits`（入口块用 `front()`），通用算法据此遍历整个 CFG。`BLI->analyze(getDomTree())` 是 LLVM 循环识别的标准入口——循环识别**必须**先有支配树（回边的定义依赖支配关系），所以这里先确保 `BDT` 存在。

**用 profile 填循环计数**（`calculateLoopInfo` 后半段）：

[lib/Core/BinaryFunction.cpp:4486-4503](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/lib/Core/BinaryFunction.cpp#L4486-L4503)

```cpp
// 回边计数 = 所有 latch（回边源块）跳回 header 的边计数之和
SmallVector<BinaryBasicBlock *, 1> Latches;
L->getLoopLatches(Latches);
for (BinaryBasicBlock *Latch : Latches) {
  auto BI = Latch->branch_info_begin();
  for (BinaryBasicBlock *Succ : Latch->successors())
    if (Succ == L->getHeader()) { L->TotalBackEdgeCount += BI->Count; }
    ++BI;
}
// 入口计数 = header 的块计数 − 回边计数（从外部进来的那部分）
L->EntryCount = L->getHeader()->getExecutionCount() - L->TotalBackEdgeCount;
```

这段把 4.1/4.2 学的「后继 + `BranchInfo`」用到了循环分析上：遍历每个 latch 块的出边，凡是跳回 `header` 的就是回边，把它的 `BranchInfo.Count` 累加成 `TotalBackEdgeCount`。`EntryCount` 用一个简洁的守恒式算出：header 的总执行次数 = 从循环外进入的次数 + 从回边回来的次数，所以「外入次数 = header 计数 − 回边计数」。退出计数 `ExitCount` 在紧随其后的循环里用 `getExitEdges` 累加（[lib/Core/BinaryFunction.cpp:4506-4520](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/lib/Core/BinaryFunction.cpp#L4506-L4520)）。

> 关键结论：支配树和循环信息在 BOLT 里**不是自研算法**，而是「`BinaryBasicBlock` 通过 `GraphTraits` 接入 LLVM 通用模板 + BOLT 在结果上贴 profile 计数」。理解了这一点，就不必去读支配树算法的源码细节，只需会用 `getDomTree().getNode(BB)->getIDom()` 和 `getLoopInfo().getLoopFor(BB)`。

#### 4.3.4 代码实践（源码阅读型 + 伪代码）

> **实践目标**：写出在 BOLT 里「取一个块的支配者」和「取一个块所属循环」的调用方式，并理解其背后链路。

操作步骤：

1. 在 [include/bolt/Core/BinaryFunction.h:949-966](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/include/bolt/Core/BinaryFunction.h#L949-L966) 确认 `getDomTree()` / `getLoopInfo()` 接口；在 [include/bolt/Core/BinaryDomTree.h:23-24](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/include/bolt/Core/BinaryDomTree.h#L23-L24) 确认 `BinaryDomTreeNode = DomTreeNodeBase<BinaryBasicBlock>`（`getIDom()` / `getBlock()` 都来自该基类）。
2. 读 [include/bolt/Core/BinaryBasicBlock.h:974-986](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/include/bolt/Core/BinaryBasicBlock.h#L974-L986) 的 `GraphTraits`，看清 `child_begin/child_end` 取的是 `succ_begin/succ_end`——这是通用算法「看得见边」的桥梁。
3. 写出下面这段**示例代码**（仅为说明调用方式，非项目原有代码）：

```cpp
// 示例代码：在某个 pass 里取块 BB 的支配者与所属循环
BinaryFunction &BF = *BB->getFunction();

// 1) 确保 dom tree 已构造（calculateLoopInfo 会顺带构造，单独用则调 constructDomTree）
if (!BF.hasDomTree())
  BF.constructDomTree();

// 取 BB 的直接支配者（immediate dominator）；入口块的 IDom 是自己
BinaryDomTreeNode *Node = BF.getDomTree().getNode(BB);
BinaryBasicBlock *IDom = Node->getIDom() ? Node->getIDom()->getBlock() : nullptr;

// 2) 取 BB 所属循环；不在任何循环里则返回 nullptr
const BinaryLoopInfo &LI = BF.getLoopInfo();   // 注意：需先 calculateLoopInfo()
const BinaryLoop *L = LI.getLoopFor(BB);
if (L) {
  unsigned Depth = L->getLoopDepth();          // 嵌套深度，1 表示最外层
  BinaryBasicBlock *Header = L->getHeader();   // 循环头
  uint64_t Back = L->TotalBackEdgeCount;       // 回边总热度
}
```

需要观察的现象 / 预期结果：

- `getLoopInfo()` 返回 `const BinaryLoopInfo &`，而它内部依赖 `BDT` 已构造——`calculateLoopInfo()` 会自己保证这一点（[lib/Core/BinaryFunction.cpp:4455-4456](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/lib/Core/BinaryFunction.cpp#L4455-L4456)）。所以实践中通常先 `calculateLoopInfo()` 再 `getLoopInfo()`。
- 你会注意到：取支配者/循环都没有写任何算法，全是模板提供的接口——印证了「复用通用模板」这一设计。

> 说明：示例代码只为演示 API 形态，不保证在仓库里能直接编译运行；真实调用点散布在各优化 pass 里（如 `FrameAnalysis`、`RegReAssign`）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `calculateLoopInfo()` 必须先确保 `hasDomTree()`？

**参考答案**：因为「回边」的定义是「目标支配源」的边，循环识别（`LoopInfoBase::analyze`）依赖支配关系来判定哪些边是回边、进而圈出循环体。没有支配树就无法识别循环。所以 [lib/Core/BinaryFunction.cpp:4454-4459](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/lib/Core/BinaryFunction.cpp#L4454-L4459) 里 `calculateLoopInfo` 先 `if (!hasDomTree()) constructDomTree();` 再 `BLI->analyze(getDomTree());`。

**练习 2**：`BinaryLoopInfo` 里的 `OuterLoops`、`TotalLoops`、`MaximumDepth` 三个统计量分别怎么算出来的？

**参考答案**：在 `calculateLoopInfo()` 里用一个栈遍历所有循环（[lib/Core/BinaryFunction.cpp:4462-4476](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/lib/Core/BinaryFunction.cpp#L4462-L4476)）：顶层循环（`BLI` 直接迭代到的）计入 `OuterLoops` 并压栈；每弹出一个循环就 `++TotalLoops`，并用 `getLoopDepth()` 更新 `MaximumDepth`，再把它的子循环压栈。所以 `OuterLoops` 是顶层循环数，`TotalLoops` 是含嵌套的全部循环数，`MaximumDepth` 是最深嵌套层数。

**练习 3**：`BinaryBasicBlock` 没有 `getIDom()` 这样的方法，却能让通用支配树算出自己的支配者，靠的是什么机制？

**参考答案**：靠 `GraphTraits` 特化。BOLT 在 [include/bolt/Core/BinaryBasicBlock.h:974-986](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/include/bolt/Core/BinaryBasicBlock.h#L974-L986) 特化了 `GraphTraits<BinaryBasicBlock *>`，告诉通用算法「后继迭代器 = `succ_begin/succ_end`、节点编号 = `getIndex()`」。`DomTreeBase<BinaryBasicBlock>::recalculate` 据此把整个 CFG 当一张可遍历的图，无需 `BinaryBasicBlock` 自己提供支配相关接口。

## 5. 综合实践

把本讲三个模块串起来，做一个**「读一个真实块的完整画像」**的源码阅读任务。

设定：随便在 [lib/Passes/](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/lib/Passes/) 下选一个优化 pass（例如 `ReorderBasicBlocks.cpp` 或 `SplitFunctions.cpp`），找到它遍历函数基本块的循环。

任务：

1. **指令与边**：在该 pass 里找到「遍历某块后继」的代码，确认它用的是 `successors()` / `succ_begin()` 而不是直接戳私有数组；再找到「读取后继热度」的地方，确认它通过 `getBranchInfo(Succ)` 或 `getTakenBranchInfo()` 取计数——把 4.1 的「双向一致 + 等长对齐」与 4.2 的「分支类型」对应上。
2. **分支类型**：找到该 pass 里判断「这个块是不是条件分支」的代码（通常看 `succ_size() == 2` 或调 `getConditionalSuccessor`），解释它依赖 4.2 的 0/1 约定。
3. **支配/循环**：如果该 pass 用到了支配树或循环信息（很多分裂/帧优化 pass 会用），找到 `getDomTree()` / `getLoopInfo()` 的调用点，对照 4.3 确认它在取「支配者」或「所属循环」时调的是 `getNode(BB)->getIDom()` 还是 `getLoopFor(BB)`。
4. 最后用一段话总结：**这个 pass 之所以能工作，依赖了 `BinaryBasicBlock` 的哪几条不变量？**（至少应提到「边双向一致」「`Successors` 与 `BranchInfo` 等长对齐」「2 后继 = 条件分支且 0=taken/1=fall-through」中的两条。）

预期结果：你会清楚地看到，`BinaryBasicBlock` 提供的「指令序列 + 双向边 + 并列 BranchInfo + GraphTraits」这一整套约定，是如何被上层优化 pass 当作「地基」直接使用的——这正是它作为 CFG 节点的价值所在。

## 6. 本讲小结

- `BinaryBasicBlock` 是 CFG 的节点，身上同时挂着：有序指令序列 `Instructions`、出边 `Successors`、入边 `Predecessors`、与 `Successors` 等长对齐的分支计数 `BranchInfo`，以及标签/地址/对齐/CFI/计数等属性。
- CFG 边的**双向一致性**靠唯一改边入口保证：`addSuccessor` 同时写 `Successors`、`BranchInfo` 和对侧 `Predecessors`；`removeSuccessor` / `removeAllSuccessors` / `replaceSuccessor` / `splitAt` 都在内部成对维护，调用方绝不直接戳单侧数组。
- 分支类型用「后继个数 + 下标约定」表达：0 个=出口，1 个=无条件/顺序，2 个=条件分支（`[0]`=taken、`[1]`=fall-through）；跳转表块的后继是去重后的全部表项目标；`analyzeBranch` 把真正的分支语义委托给目标后端 `MIB`。
- 执行计数有两套：块级 `ExecutionCount`、边级 `BinaryBranchInfo`（含特殊值 `COUNT_NO_PROFILE` / `COUNT_INFERRED`），是重排与热冷分裂的主要依据。
- 支配树 `BinaryDominatorTree` 和循环信息 `BinaryLoopInfo` **复用 LLVM 通用模板**：靠 `GraphTraits` 把 `BinaryBasicBlock` 接入，`constructDomTree`/`calculateLoopInfo` 负责构造并贴上 profile 计数；取支配者用 `getDomTree().getNode(BB)->getIDom()`，取所属循环用 `getLoopInfo().getLoopFor(BB)`。
- 整个类的体积被 `static_assert(sizeof(BinaryBasicBlock) <= 256)` 约束在 256 字节以内（Linux），因为块数量极大，控体积是为了落在 jemalloc 的高效 size class 上（呼应 [u9-l2](u9-l2-tools-and-performance.md) 的内存分配器优化）。

## 7. 下一步学习建议

- 本讲只讲了「CFG 节点是什么」，但没讲「字节流是怎么被切成基本块、CFG 是怎么连起来的」——那是 [u3-l3（反汇编与 CFG 重建）](u3-l3-disassemble-and-cfg.md) 的主题，它会讲 `BinaryFunction::disassemble()` / `buildCFG()` 和 `processIndirectBranch` 启发式，正好补上「这些 `Successors`/`Predecessors` 最初是谁建立的」。
- 边上的 `BranchInfo` 计数来自 profile，而 profile 是怎么从 `perf.data` 聚合并落到每条边上的，见 [u4-l1（Profile 格式总览）](u4-l1-profile-formats.md) 与 [u4-l2（perf2bolt 与 DataAggregator）](u4-l2-perf2bolt-aggregator.md)。
- `BinaryBasicBlock` 的指令是 `MCInst`，BOLT 如何在 `MCInst` 上挂元数据（注释/CFI 标记/profile 标记），见下一讲 [u2-l4（MCPlus 元数据）](u2-l4-mcplus-metadata.md)。
- 想看支配/循环信息的真实使用方，可以直接读 [lib/Passes/FrameAnalysis.cpp](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/lib/Passes/FrameAnalysis.cpp) 与 [lib/Passes/ShrinkWrapping.cpp](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/lib/Passes/ShrinkWrapping.cpp)，它们是 4.3 内容在工程里的落点。

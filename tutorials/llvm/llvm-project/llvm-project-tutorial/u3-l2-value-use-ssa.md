# Value / User / Use：SSA 与 def-use 链

## 1. 本讲目标

本讲是「LLVM IR 核心数据结构」单元的第二讲，承接 [u3-l1 Module / Function / BasicBlock：IR 的层次结构](u3-l1-ir-hierarchy.md) 刻意留下的那条线索。

上一讲我们看清了 IR 的**包含树**（`Module ⊃ Function ⊃ BasicBlock ⊃ Instruction`），并顺带提到一个现象：`Function`、`BasicBlock`、`Instruction` 最终都派生自同一个根基类 `Value`。本讲就要回答两个问题——

- 为什么「几乎所有 IR 对象都是一个 `Value`」？这个根基类到底提供了什么？
- 这些对象之间「谁用了谁」的关系，在内存里是怎么表达的？

学完本讲，你应当能够：

- 理解 `Value` 作为几乎所有 IR 对象公共根基类的设计动机，说出它自带的四个核心属性（类型、名字、use-list、子类 ID）。
- 掌握 `Use` / `User` 构成的 **def-use 双向链**：从一条指令出发，既能找到「它用了哪些值」（use-def），也能找到「它被哪些指令用到」（def-use）。
- 能解释 **SSA（单静态赋值）** 在 LLVM IR 中是如何靠这条双向链体现的，以及为什么它是所有优化 Pass 改写 IR 的基础。
- 写出遍历操作数与使用者的 C++ API 调用（或至少能准确描述思路）。

## 2. 前置知识

本讲承接 [u3-l1](u3-l1-ir-hierarchy.md)，请先确认你理解下面这些概念：

- **包含层次 vs 继承层次**：上一讲指出 IR 对象同时处在两套层次里——`Module` 拥有 `Function` 列表是「包含」；而 `Function`（经 `GlobalObject → GlobalValue → Constant → User`）和 `BasicBlock` 都最终是 `Value`，这是「继承」。本讲聚焦继承层次的根基 `Value`。
- **SSA 与 `%` 值**：来自 [u2-l2 阅读与编写 LLVM IR](u2-l2-read-write-ir.md)——每个 `%name` 只被定义一次。

一个贯穿全讲的核心直觉：**在 `.ll` 文本里，一条指令写成 `%sum = add i32 %a, %b`，这一行其实同时刻画了「一个定义（def `%sum`）」和「两个使用（use `%a`、`%b`）」。** 本讲就是把「def」和「use」这两种关系对象化，并揭示它们在内存里是同一条双向链的两个方向。

```llvm
define i32 @f(i32 %a, i32 %b) {
entry:
  %sum = add i32 %a, %b      ; def %sum；use %a、use %b
  %sum2 = add i32 %sum, 1    ; def %sum2；use %sum、use 常量 1
  ret i32 %sum2              ; use %sum2
}
```

站在 `%sum` 的视角看：它被 `%a`、`%b`「定义出来」（不，准确说是它**使用了** `%a`、`%b`），又被 `%sum2`「使用」。这条「定义—使用」的关系网，就是本讲的主角。

> 术语澄清：本文严格区分 **use**（一次使用，一条边）和 **user**（使用方，即拥有这次使用的那个 `User` 对象）。后面会看到 `%x = mul %a, %a` 对 `%a` 有**两次 use**、但只有**一个 user**（那条 `mul`）。这是本讲一个容易混淆、却很关键的细节。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [llvm/include/llvm/IR/Value.h](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/IR/Value.h) | `Value` 类声明：根基类，定义类型、名字、use-list 与子类 ID；并在文件末尾内联了 `Use::set`。 |
| [llvm/lib/IR/Value.cpp](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/lib/IR/Value.cpp) | `Value` 的实现：构造/析构、`deleteValue` 派发、`replaceAllUsesWith`（RAUW）等改写逻辑。 |
| [llvm/include/llvm/IR/Use.h](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/IR/Use.h) | `Use` 类声明：表示「一条 def→user 的边」，是双向链的节点。 |
| [llvm/lib/IR/Use.cpp](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/lib/IR/Use.cpp) | `Use` 的实现：`getOperandNo`（用指针算术求操作数下标）等。 |
| [llvm/include/llvm/IR/User.h](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/IR/User.h) | `User` 类声明：`Value` 的子类，代表「有操作数」的值（指令、常量）；定义操作数数组与 `getOperand`。 |
| [llvm/include/llvm/IR/Instruction.h](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/IR/Instruction.h) | `Instruction` 类声明：继承自 `User`，是「最大的 User 家族」。 |

> 说明：`Type` 系统（每个 `Value` 都有的 `getType()`）是下一讲 [u3-l3 类型系统与常量](u3-l3-type-system-constants.md) 的主题，本讲只把 `Type *` 当作「每个值自带的类型标签」来用。

---

## 4. 核心概念与源码讲解

### 4.1 Value：几乎所有 IR 对象的根基类

#### 4.1.1 概念说明

`Value` 是 LLVM IR 对象模型里**最重要的基类**。官方注释开门见山：

> This is a very important LLVM class. It is the base class of all values computed by a program that may be used as operands to other values.（[Value.h:L62-L69](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/IR/Value.h#L62-L69)）

这句话有三个关键词，拆开理解：

1. **「base class of all values」**：几乎你在 IR 里能点出名的东西都是 `Value`。结合上一讲的继承层次与源码可验证：
   - `Instruction` 继承自 `User`（[Instruction.h:L64-L65](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/IR/Instruction.h#L64-L65)）；
   - `Constant` 继承自 `User`（[Constant.h:L43](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/IR/Constants.h#L43)）；
   - `GlobalValue` 继承自 `Constant`（[GlobalValue.h:L49](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/IR/GlobalValue.h#L49)），`GlobalObject` 继承自 `GlobalValue`（[GlobalObject.h:L28](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/IR/GlobalObject.h#L28)），`Function` 又继承自 `GlobalObject`；
   - 而 `User` 继承自 `Value`（[User.h:L44](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/IR/User.h#L44)）；
   - `BasicBlock`、`Argument` 也都直接或间接派生自 `Value`。

   于是有完整的继承链：`Function → GlobalObject → GlobalValue → Constant → User → Value`，`Instruction → User → Value`。**`Value` 是这棵继承树的根。**

2. **「may be used as operands to other values」**：一个 `Value` 可以被别的 `User` 当作操作数引用。这是「能被使用」的资格——而 `Value` 自带的 use-list 正是用来记录「谁引用了我」。

3. **「All Values have a Type」**：每个 `Value` 都带一个类型。注意原文紧接着强调：**「Type is not a subclass of Value」**——类型不是值，类型是值的「标签」。

#### 4.1.2 核心流程

「是一个 `Value`」意味着什么？一个 `Value` 对象自带四样东西：

1. **一个类型 `VTy`**：通过 `getType()` 取（[Value.h:L254-L255](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/IR/Value.h#L254-L255)）。上下文也通过类型反查：`getContext()` 返回 `VTy->getContext()`（[Value.h:L257](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/IR/Value.h#L257)）。
2. **一个可选的名字**：`hasName()` / `getName()` / `setName()`，对应 `.ll` 里的 `%name` / `@name`。命名会自动同步到所属的符号表（详见上一讲 `SymbolTableList`）。
3. **一个 use-list**：`UseList` 指针，指向所有引用本 `Value` 的 `Use` 构成的链表头。这是本讲 4.2 的主角。
4. **一个子类 ID `SubclassID`**：一个 `unsigned char`，用来在**没有虚函数表（vtable）**的前提下做运行时类型识别（RTTI）。

这里有一个体现 LLVM「为内存而战」的工程取舍。`Value` 对象在编译期会产生**数以亿计**的实例（每条指令、每个常量都是一个 `Value`），所以它的体积被压榨到极致。构造函数末尾有一条静态断言锁死大小（[Value.cpp:L73-L74](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/lib/IR/Value.cpp#L73-L74)）：

\[ \text{sizeof(Value)} = 2 \times \text{sizeof(void*)} + 2 \times \text{sizeof(unsigned)} \]

在 64 位平台上仅 24 字节。为了做到这点，`Value` 甚至**故意不给析构函数加 `virtual`**——因为一旦有虚函数，每个对象就要多背一个 vtable 指针（8 字节）。代价是：不能直接 `delete` 一个基类 `Value*`，而必须调用专门的 `deleteValue()`，它根据 `SubclassID` 用 `switch` 派发到正确子类的 `delete`（[Value.cpp:L108-L136](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/lib/IR/Value.cpp#L108-L136)）。

`SubclassID` 同时也是 `isa<>` / `dyn_cast<>` 的底层机制。LLVM 没有用 C++ 自带的 RTTI（`dynamic_cast`），而是自定义了一套更省、更快的类型识别：`getValueID()` 直接返回 `SubclassID`（[Value.h:L543-L545](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/IR/Value.h#L543-L545)），再由 `isa_impl` 特化做范围判断。例如「这个 `Value` 是不是指令」只需一次整数比较（[Value.h:L996-L1000](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/IR/Value.h#L996-L1000)）：

```cpp
template <> struct isa_impl<Instruction, Value> {
  static inline bool doit(const Value &Val) {
    return Val.getValueID() >= Value::InstructionVal;
  }
};
```

> 小贴士：`ValueTy` 这个枚举（[Value.h:L524-L531](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/IR/Value.h#L524-L531)）把所有子类编号集中管理，其中 `InstructionVal` 被刻意设为最大值，指令的真实 ID = `InstructionVal + opcode`。这就是注释里「没有 ID 恰好等于 `InstructionVal` 的值」的原因。

#### 4.1.3 源码精读

**`Value` 的核心数据成员**只有两个（外加一堆位域），定义在类的私有区（[Value.h:L117-L119](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/IR/Value.h#L117-L119)）——这段代码说明每个 Value 本质就是「一个类型 + 一个 use-list 头」：

```cpp
private:
  Type *VTy;
  Use *UseList = nullptr;
```

`SubclassID` 在最开头（[Value.h:L76](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/IR/Value.h#L76)），紧随其后的是一堆用位域（bitfield）压缩的标志位（`HasValueHandle`、`NumUserOperands`、`HasName`、`HasHungOffUses` 等），全部塞进寥寥几个字节里。

**构造函数**把上述成员初始化好（[Value.cpp:L54-L75](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/lib/IR/Value.cpp#L54-L75)）：注意 `UseList` 默认为 `nullptr`（新生的 `Value` 还没人用它），并做了一些类型合法性断言。

**析构函数**里有一段非常能说明问题的 Debug 检查（[Value.cpp:L77-L106](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/lib/IR/Value.cpp#L77-L106)），这是理解「def-use 链必须自洽」的最佳注脚：

```cpp
  if (!materialized_use_empty()) {
    dbgs() << "While deleting: " << *VTy << " %" << getName() << "\n";
    for (auto *U : users())
      dbgs() << "Use still stuck around after Def is destroyed:" << *U << "\n";
    llvm_unreachable("Uses remain when a value is destroyed!");
  }
```

含义：**销毁一个 `Value` 时，它的 use-list 必须已经为空。** 如果还有别的指令引用着它，就说明你制造了「悬空引用」（dangling reference）——这在 LLVM 里属于严重错误。这条断言反过来证明：use-list 始终如实记录着「谁还在用我」。

#### 4.1.4 代码实践（源码阅读型：给 Value 分类）

**实践目标**：把「几乎所有 IR 对象都是 `Value`」从口号变成可验证的事实。

**操作步骤**：

1. 准备本讲第 2 节那段 `.ll`（`@f` 函数）。人工列出其中出现的每一个「值」：`@f`、`%a`、`%b`、`%sum`、`%sum2`、常量 `1`。
2. 对每个值，写出它的**具体子类**（`Function` / `Argument` / `Instruction` / `ConstantInt`），并按下表预测它满足哪些 `isa<>`：

   | 值 | 具体子类 | isa\<Instruction\> | isa\<Constant\> | isa\<User\> | isa\<Value\> |
   |----|----------|--------------------|-----------------|-------------|--------------|
   | `@f` | Function | 否 | 是 | 是 | 是 |
   | `%a` | Argument | 否 | 否 | 否 | 是 |
   | `%sum` | Instruction | 是 | 否 | 是 | 是 |
   | `1` | ConstantInt | 否 | 是 | 是 | 是 |

3. 对照源码核对你的预测：`isa<Instruction>` 走 [Value.h:L996-L1000](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/IR/Value.h#L996-L1000) 的 `>= InstructionVal`；`isa<Constant>` 走 [Value.h:L961-L967](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/IR/Value.h#L961-L967) 的 `<= ConstantLastVal`；`User::classof` 则是「Instruction 或 Constant」（[User.h:L336-L338](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/IR/User.h#L336-L338)）。

**需要观察的现象**：`%a`（Argument）虽然是 `Value`，却**不是** `User`——它没有操作数，只被别人用。而 `@f` 和 `1` 既是 `Constant` 又是 `User`——它们也能「使用」别的常量（例如复合常量 `ConstantArray` 引用其元素）。

**预期结果**：你会得出一条重要结论——**`User` = `Instruction` ∪ `Constant`**（[User.h:L336-L338](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/IR/User.h#L336-L338)），即「有操作数的值」恰好就是指令和常量两大类。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `Value` 的析构函数不是 `virtual`，却仍然能正确析构子类（比如一条 `Instruction`）？

> **参考答案**：因为 `Value` 用 `SubclassID` 替代了 vtable。删除时不能写 `delete vptr`，而必须调 `vptr->deleteValue()`，它根据 `getValueID()` 在 `switch` 里 `static_cast` 到正确子类再 `delete`（[Value.cpp:L108-L136](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/lib/IR/Value.cpp#L108-L136)）。省掉 vtable 指针是为了把每个 `Value` 压到 24 字节（[Value.cpp:L73-L74](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/lib/IR/Value.cpp#L73-L74)），这在数十亿实例的规模下意义巨大。

**练习 2**：给定一个 `Value *V`，不包含任何子类头文件，如何判断它是不是「指令」？底层做了几次运算？

> **参考答案**：写 `isa<Instruction>(*V)`。它调用 [Value.h:L996-L1000](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/IR/Value.h#L996-L1000) 的 `isa_impl`，等价于 `V->getValueID() >= Value::InstructionVal`——读一个 `unsigned char` 字段、做一次整数比较即可，开销极低。

---

### 4.2 Use / User：构成 SSA 的 def-use 双向链

#### 4.2.1 概念说明

如果说 `Value` 回答了「我是一个值」，那么 `Use` / `User` 回答了「值与值之间如何连接」。先看 `Use` 的官方定义（[Use.h:L29-L34](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/IR/Use.h#L29-L34)）：

> A Use represents **the edge between a Value definition and its users**. This is notionally a **two-dimensional linked list**.

一句话：**一个 `Use` 就是 def-use 图上的一条边。** 它连接两端的桥梁是 `User`——`User` 是「拥有操作数」的 `Value`（[User.h:L44](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/IR/User.h#L44)，`class User : public Value`），指令和常量都是 `User`。

理解这条链的关键，是抓住 `Use` 同时参与的**两个维度**（这就是「two-dimensional」的含义）：

- **维度 1（User 的操作数数组）**：每个 `User` 持有一段连续的 `Use` 数组，即它的操作数列表。`getOperand(i)` 就是取数组第 `i` 个 `Use` 所指向的 `Value`。沿着这个数组走，回答的是「**我（User）用了哪些值**」——即 **use-def** 方向。
- **维度 2（Value 的 use-list）**：每个 `Value` 有一个 `UseList`，把所有「指向我」的 `Use` 串成一条侵入式链表。沿着这条链走，回答的是「**谁（哪些 User）在用我**」——即 **def-use** 方向。

妙处在于：**同一个 `Use` 对象，既是它所属 `User` 操作数数组里的一个元素（维度 1），又是它所指向 `Value` 的 use-list 里的一个节点（维度 2）。** 一条边，两头各挂一处。所以从任何一条边都能 O(1) 跳到两端：`Use::get()` 拿到被引用的 `Value`，`Use::getUser()` 拿到使用方 `User`（[Use.h:L54-L61](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/IR/Use.h#L54-L61)）。

用本讲第 2 节的 IR 画成图（每个 `Use` 是一条带箭头的边）：

```
        维度1：User 的操作数数组                维度2：Value 的 use-list
        （回答：我用了谁？）                    （回答：谁在用我？）

  %sum (Instruction, 也是 User)              %a (Argument, 仅是 Value)
  ┌─ operand[0]: Use ─────────────────────►  UseList ──► Use(op0 of %sum) ──► nil
  │  operand[1]: Use ────────────┐                          │
  └──────────────────────────────┘                          │ 每条 Use 都能：
                                                               │  · get()     → %a   (跳到 def)
  %sum2 (Instruction)                                          │  · getUser() → %sum (跳到 user)
  ┌─ operand[0]: Use ────────────┐                          │  · getNext() → 下一条边
  │  operand[1]: Use(常量1)        │
  └──────────────────────────────┘     %sum 的 use-list：
                                       UseList ──► Use(op0 of %sum2) ──► nil
```

读法：`%sum` 的操作数数组里有两个 `Use`，分别指向 `%a`、`%b`（这是 `%sum` 的 use-def，即「它用了谁」）；同时 `%a` 的 use-list 里挂着一个 `Use`，它的 `getUser()` 是 `%sum`（这是 `%a` 的 def-use，即「谁用了它」）。注意 `%a`、`%b` 那两个 `Use` 是**同一个对象**，从两边看过去是同一条边。

#### 4.2.2 核心流程

**（1）双向链是如何自动维护的**

当你写下 `%sum = add i32 %a, %b`，IRBuilder 在构造这条 `Instruction`（一个 `User`）时，会为它的两个操作数各创建一个 `Use`，分别指向 `%a` 和 `%b`。指向一个 `Value` 的动作由 `Use::set` 完成（它在 [Value.h:L874-L879](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/IR/Value.h#L874-L879) 内联实现）：

```cpp
void Use::set(Value *V) {
  removeFromList();   // 先把自己从旧 Value 的 use-list 里摘下来
  Val = V;            // 记住新指向的 Value
  if (V)
    V->addUse(*this); // 再把自己挂到新 Value 的 use-list 头部
}
```

`addUse` 把这个 `Use` 插到 `Value::UseList` 链表头部（[Value.h:L513-L516](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/IR/Value.h#L513-L516)），插入与摘除的指针操作在 `Use::addToList` / `removeFromList` 里（[Use.h:L87-L93](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/IR/Use.h#L87-L93)、[Use.h:L95-L104](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/IR/Use.h#L95-L104)）。**关键结论：你永远不需要手动去更新别人的 use-list——`Use::set` 一调用，两边同步。** 这就是为什么 4.1.3 里那条「销毁时 use-list 必须为空」的断言能成立：只要还有 `Use` 指着一个 `Value`，那条 `Use` 就一定在这个 `Value` 的链表上。

**（2）两个方向的遍历 API**

从 `Value` 出发找使用者（def-use）：`use_empty()` 判空（[Value.h:L346-L349](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/IR/Value.h#L346-L349)），`uses()` 给出「所有 `Use`」（[Value.h:L380-L383](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/IR/Value.h#L380-L383)），`users()` 给出「所有 `User`」（[Value.h:L426-L429](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/IR/Value.h#L426-L429)）。两者底层都从 `UseList` 头开始沿 `Next` 走。

从 `User` 出发找操作数（use-def）：`operands()` 给出操作数 `Use` 区间（[User.h:L267-L269](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/IR/User.h#L267-L269)），`getOperand(i)` 取单个操作数的 `Value`（[User.h:L207-L210](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/IR/User.h#L207-L210)），`getNumOperands()` 给个数（[User.h:L229](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/IR/User.h#L229)）。

**（3）use 与 user 的区别**

这是本讲最易混淆、却必须分清的点。`hasOneUse()`（[Value.h:L439](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/IR/Value.h#L439)）与 `hasOneUser()`（[Value.h:L449-L457](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/IR/Value.h#L449-L457)）并不等价。官方注释说得很清楚：

> Note that this is not the same as "has one use". If a value has one use, then there certainly is a single user. But if value has several uses, it is possible that all uses are in a single user, or not.

例如 `%x = mul i32 %a, %a`：对 `%a` 而言有**两个 use**（`mul` 的两个操作数都是它），但只有**一个 user**（那条 `mul`）。`hasOneUser()` 需要遍历整条 use-list 比较相邻 `getUser()` 是否相同，所以注释也提醒它「可能较慢」。

**（4）RAUW：改写 IR 的万能钥匙**

任何一个优化 Pass，本质上都在做「把某些值替换成另一些值」。LLVM 把这个操作收敛成一个方法：`replaceAllUsesWith`（[Value.h:L300](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/IR/Value.h#L300)，简称 **RAUW**）。它的实现 `doRAUW`（[Value.cpp:L518-L551](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/lib/IR/Value.cpp#L518-L551)）核心就是一个循环：

```cpp
while (!materialized_use_empty()) {
  Use &U = *UseList;          // 取 use-list 头部那条边
  // ...（对常量 User 特殊处理）...
  U.set(New);                 // 把这条边重定向到 New
}
```

每调一次 `U.set(New)`，那条 `Use` 就从「this 的 use-list」摘下、挂到「New 的 use-list」，循环到 `this` 的 use-list 为空为止。**RAUW 之所以能成立，正是因为 use-list 完整、忠实地记录了「所有」使用者**——这又把我们引回 SSA。

**（5）与 SSA 的关系**

SSA（Single Static Assignment，单静态赋值）要求：每个值在其作用域内只被**定义一次**。在 LLVM IR 里：

- 「定义一次」= 每个 `Value` 对象只对应一个 def 点（一条指令的输出、一个参数、一个常量）。
- 「所有引用」= 指向这个 `Value` 的每一条 `Use`，都必然挂在这个 `Value` 的 use-list 上，无一遗漏。

因此 def-use 链是**完备**的：从任一定义出发，能枚举它的全部使用者；从任一使用出发，能追溯到唯一一个定义。这正是优化器敢于做「死代码删除」「常量传播」「拷贝消除」等改写的安全保障——比如 RAUW 把 `%old` 全替换成 `%new` 后，`%old` 的 use-list 一定变空，于是可以安全删除它（4.1.3 的断言就是这层安全保障的守门员）。

> 关于控制流汇合处的「多一定义」：当多条路径给同一个变量带来不同值时，LLVM 用 `phi` 节点在汇合点按来源块选值（见 [u2-l2](u2-l2-read-write-ir.md)）。`phi` 仍是「每个 `Value` 定义一次」——它定义了一个新值，只是这个值的来源由前驱块决定。def-use 链对 `phi` 同样适用。

#### 4.2.3 源码精读

**`Use` 的数据成员**（[Use.h:L82-L85](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/IR/Use.h#L82-L85)）——四个字段正好对应「一条边」所需的全部信息：

```cpp
Value *Val = nullptr;     // 这条边指向谁（def 端）
Use *Next = nullptr;      // 维度2链表：同一 Value 的下一条 Use
Use **Prev = nullptr;     // 维度2链表：指向前驱的「指向我的指针」（双向，便于 O(1) 删除）
User *Parent = nullptr;   // 这条边归谁所有（user 端）
```

`Prev` 用了「指向指针的指针」（`Use **`）这种侵入式链表经典技巧，使得删除任意一个节点都是 O(1)，无需从头查找——见 `removeFromList`（[Use.h:L95-L104](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/IR/Use.h#L95-L104)）。

**`getUser` 与 `get`**（[Use.h:L54-L61](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/IR/Use.h#L54-L61)）分别给出边的两端；`getNext`（[Use.h:L71](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/IR/Use.h#L71)）沿维度 2 前进。

**操作数下标怎么算**：`getOperandNo` 用指针算术一步算出本 `Use` 在 `User` 操作数数组里的下标（[Use.cpp:L36-L38](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/lib/IR/Use.cpp#L36-L38)）：

```cpp
unsigned Use::getOperandNo() const {
  return this - getUser()->op_begin();
}
```

这说明操作数数组在内存里是**连续**的，下标 = 当前 `Use` 地址减去数组起点，除以 `sizeof(Use)`。

**`User` 的操作数数组布局**：`User` 的构造函数（[User.h:L119-L135](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/IR/User.h#L119-L135)）展示了常见布局——操作数 `Use` 数组被**协同分配**在 `User` 对象**之前**的内存里，每个 `Use` 用 `Parent=this` 构造：

```cpp
Use *Operands = reinterpret_cast<Use *>(this) - NumUserOperands;
for (unsigned I = 0; I < NumUserOperands; ++I)
  new (&Operands[I]) Use(this);
```

> 补充：这是固定操作数个数（如 `add` 恒有 2 个操作数）的常见情况，称为 intrusive operands；对于操作数个数可变的指令（如 `phi`、`call`），`User` 改用「hung-off」布局——操作数数组挂在另一块分配里，由 `HasHungOffUses` 位区分。`getOperandList()`（[User.h:L200-L202](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/IR/User.h#L200-L202)）按这个位选择两种布局之一。两者对外的 API 完全一致。

**`dropAllReferences`**（[User.h:L324-L327](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/IR/User.h#L324-L327)）把所有操作数置空（每个 `U.set(nullptr)` 都会顺带把自己从对方 use-list 摘除）——这是销毁一个 `User` 前的标配动作，保证不会留下指向即将失效对象的引用。

**一个真实的 RAUW 用法**：官方示例 `SimplifyCFG` 在删除一个不可达块前，先把块内每条指令的所有使用替换为 `poison`，再删除指令（[SimplifyCFG.cpp:L82-L88](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/examples/IRTransforms/SimplifyCFG.cpp#L82-L88)）：

```cpp
// Replace all instructions in BB with a poison constant. The block is
// unreachable, so the results of the instructions should never get used.
while (!BB.empty()) {
  Instruction &I = BB.Back();
  I.replaceAllUsesWith(PoisonValue::get(I.getType()));
  I.eraseFromParent();
}
```

这正是 RAUW 的典型用法：**改完使用关系，再删定义**，从而保证 4.1.3 那条「use-list 为空才能销毁」的断言不被触发。

#### 4.2.4 代码实践（本讲核心实践：双向遍历 def-use）

**实践目标**：给定一条指令，用 **use-def** 关系找到它的操作数定义，用 **def-use** 关系找到它的所有使用者。这是本讲规格要求的实践任务。

**操作步骤**：

1. 用本讲第 2 节的 `@f` 函数，把注意力集中在中间那条 `%sum = add i32 %a, %b` 上。它是一个 `Instruction &I`（因而也是 `User`），同时又是一个 `Value`。

2. **use-def 方向**（I 用了谁？）——遍历 `User` 的操作数。下面这段是演示 API 用法的**示例代码**（非项目原有代码），展示如何枚举操作数及其来源：

   ```cpp
   // 示例代码：遍历指令 I 的操作数，找到每个操作数的「定义」
   for (unsigned Op = 0; Op < I.getNumOperands(); ++Op) {
       Value *V = I.getOperand(Op);          // use-def：第 Op 个操作数指向的 Value
       errs() << "  operand " << Op << " : " << *V << "\n";
       // 若 V 是 Instruction/Argument，它就是该操作数的「定义点」
   }
   // 等价的 range 写法：for (Value *Op : I.operands()) { ... }
   ```

   预期：`operand 0` 打印 `i32 %a`，`operand 1` 打印 `i32 %b`——它们正是 `%sum` 的两个 use-def 来源。

3. **def-use 方向**（谁用了 I？）——遍历 `Value` 的 use-list。**示例代码**：

   ```cpp
   // 示例代码：遍历 %sum 的所有使用者
   for (User *U : I.users()) {               // def-use：每个引用 I 的 User
       errs() << "  used by: " << *U << "\n";
   }
   // 或遍历「边」本身：for (Use &U : I.uses()) { U.getUser(); U.getOperandNo(); }
   ```

   预期：`I.users()` 会打印 `%sum2 = add i32 %sum, 1`——`%sum2` 是 `%sum` 唯一的 user。

4. （可选，需构建环境）把上述片段写成一个最小的 [新 PM Pass](u4-l4-write-your-own-pass.md)，在 `run(Function &F)` 里对每条指令打印它的操作数与使用者，用 `opt -passes=yourpass` 跑一遍 `@f` 的 `.ll`。

**需要观察的现象**：

- `%sum` 的 use-def 与 def-use 是**同一条边的两个方向**：`%sum` 的操作数里有 `%a`，反过来 `%a` 的 users 里就有 `%sum`。
- `%a`（Argument）的 `users()` 同时包含 `%sum`；`%b` 同理。`%a` 自己没有操作数（不是 `User`），所以对它调 `getNumOperands()` 在编译期就不可行（类型上就不是 `User`）。
- 若把 IR 改成 `%x = mul i32 %a, %a`，则 `%a` 的 `uses()` 有两个元素、但 `users()` 只有一个（那条 `mul`）——直观印证 4.2.2「use ≠ user」。

**预期结果**：对 `@f`，`%sum` 打印出 2 个操作数（`%a`、`%b`）、1 个使用者（`%sum2`）。

**关于运行**：若当前环境没有可链接的 LLVM 库，本实践可完全降级为**源码阅读型**——对照 [User.h:L207-L210](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/IR/User.h#L207-L210)（`getOperand`）、[User.h:L267-L269](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/IR/User.h#L267-L269)（`operands()`）、[Value.h:L426-L429](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/IR/Value.h#L426-L429)（`users()`）确认上述 API 调用合法，并口述每个调用的返回值。运行结果待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `Use::getOperandNo()`（[Use.cpp:L36-L38](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/lib/IR/Use.cpp#L36-L38)）能用 `this - op_begin()` 这种指针减法算下标？它依赖什么前提？

> **参考答案**：它依赖「`User` 的操作数是一段**连续**的 `Use` 数组」这一前提（固定操作数用 intrusive 布局，见 [User.h:L132-L134](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/IR/User.h#L132-L134)）。两个 `Use*` 相减即得到下标差，除以 `sizeof(Use)` 隐含在指针算术里。对 hung-off 布局，操作数仍是一段连续数组（只是分配在另一块内存），所以该公式同样成立。

**练习 2**：`replaceAllUsesWith(New)` 执行完之后，`this` 的 `use_empty()` 一定为真吗？为什么？

> **参考答案**：一定为真。`doRAUW` 的 `while (!materialized_use_empty())` 循环（[Value.cpp:L532-L544](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/lib/IR/Value.cpp#L532-L544)）每次取 use-list 头那条 `Use` 调 `U.set(New)`，而 `set` 会把该 `Use` 从 `this` 的链表摘除（[Value.h:L874-L879](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/IR/Value.h#L874-L879)）。循环到链表为空才退出，故结束时 use-list 必空。（常量 User 会走 `handleOperandChange` 特殊路径，但最终效果一致。）

**练习 3**：判断对错并说明理由——「一个 `Value` 的 `uses()` 元素个数，等于它的 `users()` 元素个数」。

> **参考答案**：**错**。`uses()` 数的是「边」（`Use`），`users()` 数的是「使用方」（`User`）。当同一个 `User` 多次引用同一个 `Value` 时（如 `%x = mul %a, %a`），`%a` 有 2 个 use 却只有 1 个 user。只有「每个 user 至多用一次」时两者才相等。这正是 `hasOneUse`（[Value.h:L439](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/IR/Value.h#L439)）与 `hasOneUser`（[Value.h:L449-L457](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/IR/Value.h#L449-L457)）不等价的原因。

---

## 5. 综合实践

把本讲的两条线（`Value` 的属性 + def-use 双向链）串起来，完成下面这个贯穿性小任务——**手写一个最简的「无用指令检测」**。它正是死代码消除（DCE）Pass 的核心思路。

**背景**：一条**有副作用但不产生可用结果**的指令（典型如纯算术 `add`/`mul`），如果它的结果**没有任何使用者**，就是无用的，可以删除。判定「没有任何使用者」用的正是本讲的 `use_empty()`。

**任务**：

1. 阅读下面这段**示例代码**（非项目原有代码），它遍历一个函数，找出结果无人使用的非终结指令：

   ```cpp
   // 示例代码：朴素的死指令检测思路（仅示意，非完整 DCE）
   #include "llvm/IR/Function.h"
   #include "llvm/IR/Instructions.h"
   #include "llvm/Support/raw_ostream.h"
   using namespace llvm;

   void findDeadInstructions(Function &F) {
     SmallVector<Instruction *, 16> Dead;
     for (Instruction &I : instructions(F)) {
       if (I.isTerminator())            // ret/br 等终结指令不能删
         continue;
       if (I.mayHaveSideEffects())      // 有副作用（store/call 等）不能轻易删
         continue;
       if (I.use_empty())               // 关键判定：结果无人使用 → def-use 为空
         Dead.push_back(&I);
     }
     for (Instruction *I : Dead)
       errs() << "dead: " << *I << "\n";
   }
   ```

2. 解释清楚三件事，把知识串起来：
   - 为什么用 `I.use_empty()` 就能断定「无人使用 I 的结果」？（答：因为 `Value::use_empty()` 直接检查 `UseList == nullptr`，见 [Value.h:L346-L349](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/IR/Value.h#L346-L349)；use-list 完备地记录了所有引用。）
   - 删除 `I` 之前需要先 `I.replaceAllUsesWith(...)` 吗？（答：不需要，因为它已经 `use_empty()`，没有使用者要改写；这正是 4.1.3 那条析构断言能通过的原因。）
   - 真要删除时，参考 [SimplifyCFG.cpp:L86-L87](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/examples/IRTransforms/SimplifyCFG.cpp#L86-L87) 的范式——它对**有**使用者的指令先 RAUW 再删；这里对象是**无**使用者的，可直接 `I->eraseFromParent()`。

3. 构造一段含无用指令的 `.ll` 来验证思路，例如：

   ```llvm
   define i32 @demo(i32 %a) {
   entry:
     %useless = mul i32 %a, 0     ; 结果 0 没人用（且无副作用）→ 应被判为 dead
     ret i32 42
   }
   ```
   `%useless` 的 `users()` 为空，会被上述代码打印为 `dead`。而 `ret i32 42` 是终结指令、`%a` 是参数（不是指令），都不在候选里。

**进阶（可选）**：把 `findDeadInstructions` 改写成一个真正的新 PM Pass 并删除死指令（删除后可能让上游指令也变 dead，故需循环到不动点）。这会自然地把你引向 [u4-l4 编写你自己的 LLVM Pass](u4-l4-write-your-own-pass.md) 与经典的 `DCE` Pass。

> 若无构建环境，可降级为纯阅读：找到 `llvm/lib/Transforms/Scalar/DeadCodeElimination.cpp`，对照其中 `use_empty()` 的判定与删除循环，确认它和上面的示例思路一致。删除到不动点的真实实现待本地验证。

## 6. 本讲小结

- **`Value` 是几乎所有 IR 对象的根基类**：`Function`（经 `GlobalObject → GlobalValue → Constant → User`）、`BasicBlock`、`Argument`、`Instruction`、`Constant` 最终都是 `Value`。它自带四样东西：类型 `VTy`、可选名字、use-list 头 `UseList`、子类 ID `SubclassID`。
- 为了把每个 `Value` 压到 24 字节，**它没有虚析构**，改用 `SubclassID` 派发的 `deleteValue()` 来销毁（[Value.cpp:L108-L136](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/lib/IR/Value.cpp#L108-L136)）；同一套 ID 也是 `isa<>`/`dyn_cast<>` 的底层（[Value.h:L996-L1000](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/IR/Value.h#L996-L1000)）。
- **`Use` 是一条边**，同时挂在两处：既是 `User` 操作数数组里的一个元素（维度 1，use-def），又是所指向 `Value` 的 use-list 里的一个节点（维度 2，def-use）。这就是官方说的「two-dimensional linked list」。
- **`User` = `Instruction` ∪ `Constant`**（[User.h:L336-L338](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/IR/User.h#L336-L338)），是「有操作数的 Value」。`operands()`/`getOperand()` 给 use-def 方向，`uses()`/`users()` 给 def-use 方向。
- **use ≠ user**：`%x = mul %a, %a` 对 `%a` 有 2 个 use、1 个 user；`hasOneUse` 与 `hasOneUser` 因此不等价。
- **SSA 的完备性靠这条链保证**：每个 `Value` 只定义一次、use-list 如实记录全部引用，所以 `replaceAllUsesWith`（RAUW）能「一次性改完所有使用者」，改完后 use-list 必空，方可安全删除定义。

## 7. 下一步学习建议

本讲打通了「值是什么」与「值与值怎么连」。接下来有两条自然的延伸：

- **[u3-l3 类型系统与常量](u3-l3-type-system-constants.md)**：本讲反复出现的 `getType()` 返回的 `Type *` 到底是什么？`Type` 为何在 `LLVMContext` 里被唯一化？`ConstantInt`/`ConstantExpr` 这些「常量 Value」如何表示？这些都在下一讲展开。
- **[u3-l4 IRBuilder：以代码构造 IR](u3-l4-irbuilder.md)**：本讲强调了 `Use::set` 会自动维护双向链——而你在代码里创建指令时，几乎不会手写 `Use::set`，而是用 `IRBuilder`。下一讲就讲它如何便捷地构造指令、管理插入点，并顺带做常量折叠。

建议在进入下一讲前，先做两件事巩固本讲：

1. 重读 [Value.h:L874-L879](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/IR/Value.h#L874-L879) 的 `Use::set` 与 [Value.cpp:L532-L544](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/lib/IR/Value.cpp#L532-L544) 的 `doRAUW` 循环，确认你能在脑中演练「一条边从旧 Value 摘下、挂到新 Value」的全过程。
2. 浏览 [llvm/include/llvm/IR/User.h](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/IR/User.h) 里的 `operands()` / `operand_values()` / `replaceUsesOfWith()`，为下一讲用 `IRBuilder` 构造并改写 IR 建立接口印象。

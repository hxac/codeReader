# AST Bridge：从 C++ 到 Quake

## 1. 本讲目标

CUDA-Q 的 C++ 前端 `cudaq-quake` 要做的第一件事，就是把一段普通的 C++ 源码（带有 `__qpu__` 标注的内核）翻译成上一讲（u4-l2）讲过的 **Quake/CC MLIR**。承担这个翻译工作的组件就是 **AST Bridge（AST 桥）**。

学完本讲，你应当能够：

1. 说清楚 Clang 的 `RecursiveASTVisitor` 是如何“遍历”一棵 AST 的，以及 CUDA-Q 为什么要用「后序遍历 + 两个栈」来组装 MLIR。
2. 描述 AST Bridge 从“发现内核”到“逐个翻译内核”的两遍流程，并理解内核名（mangled name）的生成规则。
3. 拿着一个具体的门调用（例如 `x(q)`），沿着 `VisitCallExpr` → `buildOp` 这条路径，准确说出它最终生成哪一条 Quake 操作。
4. 知道测量句柄、控制流（`if`/`for`）、量子比特分配在桥里分别落到哪段代码，以及哪些 C++ 写法会被桥直接拒绝。

本讲依赖 u4-l2（Quake 方言）建立的词汇表：`quake.alloca`、`quake.x`、`quake.mz`、`!quake.ref`/`!quake.veq`、控制位方括号语法等。如果你还不熟悉这些，建议先读 u4-l2。

## 2. 前置知识

### 2.1 什么是 AST

C++ 源码经过词法、语法分析后，编译器会把它表示成一棵 **抽象语法树（Abstract Syntax Tree, AST）**。例如：

```cpp
void operator()(double t) __qpu__ {
  cudaq::qubit q;
  ry(t, q);
  mz(q);
}
```

在 Clang 的 AST 里，它大致是这样的节点树（简化）：

- `CXXMethodDecl`（`operator()` 这个函数声明）
  - 参数 `ParmVarDecl`（`double t`）
  - 函数体 `CompoundStmt`（语句块）
    - `DeclStmt` → `VarDecl`（`cudaq::qubit q`）
    - `CallExpr`（`ry(t, q)`）
    - `CallExpr`（`mz(q)`）

AST Bridge 的工作就是「遍历这棵树，每访问到一个节点，就生成对应的 MLIR 操作，最后拼出一个 `func.func`」。Clang 提供了一个现成的遍历框架叫 `RecursiveASTVisitor`，AST Bridge 就建立在它之上。

### 2.2 RecursiveASTVisitor 的约定

`RecursiveASTVisitor`（简称 RAV）是 Clang 提供的一个模板基类。它的核心机制是通过「名称约定」来挂钩：

- 你定义 `bool VisitFoo(clang::Foo *x)`，RAV 在「访问完」每个 `Foo` 节点后就会回调它。
- 你定义 `bool TraverseFoo(clang::Foo *x)`，RAV 会用你的版本代替默认的「递归遍历子节点」逻辑。
- `bool WalkUpFromFoo(clang::Foo *x)` 控制是否要继续向上走基类的 Visit。

CUDA-Q 的桥类 `QuakeBridgeVisitor` 就是 `RecursiveASTVisitor<QuakeBridgeVisitor>` 的子类。理解这一点是读懂本讲全部源码的前提。

## 3. 本讲源码地图

AST Bridge 的实现拆成 5 个文件，按 AST 节点类别分工，编译成一个库 `cudaq-mlirgen`（见 [cudaq/lib/Frontend/nvqpp/CMakeLists.txt:12-19](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/cudaq/lib/Frontend/nvqpp/CMakeLists.txt#L12-L19)）：

| 文件 | 职责 |
| --- | --- |
| [cudaq/include/cudaq/Frontend/nvqpp/ASTBridge.h](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/) | 桥的核心声明：`QuakeBridgeVisitor`（RAV 子类）、`ASTBridgeAction`/`ASTBridgeConsumer`（Clang 前端动作入口）。 |
| [cudaq/lib/Frontend/nvqpp/ASTBridge.cpp](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/) | 入口实现：内核发现（`QPUCodeFinder`）、两遍翻译流程（`HandleTranslationUnit`）、内核名 mangling。 |
| [cudaq/lib/Frontend/nvqpp/ConvertExpr.cpp](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/) | **表达式（Expr）翻译**：最大的一块，所有门调用、测量、运算符、类型转换都在这里。 |
| [cudaq/lib/Frontend/nvqpp/ConvertStmt.cpp](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/) | **语句（Stmt）翻译**：`if`/`for`/`while`/`return` 等控制流。 |
| [cudaq/lib/Frontend/nvqpp/ConvertDecl.cpp](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/) | **声明（Decl）翻译**：函数声明、参数声明、变量声明（含量子比特分配）。 |

> 另有 `ConvertType.cpp` 负责类型翻译（`BuiltinType`/`PointerType` 等），本讲不深入。

## 4. 核心概念与源码讲解

本讲拆成五个最小模块：① RAV 机制与「双栈」设计；② 内核发现与两遍翻译；③ 门调用翻译（重点 + 实践任务）；④ 测量、控制流与参数；⑤ 扩展点与常见陷阱。

### 4.1 RecursiveASTVisitor 机制与「双栈」设计

#### 4.1.1 概念说明

桥的核心类是 `QuakeBridgeVisitor`，它继承自 `RecursiveASTVisitor`：

[cudaq/include/cudaq/Frontend/nvqpp/ASTBridge.h:170-172](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/cudaq/include/cudaq/Frontend/nvqpp/ASTBridge.h#L170-L172) —— 声明桥类是 RAV 的子类：

```cpp
class QuakeBridgeVisitor
    : public clang::RecursiveASTVisitor<QuakeBridgeVisitor> {
  using Base = clang::RecursiveASTVisitor<QuakeBridgeVisitor>;
```

头文件里那段注释把整体设计讲得很清楚（[ASTBridge.h:156-169](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/cudaq/include/cudaq/Frontend/nvqpp/ASTBridge.h#L156-L169)）：

> The general design is to walk the tree in a **post-order traversal** and assemble the IR from the leaves back down the tree. … Traversals over types should push Type values to the **type stack**. Traversals over expressions should create IR … as well as push subexpressions on the **[value] stack** for parent nodes.

要点有三条：

1. **后序遍历**：先访问子节点，再访问父节点。`shouldTraversePostOrder()` 返回 `true` 开启这个模式（[ASTBridge.h:514](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/cudaq/include/cudaq/Frontend/nvqpp/ASTBridge.h#L514)）。这样父节点被访问时，它的所有子节点已经把结果「放好」了。
2. **值栈 `valueStack`**：每个表达式节点访问完后，把自己产生的 `mlir::Value` 压栈；父节点（比如 `CallExpr`）按需从栈顶弹出若干个作为自己的操作数。
3. **类型栈 `typeStack`**：类型翻译的结果压到这里，供声明类节点（函数签名、变量声明）取用。

这两个栈是整个桥的「数据总线」——节点之间不直接传值，全靠栈通信。

#### 4.1.2 核心流程

以 `ry(t, q)` 这个调用为例，后序遍历的访问顺序是：

```text
1. VisitDeclRefExpr(t)        → 压入 t 的 Value
2. VisitCXXConstructExpr(q)   → 压入 q 的 Value (!quake.ref)
3. VisitCallExpr(ry(t,q))     → 弹出 [t, q]，生成 quake.ry，不入栈(无返回值)
```

栈操作有三个基本原语，定义在头文件里（[ASTBridge.h:609-630](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/cudaq/include/cudaq/Frontend/nvqpp/ASTBridge.h#L609-L630)）：

- `pushValue(v)`：压一个值。
- `popValue()`：弹出栈顶。
- `lastValues(n)`：弹出并返回最后 `n` 个值，**保持左右顺序**。注释里给了一个直观例子：对 `foo(a, b, c)`，`lastValues(3)` 返回 `[value_a value_b value_c]`。

Debug 构建里，`pushValue`/`popValue` 会打印带缩进的日志，能看到完整的入栈/出栈轨迹（[ASTBridge.cpp:403-436](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/cudaq/lib/Frontend/nvqpp/ASTBridge.cpp#L403-L436)），这是排查桥问题最趁手的工具。

#### 4.1.3 源码精读

来看一个最典型的「先弹子节点、再造当前节点」的写法——`VisitCallExpr` 的入口校验：

[cudaq/lib/Frontend/nvqpp/ConvertExpr.cpp:1518-1529](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/cudaq/lib/Frontend/nvqpp/ConvertExpr.cpp#L1518-L1529) —— 入口先把被调函数与所有实参从栈上「点清点」：

```cpp
bool QuakeBridgeVisitor::VisitCallExpr(clang::CallExpr *x) {
  auto loc = toLocation(x->getSourceRange());
  auto *callee = x->getCalleeDecl();
  auto *func = dyn_cast<clang::FunctionDecl>(callee);
  ...
  assert(valueStack.size() >= x->getNumArgs() + 1 &&
         "stack must contain all arguments plus the expression to call");
  StringRef funcName;
  if (auto *id = func->getIdentifier())
    funcName = id->getName();
```

这里的断言就是「双栈契约」的体现：到 `VisitCallExpr` 时，栈上至少要有「实参个数 + 1」个值——多出来的 1 个是被调函数本身（作为 `func::ConstantOp` 压的）。拿到 `funcName` 后，接下来的大段 `if (funcName == ...)` 就是分门别类的翻译逻辑（见 4.3）。

#### 4.1.4 代码实践

**实践目标**：亲眼看到「双栈」是如何随遍历伸缩的。

**操作步骤**：

1. 准备一个最小内核（保存为 `stack_trace.cpp`）：

   ```cpp
   #include <cudaq.h>
   struct K {
     void operator()() __qpu__ {
       cudaq::qubit q;
       x(q);
     }
   };
   ```

2. 用 debug 构建的 `cudaq-quake` 处理它，并打开桥的调试日志：

   ```bash
   cudaq-quake stack_trace.cpp -debug-only=lower-ast 2>&1 | grep -E "push value|pop value"
   ```

   > 说明：`-debug-only=lower-ast` 来自源码里的 `#define DEBUG_TYPE "lower-ast"`（[ASTBridge.cpp:25](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/cudaq/lib/Frontend/nvqpp/ASTBridge.cpp#L25)），只有在 `NDEBUG` 未定义（debug 构建）时才生效。

**需要观察的现象**：日志里会出现带缩进的 `+push value` / `-pop value` 行，缩进深度随遍历层次增减，对应 `x(q)` 时会弹出 `q` 再生成 `quake.x`。

**预期结果**：能看到「qubit 构造 → push」、再到「call x → pop q → 生成 quake.x」的成对出入栈记录。**待本地验证**：若你的 `cudaq-quake` 是 release 构建，将看不到日志，需改用 debug 构建。

#### 4.1.5 小练习与答案

**练习 1**：为什么桥要用后序遍历，而不是先序？
**答案**：因为父节点（如 `CallExpr`）需要子节点（如实参表达式）先求值，才能拿到自己的操作数。后序遍历天然保证了「子先于父」，配合值栈，父节点访问时栈顶恰好是它需要的子结果。

**练习 2**：`lastValues(3)` 在 `foo(a, b, c)` 调用里返回什么顺序？
**答案**：返回 `[value_a, value_b, value_c]`，即与源码一致的左到右顺序（见 [ASTBridge.h:618-627](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/cudaq/include/cudaq/Frontend/nvqpp/ASTBridge.h#L618-L627) 的注释）。

### 4.2 内核发现与两遍翻译

#### 4.2.1 概念说明

Clang 在解析完整个翻译单元（translation unit）后，会把所有顶层声明交给一个 `ASTConsumer`。CUDA-Q 提供了 `ASTBridgeConsumer`（[ASTBridge.h:738](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/cudaq/include/cudaq/Frontend/nvqpp/ASTBridge.h#L738)）。它的任务有两步：

1. **发现**：在整个 AST 里找出所有带 `__qpu__` 标注的内核，收集到 `functionsToEmit` 列表。
2. **翻译**：对列表里的每个内核，创建 `QuakeBridgeVisitor` 去遍历它，生成 MLIR。

回顾 u1-l4：`__qpu__` 本质是 `__attribute__((annotate("quantum")))`。桥就用这个 annotation 来识别内核：

[cudaq/lib/Frontend/nvqpp/ASTBridge.cpp:473-479](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/cudaq/lib/Frontend/nvqpp/ASTBridge.cpp#L473-L479) —— 内核就是带 `"quantum"` annotation 的函数：

```cpp
bool ASTBridgeAction::ASTBridgeConsumer::isQuantum(
    const clang::FunctionDecl *decl) {
  if (auto attr = decl->getAttr<clang::AnnotateAttr>())
    return attr->getAnnotation().str() == cudaq::kernelAnnotation;
  return false;
}
```

`cudaq::kernelAnnotation` 就是字符串 `"quantum"`。同理 `isCustomOpGenerator` 识别自定义门生成器（`generatorAnnotation`）。

#### 4.2.2 核心流程

发现逻辑由一个独立的轻量 visitor `QPUCodeFinder` 完成（[ASTBridge.cpp:166-398](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/cudaq/lib/Frontend/nvqpp/ASTBridge.cpp#L166-L398)）。它在 `HandleTopLevelDecl` 里被调用（[ASTBridge.cpp:704-713](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/cudaq/lib/Frontend/nvqpp/ASTBridge.cpp#L704-L713)），把每个内核（用 mangled 的「标签名」标识）连同它的 `FunctionDecl*` 放进 `functionsToEmit`，并顺带建一棵 `CallGraph`。

`QPUCodeFinder` 还做了一件重要的「语义检查」工作：禁止非内核函数使用量子类型。在 `VisitVarDecl` 里（[ASTBridge.cpp:368-380](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/cudaq/lib/Frontend/nvqpp/ASTBridge.cpp#L368-L380)），如果当前函数不是内核、却声明了 `qubit`/`qvector` 等类型，就直接报错。这是「量子类型只能在内核里用」这条规则的落点。

翻译阶段在 `HandleTranslationUnit` 里，采用**两遍**结构（[ASTBridge.cpp:598-702](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/cudaq/lib/Frontend/nvqpp/ASTBridge.cpp#L598-L702)）：

```text
第 1 遍：为每个内核生成「函数声明」(generateFunctionDeclaration)
        —— 只翻译签名，把 FuncOp 放进 ModuleOp，尚无函数体
第 2 遍：对每个内核做 TraverseDecl(FunctionDecl)
        —— 真正遍历函数体，逐条生成 Quake/CC 操作
        —— 完成后打上 kernel / entry-point 属性，必要时补一个宿主侧占位函数
```

为什么要先声明、再定义？因为内核 A 的函数体里可能调用内核 B，此时需要 B 的 `FuncOp` 已经存在，才能正确生成 `func.call`。第一遍先「占位」，第二遍再「填肉」，就保证了任意调用顺序都能解析。

#### 4.2.3 源码精读

来看第二遍里给内核「贴标签」的关键逻辑——区分入口内核（entry-point）和纯设备内核：

[cudaq/lib/Frontend/nvqpp/ASTBridge.cpp:652-680](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/cudaq/lib/Frontend/nvqpp/ASTBridge.cpp#L652-L680) —— 按签名是否含量子类型，决定一个内核是「入口」还是「设备内部」：

```cpp
auto unitAttr = UnitAttr::get(ctx);
func->setAttr(kernelAttrName, unitAttr);          // 所有内核都打上 kernel
bool hasDeviceOnlyTypes =
    hasAnyQuakeOrHandleTypes(func.getFunctionType());
...
if (!hasDeviceOnlyTypes && ...) {
  func->setAttr(entryPointAttrName, unitAttr);    // 没有量子参数 → 入口内核
  addFunctionDecl(fdPair.second, visitor, func.getFunctionType(),
                  entryName, func.empty());        // 补一个宿主侧函数
}
```

这里的关键判断 `hasAnyQuakeOrHandleTypes`（[ASTBridge.cpp:67-78](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/cudaq/lib/Frontend/nvqpp/ASTBridge.cpp#L67-L78)）：如果内核的参数或返回类型里有量子类型（`!quake.ref`/`!quake.veq`）或测量句柄（`!cc.measure_handle`），那它就是「纯量子」的，只能被其他内核调用、不能从宿主直接 launch。反之就是**入口内核**——也就是 `cudaq::sample` 等能在宿主侧启动的那个。这条边界在 u3-l1（执行模型）里会再次出现。

内核名生成由 `getTagNameOfFunctionDecl` 负责（[ASTBridge.cpp:112-160](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/cudaq/lib/Frontend/nvqpp/ASTBridge.cpp#L112-L160)）：普通函数是 `function_<名>.<mangled>`，类成员是 `<类的 mangled 类型名>`，模板特化还会拼上模板参数。最终再加一个 `nvq++` 前缀（`__nvqpp__mlirgen__`），就是你在 IR 里看到的 `@__nvqpp__mlirgen__super` 这样的函数名（见 4.4.3 的 FileCheck 例子）。

#### 4.2.4 代码实践

**实践目标**：验证「内核识别靠 annotation、入口/非入口靠签名」。

**操作步骤**：

1. 读上面引用的 `isQuantum`（[ASTBridge.cpp:473-479](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/cudaq/lib/Frontend/nvqpp/ASTBridge.cpp#L473-L479)）和 `hasAnyQuakeOrHandleTypes`（[ASTBridge.cpp:67-78](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/cudaq/lib/Frontend/nvqpp/ASTBridge.cpp#L67-L78)）。
2. 写两个内核对比：一个签名是 `void operator()() __qpu__`（无量子参数，是入口），一个是 `void operator()(cudaq::qubit& q) __qpu__`（带量子参数，是设备内部）。
3. 用 `cudaq-quake` 各自生成 IR，对比 `func.func` 上挂的属性。

**需要观察的现象**：无量子参数的内核会同时带 `quake.kernel` 和 `quake.entry_point` 属性；带 `qubit&` 参数的只有 `quake.kernel`，且函数名同样带 `__nvqpp__mlirgen__` 前缀。

**预期结果**：签名差异直接决定能否从宿主启动。**待本地验证**。

#### 4.2.5 小练习与答案

**练习**：`getTagNameOfFunctionDecl` 对「普通函数」「类成员函数」「函数模板特化」分别生成什么前缀？
**答案**：普通函数 `function_<名>.<mangled>`；类成员 `<类类型 mangled>`；模板特化额外拼模板参数（[ASTBridge.cpp:142-160](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/cudaq/lib/Frontend/nvqpp/ASTBridge.cpp#L142-L160)）。这些 tag 再加上 `__nvqpp__mlirgen__` 前缀就是 MLIR 里的函数符号名。

### 4.3 门调用翻译（重点）

#### 4.3.1 概念说明

这是本讲的重头戏，也是本讲的实践任务所在。一个门调用，比如 `x(q)`、`ry(t, q)`、`x<cudaq::ctrl>(c, t)`，在 C++ 层面就是一个对 `cudaq::` 命名空间下某函数的 `CallExpr`。桥的任务是把它翻成对应的 Quake 操作：`quake.x`、`quake.ry`、带控制位的 `quake.x [...]`。

整个翻译用一个**名字驱动的分发**实现：拿到 `funcName` 后，一连串 `if (funcName == ...)` 把它映射到对应的 Quake Op 类型，再用模板函数 `buildOp` 统一生成。这套设计的好处是——新增一个门，往往只需要加一行 `if`。

#### 4.3.2 核心流程

门调用的处理路径是这样的：

```text
VisitCallExpr(x)
  ├── (弹掉 this 指针、callee 等技术性值)
  ├── 判定 isInNamespace(func, "cudaq")          ← 是不是 cudaq 命名空间里的函数?
  ├── 从模板参数探测修饰符: isAdjoint (adj) / isControl (ctrl)
  └── 按 funcName 分发:
        mz/mx/my  → quake.{Mz,Mx,My}Op           (4.4.1)
        h/x/y/z/... → buildOp<quake::XOp>(...)    ← 本模块核心
        control/adjoint/compute_action → 整块子内核受控/求逆 (见 ConvertExpr 后段)
```

`buildOp` 是统一的「造门」函数，它处理三种参数形态（注释见 [ConvertExpr.cpp:259-273](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/cudaq/lib/Frontend/nvqpp/ConvertExpr.cpp#L259-L273)）：

1. `op(qubit...)`：最后一个比特是 target，前面都是 control。
2. `op(qurange, qubit)`：control 打包在一个 veq 里。
3. `op(qurange)`：对 veq 里每个比特逐个施加门（语法糖，无控制位）。

它还处理两个修饰维度：**adjoint**（伴随，`<adj>`）和 **control**（受控），以及负控（4.3.3 讲）。

#### 4.3.3 源码精读

先看分发入口——修饰符的探测。在 `VisitCallExpr` 进入 `cudaq` 分支后，先看模板参数是不是 `ctrl` 或 `adj`：

[cudaq/lib/Frontend/nvqpp/ConvertExpr.cpp:1900-1915](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/cudaq/lib/Frontend/nvqpp/ConvertExpr.cpp#L1900-L1915) —— 从门调用的模板实参里读出 ctrl/adj 修饰符：

```cpp
if (isInNamespace(func, "cudaq")) {
  bool isAdjoint = false;
  bool isControl = false;
  auto *functionDecl = x->getCalleeDecl()->getAsFunction();
  if (auto *templateArgs = functionDecl->getTemplateSpecializationArgs())
    if (templateArgs->size() > 0) {
      auto gateModifierArg = templateArgs->asArray()[0];
      ...
      isAdjoint = structTypeAsRecord->getName() == "adj";
      isControl = structTypeAsRecord->getName() == "ctrl";
    }
```

这正是 u2-l2 讲过的「修饰符是空类型标签 `ctrl`/`adj`，作为模板参数」在编译器侧的落点。

接着看门名到 Quake Op 的映射。以 `x` 为例：

[cudaq/lib/Frontend/nvqpp/ConvertExpr.cpp:2067-2082](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/cudaq/lib/Frontend/nvqpp/ConvertExpr.cpp#L2067-L2082) —— `x`/`cx`/`cnot`/`ccx` 都映射到 `quake::XOp`，区别只在是否带控制位：

```cpp
if (funcName == "h")
  return buildOp<cudaq::quake::HOp>(builder, loc, args, negations,
                                    reportNegateError, /*adjoint=*/false,
                                    isControl);
...
if (funcName == "x")
  return buildOp<cudaq::quake::XOp>(builder, loc, args, negations,
                                    reportNegateError, /*adjoint=*/false,
                                    isControl);
if (funcName == "cnot" || funcName == "cx" || funcName == "ccx")
  return buildOp<cudaq::quake::XOp>(builder, loc, args, negations,
                                    reportNegateError, /*adjoint=*/false,
                                    /*control=*/true);
```

注意两点：① 源码层面的 `cnot`/`cx`/`ccx` 在 Quake 里**没有独立的操作**，它们就是「带控制位的 `quake.x`」——这与 u4-l2 讲的「CNOT/Toffoli 就是 `x` 带控制位」完全一致。② `isControl` 直接来自上面的模板参数探测；用户写 `x<cudaq::ctrl>(...)` 时 `isControl=true`，写 `cx(...)` 时硬编码 `/*control=*/true`，两者殊途同归。

现在看 `buildOp` 内部。对一个最简单的 `x(q)`（单 target、无控制、无负控），命中 else 分支里的 `ctrls.empty()` 路径：

[cudaq/lib/Frontend/nvqpp/ConvertExpr.cpp:317-334](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/cudaq/lib/Frontend/nvqpp/ConvertExpr.cpp#L317-L334) —— 没有控制位时，对每个 target 各造一个门：

```cpp
auto [target, ctrls] =
    maybeUnpackOperands(builder, loc, operands, isControl);
...
auto negs =
    negatedControlsAttribute(builder.getContext(), ctrls, negations);
if (ctrls.empty())
  // 可能有多个 target,但无控制位, op(q, r, s, ...)
  for (auto t : target)
    A::create(builder, loc, isAdjoint, ValueRange(), ValueRange(), t, negs);
else {
  assert(target.size() == 1 && ...);
  A::create(builder, loc, isAdjoint, ValueRange(), ctrls, target, negs);
}
```

对 `x(q)`：`maybeUnpackOperands` 在非控制态下原样返回 `target=[q], ctrls=[]`（[ConvertExpr.cpp:196-197](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/cudaq/lib/Frontend/nvqpp/ConvertExpr.cpp#L196-L197)），于是走 `ctrls.empty()` 分支，调用 `quake::XOp::create(..., t, negs)`，最终打印出：

```mlir
quake.x %q : (!quake.ref) -> ()
```

**负控（negated control）** 的实现值得单独看。u2-l2 讲过：负控靠写在比特上的 `!` 操作符（`qudit::operator!`）实现。在桥里，`operator!` 并不立刻生成门，而是把该比特塞进一个待处理列表 `negations`：

[cudaq/lib/Frontend/nvqpp/ConvertExpr.cpp:3108-3114](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/cudaq/lib/Frontend/nvqpp/ConvertExpr.cpp#L3108-L3114) —— `!q` 把 q 登记到 `negations`，原地返回 q 本身：

```cpp
// Lower cudaq::qudit<>::operator!()
if (isInClassInNamespace(func, "qudit", "cudaq") &&
    isExclaimOperator(x->getOperator())) {
  auto qubit = popValue();
  negations.push_back(qubit);
  return replaceTOSValue(qubit);
}
```

等到紧随其后的门调用时，`buildOp` 通过 `negatedControlsAttribute` 把 `negations` 转成一个布尔数组属性，附加到生成的门上，并清空 `negations`：

[cudaq/lib/Frontend/nvqpp/ConvertExpr.cpp:245-257](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/cudaq/lib/Frontend/nvqpp/ConvertExpr.cpp#L245-L257) —— 把 `negations` 列表翻译成 `neg` 数组属性，然后清空列表：

```cpp
static DenseBoolArrayAttr
negatedControlsAttribute(MLIRContext *ctx, ValueRange ctrls,
                         SmallVector<Value> &negations) {
  if (negations.empty())
    return {};
  SmallVector<bool> negatedControls(ctrls.size());
  for (auto v : llvm::enumerate(ctrls))
    negatedControls[v.index()] = std::find(negations.begin(), negations.end(),
                                           v.value()) != negations.end();
  auto boolVecAttr = DenseBoolArrayAttr::get(ctx, negatedControls);
  negations.clear();          // ← 消费完即清空,避免污染下一个门
  return {boolVecAttr};
}
```

这条官方测试把整套机制演示得明明白白，是本模块的最佳参照：

[cudaq/test/AST-Quake/negation.cpp:13-28](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/cudaq/test/AST-Quake/negation.cpp#L13-L28) —— `x<cudaq::ctrl>(!qr[0], qr[1], qr[2])` 的预期 IR：

```cpp
struct NegationOperatorTest {
  void operator()() __qpu__ {
    cudaq::qvector qr(3);
    x<cudaq::ctrl>(!qr[0], qr[1], qr[2]);
  }
};
// CHECK: %[[VAL_0:.*]] = quake.alloca !quake.veq<3>
// CHECK: quake.x [%[[VAL_1]], %[[VAL_2]] neg [true, false]] %[[VAL_3]] ...
```

源码里 `!qr[0]` 登记进 `negations`，门调用时 `qr[0]`、`qr[1]` 作 control，`qr[2]` 作 target，`neg [true, false]` 正好标记第 0 个 control 为负控。读到这里，你应该已经能在脑子里把这条 IR「反编译」回 C++ 了。

#### 4.3.4 代码实践（本讲主实践任务）

**实践目标**：在 `ConvertExpr.cpp` 中定位处理门调用的代码路径，追踪 `x(q)` 调用最终生成哪个 Quake 操作。

**操作步骤**：

1. **定位入口**。打开 [ConvertExpr.cpp:1518](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/cudaq/lib/Frontend/nvqpp/ConvertExpr.cpp#L1518) 的 `VisitCallExpr`。确认 `funcName` 是怎么从 `func->getIdentifier()->getName()` 拿到的。
2. **跟到 cudaq 分发**。读到 [ConvertExpr.cpp:1900](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/cudaq/lib/Frontend/nvqpp/ConvertExpr.cpp#L1900) 的 `if (isInNamespace(func, "cudaq"))`，理解 `funcName == "x"` 这一支（[ConvertExpr.cpp:2075](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/cudaq/lib/Frontend/nvqpp/ConvertExpr.cpp#L2075)）调用 `buildOp<cudaq::quake::XOp>(...)`。
3. **跟进 buildOp**。读 [ConvertExpr.cpp:274-338](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/cudaq/lib/Frontend/nvqpp/ConvertExpr.cpp#L274-L338)。对单 target 的 `x(q)`，确认它走 `else` 分支、`maybeUnpackOperands` 返回 `ctrls` 为空、最终调用 `A::create(...)` 即 `quake::XOp::create`。
4. **用 IR 验证**。写下面这个最小内核：

   ```cpp
   // trace_x.cpp
   #include <cudaq.h>
   struct K {
     void operator()() __qpu__ {
       cudaq::qubit q;
       x(q);
     }
   };
   ```

   运行 `cudaq-quake trace_x.cpp`，在输出里找 `quake.x`。

**需要观察的现象**：IR 里会出现一行形如 `quake.x %[[q]] : (!quake.ref) -> ()` 的操作，没有控制位、没有 `neg` 属性。

**预期结果**：`x(q)` 一对一对应到一条 `quake.x`，且 target 是 q 对应的 `!quake.ref` 值。如果再改成 `x<cudaq::ctrl>(q2, q)`，你应能看到 `quake.x [%q2] %q`。

> 进阶：把 `x(q)` 换成 `x<cudaq::ctrl>(!q2, q)`，对比输出里是否出现 `neg [true]`，并把这与 4.3.3 的 `negatedControlsAttribute` 对应起来。**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：`cnot(a, b)` 和 `x<cudaq::ctrl>(a, b)` 在 IR 里有区别吗？
**答案**：没有。两者都进入 `buildOp<cudaq::quake::XOp>(..., /*control=*/true)`（[ConvertExpr.cpp:2075-2082](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/cudaq/lib/Frontend/nvqpp/ConvertExpr.cpp#L2075-L2082)），生成同一条带控制位的 `quake.x`。它们只是源码层的不同写法（语义糖）。

**练习 2**：`!q` 在桥里会立刻生成一个 X 门吗？
**答案**：不会。它只是把 `q` 登记进 `negations` 列表（[ConvertExpr.cpp:3112](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/cudaq/lib/Frontend/nvqpp/ConvertExpr.cpp#L3112)），真正生成「负控」属性是在紧随其后的门调用里，由 `negatedControlsAttribute` 消费并清空。是否最终展开成前后两个 X 门，由后续 Pass 决定（参考 u2-l2 关于 `C¬(U)=(X⊗I)C(U)(X⊗I)` 的说明）。

### 4.4 测量、控制流与参数翻译

#### 4.4.1 概念说明

**测量**在桥里走的是一条与门类似的 `funcName` 分支，但生成的是 `quake.mz/mx/my`，结果是 u2-l3 讲过的**测量句柄** `!cc.measure_handle`，而不是普通的 i1。这是「延迟判别（deferred discrimination）」的实现：测量本身不立刻给出 0/1，只有在被 `if`/`bool` 转换等场景「消费」时，才插入 `quake.discriminate` 把句柄塌缩成 i1。

**控制流**（`if`/`for`/`while`）由 `ConvertStmt.cpp` 负责，翻译成 CC 方言的 `cc.if`、循环等结构——注意不是 Quake 操作，因为循环和分支是经典计算。

#### 4.4.2 核心流程

测量翻译（在 `VisitCallExpr` 的 cudaq 分支里）：

[cudaq/lib/Frontend/nvqpp/ConvertExpr.cpp:2024-2044](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/cudaq/lib/Frontend/nvqpp/ConvertExpr.cpp#L2024-L2044) —— `mz/mx/my` 翻成测量 Op，结果是 `!cc.measure_handle`：

```cpp
if (funcName == "mx" || funcName == "my" || funcName == "mz") {
  ...
  Type measTy = cc::MeasureHandleType::get(builder.getContext());
  if (useStdvec)
    measTy = cc::StdvecType::get(measTy);
  if (funcName == "mx")
    return pushValue(cudaq::quake::MxOp::create(...).getMeasOut());
  if (funcName == "my")
    return pushValue(cudaq::quake::MyOp::create(...).getMeasOut());
  return pushValue(
      cudaq::quake::MzOp::create(builder, loc, measTy, args).getMeasOut());
}
```

注意 `mx`/`my` 并不是「先旋转再 mz」，那是**库模式**（u2-l3）的做法；在 MLIR 模式下，桥直接发出独立的 `quake.mx`/`quake.my`，把基变换推迟到后端。

「消费句柄」的判别发生在**隐式类型转换**处。当 `measure_handle` 被转成 `bool`（典型场景：`if (mz(q))`），命中 `CK_UserDefinedConversion` 这一支，桥插入 `quake.discriminate`：

[cudaq/lib/Frontend/nvqpp/ConvertExpr.cpp:902-925](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/cudaq/lib/Frontend/nvqpp/ConvertExpr.cpp#L902-L925) —— 句柄转 bool 时才插入 `quake.discriminate`：

```cpp
// `cudaq::measure_handle::operator bool()` is the single sanctioned
// coercion surface on `measure_handle`. ...
if (auto intTy = dyn_cast<IntegerType>(castToTy);
    intTy && intTy.getWidth() == 1) {
  Value handleVal = loadHandleIfPointer(builder, loc, sub);
  if (isa<cc::MeasureHandleType>(handleVal.getType())) {
    ...
    return pushValue(cudaq::quake::DiscriminateOp::create(
        builder, loc, builder.getI1Type(), handleVal));
  }
}
```

这条官方测试把「句柄—判别—返回」的完整链条拍成了 FileCheck，非常直观：

[cudaq/test/AST-Quake/single_qubit_ctor.cpp:37-44](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/cudaq/test/AST-Quake/single_qubit_ctor.cpp#L37-L44) 与 [:23-32](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/cudaq/test/AST-Quake/single_qubit_ctor.cpp#L23-L32) —— `return mz(q)` 的 IR：

```cpp
struct super {
  bool operator()(double inputPi) __qpu__ {
    cudaq::qubit q;
    rx(inputPi, q);
    ry(inputPi / 2.0, q);
    return mz(q);          // mz 产生 measure_handle, return 转 bool 时插入 discriminate
  }
};
// CHECK: %[[V5:.*]] = quake.mz %[[V1]] : (!quake.ref) -> !cc.measure_handle
// CHECK: %[[VAL_6:.*]] = quake.discriminate %[[HL]] :
// CHECK: return %[[VAL_6]] : i1
```

`mz(q)` 出来的是 `!cc.measure_handle`；`return` 要求 `bool`，于是桥在转换点插入了 `quake.discriminate`，得到 i1 再返回。这正是 u2-l5「延迟判别」在桥侧的真实样子。

#### 4.4.3 源码精读：控制流与比特分配

控制流的代表是 `TraverseIfStmt`。它不靠 `VisitIfStmt`，而是用 `TraverseIfStmt` 自定义整个遍历，把 then/else 各自包进一个 region：

[cudaq/lib/Frontend/nvqpp/ConvertStmt.cpp:509-545](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/cudaq/lib/Frontend/nvqpp/ConvertStmt.cpp#L509-L545) —— `if` 被翻译成 `cc.if`，条件值从值栈弹出：

```cpp
bool QuakeBridgeVisitor::TraverseIfStmt(clang::IfStmt *x, ...) {
  ...
  auto stmtBuilder = [&](clang::Stmt *stmt) {
    return [&, stmt](OpBuilder &builder, Location loc, Region &region) {
      ...
      builder.setInsertionPointToStart(&bodyBlock);
      if (!TraverseStmt(stmt)) { result = false; return; }
      if (!hasTerminator(region.back()))
        cc::ContinueOp::create(builder, loc);
    };
  };
  ...
  cc::IfOp::create(builder, loc, TypeRange{}, popValue(),
                   stmtBuilder(x->getThen()), stmtBuilder(x->getElse()));
```

注意 `popValue()` 弹出的是条件——如果条件来自测量句柄，那它已经在 4.4.2 的 cast 路径里被 `quake.discriminate` 成了 i1。`TraverseForStmt`（[ConvertStmt.cpp:574](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/cudaq/lib/Frontend/nvqpp/ConvertStmt.cpp#L574) 起）思路类似，把 init/cond/inc/body 拆进 CC 循环结构。

量子比特的分配发生在变量声明处。`VisitVarDecl` 根据类型分流：

[cudaq/lib/Frontend/nvqpp/ConvertDecl.cpp:691-726](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/cudaq/lib/Frontend/nvqpp/ConvertDecl.cpp#L691-L726) —— `qvector`/`qreg` 翻成 `quake.alloca`，单个 `qubit` 翻成「大小为 1 的 veq + extract_ref」：

```cpp
if (auto qType = dyn_cast<cudaq::quake::VeqType>(type)) {
  // !quake.veq 类型 (qvector/qreg)
  ...
  qreg = cudaq::quake::AllocaOp::create(builder, loc, qType, qregSizeVal);
  symbolTable.insert(name, qreg);
  return pushValue(qreg);
}
if (auto qType = dyn_cast<cudaq::quake::RefType>(type)) {
  // !quake.ref 类型 (单个 qubit)
  ...
  auto qregSizeOne = cudaq::quake::AllocaOp::create(
      builder, loc, cudaq::quake::VeqType::get(builder.getContext(), 1));
  Value addressTheQubit =
      cudaq::quake::ExtractRefOp::create(builder, loc, qregSizeOne, zero);
  symbolTable.insert(name, addressTheQubit);
```

这解释了 4.3.4 实践里 `cudaq::qubit q;` 为什么会产生一个 `quake.alloca !quake.veq<1>` 再 `extract_ref`——单比特在 Quake 层面也是「从 1 元 veq 里取出来」。

参数翻译在 `VisitParmVarDecl`（[ConvertDecl.cpp:596-617](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/cudaq/lib/Frontend/nvqpp/ConvertDecl.cpp#L596-L617)）：参数并不在这里「创建」，而是在 `createEntryBlock`（[ConvertDecl.cpp:110](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/cudaq/lib/Frontend/nvqpp/ConvertDecl.cpp#L110)）时就已经作为入口 block 的参数挂好、塞进符号表；`VisitParmVarDecl` 只是确认符号表里已有这个名字并压栈。这是「函数参数先于函数体被处理」的体现。

#### 4.4.4 代码实践

**实践目标**：观察「延迟判别」与控制流的 IR 形态。

**操作步骤**：

1. 准备内核：

   ```cpp
   #include <cudaq.h>
   struct Mid {
     void operator()() __qpu__ {
       cudaq::qubit q, r;
       x(q);
       if (mz(q))        // 测量句柄驱动 if
         x(r);
     }
   };
   ```

2. 运行 `cudaq-quake` 生成 IR。

**需要观察的现象**：`mz(q)` 产生 `!cc.measure_handle`；`if` 条件处出现 `quake.discriminate` 把句柄转 i1；then 分支被包在 `cc.if` 里。

**预期结果**：IR 里依次出现 `quake.mz` → `quake.discriminate` → `cc.if`，且 `cc.if` 的 then region 里有 `quake.x %r`。对照 4.4.2 的 `single_qubit_ctor.cpp` 测试理解。**待本地验证**。

#### 4.4.5 小练习与答案

**练习**：为什么 `mz(q)` 不直接返回 i1，而要返回 `measure_handle`？
**答案**：为了支持「延迟判别」与「线路中途测量」。桥侧只发出测量动作，把句柄传下去；只有在真正需要 0/1（`if`、`return bool` 等）时才插入 `quake.discriminate` 塌缩。这让后端可以选择是批量采样还是逐 shot 塌缩（见 u2-l5）。

### 4.5 扩展点与常见陷阱

#### 4.5.1 概念说明

桥是一个「按名字硬编码」的翻译器：它认识 `cudaq::x`、`cudaq::mz` 等一组**已知**函数名。这意味着：

- **扩展点 1：自定义门**。如果用户注册了自定义门，桥需要知道它的名字。这靠 `customOperationNames` 映射和 `isCustomOpGenerator` 识别（[ASTBridge.cpp:481-486](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/cudaq/lib/Frontend/nvqpp/ASTBridge.cpp#L481-L486)），细节在 u7-l4。
- **扩展点 2：拦截运算符**。`operator[]`、`operator()`、`operator!` 都在 `VisitCXXOperatorCallExpr` 里被拦截，分别翻译成 `quake.extract_ref`、内核间接调用、负控登记（见 4.3 与 [ConvertExpr.cpp:2935-3055](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/cudaq/lib/Frontend/nvqpp/ConvertExpr.cpp#L2935-L3055)）。
- **扩展点 3：未支持语句**。`TraverseAsmStmt`、`TraverseGotoStmt`、`TraverseSwitchStmt`、`TraverseCXXForRangeStmt` 等都被显式声明但不真正 lowering（[ASTBridge.h:289-306](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/cudaq/include/cudaq/Frontend/nvqpp/ASTBridge.h#L289-L306)），意味着内核里用 `goto`、`switch`、范围 `for` 会报错。

#### 4.5.2 常见陷阱

1. **「这个门/函数桥不认识」**。`VisitCallExpr` 末尾有个 fall-through，对未识别的 `funcName` 会走 `TODO`/报错路径。如果你自定义了一个 `cudaq::my_gate` 但没注册，就会撞到这里。新增门要改 4.3 那段 `if` 链。
2. **量子类型越界使用**。`QPUCodeFinder::VisitVarDecl`（[ASTBridge.cpp:368-380](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/cudaq/lib/Frontend/nvqpp/ASTBridge.cpp#L368-L380)）会拒绝在非内核函数里声明 `qubit`/`qvector` 等。
3. **测量句柄跨边界**。入口内核禁止让 `measure_handle` 跨宿主/设备边界，必须在内核内先判别（[ASTBridge.cpp:664-670](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/cudaq/lib/Frontend/nvqpp/ASTBridge.cpp#L664-L670)），否则报错——这与 u2-l5 的入口内核约束一致。
4. **未支持语句**。范围 `for`（`for (auto &e : v)`）当前会被拦截报错，需改写成经典 `for` 循环。
5. **`TODO_BRIDGE` / `TODO_x` 宏**（[ASTBridge.h:80-103](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/cudaq/include/cudaq/Frontend/nvqpp/ASTBridge.h#L80-L103)）：遇到「桥尚未实现」的写法时，会通过 Clang 诊断引擎报一个 `is not yet supported` 的错误，而不是默默生成错误代码。看到这条报错，就说明你撞到了桥的覆盖盲区。

## 5. 综合实践

把本讲的几条主线串起来，做一次「端到端」的源码阅读追踪。

**任务**：解释下面这个内核，从 C++ 源码到 `quake` IR 的**每一步**分别发生在桥的哪段代码。

```cpp
#include <cudaq.h>
struct Bell {
  void operator()() __qpu__ {
    cudaq::qubit a, b;
    h(a);
    x<cudaq::ctrl>(a, b);
    mz(a);
    mz(b);
  }
};
```

**要求完成的步骤**：

1. **发现阶段**：指出 `Bell::operator()` 是在哪段代码被判定为内核的（提示：`isQuantum`），它如何进入 `functionsToEmit`。
2. **签名/入口判定**：解释为什么它是入口内核（参考 `hasAnyQuakeOrHandleTypes`）。
3. **逐语句追踪**：对 `h(a)`、`x<cudaq::ctrl>(a, b)`、`mz(a)` 这三句，分别写出：
   - 命中 `VisitCallExpr` 的哪个 `funcName` 分支；
   - 调用了哪个 `buildOp<...>` 或直接造了哪个 Quake Op；
   - 生成的 `quake.*` 操作长什么样（控制位写法、有无 `neg`）。
4. **IR 验证**：用 `cudaq-quake bell.cpp` 实际生成 IR，与自己画的图对比。重点确认 `x<cudaq::ctrl>(a, b)` 是否变成 `quake.x [%a] %b`，以及两条 `mz` 是否都产生 `!cc.measure_handle`。
5. **延伸思考**：如果把 `x<cudaq::ctrl>(a, b)` 改成 `x<cudaq::ctrl>(!a, b)`，IR 里会多出什么属性？追到 `negatedControlsAttribute` 与 `operator!` 那两段代码解释。

**预期产出**：一张「C++ 表达式 → 桥中函数 → Quake 操作」的对照表。如果无法本地构建，标注「待本地验证」并把对照表填到源码阅读能确定的部分。

## 6. 本讲小结

- AST Bridge = `RecursiveASTVisitor` 子类 `QuakeBridgeVisitor`，用**后序遍历 + 值栈/类型栈**组装 MLIR；节点之间靠栈通信。
- 翻译分**两遍**：第一遍给所有内核生成声明占位，第二遍才填函数体；内核靠 `__qpu__`（`annotate("quantum")`）识别，靠签名是否含量子类型区分「入口内核」与「设备内部内核」。
- 门调用翻译是**名字驱动分发**：`VisitCallExpr` 进入 `cudaq` 分支后，一连串 `if (funcName == ...)` 把门名映射到 Quake Op，统一由 `buildOp` 生成；`cnot`/`cx`/`ccx` 没有独立 Op，就是「带控制位的 `quake.x`」。
- 修饰符 `ctrl`/`adj` 来自门调用的模板参数；负控靠 `operator!` 把比特登记进 `negations`，在紧随的门调用里转成 `neg` 数组属性。
- 测量 `mz/mx/my` 产生**测量句柄** `!cc.measure_handle`（延迟判别），只有在 `if`/`bool` 转换处才插入 `quake.discriminate` 塌缩成 i1；`if`/`for` 翻成 CC 方言的 `cc.if`、循环结构。

## 7. 下一步学习建议

- **下一讲 u4-l5（nvq++ 驱动脚本）**：本讲只讲了「单文件、单次翻译」。真实的 `nvq++` 是一个 bash 编排脚本，会多次调用 `cudaq-quake` 并串联起内核注册、入口改写、QIR lowering。学完本讲再去看 nvq++，你就能理解它在每一步「喂」给桥的是什么。
- **深入优化 Pass（u4-l6）**：桥生成的 Quake IR 还很「原始」（比如测量句柄四处散落、负控尚未展开）。建议接着读 `LambdaLifting`、`AggressiveInlining` 等 Pass，看它们如何把桥的输出整理成可 lowering 的形态。
- **对照 Python 前端（u5-l2）**：Python 端有平行的 `ast_bridge.py`（u5-l2），用 Python AST 而非 Clang AST 做类似的事。两者产出的 Quake IR 是同构的，对比阅读能加深对「桥」这一抽象的理解。
- **继续阅读的源码**：想看更复杂的翻译，可读 `ConvertExpr.cpp` 里 `cudaq::control`/`cudaq::adjoint`/`cudaq::compute_action` 的处理（[ConvertExpr.cpp:2254](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/cudaq/lib/Frontend/nvqpp/ConvertExpr.cpp#L2254) 起），它们把整块子内核受控/求逆，是 u2-l4「内核组合」在编译器侧的落点。

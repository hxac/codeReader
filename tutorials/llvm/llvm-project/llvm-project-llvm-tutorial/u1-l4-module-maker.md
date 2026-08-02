# 第一个 IR 程序：ModuleMaker

## 1. 本讲目标

学完本讲，你应该能够：

- 用 C++ **从零**构造一个最小的 LLVM Module，理解 `LLVMContext`、`Module`、`Function`、`BasicBlock`、`Instruction` 之间的构造与归属关系。
- 掌握「创建指令」与「把指令插入到基本块」这两步分离的 API 模式。
- 把构造好的内存 IR 用 `WriteBitcodeToFile` 写成 `.bc` 位码文件，并用 `llvm-dis` 验证。
- 看懂 `examples/ModuleMaker` 这个最小 out-of-tree 风格示例的 CMake 脚本，知道示例如何链接 LLVM 库、如何开启示例构建。

本讲是「反向」的起点：前面几讲里 `opt`、`llc`、`lli`、`llvm-as` 都是 **消费** IR 的工具，而本讲第一次让你 **生产** IR——把 LLVM 当成一个 C++ 库来调用。

## 2. 前置知识

在继续前，请确认你已经理解下面这些来自前面讲义的概念（本讲直接使用，不再展开）：

- **三段式编译模型**：前端 → IR → 后端。本讲停留在 IR 这一层的「构造」上。
- **IR 的两种等价格式**：人类可读的 `.ll` 文本与紧凑二进制 `.bc` 位码，二者可由 `llvm-as` / `llvm-dis` 无损互转（见 u1-l3）。
- **IR 的四层包含关系**：`Module`（一个翻译单元）包含若干 `Function`，`Function` 包含若干 `BasicBlock`，`BasicBlock` 包含若干 `Instruction`，且每个基本块以一条 **终结指令（terminator）** 收尾（如 `ret`）。
- **CMake 基础**：知道 `cmake -B build -G Ninja` 配置、`cmake --build build` 构建的基本节奏（见 u1-l2）。

本讲用到的一个新术语：

- **归属（ownership）**：LLVM 的 IR 对象有明确的树形归属——`Module` 拥有它内部的 `Function`，`Function` 拥有 `BasicBlock`，`BasicBlock` 拥有 `Instruction`。因此销毁 `Module` 时，整棵树会被自动回收。构造 API 中经常通过「把父对象作为最后一个参数」来实现自动挂接。

## 3. 本讲源码地图

本讲只围绕一个示例项目展开，它非常短：

| 文件 | 作用 |
| --- | --- |
| [examples/ModuleMaker/ModuleMaker.cpp](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/examples/ModuleMaker/ModuleMaker.cpp) | 全部逻辑所在：构造一个只含 `main` 函数的 Module 并把位码写到标准输出。 |
| [examples/ModuleMaker/CMakeLists.txt](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/examples/ModuleMaker/CMakeLists.txt) | 声明该示例依赖的 LLVM 组件，并用 `add_llvm_example` 注册目标。 |
| [examples/ModuleMaker/README.txt](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/examples/ModuleMaker/README.txt) | 一句话说明：这个示例展示 LLVM API 的最基本用法，以及如何链接 LLVM 库。 |

它还会引用到几个核心头文件（仅用于解释 API，不需要你修改）：`Function.h`、`DerivedTypes.h`、`BasicBlock.h`、`InstrTypes.h`、`Instruction.h`、`BitcodeWriter.h`。

## 4. 核心概念与源码讲解

### 4.1 创建 LLVMContext 与 Module

#### 4.1.1 概念说明

`LLVMContext` 是 LLVM 的「上下文」对象，可以理解成一个 **IR 对象的命名空间 / 容器**。同一个 `LLVMContext` 里的类型是唯一的（比如 `i32` 类型对象全局只有一份）；不同 `LLVMContext` 之间的对象互相隔离、不能混用。绝大多数 IR API 的第一个参数都是它。

`Module` 是一段 IR 在内存里的 **根对象**，对应一个「翻译单元」。它持有函数表、全局变量、数据布局、目标三元组（triple）等。在 C++ 中你需要先有一个 `LLVMContext`，再在它之上创建 `Module`。

#### 4.1.2 核心流程

```
1. 在栈上创建 LLVMContext（本程序只有一个）
2. 在堆上 new 一个 Module，并把 Context 绑定进去
3. Module 名字只是一个标识（如 "test"），与函数名无关
```

#### 4.1.3 源码精读

入口和上下文的创建：

[examples/ModuleMaker/ModuleMaker.cpp:30-35](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/examples/ModuleMaker/ModuleMaker.cpp#L30-L35) —— 创建 `LLVMContext`，再 `new Module("test", Context)`。这里用 `new` 在堆上创建 Module，是因为它需要在函数末尾被 `delete`，从而演示「显式回收整棵 IR 树」的归属模型。

注意：示例特意用了裸 `new` / `delete` 来教学。实际工程代码中更推荐 `std::unique_ptr` 等手段（LLVM 自身也在逐步这样迁移，例如近期提交就把一些裸指针改为 `std::move` 的 `unique_ptr`）。

#### 4.1.4 代码实践

1. **目标**：确认 `LLVMContext` 与 `Module` 的生命周期关系。
2. **步骤**：打开源码，定位到 `new Module("test", Context)` 那一行，把字符串 `"test"` 改成 `"my_first_module"`。
3. **观察现象**：重新编译运行后，生成的 `.ll` 文件顶部会出现形如 `; ModuleID = 'my_first_module'` 的注释行（`ModuleID` 就是构造时传入的名字）。
4. **预期结果**：`ModuleID` 随你传入的字符串变化；函数名 `main` 不受影响。完整运行结果属于「待本地验证」，但 `ModuleID` 行为是确定的。

#### 4.1.5 小练习与答案

- **练习**：如果把 `LLVMContext Context;` 这一行删掉会发生什么？
- **答案**：编译失败——后续每一处 `Type::getInt32Ty(Context)`、`BasicBlock::Create(Context, ...)`、`Module` 构造都依赖这个对象。`LLVMContext` 是几乎所有 IR 构造的入口。
- **练习**：`Module` 的名字（`"test"`）和它内部 `main` 函数的名字之间有依赖关系吗？
- **答案**：没有。`Module` 名字只是该翻译单元的标识（会写进 `ModuleID`），函数名由创建 `Function` 时单独指定。

### 4.2 构造 Function 与 BasicBlock

#### 4.2.1 概念说明

要创建一个函数，必须先描述它的 **类型** `FunctionType`（返回类型 + 参数类型列表 + 是否变参），再用这个类型去创建 `Function` 对象。本示例的 `main` 是 `int ()`——无参数、返回 `i32`、非变参。

`Function::Create` 有多个重载，其中一类重载允许把 `Module*` 作为最后一个参数：传入后，函数会被 **自动追加** 到该 Module 中，省去手动挂接。`BasicBlock::Create` 同理，传入 `Function*` 后基本块会自动挂到该函数下。

#### 4.2.2 核心流程

```
1. FunctionType::get(返回类型 i32, 非变参)        → 得到 "int()" 的函数类型
2. Function::Create(类型, 外部链接, "main", Module) → 创建函数并自动挂入 Module
3. BasicBlock::Create(Context, "EntryBlock", 函数)  → 创建入口基本块并自动挂入函数
```

`Function::ExternalLinkage` 表示这个函数对外可见（可以被别的模块调用、是符号表里的一个外部符号），这正是 `main` 应有的可见性。

#### 4.2.3 源码精读

函数类型与函数创建：

[examples/ModuleMaker/ModuleMaker.cpp:38-43](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/examples/ModuleMaker/ModuleMaker.cpp#L38-L43) —— 先用 `FunctionType::get` 描述 `int ()`，再用 `Function::Create` 创建 `main` 并把它挂进 Module `M`。

这里调用的 `Function::Create` 重载签名见 [include/llvm/IR/Function.h:175-179](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/include/llvm/IR/Function.h#L175-L179)，最后一个参数 `Module *M = nullptr` 正是「自动挂入模块」的开关；`FunctionType::get` 的非变参重载见 [include/llvm/IR/DerivedTypes.h:177](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/include/llvm/IR/DerivedTypes.h#L177)。

基本块创建：

[examples/ModuleMaker/ModuleMaker.cpp:47](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/examples/ModuleMaker/ModuleMaker.cpp#L47) —— `BasicBlock::Create(Context, "EntryBlock", F)` 把入口块挂到函数 `F` 下。其声明见 [include/llvm/IR/BasicBlock.h:206](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/include/llvm/IR/BasicBlock.h#L206)。

#### 4.2.4 代码实践

1. **目标**：理解「先类型，后函数」的两步构造。
2. **步骤**：把 `getInt32Ty(Context)` 临时改成 `getInt64Ty(Context)`（返回类型由 `i32` 改为 `i64`）。
3. **观察现象**：用 `llvm-dis` 查看输出，函数签名会从 `define i32 @main()` 变成 `define i64 @main()`；同时后续 `ConstantInt::get(...)` 创建的常量也会是 64 位。
4. **预期结果**：函数返回类型与 `FunctionType` 中声明的返回类型严格一致。把改动还原后再继续下一节。

#### 4.2.5 小练习与答案

- **练习**：`Function::ExternalLinkage` 改成 `InternalLinkage`（内部链接）会怎样？
- **答案**：`main` 在 IR 里会带上 `internal` 标记（`define internal i32 @main()`），表示它只在当前 Module 内可见。对 `main` 这种入口函数通常用 `ExternalLinkage`。
- **练习**：为什么创建 `Function` 时需要先有 `FunctionType`，而创建 `BasicBlock` 时不需要「类型」？
- **答案**：函数的签名（参数与返回类型）是它对外契约的一部分，必须显式描述；基本块只是「指令的有序容器」，没有独立的类型签名，所以只需 Context、名字、所属函数即可。

### 4.3 插入指令并写出位码

#### 4.3.1 概念说明

这是本讲最关键的一节。它体现 LLVM API 的一个重要模式：**创建指令（create）和插入指令（insert）是两件分离的事**。

- `BinaryOperator::Create(...)` 只是在堆上造出一条 `add` 指令对象，此时它 **还不在任何基本块里**，是一条「游离」的指令。
- 必须再调用 `Instruction::insertInto(BB, BB->end())`，才能把它挂进基本块。

之所以这样设计，是为了灵活性——你可以先构造一批指令，再决定它们的插入位置（开头、末尾、某条指令之前）。当然，本示例为了简洁，造完立刻插入到末尾。

另外两个要点：

- **常量**：`2`、`3` 这种立即数用 `ConstantInt::get(...)` 创建，它们是 `Value` 的子类，可以直接作为指令的操作数。
- **终结指令**：每个基本块必须以一条终结指令收尾。本例用 `ReturnInst::Create(Context, 返回值)` 创建 `ret`。

最后，`WriteBitcodeToFile(*M, outs())` 把整棵 Module 序列化为位码写到标准输出。

#### 4.3.2 核心流程

```
1. 用 ConstantInt::get 创建常量操作数 2、3
2. BinaryOperator::Create(Add, 2, 3, "addresult")  → 造出游离的 add 指令
3. Add->insertInto(BB, BB->end())                   → 把它插到入口块末尾
4. ReturnInst::Create(Context, Add)                 → 造出 ret %addresult
   ->insertInto(BB, BB->end())                      → 同样插到末尾
5. WriteBitcodeToFile(*M, outs())                    → 整个 Module 序列化为 .bc 写到 stdout
6. delete M                                          → 回收整棵 IR 树
```

最终对应的 IR（**未经任何优化**，因为示例没有跑优化器）是：

```llvm
define i32 @main() {
EntryBlock:
  %addresult = add i32 2, 3
  ret i32 %addresult
}
```

#### 4.3.3 源码精读

创建常量与加法指令：

[examples/ModuleMaker/ModuleMaker.cpp:50-58](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/examples/ModuleMaker/ModuleMaker.cpp#L50-L58) —— `BinaryOperator::Create(Instruction::Add, Two, Three, "addresult")` 只造指令、不插入；随后 `Add->insertInto(BB, BB->end())` 才真正把它挂进基本块。`BinaryOperator::Create` 的签名见 [include/llvm/IR/InstrTypes.h:233-235](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/include/llvm/IR/InstrTypes.h#L233-L235)，`insertInto` 的声明见 [include/llvm/IR/Instruction.h:262-263](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/include/llvm/IR/Instruction.h#L262-L263)。

返回指令：

[examples/ModuleMaker/ModuleMaker.cpp:61](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/examples/ModuleMaker/ModuleMaker.cpp#L61) —— `ReturnInst::Create(Context, Add)` 造出返回指令并把 `add` 的结果作为返回值，链式 `->insertInto(...)` 立刻插入。

写出位码与回收：

[examples/ModuleMaker/ModuleMaker.cpp:64-67](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/examples/ModuleMaker/ModuleMaker.cpp#L64-L67) —— `WriteBitcodeToFile(*M, outs())` 把 Module 写到 `outs()`（标准输出）；`delete M` 利用归属模型一次性回收整棵 IR 树。`WriteBitcodeToFile` 的声明见 [include/llvm/Bitcode/BitcodeWriter.h:132-136](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/include/llvm/Bitcode/BitcodeWriter.h#L132-L136)。

> 备注：这个示例 **没有** 调用 `verifyModule`（IR 验证器）。它构造的 IR 足够简单、必然合法；而 `opt`、`llc` 这类工具在处理输入 IR 时会调用验证器。等你自己开始构造更复杂的 IR 时，建议主动调用 `verifyModule` 来排查 SSA 错误（验证器将在后续讲义展开）。

#### 4.3.4 代码实践（本讲主任务）

**目标**：把 `main` 的返回值从 `2 + 3` 改成 `(2 + 3) * 4`，并用 `llvm-dis` 验证。

**操作步骤**：

1. 先确认环境里已经构建好 LLVM（含 `llvm-dis`）。ModuleMaker 默认 **不** 随主构建产出，因为 `add_llvm_example` 在未开启 `LLVM_BUILD_EXAMPLES` 时会把目标设为 `EXCLUDE_FROM_ALL`（见 [cmake/modules/AddLLVM.cmake:1659-1674](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/cmake/modules/AddLLVM.cmake#L1659-L1674)）。因此配置时需要显式开启：

   ```bash
   cmake -B build -G Ninja \
         -DLLVM_BUILD_EXAMPLES=ON \
         -DCMAKE_BUILD_TYPE=Release
   ```

2. 修改 `examples/ModuleMaker/ModuleMaker.cpp`，在 `add` 之后增加一个常量 `4` 和一条 `mul` 指令，并把返回值改成 `mul` 的结果。改后的关键片段（**示例代码**，对照原文件 50–61 行）：

   ```cpp
   // —— 示例代码：把 2+3 再乘 4 ——
   Value *Two   = ConstantInt::get(Type::getInt32Ty(Context), 2);
   Value *Three = ConstantInt::get(Type::getInt32Ty(Context), 3);
   Value *Four  = ConstantInt::get(Type::getInt32Ty(Context), 4);   // 新增

   Instruction *Add = BinaryOperator::Create(Instruction::Add, Two, Three,
                                             "addresult");
   Add->insertInto(BB, BB->end());

   // 新增：(2 + 3) * 4
   Instruction *Mul = BinaryOperator::Create(Instruction::Mul, Add, Four,
                                             "mulresult");
   Mul->insertInto(BB, BB->end());

   // 返回值由 Add 改为 Mul
   ReturnInst::Create(Context, Mul)->insertInto(BB, BB->end());
   ```

3. 只构建 ModuleMaker 这一个目标：

   ```bash
   cmake --build build --target ModuleMaker
   ```

4. 运行它，把位码（写到 stdout）重定向到文件，再用 `llvm-dis` 转可读文本：

   ```bash
   ./build/bin/ModuleMaker > main.bc
   ./build/bin/llvm-dis main.bc -o main.ll
   cat main.ll
   ```

**需要观察的现象**：`main.ll` 中 `main` 函数体现两步计算：先 `add` 再 `mul`，最后返回 `mul` 的结果。注意 `BinaryOperator::Create(Instruction::Mul, ...)` 的第一个操作数是上一条 `add` 的结果 `Add`（一个 `Value*`），第二个操作数是常量 `Four`。

**预期结果**（本示例不跑优化，所以指令不会被折叠）：

```llvm
; ModuleID = 'test'
define i32 @main() {
EntryBlock:
  %addresult = add i32 2, 3
  %mulresult = mul i32 %addresult, 4
  ret i32 %mulresult
}
```

> 为什么不是直接 `ret i32 20`？因为 ModuleMaker 只负责 **构造** IR，不运行任何优化 pass。如果你把这段 `.bc` 再喂给 `opt -passes=instcombine`，常量折叠才会把它简化成 `ret i32 20`——这正好印证「构造」与「优化」是两个独立阶段（衔接 u1-l3 中 `opt` 的职责）。

#### 4.3.5 小练习与答案

- **练习**：如果只调用 `BinaryOperator::Create(...)` 而忘了 `insertInto`，程序会怎样？
- **答案**：那条 `add` 指令游离在内存中、不在任何基本块里，因此 `WriteBitcodeToFile` 写出的 IR 里 **不会出现** 这条指令；并且因为返回指令引用了它，行为不可靠。这正说明「create 与 insert 分离」需要使用者自行保证一致性。
- **练习**：把 `Add` 同时作为 `mul` 的两个操作数（即构造 `(2+3)*(2+3)`）会得到什么 IR？
- **答案**：由于两个操作数都指向同一个 `Value*`（`Add`），IR 中会出现 `%mulresult = mul i32 %addresult, %addresult`，两个操作数引用同一条 `%addresult`。LLVM 允许一条指令的多个操作数指向同一个 `Value`。
- **练习**：`WriteBitcodeToFile(*M, outs())` 里的 `*M` 为什么用解引用？
- **答案**：该函数接收的是 `const Module&`（引用），而 `M` 是 `Module*`（指针），所以需要解引用传值（见 [include/llvm/Bitcode/BitcodeWriter.h:132](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/include/llvm/Bitcode/BitcodeWriter.h#L132) 的参数类型）。

## 5. 综合实践

把本讲的三个最小模块串起来，做一个小任务：**让 ModuleMaker 生成一个「读入两个 i32 参数并返回它们的和」的函数，而不是无参的 `main`。**

提示与步骤：

1. 把函数类型从 `int ()` 改成 `int (int, int)`：`FunctionType::get` 的参数列表里加入两个 `i32`（可参考 [include/llvm/IR/DerivedTypes.h:173](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/include/llvm/IR/DerivedTypes.h#L173) 的带参重载，传入 `ArrayRef<Type*>{i32, i32}`）。
2. 给函数换一个名字（如 `"add"`），保留 `ExternalLinkage`。
3. 在入口块里，用函数参数迭代器（`F->arg_begin()` 拿到第一个参数、`+1` 拿到第二个）取得两个形参 `Value*`。
4. 用 `BinaryOperator::Create(Instruction::Add, Arg0, Arg1, "sum")` 构造加法并 `insertInto`。
5. 用 `ReturnInst::Create` 返回 `sum`。

**预期结果**（待本地验证具体字节，但结构确定）：

```llvm
define i32 @add(i32 %0, i32 %1) {
EntryBlock:
  %sum = add i32 %0, %1
  ret i32 %sum
}
```

完成后，你还可以把它喂给 `lli` 之外的方式验证——但注意 `lli` 默认执行 `main`，没有 `main` 时需要指定入口（这部分留到 JIT 单元再讲）。本任务的重点是练手「构造带参数函数 + 用指令计算 + 返回」这条链。

## 6. 本讲小结

- `LLVMContext` 是 IR 对象的容器与类型唯一性的保证；`Module` 是一段 IR 的根，二者先创建，后续一切挂在它们之下。
- 函数创建遵循「先 `FunctionType`、后 `Function::Create`」两步；通过把 `Module*` / `Function*` 作为最后参数，可实现自动挂接。
- LLVM API 的核心模式：**创建指令与插入指令分离**——`BinaryOperator::Create` 只造对象，`Instruction::insertInto` 才把它挂进基本块。
- 每个基本块必须以终结指令收尾，本例用 `ReturnInst::Create`。
- `WriteBitcodeToFile` 把 Module 序列化为 `.bc`；`delete Module` 利用归属模型回收整棵 IR 树。
- ModuleMaker 只 **构造** IR、不优化，因此输出会保留手写的每一条指令；要简化需另行交给 `opt`。

## 7. 下一步学习建议

- **横向巩固**：阅读同目录下的 `examples/Fibonacci/fibonacci.cpp`，它用更高级的 `IRBuilder` 来构造带控制流（循环/递归）的函数，是本讲「手工 `insertInto`」写法的升级版。后续 u2-l3 会专门讲 `IRBuilder`。
- **纵向深入**：进入「LLVM IR 基础」单元（u2），系统学习 IR 的四层结构（u2-l1）、类型系统与 `Value`（u2-l2），以及 IR 的文本与位码格式（u2-l4）。
- **动手延伸**：本讲提到示例没有调用 `verifyModule`；等学完 u2 后，尝试在 ModuleMaker 里加入验证器调用，故意构造一条非法 IR（例如基本块不以终结指令结尾），观察验证器如何报错。

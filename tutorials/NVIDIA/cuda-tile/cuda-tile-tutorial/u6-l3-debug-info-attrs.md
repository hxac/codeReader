# 调试信息属性与位置

## 1. 本讲目标

学完本讲后，你应该能够：

- 理解 `cuda_tile` 方言的**调试信息属性类层级**：`DINodeAttr → DIScopeAttr → DILocalScopeAttr` 这条继承链，以及挂在它上面的五个具体属性（`di_file` / `di_compile_unit` / `di_subprogram` / `di_lexical_block` / `di_loc`）各自描述什么。
- 看懂 `test/Dialect/CudaTile/debuginfo_attr.mlir` 这套测试如何用「文件 → 编译单元 → 子程序 → 词法块 → 带 scope 的位置」把一段源代码的调试信息还原到 IR 上，并能据此亲手给一个 `entry` 内核附加调试信息。
- 理解 `verifyFuncDebugInfo` / `verifyFuncBodyDebugInfo` 这两个校验函数实现的 6 条规则，明白「函数 scope 与函数体内操作 scope 必须一致」这条一致性约束为何重要，以及如何用 `cuda-tile-opt` 触发并阅读相关报错。

## 2. 前置知识

本讲建立在以下已学概念之上（见前置讲义摘要）：

- **属性系统与优化提示（u6-l1）**：`cuda_tile` 方言的属性由 `.td` 声明、`cuda-tile-tblgen` 生成 `.inc` 胶水、`registerAttributes` 注册。本讲的调试信息属性就是这套机制的一个具体应用，只是它们基类不同（派生自 `DINodeAttr` 而非普通 `AttrDef`）。
- **entry 内核与控制流（u5-l4）**：`entry` 是 GPU 内核的容器操作，带一个 body region；`for`/`if` 等控制流会嵌套出多层词法作用域。调试信息要描述的正是「这条 IR 指令来自源程序的哪个函数、哪个嵌套块、哪一行哪一列」。
- **MLIR 的 Location 机制**：每个操作都有一个 `Location` 属性（`op.getLoc()`），最常见的是 `FileLineColLoc`（写作 `loc("/path":line:col)`）。本讲引入的 `DILocAttr` 是一种**自定义 Location 属性**，它在「行号列号」之外额外绑定一个「调试作用域」。

补充两个本讲要用到的 MLIR 基础概念：

- **调试信息（Debug Info, DI）**：编译器在生成机器码的同时，附带产出一份「IR 指令 ↔ 源代码位置」的映射，供调试器（如 `cuda-gdb`）单步执行、设断点、打印变量。CUDA Tile IR 把这份信息建模成一组**属性（Attribute）**，附在操作和函数上。
- **作用域（Scope）**：源程序里名字的可见范围。一个函数是一个作用域（子程序 `di_subprogram`），函数里的 `{ }` 块又是更内层的作用域（词法块 `di_lexical_block`）。作用域可嵌套，形成一棵树；调试器用它来确定「当前停在哪段源代码里」。

## 3. 本讲源码地图

本讲涉及的关键文件及其作用：

| 文件 | 作用 |
| --- | --- |
| [include/cuda_tile/Dialect/CudaTile/IR/Attributes.h](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Attributes.h) | 手写的 C++ 基类 `DINodeAttr` / `DIScopeAttr` / `DILocalScopeAttr`，构成 DI 属性的类层级根 |
| [include/cuda_tile/Dialect/CudaTile/IR/AttrDefs.td](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/AttrDefs.td) | 用 TableGen 声明五个具体 DI 属性（参数、汇编格式、`sinceVersion`） |
| [include/cuda_tile/Dialect/CudaTile/IR/Ops.h](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Ops.h) | 声明两个调试信息校验函数 `verifyFuncDebugInfo` / `verifyFuncBodyDebugInfo` |
| [lib/Dialect/CudaTile/IR/CudaTile.cpp](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/IR/CudaTile.cpp) | `DebugInfoVerifier` 实现 6 条校验规则；`EntryOp` 在 `verify`/`verifyRegions` 里调用它们 |
| [test/Dialect/CudaTile/debuginfo_attr.mlir](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/Dialect/CudaTile/debuginfo_attr.mlir) | 合法用法范例：完整展示文件→编译单元→子程序→词法块→位置的嵌套 |
| [test/Dialect/CudaTile/debuginfo_attr_invalid.mlir](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/Dialect/CudaTile/debuginfo_attr_invalid.mlir) | 非法用例：逐条触发 Rule 1~5 的校验报错 |

## 4. 核心概念与源码讲解

### 4.1 调试信息属性类层级：一棵作用域树

#### 4.1.1 概念说明

要把「源代码位置」记录到 IR 里，CUDA Tile 沿用了 LLVM/MLIR 的 DWARF 风格调式信息建模，把现实世界里的概念一一映射成属性：

| 源代码概念 | 对应 DI 属性 | 说明 |
| --- | --- | --- |
| 一个源文件 | `di_file<"foo.py" in "/tmp/">` | 文件名 + 目录 |
| 一次编译的根 | `di_compile_unit<file = ...>` | 编译单元，整棵作用域树的根 |
| 一个函数 | `di_subprogram<file=..., line=1, name="f", linkageName="f", compileUnit=..., scopeLine=2>` | 子程序，函数级作用域 |
| 函数里的 `{ }` 块 | `di_lexical_block<scope=..., file=..., line=3, column=4>` | 词法块，可层层嵌套 |
| 一条具体指令的位置 | `di_loc<loc("...":7:8) in #scope>` | 带 scope 的位置（Location 属性） |

这些属性不是平铺的，而是组织成一棵**作用域树**：`di_compile_unit` 与 `di_file` 是顶层 scope；`di_subprogram` 挂在编译单元下；`di_lexical_block` 又可挂在子程序或更内层的词法块下。树的「父子关系」通过每个属性的 `scope` 参数表达。

为了在 C++ 里统一操作这些属性，项目定义了一条三层的类层级（基类链）：

```text
mlir::Attribute                ← 所有属性的根（MLIR 内建）
    └── DINodeAttr             ← 所有调试信息属性的根
            └── DIScopeAttr    ← 表示「一个作用域」的 DI 属性
                    └── DILocalScopeAttr  ← 表示「函数内的局部作用域」
```

这条链的意义在于：**类型约束按「能做什么」逐层收紧**。

- `DINodeAttr`：只要是调试信息属性就是它（最宽）。
- `DIScopeAttr`：能当别人的 `scope`（即能做容器）。`di_file`、`di_compile_unit` 是它。
- `DILocalScopeAttr`：是「函数体内」的局部作用域，能被 `di_loc` 直接引用为 scope。`di_subprogram`、`di_lexical_block` 是它。

注意一个关键点：`di_loc` 的 scope 参数类型被声明为 `DILocalScopeAttr`，**而不是更宽的 `DIScopeAttr`**。这意味着你**不能**把一条指令的位置直接挂在 `di_file` 或 `di_compile_unit` 上——指令必须属于某个子程序或词法块。这条约束从类型层就堵住了「全局位置」的写法。

#### 4.1.2 核心流程

从源代码到 IR 调试信息的映射流程：

```text
源文件 foo.py
   │  di_file<"foo.py" in "/tmp/">            （第 1 步：描述文件）
   ▼
编译单元
   │  di_compile_unit<file = #file>           （第 2 步：建根作用域）
   ▼
函数 test_func
   │  di_subprogram<file, line, name, linkageName, compileUnit, scopeLine>
   │                                           （第 3 步：函数作用域）
   ▼
函数体内的 { } 块（可嵌套）
   │  di_lexical_block<scope, file, line, column>
   │                                           （第 4 步：词法块作用域）
   ▼
每条指令
      di_loc<FileLineColLoc in scope>          （第 5 步：带 scope 的位置）
      附在 op 上：  %c = constant ... loc(#di_loc)
```

第 5 步是关键：`di_loc` 把「行号列号」（`FileLineColLoc`）与「作用域」（`DILocalScopeAttr`）打包成一个**自定义 Location 属性**，再挂到具体操作上。这样调试器既能知道「停在第几行」，又能知道「当前在哪个函数/块的词法环境里」。

#### 4.1.3 源码精读

**手写基类链**。三个基类在 `Attributes.h` 里手写（不走 TableGen），目的是给生成的具体属性提供一个公共父类与 RTTI 入口（`classof`）：

[include/cuda_tile/Dialect/CudaTile/IR/Attributes.h:25-44](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Attributes.h#L25-L44) 定义了 `DINodeAttr`（所有 DI 属性根）、`DIScopeAttr`（作用域）、`DILocalScopeAttr`（局部作用域）三层基类，逐层 `public` 继承并各自提供 `classof`，用于 `isa<>` / `dyn_cast<>` 类型判断。

**TableGen 包装基类**。`.td` 侧有一组对应的包装类，让具体属性声明时只需指明「我挂在哪一层」。包装类把 `baseCppClass` 透传下去：

[include/cuda_tile/Dialect/CudaTile/IR/AttrDefs.td:534-556](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/AttrDefs.td#L534-L556) 定义 `CudaTile_DIAttr` 通用包装（带 `mnemonic` 与 `sinceVersion`），再派生出 `CudaTile_DINodeAttr` / `CudaTile_DIScopeAttr` / `CudaTile_DILocalScopeAttr` 三个分组基类；具体属性 `def` 时只要继承对应基类，生成的 C++ 类就会自动继承到手写的那条基类链。

**带 scope 的位置 `DILocAttr`**。注意它不是普通 `AttrDef`，而是 `LocationAttrDef`——即它本身就是一个 MLIR Location，可直接放进 `op.getLoc()`：

[include/cuda_tile/Dialect/CudaTile/IR/AttrDefs.td:562-579](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/AttrDefs.td#L562-L579) 声明 `di_loc`，两个参数：`FileLineColLoc sourceLoc`（行号列号）与 `DILocalScopeAttr scope`（局部作用域），汇编格式 `` `<` $sourceLoc `in` $scope `>` ``。scope 类型限定为 `DILocalScopeAttr`，从类型层禁止把指令直接挂在文件/编译单元上。

**两个顶层 scope**。`di_file` 与 `di_compile_unit` 都继承 `CudaTile_DIScopeAttr`（不是 Local）：

[include/cuda_tile/Dialect/CudaTile/IR/AttrDefs.td:585-612](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/AttrDefs.td#L585-L612) 定义 `di_compile_unit`（仅一个 `file` 参数）与 `di_file`（`name` + `directory` 两个 `StringAttr`），二者均为 `DIScopeAttr`，可作 scope 但**不能**直接作 `di_loc` 的 scope。

**两个局部 scope**。`di_subprogram` 与 `di_lexical_block` 都继承 `CudaTile_DILocalScopeAttr`：

[include/cuda_tile/Dialect/CudaTile/IR/AttrDefs.td:618-669](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/AttrDefs.td#L618-L669) 定义 `di_lexical_block`（参数 `scope`/`file`/`line`，`column` 可选）与 `di_subprogram`（参数 `file`/`line`/`name`/`linkageName`/`compileUnit`，`scopeLine` 可选）。`di_lexical_block` 的 `scope` 是 `DILocalScopeAttr`，因此词法块可以套在子程序里，也可以套在更内层的词法块里，形成任意深度嵌套。`di_subprogram` 还提供了 `cloneWithNewLinkageName` 方法，用于在校正符号名时复制出一个仅 linkage 名不同的副本。

#### 4.1.4 代码实践

**实践目标**：亲手用文本 MLIR 搭出一棵最小作用域树，验证它被 `cuda-tile-opt` 接受。

**操作步骤**：

1. 在项目根目录创建 `my_debug.mlir`，写入以下内容（只造属性，先不写内核）：

   ```mlir
   #file = #cuda_tile.di_file<"my_kernel.py" in "/home/me/">
   #cu   = #cuda_tile.di_compile_unit<file = #file>
   #sub  = #cuda_tile.di_subprogram<file = #file, line = 1, name = "add", linkageName = "add", compileUnit = #cu, scopeLine = 1>
   #blk  = #cuda_tile.di_lexical_block<scope = #sub, file = #file, line = 5, column = 2>
   #loc_sub = #cuda_tile.di_loc<loc("/home/me/my_kernel.py":3:4) in #sub>
   #loc_blk = #cuda_tile.di_loc<loc("/home/me/my_kernel.py":6:7) in #blk>
   ```

2. 运行（构建产物里）：

   ```bash
   cuda-tile-opt --mlir-print-debuginfo my_debug.mlir
   ```

**需要观察的现象**：命令应正常退出（无报错），并把 6 个属性原样回显。注意 `--mlir-print-debuginfo` 是关键：不加它时 MLIR 默认会**抑制** Location 的打印，调试信息不会显示。

**预期结果**：终端输出与你写入的属性一致。随后可做一个反向小实验：把 `#loc_sub` 的 `in #sub` 改成 `in #file`（即把指令位置挂到文件作用域上），重新运行，应得到类型错误——因为 `di_loc` 的 scope 只接受 `DILocalScopeAttr`，`di_file` 是 `DIScopeAttr` 但不是 Local。**待本地验证**（具体报错文案以本地构建为准）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `di_file` 和 `di_compile_unit` 不能直接作为 `di_loc` 的 `scope`？

**参考答案**：因为 `di_loc` 的 `scope` 参数被声明为 `DILocalScopeAttr`，而 `di_file`/`di_compile_unit` 只继承到 `DIScopeAttr` 这一层，不是 `DILocalScopeAttr`。一条源指令必然落在某个函数（子程序）或其内部的词法块里，不存在「直接属于文件」的指令，所以从类型层就排除了这种写法。

**练习 2**：`di_lexical_block` 的 `scope` 参数类型是 `DILocalScopeAttr` 而非 `DIScopeAttr`，这允许了什么、又禁止了什么？

**参考答案**：允许词法块的父作用域是另一个词法块或子程序（二者都是 `DILocalScopeAttr`），从而支持任意深度的 `{ { {} } }` 嵌套；禁止把词法块直接挂在 `di_file`/`di_compile_unit` 下，即词法块必须最终归属于某个子程序。

---

### 4.2 debuginfo_attr.mlir：把调试信息附到 entry 内核上

#### 4.2.1 概念说明

上一节我们只造了「孤立的属性」。真实场景里，这些属性要附在真实的 IR 上：函数（`entry`）的位置由它末尾的 `loc(...)` 给出，函数体内每条操作的位置由各自的 `loc(...)` 给出。`test/Dialect/CudaTile/debuginfo_attr.mlir` 就是官方给出的「合法范例」，它演示了：

1. 如何声明一整套 DI 属性（文件、编译单元、两个子程序、多层词法块、多个 `di_loc`）。
2. 如何把 `di_loc` 挂到 `entry` 操作本身（函数级位置）和它体内的 `constant` 操作（指令级位置）。
3. 关键细节：**嵌套词法块的 `di_loc` 必须层层引用**——内层块的 `di_loc` 引用内层块属性，内层块属性的 `scope` 又指向更外层块，最终回到子程序。

#### 4.2.2 核心流程

[test/Dialect/CudaTile/debuginfo_attr.mlir:1-109](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/Dialect/CudaTile/debuginfo_attr.mlir#L1-L109) 的整体结构：

```text
声明阶段（属性定义，第 3-82 行）
  #file, #compile_unit                 ← 顶层 scope
  #func, #entry                         ← 两个子程序（Local scope）
  #block_func, #inner_block_func        ← 套在 #func 下的两层词法块
  #block_entry, #inner_block_entry      ← 套在 #entry 下的两层词法块
  #di_loc_func, #di_loc_block_func, #di_loc_inner_block_func   ← 三层位置
使用阶段（附到操作上，第 84-108 行）
  cuda_tile.module @kernels {
    entry @test_func() {
      %c1 = constant ... loc(#di_loc_func)              ← 挂在子程序作用域
      %c2 = constant ... loc(#di_loc_block_func)        ← 挂在第一层词法块
      %c3 = constant ... loc(#di_loc_inner_block_func)  ← 挂在第二层词法块
      return loc(unknown)
    } loc(#di_loc_func)                                  ← entry 自身的位置
  }
```

最值得注意的对应关系：`#inner_block_func` 的 `scope = #block_func`，而 `#block_func` 的 `scope = #func`，形成 `inner_block → block → subprogram` 的作用域链。`%c3` 的位置 `#di_loc_inner_block_func` 指向 `#inner_block_func`，于是调试器从 `%c3` 出发能一路回溯到它所属的函数 `test_func`。

#### 4.2.3 源码精读

**声明编译单元与子程序**：

[test/Dialect/CudaTile/debuginfo_attr.mlir:3-29](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/Dialect/CudaTile/debuginfo_attr.mlir#L3-L29) 声明 `#file`、`#compile_unit`、以及两个子程序 `#func`（`test_func`）与 `#entry`（`test_entry`）。注意 `di_subprogram` 用的是 `struct(params)` 汇编格式，参数**顺序无关**但**必填项缺一不可**。

**声明嵌套词法块与位置**：

[test/Dialect/CudaTile/debuginfo_attr.mlir:31-82](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/Dialect/CudaTile/debuginfo_attr.mlir#L31-L82) 分别为两个子程序各搭两层词法块（`#block_func`/`#inner_block_func` 与 `#block_entry`/`#inner_block_entry`），并为「子程序/一层块/二层块」三种作用域各造一个 `di_loc`。这里可以清楚看到 `di_loc` 的 `in` 后面跟的是 `DILocalScopeAttr`（子程序或词法块），而 `di_loc` 内层的 `loc("...":l:c)` 才是行号列号。

**把位置附到真实内核上**：

[test/Dialect/CudaTile/debuginfo_attr.mlir:90-95](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/Dialect/CudaTile/debuginfo_attr.mlir#L90-L95) 展示 `entry @test_func()`：三条 `constant` 分别带 `#di_loc_func`、`#di_loc_block_func`、`#di_loc_inner_block_func` 三个位置，`entry` 自身的位置写作 `} loc(#di_loc_func)`。`return` 用 `loc(unknown)`——返回操作没有有意义的源位置时可用 `unknown`。

**FileCheck 校验回显**。文件开头的 `// CHECK-DAG` 行用 FileCheck 验证 `--mlir-print-debuginfo` 输出与输入一致，这是 round-trip（往返）测试的标准写法：

[test/Dialect/CudaTile/debuginfo_attr.mlir:1](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/Dialect/CudaTile/debuginfo_attr.mlir#L1) 的 `RUN: cuda-tile-opt --mlir-print-debuginfo %s | FileCheck %s` 表明：解析后再打印，DI 属性应**逐字保持**，这正是 4.1 节类层级与 `struct(params)` 解析/打印正确性的回归保护。

#### 4.2.4 代码实践

**实践目标**：仿照官方范例，给一个真正做计算的 `entry` 内核附加完整调试信息并通过校验。

**操作步骤**：

1. 创建 `my_kernel_debug.mlir`：

   ```mlir
   #file = #cuda_tile.di_file<"add.py" in "/tmp/">
   #cu  = #cuda_tile.di_compile_unit<file = #file>
   #sub = #cuda_tile.di_subprogram<file = #file, line = 1, name = "add", linkageName = "add", compileUnit = #cu, scopeLine = 1>
   #di_loc = #cuda_tile.di_loc<loc("/tmp/add.py":2:3) in #sub>

   cuda_tile.module @kernels {
     entry @add() {
       %a = constant <i32: 1> : !cuda_tile.tile<i32> loc(#di_loc)
       %b = constant <i32: 2> : !cuda_tile.tile<i32> loc(#di_loc)
       return loc(unknown)
     } loc(#di_loc)
   }
   ```

   注意三个一致性：`entry` 的符号名 `@add` == `di_subprogram` 的 `linkageName = "add"`；`entry` 自身的 `loc(#di_loc)` 的 scope 是 `#sub`；体内操作的 `loc` 也指向同一个 `#sub`。（这三点正是下一节校验规则要查的。）

2. 运行：

   ```bash
   cuda-tile-opt --mlir-print-debuginfo my_kernel_debug.mlir
   ```

**需要观察的现象**：无报错，IR 原样回显，`constant` 与 `entry` 都带上了 `loc(#cuda_tile.di_loc<...>)`。

**预期结果**：校验通过。若漏掉 `entry` 自身的 `loc(#di_loc)`（函数无 scope），但体内操作仍有 scope，则会触发下一节的 Rule 3 报错——这正是实践 4.3 要故意制造的。

#### 4.2.5 小练习与答案

**练习 1**：在 `debuginfo_attr.mlir` 里，`%c3` 的位置 `#di_loc_inner_block_func` 如何让调试器知道它属于函数 `test_func`？

**参考答案**：`#di_loc_inner_block_func` 的 scope 是 `#inner_block_func`；`#inner_block_func` 的 scope 是 `#block_func`；`#block_func` 的 scope 是 `#func`（即 `test_func` 的子程序）。沿 `scope` 链层层回溯即到达所属函数。

**练习 2**：为什么范例里 `return` 用 `loc(unknown)` 而不给它一个 `di_loc`？

**参考答案**：`return` 是编译器生成的隐式收尾操作，没有对应的源代码行；用 `loc(unknown)` 表示「无有意义的位置」是 MLIR 的常见做法，且不会违反调试信息校验（校验只对「确实带了 scope」的位置查一致性）。

---

### 4.3 函数调试信息校验：6 条一致性规则

#### 4.3.1 概念说明

自由地声明 DI 属性并不难，难的是保证「函数级调试信息」与「函数体内指令级调试信息」**前后一致**。如果不一致，调试器会跳到错误的源文件、错误的函数，甚至崩溃。CUDA Tile 用一个 `DebugInfoVerifier` 在编译期强制 6 条规则：

| 规则 | 含义 | 触发场景 |
| --- | --- | --- |
| Rule 1 | 函数若有 scope，其 scope **必须是子程序**（`di_subprogram`） | 把函数位置挂到词法块上 |
| Rule 2 | 函数名必须等于其子程序 scope 的 `linkageName` | `entry @foo` 配 `linkageName = "test"` |
| Rule 3 | 函数若无 scope，其体内操作**也不得**有 scope | 函数用 `loc(unknown)` 但体内操作带 `di_loc` |
| Rule 4 | 体内操作的 scope 必须**与函数 scope 同属一个子程序** | 操作的 scope 指向另一个函数的子程序 |
| Rule 5 | 全局变量（非函数操作）**不得**有 scope | 给 module 顶层操作挂 `di_loc` |
| Rule 6 | 函数位置不得是 `CallSiteLoc` | `entry ... loc(callsite(...))` |

其中 Rule 3、4 最常被前端踩坑，它们共同表达一条核心原则：**调试信息要么整函数都没有，要么整函数都有且归属于同一个子程序**。

> 一个易混点：Rule 4 说「操作的 scope 必须匹配函数 scope」，但操作的 `di_loc` 可以指向**词法块**而非直接指向子程序。校验器会先把词法块沿 `scope` 链**归约到它所属的子程序**，再与函数的子程序比较。因此「操作在词法块里」是合法的，只要这个词法块最终挂在当前函数的子程序下。

#### 4.3.2 核心流程

校验分两个阶段，对应两个函数，分别挂在 `entry` 的两个不同校验钩子上：

```text
EntryOp::verify()          ← 校验函数自身（Rule 1、2、6）
    └─ verifyFuncDebugInfo(funcOp)
         · 取出 func.getLoc()，用 getDILoc() 剥离 Location 包装
         · Rule 6：Location 直接是 CallSiteLoc → 报错
         · 若剥出了 DILoc：
              Rule 1：其 scope 必须是 DISubprogramAttr
              Rule 2：subprogram.getLinkageName() == func.getName()

EntryOp::verifyRegions()   ← 校验函数体内所有操作（Rule 3、4）
    └─ verifyFuncBodyDebugInfo(funcOp)
         · 先求出函数的子程序 fnSubprogram（同样剥离+归约）
         · walk 遍历体内每个 op（含控制流嵌套里的 op）：
              求出 op 的子程序 opSubprogram（getDILoc + getSubprogram）
              若 opSubprogram 非空：
                   Rule 3：fnSubprogram 为空 → 报错（函数无 scope 却有操作带 scope）
                   Rule 4：fnSubprogram != opSubprogram → 报错（操作归属别的子程序）

（模块级 Rule 5 由 verifyModule 单独处理 module 顶层非函数操作）
```

两个关键辅助函数让校验足够鲁棒：

- `getDILoc(loc)`：MLIR 的 Location 可以层层包装（`NameLoc`、`FusedLoc`、`CallSiteLoc`、`OpaqueLoc`），这个函数**递归剥壳**，直到找到内层的 `DILocAttr` 或确认没有。这样无论前端怎么包 Location，校验都能掏出真正的 DI 位置。
- `getSubprogram(scope)`：把一个 `DILocalScopeAttr` **沿 scope 链归约**到它所属的 `DISubprogramAttr`（若是 `DILexicalBlock` 就取其 `scope` 再递归）。这是 Rule 4 能接受「操作在词法块里」的原因。

#### 4.3.3 源码精读

**两个校验函数的声明**：

[include/cuda_tile/Dialect/CudaTile/IR/Ops.h:44-46](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Ops.h#L44-L46) 在 `impl` 命名空间声明 `verifyFuncDebugInfo` 与 `verifyFuncBodyDebugInfo`，入参是 `FunctionOpInterface`（`entry` 实现了该接口），返回 `LogicalResult`。

**6 条规则的注释总览**：

[lib/Dialect/CudaTile/IR/CudaTile.cpp:1130-1139](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/IR/CudaTile.cpp#L1130-L1139) 在 `DebugInfoVerifier` 类前用注释列全 6 条规则，是理解整个校验逻辑的最佳入口。

**Rule 1、2、6（函数自身）**：

[lib/Dialect/CudaTile/IR/CudaTile.cpp:1143-1169](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/IR/CudaTile.cpp#L1143-L1169) 是 `verifyFunc`：先查 Rule 6（`isa<CallSiteLoc>` 直接报错），再用 `getDILoc` 剥出 DI 位置，若存在则要求其 scope 是 `DISubprogramAttr`（Rule 1），且 `subprogram.getLinkageName() == func.getName()`（Rule 2）。注意 Rule 2 比较的是**linkageName**与函数符号名——这就是 4.2 实践里强调「`entry @add` 必须配 `linkageName = "add"`」的根源。

**Rule 3、4（函数体）**：

[lib/Dialect/CudaTile/IR/CudaTile.cpp:1172-1208](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/IR/CudaTile.cpp#L1172-L1208) 是 `verifyFuncBody`：先求函数子程序 `fnSubprogram`，再 `func.walk([&](Operation *op){...})` 遍历体内每个操作（`walk` 会自动下钻进 `for`/`if` 等嵌套 region）。对每个带 scope 的操作，分别查 Rule 3（函数无 scope 却有操作带 scope）与 Rule 4（子程序不匹配），命中即 `emitOpError` 并 `interrupt()` 中止遍历。

**Rule 5（模块级全局变量）**：

[lib/Dialect/CudaTile/IR/CudaTile.cpp:1211-1220](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/IR/CudaTile.cpp#L1211-L1220) 是 `verifyModule`：遍历 module 下所有**非函数**操作（即全局变量等），若其位置剥出了 DI 位置就报错——全局变量不属于任何子程序，自然不能带函数式 scope。

**归约辅助：从局部 scope 找到子程序**：

[lib/Dialect/CudaTile/IR/CudaTile.cpp:1224-1231](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/IR/CudaTile.cpp#L1224-L1231) 是 `getSubprogram`：用 `TypeSwitch` 分派——若是 `DISubprogramAttr` 直接返回，若是 `DILexicalBlockAttr` 则取其 `getScope()` 递归，否则返回空。这正是 Rule 4 能容忍「操作 scope 指向词法块」的实现。

**剥壳辅助：从任意 Location 找到 DI 位置**：

[lib/Dialect/CudaTile/IR/CudaTile.cpp:1234-1251](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/IR/CudaTile.cpp#L1234-L1251) 是 `getDILoc`：用 `TypeSwitch` 分派各种 Location 包装——`DILocAttr` 直接返回，`CallSiteLoc` 取 `getCaller()` 递归，`FusedLoc` 遍历子位置取第一个非空的，`NameLoc` 取 `getChildLoc()`，`OpaqueLoc` 取 `getFallbackLocation()`，其余返回空。这保证校验对任意嵌套的 Location 包装都鲁棒。

**包装与挂载点**：

[lib/Dialect/CudaTile/IR/CudaTile.cpp:1254-1261](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/IR/CudaTile.cpp#L1254-L1261) 把两个静态方法包装成 `impl::verifyFuncDebugInfo` / `verifyFuncBodyDebugInfo`。

[lib/Dialect/CudaTile/IR/CudaTile.cpp:2762-2782](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/IR/CudaTile.cpp#L2762-L2782) 是挂载点：`EntryOp::verify()` 在做完结果数与参数类型检查后调用 `verifyFuncDebugInfo(*this)`（函数自身），`EntryOp::verifyRegions()` 调用 `verifyFuncBodyDebugInfo(*this)`（函数体）。之所以分两个钩子，是因为 MLIR 的 `verify()` 校验操作自身、`verifyRegions()` 校验其 region 内容，二者时机不同。

#### 4.3.4 代码实践

**实践目标**：参照 `debuginfo_attr_invalid.mlir`，亲手触发 Rule 2 与 Rule 4 的报错，并对照源码读懂错误信息。

**操作步骤**：

1. **触发 Rule 2（函数名与 linkageName 不匹配）**。创建 `bad_rule2.mlir`：

   ```mlir
   #file = #cuda_tile.di_file<"foo.py" in "/tmp/">
   #cu  = #cuda_tile.di_compile_unit<file = #file>
   #sub = #cuda_tile.di_subprogram<file = #file, line = 1, name = "test", linkageName = "test", compileUnit = #cu, scopeLine = 2>
   #di_loc = #cuda_tile.di_loc<loc("/tmp/foo.py":7:8) in #sub>

   cuda_tile.module @kernels {
     entry @foo() {            // ← 符号名是 foo，但 subprogram 的 linkageName 是 test
       return loc(#di_loc)
     } loc(#di_loc)
   }
   ```

   运行：

   ```bash
   cuda-tile-opt --mlir-print-debuginfo bad_rule2.mlir
   ```

2. **触发 Rule 4（操作 scope 与函数 scope 不匹配）**。创建 `bad_rule4.mlir`：

   ```mlir
   #file = #cuda_tile.di_file<"foo.py" in "/tmp/">
   #cu   = #cuda_tile.di_compile_unit<file = #file>
   #test = #cuda_tile.di_subprogram<file = #file, line = 1, name = "test", linkageName = "test", compileUnit = #cu, scopeLine = 2>
   #inv  = #cuda_tile.di_subprogram<file = #file, line = 13, name = "invalid", linkageName = "invalid", compileUnit = #cu, scopeLine = 14>
   #di_loc_func     = #cuda_tile.di_loc<loc("/tmp/foo.py":7:8) in #test>
   #di_loc_invalid  = #cuda_tile.di_loc<loc("/tmp/foo.py":15:16) in #inv>

   cuda_tile.module @kernels {
     entry @invalid() {                  // ← 函数 scope 是 #inv（linkageName=invalid，匹配）
       return loc(#di_loc_func)          // ← 但操作 scope 指向 #test，不属于 #inv
     } loc(#di_loc_invalid)
   }
   ```

   运行：

   ```bash
   cuda-tile-opt --mlir-print-debuginfo bad_rule4.mlir
   ```

**需要观察的现象**：

- `bad_rule2.mlir` 应报 `invalid function debug info scope ... Function name "foo" does not match subprogram scope linkage name "test"`，这正是 [lib/Dialect/CudaTile/IR/CudaTile.cpp:1161-1166](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/IR/CudaTile.cpp#L1161-L1166) 发出的错误。
- `bad_rule4.mlir` 应报 `invalid operation debug info scope ... Operation debug info scope does not match function debug info scope`，对应 [lib/Dialect/CudaTile/IR/CudaTile.cpp:1196-1202](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/IR/CudaTile.cpp#L1196-L1202)。

**预期结果**：两个文件都应**校验失败并退出非零**，错误信息与上述吻合。可与官方用例 [test/Dialect/CudaTile/debuginfo_attr_invalid.mlir:51-59](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/Dialect/CudaTile/debuginfo_attr_invalid.mlir#L51-L59)（Rule 2）和 [test/Dialect/CudaTile/debuginfo_attr_invalid.mlir:157-165](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/Dialect/CudaTile/debuginfo_attr_invalid.mlir#L157-L165)（Rule 4）对照。**待本地验证**（具体退出码与文案以本地构建为准）。

#### 4.3.5 小练习与答案

**练习 1**：如果 `entry` 函数自己不带任何 DI 位置（用 `loc(unknown)`），但体内某条操作带了 `di_loc`，会触发哪条规则？为什么这条约束是必要的？

**参考答案**：触发 Rule 3（「Operation has debug info scope, but function debug info scope is undefined」）。必要性在于：调试器从一条指令回溯作用域时，最终要落在「所属函数」上；若函数本身没有子程序 scope，体内指令的 scope 就成了无源之水，调试器无法确定它属于哪个函数，因此必须禁止这种「半截」调试信息。

**练习 2**：`getSubprogram` 为什么要把 `DILexicalBlockAttr` 递归到其 `scope`，而不是直接报错？

**参考答案**：因为操作的 `di_loc` 合法地可以指向**词法块**（指令在 `{ }` 块里）。校验 Rule 4 要比较的是「操作所属的子程序」与「函数的子程序」，所以需要先把词法块沿 scope 链归约到子程序再比较；若直接报错，就会把「操作在词法块里」这种完全正常的写法误判为非法。

**练习 3**：为什么 Rule 6 单独禁止函数位置是 `CallSiteLoc`？

**参考答案**：`CallSiteLoc` 表达的是「这个调用发生在某处」，语义上属于**调用点**而非**函数定义**。函数定义的位置应当是一个确定的源位置（带子程序 scope 的 `di_loc`），把调用点位置当作函数定义位置会造成语义混淆，因此校验器在函数级别直接拒绝它（见 `verifyFunc` 开头的 `isa<CallSiteLoc>` 检查）。

## 5. 综合实践

把本讲三个模块串起来，完成一个「带完整、且故意制造一处可修复错误的调试信息」的小内核：

1. **构造合法基线**。仿照 4.2，写一个 `entry @vecadd` 内核：声明 `#file`/`#cu`/`#sub`（`linkageName = "vecadd"`）与一层 `#block` 词法块；为子程序和词法块各造一个 `#di_loc`；体内放两条 `constant`（分别带子程序位置和词法块位置），`entry` 自身带子程序位置。用 `cuda-tile-opt --mlir-print-debuginfo` 确认通过。

2. **故意触发并修复 Rule 4**。复制出第二份文件，把函数的子程序换成另一个 `linkageName = "other"` 的子程序（同时把 `entry` 改名 `@other` 以满足 Rule 2），但**保持体内操作的位置仍指向原来的 `#sub`**。运行确认它触发 Rule 4 报错；然后把体内操作的位置改指向挂在 `#other` 下的词法块，重新运行确认通过。这一步让你亲手体验 `getSubprogram` 归约与「整函数同属一个子程序」原则。

3. **读懂错误与源码的对应**。对步骤 2 触发的报错，回到 [lib/Dialect/CudaTile/IR/CudaTile.cpp:1196-1202](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/IR/CudaTile.cpp#L1196-L1202)，确认错误文案、触发的 `emitError` 与 `WalkResult::interrupt()` 的中止行为与你的观察察一致。

完成上述三步后，你应能独立地为任意 CUDA Tile 内核附加合法的调试信息，并诊断前端 lowering 产出的非法调试信息。

## 6. 本讲小结

- CUDA Tile 用一组 DI 属性建模调试信息，类层级为 `DINodeAttr → DIScopeAttr → DILocalScopeAttr`：`di_file`/`di_compile_unit` 是顶层 scope，`di_subprogram`/`di_lexical_block` 是可被 `di_loc` 直接引用的局部 scope，`di_loc` 是把「行号列号 + 局部 scope」打包的自定义 Location 属性。
- `di_loc` 的 scope 类型限定为 `DILocalScopeAttr`，从类型层就禁止把指令直接挂在文件/编译单元上；词法块可任意嵌套，形成一棵以子程序为根的作用域树。
- `debuginfo_attr.mlir` 是官方合法范例，演示了文件→编译单元→子程序→（多层）词法块→位置的完整声明，以及如何把 `di_loc` 附到 `entry` 自身和体内操作上；`--mlir-print-debuginfo` 是查看 DI 信息的必备开关。
- `DebugInfoVerifier` 用 6 条规则保证一致性，核心是 Rule 2（函数名 == 子程序 linkageName）、Rule 3（函数无 scope 则操作也不得有）、Rule 4（操作 scope 经词法块归约后须与函数 scope 同属一个子程序）。
- 两个辅助函数让校验鲁棒：`getDILoc` 递归剥离 `NameLoc`/`FusedLoc`/`CallSiteLoc`/`OpaqueLoc` 包装，`getSubprogram` 沿 scope 链把词法块归约到子程序。
- 校验分两阶段挂载：`EntryOp::verify()` 调 `verifyFuncDebugInfo`（函数自身，Rule 1/2/6），`EntryOp::verifyRegions()` 调 `verifyFuncBodyDebugInfo`（函数体，Rule 3/4），Rule 5（全局变量不得有 scope）由模块级 `verifyModule` 处理。

## 7. 下一步学习建议

- **向字节码深入**：本讲的 DI 属性都带 `sinceVersion = "13.1"`，它们在字节码里有专门的 **Debug 段**。建议进入第 7 单元（专家·字节码二进制格式），重点读 u7-l2（字节码写入器）与 u7-l3（读取器），看 `DebugInfoWriter`/`DebugInfoReader` 如何把这些属性序列化/反序列化，以及惰性构建作用域表的实现。
- **向变换深入**：调试信息会被优化 Pass 改动。学完本讲后可直接读 u9-l4（调试信息合成与规范化），看 `SynthesizeDebugInfoScopesPass` 如何为缺乏调试信息的前端**兜底合成** `DIScope`，以及 `cloneWithNewLinkageName` 这类方法在符号重命名变换中如何保持调试信息一致。
- **继续阅读源码**：若想了解前端如何批量生成这些属性，可跟踪 `lib/Dialect/CudaTile/IR/Attributes.cpp` 中基类的 `classof` 实现，以及 `DISubprogramAttr::cloneWithNewLinkageName` 的真实调用点（grep `cloneWithNewLinkageName`）。

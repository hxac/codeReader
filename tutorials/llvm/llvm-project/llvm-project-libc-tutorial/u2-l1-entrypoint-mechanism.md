# 入口点（entrypoint）机制

## 1. 本讲目标

本讲是进入 LLVM-libc「内部运作」的第一把钥匙。学完后你应当能够：

- 说清楚 **entrypoint（入口点）** 到底是什么，它和「一个函数」是什么关系。
- 解释 LLVM-libc 为什么不采用传统 libc 的「单体归档（monolithic archive）」做法，而要把每个函数做成一个**独立的构建单元**。
- 复述一个入口点的**三阶段生命周期**：实现 → CMake 注册 → 平台配置。
- 区分两条核心 CMake 规则：`add_entrypoint_object`（造单个目标文件）与 `add_entrypoint_library`（把目标文件聚合成静态库）。
- 理解平台配置文件 `entrypoints.txt` 是如何通过 **SKIP 机制**决定一个入口点「编不编译」的。

本讲是单元 2 的总纲。后续讲义（实现规范、CMake 规则详解、平台配置）都会围绕「入口点」这个中心抽象展开。

## 2. 前置知识

本讲承接入门层的认知，默认你已经了解（若不熟悉，请先回顾对应讲义）：

- **函数的「五件套」**（u1-l5）：以 `isalpha` 为例，一个公开函数由 `yaml` 规范、内部头 `.h`、实现 `.cpp`、`CMakeLists.txt` 注册、单元测试五部分协作。本讲把视角从「一个函数」抬升到「所有函数共有的构建抽象」。
- **Full / Overlay 两种构建模式**（u1-l4）：Full 产出 `libc.a`/`libm.a`，Overlay 产出 `libllvmlibc.a`。本讲会解释为什么入口点粒度能让同一个实现同时服务这两种模式。
- **目录组织约定**（u1-l2）：`src/<头文件名>/<函数>.cpp`，入口点注册名是目录路径的点分形式（如 `libc.src.ctype.isalpha`）。

几个通俗概念先铺垫：

- **构建单元（build unit）**：编译器能单独编译、链接器能单独取舍的最小单位。传统 libc 里，一个 `.c` 文件常常塞进几十个函数，整体编进一个 `.a`；入口点则让「一个函数 ≈ 一个构建单元」。
- **配置驱动（configuration-driven）**：同一个函数的实现代码在仓库里只有一份，但「这次构建要不要把它编进产物」由配置文件决定，而不是由代码本身决定。

## 3. 本讲源码地图

本讲围绕「入口点」这一抽象展开，涉及的关键文件如下：

| 文件 | 角色 |
| --- | --- |
| [docs/dev/entrypoints.md](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/docs/dev/entrypoints.md) | 官方技术参考，定义入口点概念、生命周期与三条 CMake 规则。本讲的主要依据。 |
| [cmake/modules/LLVMLibCObjectRules.cmake](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/cmake/modules/LLVMLibCObjectRules.cmake) | 定义 `add_entrypoint_object` / `create_entrypoint_object`，是「造单个目标文件」规则的真实实现，也是 SKIP 机制的所在地。 |
| [cmake/modules/LLVMLibCLibraryRules.cmake](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/cmake/modules/LLVMLibCLibraryRules.cmake) | 定义 `add_entrypoint_library`，负责把入口点对象聚合成静态库（`libc.a` 等）。 |
| [lib/CMakeLists.txt](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/lib/CMakeLists.txt) | 在构建末端调用 `add_entrypoint_library` 产出 `libc.a` / `libm.a` / `libllvmlibc.a`。 |
| [CMakeLists.txt](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/CMakeLists.txt) | 加载 `entrypoints.txt`、推导出 `TARGET_ENTRYPOINT_NAME_LIST`，把「配置」喂给 SKIP 机制。 |
| [config/linux/x86_64/entrypoints.txt](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/config/linux/x86_64/entrypoints.txt) | Linux/x86_64 平台「支持哪些入口点」的事实来源。 |
| [src/ctype/CMakeLists.txt](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/src/ctype/CMakeLists.txt) | 一个「普通」入口点（`isalpha` 等）的注册范例。 |
| [src/setjmp/CMakeLists.txt](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/src/setjmp/CMakeLists.txt) | 「别名（ALIAS）」型入口点的真实范例：`setjmp`/`longjmp` 转发到架构特化实现。 |

## 4. 核心概念与源码讲解

本讲拆成四个最小模块，对应入口点的四个侧面：**定义 → 生命周期 → CMake 规则 → 配置入口**。

### 4.1 什么是入口点（entrypoint 定义）

#### 4.1.1 概念说明

在传统 C 库里，所有函数被打包进一个「大黑箱」式的静态归档（`libc.a` 里塞了上千个符号）。LLVM-libc 把这件事彻底改写：**每一个对外公开的函数或全局变量，都被当作一个独立的、有名有姓的「入口点（entrypoint）」**。

官方文档开篇就点明，入口点是 LLVM-libc 的「中心抽象」：

> A public function or a global variable provided by LLVM-libc is called an *entrypoint*. The notion of entrypoints is central to LLVM-libc's source layout, build system, and configuration management.

也就是说，`malloc`、`printf`、`isalpha`、`errno`（全局变量）各自都是一个 entrypoint。它不仅是「一个函数」，更是一个**贯穿源码布局、构建系统、配置管理三者的统一坐标**。

为什么要这么做？文档列了三个直接收益：

- **粒度化构建目标（Granular build targets）**：你可以只构建你真正需要的那几个对象，而不必拖上整个 `libc.a`。
- **配置驱动的选择（Configuration-driven selection）**：不同 OS / 架构可以为「同名函数」挑选不同的实现。
- **支持多种构建模式**：在 Overlay 模式下只替换宿主 libc 的少数函数，或在 Full 模式下构建完整库。

一句话直觉：**传统 libc 把「函数」当作归档里的符号；LLVM-libc 把「函数」当作一个一等公民的构建对象**，于是「编不编它、用哪份实现、放进哪个库」都变成可独立回答的问题。

#### 4.1.2 核心流程

入口点的「独立性」体现在三个正交关注点被解耦：

```text
       ┌─────────────────────────────────────────────┐
       │              一个 entrypoint                │
       ├─────────────────────────────────────────────┤
       │ 关注点 A：实现      （HOW：算法写在哪）       │
       │ 关注点 B：构建目标  （HOW：怎么编成 .o）      │
       │ 关注点 C：平台取舍  （WHETHER：要不要进产物） │
       └─────────────────────────────────────────────┘
```

- 关注点 A 由 `src/` 下的 `.cpp` / `.h` 回答。
- 关注点 B 由函数目录下的 `CMakeLists.txt`（`add_entrypoint_object`）回答。
- 关注点 C 由 `config/<os>/<arch>/entrypoints.txt` 回答。

这三者**互相独立**：一份实现可以同时被 Full 和 Overlay 选用；一个 CMake 目标可以注册了却因为不在 `entrypoints.txt` 里而被跳过。这种解耦正是后续模块要展开的内容。

#### 4.1.3 源码精读

入口点的权威定义在官方文档里：

[docs/dev/entrypoints.md:5-9](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/docs/dev/entrypoints.md#L5-L9) — 这段直接定义 entrypoint 是「公开函数或全局变量」，并声明它是源码布局、构建系统、配置管理的中心概念。

紧接着，文档对比传统 libc，给出「为什么选择 entrypoint 粒度」的三条理由：

[docs/dev/entrypoints.md:13-21](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/docs/dev/entrypoints.md#L13-L21) — 列出粒度化构建目标、配置驱动选择、支持 Overlay/Full 三大收益。

在真实代码里，入口点最直观的「具象」就是函数目录下的 `add_entrypoint_object` 调用。以 `isalpha` 为例：

[src/ctype/CMakeLists.txt:13-22](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/src/ctype/CMakeLists.txt#L13-L22) — 这里把 `isalpha` 注册为一个名为 `isalpha` 的入口点目标，给出它的源文件、头文件与依赖。注意目标名（`isalpha`）就是入口点的「名字」，而非完整的点分路径（完整路径 `libc.src.ctype.isalpha` 由所在目录自动推导）。

#### 4.1.4 代码实践

1. **实践目标**：用一个真实函数，亲手把入口点的「三个关注点」对上号。
2. **操作步骤**：
   - 选定函数 `isalpha`。
   - 在仓库里找出分别回答 A/B/C 三个关注点的文件：
     - A（实现）：`src/ctype/isalpha.cpp` 与 `src/ctype/isalpha.h`
     - B（构建目标）：`src/ctype/CMakeLists.txt`
     - C（平台取舍）：`config/linux/x86_64/entrypoints.txt`
   - 把它们的路径抄下来，做成一张三列表格。
3. **需要观察的现象**：三处文件名里都出现 `isalpha`（或其点分路径 `libc.src.ctype.isalpha`），说明它们围绕的是同一个入口点。
4. **预期结果**：你能用「同一个名字」串联起实现、构建、配置三个文件，体会到「入口点 = 一个贯穿三层的统一坐标」。

#### 4.1.5 小练习与答案

**练习 1**：文档说「全局变量」也可以是入口点。请举一个例子。
**答案**：`errno` 就是一个全局变量型入口点。在 `config/linux/x86_64/entrypoints.txt` 里能看到 `libc.src.errno.errno` 这样的条目。

**练习 2**：下面哪种说法最准确地描述了 entrypoint？
- (a) 一个 `.cpp` 源文件。
- (b) 一个对外公开的函数或全局变量，同时是源码、构建、配置的统一坐标。
- (c) 静态库里的一个符号。

**答案**：(b)。(a) 只覆盖了实现这一面；(c) 是结果而非定义；入口点的关键在于它把实现、构建目标、平台取舍三者用同一个名字串起来。

---

### 4.2 入口点的生命周期（实现 → 注册 → 配置）

#### 4.2.1 概念说明

一个入口点从「被写下」到「出现在最终库（如 `libc.a`）里」，会经历三个阶段。官方文档把它们概括为入口点的「生命周期（lifecycle）」：

1. **实现（Implementation）**：用 `.cpp` 文件按 LLVM-libc 的编码与实现规范把函数写出来。
2. **注册（Registration）**：用 `add_entrypoint_object` 规则把入口点定义成一个 CMake 目标，让它「可被构建」。
3. **配置（Configuration）**：把目标名加进某个 `entrypoints.txt`，让它在某个 OS/架构组合下「真正进入产物」。

注意三个动词的递进：**写出来 → 能构建 → 进产物**。只做前两步，入口点还不会出现在 `libc.a` 里；少了第三步，它就只是一个「已注册但被跳过」的目标（这正是 4.4 节 SKIP 机制的来源）。

#### 4.2.2 核心流程

```text
[1] 实现            [2] 注册                  [3] 配置
src/ctype/isalpha.cpp   add_entrypoint_object      config/.../entrypoints.txt
src/ctype/isalpha.h     (在 src/ctype/CMakeLists)   里写入 libc.src.ctype.isalpha
        │                       │                            │
        ▼                       ▼                            ▼
   函数算法与签名           CMake 目标 isalpha          决定「编不编进 libc.a」
   （HOW：怎么做）          （HOW：怎么编）              （WHETHER：要不要）
        └─────────── 三者合起来，一个入口点才真正“活”在产物里 ───────────┘
                                   │
                                   ▼
                          最终被 add_entrypoint_library
                          聚合进 libc.a / libm.a
```

一个常被忽略的细节：**实现文件结构本身也遵循规范**。入口点的内部头在 `LIBC_NAMESPACE_DECL` 命名空间里声明函数；实现文件用 `LLVM_LIBC_FUNCTION` 宏定义函数。这套规范的细节属于下一讲（u2-l2），本讲只需知道：实现阶段不是随便写个 C++ 函数，而是要套上「入口点专用外壳」。

#### 4.2.3 源码精读

文档把生命周期浓缩成三步列表：

[docs/dev/entrypoints.md:24-30](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/docs/dev/entrypoints.md#L24-L30) — 明确 Implementation / Registration / Configuration 三个阶段及其各自产物。

文档随后给出实现阶段的两条结构约定（头文件结构、源文件结构）：

[docs/dev/entrypoints.md:34-61](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/docs/dev/entrypoints.md#L34-L61) — 说明实现位于 `src/` 下、按公共头文件组织（`ctype.h` → `src/ctype/`），并展示内部头与 `.cpp` 的标准写法（`LIBC_NAMESPACE_DECL` 与 `LLVM_LIBC_FUNCTION`）。

注册阶段的范例就是我们刚看过的 `isalpha`：

[src/ctype/CMakeLists.txt:13-22](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/src/ctype/CMakeLists.txt#L13-L22) — 完成「注册」这一步：入口点 `isalpha` 现在是一个 CMake 目标了。

配置阶段的范例在平台配置文件里：

[config/linux/x86_64/entrypoints.txt:13-28](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/config/linux/x86_64/entrypoints.txt#L13-L28) — `ctype.h` 一节下列出了 `libc.src.ctype.isalpha` 等条目，完成「配置」这一步，使 `isalpha` 真正进入 Linux/x86_64 的产物。

#### 4.2.4 代码实践

1. **实践目标**：用「生命周期三阶段」框架追踪一个真实入口点 `isalpha`。
2. **操作步骤**：
   - **实现**：打开 `src/ctype/isalpha.cpp` 与 `src/ctype/isalpha.h`，确认头文件里函数声明被包在 `LIBC_NAMESPACE_DECL` 命名空间内，`.cpp` 里用 `LLVM_LIBC_FUNCTION(int, isalpha, (int c))` 定义。
   - **注册**：打开 `src/ctype/CMakeLists.txt`，定位 `isalpha` 的 `add_entrypoint_object`，记下它的 `SRCS`、`HDRS`、`DEPENDS`。
   - **配置**：打开 `config/linux/x86_64/entrypoints.txt`，搜索 `isalpha`，确认它在 `ctype.h` 一节里。
3. **需要观察的现象**：三个文件分别对应「写出来 / 能构建 / 进产物」，名字一致、职责分明。
4. **预期结果**：你能画出一条「`isalpha.cpp` → `add_entrypoint_object` → `entrypoints.txt`」的链路图，并标注每个文件回答的是 HOW 还是 WHETHER。

#### 4.2.5 小练习与答案

**练习 1**：如果一个开发者只完成了「实现」和「注册」，漏掉了「配置」，会发生什么？
**答案**：该入口点会被解析为一个 CMake 目标，但因为它的名字不在平台 `entrypoints.txt` 推导出的列表里，构建系统会把它标记为 `SKIPPED`（见 4.4 节），即「注册了但不编译」，最终不会进入 `libc.a`。

**练习 2**：把生命周期三阶段与「HOW / WHETHER」对应起来。
**答案**：实现与注册都回答 HOW（怎么做、怎么编），配置回答 WHETHER（要不要进产物）。三阶段是串行依赖：没有实现就无从注册，没有注册配置就无从引用。

---

### 4.3 CMake 规则概览（add_entrypoint_object / add_entrypoint_library）

#### 4.3.1 概念说明

入口点能被构建系统理解，靠的是两条专用 CMake 规则：

- **`add_entrypoint_object`**：把**单个**入口点编成一个目标文件（object file）。它是「一个函数 = 一个构建单元」在构建系统里的具体体现。
- **`add_entrypoint_library`**：把**一批**入口点对象聚合（aggregate）成一个静态库（如 `libc.a`、`libm.a`）。

可以这样类比：`add_entrypoint_object` 像「做一个零件」，`add_entrypoint_library` 像「把一堆零件装箱出厂」。

此外还有一个常用变体——**别名（ALIAS）型入口点**：当某个函数其实只是「另一个入口点的别名」（典型场景：`setjmp` 在不同架构下转发到架构特化实现），可以用 `ALIAS` 选项声明，避免重复实现。文档还提到一个 `REDIRECTED` 选项用于「重定向型」入口点（当函数只是另一个函数的简单别名时），二者都服务于「一个名字指向另一个实现」的需求。

#### 4.3.2 核心流程

`add_entrypoint_object` 的典型调用形态（来自文档与真实代码）：

```cmake
add_entrypoint_object(
  isalpha          # 入口点名字（= 目标名）
  SRCS isalpha.cpp # 源文件
  HDRS isalpha.h   # 内部实现头
  DEPENDS          # 依赖（其他 object / header library 目标）
    libc.src.__support.ctype_utils
)
```

规则内部的处理流程（简化）：

```text
add_entrypoint_object(isalpha ...)
        │
        ▼
create_entrypoint_object(fq_target_name)   ← 真正干活的核心函数
        │
        ├─ [1] 查 TARGET_ENTRYPOINT_NAME_LIST：名字在不在配置列表里？
        │       ├─ 不在 → 造一个空 custom_target，标记 SKIPPED=YES，return
        │       └─ 在   → 继续
        ├─ [2] ALIAS 分支？ → 复用被别名目标的 OBJECT_FILE，return
        ├─ [3] 正常分支：add_library(... OBJECT ...) 编出 .o
        └─ [4] 设置 ENTRYPOINT_NAME / TARGET_TYPE=ENTRYPOINT_OBJ 等属性
```

最关键的是第 [1] 步：**注册与编译被解耦**。`add_entrypoint_object` 总是会被解析（目标总会被创建），但「要不要真的编出 `.o`」取决于配置列表。这是入口点机制能在「粒度化构建」与「跨平台」之间取得平衡的核心。

`add_entrypoint_library` 则简单直接：吃进一组入口点目标，递归收集它们（及其依赖）的目标文件，打成一个 `STATIC` 库。

#### 4.3.3 源码精读

规则的用法注释（含 `ALIAS|REDIRECTED`、`NAME` 等选项）写在实现文件顶部：

[cmake/modules/LLVMLibCObjectRules.cmake:156-168](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/cmake/modules/LLVMLibCObjectRules.cmake#L156-L168) — 这是 `add_entrypoint_object` 的官方用法说明，列出了 `ALIAS`、`REDIRECTED`、`NAME`、`SRCS`、`HDRS`、`DEPENDS` 等参数。

真正干活的核心函数 `create_entrypoint_object` 起始于：

[cmake/modules/LLVMLibCObjectRules.cmake:169-176](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/cmake/modules/LLVMLibCObjectRules.cmake#L169-L176) — 解析参数，`"ALIAS;REDIRECTED"` 是两个可选开关。

SKIP 与正常分支的分界在这里：

[cmake/modules/LLVMLibCObjectRules.cmake:178-196](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/cmake/modules/LLVMLibCObjectRules.cmake#L178-L196) — 用 `list(FIND TARGET_ENTRYPOINT_NAME_LIST ...)` 判断入口点名字是否在配置列表中；找不到（`index == -1`）就造一个空目标并设 `"SKIPPED" "YES"` 后 `return()`。这就是「注册了但不编译」的实现。

ALIAS 分支：复用被别名目标的产物，不重复编译：

[cmake/modules/LLVMLibCObjectRules.cmake:200-261](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/cmake/modules/LLVMLibCObjectRules.cmake#L200-L261) — 检查依赖必须恰好一个、校验被别名目标本身也是入口点，然后把它的 `OBJECT_FILE` 直接拿来用，并标记 `IS_ALIAS "YES"`。

`add_entrypoint_library` 的实现：

[cmake/modules/LLVMLibCLibraryRules.cmake:127-163](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/cmake/modules/LLVMLibCLibraryRules.cmake#L127-L163) — 先 `get_all_object_file_deps` 递归收集所有依赖的目标文件，再用 `add_library(... STATIC ...)` 打成静态库。注意它上方的注释（133-135 行）：**只有被显式列在 `DEPENDS` 里的入口点才会进库，隐式依赖不会被自动加进去**——这保证了库的内容完全受配置控制。

聚合的真实调用点在 `lib/CMakeLists.txt`：

[lib/CMakeLists.txt:4-27](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/lib/CMakeLists.txt#L4-L27) — Full 模式产出 `libc`/`libm`/`libmvec`（用 `TARGET_LIBC_ENTRYPOINTS` 等），Overlay 模式产出 `llvmlibc`（用 `TARGET_LLVMLIBC_ENTRYPOINTS`），随后调用 `add_entrypoint_library(libc DEPENDS ${TARGET_LIBC_ENTRYPOINTS})`。这一处正是「入口点 → 静态库」的终点。

ALIAS 的真实范例（`setjmp`/`longjmp` 转发到架构特化实现）：

[src/setjmp/CMakeLists.txt:18-30](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/src/setjmp/CMakeLists.txt#L18-L30) — `setjmp` 与 `longjmp` 都声明为 `ALIAS`，`DEPENDS` 指向 `.${LIBC_TARGET_ARCHITECTURE}.setjmp`（如 x86_64 下的特化实现）。于是「公开入口点名」与「架构特化实现」被解耦：上层用稳定名字 `setjmp`，底层按架构挑选实现。

> 说明：`REDIRECTED` 选项在规则签名中声明（见 [LLVMLibCObjectRules.cmake:172-173](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/cmake/modules/LLVMLibCObjectRules.cmake#L172-L173)），文档描述它用于「函数是另一个函数简单别名」的重定向场景；但在当前仓库的 `src/` 下，实际承载「一个名字指向另一个实现」这一需求的、被大量使用的形式是 `ALIAS`（`setjmp`、`longjmp`、`termios`、`fcntl`、`stdio` 等目录都有）。两者的设计意图一致。

#### 4.3.4 代码实践

1. **实践目标**：读懂一个普通入口点与一个别名入口点的 CMake 写法差异。
2. **操作步骤**：
   - 打开 [src/ctype/CMakeLists.txt](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/src/ctype/CMakeLists.txt)，观察 `isalpha` 是「普通」入口点：有 `SRCS`/`HDRS`，自己提供实现。
   - 打开 [src/setjmp/CMakeLists.txt](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/src/setjmp/CMakeLists.txt)，观察 `setjmp` 是「别名」入口点：带 `ALIAS`，**没有** `SRCS`，`DEPENDS` 指向另一个目标。
   - 仿照 `isalpha`，为一个**假想**函数 `iscyrillic` 写一段 `add_entrypoint_object`（只写 CMake 片段，不要真的改仓库）：
     ```cmake
     # 示例代码（仅供练习，非仓库原有内容）
     add_entrypoint_object(
       iscyrillic
       SRCS iscyrillic.cpp
       HDRS iscyrillic.h
       DEPENDS
         libc.src.__support.ctype_utils
     )
     ```
3. **需要观察的现象**：普通入口点用 `SRCS` 自带实现；别名入口点用 `ALIAS` + 单个 `DEPENDS` 转发。
4. **预期结果**：你能口头复述「普通入口点产自己的 `.o`，别名入口点复用被别名目标的 `.o`」。若不确定别名目标的 `.o` 是如何被复用的，可回到 4.3.3 的源码链接再读一遍 `IS_ALIAS` 分支。

#### 4.3.5 小练习与答案

**练习 1**：`add_entrypoint_library` 的注释强调「隐式入口点依赖不会自动加进库」。为什么这是重要的设计？
**答案**：它保证了「库里有哪些函数」完全由显式 `DEPENDS`（也就是配置）决定，而不是被某个入口点的内部依赖意外牵连进来。这让 Overlay/Full 的取舍精确可控——你只会得到你点名的那些函数。

**练习 2**：以下两段 CMake，哪一段是别名入口点？
- (a) `add_entrypoint_object(foo SRCS foo.cpp HDRS foo.h)`
- (b) `add_entrypoint_object(foo ALIAS DEPENDS .x86_64.foo)`

**答案**：(b)。`ALIAS` 关键字 + 单个 `DEPENDS` + 无 `SRCS` 是别名入口点的特征；(a) 是自带实现的普通入口点。

---

### 4.4 配置文件入口（entrypoints.txt 与 SKIP 机制）

#### 4.4.1 概念说明

第三个、也是最容易被忽略的一个侧面：**一个入口点最终「编不编进产物」，由平台配置文件 `entrypoints.txt` 决定**。文档把它定位为某平台「支持什么」的「事实来源（source of truth）」：

> This file acts as the "source of truth" for what is supported on a given platform. A typical bring-up procedure involves progressively adding targets to this file as they are implemented and tested.

这句话点出了 `entrypoints.txt` 的两个用途：

1. **声明支持范围**：它列出该 OS/架构下所有「进入产物」的入口点点分名。
2. **渐进式 bring-up**：移植到新平台时，不是一次性全做完，而是「实现一个、测试一个、加进 `entrypoints.txt` 一个」。

而把「配置」与「构建」真正连接起来的，是上一节提到的 **SKIP 机制**：构建系统会**解析所有** `add_entrypoint_object`，但对不在配置列表里的那些，只造一个空的、标记 `SKIPPED` 的占位目标，不真正编译。于是「配置驱动选择」从一句口号变成了可观察的构建行为。

#### 4.4.2 核心流程

从 `entrypoints.txt` 到「编不编译」的完整链路：

```text
config/<os>/<arch>/entrypoints.txt
   │  set(TARGET_LIBC_ENTRYPOINTS  libc.src.ctype.isalpha  ...)
   ▼
libc/CMakeLists.txt 加载该文件           （CMakeLists.txt:385-389）
   │  → 推导 TARGET_ENTRYPOINT_NAME_LIST = [isalpha, ...]  （取最后一个点分量）
   ▼
src/ctype/CMakeLists.txt: add_entrypoint_object(isalpha ...)
   │  → create_entrypoint_object 查询 TARGET_ENTRYPOINT_NAME_LIST
   ▼
  ┌───────────────────────────────────────┐
  │ 名字在列表里？                          │
  │  是 → 编出真实 .o（TARGET_TYPE=ENTRYPOINT_OBJ）│
  │  否 → 造空目标，SKIPPED=YES（不编译）        │
  └───────────────────────────────────────┘
   ▼
lib/CMakeLists.txt: add_entrypoint_library(libc DEPENDS ${TARGET_LIBC_ENTRYPOINTS})
   │  → 只有「没被跳过」的入口点对象被打进 libc.a
   ▼
最终产物 libc.a / libm.a / libllvmlibc.a
```

一个推论：**同一个 `src/` 树，喂不同的 `entrypoints.txt`，就能产出内容不同的库**。这就是 Overlay（只挑少数纯算法函数）与 Full（尽可能多）能够共用同一份实现代码的根本原因。

#### 4.4.3 源码精读

构建根加载 `entrypoints.txt`（找不到则直接 `FATAL_ERROR`）：

[CMakeLists.txt:385-389](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/CMakeLists.txt#L385-L389) — 把 `entrypoints.txt` 当作 CMake 片段 `include` 进来，由它填充 `TARGET_LIBC_ENTRYPOINTS` 等列表。

从点分路径推导「入口点名字列表」：

[CMakeLists.txt:415-425](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/CMakeLists.txt#L415-L425) — 遍历配置出的入口点列表，用 `string(FIND ... REVERSE)` 找最后一个点，截取末尾分量（如 `libc.src.ctype.isalpha` → `isalpha`），汇成 `TARGET_ENTRYPOINT_NAME_LIST`。这正是 SKIP 检查所依据的名单。

SKIP 检查本身：

[cmake/modules/LLVMLibCObjectRules.cmake:179-196](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/cmake/modules/LLVMLibCObjectRules.cmake#L179-L196) — `list(FIND TARGET_ENTRYPOINT_NAME_LIST ${ADD_ENTRYPOINT_OBJ_NAME} ...)`；若返回 `-1`（不在名单），就 `add_custom_target` 一个空目标并设 `"SKIPPED" "YES"`，然后立刻 `return()`，跳过后续所有编译动作。

`entrypoints.txt` 的真实样貌（Linux/x86_64）：

[config/linux/x86_64/entrypoints.txt:13-28](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/config/linux/x86_64/entrypoints.txt#L13-L28) — `ctype.h` 一节下列出 `isalnum`、`isalpha`、…、`toupper` 等条目，每条都是一个入口点的点分全名。这一节就是「Linux/x86_64 支持哪些 ctype 函数」的事实来源。

文档对配置阶段的总述：

[docs/dev/entrypoints.md:96-109](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/docs/dev/entrypoints.md#L96-L109) — 说明 `entrypoints.txt` 的位置（`config/<os>/` 或 `config/<os>/<arch>/`）、角色（事实来源）与 bring-up 流程，并强调「实现新入口点后必须把目标名加进相关 `entrypoints.txt` 才会被构建」。

#### 4.4.4 代码实践

1. **实践目标**：亲眼看到 SKIP 机制在配置与构建之间起作用。
2. **操作步骤**：
   - 打开 [config/linux/x86_64/entrypoints.txt](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/config/linux/x86_64/entrypoints.txt)，统计 `ctype.h` 一节下列了多少个入口点（预期：`isalnum` … `toupper` 共 16 个）。
   - 打开 [src/ctype/CMakeLists.txt](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/src/ctype/CMakeLists.txt)，数一下该文件里 `add_entrypoint_object` 注册了多少个 ctype 入口点；比较两者数量是否一致。
   - 再打开 `config/linux/x86_64/exclude.txt`（同级目录），看是否有 ctype 函数被显式排除，并思考为什么会被排除（提示：Overlay 守卫、依赖宿主私有 ABI、尚未移植等）。
3. **需要观察的现象**：`src/ctype/` 里注册的入口点，可能比 `entrypoints.txt` 里列出的更多（例如带 `_l` 后缀的 locale 版函数常被排除），这正是 SKIP 机制在「裁剪」产物。
4. **预期结果**：你能用一句话解释「为什么 `src/` 里写了的函数不一定出现在 `libc.a` 里」——因为它可能不在该平台的 `entrypoints.txt` 名单中，从而被 SKIP。若你尚未实际构建过该项目，相关构建日志现象标注「待本地验证」。

#### 4.4.5 小练习与答案

**练习 1**：SKIP 机制为什么能同时服务「渐进式 bring-up」和「Overlay/Full 两种模式」？
**答案**：因为「实现/注册」与「编不编」被解耦了。bring-up 时，已实现并测试的函数加进 `entrypoints.txt` 就生效、没加的自动被跳过；Overlay 模式只往 `entrypoints.txt` 里放少数纯算法函数，其余被跳过、回退到系统 libc，Full 模式则放尽可能多。同一份代码、同一套规则，靠配置就能切换。

**练习 2**：`TARGET_ENTRYPOINT_NAME_LIST` 存的是完整点分路径（如 `libc.src.ctype.isalpha`）还是末尾名字（如 `isalpha`）？为什么这点重要？
**答案**：存的是末尾名字。因为 `add_entrypoint_object` 在各函数目录里用的是短名（`isalpha`），SKIP 检查要能匹配，就必须把配置里的点分路径归一成同一个短名。这一步发生在 [CMakeLists.txt:415-425](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/CMakeLists.txt#L415-L425)。

---

## 5. 综合实践

把四个最小模块串起来，完成本讲的主实践任务。

**任务**：阅读 [docs/dev/entrypoints.md](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/docs/dev/entrypoints.md)，用自己的语言回答两个问题，并用真实代码佐证。

**问题 1：为什么传统 libc 是单体归档，而 LLVM-libc 选择 entrypoint 粒度？**

请围绕下列要点组织你的答案（先自己写，再对照）：

- 传统单体归档把「实现」「构建」「平台取舍」三者焊死在一起：一个 `.c` 文件含很多函数，整体编进一个 `.a`，难以单独取舍某个函数、难以按平台挑选实现。
- entrypoint 粒度把这三者解耦（见 4.1.2 的三关注点图），带来文档承诺的三大收益：粒度化构建目标、配置驱动选择、支持 Overlay/Full。
- 用真实代码佐证：SKIP 机制（[LLVMLibCObjectRules.cmake:179-196](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/cmake/modules/LLVMLibCObjectRules.cmake#L179-L196)）证明「注册」与「编译」可以分离；Overlay 与 Full 共用 `add_entrypoint_library` 只是换了入口点列表（[lib/CMakeLists.txt:4-27](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/lib/CMakeLists.txt#L4-L27)）证明同一份实现能服务两种模式。

**问题 2：举出一个「重定向/别名」型入口点的使用场景，并找到真实代码。**

- 场景：某个公开函数在不同平台/架构下需要指向**不同的底层实现**，但对外暴露的名字必须稳定。典型例子是 `setjmp`/`longjmp`——它们高度依赖架构寄存器约定，实现因架构而异，但 C 标准要求统一的公开名。
- 真实代码：[src/setjmp/CMakeLists.txt:18-30](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/src/setjmp/CMakeLists.txt#L18-L30) 把 `setjmp`、`longjmp` 声明为 `ALIAS`，`DEPENDS` 指向 `.${LIBC_TARGET_ARCHITECTURE}.setjmp`。这样在 x86_64 下它转发到 x86_64 特化实现，在 aarch64 下转发到 aarch64 特化实现，而上层配置与公共头里始终用稳定的名字 `setjmp`。
- 关于 `REDIRECTED`：文档（[entrypoints.md:87-88](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/docs/dev/entrypoints.md#L87-L88)）把它描述为「函数是另一个函数的简单别名」时可用的选项，规则签名里也声明了它（[LLVMLibCObjectRules.cmake:172-173](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/cmake/modules/LLVMLibCObjectRules.cmake#L172-L173)）；当前仓库里实际承担这类「一个名字指向另一实现」需求、且被广泛使用的形式是 `ALIAS`。请把 `setjmp` 的 `ALIAS` 写法作为你答案里的具体实例。

**交付物**：一段 200 字左右的中文解释 + 一张标注了文件/行号的调用关系草图（`setjmp` → `.${LIBC_TARGET_ARCHITECTURE}.setjmp`）。

## 6. 本讲小结

- **入口点（entrypoint）是 LLVM-libc 的中心抽象**：每个公开函数/全局变量都是一个独立、有名的构建单元，贯穿源码布局、构建系统与配置管理（[entrypoints.md:5-9](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/docs/dev/entrypoints.md#L5-L9)）。
- **三阶段生命周期**：实现（`.cpp`/`.h`）→ 注册（`add_entrypoint_object`）→ 配置（`entrypoints.txt`），分别回答 HOW、HOW、WHETHER（[entrypoints.md:24-30](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/docs/dev/entrypoints.md#L24-L30)）。
- **两条核心 CMake 规则**：`add_entrypoint_object` 造单个目标文件，`add_entrypoint_library` 把一批对象聚合成 `libc.a`/`libm.a`/`libllvmlibc.a`（[LLVMLibCLibraryRules.cmake:127-163](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/cmake/modules/LLVMLibCLibraryRules.cmake#L127-L163)）。
- **SKIP 机制**把「注册」与「编译」解耦：不在配置名单里的入口点只造空占位目标、不编译（[LLVMLibCObjectRules.cmake:179-196](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/cmake/modules/LLVMLibCObjectRules.cmake#L179-L196)）。
- **`entrypoints.txt` 是平台支持范围的事实来源**，支撑渐进式 bring-up 与 Overlay/Full 共用同一份实现（[entrypoints.md:96-109](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/docs/dev/entrypoints.md#L96-L109)）。
- **别名（ALIAS）型入口点**让「稳定的公开名」与「架构特化实现」解耦，`setjmp`/`longjmp` 是典型实例（[src/setjmp/CMakeLists.txt:18-30](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/src/setjmp/CMakeLists.txt#L18-L30)）。

## 7. 下一步学习建议

本讲建立了「入口点」这一贯穿全项目的中心抽象。接下来建议按依赖顺序学习：

- **u2-l2 实现规范与核心宏**：深入本讲一笔带过的 `LIBC_NAMESPACE_DECL` 与 `LLVM_LIBC_FUNCTION` 宏，看入口点的「实现外壳」如何用 asm 别名把 C++ 符号映射成公开 C 链接名。
- **u2-l3 CMake 构建规则详解**：把本讲的规则概览展开，逐字段读懂 `SRCS`/`HDRS`/`DEPENDS` 如何约束构建顺序，并动手为假想函数写一份完整的 `add_entrypoint_object`。
- **u2-l4 平台配置体系**：把本讲的 `entrypoints.txt` 扩展到完整的配置树（`exclude.txt`、`headers.txt`、`config.json`），理解移植时如何渐进填充入口点。

旁读建议：在进入 u2-l2 之前，可重读 u1-l5 的 `isalpha` 五件套，把本讲的「三阶段生命周期」逐一对到那五个文件上，巩固「抽象 ↔ 具象」的对应关系。

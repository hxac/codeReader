# CMake 构建规则详解

## 1. 本讲目标

本讲深入 LLVM-libc 构建系统的「肌肉」：那些把一个函数从源码变成 `libc.a` 里一个目标文件的 CMake 规则。学完后你应该能够：

- 读懂任意一个函数目录下的 `CMakeLists.txt`，看懂 `SRCS` / `HDRS` / `DEPENDS` 各自表达什么。
- 解释 `add_entrypoint_object` 内部到底做了哪些事（命名、SKIP、双 object library、依赖传播）。
- 解释 `add_entrypoint_library` 如何把成百上千个入口点对象递归聚合成 `libc.a` / `libm.a`，并理解「隐式入口点依赖不会被加入库」这条关键规则。
- 看懂 `lib/CMakeLists.txt` 如何按 Full / Overlay 模式产出不同的归档名（`c`/`m`/`mvec` vs `llvmlibc`）并安装。
- 为一个新函数写出正确的 `add_entrypoint_object` 规则并排好依赖。

## 2. 前置知识

本讲承接 **u2-l1（入口点机制）** 与 **u2-l2（实现规范与核心宏）**，不再重复「入口点是什么」「`LLVM_LIBC_FUNCTION` 宏如何展开」。这里默认你已经知道：

- 每个公开函数都是一个名叫 **entrypoint（入口点）** 的独立构建单元，有「实现 → CMake 注册 → `entrypoints.txt` 配置」三阶段生命周期。
- 实现代码包在 `LIBC_NAMESPACE_DECL`（默认 `__llvm_libc`）命名空间里，公开符号由 `LLVM_LIBC_FUNCTION` 通过 asm 别名导出。

本讲只回答其中一阶段的问题：**「注册」这一步，CMake 到底做了什么？** 为此需要先建立两个 CMake 直觉：

- **目标是依赖图的节点**。CMake 里一切皆「目标（target）」。`add_library`、`add_custom_target`、`add_executable` 都在创建目标；`target_link_libraries` / `add_dependencies` 则在目标之间连边，连边既决定「谁先构建」，也决定「编译/链接选项如何沿边传播」。
- **目标名是有结构的字符串**。LLVM-libc 用点分全限定名（fully qualified name，下称 FQ 名）给目标命名，形如 `libc.src.ctype.isalpha`。它由「目录路径」推导而来，因此目标和源码位置一一对应——这是整个配置体系的坐标系。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [docs/dev/entrypoints.md](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/docs/dev/entrypoints.md) | 入口点机制的官方技术参考，讲清两条规则的用法。 |
| [src/ctype/CMakeLists.txt](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/src/ctype/CMakeLists.txt) | 一个真实、最简单的函数目录：一串 `add_entrypoint_object`，是本讲的主样本。 |
| [lib/CMakeLists.txt](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/lib/CMakeLists.txt) | 聚合层：把入口点对象打包成 `libc.a`/`libm.a`/`libllvmlibc.a` 并安装。 |
| [cmake/modules/LLVMLibCObjectRules.cmake](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/cmake/modules/LLVMLibCObjectRules.cmake) | `add_entrypoint_object` 的**真正实现**（`create_entrypoint_object`）。 |
| [cmake/modules/LLVMLibCLibraryRules.cmake](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/cmake/modules/LLVMLibCLibraryRules.cmake) | `add_entrypoint_library` 的**真正实现**，以及对象依赖的递归收集。 |
| [cmake/modules/LLVMLibCTargetNameUtils.cmake](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/cmake/modules/LLVMLibCTargetNameUtils.cmake) | 把「目录路径 + 局部名」拼成 FQ 目标名、把相对依赖名解析成 FQ 名的工具函数。 |
| [libc/CMakeLists.txt](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/CMakeLists.txt) | 顶层构建根：从 `entrypoints.txt` 读出 `TARGET_ENTRYPOINT_NAME_LIST`，驱动 SKIP 机制。 |

> 提示：很多教程只讲 `src/*/CMakeLists.txt` 怎么写。但要看懂「为什么这么写有效」，必须读到 `cmake/modules/` 下的实现。本讲会把两层都拆开。

## 4. 核心概念与源码讲解

### 4.1 add_entrypoint_object：把一个函数变成可构建的目标

#### 4.1.1 概念说明

`add_entrypoint_object` 是入口点生命周期的第二步——「注册」。它在函数自己的源码目录里被调用，输入是「函数名 + 源文件 + 头文件 + 依赖」，输出是一个 CMake 目标，该目标最终编译出一个包含该函数实现的 **object file（目标文件，`.o`）**。

它解决三个问题：

1. **命名**：给这个函数目标一个全限定名，让配置体系能用同一个名字指代它。
2. **是否真正编译**：并不是所有写了 `add_entrypoint_object` 的函数都会被编译——当前平台若没把它列进 `entrypoints.txt`，它只会得到一个空占位目标（SKIP）。
3. **依赖与选项传播**：通过 `DEPENDS` 既约束构建顺序，又让被依赖目标的编译选项、头文件搜索路径沿依赖图自动传过来。

#### 4.1.2 核心流程

调用 `add_entrypoint_object(isalpha SRCS ... HDRS ... DEPENDS ...)` 后，CMake 内部大致经过：

```text
add_entrypoint_object(target_name, ...)            # 用户调用的薄包装
   │
   ├─ 若未给 NAME，则 NAME = target_name
   ▼
add_target_with_flags(...)                          # 收集 FLAGS、展开 flag 组合
   │  get_fq_target_name → libc.src.ctype.isalpha   # ① 命名
   ▼
create_entrypoint_object(fq_target_name)            # 真正干活
   │
   ├─ ② 查 NAME 是否在 TARGET_ENTRYPOINT_NAME_LIST
   │     不在 → 建 add_custom_target 空目标，SKIPPED=YES，return   ← SKIP
   │
   ├─ [ALIAS 分支：必须有且仅有一个 DEPENDS，转发到被别名目标]
   │
   ├─ full_deps_list = 用户 DEPENDS + libc.src.__support.common      # 自动加 common
   │
   ├─ ③ 建 internal object library  (isalpha.__internal__)          # 给测试/lint 用
   ├─ ④ 建 public  object library  (isalpha)        # 带 -DLIBC_COPT_PUBLIC_PACKAGING
   │     两者的 include 目录、编译选项、link 库都来自 full_deps_list
   │
   └─ ⑤ set_target_properties: OBJECT_FILE/TARGET_TYPE=ENTRYPOINT_OBJ/...
```

最关键的两点直觉：

- **「写了规则」≠「会编译」**。是否真正生成 `.o` 取决于当前平台的配置名单；这是入口点机制实现「同一份实现服务多平台」的核心杠杆。
- **一个函数会建两个 object library**：一个内部版（供单元测试和 clang-tidy 直接链接内部符号）、一个公开版（带公开打包宏，最终进 `libc.a`）。

#### 4.1.3 源码精读

先看官方文档给出的用法骨架，这就是你会反复手写的样子：

[docs/dev/entrypoints.md:L72-L88](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/docs/dev/entrypoints.md#L72-L88) —— `add_entrypoint_object` 文档示例：第一个参数是入口点名，`SRCS` 是实现源文件，`HDRS` 是内部实现头，`DEPENDS` 列内部依赖。文档同时点出 `REDIRECTED` 选项用于「一个函数只是另一个的别名」。

再看一个真实样本。在 `src/ctype/` 下，`isalpha` 的注册只有短短几行：

[src/ctype/CMakeLists.txt:L13-L22](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/src/ctype/CMakeLists.txt#L13-L22) —— 真实的 `isalpha` 注册：`SRCS isalpha.cpp`、`HDRS isalpha.h`、`DEPENDS` 两个 `__support` 工具。

这段看似简单的调用背后是一长串 CMake。第一层是用户调用的薄包装 `add_entrypoint_object`：

[cmake/modules/LLVMLibCObjectRules.cmake:L425-L444](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/cmake/modules/LLVMLibCObjectRules.cmake#L425-L444) —— 包装函数：若未传 `NAME` 则默认 `NAME = target_name`，再委托给 `add_target_with_flags`（处理 FLAGS 展开），最终调用真正的 `create_entrypoint_object`。

真正干活的是 `create_entrypoint_object`，它开头的 SKIP 判断是理解「为什么不一定会编译」的钥匙：

[cmake/modules/LLVMLibCObjectRules.cmake:L169-L196](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/cmake/modules/LLVMLibCObjectRules.cmake#L169-L196) —— 用 `list(FIND TARGET_ENTRYPOINT_NAME_LIST ${NAME} ...)` 在配置名单里查这个入口点。查不到（`index == -1`）就只建一个 `add_custom_target` 空壳，打上 `SKIPPED=YES` 并 `return()`，**完全不编译**。

那么 `TARGET_ENTRYPOINT_NAME_LIST` 从哪来？它是顶层构建根从各平台 `entrypoints.txt` 读出来的名单，并对每个全限定名取「最后一个 `.` 之后的部分」：

[libc/CMakeLists.txt:L415-L425](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/CMakeLists.txt#L415-L425) —— 遍历 `TARGET_LLVMLIBC_ENTRYPOINTS`，反向找最后一个点，取末尾分量塞进 `TARGET_ENTRYPOINT_NAME_LIST`。例如 `libc.src.ctype.isalpha` → `isalpha`。所以配置名单与 SKIP 判断用的是**函数短名**。

> 这也解释了 u2-l1 提到的「TARGET_ENTRYPOINT_NAME_LIST 由点分路径取末尾分量推导」——本讲给出了它的源码出处。

过了 SKIP 这一关，函数才会真正被编译。注意它会**自动**把 `libc.src.__support.common` 拼进依赖（`common.h` 提供 `LLVM_LIBC_FUNCTION` 宏，见 u2-l2），所以你不必在每个函数里手写这条依赖：

[cmake/modules/LLVMLibCObjectRules.cmake:L286-L296](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/cmake/modules/LLVMLibCObjectRules.cmake#L286-L296) —— `full_deps_list` 在用户 `DEPENDS` 基础上追加 `libc.src.__support.common`。

接着创建两个 object library（内部版 + 公开版），它们的编译选项、头文件搜索路径、link 库都来自同一个 `full_deps_list`：

[cmake/modules/LLVMLibCObjectRules.cmake:L297-L325](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/cmake/modules/LLVMLibCObjectRules.cmake#L297-L325) —— 先建 `${fq_target_name}.__internal__`（不加公开打包宏），再建 `${fq_target_name}`（加 `-DLIBC_COPT_PUBLIC_PACKAGING`）；两者都用 `target_link_libraries(... ${full_deps_list})`，这正是 DEPENDS 传播选项与头文件路径的落点。

最后把生成的 object 文件路径记进属性，供后续聚合使用：

[cmake/modules/LLVMLibCObjectRules.cmake:L338-L347](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/cmake/modules/LLVMLibCObjectRules.cmake#L338-L347) —— `OBJECT_FILE=$<TARGET_OBJECTS:${fq_target_name}>` 用生成器表达式把目标对象挂到属性上，`TARGET_TYPE=ENTRYPOINT_OBJ` 给聚合层做类型识别。

#### 4.1.4 代码实践

**实践目标**：亲眼看到 SKIP 机制的效果——一个「写了规则但没进配置名单」的函数不会被编译。

**操作步骤**：

1. 在已完成的 Full 构建目录下，用 `ninja -t targets all | grep isalpha` 列出与 `isalpha` 相关的 CMake 目标，确认存在 `libc.src.ctype.isalpha`。
2. 打开 [config/linux/x86_64/entrypoints.txt](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/config/linux/x86_64/entrypoints.txt)，搜索 `isalpha`，确认它在该平台的配置名单里。
3. 想象你把这一行从 `entrypoints.txt` 删除（**不要真的改源码**，仅在脑中模拟），重新 `cmake` 配置后再次 `ninja -t targets all | grep isalpha`。

**需要观察的现象**：

- 步骤 1 中目标存在；步骤 2 中能在配置名单找到 `libc.src.ctype.isalpha`。
- 思考：删除配置行后，`TARGET_ENTRYPOINT_NAME_LIST` 不再含 `isalpha`，`create_entrypoint_object` 走 SKIP 分支，只剩一个空 `add_custom_target`。

**预期结果**：你会发现「写不写 `add_entrypoint_object`」回答的是「**能不能被构建**」，而「在不在这个平台的 `entrypoints.txt`」回答的是「**这次构建要不要它**」。两件事解耦，正是入口点粒度的价值。

> 待本地验证：若你尚未跑通构建，可只做步骤 1–2 的源码阅读部分；步骤 3 的「删除后行为」按上述推理理解即可。

#### 4.1.5 小练习与答案

**练习 1**：`isalpha` 的 `add_entrypoint_object` 里并没有写 `DEPENDS libc.src.__support.common`，但 `LLVM_LIBC_FUNCTION` 宏（来自 `common.h`）依然可用，为什么？

**参考答案**：`create_entrypoint_object` 会把 `libc.src.__support.common` 自动追加进 `full_deps_list`（见 [LLVMLibCObjectRules.cmake:L286](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/cmake/modules/LLVMLibCObjectRules.cmake#L286)），并通过 `target_link_libraries` 传播其头文件路径，所以每个入口点都隐式拿到了 `common.h`。

**练习 2**：为什么 `create_entrypoint_object` 要建「内部」和「公开」两个 object library？

**参考答案**：内部版（不带 `-DLIBC_COPT_PUBLIC_PACKAGING`）供单元测试与 clang-tidy 直接链接内部实现、检查内部头；公开版带公开打包宏，其 object 文件最终被打进 `libc.a` 对外发布。两套对象共享同一份源码、同一套依赖，仅靠一个宏区分用途。

### 4.2 DEPENDS 依赖：表达内部依赖与构建顺序

#### 4.2.1 概念说明

`DEPENDS` 在 LLVM-libc 里不只是「构建顺序」。一个入口点依赖另一个目标时，会同时获得两样东西：

- **构建顺序**：被依赖目标先构建（`add_dependencies`）。
- **接口传播**：被依赖目标的 `INTERFACE` 编译选项、头文件搜索路径、链接库会沿边自动传过来（`target_link_libraries`）。

这就是为什么 `isalpha` 只要在 `DEPENDS` 里写上 `libc.src.__support.ctype_utils`，就能直接 `#include "src/__support/ctype_utils.h"` 而不用手动加 include 目录——头文件库 `ctype_utils` 把自己的 include 路径作为接口暴露了出来。

`DEPENDS` 的取值有两种写法：

- **绝对名（FQ 名）**：`libc.src.__support.ctype_utils`、`libc.include.ctype`、`libc.src.__support.CPP.limits`。以 `libc.` 开头，跨目录引用时必须用这种。
- **相对名**：以 `.` 开头，如 `.ctype_utils`，表示「相对于当前目录」的目标，CMake 会自动拼成 FQ 名。

#### 4.2.2 核心流程

依赖名从「书写形式」到「真实目标」的解析由两个小函数完成：

```text
get_fq_target_name(local_name)        # 目录相对路径 + local_name → libc.<dir.dot>.<local>
get_fq_dep_name(name)                 # name 以 '.' 开头？
                                       #   是 → 相对名，拼当前目录前缀
                                       #   否 → 视作已经是 FQ 名，原样使用
```

设当前源码目录相对 `LIBC_SOURCE_DIR` 的路径为 `rel_path`，则 FQ 名为：

\[
\text{FQ} = \text{libc.}\,\text{rel\_path}\text{(把 "/" 换成 ".")}\,.\,\text{local\_name}
\]

例如 `src/ctype/` 下的 `isalpha` → `libc.src.ctype.isalpha`；`src/__support/` 下的 `ctype_utils` → `libc.src.__support.ctype_utils`。

#### 4.2.3 源码精读

FQ 名的推导极其简短，就是「目录相对路径 + 点 + 局部名」：

[cmake/modules/LLVMLibCTargetNameUtils.cmake:L1-L5](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/cmake/modules/LLVMLibCTargetNameUtils.cmake#L1-L5) —— `get_fq_target_name`：算出当前目录相对源码根的相对路径，把 `/` 替换成 `.`，前缀 `libc.`，再拼局部名。

依赖名的「相对 / 绝对」分流同样简短：

[cmake/modules/LLVMLibCTargetNameUtils.cmake:L13-L23](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/cmake/modules/LLVMLibCTargetNameUtils.cmake#L13-L23) —— `get_fq_dep_name`：名字是否以 `.` 开头决定相对还是绝对；相对名去掉首字符后用 `get_fq_target_name` 补全当前目录前缀。

回到真实样本，`isalpha` 的 `DEPENDS` 全是绝对名，它们与源码里的 `#include` 一一对应：

[src/ctype/CMakeLists.txt:L13-L22](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/src/ctype/CMakeLists.txt#L13-L22) 与 [src/ctype/isalpha.cpp:L9-L14](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/src/ctype/isalpha.cpp#L9-L14) 对比 —— CMake 里的 `libc.src.__support.CPP.limits` / `libc.src.__support.ctype_utils` 正好对应源码里的 `#include "src/__support/CPP/limits.h"` / `#include "src/__support/ctype_utils.h"`。依赖不是装饰：少了它，include 路径就传不进来，编译会找不到头文件。

相对名的写法在 `__support` 内部很常见——同一目录下的兄弟目标互相依赖时省得写长名：

[src/__support/CMakeLists.txt:L205-L206](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/src/__support/CMakeLists.txt#L205-L206) —— `str_to_integer` 依赖 `.ctype_utils`（相对名，解析为 `libc.src.__support.ctype_utils`），与同处一个文件、写成绝对名的 `libc.src.__support.CPP.limits` 混用。两种写法等价，选择只看可读性。

被依赖的目标本身需要先被定义。`ctype_utils` 是个「头文件库」（`add_header_library`），它只暴露头文件与接口，不产生 `.o`：

[src/__support/CMakeLists.txt:L155-L159](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/src/__support/CMakeLists.txt#L155-L159) —— `ctype_utils` 头文件库定义。`isalpha` 依赖它，本质是「我要用它的头文件路径」。

> 类型提示：LLVM-libc 有三类可被 `DEPENDS` 的目标——`ENTRYPOINT_OBJ`（入口点对象）、`OBJECT_LIBRARY`（`add_object_library`，产出 `.o`）、`HDR_LIBRARY`（`add_header_library`，只暴露头文件/接口，如上面的 `ctype_utils`）。聚合层会按类型区别对待它们。

最后看一眼 DEPENDS 如何同时承担「顺序」和「接口传播」两职责——在 `create_entrypoint_object` 里这两行紧挨着：

[cmake/modules/LLVMLibCObjectRules.cmake:L309-L310](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/cmake/modules/LLVMLibCObjectRules.cmake#L309-L310) —— `add_dependencies` 约束构建顺序；紧随其后的 `target_link_libraries(... ${full_deps_list})`（[L310](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/cmake/modules/LLVMLibCObjectRules.cmake#L310)）让被依赖目标的 `INTERFACE` 选项与 include 路径自动传到当前目标。

#### 4.2.4 代码实践

**实践目标**：体会「删掉一条 DEPENDS 会让编译失败」，从而理解 DEPENDS 不仅仅是顺序。

**操作步骤**（**源码阅读型实践**，不实际改源码）：

1. 在 [src/ctype/CMakeLists.txt](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/src/ctype/CMakeLists.txt) 的 `isalpha` 块中，定位 `DEPENDS` 下的 `libc.src.__support.ctype_utils`。
2. 想象删掉这一行后重新构建。

**需要观察的现象 / 预期结果**：`ctype_utils` 这个 `HDR_LIBRARY` 不再被 link 进来，它的头文件搜索路径不再传播到 `isalpha` 目标，于是 `#include "src/__support/ctype_utils.h"` 会报「找不到头文件」。结论：`DEPENDS` 既是顺序约束，也是 include/选项的传播通道，缺一不可。

> 待本地验证：上述失败现象需在实际构建中触发；若只做阅读，按依赖传播机制理解即可。

#### 4.2.5 小练习与答案

**练习 1**：在 `src/ctype/` 目录里写 `DEPENDS .ctype_utils` 会被解析成什么 FQ 名？能解析到真实目标吗？

**参考答案**：解析为 `libc.src.ctype.ctype_utils`，但该目标不存在（`ctype_utils` 实际定义在 `src/__support/`，FQ 名是 `libc.src.__support.ctype_utils`）。相对名按「当前目录」拼接，所以在 `src/ctype/` 里只能用绝对名 `libc.src.__support.ctype_utils` 引用它。

**练习 2**：为什么 `isalnum` 比 `isalpha` 多了一条 `libc.include.ctype` 依赖？（提示：看 `isalnum_l` 与 locale。）

**参考答案**：`libc.include.ctype` 是由 hdrgen 生成的公共头 `ctype.h` 对应的目标。需要引用公共头里定义的类型（如 `locale_t`）的入口点会依赖它；纯算法函数（如 `isalpha`）只依赖内部 `__support` 头，不需要。详见 u3-l1 头文件生成。

### 4.3 静态库聚合：add_entrypoint_library

#### 4.3.1 概念说明

单个入口点只是一个 object file。C 标准库的形态是一个 **静态归档（archive，`.a`）**——本质是「一堆 `.o` 打包成一个文件」。`add_entrypoint_library` 就是把一组入口点对象（连同它们的内部对象依赖）聚合成一个 `STATIC` 库目标。

这里有一个**极易踩坑**的关键规则（源码里专门用 NOTE 标注）：

> **隐式入口点依赖不会被加入库。** 如果想让某个入口点出现在 `libc.a` 里，必须把它**显式**列在 `add_entrypoint_library` 的 `DEPENDS` 里。仅靠「A 内部依赖 B」不会让 B 自动进库。

为什么？因为内部依赖链里的对象（如 `__support` 的 object library）会被递归收集并打进库，但「入口点」本身是公开边界——库只收录你点名要的入口点，避免把所有间接可达的函数都塞进来。

#### 4.3.2 核心流程

`add_entrypoint_library` 内部两步走：

```text
add_entrypoint_library(target_name DEPENDS <入口点目标列表>)
   │
   ├─ ① get_all_object_file_deps(all_deps, fq_deps_list)
   │      对每个入口点：
   │        - 校验它确是 ENTRYPOINT_OBJ / ENTRYPOINT_EXT（否则 FATAL_ERROR）
   │        - collect_object_file_deps 递归收集它身后的「非入口点」对象依赖
   │          （OBJECT_LIBRARY / 别名入口点的本体 等）
   │        - 再把入口点对象本身补进列表
   │      → 得到「要进库的全部 object 目标」
   │
   └─ ② add_library(${target_name} STATIC ${objects})   # objects = 各目标的 $<TARGET_OBJECTS:...>
```

`collect_object_file_deps` 的递归逻辑很关键：遇到 `OBJECT_LIBRARY` 就把自身 + 递归其 `DEPS`；遇到 `ENTRYPOINT_OBJ`（含别名，会先解到本体）就只递归其 `DEPS`、**不把入口点自身**加进结果——入口点由外层 `get_all_object_file_deps` 显式补。这就实现了「内部对象自动随依赖进库，入口点只收显式列出的」。

#### 4.3.3 源码精读

那条 NOTE 直接写在函数定义上方，值得逐字读：

[cmake/modules/LLVMLibCLibraryRules.cmake:L127-L135](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/cmake/modules/LLVMLibCLibraryRules.cmake#L127-L135) —— 注释明确：想让入口点进库，必须在 `DEPENDS` 里显式列出；隐式入口点依赖不会自动加入。

递归收集的「类型分派」逻辑：

[cmake/modules/LLVMLibCLibraryRules.cmake:L52-L81](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/cmake/modules/LLVMLibCLibraryRules.cmake#L52-L81) —— `get_all_object_file_deps`：先校验依赖是入口点类型，再对每个调 `collect_object_file_deps` 收集内部对象，最后把入口点本身补进 `all_deps`。这就是「显式入口点 + 隐式内部对象」的实现。

`add_entrypoint_library` 的主体则很短：

[cmake/modules/LLVMLibCLibraryRules.cmake:L136-L163](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/cmake/modules/LLVMLibCLibraryRules.cmake#L136-L163) —— 收集到 `all_deps` 后，用生成器表达式 `$<TARGET_OBJECTS:${dep}>` 把每个目标产出的 `.o` 汇成 `objects`，最后 `add_library(${target_name} STATIC ${objects})` 得到归档目标，并把 `ARCHIVE_OUTPUT_DIRECTORY` 指向 `${LIBC_LIBRARY_DIR}`。

#### 4.3.4 代码实践

**实践目标**：验证「隐式入口点依赖不进库」这条规则的真实后果。

**操作步骤**（**源码阅读型实践**）：

1. 假设入口点 `foo` 在实现里调用了另一个入口点 `bar` 的内部符号，并在 `DEPENDS` 里写了 `bar`。
2. 但你**没有**把 `bar` 加进 `entrypoints.txt`（也没在聚合层显式列出）。
3. 阅读 [LLVMLibCLibraryRules.cmake:L52-L81](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/cmake/modules/LLVMLibCLibraryRules.cmake#L52-L81) 与 [L127-L135](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/cmake/modules/LLVMLibCLibraryRules.cmake#L127-L135)。

**需要观察的现象 / 预期结果**：仅靠 `foo` 的 `DEPENDS bar`，`bar` 的 object 文件**不会**进入最终归档（聚合层只收显式列出的入口点）。下游链接时会报 `undefined reference to bar`。所以「函数之间互相调用」必须在配置层把每个被调用的入口点都显式登记。这也呼应 u2-l1 强调的「配置驱动选择」。

> 说明：本实践重在理解规则，不一定需要实际触发链接错误。

#### 4.3.5 小练习与答案

**练习 1**：`add_entrypoint_library` 为什么用 `STATIC` 而不是 `OBJECT`？

**参考答案**：C 库的交付形态是静态归档 `.a`（一组 `.o` 加上符号索引），链接器按需从中抽取成员。`STATIC` 让 CMake 调用归档器（`ar`）把所有 object 文件打包成 `.a`；若用 `OBJECT` 只会得到一组未归档的对象，不符合 libc 的链接约定。

**练习 2**：`collect_object_file_deps` 遇到 `ENTRYPOINT_OBJ` 别名时如何处理？

**参考答案**：先读 `IS_ALIAS` 属性，若是别名则取其 `DEPS`（即被别名的本体目标）作为真正要展开的对象，再递归本体的内部依赖。这样别名入口点最终指向的是本体的 object 文件（见 [LLVMLibCLibraryRules.cmake:L22-L41](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/cmake/modules/LLVMLibCLibraryRules.cmake#L22-L41)）。

### 4.4 lib 目标：从配置名单到 libc.a / libm.a

#### 4.4.1 概念说明

`lib/CMakeLists.txt` 是聚合层的最顶端。它把上一节的 `add_entrypoint_library` 包进一个循环：按构建模式决定要产出哪些归档、用什么名字，然后逐个生成并安装。

核心是一个「三列对齐」的循环：

| 归档文件名（`ARCHIVE_OUTPUT_NAME`） | 库目标名 | 取用的入口点名单变量 |
| --- | --- | --- |
| Full 模式：`c` / `m` / `mvec` | `libc` / `libm` / `libmvec` | `TARGET_LIBC_ENTRYPOINTS` / `TARGET_LIBM_ENTRYPOINTS` / `TARGET_LIBMVEC_ENTRYPOINTS` |
| Overlay 模式：`llvmlibc` | `libc` | `TARGET_LLVMLIBC_ENTRYPOINTS` |

也就是说，同一个库目标名 `libc` 在两种模式下产出不同的归档文件（`libc.a` vs `libllvmlibc.a`），取用的入口点名单也不同——这是 u1-l4 讲的 Full/Overlay 二分在构建产物上的最终落地。

#### 4.4.2 核心流程

```text
按 LLVM_LIBC_FULL_BUILD 选列：c/m/mvec  或  llvmlibc
foreach (name, target, entrypoint_list) in ZIP:
    若 entrypoint_list 为空 → continue          # 该平台没有该库就跳过
    add_entrypoint_library(${target} DEPENDS ${${entrypoint_list}})
    set_target_properties(... ARCHIVE_OUTPUT_NAME ${name})   # 决定 .a 文件名
    if Full: target_link_libraries(... PUBLIC libc-headers)  # 头文件随库
             若存在 libc-startup，add_dependencies             # 启动对象依赖
    if GPU:  add_bitcode_entrypoint_library(...)              # GPU 额外产出 bitcode
install(TARGETS ${added_archive_targets} ... ARCHIVE DESTINATION ...)
```

注意 `ARCHIVE_OUTPUT_NAME` 这一步：它把「目标名」和「产物文件名」解耦。目标都叫 `libc`，但 Full 模式产物是 `libc.a`，Overlay 模式产物是 `libllvmlibc.a`。

#### 4.4.3 源码精读

模式分流在最开头，三列对齐地列出名字、目标、名单：

[lib/CMakeLists.txt:L1-L13](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/lib/CMakeLists.txt#L1-L13) —— Full 走 `c`/`m`/`mvec` 三件套与 `TARGET_LIBC/LIBM/LIBMVEC_ENTRYPOINTS`；Overlay 走 `llvmlibc` 单件与 `TARGET_LLVMLIBC_ENTRYPOINTS`。

主循环把上一节的 `add_entrypoint_library` 用起来，并设置产物名、挂上头文件与启动对象依赖：

[lib/CMakeLists.txt:L17-L39](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/lib/CMakeLists.txt#L17-L39) —— `ZIP_LISTS` 同时遍历三列；`add_entrypoint_library(${archive_1} DEPENDS ${${archive_2}})` 聚合入口点；`set_target_properties(... ARCHIVE_OUTPUT_NAME ${archive_0})` 决定最终 `.a` 文件名；Full 模式额外 `target_link_libraries(... PUBLIC libc-headers)` 并把 `libc-startup` 挂为依赖。这一段也包含了 GPU 目标额外产出 bitcode 库的分支（与 u11-l2 相关，此处只作了解）。

最后是安装，把归档放进 `LIBC_INSTALL_LIBRARY_DIR`：

[lib/CMakeLists.txt:L59-L63](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/lib/CMakeLists.txt#L59-L63) —— `install(TARGETS ${added_archive_targets} ARCHIVE DESTINATION ...)`。

此外还有一个 CMake 脚本级的「裁剪」手段值得对比：在 `src/ctype/CMakeLists.txt` 末尾，locale 版函数被一段 `if(NOT LLVM_LIBC_FULL_BUILD) return() endif()` 直接挡掉：

[src/ctype/CMakeLists.txt:L163-L166](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/src/ctype/CMakeLists.txt#L163-L166) —— Overlay 模式下直接 `return()`，连 locale 版入口点的 `add_entrypoint_object` 都不执行。这与 4.1 的 SKIP 机制是**两种不同层级**的裁剪：SKIP 是「规则内部按配置名单决定要不要编译」；`return()` 是「CMake 脚本层按模式决定要不要定义这些目标」。两者配合，共同实现平台/模式裁剪。

#### 4.4.4 代码实践

**实践目标**：把 4.1–4.4 串起来，亲手为假想函数写一份合规的注册规则。

**实践任务**：以 `ctype/CMakeLists.txt` 中的 `isalnum` 为模板，为假想函数 `iscyrillic` 写一份 `add_entrypoint_object`，正确列出 `SRCS`、`HDRS` 与对 `libc.include.ctype` 的 `DEPENDS`（假设 `iscyrillic` 也要用 `ctype_utils` 和 `CPP/limits`）。

**操作步骤**：

1. 复制 [src/ctype/CMakeLists.txt:L1-L11](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/src/ctype/CMakeLists.txt#L1-L11)（`isalnum` 块）作为模板。
2. 改写为下面的「示例代码」（**不是项目原有代码，仅供练习**）：

```cmake
# 示例代码：假想函数 iscyrillic 的注册规则
add_entrypoint_object(
  iscyrillic
  SRCS
    iscyrillic.cpp
  HDRS
    iscyrillic.h
  DEPENDS
    libc.include.ctype
    libc.src.__support.CPP.limits
    libc.src.__support.ctype_utils
)
```

3. 逐项核对你的选择是否正确（见下方预期结果）。

**需要观察的现象**：

- 第一个位置参数 `iscyrillic` 会被 `add_entrypoint_object` 默认当作 `NAME`，最终 FQ 目标名为 `libc.src.ctype.iscyrillic`。
- `DEPENDS` 里写出 `libc.include.ctype`（绝对名），CMake 能解析到由 hdrgen 生成的 `ctype.h` 目标，其头文件路径会传播进来。
- `libc.src.__support.common` 会被规则**自动**追加，无需手写。

**预期结果**：这份规则与 `isalnum` 结构一致；若再在 `include/ctype.yaml` 加上 `iscyrillic` 的签名、在 `config/.../entrypoints.txt` 注册 `libc.src.ctype.iscyrillic`、写好 `iscyrillic.cpp/h` 与单元测试，就构成了 u11-l3 将要讲的「贡献一个新函数」的完整触点。本讲只覆盖其中的「CMake 注册」一环。

> 待本地验证：本练习为「书写型」，不要求实际构建；若要真跑通，还需补齐 YAML/源码/测试/配置清单，那是后续讲义的内容。

#### 4.4.5 小练习与答案

**练习 1**：为什么 Full 模式产出 `libc.a`、Overlay 模式产出 `libllvmlibc.a`，但**库目标名都叫 `libc`**？

**参考答案**：目标名是 CMake 内部的依赖图节点标识，写构建脚本时引用方便；产物文件名通过 `ARCHIVE_OUTPUT_NAME` 单独控制（Full=`c`→`libc.a`，Overlay=`llvmlibc`→`libllvmlibc.a`）。两模式用同一目标名简化了下游对 `libc` 目标的引用，但产出不同文件名以避免覆盖系统 libc。

**练习 2**：在 `lib/CMakeLists.txt` 的循环里，为什么要有「`entrypoint_list` 为空就 `continue`」？

**参考答案**：不是所有平台都有全部三种库（例如某平台可能没有 `libmvec` 对应的名单）。若 `TARGET_LIBMVEC_ENTRYPOINTS` 这类变量为空，`add_entrypoint_library` 会因缺少 `DEPENDS` 而报错；`continue` 让循环跳过这种「该平台不提供的库」，保证构建脚本能跨平台通用。

## 5. 综合实践

把本讲四个模块串成一个跟踪任务：**追踪 `isalpha` 从「一条 `add_entrypoint_object`」到「`libc.a` 的一个成员」的完整旅程。**

1. **命名**：从 [src/ctype/CMakeLists.txt:L13-L22](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/src/ctype/CMakeLists.txt#L13-L22) 出发，用 [LLVMLibCTargetNameUtils.cmake:L1-L5](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/cmake/modules/LLVMLibCTargetNameUtils.cmake#L1-L5) 推导出 FQ 目标名 `libc.src.ctype.isalpha`，并说明它如何出现在 `config/linux/x86_64/entrypoints.txt` 中。
2. **是否编译**：用 [LLVMLibCObjectRules.cmake:L169-L196](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/cmake/modules/LLVMLibCObjectRules.cmake#L169-L196) 与 [libc/CMakeLists.txt:L415-L425](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/CMakeLists.txt#L415-L425) 解释：`isalpha` 之所以会被编译，是因为它的短名在 `TARGET_ENTRYPOINT_NAME_LIST` 里。
3. **依赖传播**：列出 `isalpha` 的 `DEPENDS`（[src/ctype/CMakeLists.txt:L19-L21](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/src/ctype/CMakeLists.txt#L19-L21)），对照 [src/ctype/isalpha.cpp:L9-L14](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/src/ctype/isalpha.cpp#L9-L14) 的 `#include`，说明每条依赖分别提供了哪个头文件路径。
4. **聚合**：说明 `isalpha` 作为 `libc.src.ctype.isalpha` 被显式列入 `TARGET_LIBC_ENTRYPOINTS`，最终经 [lib/CMakeLists.txt:L17-L39](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/lib/CMakeLists.txt#L17-L39) 的 `add_entrypoint_library(libc ...)` 聚合进 `libc.a`。

**交付物**：一张标注了「函数名 → FQ 目标名 → 配置名单 → object 文件 → 归档成员」的流转图，并附一段话解释「为什么 `isalpha` 不会出现在 Overlay 模式的 `libllvmlibc.a` 里也能正常」——提示：Overlay 模式同样会编译未带 locale 依赖的 ctype 函数，只是产物文件名与名单不同。

## 6. 本讲小结

- `add_entrypoint_object` 是入口点「注册」阶段的规则，输入 `SRCS/HDRS/DEPENDS`，输出一个 FQ 目标名（`libc.<目录>.<函数>`）对应的 object 目标。
- **写了规则不等于会编译**：`create_entrypoint_object` 先查短名是否在 `TARGET_ENTRYPOINT_NAME_LIST`（源自 `entrypoints.txt`），不在则只建空壳目标并 SKIP——这是平台/模式裁剪的第一层。
- `DEPENDS` 同时承担「构建顺序」与「接口（头文件路径、编译选项）传播」两职责；依赖名分绝对（`libc.src.__support.ctype_utils`）与相对（`.ctype_utils`）两种写法，由 `get_fq_dep_name` 分流。每个入口点还会自动追加 `libc.src.__support.common`。
- `add_entrypoint_library` 把一组**显式列出**的入口点对象连同其递归内部对象依赖聚合成 `STATIC` 归档；**隐式入口点依赖不会自动进库**，必须在聚合层显式登记。
- `lib/CMakeLists.txt` 按模式产出不同归档：Full 模式 `libc.a`/`libm.a`/`libmvec.a`（取 `TARGET_LIBC/LIBM/LIBMVEC_ENTRYPOINTS`），Overlay 模式 `libllvmlibc.a`（取 `TARGET_LLVMLIBC_ENTRYPOINTS`），目标名与产物名通过 `ARCHIVE_OUTPUT_NAME` 解耦。
- 裁剪有两条路：规则内的 SKIP（按配置名单）与 CMake 脚本层的 `if(...) return()`（按模式，如 ctype 的 locale 版函数），二者配合实现「同一份实现服务多平台/多模式」。

## 7. 下一步学习建议

- **下一讲 u2-l4（平台配置体系）** 会专讲 `entrypoints.txt` / `exclude.txt` / `headers.txt` / `config.json` 这一整套配置树——本讲反复提到的 `TARGET_ENTRYPOINT_NAME_LIST` 正是从这里读出来的，届时会把「配置如何驱动 SKIP」补全。
- 想验证聚合细节，可继续阅读 [cmake/modules/LLVMLibCLibraryRules.cmake](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/cmake/modules/LLVMLibCLibraryRules.cmake) 中 `collect_object_file_deps` 对三类目标的递归处理。
- 若你对「依赖目标如何携带头文件路径」感兴趣，可预习 u3（头文件生成体系）中的 `add_header_library` / `add_gen_header`，它们定义了 `HDR_LIBRARY` 这类被 `DEPENDS` 的目标。
- 准备动手加函数的读者，可先看 [docs/dev/implementing_a_function.md](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/docs/dev/implementing_a_function.md)，它把本讲的「CMake 注册」与 YAML、`entrypoints.txt`、测试串成一份检查清单，是 u11-l3 的前奏。

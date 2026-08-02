# 贡献一个完整新函数：端到端实战

## 1. 本讲目标

本讲是「扩展与二次开发」单元的收尾，也是整本手册的收官实战。前面十几讲我们分别学会了 YAML 规范、实现头/源、`LLVM_LIBC_FUNCTION` 宏、`__support` 下沉、CMake 注册、平台 `entrypoints.txt` 裁剪、单元测试框架——它们都是「零件」。本讲要把这些零件**装配成一次真实、完整、可提交的贡献**：从零添加一个公开函数，并让它同时满足「能编译、能进产物、能被测试、能通过测试」。

学完本讲，你应当能够：

- 默写出官方 `implementing_a_function.md` 给出的**六步清单**，并说清每一步对应哪个文件、解决哪个问题。
- 独立完成一次端到端贡献：改 YAML、写 `.h`/`.cpp`、加 `add_entrypoint_object`、注册 `entrypoints.txt`、写并注册单元测试。
- 保证「依赖一致性」——CMake 的 `DEPENDS` 与 C++ 源码里的 `#include` 一一对应，避免构建期找不到头或链接期找不到符号。
- 理解 Overlay/Full 两种构建模式对同一个函数（尤其是依赖私有 ABI 的函数）的不同影响，知道在哪些步骤要加模式守卫。

本讲的四条主线（最小模块）：**六步流程**、**文件触点**、**依赖一致性**、**测试验证**。

## 2. 前置知识

本讲不再重新讲解底层概念，而是直接调用前面讲义建立的心智模型。开始前请确认你已掌握：

- **入口点（entrypoint）生命周期**（u2-l1）：实现 → `add_entrypoint_object` 注册 → `entrypoints.txt` 配置三阶段，分别回答 HOW、HOW、WHETHER。
- **实现规范与核心宏**（u2-l2）：所有内部符号包进 `LIBC_NAMESPACE_DECL` 命名空间，公开函数用 `LLVM_LIBC_FUNCTION` 宏借 asm 别名把 C++ 符号映射成 C 链接名。
- **头文件生成管线**（u3-l1）：`include/<header>.yaml` 是公共头的事实来源，hdrgen 据此生成可安装的公共头。
- **CMake 构建规则**（u2-l3）：`add_entrypoint_object` 用 `SRCS/HDRS/DEPENDS` 造单个目标，`DEPENDS` 同时承担构建顺序与头文件路径传播。
- **平台配置体系**（u2-l4）：`config/<os>/<arch>/entrypoints.txt` 是「本平台支持哪些函数」的事实来源（一阶裁剪），不在名单里的函数只造空壳目标。
- **单元测试框架**（u10-l1）：`TEST(Suite, Case)` 自动注册，断言用 `EXPECT_*`/`ASSERT_*`，Suite 名须以 `LlvmLibc` 打头；测试经 `add_libc_test` 注册，并 `DEPENDS` 它要测的入口点。
- **ctype 函数族**（u5-l1）与 **`__support` 设计哲学**（u4-l1）：入口点要「薄」，真正算法下沉到 `__support`。

一句话回顾：在 LLVM-libc 里，一个公开函数不是「一个文件」，而是**横跨 YAML、`.h`、`.cpp`、`CMakeLists.txt`、`entrypoints.txt`、`_test.cpp`、测试 `CMakeLists.txt` 这七个触点的协同改动**。本讲就是把这条链路走通。

## 3. 本讲源码地图

本讲引用的关键文件如下，它们正好对应六步清单里的各个触点：

| 文件 | 作用 | 对应步骤 |
| --- | --- | --- |
| `docs/dev/implementing_a_function.md` | 官方六步贡献清单（本讲的「说明书」） | 全流程 |
| `docs/dev/implementation_standard.md` | 实现头/源的标准骨架与 `LLVM_LIBC_FUNCTION` 宏说明 | 步骤 2、3 |
| `include/ctype.yaml` | ctype.h 公共头规范，函数签名的事实来源 | 步骤 1 |
| `src/ctype/isblank.h` / `isblank.cpp` | 最简入口点样例（自包含、不依赖 `ctype_utils`） | 步骤 2、3 |
| `src/ctype/isalpha.cpp` | 展示边界守卫 + 委托 `__support` 的写法 | 步骤 3 |
| `src/__support/ctype_utils.h` | 入口点下沉的公共判定逻辑 | 步骤 3 |
| `src/__support/common.h` | `LLVM_LIBC_FUNCTION` 宏定义 | 步骤 3 |
| `src/ctype/CMakeLists.txt` | `add_entrypoint_object` 注册入口点 | 步骤 4 |
| `config/linux/x86_64/entrypoints.txt` | 平台支持范围的事实来源 | 步骤 5 |
| `test/src/ctype/isalpha_test.cpp` | `TEST`/`EXPECT_*` 风格的单元测试样例 | 步骤 6 |
| `test/src/ctype/CMakeLists.txt` | 用 `add_libc_test` 注册测试目标 | 步骤 6 |

## 4. 核心概念与源码讲解

### 4.1 六步流程：implementing_a_function 检查清单

#### 4.1.1 概念说明

LLVM-libc 把「添加一个新函数」这件事**显式写成了一份检查清单**，放在 `docs/dev/implementing_a_function.md`。这份文档是本讲的纲：它把一次贡献拆成六步，每一步都点名了要改的文件、要遵循的约束，以及它在前序文档里的依据。

为什么需要这样一份清单？因为 LLVM-libc 的一个公开函数是「七触点协同」——漏掉任何一个，要么编译失败，要么函数不进产物，要么进了却没测试。清单的作用就是把这些隐式约定**外化为可勾选的步骤**，让贡献者不必靠记忆，而是靠流程。

#### 4.1.2 核心流程

六步清单的逻辑顺序是「从公开规范走向实现，再走向构建、配置、验证」：

```text
1. Header Entry       改 YAML        —— 函数「应当存在」于公共头（事实来源）
        ↓
2. Header Declaration 写 .h          —— 内部代码「能声明」这个函数
        ↓
3. Implementation     写 .cpp        —— 函数「有实现」（用 LLVM_LIBC_FUNCTION）
        ↓
4. CMake Rule         加 add_entrypoint_object —— 函数「能被编译」成目标
        ↓
5. Platform Registration  注册 entrypoints.txt —— 函数「进产物」（WHETHER）
        ↓
6. Testing            写 _test.cpp + 注册 —— 函数「被验证」正确
```

前三步回答「这个函数是什么、怎么实现」，后三步回答「它怎么进入构建、进入哪些平台产物、怎么证明它对」。注意步骤 4 与步骤 5 的分工正是 u2-l1 讲过的入口点生命周期的两个不同阶段：步骤 4 是注册（HOW，让函数可编译），步骤 5 是配置（WHETHER，让函数进特定平台产物）。二者**不能互相替代**——只做 4 不做 5，函数会被 SKIP 成空壳；只做 5 不做 4，配置名单指向一个不存在的目标。

#### 4.1.3 源码精读

这份纲本身就是源码。先看官方文档对六步的原文表述：

- [docs/dev/implementing_a_function.md:L11-L59](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/docs/dev/implementing_a_function.md#L11-L59) —— 这是 `## Step-by-Step Checklist` 整段，列出从 Header Entry 到 Testing 的六步，每步都给出文件路径与关键约束。

其中第一步对 YAML 的要求：

- [docs/dev/implementing_a_function.md:L13-L21](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/docs/dev/implementing_a_function.md#L13-L21) —— 说明要把新函数加进 `libc/include/<header>.yaml` 的 `functions` 列表，指定 `name`/`return_type`/`arguments` 与 `standards`（如 `stdc`、`POSIX`）。

第六步对测试的要求：

- [docs/dev/implementing_a_function.md:L54-L59](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/docs/dev/implementing_a_function.md#L54-L59) —— 说明测试文件放在 `libc/test/src/<header>/<func>_test.cpp`，用内部测试框架，并**必须同时更新测试目录的 `CMakeLists.txt`**（这一句常被新手漏掉）。

清单的每一步都会引用另一份文档（如 `{ref}implementation_standard`），那些细节就是后续几个最小模块的内容。

#### 4.1.4 代码实践

- **实践目标**：建立对六步清单的全局印象，能背出每步对应的文件类型。
- **操作步骤**：打开 `docs/dev/implementing_a_function.md`，把六步的标题与「File」行抄成一张速查表。
- **需要观察的现象**：注意步骤 4 与步骤 5 是两个**不同**的 CMake/配置动作（一个在 `src/<header>/CMakeLists.txt`，一个在 `config/<os>/<arch>/entrypoints.txt`）。
- **预期结果**：你得到一张「步骤 → 文件 → 作用」的三列表。
- 待本地验证（无需运行命令，纯阅读）。

#### 4.1.5 小练习与答案

- **练习 1**：如果只完成了步骤 1~4，跳过步骤 5，函数会发生什么？
  - **答案**：函数能编译成目标，但短名不在 `entrypoints.txt` 推导的 `TARGET_ENTRYPOINT_NAME_LIST` 中，于是只造一个**空壳占位目标**（SKIP），不会进入最终的 `libc.a`/`libm.a` 产物（见 u2-l3、u2-l4）。
- **练习 2**：步骤 6 为什么要同时改 `_test.cpp` **和** 测试目录的 `CMakeLists.txt`？
  - **答案**：写好的测试源文件不会自动被构建；必须用 `add_libc_test` 注册成目标，否则它只是一段没人编译的代码。

### 4.2 文件触点：每个步骤要改的文件与标准骨架

#### 4.2.1 概念说明

「文件触点」指一次贡献必须**同步修改的所有文件**。这一个小模块用 `isblank`（ctype.h 中最简单的函数之一）作为「最小可工作样例」逐个展示这些触点的真实样貌——它自包含到连 `ctype_utils` 都不需要，是理解骨架的最佳起点。掌握骨架后，复杂函数只是「在 `.cpp` 里多 include 几个 `__support` 头、在 `DEPENDS` 里多列几条依赖」而已。

#### 4.2.2 核心流程

以 `isblank` 为例，七个触点的对应关系：

| 步骤 | 触点文件 | isblank 的内容要点 |
| --- | --- | --- |
| 1 | `include/ctype.yaml` | 加一条 `name: isblank`、`standards: [stdc]`、`return_type: int`、`arguments: [{type: int}]` |
| 2 | `src/ctype/isblank.h` | `LIBC_NAMESPACE_DECL` 内声明 `int isblank(int c);` |
| 3 | `src/ctype/isblank.cpp` | `LLVM_LIBC_FUNCTION(int, isblank, (int c))` 定义实现 |
| 4 | `src/ctype/CMakeLists.txt` | `add_entrypoint_object(isblank SRCS isblank.cpp HDRS isblank.h)` |
| 5 | `config/linux/x86_64/entrypoints.txt` | 在 ctype.h 块加一行 `libc.src.ctype.isblank` |
| 6a | `test/src/ctype/isblank_test.cpp` | 用 `TEST`/`EXPECT_*` 写断言 |
| 6b | `test/src/ctype/CMakeLists.txt` | `add_libc_test(isblank_test DEPENDS libc.src.ctype.isblank)` |

#### 4.2.3 源码精读

**步骤 1 · YAML 规范**。先看 `isblank` 在 YAML 里长什么样——这正是 hdrgen 生成公共头 `ctype.h` 中 `int isblank(int);` 声明的依据（u3-l1）：

- [include/ctype.yaml:L25-L30](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/include/ctype.yaml#L25-L30) —— `isblank` 条目：`standards: [stdc]`、`return_type: int`、单参数 `type: int`。对照 [include/ctype.yaml:L7-L18](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/include/ctype.yaml#L7-L18) 的 `isalnum`/`isalpha`，可见单参 `int->int` 函数的 YAML 写法高度一致，新增函数照抄即可。

**步骤 2 · 实现头骨架**。`implementation_standard.md` 给出的标准骨架是「`#ifndef` 守卫（镜像路径）+ `LIBC_NAMESPACE_DECL` 内声明」：

- [docs/dev/implementation_standard.md:L19-L37](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/docs/dev/implementation_standard.md#L19-L37) —— 以 `isalpha.h` 为例的「Implementation Header File Structure」，强调守卫名 `LLVM_LIBC_SRC_CTYPE_ISALPHA_H` 就是把文件路径大写化、`/` 换 `_`，声明必须落在 `LIBC_NAMESPACE_DECL` 内。
- [src/ctype/isblank.h:L9-L20](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/docs/../src/ctype/isblank.h#L9-L20) —— `isblank` 的真实实现头，与上述骨架逐字对应：守卫 + include `macros/config.h` + 命名空间内一句声明。

**步骤 3 · 实现源骨架**。`implementation_standard.md` 给出的 `.cpp` 骨架是「命名空间内用 `LLVM_LIBC_FUNCTION` 定义」：

- [docs/dev/implementation_standard.md:L43-L61](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/docs/dev/implementation_standard.md#L43-L61) —— `.cpp` File Structure，强调实现体**必须**用 `LLVM_LIBC_FUNCTION(int, isalpha, (int c))` 宏定义。
- [src/ctype/isblank.cpp:L14-L20](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/src/ctype/isblank.cpp#L14-L20) —— `isblank` 的真实实现：`LLVM_LIBC_FUNCTION(int, isblank, (int c))` 直接返回 `c == ' ' || c == '\t'`。注意它**没有**边界守卫、**没有**委托 `ctype_utils`——因为空格/制表符的判定对任意 `int` 都安全（非空格非制表符就是假），这是「自包含」的极简样例。

对照一个**需要**边界守卫与委托的函数 `isalpha`：

- [src/ctype/isalpha.cpp:L18-L22](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/src/ctype/isalpha.cpp#L18-L22) —— 先把 `c` 守卫到 `[0, UCHAR_MAX]`，越界返回 0，再把真正的判定委托给 [src/__support/ctype_utils.h:L244-L302](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/src/__support/ctype_utils.h#L244-L302) 的 `internal::isalpha(char)`。这正是 u5-l1 讲过的「入口点薄壳 + 算法下沉」模式。

**步骤 3 关键宏**。`LLVM_LIBC_FUNCTION` 把内部 C++ 函数名改写成公开 C 符号，靠的是 asm 别名：

- [docs/dev/implementation_standard.md:L63-L75](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/docs/dev/implementation_standard.md#L63-L75) —— 文档对宏展开的说明：实现体命名为 `__name_impl__` 并 `asm(c_alias)` 改名为公开符号，再用 `[[gnu::alias]]` 让命名空间内的 `name` 指向同一符号（u2-l2）。
- [src/__support/common.h:L56-L66](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/src/__support/common.h#L56-L66) —— 真实宏定义 `LLVM_LIBC_FUNCTION_IMPL_4`（`__##name##_impl__ asm(c_alias)` + `[[gnu::alias(c_alias)]]`），以及 `LLVM_LIBC_ADD_FUNCTION_C_ALIAS`（为同一入口点追加额外公开 C 别名，如 scanf 的 `__isoc99_*`，见 u2-l2）。
- [src/__support/common.h:L98-L100](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/src/__support/common.h#L98-L100) —— `LLVM_LIBC_FUNCTION(...)` 入口宏用 `GET_FIFTH` 在「三参版」与「四参版（带显式 c_alias）」间分派；平时写 `LLVM_LIBC_FUNCTION(int, isblank, (int c))` 走三参版，c_alias 默认就是 `#name`。

**步骤 5 · 平台注册**。`entrypoints.txt` 用点分全限定名罗列本平台支持的入口点：

- [config/linux/x86_64/entrypoints.txt:L12-L28](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/config/linux/x86_64/entrypoints.txt#L12-L28) —— ctype.h 块，`libc.src.ctype.isblank` 就在其中（u2-l4）。新增函数就是在对应头文件的块里加同样格式的一行。

#### 4.2.4 代码实践

- **实践目标**：用 `isblank` 作「标尺」，亲手核对七个触点都已就位。
- **操作步骤**：依次打开上表七个文件，确认每个文件里都有 `isblank` 字样。
- **需要观察的现象**：注意 `src/ctype/isblank.cpp` 里 `#include` 了 `common.h` 和 `macros/config.h`，但**没有** `ctype_utils.h`；而 `src/ctype/CMakeLists.txt` 里 `isblank` 这条**也没有** `DEPENDS libc.src.__support.ctype_utils`（见 4.3）。
- **预期结果**：七个触点一一对应，无一缺失。
- 待本地验证（纯阅读 + 对照）。

#### 4.2.5 小练习与答案

- **练习 1**：实现头的守卫名 `LLVM_LIBC_SRC_CTYPE_ISBLANK_H` 是怎么从文件路径推导出来的？
  - **答案**：把相对路径 `src/ctype/isblank.h` 大写、`.` 与 `/` 都换成 `_`，前面加项目前缀 `LLVM_LIBC_`（见 implementation_standard.md 的 isalpha 示例）。
- **练习 2**：为什么 `isblank` 的实现可以直接返回布尔表达式，而 `isalpha` 必须先做范围守卫？
  - **答案**：`c == ' ' || c == '\t'` 对任意 `int`（含负值、超大值）都给出正确结果；而 `isalpha` 若直接把越界 `int` 强转 `char` 会误判（如 323 截断成 `'C'`），故须先把输入限制在 `[0, UCHAR_MAX]`（u5-l1）。

### 4.3 依赖一致性：CMake DEPENDS 与 #include 一一对应

#### 4.3.1 概念说明

「依赖一致性」是新手最易踩坑、评审最常打回的地方。LLVM-libc 有一条铁律（u4-l1）：**C++ 源码里 `#include` 的每一个 `__support`（以及其它内部库）头，都必须在所在 `add_entrypoint_object` 的 `DEPENDS` 里有对应的目标**。这条对应关系是双向的：

- 缺 `#include` 有 `DEPENDS` → 编译期找不到头，报错（容易发现）。
- 有 `#include` 缺 `DEPENDS` → 头文件搜索路径/编译选项没被传播，构建可能「碰巧」通过（如果头在系统路径里被找到）却埋下隐患，或在 hermetic/交叉构建下失败（难发现）。

`DEPENDS` 不只是「构建顺序」，它还是**头文件路径与编译选项的传播接口**（u2-l3）。所以一致性不是风格问题，而是正确性问题。

#### 4.3.2 核心流程

判定的伪代码：

```text
对于入口点 X 的 .cpp 里每个 #include "src/__support/<path>/<foo>.h":
    在 X 的 add_entrypoint_object 的 DEPENDS 中
    必须存在 libc.src.__support.<path>.<foo>   # 把 / 换成 .
```

点分名的换算规则就是「把相对路径的点分形式」：文件 `src/__support/ctype_utils.h` → 目标 `libc.src.__support.ctype_utils`；文件 `src/__support/CPP/limits.h` → 目标 `libc.src.__support.CPP.limits`。

#### 4.3.3 源码精读

看 `isalpha` 如何保持一致性——它的 `.cpp` include 了什么，`CMakeLists.txt` 就 DEPENDS 什么：

- [src/ctype/isalpha.cpp:L9-L14](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/src/ctype/isalpha.cpp#L9-L14) —— include 了 `isalpha.h`、`CPP/limits.h`、`common.h`、`ctype_utils.h`、`macros/config.h`。
- [src/ctype/CMakeLists.txt:L13-L22](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/docs/../src/ctype/CMakeLists.txt#L13-L22) —— `isalpha` 的 `add_entrypoint_object`：`DEPENDS` 列了 `libc.src.__support.CPP.limits` 与 `libc.src.__support.ctype_utils`，正好对应 `.cpp` 里的两个 `__support` 头（`common.h` 与 `macros/config.h` 由 `__support.common` 自动追加，无需手写，见 u2-l3）。

对照 `isblank`，它**不** include `ctype_utils.h`/`CPP/limits.h`：

- [src/ctype/CMakeLists.txt:L24-L30](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/src/ctype/CMakeLists.txt#L24-L30) —— `isblank` 的规则**只有** `SRCS/HDRS`、没有 `DEPENDS`，恰好对应它的 `.cpp` 不依赖任何 `__support` 子库（`common.h` 仍由自动追加机制覆盖）。这就是一致性的正面范例：依赖列表精确反映真实 include。

再看一处「依赖私有 ABI 类型」的情形。locale 版函数（如 `isalpha_l`）需要一个 `locale_t` 类型，它来自代理头目标 `libc.hdr.types.locale_t`：

- [src/ctype/CMakeLists.txt:L181-L191](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/src/ctype/CMakeLists.txt#L181-L191) —— `isalpha_l` 的 `DEPENDS` 多了 `libc.hdr.types.locale_t`。更重要的是它们整段被 [src/ctype/CMakeLists.txt:L163-L166](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/src/ctype/CMakeLists.txt#L163-L166) 的 `if(NOT LLVM_LIBC_FULL_BUILD) return()` 守住——**仅在 Full 模式下构建**，因为 locale 版函数依赖 `locale_t` 这一私有 ABI（u1-l4、u2-l2 讲过的 Overlay 守卫）。

这条 locale 守卫提醒我们一个更高层的一致性：**CMake 里的模式守卫要与该函数能否进 Overlay 的 ABI 判定一致**。函数若依赖实现私有布局（如 `FILE`、`locale_t`），就不能进 Overlay，必须在 `src` 侧加 `if(NOT LLVM_LIBC_FULL_BUILD) return()`，并在 `entrypoints.txt` 里只放进 Full 块。

#### 4.3.4 代码实践

- **实践目标**：亲手核对 `isdigit` 的依赖一致性。
- **操作步骤**：打开 `src/ctype/isdigit.cpp`，数它 include 了哪些 `__support` 头；再到 `src/ctype/CMakeLists.txt` 找 `isdigit` 的 `DEPENDS`，逐一比对。
- **需要观察的现象**：`isdigit.cpp` include `ctype_utils.h` 与 `CPP/limits.h`，`DEPENDS` 应同时列 `libc.src.__support.ctype_utils` 与 `libc.src.__support.CPP.limits`。
- **预期结果**：两边一一对应，无多无少。
- 待本地验证（纯对照）。

#### 4.3.5 小练习与答案

- **练习 1**：文件 `src/__support/CPP/span.h` 对应的 CMake 目标点分名是什么？
  - **答案**：`libc.src.__support.CPP.span`。
- **练习 2**：为什么 `common.h` 和 `macros/config.h` 几乎出现在每个 `.cpp` 里，却很少出现在手写的 `DEPENDS` 里？
  - **答案**：`add_entrypoint_object` 会自动给每个入口点追加 `__support.common`（u2-l3），它已经带上 `common.h` 与 `macros/config.h`，故无需重复手写。
- **练习 3**：locale 版 ctype 函数为什么被 `if(NOT LLVM_LIBC_FULL_BUILD) return()` 挡住？
  - **答案**：它们依赖 `locale_t` 这一实现私有 ABI 类型；Overlay 模式下只能放入不依赖私有 ABI 的纯算法函数（u1-l4）。

### 4.4 测试验证：用 test/UnitTest 写并注册单元测试

#### 4.4.1 概念说明

第六步「Testing」是贡献的验收环节。LLVM-libc 自带一套 gtest 风格的测试框架（u10-l1），原因是目标环境（GPU/baremetal）常常没有 C++ 标准库，无法直接用 GoogleTest。测试有两个触点：写 `_test.cpp`、在测试目录的 `CMakeLists.txt` 用 `add_libc_test` 注册。两个都做完，测试才会被编译并出现在 `check-libc` 里。

测试的一个关键约定：**断言针对内部命名空间下的函数**（`LIBC_NAMESPACE::isalpha`），而非公开 C 符号——因为公开符号是 asm 别名，内部 C++ 名才是测试能直接调用的实体（u1-l5、u10-l1）。

#### 4.4.2 核心流程

```text
1. 新建 test/src/<header>/<func>_test.cpp
   - include "test/UnitTest/Test.h"
   - include 被测内部头 "src/<header>/<func>.h"
   - TEST(LlvmLibc<Func>, CaseName) { EXPECT_*(LIBC_NAMESPACE::func(...), ...); }
2. 在 test/src/<header>/CMakeLists.txt 追加
   add_libc_test(<func>_test SUITE libc-<header>-tests SRCS <func>_test.cpp
                 DEPENDS libc.src.<header>.<func>)
3. （可选）ninja libc.test.src.<header>.<func>_test.__unit__  单跑该测试
```

Suite 名必须以 `LlvmLibc` 打头，否则会触发编译期 `static_assert` 失败（u10-l1）。

#### 4.4.3 源码精读

看 `isalpha` 的测试是怎么写的：

- [test/src/ctype/isalpha_test.cpp:L9-L12](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/test/src/ctype/isalpha_test.cpp#L9-L12) —— 测试的 include：`CPP/span.h`（构造参考数组）、被测内部头 `src/ctype/isalpha.h`、测试框架 `test/UnitTest/Test.h`。
- [test/src/ctype/isalpha_test.cpp:L33-L42](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/test/src/ctype/isalpha_test.cpp#L33-L42) —— `TEST(LlvmLibcIsAlpha, SimpleTest)`：用 `EXPECT_NE(..., 0)` 断言「真即非零」（C 标准语义），用 `EXPECT_EQ(..., 0)` 断言假；注意调用的是 `LIBC_NAMESPACE::isalpha`。
- [test/src/ctype/isalpha_test.cpp:L44-L54](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/test/src/ctype/isalpha_test.cpp#L44-L54) —— `DefaultLocale`：在 `[-255, 255)` 区间穷举所有 `int`，用 `in_span` 判定是否属于字母集合，分别断言真/假——这是 ctype 函数「穷举全空间」的标准测法，能覆盖负值与越界边界（u5-l1 的边界非对称约定）。

再看测试是如何被注册成构建目标的：

- [test/src/ctype/CMakeLists.txt:L13-L21](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/test/src/ctype/CMakeLists.txt#L13-L21) —— `add_libc_test(isalpha_test ...)`：`SUITE libc-ctype-tests` 归入测试套件、`SRCS isalpha_test.cpp` 指定源、`DEPENDS libc.src.ctype.isalpha` 把被测入口点链进来。这条 `DEPENDS` 与 4.3 同理——测试要链接被测函数的目标文件。
- [test/src/ctype/CMakeLists.txt:L1-L1](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/test/src/ctype/CMakeLists.txt#L1-L1) —— `add_custom_target(libc-ctype-tests)` 先声明套件伞目标，后续每条 `add_libc_test(... SUITE libc-ctype-tests ...)` 都挂到它名下。

`add_libc_test` 还会为每个测试派生 `.__unit__`（普通单测）与 `.__hermetic__`（封闭测试，`-nostdlib` 加自带 bump 分配器）两个目标（u10-l1），所以注册后既可单跑也可纳入 `check-libc`。

#### 4.4.4 代码实践

- **实践目标**：跑通一个已存在的 ctype 单测，建立「注册 → 可运行」的直觉。
- **操作步骤**：在已配置好的 runtimes 构建目录里执行 `ninja libc.test.src.ctype.isalpha_test.__unit__` 然后运行该可执行文件（命令格式见 u1-l3）。
- **需要观察的现象**：测试输出 PASS；若改坏 `isalpha` 的实现（仅在本地练习），对应断言会 FAIL。
- **预期结果**：所有用例通过。
- 待本地验证（需要先完成 u1-l3 的 runtimes 构建）。

#### 4.4.5 小练习与答案

- **练习 1**：为什么测试调 `LIBC_NAMESPACE::isalpha(...)` 而不是直接调公开的 `isalpha(...)`？
  - **答案**：公开 `isalpha` 是 asm 别名产生的 C 符号，C++ 代码直接调用内部命名空间名更直接，也避免与系统 libc 同名符号冲突（u1-l5）。
- **练习 2**：`EXPECT_NE(isalpha('a'), 0)` 为什么用「不等于 0」而不是「等于 1」？
  - **答案**：C 标准只保证判定为真时返回**非零**值，不保证是 1；用 `EXPECT_NE(..., 0)` 尊重这一语义（u1-l5、u10-l1）。

## 5. 综合实践

把四条主线串起来：**亲手添加一个练习用的小函数，完整走完六步**。为保证不与仓库现有函数冲突、且能立刻自我验证，我们用一个**虚构的 ctype 辅助判定** `isvowel`（判断英文字母是否为元音 a/e/i/o/u）作为练习目标。

> ⚠️ 说明：`isvowel` **不是** C 标准、POSIX 或 GNU 定义的函数，仅为本讲设计的练习函数。下面所有「示例代码」标签的内容都是为练习而写的样例，**不是**仓库已有代码。请在一个本地练习分支上操作，不要提交到上游。

**练习目标**：按六步清单把 `isvowel` 加进 ctype，让它能编译、进 Linux x86_64 产物、被单元测试覆盖。

**操作步骤（每一步标注它由哪讲支撑）**：

1. **Header Entry**（u3-l1）。在 `include/ctype.yaml` 的 `functions` 列表追加（示例代码）：
   ```yaml
     - name: isvowel
       standards:
         - gnu          # 练习函数，借用 gnu 标准占位
       return_type: int
       arguments:
         - type: int
   ```
2. **Header Declaration**（u2-l2）。新建 `src/ctype/isvowel.h`（示例代码）：
   ```cpp
   #ifndef LLVM_LIBC_SRC_CTYPE_ISVOWEL_H
   #define LLVM_LIBC_SRC_CTYPE_ISVOWEL_H
   #include "src/__support/macros/config.h"
   namespace LIBC_NAMESPACE_DECL {
   int isvowel(int c);
   } // namespace LIBC_NAMESPACE_DECL
   #endif // LLVM_LIBC_SRC_CTYPE_ISVOWEL_H
   ```
3. **Implementation**（u2-l2、u5-l1）。新建 `src/ctype/isvowel.cpp`，仿照 `isalpha` 做边界守卫（示例代码）：
   ```cpp
   #include "src/ctype/isvowel.h"
   #include "src/__support/CPP/limits.h"
   #include "src/__support/common.h"
   #include "src/__support/ctype_utils.h"
   #include "src/__support/macros/config.h"
   namespace LIBC_NAMESPACE_DECL {
   LLVM_LIBC_FUNCTION(int, isvowel, (int c)) {
     if (c < 0 || c > cpp::numeric_limits<unsigned char>::max())
       return 0;
     const char ch = static_cast<char>(c);
     // 复用 ctype_utils 的判定，保持编码无关
     return static_cast<int>(internal::islower(ch) || internal::isupper(ch)) &&
            (ch == 'a' || ch == 'e' || ch == 'i' || ch == 'o' || ch == 'u' ||
             ch == 'A' || ch == 'E' || ch == 'I' || ch == 'O' || ch == 'U');
   }
   } // namespace LIBC_NAMESPACE_DECL
   ```
4. **CMake Rule**（u2-l3、4.3）。在 `src/ctype/CMakeLists.txt` 追加，注意 `DEPENDS` 与 `.cpp` 的 `#include` 一一对应（示例代码）：
   ```cmake
   add_entrypoint_object(
     isvowel
     SRCS
       isvowel.cpp
     HDRS
       isvowel.h
     DEPENDS
       libc.src.__support.CPP.limits
       libc.src.__support.ctype_utils
   )
   ```
5. **Platform Registration**（u2-l4）。在 `config/linux/x86_64/entrypoints.txt` 的 ctype.h 块加一行（示例代码）：
   ```cmake
       libc.src.ctype.isvowel
   ```
6. **Testing**（u10-l1、4.4）。新建 `test/src/ctype/isvowel_test.cpp`（示例代码）：
   ```cpp
   #include "src/ctype/isvowel.h"
   #include "test/UnitTest/Test.h"
   TEST(LlvmLibcIsVowel, Basic) {
     EXPECT_NE(LIBC_NAMESPACE::isvowel('a'), 0);
     EXPECT_NE(LIBC_NAMESPACE::isvowel('E'), 0);
     EXPECT_EQ(LIBC_NAMESPACE::isvowel('b'), 0);
     EXPECT_EQ(LIBC_NAMESPACE::isvowel('3'), 0);
     EXPECT_EQ(LIBC_NAMESPACE::isvowel(-1), 0);   // 越界返回 0
     EXPECT_EQ(LIBC_NAMESPACE::isvowel(300), 0);  // 越界（300 截断非元音）
   }
   ```
   再在 `test/src/ctype/CMakeLists.txt` 追加（示例代码）：
   ```cmake
   add_libc_test(
     isvowel_test
     SUITE
       libc-ctype-tests
     SRCS
       isvowel_test.cpp
     DEPENDS
       libc.src.ctype.isvowel
   )
   ```

**需要观察的现象**：重新配置并 `ninja libc.test.src.ctype.isvowel_test.__unit__` 后运行测试；若前三步的边界守卫写错（例如漏掉 `c < 0` 判断），`isvowel(-1)` 用例会暴露问题；若 `DEPENDS` 漏写 `ctype_utils`，hermetic 构建可能报找不到 `ctype_utils.h`。

**预期结果**：测试全部通过，且 `isvowel` 出现在 `libc.a` 的符号表中（可用 `nm libc.a | grep isvowel` 核对，待本地验证）。

> 这一步把六步流程、文件触点、依赖一致性、测试验证四条主线全部串起：步骤 1 用了 u3-l1 的 YAML 知识，步骤 2~3 用了 u2-l2 的实现规范，步骤 4 用了 u2-l3 的 CMake 规则并呼应 4.3 的依赖一致性，步骤 5 用了 u2-l4 的平台配置，步骤 6 用了 u10-l1 的测试框架。

## 6. 本讲小结

- 一次合格贡献是**六步清单**的串联：YAML → 实现头 → 实现 → CMake 注册 → 平台 `entrypoints.txt` → 测试，前五步使函数「能编译、进产物」，第六步证明它「正确」。
- 一次贡献要同步改动**七个文件触点**（含测试目录的 `CMakeLists.txt`）；漏掉测试注册是新手最常见失误。
- **依赖一致性**是硬约束：`.cpp` 里每个 `__support` `#include` 都要在 `add_entrypoint_object` 的 `DEPENDS` 里有对应点分目标；依赖私有 ABI（如 `locale_t`）的函数还要加 Overlay 守卫。
- `LLVM_LIBC_FUNCTION` 宏借 asm 别名把内部 C++ 函数映射成公开 C 符号，测试则直接对内部命名空间名 `LIBC_NAMESPACE::func` 断言。
- 步骤 4（注册）与步骤 5（配置）分别对应入口点生命周期的 HOW 与 WHETHER，**不可互相替代**。
- 测试经 `add_libc_test` 注册后，既可单跑（`.__unit__`）也纳入 `check-libc`；ctype 类函数常用「穷举 `[-255,255)`」覆盖边界。

## 7. 下一步学习建议

- **真正提一个 PR**：把综合实践的 `isvowel` 思路换成仓库里一个**确有缺口的真实小函数**（可在 `docs/headers/index.rst` 的覆盖矩阵里寻找「未实现」项），按本讲六步走一遍真实贡献。
- **深入构建系统**：若你的函数涉及多平台/多架构，复习 u2-l3（`add_entrypoint_library` 聚合）、u2-l4（`exclude.txt` 二阶裁剪）与 u11-l1（移植到新平台），理解「同一个函数如何按平台换实现」。
- **进阶测试与正确性**：数学类函数请接 u10-l2（MPFR/MPC 高精度对照）；内存/字符串类函数请接 u10-l3（模糊测试与微基准），让你的贡献同时通过「正确、健壮、快」三重验收。
- **阅读他人 PR**：在 LLVM 仓库按 `libc.src.ctype` 或 `libc.src.string` 路径检索近期提交，对照本讲清单观察真实贡献者是如何填这七个触点的。

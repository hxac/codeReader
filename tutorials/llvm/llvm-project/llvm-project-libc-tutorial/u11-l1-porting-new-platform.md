# 移植到新平台：porting 指南

## 1. 本讲目标

本讲是「扩展与二次开发」单元的第一讲。前面你已经学过 LLVM-libc 如何把一个函数组织成入口点（u2-l1）、如何用 `config/<os>/<arch>/` 配置树决定「本平台支持哪些函数」（u2-l4）、以及公共头文件如何由 YAML 生成（u3-l1）。本讲把这些知识**反转过来用**：不再是「在一个已经移植好的平台上读配置」，而是「亲手为一个全新 OS/架构搭起这套配置」。

学完后你应该能够：

1. 说出把 LLVM-libc 带到一个**新 OS** 的完整步骤：在哪里建目录、建哪些文件、构建系统如何找到它们。
2. 理解「渐进式 bring-up」的含义——为什么不能一次性把几百个入口点全列进 `entrypoints.txt`，而要分波次地「实现一个、测试一个、加一个」。
3. 认识平台底层需要补齐的**最小实现集**：一条 syscall 封装链、一组启动对象（startup），以及它们各自为什么不可省。
4. 看懂官方对「上游化（upstreaming）一个目标」的硬性要求：维护者、CI、以及被淘汰（sunset）的规则。

> 本讲是「方法论」讲，重在流程与决策点，不逐行讲解某一具体函数的实现。具体到 syscall 与 startup 的内部机制，分别由 u8-l1、u8-l2 深入讲解，本讲只引用其结论。

## 2. 前置知识

阅读本讲前，最好已经建立以下认知（本讲会直接使用这些术语而不再展开）：

- **入口点（entrypoint）**：每个公开函数/全局变量是一个独立、有名的构建单元，有「实现 → `add_entrypoint_object` 注册 → `entrypoints.txt` 配置」三阶段生命周期（u2-l1）。
- **平台配置树**：`config/<os>/<arch>/` 下的 `entrypoints.txt`（支持范围的事实来源）、`headers.txt`（要生成的公共头）、`exclude.txt`（运行环境探测后的二阶裁剪）、`config.json`（实现细节旋钮）四个文件，把「某平台支持什么」外化为纯数据/脚本（u2-l4）。
- **Full 与 Overlay 两种构建模式**：Full 模式是完整 libc 替换品，**强制要求 `headers.txt`**；Overlay 模式只覆盖少数符号、可回退系统头（u1-l4）。
- **syscall 封装链**：`OSUtil/syscall.h` 按 OS 分派 → `<os>/syscall.h` 按架构分派 → 具体 `syscall_impl` 内联汇编；之上是返回 `ErrorOr` 的 `syscall_checked`（u8-l1）。
- **程序启动链**：`_start`（架构相关，取栈参数、对齐栈）→ `do_start`（架构无关，初始化 TLS 后调 `main`），由 `crt1.o` 等 relocatable 对象合并而成（u8-l2）。

两个本讲会用到的关键事实，先点明：

- 目标 OS 与架构不是手填的，而是构建系统从**目标三元组（target triple）**推导出来的；推导结果决定了去哪个 `config/` 子目录找配置。
- `entrypoints.txt` / `headers.txt` 本质是 **CMake 脚本**（用 `set()`、`list(APPEND ...)`），构建时由顶层 `CMakeLists.txt` 直接 `include()` 进来——所以它们既能列名单，也能写 `if()` 条件块。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [docs/porting.md](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/docs/porting.md) | 官方移植指南，本讲的主线骨架。讲清「建 config 目录 → 填 entrypoints.txt/headers.txt → 上游化的维护/CI/淘汰规则」。 |
| [libc/CMakeLists.txt](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/CMakeLists.txt) | 顶层构建脚本。决定如何从 `LIBC_TARGET_OS`/`LIBC_TARGET_ARCHITECTURE` 解析出 `LIBC_CONFIG_PATH`，并 `include()` 那里的 `entrypoints.txt`/`headers.txt`。 |
| [cmake/modules/LLVMLibCArchitectures.cmake](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/cmake/modules/LLVMLibCArchitectures.cmake) | 从目标三元组推导 `LIBC_TARGET_OS` 与 `LIBC_TARGET_ARCHITECTURE`，并设置 `LIBC_TARGET_OS_IS_LINUX` 等便捷变量。移植新 OS 时常需在此登记。 |
| [config/linux/x86_64/entrypoints.txt](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/config/linux/x86_64/entrypoints.txt) | 一个成熟平台的入口点名单范本，含 `if(FULL_BUILD)`、`if(EXPERIMENTAL)` 等条件块结构。 |
| [config/linux/x86_64/headers.txt](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/config/linux/x86_64/headers.txt) | 对应的公共头名单范本。 |
| [config/baremetal/aarch64/entrypoints.txt](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/config/baremetal/aarch64/entrypoints.txt) | 一个**最小化**真实移植的范本：只含不依赖 syscall 的函数族，是「第一波入口点」的最佳参照。 |
| [config/linux/x86_64/exclude.txt](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/config/linux/x86_64/exclude.txt) | 二阶裁剪范本：用 `try_compile`/`check_symbol_exists` 探测运行环境，把内核不支持的入口点排除。 |
| [src/__support/OSUtil/syscall.h](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/src/__support/OSUtil/syscall.h)、[linux/syscall.h](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/src/__support/OSUtil/linux/syscall.h)、[linux/x86_64/syscall.h](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/src/__support/OSUtil/linux/x86_64/syscall.h) | syscall 封装的三级分派链；移植时要为新 OS/架构补的就是这条链的最底层。 |
| [startup/linux/x86_64/start.cpp](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/startup/linux/x86_64/start.cpp) | 架构相关 `_start` 范本；移植新架构时要写一个对应的程序入口。 |

## 4. 核心概念与源码讲解

### 4.1 配置树搭建

#### 4.1.1 概念说明

「移植」的第一步，不是写代码，而是**让构建系统能找到你的平台**。LLVM-libc 把「某平台支持什么」全部外化到 `config/` 目录下，构建系统靠一个简单约定定位它：

- 若你移植的是一个**新 OS**，就在 `libc/config/` 下建一个以该 OS 命名的目录（如 `linux/`、`windows/`）。
- 若同一 OS 下**不同架构支持面不同**（最典型的就是 syscall 实现随架构变化），就在该 OS 目录下再建**架构子目录**（如 `config/linux/x86_64/`）。

这套约定之所以能工作，是因为顶层 `CMakeLists.txt` 会按「OS + 架构」拼出路径去找配置文件。所以「搭建配置树」本质就是「按命名约定建好目录，并放对文件」。

#### 4.1.2 核心流程

官方移植指南把这一步拆得很清楚：

1. **为新 OS 建目录**：在 `libc/config/` 下加一个 OS 目录。Linux 与 Windows 各有自己的目录，是目前活跃开发的两条线。
2. **决定是否需要架构子目录**：如果各架构支持面不同，就在 OS 目录下为每个目标架构建子目录；否则只在 OS 目录放一份配置即可。
3. **构建系统按目录名匹配**：libc 的 CMake 机制「寻找以目标架构命名的子目录」（looks for subdirectories named after the target architecture）。

这一流程见官方原文：

[docs/porting.md:8-14](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/docs/porting.md#L8-L14) —— 说明为新 OS 在 `libc/config` 下建目录。

[docs/porting.md:30-47](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/docs/porting.md#L30-L47) —— 说明何时需要架构子目录，并以 `config/linux/x86_64`、`config/linux/aarch64` 为例，点出「CMake 按架构名找子目录」这一关键机制。

#### 4.1.3 源码精读：构建系统如何定位你的配置

「按目录名匹配」不是一句空话，它落实在顶层 `CMakeLists.txt` 的路径解析逻辑里。这是移植者必须看懂的一段：

[libc/CMakeLists.txt:182-198](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/CMakeLists.txt#L182-L198) —— 这段做三件事：

1. **优先找架构子目录**：若 `config/${LIBC_TARGET_OS}/${LIBC_TARGET_ARCHITECTURE}` 存在，`LIBC_CONFIG_PATH` 指向它（架构粒度配置）。
2. **回退到 OS 目录**：否则若 `config/${LIBC_TARGET_OS}` 存在，`LIBC_CONFIG_PATH` 指向它（OS 粒度配置）。
3. **都不存在则致命错误**：若两处都没有且未手填 `LIBC_CONFIG_PATH`，直接 `FATAL_ERROR`。

这里有一个移植者常用的「逃生口」：变量 `LIBC_CONFIG_PATH`（[CMakeLists.txt:166](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/CMakeLists.txt#L166)）。你可以完全绕开 `<os>/<arch>` 命名约定，把自己的配置目录路径直接喂给它——这在「新 OS 还没在架构模块里登记、但想先跑通构建」的早期阶段非常有用。

那么 `LIBC_TARGET_OS` 与 `LIBC_TARGET_ARCHITECTURE` 又从哪来？它们由架构模块从**目标三元组**推导：

[cmake/modules/LLVMLibCArchitectures.cmake:123-159](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/cmake/modules/LLVMLibCArchitectures.cmake#L123-L159) —— 从三元组解析出 `libc_arch` 与 `libc_sys`，分别赋给 `LIBC_TARGET_ARCHITECTURE` / `LIBC_TARGET_OS`。

这里有两个对移植者极重要的「归一化」细节：

- 架构名会被规整。例如 `riscv64` 会被改成统一的目录名 `riscv`：

  [cmake/modules/LLVMLibCArchitectures.cmake:177-184](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/cmake/modules/LLVMLibCArchitectures.cmake#L177-L184) —— `riscv64`/`riscv32` 都被归一为 `LIBC_TARGET_ARCHITECTURE = "riscv"`。所以你的配置目录应叫 `config/<os>/riscv/`，而不是 `riscv64/`。

- OS 名也会被规整与分类：

  [cmake/modules/LLVMLibCArchitectures.cmake:161-164](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/cmake/modules/LLVMLibCArchitectures.cmake#L161-L164) —— 三元组里 `unknown`/`none` 系统被当作 `baremetal`。

  [cmake/modules/LLVMLibCArchitectures.cmake:198-219](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/cmake/modules/LLVMLibCArchitectures.cmake#L198-L219) —— 设置 `LIBC_TARGET_OS_IS_LINUX`/`_BAREMETAL`/`_GPU` 等便捷变量。

> **关键陷阱**：若你的新 OS 三元组解析出的 `LIBC_TARGET_OS` 既不是已知值，也没在这段 `if` 链里登记，那么**没有任何 `LIBC_TARGET_OS_IS_*` 会被置真**，顶层构建里大量 `if(LIBC_TARGET_OS_IS_LINUX)` 的分支都不会进——构建可能在别处以奇怪的方式失败。这就是为什么移植一个**真正全新**的 OS（而非 Linux 衍生）往往要先在 `LLVMLibCArchitectures.cmake` 加一行登记。`config/<os>/` 目录建好了只是「文件就位」，构建系统能不能正确识别这个 OS 还得看这里。

#### 4.1.4 代码实践

**实践目标**：亲手验证「构建系统如何从三元组定位到配置目录」，而不真正改任何源码。

**操作步骤**：

1. 在 `libc/` 下打开 [cmake/modules/LLVMLibCArchitectures.cmake](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/cmake/modules/LLVMLibCArchitectures.cmake)，找到第 169–196 行的架构归一化 `if` 链。
2. 对下列三个目标三元组，手动追踪 `get_arch_and_system_from_triple` 与归一化逻辑会得到什么 `LIBC_TARGET_OS` 与 `LIBC_TARGET_ARCHITECTURE`：
   - `x86_64-linux-gnu`
   - `aarch64-none-elf`（注意中间字段 `none`）
   - `riscv64-unknown-elf`
3. 对每个结果，推出 `LIBC_CONFIG_PATH` 会指向 `config/` 下的哪个目录（参考 [CMakeLists.txt:182-198](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/CMakeLists.txt#L182-L198)）。

**需要观察的现象**：

- `x86_64-linux-gnu` → OS=`linux`、架构=`x86_64` → `config/linux/x86_64/`（真实存在）。
- `aarch64-none-elf` → 因 `none` 被 [L161-164](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/cmake/modules/LLVMLibCArchitectures.cmake#L161-L164) 当成 baremetal → OS=`baremetal`、架构=`aarch64` → `config/baremetal/aarch64/`（真实存在）。
- `riscv64-unknown-elf` → OS=`baremetal`、架构被归一为 `riscv` → `config/baremetal/riscv/`（真实存在）。

**预期结果**：三个三元组都能落到仓库里**已存在**的配置目录——这说明仓库现有的 `config/` 树已经覆盖了这些目标。若你把某个三元组改成仓库没有的目录（例如假想的 `riscv64-myos-elf`），那么按 [CMakeLists.txt:194-195](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/CMakeLists.txt#L194-L195) 会得到 `FATAL_ERROR`，除非用 `LIBC_CONFIG_PATH` 指向自建目录。（构建命令的执行结果待本地验证；本实践重点是**纸面追踪路径解析**。）

#### 4.1.5 小练习与答案

**练习 1**：为什么移植者有时宁愿用 `LIBC_CONFIG_PATH` 手指配置目录，也不愿意立即去 `LLVMLibCArchitectures.cmake` 登记新 OS？

**参考答案**：早期 bring-up 阶段只想先验证「我的 config 文件写得对不对、能不能 include 进来」，还不愿改动公共的架构判定模块（那会影响所有人、需要更慎重的评审）。`LIBC_CONFIG_PATH` 是一个纯本地的覆盖开关，让你在自己的构建命令里指一个目录就能跑通配置加载，把「文件就位」与「OS 正式登记」两件事解耦。

**练习 2**：假设你要为 `riscv64` 移植到一个新 OS `myos`，应该把配置目录命名为 `config/myos/riscv64/` 还是 `config/myos/riscv/`？

**参考答案**：`config/myos/riscv/`。因为 [LLVMLibCArchitectures.cmake:177-184](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/cmake/modules/LLVMLibCArchitectures.cmake#L177-L184) 把 `riscv64`/`riscv32` 统一归一为 `riscv`，构建系统拼出来的路径用的是归一化后的名字。当然，前提是 `myos` 这个 OS 也已在 OS 归一化逻辑里被识别，否则还得用 `LIBC_CONFIG_PATH`。

### 4.2 入口点渐进填充

#### 4.2.1 概念说明

配置目录建好后，里面最关键的文件是 `entrypoints.txt`——它列出本平台要纳入构建的入口点全限定名（如 `libc.src.ctype.isalpha`），是「本平台支持哪些函数」的**事实来源**（见 u2-l4）。

「渐进式（progressive）」是这里的核心方法论：一个全新平台**不可能**一上来就把几百个入口点全列上——因为每一个被列上的入口点，背后都需要：(a) 它的实现文件存在；(b) 它依赖的 `__support` 子模块在本平台可用；(c) 它最终依赖的平台底层（syscall、startup）已就绪。所以官方指南明确说：bring-up 过程「随着目标被实现并测试，渐进地把它们加进这个文件」。

换句话说，`entrypoints.txt` 是一份**随移植进度增长的清单**：写进去 = 我保证它在这个平台上能编译、能跑、测过；写不进去就由 SKIP 机制变成空占位目标（u2-l3）。

#### 4.2.2 核心流程

`entrypoints.txt` 的标准写法是「一个 `set()` 起头 + 若干 `list(APPEND ...)` 条件块」：

1. `set(TARGET_LIBC_ENTRYPOINTS ...)` 列出**无条件**纳入的 libc 入口点。
2. 用 `if(LLVM_LIBC_FULL_BUILD)`、`if(LIBC_TYPES_HAS_FLOAT128)`、`if(LLVM_LIBC_ENABLE_EXPERIMENTAL_ENTRYPOINTS)` 等条件块，按构建模式/硬件能力/实验开关**追加**额外入口点。
3. 最后 `set(TARGET_LLVMLIBC_ENTRYPOINTS ${TARGET_LIBC_ENTRYPOINTS} ${TARGET_LIBM_ENTRYPOINTS} ...)` 把各库的名单合并成最终名单。

而顶层构建脚本对它的处理极其简单——直接 `include()`：

[libc/CMakeLists.txt:389-401](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/CMakeLists.txt#L389-L401) —— `entrypoints.txt` 必须存在（否则 `FATAL_ERROR`）；`headers.txt` 在 Full 模式下必须存在，Overlay 模式下可选。这就是为什么「在 `entrypoints.txt` 里加一行」真的会让一个函数进入构建：它被当作 CMake 脚本执行，直接修改了 `TARGET_LLVMLIBC_ENTRYPOINTS` 这个变量。

#### 4.2.3 源码精读：成熟平台与最小平台两个范本

**范本 A：成熟平台（Linux/x86_64）的条件块结构**

[config/linux/x86_64/entrypoints.txt:1-11](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/config/linux/x86_64/entrypoints.txt#L1-L11) —— 开头的 `set(TARGET_LIBC_ENTRYPOINTS ...)`，按头文件分组列出无条件入口点（这里展示 arpa/inet.h 段）。注意每个入口点都是点分全限定名 `libc.src.<头文件>.<函数>`。

[config/linux/x86_64/entrypoints.txt:1213-1215](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/config/linux/x86_64/entrypoints.txt#L1213-L1215) —— `if(LLVM_LIBC_FULL_BUILD)` 块。它把一大批**只在 Full 模式下才提供**的入口点（pthread、signal、stdio 的 fopen/fread/fwrite、stdlib 的 exit/atexit 等）追加进来。这些函数要么依赖系统调用、要么依赖启动流程，在 Overlay 模式下回退给系统 libc，所以用 Full 守卫圈起来。**这正是「渐进填充」要学习的模式：用条件块精确表达「这个函数在什么前提下才纳入」。**

[config/linux/x86_64/entrypoints.txt:530-535](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/config/linux/x86_64/entrypoints.txt#L530-L535) —— `if(LLVM_LIBC_INCLUDE_SCUDO)` 块，按是否启用 Scudo 分配器追加 `mallopt`。这示范了「按构建特性开关」追加入口点。

[config/linux/x86_64/entrypoints.txt:1627-1631](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/config/linux/x86_64/entrypoints.txt#L1627-L1631) —— 末尾把 libc/libm/libmvec 三份名单合并成 `TARGET_LLVMLIBC_ENTRYPOINTS`。这是每个 `entrypoints.txt` 都要有的收尾。

**范本 B：最小平台（baremetal/aarch64）——第一波入口点的最佳参照**

baremetal 是「无 OS、无 syscall」的裸机目标，它的 `entrypoints.txt` 是一份**只含不依赖系统调用的函数族**的清单，完美示范了「第一波该上什么」：

[config/baremetal/aarch64/entrypoints.txt:1-24](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/config/baremetal/aarch64/entrypoints.txt#L1-L24) —— 开头就是 `__assert_fail`、`__stack_chk_fail`（编译器需要）、整个 ctype 族——这些**纯算法、零 syscall** 的函数是任何平台最先能跑通的。

观察这份清单你会发现：它包含 ctype、string、stdlib 数值转换、math、stdio 的 `printf`/`sprintf`/`snprintf`（这些走内存缓冲，不一定要 syscall），但**几乎不含** `unistd`（read/write/close…）、`sys/*`（mmap/socket/…）这类需要系统调用的入口点。这正是「第一波优先实现 syscall 无关入口点」原则的活样本。

此外，baremetal 还登记了自己专属的启动入口点：

[config/baremetal/aarch64/entrypoints.txt:308-311](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/config/baremetal/aarch64/entrypoints.txt#L308-L311) —— `libc.startup.baremetal.init` 与 `libc.startup.baremetal.fini`。说明 bring-up 时启动对象也要作为一种「入口点」登记进同一份名单。

**配套的 headers.txt**

`headers.txt` 与 `entrypoints.txt` 平行，列出本平台要生成/安装的公共头。它只在 Full 模式强制（见上 [CMakeLists.txt:399-400](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/CMakeLists.txt#L399-L400)）。

[config/linux/x86_64/headers.txt:1-9](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/config/linux/x86_64/headers.txt#L1-L9) —— `set(TARGET_PUBLIC_HEADERS ...)` 列出点分头名（`libc.include.ctype` 等）。

[config/linux/x86_64/headers.txt:91-96](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/config/linux/x86_64/headers.txt#L91-L96) —— 用 `if(LLVM_LIBC_FULL_BUILD AND LLVM_LIBC_ENABLE_EXPERIMENTAL_ENTRYPOINTS)` 追加实验性头（regex、sys/ptrace）。说明头名单也支持条件块，且与 entrypoints 的实验开关**联动**——一个函数进了实验入口点名单，它的头也得进实验头名单。

#### 4.2.4 代码实践

**实践目标**：体验「渐进填充」的决策——判断一个入口点能不能安全地加进一个新平台的 `entrypoints.txt`。

**操作步骤**：

1. 打开 [config/baremetal/aarch64/entrypoints.txt](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/config/baremetal/aarch64/entrypoints.txt)，确认它**没有** `libc.src.unistd.write`、`libc.src.sys.mman.mmap`。
2. 思考：如果强行把 `libc.src.unistd.write` 加进 baremetal 的名单，会发生什么？沿着 `write` 的实现追踪它的依赖（提示：它最终会落到 u8-l1 讲的 syscall 封装链，而 baremetal 没有 OS、没有 syscall）。
3. 对照 [config/linux/x86_64/entrypoints.txt:414-461](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/config/linux/x86_64/entrypoints.txt#L414-L461) 的 unistd 段，确认这些函数在 Linux 上是有的——说明同一函数在不同平台的「是否纳入」完全由各平台 `entrypoints.txt` 自己决定。

**需要观察的现象**：一个入口点能否进某平台名单，不取决于函数实现写没写（实现可能本来就存在），而取决于**它的整条依赖链（尤其是平台底层）在该平台是否就绪**。

**预期结果**：你能用一句话概括「第一波入口点的筛选标准」——优先选不依赖 syscall 的纯算法函数（ctype/string/math/数值转换/内存缓冲型 printf）。实际编译验证待本地进行。

#### 4.2.5 小练习与答案

**练习 1**：`config/linux/x86_64/entrypoints.txt` 里 `pthread_*`、`fopen` 等函数为什么被包在 `if(LLVM_LIBC_FULL_BUILD)` 里，而不是无条件列出？

**参考答案**：这些函数依赖系统调用与启动流程提供的运行时（线程、文件描述符、TLS）。Overlay 模式不提供这些底层，而是回退给系统 libc；只有 Full 模式才由 LLVM-libc 自己实现整套底层，所以它们用 `if(LLVM_LIBC_FULL_BUILD)` 守卫，确保只在「我们自己当 libc」时才纳入。这也呼应 u1-l4 讲的两种模式产物差异。

**练习 2**：假如你在新平台实现并测好了 `memcpy`，下一步该改哪两个文件让它真正进入产物？

**参考答案**：(1) 在该平台的 `entrypoints.txt` 加一行 `libc.src.string.memcpy`；(2) 若是 Full 模式，还要在 `headers.txt` 确保 `libc.include.string` 已列出（否则公共头里不会生成 `memcpy` 声明）。两处都改完，配合 u2-l3 讲的 SKIP 机制，`memcpy` 才会从「空占位目标」变成「真正编译进 .a 的对象」。

### 4.3 平台底层补齐

#### 4.3.1 概念说明

只填 `entrypoints.txt` 是不够的。当名单里的函数开始依赖系统调用或程序启动时，你必须提供**平台底层**的最小实现集，否则函数虽被纳入构建，却在链接期找不到符号、或运行期崩溃。移植者要补的底层主要有两块：

1. **syscall 封装链的最底层**：移植到新 OS 时，要为新 OS 写一套 syscall 实现；移植到新架构时，要为新架构写一条陷入内核的汇编。
2. **启动对象（startup）**：移植到新 OS/架构时，要提供程序入口 `_start` 及 TLS 初始化等，让控制权能从内核/加载器交到 `main`。

这两块分别在 u8-l1、u8-l2 深入讲解；本讲只看「移植者要在哪里、以什么形式把它们补上」。

#### 4.3.2 核心流程

**syscall 封装链的三级分派**（移植新 OS/架构时补的是最底层）：

```
OSUtil/syscall.h            ← 第 1 级：按 OS 预定义宏选 OS
   └── linux/syscall.h      ← 第 2 级：按 LIBC_TARGET_ARCH_IS_* 选架构
          └── linux/<arch>/syscall.h  ← 第 3 级：具体陷入指令（syscall/svc/ecall）
```

移植时：

- **新 OS**：在第 1 级加一个 `#elif`，并新建 `<os>/syscall.h`（含第 2、3 级）。
- **新架构（已有 OS）**：在第 2 级加一个 `#elif`，并新建 `<os>/<arch>/syscall.h`（第 3 级）。

**启动对象的补齐**：

- 每个已有 OS/架构在 `startup/<os>/<arch>/` 下都有一个 `start.cpp`（架构相关的 `_start`）加 `tls.cpp`（TLS 初始化）等，由 `merge_relocatable_object` 用 `cc -r` 合并成 `crt1.o`（u8-l2）。
- 移植新架构时，照此结构为新架构写一个 `start.cpp` 与 `tls.cpp`。

#### 4.3.3 源码精读：syscall 三级分派链

**第 1 级——按 OS 选**：

[src/__support/OSUtil/syscall.h:17-23](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/src/__support/OSUtil/syscall.h#L17-L23) —— 用编译器预定义宏 `__APPLE__`/`__linux__`/`__FreeBSD__` 选 OS。移植新 OS 时，这里要加 `#elif defined(__myos__)` 并 `#include "myos/syscall.h"`。注意这层用的是**源码通道**（编译器预定义宏），与 4.1 讲的**构建通道**（CMake 的 `LIBC_TARGET_OS`）是两条独立的分派路径，但天然一致。

**第 2 级——按架构选 + 提供两个公共封装**：

[src/__support/OSUtil/linux/syscall.h:19-29](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/src/__support/OSUtil/linux/syscall.h#L19-L29) —— 用 `LIBC_TARGET_ARCH_IS_X86_64` 等宏选架构子头文件。移植 Linux 新架构时，这里加 `#elif defined(LIBC_TARGET_ARCH_IS_MYARCH)`。

[src/__support/OSUtil/linux/syscall.h:35-39](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/src/__support/OSUtil/linux/syscall.h#L35-L39) —— `syscall_impl` 变参模板：把参数转 `long` 后调架构版 `syscall_impl`，**不做错误检查**。

[src/__support/OSUtil/linux/syscall.h:48-56](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/src/__support/OSUtil/linux/syscall.h#L48-L56) —— `syscall_checked`：在 `syscall_impl` 之上判错，按「内核返回负 errno」约定取反成正 errno 包进 `Error`，返回 `ErrorOr`。这两个封装是 OS 级的、与架构无关，所以移植新架构时**不用重写它们**，只需提供第 3 级。

**第 3 级——架构相关陷入指令**：

[src/__support/OSUtil/linux/x86_64/syscall.h:24-31](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/src/__support/OSUtil/linux/x86_64/syscall.h#L24-L31) —— x86_64 的 `syscall_impl(long)`：把系统调用号放 `rax`，执行 `syscall` 指令，结果回 `rax`。它被标 `[[gnu::always_inline]]`（注释说明是为 CET 影子栈）。这就是移植 Linux 新架构时要照抄并改指令的部分——aarch64 用 `svc 0`、riscv 用 `ecall`，参数寄存器约定也不同，但骨架（一组按参数个数重载的 `syscall_impl`）完全一致。

> 移植 syscall 时，配合 `syscall_wrappers/*.h`（把每条系统调用包成返回 `ErrorOr` 的薄封装，供上层入口点消费），就完成了 u4-3 与 u8-1 讲的「内核负 errno → ErrorOr → 公开入口点设 `libc_errno` 并返回 -1」端到端错误传播链。

**启动对象范本**：

[startup/linux/x86_64/start.cpp:11-33](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/startup/linux/x86_64/start.cpp#L11-L33) —— 架构相关的 `_start`：从栈上取 `argc/argv` 写进全局 `app.args`，把栈指针对齐到 16 字节（x86_64 ABI 要求），然后跳进架构无关的 `do_start()`。移植新架构时，要写一个对应的 `_start`，正确完成「取栈参数 + 满足本架构栈对齐 ABI」这两件最关键的事，剩下的 TLS/构造函数/`main` 调用都由共享的 `do_start` 完成（详见 u8-l2）。

[startup/linux/x86_64/CMakeLists.txt:1-19](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/startup/linux/x86_64/CMakeLists.txt#L1-L19) —— 用 `add_startup_object` 分别注册 `tls`/`start`/`irelative` 三个对象，每个都带 `-ffreestanding -fno-builtin` 等裸编译选项，并经 `DEPENDS` 引用 `libc.config.app_h`（即 [config/linux/app.h](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/config/linux/app.h)）。移植者照此为新架构建一个同名 `CMakeLists.txt` 即可。

**应用描述结构**（启动与 syscall 都要用）：

[config/linux/app.h:18-62](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/config/linux/app.h#L18-L62) —— `TLSImage`（描述 TLS 镜像的地址/大小/对齐）、`Args`（argc/argv）、`AppProperties`（页面大小、参数、TLS、env）。移植新 OS 时通常要提供一份等价的 `app.h`，因为 `do_start` 与 TLS 初始化都依赖它（u8-l2）。

#### 4.3.4 代码实践

**实践目标**：把「平台底层最小实现集」具体化——为一个假想的新架构列出要新建的文件清单（只列清单，不写代码）。

**操作步骤**：假设你要把 **Linux** 移植到一个新架构 `myarch`（OS 已是 Linux，只缺这个架构）。请基于本节源码，列出需要新建/修改的文件：

1. 新建 `src/__support/OSUtil/linux/myarch/syscall.h`（参照 [linux/x86_64/syscall.h](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/src/__support/OSUtil/linux/x86_64/syscall.h)，把 `syscall` 指令换成 `myarch` 的陷入指令、改参数寄存器约定）。
2. 在 [linux/syscall.h:19-29](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/src/__support/OSUtil/linux/syscall.h#L19-L29) 加一行 `#elif defined(LIBC_TARGET_ARCH_IS_MYARCH)` 包含它。
3. 在 [LLVMLibCArchitectures.cmake](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/cmake/modules/LLVMLibCArchitectures.cmake) 的架构归一化链里登记 `myarch`（否则宏不会被定义）。
4. 新建 `startup/linux/myarch/start.cpp` 与 `tls.cpp` 及 `CMakeLists.txt`（参照 [startup/linux/x86_64/](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/startup/linux/x86_64/start.cpp)）。
5. 新建 `config/linux/myarch/entrypoints.txt`（从 ctype/string 等无 syscall 函数起步）与 `headers.txt`。

**需要观察的现象**：清单里**没有任何「业务函数」代码**——`memcpy`/`strlen`/`round` 这些函数的实现是跨架构共享的，移植新架构时不用碰它们；要补的全是「把控制权接进来（startup）」和「把请求递给内核（syscall）」这两类粘合层。

**预期结果**：你得出一个清晰结论——**平台底层的最小实现集 = 一条 syscall 陷入指令 + 一个 `_start` 入口 + 一份 `app.h`**，其余算法复用既有实现。这正体现了 LLVM-libc「平台无关算法 + 可替换平台底层」的 retargetable 设计（u1-l1）。

#### 4.3.5 小练习与答案

**练习 1**：移植 Linux 到新架构时，`syscall_checked`（带错误检查的封装）需要重写吗？

**参考答案**：不需要。[linux/syscall.h:48-56](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/src/__support/OSUtil/linux/syscall.h#L48-L56) 的 `syscall_checked` 是 OS 级、架构无关的，它建立在架构版 `syscall_impl` 之上。新架构只需提供自己的 `syscall_impl`（陷入指令 + 寄存器约定），上层封装自动复用。这正是三级分派设计带来的好处——每一层只换一种东西。

**练习 2**：为什么 `_start` 必须按架构单独写，而不能像 `do_start` 那样共享？

**参考答案**：因为内核/加载器把 `argc/argv/envp` 放在栈上的具体布局、以及「栈指针对齐到几字节」都是**架构 ABI 规定**的（x86_64 要 16 字节对齐，见 [start.cpp:18-30](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/startup/linux/x86_64/start.cpp#L18-L30)）。这些差异只在最入口处存在，一旦 `_start` 把参数规整进架构无关的 `AppProperties` 并对齐好栈，后续的 TLS 初始化、调 `main` 就与架构无关了，所以 `do_start` 可以共享。

### 4.4 bring-up 验证与上游化

#### 4.4.1 概念说明

前面三节解决了「怎么把一个平台跑起来」。但 LLVM-libc 是上游项目，把目标**合进主干（upstreaming）**有一套硬性治理规则，写在本讲的官方指南后半部分：维护者责任、CI 构建机、以及「淘汰（sunsetting）」机制。理解这些，才知道 bring-up 不是「能编译就算完」，而是「能持续保持不坏」。

#### 4.4.2 核心流程

官方指南给出的上游化三要求：

1. **维护者（Maintenance）**：必须有至少一人负责保持目标可用——坏了要修、相关 patch 要 review、CI 要跑起来。维护者列入 `libc/maintainers.md`。
2. **CI 构建机（CI builders）**：每个目标至少一台 CI，既用来发现目标何时坏掉，也帮助没有该架构硬件的人修 bug。LLVM-libc 同时有 GitHub presubmit 与 buildbot postsubmit。
3. **淘汰（Sunsetting）**：目标若长期坏（CI 失败超 30 天改非阻塞、再过 90 天可被淘汰）或长期无维护/无贡献（两个大版本间零贡献可标 deprecated，再一个大版本仍无动静可被淘汰），就会被移除。重启一个被淘汰的目标时，鼓励从「删除它的那次提交」入手找起点。

#### 4.4.3 源码精读

[docs/porting.md:77-93](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/docs/porting.md#L77-L93) —— Upstreaming 与 Maintenance：说明加一个目标需要维护者，维护者要修坏、review、保 CI。

[docs/porting.md:95-111](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/docs/porting.md#L95-L111) —— CI builders：每个目标至少一台 CI，并列出 Linux/Windows postsubmit 与 presubmit 的具体配置链接。

[docs/porting.md:113-136](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/docs/porting.md#L113-L136) —— Sunsetting：broken（30 天 + 90 天）与 stale（无维护/无贡献）两类淘汰条件，以及「重启时看删除提交」的建议。

**bring-up 中验证「真的能用」的二阶裁剪**

除了「能编译」，还要确认「在目标运行环境真的能跑」。`exclude.txt` 就是这套二阶验证的产物——它用 `try_compile`/`check_symbol_exists` 在配置期探测运行环境，把不支持的入口点剔除：

[config/linux/x86_64/exclude.txt:1-30](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/config/linux/x86_64/exclude.txt#L1-L30) —— 探测 `sys/random.h` 是否存在（不存在说明是老内核，连带着排除 `stat`/`getrandom`）；探测 `SYS_faccessat2` 是否存在（不存在则排除 `faccessat`）。移植新平台时，这种「按运行期能力裁剪」的探测也应逐步补上，让名单反映**真实可用面**而非「写死的一厢情愿」。

> 与 u2-l4 联动：`entrypoints.txt` 是一阶裁剪（按平台名单），`exclude.txt` 是二阶裁剪（按运行环境探测），二者共同决定最终 `TARGET_LLVMLIBC_REMOVED_ENTRYPOINTS`。bring-up 时通常先把名单写小、跑通，再逐步加 `exclude.txt` 探测来精修。

#### 4.4.4 代码实践

**实践目标**：理解「能编译」与「能用」的差距，以及 exclude 机制的必要性。

**操作步骤**：

1. 读 [config/linux/x86_64/exclude.txt:4-21](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/config/linux/x86_64/exclude.txt#L4-L21)：注意它用「`sys/random.h` 不存在」**推断**「这是老内核，可能也没有 statx 系统调用」，从而排除 `libc.src.sys.stat.stat`。
2. 回答：为什么不直接在 `entrypoints.txt` 里写死「老内核排除 stat」，而要用 `try_compile` 探测？
3. 读 sunsetting 段 [docs/porting.md:119-128](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/docs/porting.md#L119-L128)，思考：如果你为一个很冷门的目标做 bring-up 但无力长期维护，会发生什么？

**需要观察的现象**：移植不只是「让它能编译过一次」，而是要面对「不同内核版本/硬件能力/长期维护成本」等持续性问题。

**预期结果**：你能说出 exclude 用 `try_compile` 而非写死的理由——同一 OS 在不同版本/发行版上能力不同，写死会误伤；运行期探测能让构建**自适应**目标环境。你也能意识到：把一个无人维护的目标推上游，最终大概率被 sunset。

#### 4.4.5 小练习与答案

**练习 1**：一个目标的 postsubmit CI 连续失败 35 天，按官方规则接下来会发生什么？

**参考答案**：失败超 30 天后（[docs/porting.md:119-122](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/docs/porting.md#L119-L122)），CI 应被改为**不阻塞提交、失败不通知人**；若再持续 90 天仍坏，该目标**可能被 sunset**（从代码与构建系统中移除所有针对它的引用，并关停其 buildbot）。

**练习 2**：bring-up 阶段，`entrypoints.txt` 与 `exclude.txt` 的填写顺序应如何安排？

**参考答案**：先把 `entrypoints.txt` 写到**最小可跑集**（无 syscall 的纯算法函数，参照 baremetal 范本），让构建与基本测试通过；再随移植深入，逐步加需要 syscall/startup 的函数族；最后才补 `exclude.txt`，用 `try_compile`/`check_symbol_exists` 把「名义上列了但运行环境实际不支持」的函数剔掉。即「先让它跑起来，再让它跑对，最后让它跑得诚实」。

## 5. 综合实践

**任务**：为一个假想的新平台 **`myos/riscv64`**（一个自研类 Unix 嵌入式 OS，运行在 64 位 RISC-V 上）规划一份完整的 bring-up 文件清单与实施路线。

要求产出一份文档，包含：

1. **配置树**：列出要在 `config/` 下新建的目录与文件（提示：注意架构名归一化——目录该叫 `riscv` 还是 `riscv64`？参见 [LLVMLibCArchitectures.cmake:177-184](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/cmake/modules/LLVMLibCArchitectures.cmake#L177-L184)）。若 `myos` 尚未被 OS 归一化逻辑识别（参见 [L198-219](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/cmake/modules/LLVMLibCArchitectures.cmake#L198-L219)），说明你打算怎么处理（改架构模块登记 / 用 `LIBC_CONFIG_PATH`）。
2. **平台底层**：列出要新建的 syscall 文件（参照三级分派链，本平台陷入指令用 RISC-V 的 `ecall`）与 startup 文件（`_start` + `tls` + `app.h`），每个文件一句话说明它解决什么。
3. **第一波入口点**：从 [config/baremetal/aarch64/entrypoints.txt](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/config/baremetal/aarch64/entrypoints.txt) 借鉴，列出「第一波应优先实现哪一类入口点」并说明理由（提示：按「是否依赖 syscall」排序）。
4. **验证与上游化**：写出你打算用什么命令验证最小集能编译/测试，以及若要推上游需要准备哪三样东西（维护者、CI、对 sunsetting 的承诺）。

**评判标准**（自检）：

- 目录命名是否考虑了 `riscv` 归一化（而不是想当然写成 `riscv64`）。
- 第一波入口点是否**全是 syscall 无关函数**（ctype/string/math/数值转换/内存缓冲型 printf），把 `read`/`write`/`mmap` 等留到 syscall 底层就绪后再加。
- 是否识别出 `myos` 是个 OS 归一化逻辑里没有的新值，并给出处理方案。
- 是否区分了「源码通道分派（`#ifdef __myos__`）」与「构建通道分派（`LIBC_TARGET_OS`）」两条路径都要照顾到。

> 这是一道**纯设计题**，不需要也不能在本仓库里真改源码。产出是一份规划文档，可写在本地笔记里。所有命令的运行结果待本地验证。

## 6. 本讲小结

- **移植第一步是让构建系统找得到你**：在 `config/<os>/<arch>/` 按命名约定建目录，构建系统由 [CMakeLists.txt:182-198](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/CMakeLists.txt#L182-L198) 据目标三元组推导的 `LIBC_TARGET_OS`/`LIBC_TARGET_ARCHITECTURE` 定位它；命名要遵循架构归一化（如 `riscv64`→`riscv`）。
- **`entrypoints.txt` 是渐进增长的清单**：用 `set()` + `if()` 条件块表达「本平台在什么前提下支持哪些函数」，由顶层 `CMakeLists.txt` 直接 `include()`；写进去等于承诺「能编译、能跑、测过」。baremetal 配置是「第一波（syscall 无关）入口点」的最佳范本。
- **平台底层最小实现集很薄**：一条 syscall 陷入指令（三级分派链的最底层）+ 一个架构相关 `_start` + 一份 `app.h`；其余算法跨架构复用，这正是 retargetable 设计的体现。
- **两条分派通道要同时照顾**：源码通道（`OSUtil/syscall.h` 的 `#ifdef`）与构建通道（CMake 的 `LIBC_TARGET_OS_IS_*`）各自独立却必须一致；移植全新 OS 常需在 `LLVMLibCArchitectures.cmake` 登记。
- **「能编译」≠「能用」**：`exclude.txt` 用 `try_compile`/`check_symbol_exists` 做运行环境二阶裁剪，让名单反映真实可用面。
- **上游化是长期承诺**：维护者 + CI + 接受 sunsetting 规则，三者缺一不可；无人维护的目标最终会被淘汰。

## 7. 下一步学习建议

- 想动手把一个**新函数**贡献进现有平台（而不是移植整个平台）？进入 **u11-l3 贡献一个完整新函数：端到端实战**，它把 YAML 规范、实现、CMake 注册、`entrypoints.txt`、测试串成一次真实贡献。
- 想了解非 Linux 的特殊目标（GPU/baremetal/UEFI）怎么构建与启动？进入 **u11-l2 特殊目标：GPU、baremetal 与 UEFI**，它讲解这三类目标的构建差异与各自 startup。
- 想深入本讲引用的平台底层机制？复习 **u8-l1（OSUtil 与 syscall 封装）** 与 **u8-l2（程序启动与 TLS）**——本讲只讲了「移植者要补什么」，这两讲讲清「补的那层内部如何工作」。
- 想复习配置树四文件的职责分工？回到 **u2-l4 平台配置体系**，本讲的「渐进填充」正是建立在其「`entrypoints.txt` 是支持范围事实来源」这一结论之上。

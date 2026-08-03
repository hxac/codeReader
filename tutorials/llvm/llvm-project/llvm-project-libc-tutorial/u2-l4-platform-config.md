# 平台配置体系：entrypoints.txt 与 config 树

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 `config/<os>/<arch>/` 这棵「配置树」由哪几类文件组成，以及它们各自回答什么问题。
- 解释 `entrypoints.txt` 为何被称为「某平台支持哪些函数」的**事实来源（source of truth）**，并看懂它如何用 `if(...)` 条件块做一阶裁剪。
- 看懂 `exclude.txt` 与 `headers.txt` 如何在 `entrypoints.txt` 之上做二阶裁剪与头文件生成。
- 掌握 `config.json` 三层覆盖加载机制，以及它如何把「平台默认值」变成一个个 CMake 变量影响实现选择。
- 在阅读任意函数时，能判断它在某个平台「是否进产物」「是否被排除」「头文件是否生成」「实现细节如何被配置」。

## 2. 前置知识

本讲紧接 **u2-l3（CMake 构建规则详解）**。在那一讲里，你已经知道：

- `add_entrypoint_object` 把单个函数注册成构建目标，但它是否**真正编译**取决于一个叫 `TARGET_ENTRYPOINT_NAME_LIST` 的名单——函数短名不在名单里，就只造一个空壳目标（SKIP 机制）。
- `add_entrypoint_library` 把显式列出的入口点聚合成 `libc.a` / `libm.a` / `libllvmlibc.a`。

那么一个自然的问题就出现了：**这份 `TARGET_ENTRYPOINT_NAME_LIST` 名单，以及聚合时要引用的那些入口点全限定名，到底是从哪里来的？** 答案就是本讲的主题——平台配置树。本讲补上 u2-l3 故意留白的那一环，把「构建侧如何消费名单」接到「配置侧如何产生名单」。

你还需要回忆 u1-l4 引入的概念：**Full 模式**（`LLVM_LIBC_FULL_BUILD=ON`，完整替换 libc）与 **Overlay 模式**（开关 OFF，仅覆盖少数符号）。配置树里的很多条件判断都以这个开关为分水岭。

> 关键直觉：传统 libc 的「平台支持范围」散落在 `#ifdef` 与 `Makefile` 各处；LLVM-libc 把它**外化成一组纯数据/纯 CMake 脚本文件**，于是「换一个平台」≈「换一棵 config 树」，源码本身尽量保持平台无关。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `config/linux/x86_64/entrypoints.txt` | 该平台**支持哪些入口点**的事实来源，用 CMake `set()`/`list()` 列出全限定名 |
| `config/linux/x86_64/exclude.txt` | 在运行环境特性探测后，把**不支持的入口点**追加到移除名单 |
| `config/linux/x86_64/headers.txt` | 该平台**要生成/安装哪些公共头文件**的名单 |
| `config/config.json` | **全局默认**配置项（一组 CMake 变量的默认值） |
| `config/linux/config.json` | Linux 平台对全局默认的**覆盖** |
| `CMakeLists.txt` | 读取上述文件的「总调度」，解析路径、include、应用移除、推导短名名单 |
| `cmake/modules/LibcConfig.cmake` | 解析 `config.json` 的工具函数（`read_libc_config` / `load_libc_config`） |
| `cmake/modules/LLVMLibCObjectRules.cmake` | SKIP 机制：用 `TARGET_ENTRYPOINT_NAME_LIST` 决定入口点是否真正编译 |

## 4. 核心概念与源码讲解

本讲按四个最小模块展开：`entrypoints.txt`、`exclude.txt`、`headers.txt`、`config.json`。前三者是「清单」类文件（决定**支持范围**），第四者是「旋钮」类文件（决定**实现细节**）。

### 4.1 entrypoints.txt：平台支持范围的事实来源

#### 4.1.1 概念说明

`entrypoints.txt` 回答的问题是：**在「这个 OS + 这个架构 + 这种构建模式」下，LLVM-libc 打算对外提供哪些入口点？**

它不是被构建系统「统计」出来的，而是**由维护者手工声明**的。也就是说，某个函数即便在 `src/` 下有完整实现、有 CMake 注册、有单元测试，只要它没有出现在目标平台的 `entrypoints.txt` 里，它就不会进入该平台的最终产物（`libc.a` 等）。

这一点正是 u2-l1 讲过的「入口点三阶段生命周期」的第三阶段——**配置（WHETHER）**。前两阶段（实现 HOW、注册 HOW）让一个函数「可以被构建」，而 `entrypoints.txt` 决定它「是否真的被构建进某个平台的库」。

#### 4.1.2 核心流程

`entrypoints.txt` 本身就是一段 CMake 脚本（注意它的后缀是 `.txt` 但内容是 CMake），被主 `CMakeLists.txt` 在配置阶段 `include()` 进来。它的工作流程是：

```text
1. set(TARGET_LIBC_ENTRYPOINTS ...)        # 先放 libc 的入口点（基础集合）
2. if(LLVM_LIBC_INCLUDE_SCUDO)             # 条件块：按开关/类型支持追加
      list(APPEND TARGET_LIBC_ENTRYPOINTS ...)
3. set(TARGET_LIBM_ENTRYPOINTS ...)        # 再放 libm 的入口点
4. if(LIBC_TYPES_HAS_FLOAT16) ... endif()  # 按硬件类型支持继续追加
5. if(LLVM_LIBC_FULL_BUILD)                # Full 模式独有入口点（locale/pthread/...）
      list(APPEND TARGET_LIBC_ENTRYPOINTS ...)
6. if(LLVM_LIBC_ENABLE_EXPERIMENTAL_ENTRYPOINTS)  # 实验性入口点
7. set(TARGET_LLVMLIBC_ENTRYPOINTS         # 汇总：合并 libc + libm + libmvec
        ${TARGET_LIBC_ENTRYPOINTS}
        ${TARGET_LIBM_ENTRYPOINTS}
        ${TARGET_LIBMVEC_ENTRYPOINTS})
```

这份汇总名单 `TARGET_LLVMLIBC_ENTRYPOINTS` 之后被两条路径消费：

- **聚合路径**：`lib/CMakeLists.txt` 把它（或其子集）喂给 `add_entrypoint_library`，组装出静态归档（u2-l3 已讲）。
- **SKIP 路径**：主 `CMakeLists.txt` 从每个全限定名里取末尾短名，组装成 `TARGET_ENTRYPOINT_NAME_LIST`，再交给 `add_entrypoint_object` 判定是否真正编译。

全限定名（FQ 名）取短名的规则就是「最后一个 `.` 之后的部分」——`libc.src.ctype.isalpha` 的短名就是 `isalpha`。这与 u2-l1 讲的「点分路径取末尾分量」完全一致。

#### 4.1.3 源码精读

**① 主 `CMakeLists.txt` 在配置阶段 `include` 该文件**（路径由 `LIBC_CONFIG_PATH` 指向，4.4 节会讲它怎么算出来）：

[CMakeLists.txt:384-401](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/CMakeLists.txt#L384-L401) —— 读入三件套：`entrypoints.txt` 必须存在（否则 `FATAL_ERROR`），`exclude.txt` 与 `headers.txt` 可选。

**② `entrypoints.txt` 开头：声明 `TARGET_LIBC_ENTRYPOINTS` 基础集合**，每个头文件一段、带注释：

[config/linux/x86_64/entrypoints.txt:12-28](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/config/linux/x86_64/entrypoints.txt#L12-L28) —— `ctype.h` 下注册了 16 个基础入口点（`isalnum`…`toupper`）。注意命名是点分全限定名 `libc.src.ctype.isalpha`，与 `src/ctype/isalpha.cpp` 的目录路径一一对应（u1-l2 讲过的「四件套坐标」）。

**③ 条件追加**：按硬件类型支持追加，例如 `_Float16` 数学函数只在编译器支持该类型时才进名单：

[config/linux/x86_64/entrypoints.txt:825-826](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/config/linux/x86_64/entrypoints.txt#L825-L826) —— `if(LIBC_TYPES_HAS_FLOAT16)` 守卫，把一整批 `*f16` 函数 `APPEND` 进 `TARGET_LIBM_ENTRYPOINTS`。这些 `LIBC_TYPES_HAS_*` 变量是更早的编译特性探测阶段设置的。

**④ Full 模式专属入口点**：locale 版（`isalpha_l` 等）、`pthread_*`、带状态的标准库函数（`fopen`/`exit`/`signal`…）只在 Full 模式下提供：

[config/linux/x86_64/entrypoints.txt:1213-1216](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/config/linux/x86_64/entrypoints.txt#L1213-L1216) —— `if(LLVM_LIBC_FULL_BUILD)` 块开头追加 ctype 的 `_l` 变体（共 14 个）。结合 u1-l4：Overlay 模式下这些函数**不会**进入 `libllvmlibc.a`，因为它们依赖 libc 私有 ABI 或 locale 状态。

**⑤ 汇总成最终名单**：

[config/linux/x86_64/entrypoints.txt:1627-1631](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/config/linux/x86_64/entrypoints.txt#L1627-L1631) —— `set(TARGET_LLVMLIBC_ENTRYPOINTS ${TARGET_LIBC_ENTRYPOINTS} ${TARGET_LIBM_ENTRYPOINTS} ${TARGET_LIBMVEC_ENTRYPOINTS})`。这就是该平台「对外提供入口点全集」的单一事实来源。

**⑥ 消费端：从全限定名推导短名名单**（喂给 SKIP 机制）：

[CMakeLists.txt:415-425](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/CMakeLists.txt#L415-L425) —— 对每个全限定名，找到最后一个 `.` 的位置，取其后缀作为短名，组装出 `TARGET_ENTRYPOINT_NAME_LIST`。

**⑦ SKIP 机制真正用上它**：

[cmake/modules/LLVMLibCObjectRules.cmake:179-181](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/cmake/modules/LLVMLibCObjectRules.cmake#L179-L181) —— `list(FIND TARGET_ENTRYPOINT_NAME_LIST ${ADD_ENTRYPOINT_OBJ_NAME} entrypoint_name_index)`，找不到（`EQUAL -1`）就只 `add_custom_target` 建一个空壳，不编译。这条线把「配置树里的名单」和「构建系统是否编译某函数」牢牢焊在一起。

#### 4.1.4 代码实践

**实践目标**：亲手验证「函数实现存在 ≠ 进入产物」。

**操作步骤**：

1. 在 `config/linux/x86_64/entrypoints.txt` 里找到 `ctype.h` 段（约第 12–28 行），数一数基础入口点的数量。
2. 再去 `src/ctype/` 目录看实现文件（`ls src/ctype/*.cpp`），你会发现实现数量比 `entrypoints.txt` 里列出的多——多出来的就是「实现了但没在本平台注册」的。
3. 例如 `isalpha_l` 的实现 `src/ctype/isalpha_l.cpp` 存在，但它只出现在 `if(LLVM_LIBC_FULL_BUILD)` 块里。对照 u1-l4，说明 Overlay 模式构建时它会被 SKIP 成空壳。

**需要观察的现象**：实现文件个数 > `entrypoints.txt` 在 Overlay 模式下实际启用的入口点个数。

**预期结果**：基础 ctype 入口点为 16 个；带 `_l` 后缀的 14 个仅在 Full 模式出现；Overlay 模式下后者对应的 `add_entrypoint_object` 不会真正编译。

#### 4.1.5 小练习与答案

**练习 1**：为什么 LLVM-libc 不直接「扫描 `src/` 自动注册所有函数」，而要维护一份手工 `entrypoints.txt`？

**参考答案**：因为「能实现一个函数」和「在某平台/某模式下应当对外提供它」是两件事。同一个实现，Full 模式可以提供 `fopen`（自带 `FILE` 私有布局），Overlay 模式就不行（会和系统 libc 的 `FILE` 布局冲突，见 u1-l4）。手工名单让维护者对「平台支持范围」有显式、可审查的控制，也支撑新平台「先实现少数函数、逐步填充」的渐进式 bring-up。

**练习 2**：全限定名 `libc.src.sys.socket.recvmmsg` 的短名是什么？这个短名会被用在哪里？

**参考答案**：短名是 `recvmmsg`（最后一个 `.` 之后的部分）。它会被加入 `TARGET_ENTRYPOINT_NAME_LIST`，供 `add_entrypoint_object` 里的 SKIP 判断使用——只有短名在名单里，对应的 `src/sys/socket/recvmmsg.cpp` 才会真正编译。

---

### 4.2 exclude.txt：基于运行环境的二阶裁剪

#### 4.2.1 概念说明

`entrypoints.txt` 解决的是「**平台/构建模式**层面支持哪些函数」——这是一阶裁剪，答案是静态的。但还有一类裁剪是**动态**的：同一个 OS（比如 Linux），不同内核版本、不同系统头文件，能提供的系统调用并不一样。

`exclude.txt` 就负责这一层：它在配置阶段做**特性探测**（编译小程序、查符号是否存在），一旦发现当前运行环境缺某个能力，就把依赖该能力的入口点追加到移除名单 `TARGET_LLVMLIBC_REMOVED_ENTRYPOINTS`。

> 直觉：`entrypoints.txt` 是「我们打算支持」，`exclude.txt` 是「打算支持、但当前环境实际不行，先撤掉」。

#### 4.2.2 核心流程

```text
1. 主 CMakeLists.txt: if(EXISTS ".../exclude.txt") include(it)   # 可选文件
2. exclude.txt 内部：
   a. try_compile(has_sys_random ...)        # 编一段小程序探测头文件
      if(NOT has_sys_random)
          list(APPEND TARGET_LLVMLIBC_REMOVED_ENTRYPOINTS libc.src.sys.stat.stat ...)
      b. check_symbol_exists(SYS_faccessat2 "sys/syscall.h" ...)
         if(NOT HAVE_SYS_FACCESSAT2)
             list(APPEND ... libc.src.unistd.faccessat)
3. 主 CMakeLists.txt:
   foreach(removed IN TARGET_LLVMLIBC_REMOVED_ENTRYPOINTS)
       list(REMOVE_ITEM TARGET_*_ENTRYPOINTS ${removed})   # 从汇总名单里删
   endforeach()
```

删除发生在「汇总名单已经组装完成之后」，所以它对四个列表（`TARGET_LLVMLIBC_ENTRYPOINTS`/`TARGET_LIBC_ENTRYPOINTS`/`TARGET_LIBM_ENTRYPOINTS`/`TARGET_LIBMVEC_ENTRYPOINTS`）统一生效，后续推导出的短名名单 `TARGET_ENTRYPOINT_NAME_LIST` 自然也就不再包含被排除的函数——SKIP 机制会让它们的 `add_entrypoint_object` 退化为空壳。

#### 4.2.3 源码精读

**① exclude.txt 顶部说明它「可选且用于排除」**：

[config/linux/x86_64/exclude.txt:1-2](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/config/linux/x86_64/exclude.txt#L1-L2) —— "This optional file is used to exclude entrypoints/headers for specific targets."

**② 用 `try_compile` 探测 `sys/random.h` 是否存在**，作为「内核是否够新」的代理判断（够新 ⇒ 同时拥有 `statx` 系统调用）：

[config/linux/x86_64/exclude.txt:5-21](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/config/linux/x86_64/exclude.txt#L5-L21) —— 老内核（无 `sys/random.h`）下，移除 `libc.src.sys.stat.stat`；并且在**非 Full 构建**（Overlay）下额外移除 `libc.src.sys.random.getrandom`。注意 Full 模式下不移除 `getrandom`，因为 Full 模式会自带 `sys/random.h` 头文件（见代码内注释 "If we're doing a fullbuild we provide the random header ourselves."）。

**③ 用 `check_symbol_exists` 探测 `SYS_faccessat2`**：

[config/linux/x86_64/exclude.txt:23-30](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/config/linux/x86_64/exclude.txt#L23-L30) —— 系统头里没有 `SYS_faccessat2` 就移除 `libc.src.unistd.faccessat`，并打印一条 `VERBOSE` 日志。

**④ 主 `CMakeLists.txt` 把移除名单真正作用到四个列表上**：

[CMakeLists.txt:405-413](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/CMakeLists.txt#L405-L413) —— `foreach` 遍历 `TARGET_LLVMLIBC_REMOVED_ENTRYPOINTS`，对四个 `TARGET_*_ENTRYPOINTS` 列表分别 `list(REMOVE_ITEM ...)`。

#### 4.2.4 代码实践

**实践目标**：理解「同名函数在不同运行环境/构建模式下命运不同」。

**操作步骤**：

1. 阅读 `exclude.txt` 第 11–21 行。
2. 回答：假设你在一台很老的 Linux（没有 `sys/random.h`）上分别做 **Full 构建**和 **Overlay 构建**，`getrandom` 这个函数在两种构建下分别会不会被排除？为什么？
3. 再看第 23–30 行：`faccessat` 被排除的条件是什么？它的排除与构建模式有关吗？

**需要观察的现象**：同一份 `exclude.txt`，因 `LLVM_LIBC_FULL_BUILD` 开关不同，对 `getrandom` 的处置不同；而 `faccessat` 的处置与该开关无关。

**预期结果**：
- Full 构建：老内核下 `getrandom` **不**被排除（Full 自带头文件），但 `stat` 被排除。
- Overlay 构建：老内核下 `getrandom` **与** `stat` 都被排除。
- `faccessat` 仅取决于 `SYS_faccessat2` 符号是否存在，与构建模式无关。

#### 4.2.5 小练习与答案

**练习 1**：`exclude.txt` 里的探测（`try_compile` / `check_symbol_exists`）针对的是「目标平台」还是「构建主机」？这会带来什么隐患？

**参考答案**：这些 CMake 探测命令默认在**构建主机**上编译/查找符号，因此它准确的前提是「构建主机 ≈ 目标运行环境」。交叉编译到一个内核版本不同的目标时，主机有 `SYS_faccessat2` 不代表目标也有，这会带来误判隐患。这也是交叉编译场景下有时需要用 `LIBC_CONFIG_PATH` 手工指定一份预先准备好的配置树的原因。

**练习 2**：为什么 `exclude.txt` 选择 `APPEND` 到一个单独的 `TARGET_LLVMLIBC_REMOVED_ENTRYPOINTS` 列表，再由主 `CMakeLists.txt` 统一 `REMOVE_ITEM`，而不是在 `exclude.txt` 里直接修改 `TARGET_LIBC_ENTRYPOINTS`？

**参考答案**：分离「收集」与「应用」两步，让一份移除名单能被统一地作用到 libc / libm / libmvec / 汇总四个列表上（见 `CMakeLists.txt:405-413`），避免在 `exclude.txt` 里重复写四遍删除逻辑，也便于将来插入「预置配置（premade config）」贡献自己的 exclude 列表（代码里 `CMakeLists.txt:403` 的 TODO 即指向此）。

---

### 4.3 headers.txt：公共头文件名单与生成

#### 4.3.1 概念说明

`headers.txt` 回答的是：**这个平台要对外提供哪些公共头文件？** 它列出的是头文件的点分名（如 `libc.include.ctype`，对应 `include/ctype.yaml` 规范），而不是函数。

这份名单有两个用途：

1. **驱动头文件生成管线**：u3-l1 会讲公共头不是手写、而是由 `include/*.yaml` 经 hdrgen 生成。`headers.txt` 决定「哪些 yaml 该被生成、安装」。
2. **约束 Full 模式**：Full 模式必须自给自足地提供所有头文件，所以 Full 模式下 `headers.txt` 缺失会直接 `FATAL_ERROR`；Overlay 模式下它是可选的（因为可以回退到系统头文件，见 u1-l4 的代理头机制）。

#### 4.3.2 核心流程

```text
1. 主 CMakeLists.txt:
   if(EXISTS ".../headers.txt")
       include(".../headers.txt")
   elseif(LLVM_LIBC_FULL_BUILD)
       message(FATAL_ERROR "... headers.txt not found and fullbuild requested.")
2. headers.txt: set(TARGET_PUBLIC_HEADERS libc.include.ctype ...)
3. 条件追加：
   if(LLVM_LIBC_FULL_BUILD AND LLVM_LIBC_ENABLE_EXPERIMENTAL_ENTRYPOINTS)
       list(APPEND TARGET_PUBLIC_HEADERS libc.include.regex libc.include.sys_ptrace)
4. 下游（include/CMakeLists.txt + hdrgen）：据此生成并安装头文件（u3-l1 详讲）
```

#### 4.3.3 源码精读

**① 主 `CMakeLists.txt` 的 Full 模式强制要求**：

[CMakeLists.txt:391-396](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/CMakeLists.txt#L391-L396) —— `headers.txt` 不存在时，只有 Full 模式报致命错误；Overlay 模式静默跳过（因为可回退系统头）。这与 u1-l4 「Full 必须自生成头文件、Overlay 回退系统」完全呼应。

**② `headers.txt` 主体是一长串点分头文件名**：

[config/linux/x86_64/headers.txt:1-8](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/config/linux/x86_64/headers.txt#L1-L8) —— `set(TARGET_PUBLIC_HEADERS libc.include.alloca libc.include.arpa_inet ...)`，`libc.include.ctype` 在第 8 行。注意头文件名与函数不同：头文件名是 `libc.include.<header>`，而入口点是 `libc.src.<header>.<func>`。

**③ 实验性头文件的条件追加**：

[config/linux/x86_64/headers.txt:91-96](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/config/linux/x86_64/headers.txt#L91-L96) —— 仅当 Full 构建**且**启用实验性入口点时，才把 `regex`、`sys_ptrace` 两个头文件加进来。这解释了为什么 regex 在默认构建里「头文件都不存在」——它要同时满足两个开关。

#### 4.3.4 代码实践

**实践目标**：建立「入口点名单」与「头文件名单」是两份独立清单的直觉。

**操作步骤**：

1. 在 `headers.txt` 里找到 `ctype` 对应的行（`libc.include.ctype`）。
2. 在 `entrypoints.txt` 里找到 ctype 段，确认它列的是**函数**（`libc.src.ctype.isalpha`）。
3. 思考：如果某平台只想要 ctype 的头文件，却不想提供任何一个 ctype 函数实现，配置上能做到吗？

**需要观察的现象**：两份清单是解耦的——头文件名单管「声明」，入口点名单管「实现」。

**预期结果**：理论上可以只 `libc.include.ctype` 进 `headers.txt` 而不注册任何 `libc.src.ctype.*`；Overlay 模式正是类似思路——部分头来自系统，部分实现来自 LLVM-libc。但通常两份清单会保持一致，避免「声明了却没实现」。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `regex.h` 头文件要同时受 `LLVM_LIBC_FULL_BUILD` 和 `LLVM_LIBC_ENABLE_EXPERIMENTAL_ENTRYPOINTS` 两个开关约束？

**参考答案**：regex 实现目前「已知不完整」（experimental），项目不愿让普通构建误以为它可用；同时 regex 依赖 libc 私有 ABI 与 locale 等设施，无法在 Overlay 模式下提供。所以必须「Full（能提供）+ experimental（用户知情同意）」双条件全满足，它才进头文件名单。

**练习 2**：在 Overlay 模式下删掉 `headers.txt` 会发生什么？为什么？

**参考答案**：不会报错（`CMakeLists.txt:391-396` 的 `elseif` 只在 Full 模式触发 `FATAL_ERROR`）。Overlay 模式本就允许回退到系统头文件（u1-l4、u3-l2 的代理头机制），所以公共头名单可以缺失。但这意味着 Overlay 模式不会自生成这些头，而是让 `hdr/` 代理头把类型/宏转发给系统头。

---

### 4.4 config.json：实现细节的三层配置旋钮

#### 4.4.1 概念说明

前三类文件（`entrypoints.txt` / `exclude.txt` / `headers.txt`）管的是「**范围**」——某函数/某头在不在产物里。`config.json` 管的是「**细节**」——某个进了产物的函数，**用什么算法实现**、**开哪些可选特性**。

`config.json` 里每一项都是一个 CMake 变量（如 `LIBC_CONF_PRINTF_DISABLE_FLOAT`），它会被实现代码里的宏读取，从而在编译期选择不同的实现路径。这是「配置驱动实现」的典型手法。

`config.json` 最关键的设计是**三层覆盖 + 命令行最高优先**：

| 层级 | 文件 | 角色 |
| --- | --- | --- |
| 第 1 层（全局默认） | `config/config.json` | 所有平台共享的默认值 |
| 第 2 层（OS 覆盖） | `config/<os>/config.json` | 某操作系统的偏好 |
| 第 3 层（架构覆盖） | `config/<os>/<arch>/config.json` | 某架构的微调 |
| 顶层（命令行） | `cmake -DXXX=...` | 一次性覆盖，**不被任何 config.json 改写** |

注意：`config/linux/x86_64/` 目录下**没有** `config.json`（只有三个 `.txt`），所以 x86_64 实际只用到第 1 层 + 第 2 层（`config/linux/config.json`）。第 3 层在 `config/linux/arm/`、`config/linux/riscv/` 等目录才存在。

#### 4.4.2 核心流程

```text
主 CMakeLists.txt 的三步加载（见 CMakeLists.txt:203-247）：
1. read_libc_config(config/config.json ...)   # 读全局默认，逐项 set 变量
   （已被命令行 -D 设置的变量跳过，并记入 cmd_line_conf 名单）
2. foreach config_path in {config/<os>, config/<os>/<arch>}:
       load_libc_config(<path>/config.json, ${cmd_line_conf})  # 覆盖，命令行项豁免
3. generate_config_doc(config/config.json .../configure.rst)  # 顺带生成文档

LibcConfig.cmake 内部：
- read_libc_config: file(READ) + string(JSON ...) 把 JSON 解析成「{option: {value, doc}}」列表
- load_libc_config: 对每个 option，要求同名变量已存在（否则报 invalid），再覆盖其 value
```

优先级链是「**命令行 > 架构 > OS > 全局**」：后加载者覆盖先加载者，但所有 config.json 都对命令行已设置的变量「绕行」（`cmd_line_conf` 名单豁免）。

#### 4.4.3 源码精读

**① 路径与三层加载的总注释**：

[CMakeLists.txt:203-211](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/CMakeLists.txt#L203-L211) —— 注释明确说明三步加载顺序，并强调「CMake 命令行优先于 config.json」。

**② 构建待加载路径列表 `LIBC_CONFIG_JSON_FILE_LIST`**（同时它也决定了 `LIBC_CONFIG_PATH`，即 `.txt` 文件的所在目录）：

[CMakeLists.txt:177-187](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/CMakeLists.txt#L177-L187) —— 若 `config/<os>/<arch>` 目录存在，就把它追加进加载列表，并把 `LIBC_CONFIG_PATH` 指向它；否则回退到 `config/<os>`。用户也可用 `-DLIBC_CONFIG_PATH=...` 完全自定义。

**③ 第 1 层：加载全局 `config/config.json`**，跳过命令行已设置的项：

[CMakeLists.txt:212-238](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/CMakeLists.txt#L212-L238) —— `read_libc_config` 读全局默认；循环里若 `if(DEFINED ${opt_name})`（即命令行已设），则把该项记入 `cmd_line_conf` 并 `continue`，不覆盖。

**④ 第 2、3 层：循环加载 OS / 架构 config.json**，把 `cmd_line_conf` 作为「豁免名单」传入：

[CMakeLists.txt:241-247](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/CMakeLists.txt#L241-L247) —— `load_libc_config(${config_path}/config.json ${cmd_line_conf})`。

**⑤ `load_libc_config` 的覆盖语义**：

[cmake/modules/LibcConfig.cmake:108-136](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/cmake/modules/LibcConfig.cmake#L108-L136) —— 关键约束在第 115 行：`if(NOT DEFINED ${opt_name})` 就 `FATAL_ERROR`（config.json 里的选项名必须在全局 `config/config.json` 里先声明过，防止拼错）；第 118–124 行实现「豁免名单」绕行；第 133 行打印 `Overriding - <opt>: <new> (Previous value: <old>)`。

**⑥ 全局 `config/config.json` 的结构**：按功能分组，每项含 `value` 与 `doc`：

[config/config.json:14-67](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/config/config.json#L14-L67) —— `printf` 组下一堆 `LIBC_CONF_PRINTF_*` 开关，例如 `LIBC_CONF_PRINTF_DISABLE_FLOAT` 默认 `false`。这些值最终成为同名 CMake 变量，被 printf 实现里的 `#ifdef` 读取（与 u7-l3 的 modular printf 互为表里）。

**⑦ Linux 平台覆盖默认**：把字符串函数的默认实现换成更快的版本：

[config/linux/config.json:1-10](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/config/linux/config.json#L1-L10) —— `LIBC_CONF_STRING_LENGTH_IMPL` 全局默认是 `"element"`（`config/config.json:93-95`），Linux 上覆盖为 `"clang_vector"`；`LIBC_CONF_FIND_FIRST_CHARACTER_IMPL` 覆盖为 `"word"`。这就是「同一份代码，Linux 上默认走 SIMD/字长优化路径」的配置来源。

**⑧ `generate_config_doc` 顺带生成文档**，文档里也写明了三层覆盖规则：

[cmake/modules/LibcConfig.cmake:177-180](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/cmake/modules/LibcConfig.cmake#L177-L180) —— 指向 `config/config.json` 与 `config/<platform>/config.json`、`config/<platform>/<arch>/config.json` 三层默认。

#### 4.4.4 代码实践

**实践目标**：验证「命令行优先级高于 config.json」。

**操作步骤**：

1. 查 `config/config.json` 里 `LIBC_CONF_PRINTF_DISABLE_FLOAT` 的默认值（应为 `false`）。
2. 用 `cmake -DLIBC_CONF_PRINTF_DISABLE_FLOAT=ON ...` 配置一次构建（其余配置沿用 u1-l3 的 runtimes 命令）。
3. 在配置阶段的 CMake 输出里找一行类似 `LIBC_CONF_PRINTF_DISABLE_FLOAT:  (from command line)` 或 `Overriding - ...` 的日志。

**需要观察的现象**：命令行设置后，全局 `config/config.json` 的加载会**跳过**这一项（输出 `from command line`），后续 `config/linux/config.json` 也不会再覆盖它。

**预期结果**：命令行值胜出；若忘了命令行，则全局默认 `false` 生效；Linux 层若未覆盖该项，则保持全局默认。**待本地验证**：具体日志文案以你本机 CMake 版本输出为准。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `load_libc_config` 要求 config.json 里的每个选项名都必须「已经被定义」（第 115 行 `FATAL_ERROR`），而不是静默创建新变量？

**参考答案**：这是一种「拼写校验」防御。所有合法选项必须先在全局 `config/config.json` 里声明（带 `doc` 说明），平台 config.json 只能**覆盖**已声明项，不能凭空新增。这样 `LIBC_CONF_PRINTF_DISABLE_FLOATT`（拼错）会立即报错，而不是悄悄创建一个没人读的废变量。

**练习 2**：`config/linux/x86_64/` 没有 `config.json`，那 x86_64 的字符串函数实现默认走哪一层配置？如果想让 x86_64 单独用 `element` 实现，应该怎么做？

**参考答案**：x86_64 没有 `config.json`，所以字符串实现默认走第 2 层 `config/linux/config.json`——`LIBC_CONF_STRING_LENGTH_IMPL` 为 `clang_vector`。若要 x86_64 单独回退到 `element`，可新建 `config/linux/x86_64/config.json`（第 3 层）覆盖该变量，或直接在命令行 `-DLIBC_CONF_STRING_LENGTH_IMPL=element`（最高优先级）。

---

## 5. 综合实践

把四个最小模块串起来，完成规格里给定的综合任务。

**任务**：在 `config/linux/x86_64/entrypoints.txt` 中统计 `ctype.h` 下注册了多少个入口点，并找出一个在该平台被 `exclude.txt` 排除的函数，说明可能的原因。

**步骤与参考答案**：

1. **统计 ctype 入口点**。
   - 基础入口点（[entrypoints.txt:12-28](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/config/linux/x86_64/entrypoints.txt#L12-L28)）：`isalnum`、`isalpha`、`isascii`、`isblank`、`iscntrl`、`isdigit`、`isgraph`、`islower`、`isprint`、`ispunct`、`isspace`、`isupper`、`isxdigit`、`toascii`、`tolower`、`toupper` —— **共 16 个**。
   - Full 模式额外入口点（[entrypoints.txt:1215-1229](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/config/linux/x86_64/entrypoints.txt#L1215-L1229)）：`isalnum_l`…`toupper_l` —— **共 14 个** locale 变体。
   - 结论：Overlay 模式 16 个；Full 模式 16 + 14 = **30 个**。这正好对应「同一份 entrypoints.txt，因 `LLVM_LIBC_FULL_BUILD` 不同而支持范围不同」。

2. **找一个被 exclude 的函数并解释**。
   - 候选 A：`libc.src.unistd.faccessat`（[exclude.txt:23-30](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/config/linux/x86_64/exclude.txt#L23-L30)）。排除原因：`check_symbol_exists` 没在系统 `sys/syscall.h` 里找到 `SYS_faccessat2`，说明当前内核太老、没有 `faccessat2` 系统调用，而 LLVM-libc 的 `faccessat` 实现依赖它，故移除。
   - 候选 B：`libc.src.sys.stat.stat`（[exclude.txt:5-21](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/config/linux/x86_64/exclude.txt#L5-L21)）。排除原因：`try_compile` 探不到 `sys/random.h`，推断内核较老、缺少 `statx` 系统调用，`stat` 实现依赖它，故移除。

3. **串联理解**：这两个被排除的函数**本来已经**出现在 `entrypoints.txt` 的「打算支持」名单里，是 `exclude.txt` 在运行环境探测后把它们从 `TARGET_LLVMLIBC_ENTRYPOINTS` 中 `REMOVE_ITEM` 掉的；随后短名名单 `TARGET_ENTRYPOINT_NAME_LIST` 不再包含它们，于是 `add_entrypoint_object` 对它们走 SKIP 分支、不真正编译。这就是「entrypoints.txt 一阶裁剪 → exclude.txt 二阶裁剪 → SKIP 落地」的完整链路。

> 进阶思考（不必动手）：如果想把 `faccessat` 的排除行为做成「交叉编译时不依赖主机探测」，你会如何用 `-DLIBC_CONFIG_PATH` 指向一份手工预置的配置树来绕过 `try_compile`？（提示：参考 `CMakeLists.txt:403` 的 TODO 与 u11-l1 的移植流程。）

## 6. 本讲小结

- `config/<os>/<arch>/` 是「某平台支持范围」的事实来源，三类清单文件各司其职：`entrypoints.txt`（支持哪些函数）、`exclude.txt`（环境探测后再撤掉哪些）、`headers.txt`（生成/安装哪些头文件）。
- `entrypoints.txt` 是 CMake 脚本，用 `set`/`list(APPEND)` 与 `if(...)` 条件块（按构建模式、硬件类型、experimental 开关）组装出全限定名列表 `TARGET_LLVMLIBC_ENTRYPOINTS`，它是聚合静态库与 SKIP 短名名单的共同上游。
- `exclude.txt` 做 `try_compile` / `check_symbol_exists` 特性探测，把不支持的入口点追加到 `TARGET_LLVMLIBC_REMOVED_ENTRYPOINTS`，由主 `CMakeLists.txt` 统一 `REMOVE_ITEM`，是「运行环境驱动的二阶裁剪」。
- `headers.txt` 列头文件点分名，Full 模式强制要求（否则 `FATAL_ERROR`），Overlay 模式可选（可回退系统头）；它与入口点名单解耦，管「声明」而非「实现」。
- `config.json` 是「实现细节旋钮」，走「全局 → OS → 架构」三层覆盖、命令行最高优先；每个选项必须先在 `config/config.json` 声明，平台文件只能覆盖。
- 四个模块共同把「平台支持范围 + 实现选择」从源码里**外化**成可审查的数据/脚本，使「换平台」≈「换 config 树」，为 u11-l1 的移植流程与 u11-l3 的端到端贡献打下基础。

## 7. 下一步学习建议

- **进入 u3-l1（头文件生成管线）**：本讲的 `headers.txt` 只是「头文件名单」，而 yaml → hdrgen → `.h.def` 的真正生成机制在第三单元。学完后你会补全「名单 → 实际生成」的另一半。
- **进入 u11-l1（移植到新平台）**：本讲末尾的进阶思考已经触及移植——为新 OS/架构搭建一棵 `config/` 树、渐进式填充 `entrypoints.txt`，正是 u11-l1 的主线。
- **回头重读 u2-l3**：带着本讲对 `TARGET_ENTRYPOINT_NAME_LIST` 来源的理解，再看一遍 `add_entrypoint_object` 的 SKIP 分支，你会对「配置 → 构建」的闭环有更深的体会。
- **延伸阅读**：用 `cmake -DLIBC_CMAKE_VERBOSE_LOGGING=ON` 配置一次构建，观察配置阶段打印的 `Overriding - ...`、`Removing entrypoint ...` 日志，亲眼看到三层 config.json 覆盖与 exclude 移除的发生。

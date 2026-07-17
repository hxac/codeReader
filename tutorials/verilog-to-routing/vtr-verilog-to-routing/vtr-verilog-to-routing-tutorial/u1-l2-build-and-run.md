# 构建与运行 VTR

## 1. 本讲目标

上一讲（u1-l1）我们建立了 VTR 的全局观：它是一个「架构驱动」的开源 FPGA CAD 框架，输入是 Verilog 电路 + 架构 XML，产物是速度/面积评估。本讲聚焦一个最现实的问题：**怎么把这堆源码编译成可运行的工具**。

学完本讲，你应该能够：

- 用 `make` / `make -j8 vpr` 完成一次标准构建，并知道产物落在哪个目录。
- 看懂根目录 `Makefile` 这个「包装层」——它如何把用户的简单命令翻译成底层 CMake 的配置与编译。
- 区分 `release` / `debug` / `RelWithDebInfo` / `_pgo` / `_strict` 等构建类型，知道何时用哪一种。
- 掌握并行编译、关闭 IPO、ccache 等构建加速技巧。

> 本讲不要求你真的把项目编译完（编译耗时较长且依赖系统环境）。所有「需要运行」的步骤都标注了预期现象；如果你尚未配置环境，可先把步骤当作「源码阅读型实践」来理解。

## 2. 前置知识

在进入源码前，先用通俗语言建立三个概念。

### 2.1 为什么需要「构建系统」

VTR 是一个大型 C++ 项目（C++20 标准），由多个子工程组成：综合前端 PARMYS、逻辑映射 ABC、以及核心引擎 VPR。要把几百个 `.cpp` 文件编译、链接成一堆可执行文件，手写 `g++` 命令是不现实的。业界通用做法是用 **CMake** 这类「构建系统生成器」：你写一份声明式的 `CMakeLists.txt`，CMake 据此生成平台相关的构建文件（Linux 上是 Makefile），再交给 `make` 去真正编译。

### 2.2 什么是「源码内构建」为什么被禁止

如果直接在源码根目录运行 `cmake .`，CMake 会把一堆临时文件（`CMakeCache.txt`、`CMakeFiles/`）直接写进源码目录，污染仓库、难以清理，还会和版本控制冲突。这叫 **in-source build**。VTR 明确禁止它，要求所有构建产物集中到一个独立的 `build/` 目录（叫 **out-of-source build**）。

### 2.3 「包装层」是什么

CMake 功能强大但参数繁琐，对只想编译用一下的初学者不友好。VTR 在根目录放了一个 `Makefile`，它**不真正编译**，只负责把 `make` / `make vpr` / `make BUILD_TYPE=debug` 这类简单命令，翻译成正确的 CMake 配置命令并转发。这就是「为方便用户而隐藏 CMake 的包装层」。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `Makefile` | 包装层。解析 `BUILD_TYPE`、组装 `CMAKE_PARAMS`、转发目标给底层 CMake 生成的 Makefile。 |
| `CMakeLists.txt` | 顶层 CMake 配置。禁止 in-source、设定 C++ 标准、声明构建选项、逐个 `add_subdirectory` 加入子工程。 |
| `vpr/CMakeLists.txt` | VPR 子工程配置。定义 `add_executable(vpr ...)` 与安装规则，决定可执行文件产物位置。 |
| `BUILDING.md` | 面向用户的构建说明（环境准备、平台差异、GUI 开关、安装校验）。 |
| `doc/agents/build.md` | 给开发者的速查指南，列出关键 CMake 选项与可选依赖。 |

## 4. 核心概念与源码讲解

### 4.1 Makefile 包装层的工作原理

#### 4.1.1 概念说明

根目录的 `Makefile` 是用户与 CMake 之间唯一的「翻译官」。它的设计目标在文件开头第一句注释里写得很清楚：**隐藏 cmake，方便非专家用户**。

它要做三件事：

1. **接收用户的简单命令**，如 `make`、`make vpr`、`make BUILD_TYPE=debug`。
2. **把环境变量转成 CMake 参数**，主要是把 `BUILD_TYPE` 翻译成 `-DCMAKE_BUILD_TYPE=...`，并附加 strict/pgo/verbose/graphics 等条件参数，拼成 `CMAKE_PARAMS`。
3. **转发给底层**：进入 `build/` 目录，运行 `cmake` 完成配置，再用生成的 Makefile 真正编译用户要的目标。

#### 4.1.2 核心流程

当你敲下 `make -j8 vpr` 时，包装层大致经历如下流程：

```text
用户命令: make -j8 vpr
   │
   ├─ 解析 BUILD_TYPE（默认 release）→ 规范化大小写、剥离 _pgo/_strict 后缀 → CMAKE_BUILD_TYPE
   │
   ├─ 组装 CMAKE_PARAMS = -DCMAKE_BUILD_TYPE=<...> -G 'Unix Makefiles' [附加条件项]
   │       附加项由 strict/verbose/graphics/Windows 等条件决定
   │
   ├─ 确认 cmake 可执行文件存在，否则报错并提示安装命令
   │
   ├─ mkdir -p build   （若 BUILD_DIR != build，则建符号链接）
   │
   ├─ cd build && cmake <CMAKE_PARAMS> <SOURCE_DIR>   # 配置阶段
   │       （若 BUILD_TYPE 含 pgo，则走两阶段 + 跑基准收集 profile）
   │
   └─ make -C build vpr   # 真正编译用户指定的目标（vpr）
```

关键点：**包装层本身不编译任何源码**。真正的编译发生在 `make -C build`（即 CMake 生成的那个 Makefile）里。包装层只是个「调度器」。

#### 4.1.3 源码精读

包装层的默认构建类型由 `?=`（仅在未定义时赋值）设为 `release`：

> [Makefile:26](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/Makefile#L26) —— 设定 `BUILD_TYPE ?= release`，即不传参时默认用 release（带编译器优化）。

文件顶部注释列出了所有合法取值与后缀，是理解构建类型的权威说明：

> [Makefile:19-26](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/Makefile#L19-L26) —— 列出 `release` / `RelWithDebInfo` / `debug` 三种主类型，以及 `_pgo`（profile-guided optimization，两阶段优化）、`_strict`（警告视为错误）两种后缀。

`BUILD_TYPE` 会被规范化处理。用户可能写 `DEBUG` 或 `Debug`，包装层统一转小写；同时剥离后缀，得到一个 CMake 能认识的纯类型名：

> [Makefile:31-36](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/Makefile#L31-L36) —— 先 `tr` 转小写，再用 `sed` 去掉 `_pgo` / `_strict` 后缀，存入 `CMAKE_BUILD_TYPE`。

转发逻辑的核心是这条 `override`（用户传入的 `CMAKE_PARAMS` 会拼在后面，保证既加默认项又允许覆盖）：

> [Makefile:51-53](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/Makefile#L51-L53) —— 把 `-DCMAKE_BUILD_TYPE` 和生成器 `-G` 拼到 `CMAKE_PARAMS` 前面，注释里举例 `make CMAKE_PARAMS="-DVTR_ENABLE_SANITIZE=true"`。

最终真正干活的是这条规则。注意它把 `all` 和 `$(MAKECMDGOALS)`（即用户在命令行敲的所有目标，如 `vpr`）合并到同一条规则，先 `cmake` 配置、再 `make -C build` 编译：

> [Makefile:130-191](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/Makefile#L130-L191) —— 核心构建规则：检查 cmake 是否存在、`mkdir -p build`、根据是否含 `pgo` 选择两阶段或标准配置、最后 `make -C build` 编译目标。

配置阶段的标准分支（非 pgo）就一句：

> [Makefile:166-173](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/Makefile#L166-L173) —— 标准构建：`cd build && cmake $(CMAKE_PARAMS) $(SOURCE_DIR)`。

> 💡 `override` 关键字的意义：Makefile 里 `CMAKE_PARAMS := ...` 在命令行传入 `make CMAKE_PARAMS=...` 时会被命令行覆盖；而 `override` 保证包装层拼好的 `-DCMAKE_BUILD_TYPE` 等基础项**一定存在**，用户的自定义参数则追加在末尾。

#### 4.1.4 代码实践

**实践目标**：用「源码阅读」的方式验证包装层的转发逻辑，不依赖真实编译。

**操作步骤**：

1. 打开根目录 `Makefile`，定位到 [Makefile:130](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/Makefile#L130) 的构建规则。
2. 假设你运行 `make vpr`，回答：
   - `$(MAKECMDGOALS)` 此时是什么？（提示：make 的自动变量，代表命令行目标）
   - 最终真正编译 vpr 的那条命令是哪一行？（提示：`make -C $(BUILD_DIR) $(MAKECMDGOALS)`）
3. 再假设你运行 `make`（不带目标），追踪 `all` 是如何作为默认目标被命中的。

**预期现象**：你能口头复述「`make vpr` → 配置 + `make -C build vpr`」这条链路，并指出包装层没有自己编译任何 `.cpp`。

**待本地验证**：上述是源码静态分析结论。若想看真实转发，可在本地运行 `make -n vpr`（`-n` 只打印不执行），观察包装层**会**运行哪些命令。

#### 4.1.5 小练习与答案

**练习 1**：为什么根目录 Makefile 大量使用 `@+$(MAKE) -C $(BUILD_DIR)`，而不是直接写编译命令？

**参考答案**：因为包装层不负责编译，它把目标转发给 `build/` 下由 CMake 生成的那个 Makefile。`-C build` 表示「进入 build 目录执行」，`+` 表示这一步允许递归 make（保证并行 job server 正确传递），`@` 表示不在终端回显这条命令本身。

**练习 2**：运行 `make BUILD_TYPE=DEBUG_pgo_strict` 后，传给 CMake 的 `CMAKE_BUILD_TYPE` 最终是什么？

**参考答案**：先转小写 → `debug_pgo_strict`；再 `sed` 剥离 `pgo` 与 `strict` → `debug__strict` → 去掉后最终为 `debug`。同时因为名字里含 `strict`，会额外附加 `-DCMAKE_COMPILE_WARNING_AS_ERROR=on`；因为含 `pgo`，会触发两阶段 PGO 构建。参见 [Makefile:32](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/Makefile#L32) 与 [Makefile:69-72](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/Makefile#L69-L72)。

---

### 4.2 构建类型与 CMAKE_PARAMS

#### 4.2.1 概念说明

「构建类型」决定编译器用什么级别的优化和调试信息。VTR 支持的主类型对应 CMake 标准的三种：

| BUILD_TYPE | 优化 | 调试信息（`-g`） | 典型用途 |
|------------|------|------------------|----------|
| `release`（默认） | 高（`-O3` 之类） | 无 | 正式跑、性能评估 |
| `debug` | 无（`-O0`） | 有（`-g3`） | 用 gdb 调试 VPR 源码 |
| `RelWithDebInfo` | 高 | 有 | 既要性能又要可看栈 |

在主类型之外，VTR 还用两个「后缀」叠加开关：

- `_strict`：把编译器警告当作错误（`-DCMAKE_COMPILE_WARNING_AS_ERROR=on`），CI 与代码审查常用，保证代码干净。
- `_pgo`：Profile-Guided Optimization，先用带插桩的二进制跑一遍基准收集运行 profile，再用 profile 指导第二阶段编译优化，得到比普通 release 更快的二进制。代价是构建时间很长。

此外，顶层 `CMakeLists.txt` 还定义了一系列独立于构建类型的「CMake 选项」，通过 `make CMAKE_PARAMS="-DXXX=YYY"` 传入，例如断言等级、sanitizer、图形界面开关等。

#### 4.2.2 核心流程

构建类型与选项从用户输入到生效，经过两处处理：

```text
make BUILD_TYPE=debug_strict CMAKE_PARAMS="-DVTR_ASSERT_LEVEL=3"
        │                  │
        │                  └─→ 直接拼到包装层 CMAKE_PARAMS 末尾（override 保证基础项在前）
        │
        └─→ Makefile: 规范化 + 剥离后缀 → CMAKE_BUILD_TYPE=debug
                 │
                 ├─ strict 检测 → 附加 -DCMAKE_COMPILE_WARNING_AS_ERROR=on
                 └─ pgo 检测  → 触发两阶段构建（本例无 pgo）

  最终 cmake 调用: cmake -DCMAKE_BUILD_TYPE=debug -G 'Unix Makefiles' -DCMAKE_COMPILE_WARNING_AS_ERROR=on -DVTR_ASSERT_LEVEL=3 <SOURCE_DIR>
        │
        └─→ CMakeLists.txt 读取这些 -D 变量，配置编译选项、警告标志、子工程
```

注意 `doc/agents/build.md` 提醒：**CMake 选项是「粘性」的**——一旦在 `build/` 目录里配置过一次，后续的 `make` 会复用缓存里的值，直到你显式改变或 `distclean`。这是初学者常踩的坑：改了选项却「没生效」，往往是因为 build 目录里还存着旧缓存。

#### 4.2.3 源码精读

顶层 CMake 在开头就拦截了 in-source 构建，并给出了正确做法的提示：

> [CMakeLists.txt:11-15](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/CMakeLists.txt#L11-L15) —— 若源码目录与构建目录相同，直接 `FATAL_ERROR`，并提示用 Makefile 包装层或自建 `build/` 目录。

如果用户没指定 `CMAKE_BUILD_TYPE`，CMake 会强制设为 `Release`：

> [CMakeLists.txt:76-81](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/CMakeLists.txt#L76-L81) —— `if(NOT CMAKE_BUILD_TYPE)` 时默认 `Release`（注意：正常路径下包装层总会传 `-DCMAKE_BUILD_TYPE`，所以这条主要保护「绕过包装层直接 cmake」的场景）。

几个最常用的 CMake 选项在顶层被 `option()` / `set(... CACHE ...)` 声明，决定了断言密度、IPO、图形界面等：

> [CMakeLists.txt:22-23](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/CMakeLists.txt#L22-L23) —— `VTR_IPO_BUILD`（过程间优化，`auto`/`on`/`off`），开发期建议 `off` 以加快编译、便于调试。

> [CMakeLists.txt:26-27](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/CMakeLists.txt#L26-L27) —— `VTR_ASSERT_LEVEL`（0–4，默认 2），数值越大运行时断言越密集、越慢、但越容易抓到 bug。

顶层 `add_subdirectory` 把各子工程串起来，解释了为什么 `make`（全量）会同时编译 abc、parmys、vpr 等，而 `make vpr` 只编译 vpr：

> [CMakeLists.txt:514-532](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/CMakeLists.txt#L514-L532) —— 依次 `add_subdirectory(abc)`、`libs`、`parmys`、`vpr`、`ace2`、`utils`；其中 abc 与 parmys 受 `WITH_ABC` / `WITH_PARMYS` 开关控制。

VPR 的可执行文件由子工程定义，安装规则决定「`make install` 后产物去哪」：

> [vpr/CMakeLists.txt:153](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/CMakeLists.txt#L153) —— `add_executable(vpr ${EXEC_SOURCES})` 定义 vpr 目标。由于没有设置 `RUNTIME_OUTPUT_DIRECTORY`，CMake 默认把它输出到 `build/vpr/vpr`。

> [vpr/CMakeLists.txt:265](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/CMakeLists.txt#L265) —— `install(TARGETS vpr libvpr DESTINATION bin)`，配合顶层默认安装前缀指向 `build`（见下），`make install` 会把 vpr 复制到 `build/bin/`。

> [CMakeLists.txt:17-20](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/CMakeLists.txt#L17-L20) —— 默认 `CMAKE_INSTALL_PREFIX` 设为构建目录本身（`build`），所以安装后产物在 `build/bin/` 下，不会污染源码树。

把上面三条合起来，就得到了**产物路径速查**：

| 命令 | 主要可执行文件位置 |
|------|---------------------|
| `make` / `make -j8 vpr`（只编译不安装） | `build/vpr/vpr` |
| `make` 后再 `make install` | `build/bin/vpr` |

#### 4.2.4 代码实践

**实践目标**：理解构建类型对编译器标志的影响，并掌握「改了选项不生效」的排查方法。

**操作步骤**：

1. 阅读 [CMakeLists.txt:136-140](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/CMakeLists.txt#L136-L140)，确认 `debug` 类型会附加 `-O0 -g3`（关闭优化、保留调试符号）。
2. 阅读顶层对 `CMAKE_BUILD_TYPE` 默认值的处理（[CMakeLists.txt:76-81](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/CMakeLists.txt#L76-L81)），回答：为什么即使 Makefile 默认传了 release，这里仍要再设一次默认值？
3. **本地可选执行**：先 `make BUILD_TYPE=release -j8 vpr`，再 `make BUILD_TYPE=debug -j8 vpr`，观察第二次是否真的重新编译（提示：CMake 的 build type 改变会让所有目标重新编译）。

**预期现象**（本地运行时）：切换 `BUILD_TYPE` 后，`make` 会重新编译大量文件，因为编译标志变了、目标文件失效。这正是「构建类型是一等公民」的体现。

**待本地验证**：上述「重新编译」现象需在本机实际构建才能看到；本练习的源码阅读部分（步骤 1、2）可直接完成。

#### 4.2.5 小练习与答案

**练习 1**：`make BUILD_TYPE=debug` 与 `make BUILD_TYPE=RelWithDebInfo` 在调试体验和性能上有什么差别？

**参考答案**：`debug` 用 `-O0`（完全不优化），单步调试最准确但运行最慢；`RelWithDebInfo` 保留优化又带调试符号，运行快、能看栈，但单步可能因优化而「乱跳」。调试 VPR 算法逻辑优先 `debug`，复现线上性能问题用 `RelWithDebInfo`。

**练习 2**：你改了 `make CMAKE_PARAMS="-DVTR_ASSERT_LEVEL=4"`，但运行 vpr 时断言行为似乎没变，最可能的原因是什么？怎么修？

**参考答案**：CMake 选项是粘性的，`build/` 目录里缓存了旧值。要么显式再传一次该参数让它覆盖，要么 `make distclean`（[Makefile:214-217](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/Makefile#L214-L217)）删掉整个 `build/` 重新配置。

---

### 4.3 构建加速技巧

#### 4.3.1 概念说明

VTR 是个大项目，首次全量编译动辄十几分钟到几十分钟。掌握加速技巧能极大提升开发体验。主要的加速手段有四类：

1. **并行编译**：`make -jN`，让 N 个编译任务同时跑。N 通常取 CPU 核数。
2. **只编译需要的目标**：`make vpr` 而不是 `make`，跳过 abc/parmys 等不需要的子工程。
3. **关闭 IPO**：过程间优化（LTO）会显著拖慢链接时间，开发期用 `VTR_IPO_BUILD=off` 关掉。
4. **ccache**：缓存已编译的目标文件，二次编译只重编改动过的文件。顶层已用 `include(SupportCcache)` 自动探测。

此外，「增量编译」是日常常态：只要不 `distclean`，`build/` 里的中间产物会被复用，改一个文件只重编受影响的部分。

#### 4.3.2 核心流程

加速手段如何叠加生效：

```text
make -j8 vpr CMAKE_PARAMS="-DVTR_IPO_BUILD=off"
   │
   ├─ -j8           → 8 个并行编译槽（job server 经 -C 传递给底层 Makefile）
   ├─ vpr           → 只编译 vpr 目标，跳过 abc/parmys 等 ALL 目标
   └─ VTR_IPO_BUILD=off → 顶层 CMake 关闭 LTO，链接阶段显著提速
                         （首次配置后写入 build/ 缓存；后续 make 自动复用）

并行 + ccache 的协同：
   - SupportCcache 在配置阶段探测 ccache，若存在则把编译器包装为 ccache，
     命中缓存的目标文件直接复制，无需重新编译。
```

#### 4.3.3 源码精读

顶层在最早处就引入了 ccache 支持：

> [CMakeLists.txt:3-4](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/CMakeLists.txt#L3-L4) —— 把 `cmake/modules` 加入模块搜索路径并 `include(SupportCcache)`，自动用 ccache 包装编译器。

IPO 的 `auto` 默认逻辑：debug 构建自动关 IPO，非 debug 且编译器支持时才开：

> [CMakeLists.txt:117-127](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/CMakeLists.txt#L117-L127) —— `auto` 模式下，`debug` 类型或编译器不支持 IPO 时关闭，否则开启。所以调试时自然就快；但 release 开发迭代仍建议手动 `off`。

并行编译的关键在于包装层把 `-j` 透传。GNU make 的 job server 通过环境变量传递并行度，`make -C build` 会继承，所以根目录的 `-j8` 能作用于底层：

> [Makefile:187-191](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/Makefile#L187-L191) —— Linux 下最终编译走 `@+$(MAKE) -C $(BUILD_DIR) $(MAKECMDGOALS)`，`+` 前缀保证并行 job server 正确传递给递归 make。

#### 4.3.4 代码实践

**实践目标**：组合多种加速手段，构建一次「开发期最快」的 vpr。

**操作步骤**：

1. 确认机器核数：`nproc`（Linux）。
2. 执行开发期推荐命令：
   ```shell
   make -j$(nproc) vpr BUILD_TYPE=debug CMAKE_PARAMS="-DVTR_IPO_BUILD=off"
   ```
   解读每个部分的作用：`-j$(nproc)` 全核并行；`vpr` 只编核心引擎；`debug` 便于调试；`VTR_IPO_BUILD=off` 关 LTO 提速。
3. 记录产物路径：编译完成后，`vpr` 可执行文件应在 `build/vpr/vpr`（参见 [vpr/CMakeLists.txt:153](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/CMakeLists.txt#L153)）。
4. 可选：若装了 ccache，再次运行同样命令，对比「首次编译」与「二次缓存命中」的时间差。

**预期现象**：首次编译耗时较长（视机器而定，可能 5–20 分钟）；带 ccache 的二次构建只重编少数文件，明显变快。产物出现在 `build/vpr/vpr`。

**待本地验证**：编译时长与产物路径需在本机实际构建后确认；源码阅读部分（步骤中对每条参数的解读）可直接完成。

> ⚠️ 完整 VTR 流程需要 yosys/abc 等前端工具。若你只 `make vpr` 而后续想跑完整 `run_vtr_flow`，可能会因缺少 yosys 报错。需要 GUI 或完整流程时，改用 `make ensure-gui`（[Makefile:205-207](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/Makefile#L205-L207)，会构建全部目标并准备 Qt6）或直接 `make` 全量构建。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `make -j8 vpr` 比 `make -j8`（全量）快？有什么潜在副作用？

**参考答案**：`make vpr` 只编译 vpr 目标及其依赖，跳过了 abc、parmys、ace2、utils 等子工程（[CMakeLists.txt:514-532](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/CMakeLists.txt#L514-L532)）。副作用是只关心 VPR 引擎时没问题，但跑完整 VTR 流程（需要 yosys/abc）时这些工具会缺失，需另行全量构建。

**练习 2**：开发期迭代中，`VTR_IPO_BUILD=off` 能省多少时间？它牺牲了什么？

**参考答案**：IPO（链接期优化）会把跨文件优化推到链接阶段，大幅拖慢链接；关闭后链接变快、增量编译更友好，但牺牲了 release 级别的运行时性能。`doc/agents/build.md` 明确建议开发期设为 `off`。参见 [CMakeLists.txt:110-130](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/CMakeLists.txt#L110-L130)。

---

## 5. 综合实践

**任务**：从零开始，把 VTR 的 VPR 引擎构建出来，并验证它能运行。

1. **环境准备**：按 `BUILDING.md` 准备系统依赖（Linux 可运行 `./install_apt_packages.sh`）；克隆后执行 `git submodule init && git submodule update` 拉取子模块（参见 [BUILDING.md:6-9](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/BUILDING.md#L6-L9)）。
2. **最小构建**：运行
   ```shell
   make -j$(nproc) vpr
   ```
   记录耗时。
3. **定位产物**：在 `build/` 下找到 `vpr` 可执行文件（预期在 `build/vpr/vpr`）。
4. **切换 debug 重建**：删除 build 缓存后用 debug 重建，对比：
   ```shell
   make distclean
   make -j$(nproc) vpr BUILD_TYPE=debug
   ```
   观察是否全量重新编译（预期：是，因为编译标志变了）。
5. **验证可运行**（待本地验证）：执行 `./build/vpr/vpr --help`，能看到命令行帮助即说明构建成功；或按 `BUILDING.md` 的「Verifying Installation」跑一个基础回归任务：
   ```shell
   ./vtr_flow/scripts/run_vtr_task.py ./vtr_flow/tasks/regression_tests/vtr_reg_basic/basic_timing
   ```
   预期输出多行 `OK`（参见 [BUILDING.md:121-137](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/BUILDING.md#L121-L137)）。

**预期结果**：你将得到一个可运行的 `vpr`，理解 release 与 debug 两种构建的差异，并知道产物路径。命令行参数体系本身是下一讲（u1-l5）的主题，这里只需确认 `--help` 能正常输出即可。

**待本地验证**：步骤 2–5 的具体耗时与输出取决于本机环境，请实际执行后记录。

## 6. 本讲小结

- VTR 用根目录 `Makefile` 作为**包装层**，把 `make` / `make vpr` / `make BUILD_TYPE=debug` 翻译成底层 CMake 配置命令并转发，自身不编译源码。
- 构建产物集中在独立的 `build/` 目录（out-of-source），顶层 `CMakeLists.txt` 明确**禁止 in-source 构建**；`vpr` 可执行文件默认在 `build/vpr/vpr`，`make install` 后在 `build/bin/`。
- 构建类型分主类型（`release`/`debug`/`RelWithDebInfo`）与后缀（`_strict` 警告即错误、`_pgo` 两阶段优化），由 Makefile 规范化后传给 CMake。
- CMake 选项通过 `make CMAKE_PARAMS="-DXXX=YYY"` 传入，常用项包括 `VTR_ASSERT_LEVEL`、`VTR_IPO_BUILD`、`VTR_ENABLE_SANITIZE` 等；选项是**粘性**的，改了不生效多半是 build 目录缓存所致。
- 加速技巧：`-jN` 并行、只编 `vpr`、开发期 `VTR_IPO_BUILD=off`、借助自动探测的 ccache。
- GUI 与完整流程：`make ensure-gui` / `make ensure-headless` 显式控制图形界面；只需 VPR 引擎时 `make vpr` 足矣。

## 7. 下一步学习建议

构建完成后，下一讲 **u1-l3（仓库目录结构与组件地图）** 会带你梳理顶层各目录（`vpr`、`parmys`、`abc`、`libs`、`vtr_flow` 等）与 VPR 内部子目录的职责，让你能根据问题快速定位代码。之后再进入 **u1-l4（一键跑通 VTR 全流程）**，用 `run_vtr_flow.py` 把一个真实电路跑通，看到从 Verilog 到布线结果的完整链路。

建议同步阅读：
- `doc/agents/build.md` —— 开发者视角的构建速查（可选依赖、关键 CMake 选项表）。
- `doc/src/vtr/optional_build_info.md` —— 更详尽的构建信息（`BUILDING.md` 末尾有指向）。
- `Makefile` 注释 —— 它本身是一份很好的「构建类型与用法」文档。

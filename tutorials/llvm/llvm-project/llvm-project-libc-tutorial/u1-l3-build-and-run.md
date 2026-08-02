# 构建与运行入门：runtimes 构建与 Hello World

## 1. 本讲目标

本讲带你**亲手把 LLVM-libc 跑起来**。读完本讲后，你应该能够：

1. 装好 LLVM-libc 的构建依赖，并说清楚每样东西是做什么的。
2. 用官方推荐的 **runtimes 构建（runtimes build）** 写出一条 CMake 配置命令，并逐个解释 `LLVM_ENABLE_RUNTIMES`、`LLVM_LIBC_FULL_BUILD` 等关键变量的作用。
3. 用 `ninja` 构建 `libc`/`libm`，运行整套 `check-libc` 测试，并单独运行某个函数（如 `ctype.isalpha`）的单元测试。
4. 在 `-nostdinc`/`-nostdlib` 下手工把 `crt1.o`、`libc.a`、`libm.a` 链接成一个能打印 `Hello, World` 的可执行文件，并解释每一步为什么这么写。

本讲是「动手」篇：前置两讲（[u1-l1](u1-l1-project-overview.md) 讲了项目定位、[u1-l2](u1-l2-source-tree-layout.md) 讲了目录结构）已经告诉你「它是什么、目录怎么排」，本讲把它们落到「怎么编译、怎么链接、怎么验证」。

## 2. 前置知识

本讲假设你已经具备下面的认知（来自前两讲），这里只做最小回顾：

- **入口点（entrypoint）**：LLVM-libc 把每个公开函数做成一个独立的构建单元，最终聚合成静态库。
- **Full 模式 vs Overlay 模式**：Full 模式把 LLVM-libc 当作**完整的 libc 替换品**，产出 `libc.a`/`libm.a`；Overlay 模式只**覆盖系统 libc 的少数函数**，产出 `libllvmlibc.a`。本讲走的是 **Full 模式**。
- **runtimes 构建体系**：上一讲我们看到 [libc 顶层 CMakeLists.txt](https://github.com/llvm/llvm-project/blob/1ac9b999f8b521b5d6d82cf1a19858bc40a18c6a/libc/CMakeLists.txt) 会用 `FATAL_ERROR` 拒绝「直接在 `libc/` 下构建」，强制我们到上一层的 `runtimes/` 目录去配置。**runtimes 构建就是把 `libc`、`compiler-rt` 等多个「运行时子项目」放在一起统一编译的入口**，本讲就走这条路。

如果你对「为什么要把构建根放在 `runtimes/` 而不是 `libc/`」还有疑问，请先回顾 u1-l2。本讲不再重复目录细节，只聚焦**构建动作本身**。

## 3. 本讲源码地图

本讲引用的关键文件如下，先建立全局印象：

| 文件 | 作用 |
|------|------|
| `docs/getting_started.md` | 官方「上手指南」，本讲的主线（4 步流程的权威来源）。 |
| `docs/build_concepts.md` | 解释 5 种构建场景与「为什么用 runtimes 构建」。 |
| `runtimes/CMakeLists.txt` | runtimes 构建的**真正入口**，消费 `LLVM_ENABLE_RUNTIMES`。 |
| `lib/CMakeLists.txt` | 把入口点聚合成 `libc`/`libm`/`libmvec` 静态库归档。 |
| `startup/linux/CMakeLists.txt` | 产出启动对象 `crt1.o`/`crti.o`/`crtn.o`。 |
| `test/CMakeLists.txt` | 定义 `check-libc` 测试伞目标。 |
| `cmake/modules/LLVMLibCTestRules.cmake` | 定义单元测试目标命名规则（含 `__unit__` 后缀）。 |
| `examples/hello_world/hello_world.c` | 官方 Hello World 示例源码。 |
| `examples/hello_world/CMakeLists.txt` + `examples/examples.cmake` | 示例的 CMake 配置，演示 Full/Overlay 两种链接方式。 |

> 注意：`runtimes/CMakeLists.txt` 不在 `libc/` 目录下，而是它的兄弟目录（位于仓库根的 `runtimes/`）。这正是 runtimes 构建的关键——它的根在 `libc` 之外。

## 4. 核心概念与源码讲解

本讲按官方文档的 4 步流程拆成 4 个最小模块：**依赖安装 → CMake 配置 → 编译与测试 → Hello World 链接**。

### 4.1 依赖安装

#### 4.1.1 概念说明

LLVM-libc 用**现代 C++** 编写，并且**主动追求「不依赖宿主 C/C++ 运行时」**（它要自己成为 libc，自然不能反过来依赖系统的 libstdc++/libc++）。因此构建它需要：

- 一个较新的 **Clang（v15+）** 作为编译器。
- **CMake + Ninja** 作为构建系统（LLVM 全家桶的事实标准）。
- **Python**（头文件生成器 hdrgen 依赖 `pyyaml`，详见 u3-l1）。
- `gcc-multilib`：提供内核相关的头文件符号链接（如 `/usr/include/asm`），Linux Full 构建会用到。

#### 4.1.2 核心流程

Debian/Ubuntu 上一次性装齐的命令（官方原文）：

```sh
sudo apt-get update
sudo apt-get install git cmake ninja-build clang gcc-multilib
```

> 安装顺序无依赖冲突，一行装齐即可。其它发行版请替换包名（如 Fedora 用 `dnf install clang cmake ninja-build`）。

#### 4.1.3 源码精读

依赖清单与上述命令出自官方上手指南的「Install Dependencies」小节：

[docs/getting_started.md:10-19](https://github.com/llvm/llvm-project/blob/1ac9b999f8b521b5d6d82cf1a19858bc40a18c6a/libc/docs/getting_started.md#L10-L19) —— 列出 `git cmake ninja-build clang gcc-multilib` 这组依赖。

注意一个隐藏点：Full 构建在配置阶段会查找内核相关的系统头文件（如 `<asm/unistd.h>`）。文档 [docs/full_host_build.md](https://github.com/llvm/llvm-project/blob/1ac9b999f8b521b5d6d82cf1a19858bc40a18c6a/libc/docs/full_host_build.md#L16-L24) 专门提示：若报「找不到 `<asm/unistd.h>`」，多半是缺 `/usr/include/asm` 符号链接，而 `gcc-multilib` 会自动建好它。所以这个包不只是「多架构支持」，更是为了让 syscall 相关头文件可达。

#### 4.1.4 代码实践

1. **目标**：确认本机依赖齐全，并能查到 Clang 版本。
2. **步骤**：
   ```sh
   clang --version        # 期望主版本号 >= 15
   cmake --version        # 期望 >= 3.20
   ninja --version
   ls -l /usr/include/asm # 期望是一个符号链接（由 gcc-multilib 创建）
   ```
3. **观察**：前三条应分别打印版本号；第四条若报「No such file」，说明缺 `gcc-multilib`。
4. **预期结果**：Clang 版本 ≥ 15，且 `/usr/include/asm` 存在。
5. 如本机 Clang 过旧或路径不在 `PATH`，请先升级或指定绝对路径——本讲后续命令里出现 `clang` 的地方都可以替换成它的绝对路径。

#### 4.1.5 小练习与答案

- **练习 1**：为什么构建 LLVM-libc 必须用 Clang，而不能用任意 GCC？
- **答案**：因为实现规范依赖一些 Clang 特有的扩展（如 `LLVM_LIBC_FUNCTION` 宏用到的 `asm` 别名、内置位域操作等），且 runtimes 构建假设 Clang 工具链行为。详见 u2-l2 的宏展开讲解。
- **练习 2**：`gcc-multilib` 在这里提供的是「多架构编译」还是「系统头文件符号链接」？
- **答案**：在我们的场景里，它的关键作用是建立 `/usr/include/asm` → `/usr/include/<宿主三元组>/asm` 的符号链接，让 Full 构建能找到 Linux 内核头文件。

---

### 4.2 CMake 配置：runtimes 构建与关键变量

#### 4.2.1 概念说明

这是本讲最核心的一步：**用一条 CMake 命令把构建配置出来**。要理解它，先抓住三个要点：

1. **源（`-S runtimes`）为什么是 runtimes 而不是 libc？** 因为 runtimes 构建是一个「外壳」，它本身负责解析「要编译哪些运行时子项目」，再把控制权交给各自的 `CMakeLists.txt`（`libc`、`compiler-rt` 等）。直接指向 `libc/` 会被拒绝。
2. **`LLVM_ENABLE_RUNTIMES` 决定编译谁。** 官方上手命令里写的是 `"libc;compiler-rt"`——带上 `compiler-rt` 是为了启用 **Scudo 内存分配器**（LLVM-libc 的 malloc 默认走 Scudo）。
3. **`LLVM_LIBC_FULL_BUILD=ON` 选择 Full 模式。** 它的默认值是 `OFF`（即 Overlay）。Full 模式才会产出独立的 `libc.a`/`libm.a`。

#### 4.2.2 核心流程

官方配置命令（这是本讲的权威模板）：

```sh
git clone --depth=1 https://github.com/llvm/llvm-project.git
cd llvm-project
cmake -G Ninja -S runtimes -B build \
  -DLLVM_ENABLE_RUNTIMES="libc;compiler-rt" \
  -DLLVM_LIBC_FULL_BUILD=ON \
  -DCMAKE_BUILD_TYPE=Debug \
  -DCMAKE_C_COMPILER=clang \
  -DCMAKE_CXX_COMPILER=clang++ \
  -DLLVM_LIBC_INCLUDE_SCUDO=ON \
  -DCOMPILER_RT_BUILD_SCUDO_STANDALONE_WITH_LLVM_LIBC=ON \
  -DCOMPILER_RT_BUILD_GWP_ASAN=OFF \
  -DCOMPILER_RT_SCUDO_STANDALONE_BUILD_SHARED=OFF \
  -DCMAKE_EXPORT_COMPILE_COMMANDS=ON
```

逐变量解读：

| 变量 | 含义 |
|------|------|
| `-G Ninja` | 使用 Ninja 生成器（LLVM 标配，比 Make 快很多）。 |
| `-S runtimes -B build` | 源码目录是 `runtimes/`，构建目录是 `build/`（会自动创建）。 |
| `LLVM_ENABLE_RUNTIMES="libc;compiler-rt"` | 编译这两个运行时子项目。`compiler-rt` 为 Scudo 提供底层实现。 |
| `LLVM_LIBC_FULL_BUILD=ON` | **Full 模式**：产出独立 `libc.a`/`libm.a`，而非覆盖用的 `libllvmlibc.a`。 |
| `CMAKE_BUILD_TYPE=Debug` | 调试构建（含符号信息）。开发期推荐 Debug，发布用 Release。 |
| `CMAKE_C_COMPILER=clang` / `...=clang++` | 指定 C / C++ 编译器为 Clang。 |
| `LLVM_LIBC_INCLUDE_SCUDO=ON` | 把 Scudo 分配器接进 libc。 |
| `COMPILER_RT_BUILD_SCUDO_STANDALONE_WITH_LLVM_LIBC=ON` | 让 Scudo 以 LLVM-libc 为底座独立构建。 |
| `COMPILER_RT_BUILD_GWP_ASAN=OFF` | 关闭 GWP-ASan（一种采样式内存错误检测，开发期可关）。 |
| `COMPILER_RT_SCUDO_STANDALONE_BUILD_SHARED=OFF` | 不构建 Scudo 的共享库版本（LLVM-libc 当前不支持动态链接）。 |
| `CMAKE_EXPORT_COMPILE_COMMANDS=ON` | 生成 `compile_commands.json`，方便 IDE/clangd 跳转。 |

一句话总结：**这条命令的本质是「告诉 runtimes 外壳：用 Clang，以 Full 模式，把 libc 和带 Scudo 的 compiler-rt 一起编出来」。**

#### 4.2.3 源码精读

`LLVM_ENABLE_RUNTIMES` 是 runtimes 构建的「消费对象」。看真正的入口怎么处理它：

[runtimes/CMakeLists.txt:48-67](https://github.com/llvm/llvm-project/blob/1ac9b999f8b521b5d6d82cf1a19858bc40a18c6a/runtimes/CMakeLists.txt#L48-L67) —— 把 `LLVM_ENABLE_RUNTIMES` 这个分号分隔的字符串解析成一个个项目名，找到每个项目（如 `libc`、`compiler-rt`）对应的源码目录，最后 `add_subdirectory` 进入它们各自的 `CMakeLists.txt`。

这段代码揭示了两件事：
1. **`runtimes/` 是分发型入口**：它不直接编译 libc，而是「路由」到 `../libc`、`../compiler-rt`。
2. **为什么必须带 `compiler-rt`**：因为 Scudo 的实现在 `compiler-rt` 里，而 libc 的 `LLVM_LIBC_INCLUDE_SCUDO=ON` 需要它。两行变量（`LLVM_LIBC_INCLUDE_SCUDO=ON` 配 `COMPILER_RT_BUILD_SCUDO_STANDALONE_WITH_LLVM_LIBC=ON`）必须同时存在，正是为了让 libc 和 compiler-rt 「互相对得上」。

而 `LLVM_LIBC_FULL_BUILD` 的开关效应，体现在聚合阶段：

[lib/CMakeLists.txt:4-13](https://github.com/llvm/llvm-project/blob/1ac9b999f8b521b5d6d82cf1a19858bc40a18c6a/libc/lib/CMakeLists.txt#L4-L13) —— 当 `LLVM_LIBC_FULL_BUILD` 为真时，归档名是 `c`/`m`/`mvec`（即 `libc.a`/`libm.a`/`libmvec.a`）；否则（Overlay）只有一个 `llvmlibc`（即 `libllvmlibc.a`）。这就是「Full 模式产出 libc.a/libm.a」这一说法的源头。

构建场景的全景（含 Overlay/Full/Bootstrap/交叉编译）见：

[docs/build_concepts.md:12-67](https://github.com/llvm/llvm-project/blob/1ac9b999f8b521b5d6d82cf1a19858bc40a18c6a/libc/docs/build_concepts.md#L12-L67) —— 列出 5 种构建场景，并点明「runtimes 构建」是大多数贡献者最常用的、最快的路径。本讲用的就是场景 2（Full Build Mode）。

#### 4.2.4 代码实践

1. **目标**：亲手配置一次 Full 模式的 runtimes 构建，并验证产物目录结构。
2. **步骤**：执行上面那条 `cmake` 命令。配置成功后，观察 `build/` 目录。
3. **观察**：`build/` 下应出现 `libc/`、`compiler-rt/` 等子目录；`build/compile_commands.json` 应存在（因 `CMAKE_EXPORT_COMPILE_COMMANDS=ON`）。
4. **预期结果**：CMake 末尾打印 `Generating done`（或类似），无 `FATAL_ERROR`。
5. **若报「找不到 `<asm/unistd.h>`」**：说明缺 `gcc-multilib`（见 4.1），安装后重新配置即可。这一现象在 [full_host_build.md](https://github.com/llvm/llvm-project/blob/1ac9b999f8b521b5d6d82cf1a19858bc40a18c6a/libc/docs/full_host_build.md#L16-L24) 中有明确记载。**待本地验证**具体报错文案，不同发行版措辞略有差异。

#### 4.2.5 小练习与答案

- **练习 1**：把命令里的 `LLVM_LIBC_FULL_BUILD=ON` 改成 `OFF`（或不写），构建产物的「名字」会发生什么变化？
- **答案**：根据 [lib/CMakeLists.txt:9-12](https://github.com/llvm/llvm-project/blob/1ac9b999f8b521b5d6d82cf1a19858bc40a18c6a/libc/lib/CMakeLists.txt#L9-L12)，Overlay 模式下归档名变成 `llvmlibc`，即只产出 `libllvmlibc.a`，不再有独立的 `libc.a`/`libm.a`。
- **练习 2**：为什么配置命令里要把 `libc` 和 `compiler-rt` 一起放进 `LLVM_ENABLE_RUNTIMES`？
- **答案**：因为本命令打开了 `LLVM_LIBC_INCLUDE_SCUDO=ON`，而 Scudo 的实现在 `compiler-rt` 中；两者必须同时编译才能对接。如果你不想要 Scudo，理论上可以只编 `libc`，但官方上手命令默认带 Scudo 以获得完整的 malloc。

---

### 4.3 编译与测试：ninja 目标体系

#### 4.3.1 概念说明

配置好之后，所有「要做什么」都通过 `ninja` 的**目标（target）**来表达。LLVM-libc 暴露了三层目标：

1. **库目标**：`libc`（C 库）、`libm`（数学库）——分别编出 `libc.a`、`libm.a`。
2. **测试伞目标**：`check-libc`——构建并运行所有单元测试。
3. **单个测试目标**：形如 `libc.test.src.<头文件>.<函数>_test.__unit__`——只跑一个函数的单元测试。

掌握这套命名，你就能精确地「只编我要的那一块」。

#### 4.3.2 核心流程

官方推荐的三连：

```sh
# 1) 构建 C 库 + 数学库 + 跑全部单元测试
ninja -C build libc libm check-libc

# 2) 只跑某一个函数的单元测试（以 ctype.h 的 isalpha 为例）
ninja -C build libc.test.src.ctype.isalpha_test.__unit__
```

测试目标名的构成规则（要记住的套路）：

```
libc.test.src.<目录>.<函数>_test.__unit__
       │     │      │        │         │
       │     │      │        │         └─ 固定后缀，表示「单元测试可执行目标」
       │     │      │        └─ 源文件名（去扩展名）：isalpha_test
       │     │      └─ src 下的子目录：ctype（对应 ctype.h）
       │     └─ 表示这是「测试」目标
       └─ 项目名前缀
```

所以「`src/ctype/isalpha_test.cpp`」对应的测试目标就是 `libc.test.src.ctype.isalpha_test.__unit__`——目标名几乎是源码路径的点分形式，这正是 u1-l2 讲过的「目录路径 = 配置坐标」约定在测试体系里的延续。

#### 4.3.3 源码精读

**库目标怎么来的**：`libc`/`libm` 不是手写的 target，而是由 `add_entrypoint_library` 把成百上千个入口点聚合而成。

[lib/CMakeLists.txt:23-39](https://github.com/llvm/llvm-project/blob/1ac9b999f8b521b5d6d82cf1a19858bc40a18c6a/libc/lib/CMakeLists.txt#L23-L39) —— 用 `add_entrypoint_library` 把 `TARGET_LIBC_ENTRYPOINTS`（Full 模式下列出的所有 C 库入口点）聚合成 `libc` 目标，并把归档输出名设为 `c`（即 `libc.a`）。`libm` 同理用 `TARGET_LIBM_ENTRYPOINTS` 聚合，输出名 `m`。这一步把「离散的入口点」缝合成「一个静态库」。

**check-libc 怎么来的**：它是一个 lit 测试伞目标。

[test/CMakeLists.txt:29-34](https://github.com/llvm/llvm-project/blob/1ac9b999f8b521b5d6d82cf1a19858bc40a18c6a/libc/test/CMakeLists.txt#L29-L34) —— 用 `add_lit_testsuite` 定义 `check-libc`，让它去 `${LIBC_BUILD_DIR}/test` 下跑 lit；并把 `check-libc-build`（只构建测试可执行文件、不运行）作为它的依赖。

**单个测试目标的 `.__unit__` 后缀怎么来的**：

[cmake/modules/LLVMLibCTestRules.cmake:1027](https://github.com/llvm/llvm-project/blob/1ac9b999f8b521b5d6d82cf1a19858bc40a18c6a/libc/cmake/modules/LLVMLibCTestRules.cmake#L1027) —— 调用 `add_libc_unittest(${test_name}.__unit__ ...)`，正是这一行给单元测试目标加了固定的 `.__unit__` 后缀。所以你在命令行里写的 `...isalpha_test.__unit__` 对应的就是这里生成的那个可执行目标。

#### 4.3.4 代码实践

1. **目标**：构建库 + 运行全部测试 + 单独运行一个测试，并记录命令与输出。
2. **步骤**：
   ```sh
   ninja -C build libc libm check-libc
   ninja -C build libc.test.src.ctype.isalpha_test.__unit__
   ```
3. **观察**：
   - 第一条命令末尾应打印测试汇总（通过/失败数量）。
   - 第二条命令应只编译并运行 `isalpha_test`，输出类似 `Running main() ...` 与 `PASSED`。
   - 构建产物落点：`build/libc/lib/libc.a`、`build/libc/lib/libm.a`（路径以实际构建树为准）。
4. **预期结果**：全部测试通过，`isalpha_test` 显示成功。
5. **关于输出文案**：不同测试的具体打印格式取决于 LLVM 自带的测试框架（test/UnitTest，将在 u10-l1 详讲），**待本地验证**确切文本。但「目标能被 ninja 找到并执行」这一行为是确定的。

#### 4.3.5 小练习与答案

- **练习 1**：`check-libc` 和 `libc.test.src.ctype.isalpha_test.__unit__` 是什么关系？
- **答案**：前者是「跑全部」的伞目标（经 lit 调度），后者是「只跑一个」的精确目标（直接是一个测试可执行文件）。后者也是前者构建依赖树中的一个叶子节点。
- **练习 2**：如果想只构建、不运行测试（比如在没有运行环境的交叉编译场景），应该用哪个目标？
- **答案**：用 `check-libc-build`（见 [test/CMakeLists.txt:29](https://github.com/llvm/llvm-project/blob/1ac9b999f8b521b5d6d82cf1a19858bc40a18c6a/libc/test/CMakeLists.txt#L29)），它只负责把测试可执行文件编出来而不运行。

---

### 4.4 Hello World 链接：crt1.o、libc.a 的手工拼装

#### 4.4.1 概念说明

Full 模式产出的 `libc.a`/`libm.a` 是「原材料」——它们不包含程序入口。一个真正的可执行文件还需要：

- **启动对象 `crt1.o`**：提供程序入口 `_start`，负责在调用 `main` 之前初始化运行时（如 TLS 线程局部存储）。这部分代码在 `startup/` 目录（详见 u8-l2）。
- **编译器自带头文件**：如 `stdarg.h`、`stddef.h`——它们属于 Clang 的 resource dir，不属于 libc。
- **正确的链接顺序**：先 `crt1.o`（入口），再 `libc.a`（函数实现），再 `libm.a`（数学）。

而 `-nostdinc`/`-nostdlib` 这两个标志的含义是：**不要用系统的头文件和库**——这正是 Full 模式的灵魂：我们要让这个程序「眼里只有 LLVM-libc」。

#### 4.4.2 核心流程

官方 Hello World 链接命令：

```sh
clang -nostdinc -nostdlib hello.c -o hello \
  -I build/libc/include \
  -I $(clang -print-resource-dir)/include \
  build/libc/startup/linux/crt1.o \
  build/libc/lib/libc.a \
  build/libc/lib/libm.a
```

逐段拆解：

| 片段 | 作用 |
|------|------|
| `-nostdinc` | 不搜索系统默认头文件目录（避免混入系统 libc 的 `stdio.h`）。 |
| `-nostdlib` | 不链接系统默认库（避免混入系统 libc 的 `printf` 实现）。 |
| `-I build/libc/include` | 用 **LLVM-libc 生成的公共头文件**（含 `stdio.h` 等）。 |
| `-I $(clang -print-resource-dir)/include` | 补上 Clang 自带头文件（`stddef.h`、`stdarg.h` 等），这些不属于 libc。 |
| `build/libc/startup/linux/crt1.o` | 链入启动对象，提供 `_start` 入口。 |
| `build/libc/lib/libc.a` | 链入 C 库（`printf` 等实现在这里）。 |
| `build/libc/lib/libm.a` | 链入数学库。 |

流程示意：

```
内核 exec → _start (来自 crt1.o)
                │  初始化运行时 / TLS
                ▼
              main()  ──调用──►  printf()  ──实现在──►  libc.a
                │
                ▼
            返回 / exit
```

> 为什么要 `-nostdinc -nostdlib`？因为 Full 模式的目标是「程序的 libc 完全由 LLVM-libc 提供」。如果不禁用系统默认路径，链接器可能优先选用系统的 `printf`，就失去了 Full 模式的意义。Overlay 模式则恰恰相反——它要和系统 libc 共存，所以不这样做（对比见 4.4.5）。

#### 4.4.3 源码精读

**Hello World 源码本身**极简：

[examples/hello_world/hello_world.c:9-14](https://github.com/llvm/llvm-project/blob/1ac9b999f8b521b5d6d82cf1a19858bc40a18c6a/libc/examples/hello_world/hello_world.c#L9-L14) —— 只有 `#include <stdio.h>` 和一个 `printf("Hello, World\n")`。注意：源码里看不出它用的是 LLVM-libc 还是系统 libc——**区别全在编译/链接命令里**。

**`crt1.o` 是怎么产出的**：它不是单个源文件编出来的，而是**多个对象合并（relocatable merge）**而成的。

[startup/linux/CMakeLists.txt:133-140](https://github.com/llvm/llvm-project/blob/1ac9b999f8b521b5d6d82cf1a19858bc40a18c6a/libc/startup/linux/CMakeLists.txt#L133-L140) —— 用 `merge_relocatable_object(crt1 ...)` 把架构相关的 `start`、`tls`、`irelative`，加上架构无关的 `do_start`、`gnu_property_section` 合并成单一的 `crt1.o`。这就是为什么链接命令里只出现一个 `crt1.o`——它内部已经打包了「架构入口 + 通用启动逻辑」。

[startup/linux/CMakeLists.txt:154-163](https://github.com/llvm/llvm-project/blob/1ac9b999f8b521b5d6d82cf1a19858bc40a18c6a/libc/startup/linux/CMakeLists.txt#L154-L163) —— 定义 `libc-startup` 伞目标，并 `install` 这些 `.o` 启动对象。`do_start`（`do_start.cpp`）就是真正「设置运行时、初始化 TLS、再跳转到 `main`」的那段代码（其细节留到 u8-l2 详解）。

**官方链接命令的权威来源**：

[docs/getting_started.md:74-87](https://github.com/llvm/llvm-project/blob/1ac9b999f8b521b5d6d82cf1a19858bc40a18c6a/libc/docs/getting_started.md#L74-L87) —— 给出 `-nostdinc -nostdlib` 加三个 `-I`/对象/归档参数的完整命令，并标注输出为 `Hello world from LLVM-libc!`。

#### 4.4.4 代码实践

1. **目标**：把一个 `hello.c` 用 LLVM-libc 链接成可执行文件并运行，亲眼看到输出。
2. **步骤**：
   ```sh
   # 先写一个 hello.c（内容同 examples/hello_world/hello_world.c）
   clang -nostdinc -nostdlib hello.c -o hello \
     -I build/libc/include \
     -I $(clang -print-resource-dir)/include \
     build/libc/startup/linux/crt1.o \
     build/libc/lib/libc.a \
     build/libc/lib/libm.a
   ./hello
   ```
3. **观察**：
   - 链接阶段：因为 LLVM-libc 尚未实现全部函数，复杂程序可能因「未实现符号」而链接失败；`hello.c` 这种只用 `printf` 的简单程序应当成功。
   - 运行阶段：终端应打印 `Hello, World`（或你在源码里写的字符串）。
4. **预期结果**：终端打印 `Hello, World`，证明 libc.a + crt1.o 已经正确协同。
5. **待本地验证**：精确的产物路径（`build/libc/startup/linux/crt1.o` 等）取决于你的构建目录结构，若路径不符，可在 `build/` 下用 `find build -name crt1.o` 定位实际位置。

> **进阶对照**：仓库里的 `examples/` 还提供了「用 CMake 而非裸命令」链接示例的方式。[examples/hello_world/CMakeLists.txt:5-8](https://github.com/llvm/llvm-project/blob/1ac9b999f8b521b5d6d82cf1a19858bc40a18c6a/libc/examples/hello_world/CMakeLists.txt#L5-L8) 调用 `add_example(hello_world hello_world.c)`，而 [examples/examples.cmake:7-15](https://github.com/llvm/llvm-project/blob/1ac9b999f8b521b5d6d82cf1a19858bc40a18c6a/libc/examples/examples.cmake#L7-L15) 在 Full 模式下用 `-static -rtlib=compiler-rt -fuse-ld=lld`、在 Overlay 模式下用 `-l:libllvmlibc.a`——这正是 Full/Overlay 两种模式在「链接层」的直接对照。

#### 4.4.5 小练习与答案

- **练习 1**：如果去掉 `-nostdlib`，会发生什么？
- **答案**：链接器会同时看到系统 libc 和 LLVM-libc，可能导致符号冲突或优先选用系统的 `printf`，从而失去 Full 模式的「完全替换」语义。Full 模式必须配合 `-nostdlib`/`-nostdinc` 才能保证纯净。
- **练习 2**：对比 Full 与 Overlay 在示例 CMake 里的链接差异，说出 Overlay 为什么用 `-l:libllvmlibc.a` 而不是 `-nostdlib`。
- **答案**：Overlay 模式（见 [examples/examples.cmake:9-12](https://github.com/llvm/llvm-project/blob/1ac9b999f8b521b5d6d82cf1a19858bc40a18c6a/libc/examples/examples.cmake#L9-L12)）要**和系统 libc 共存**，只让 LLVM-libc 的少数函数通过链接顺序覆盖系统实现，所以它**保留**系统库、额外链入 `libllvmlibc.a`；而 Full 模式要**完全替换**，所以禁用系统库。这一对照也是 u1-l4 的核心议题。

---

## 5. 综合实践

把本讲 4 个模块串起来，完成一次**端到端的「从配置到验证」**：

1. **准备**：确认依赖（模块 4.1）。
2. **配置**：用模块 4.2 的 `cmake` 命令配置 runtimes Full 构建。
3. **构建并测试**：
   - 跑 `ninja -C build libc libm check-libc`（模块 4.3）。
   - 单独跑 `ninja -C build libc.test.src.ctype.isalpha_test.__unit__`，把**命令和输出原文**记录到一个笔记文件里（这一步直接对应本讲的 practice_task）。
4. **链接验证**：写一个 `hello.c`，用模块 4.4 的裸链接命令生成可执行文件并运行。
5. **进阶观察**：
   - 用 `nm build/libc/lib/libc.a 2>/dev/null | grep -w printf` 看 `printf` 是否在归档里（验证 `libc.a` 真的含函数实现）。
   - 用 `file build/libc/startup/linux/crt1.o` 确认 `crt1.o` 是可重定位对象（relocatable）。

**交付物**：一份包含「配置命令 + 测试命令与输出 + hello 运行截图/文本 + nm 结果」的记录。这份记录既证明你跑通了本讲，也是后续讲义（尤其 u1-l4 构建模式对比、u8-l2 启动流程）的实操基础。

> 说明：以上命令均来自官方文档，行为是确定的；但每条命令的具体输出文案、以及构建树里产物的精确相对路径，会随发行版和 Clang 版本略有不同，请以本地实际为准（关键处已标「待本地验证」）。

## 6. 本讲小结

- LLVM-libc 的依赖核心是 **Clang（≥15）+ CMake + Ninja + gcc-multilib**，其中 `gcc-multilib` 关键在于提供 `/usr/include/asm` 符号链接。
- 构建根在 **`runtimes/`**（不是 `libc/`），用 `LLVM_ENABLE_RUNTIMES="libc;compiler-rt"` 选择编译项；带 `compiler-rt` 是为了 Scudo 分配器。
- `LLVM_LIBC_FULL_BUILD=ON` 选 **Full 模式**，产出独立 `libc.a`/`libm.a`；OFF（默认）则是产出 `libllvmlibc.a` 的 Overlay 模式。
- 编译用 `ninja -C build libc libm check-libc`；单个测试目标命名遵循 `libc.test.src.<头文件>.<函数>_test.__unit__` 的点分路径约定。
- Hello World 链接靠 `-nostdinc -nostdlib` 屏蔽系统库，再手工链入 `crt1.o`（程序入口）+ `libc.a` + `libm.a`，并补上 Clang resource dir 的自带头文件。
- `crt1.o` 由 `startup/linux/` 下多个对象（`start`/`tls`/`do_start` 等）relocatable 合并而成，负责 `main` 之前的运行时初始化。

## 7. 下一步学习建议

本讲让你「跑通了 Full 构建」。接下来建议：

1. **横向对比构建模式**：进入 [u1-l4 构建模式：Overlay vs Full](u1-l4-build-modes-overlay-vs-full.md)，理解 Overlay 模式如何用链接顺序覆盖系统 libc，以及为什么 `fopen` 这类函数不能放进 `libllvmlibc.a`。
2. **纵向看一个函数的全流程**：进入 [u1-l5 第一个入口点全流程：以 isalpha 为例](u1-l5-first-entrypoint-isalpha.md)，把你刚才能编译、能测试的 `isalpha` 从 YAML 一路追到测试，建立「一个函数如何存在于整个体系」的直觉。
3. **深入构建规则**：若你想为新函数加 CMake 注册，可预习 [u2-l3 CMake 构建规则详解](u2-l3-cmake-build-rules.md)，搞懂 `add_entrypoint_object`/`add_entrypoint_library` 的内部机制。
4. **进阶阅读源码**：想理解 `crt1.o` 里的 `do_start` 到底怎么初始化运行时再跳到 `main`，可后续阅读 [startup/linux/do_start.cpp](https://github.com/llvm/llvm-project/blob/1ac9b999f8b521b5d6d82cf1a19858bc40a18c6a/libc/startup/linux/do_start.cpp)，对应 u8-l2 程序启动流程。

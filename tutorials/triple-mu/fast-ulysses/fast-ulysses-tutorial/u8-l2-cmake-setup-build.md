# 构建系统：CMakeLists 与 setup.py

## 1. 本讲目标

fast-ulysses 的全部 C++/CUDA 源码只编出一个扩展 `_C`（u1-l4 已建立这张地图），但「怎么编」本身就是一门工程课。学完本讲，你应该能够：

1. 解释 `CMAKE_CUDA_ARCHITECTURES` 在 [CMakeLists.txt](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/CMakeLists.txt) 里被**三次定格**的原因：`project()` 会把它无条件定义成编译器默认值、`find_package(Torch)` 又会把它改写成 `OFF`，所以在错误的时刻读它会读到假象。
2. 说出 Blackwell（sm_100/sm_120）需要 CUDA 12.8 的守卫为什么放在 `project()` 之后、为什么只对「确实请求了 Blackwell」的列表生效。
3. 追踪 `c10d::register_work` 的 nm 符号探测如何一路变成编译宏 `FAST_ULYSSES_HAS_WORK_REGISTRY`、再变成 [src/work.cc](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/work.cc) 里两条运行时路径（`AsyncCollectiveTensor` 与 `CompletedHandle`）的差异。
4. 说明 `$ORIGIN/../torch/lib` 这个可重定位 RUNPATH 解决什么问题，以及为什么构建**不能**在 pip 的 build isolation 里运行、torch 依赖为什么钉死到 minor。

## 2. 前置知识

本讲是纯「构建期」课程，不需要 GPU，但需要几个概念铺垫：

- **CMake 的两个阶段**：`cmake -S . -B build` 跑的是 *configure* 阶段（执行 `CMakeLists.txt`，生成 `CMakeCache.txt` 和构建脚本）；`cmake --build build` 跑的是 *build* 阶段（真正调 nvcc/编译器）。本讲大部分内容发生在 configure 阶段。
- **`project()` 不只是起个名字**：`project(fast_ulysses LANGUAGES CXX CUDA)` 会做编译器检查（编一个测试程序验证 nvcc 可用），并在此过程中设置 `CMAKE_CUDA_COMPILER_VERSION`、`CMAKE_NM` 等变量。
- **CUDA 架构（arch / sm）**：nvcc 为每个目标架构（如 sm_80、sm_90）单独编译一份设备码（一个 "device pass"），用 `-gencode` 控制。架构越多，编译越慢、产物越大。
- **C++ name mangling 与 `nm`**：C++ 编译器把 `c10d::register_work(const at::Tensor&, ...)` 这类签名编码成 `_ZN4c10d13register_workERKN2at6Tensor...` 这样的符号名；`nm -D --defined-only 库文件.so` 列出该动态库**实际导出**的符号。
- **动态链接的搜索路径**：加载 `_C.so` 时，链接器要找到 `libtorch_cpu.so` 等；搜索顺序涉及 `DT_RUNPATH` 条目与 `LD_LIBRARY_PATH`。`$ORIGIN` 是「本文件所在目录」的占位符。
- **pip build isolation（PEP 517）**：`pip install` 默认新建一个隔离环境，里面只装 `pyproject.toml` 的 `[build-system] requires` 声明的东西，再在里面跑构建——这个默认行为对本项目是致命的，原因见 4.4。
- 承接 u1-l4：仓库分层（python/ 门面 → bindings.cc → group.cc → a2a_plan.cc → transfer.cu/barrier.cu），以及 u7-l3 讲过的 work registry 语义（本讲只关心它的**构建期探测**）。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [CMakeLists.txt](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/CMakeLists.txt) | configure 阶段的总脚本：arch 快照与守卫、torch 发现、nm 探测、`_C` 目标的编译/链接属性 |
| [setup.py](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/setup.py) | setuptools 门面：调用 cmake、持久 build 目录、torch 精确 minor 钉住、生成 `_build_meta.py` |
| [pyproject.toml](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/pyproject.toml) | PEP 517 声明：`requires` 里**故意只有 setuptools**；version 与 dependencies 是 dynamic |
| [docs/develop.md](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/develop.md) | 开发者视角：安装命令、持久 `build/`、CI 能证明什么 |
| [docs/install.md](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/install.md) | 用户视角：源码构建的环境变量表、`--no-build-isolation` 的原因 |
| [src/work.cc](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/work.cc) | nm 探测结果的**消费者**：`#if FAST_ULYSSES_HAS_WORK_REGISTRY` 的两条运行时路径 |

一条主线贯穿全讲：**同一个事实（架构表、符号是否存在、链接目标）要在正确的时刻读取一次，然后定格、落盘、到处一致**。

## 4. 核心概念与源码讲解

### 4.1 arch 快照时序：CMAKE_CUDA_ARCHITECTURES 的三次定格

#### 4.1.1 概念说明

目标架构表有两个入口：

- 走 pip：环境变量 `FAST_ULYSSES_CUDA_ARCH`（setup.py 读取，转成 `-DCMAKE_CUDA_ARCHITECTURES` 传给 cmake）；
- 走裸 cmake：直接 `-DCMAKE_CUDA_ARCHITECTURES=90`。

问题在于 `CMAKE_CUDA_ARCHITECTURES` 这个变量在 configure 过程中会被**两个强力角色改写**：

1. `project(LANGUAGES CUDA)` 的编译器检查结束后，变量**总是已定义**——用户没传时它回落到编译器自己的默认值 52；
2. `find_package(Torch)` 会把它**改成 `OFF`**，改由 `TORCH_CUDA_ARCH_LIST` 环境变量通过自己的 `-gencode` 标志驱动架构选择。

所以「现在变量的值」在三个时刻含义完全不同。CMakeLists 的解法是在每个污染点**前后**各拍一次快照，再用第三步把最终列表固化进目标属性和二进制里的宏。

#### 4.1.2 核心流程

```text
t0  用户意图：-DCMAKE_CUDA_ARCHITECTURES=90（或 setup.py 的 FAST_ULYSSES_CUDA_ARCH=90）
t1  快照一：FAST_ULYSSES_ARCH_REQUESTED = 变量原值          ← 必须在 project() 之前
t2  project(fast_ulysses LANGUAGES CXX CUDA)
      ├─ 编译器检查：变量未设 → 用编译器默认(52) 编测试程序
      └─ 检查过后变量「永远已定义」（最少是 52），空值语义丢失
t3  快照一为空 → 填默认 "80;90;100;120"
t4  快照二：FAST_ULYSSES_ARCH_LIST = 最终生效列表            ← 必须在 find_package(Torch) 之前
t5  把列表转成点分形式写入 TORCH_CUDA_ARCH_LIST 环境变量（90 → 9.0）
t6  find_package(Torch) → CMAKE_CUDA_ARCHITECTURES 变成 OFF
t7  消费（第三次定格）：
      ├─ target 属性 CUDA_ARCHITECTURES = 快照二
      └─ 编译宏 FAST_ULYSSES_CUDA_ARCH_LIST="80,90,100,120" 烤进 _C（build_info 可查）
```

#### 4.1.3 源码精读

**快照一：在 `project()` 之前留住「用户没说话」这个信息。**

[CMakeLists.txt:7-15](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/CMakeLists.txt#L7-L15) 先把变量原样存进 `FAST_ULYSSES_ARCH_REQUESTED`，然后才 `project()`。注释说得很直白：`project(LANGUAGES CUDA)` 之后变量总是已定义、回落到编译器默认的 52，放在它后面的测试**无法区分「没请求」和「请求了 52」**——默认值逻辑会失效，裸 `cmake -S . -B build` 会产出一个只含 sm_52 的 .so，没有任何受支持的设备能跑它。

同一段还有一个更细的排序约束：**快照时故意不设置任何值**。如果在 `project()` 前就把默认表（含 Blackwell 的 100/120）塞进变量，旧工具链会死在 `project()` 自己的编译器检查里，报的是 nvcc 的原始错误——守卫（4.2）还没机会报出「工具链版本 + 两条出路」的人话。

[CMakeLists.txt:19-23](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/CMakeLists.txt#L19-L23) 用快照一判空后填默认 `80;90;100;120`，否则尊重用户请求：

```cmake
if ("${FAST_ULYSSES_ARCH_REQUESTED}" STREQUAL "")
    set(CMAKE_CUDA_ARCHITECTURES "80;90;100;120")
else ()
    set(CMAKE_CUDA_ARCHITECTURES "${FAST_ULYSSES_ARCH_REQUESTED}")
endif ()
```

**快照二：在 `find_package(Torch)` 之前抓住生效列表。**

[CMakeLists.txt:24-38](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/CMakeLists.txt#L24-L38) 立刻把结果存为 `FAST_ULYSSES_ARCH_LIST`，因为下面的 `find_package(Torch)` 会把 `CMAKE_CUDA_ARCHITECTURES` 设成 `OFF`、改从 `TORCH_CUDA_ARCH_LIST` 驱动——在那之后读变量只会读到 `OFF`。同一段还负责「点分化」：torch 消费 `9.0` 这种形式而不是 `90`，正则 `^(.+)(.)$` 在最后一位数字前插入小数点（90→9.0，100→10.0）。注释指出 setup.py 已经导出了这个环境变量，这里是为裸 cmake 调用补上——否则 `-DCMAKE_CUDA_ARCHITECTURES` 会被**静默忽略**。

**第三次定格：目标属性 + 烤进二进制的宏。**

[CMakeLists.txt:126-129](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/CMakeLists.txt#L126-L129) 把 `_C` 目标的 `CUDA_ARCHITECTURES` 属性显式设为快照二（不受全局变量被 Torch 改成 `OFF` 的影响）；[CMakeLists.txt:130-138](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/CMakeLists.txt#L130-L138) 再把列表用逗号连接成编译宏 `FAST_ULYSSES_CUDA_ARCH_LIST`（分号会被 `target_compile_definitions` 误读成多个定义，所以换成逗号）。这个宏的用途在 [src/bindings.cc:805-810](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L805-L810)：`build_info()` 把它报告给 Python。注释给出的理由是——**缺了本机 arch 的 .so 在运行时以不提 arch 的方式失败**（`no kernel image is available`），所以列表值得随身携带。

setup.py 侧的对应物是 [setup.py:103-108](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/setup.py#L103-L108)：读 `FAST_ULYSSES_CUDA_ARCH`（默认 `80;90;100;120`），用同样的「最后一位前插点」规则设置 `TORCH_CUDA_ARCH_LIST`，再在 [setup.py:123](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/setup.py#L123) 以 `-DCMAKE_CUDA_ARCHITECTURES={arch}` 传给 cmake。**两个入口、两个变量名，最终汇进同一条快照链**。

#### 4.1.4 代码实践

**实践目标**：亲眼看到「默认填充」与「用户指定」两条路产出不同的架构表，并理解缓存变量与普通变量的区别。

**操作步骤**（需要 cmake、nvcc 和可导入的 torch；没有工具链的读者走步骤 4 的纸面版）：

1. `cmake -S . -B build-s90 -DCMAKE_CUDA_ARCHITECTURES=90`
2. `cmake -S . -B build-def`（不带任何 -D）
3. 分别检查两处：
   - `grep "CMAKE_CUDA_ARCHITECTURES" build-s90/CMakeCache.txt build-def/CMakeCache.txt`
   - 在生成的构建脚本里找 gencode（Ninja 生成器看 `build.ninja`，Makefiles 看 `CMakeFiles/_C.dir/flags.make`）：`grep -o "compute_[0-9]*" build-def/build.ninja | sort -u`
4. 纸面推演：对照 [CMakeLists.txt:19-23](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/CMakeLists.txt#L19-L23)，写出两个 build 目录各自的 `FAST_ULYSSES_ARCH_LIST`。

**需要观察的现象与预期结果**（由源码推得，待本地验证）：

- `build-s90` 的缓存里有 `CMAKE_CUDA_ARCHITECTURES:STRING=90`（`-D` 传入的是缓存项）；gencode 只出现 `compute_90` 一组。
- `build-def` 的缓存里**大概率没有**这一项——L20 的默认值是普通 `set()`（目录作用域变量），不写缓存；但 gencode 应出现 `compute_80/90/100/120` 四组。这个「缓存里没有、编译命令里有」的落差正是快照链在工作。
- 两个目录的 configure 输出里都应出现 4.3 将观察的 `c10d::register_work available: ...` 状态行。

**预期结果**：架构表差异只由入口变量决定，与「谁改写了全局变量」无关——这就是三次定格买来的确定性。

#### 4.1.5 小练习与答案

**练习 1**：把 L19 的判空测试挪到 `project()` 之后会发生什么？
**答案**：`project()` 已把 `CMAKE_CUDA_ARCHITECTURES` 无条件定义为（至少）编译器默认 52，判空永假，默认表 `80;90;100;120` 永远不生效——裸 cmake 构建出只含 sm_52 的 .so，在任何受支持 GPU 上以 `no kernel image is available` 失败（注释原文：这个 .so "no supported device can run"）。

**练习 2**：为什么 `FAST_ULYSSES_ARCH_LIST` 必须在 `find_package(Torch)` 之前拍，而不是直接在链接目标时读 `CMAKE_CUDA_ARCHITECTURES`？
**答案**：`find_package(Torch)` 会把该变量设成 `OFF` 并改用 `TORCH_CUDA_ARCH_LIST` 驱动 `-gencode`；之后读到的永远是 `OFF`，既无法给 target 属性赋值，也无法烤进 `build_info()` 的宏。

**练习 3**：裸 `cmake -S . -B build`（不带 -D、不设环境变量）时，架构表从哪来？`TORCH_CUDA_ARCH_LIST` 又是谁设置的？
**答案**：架构表来自 L19-23 的默认 `80;90;100;120`；`TORCH_CUDA_ARCH_LIST` 由 [CMakeLists.txt:30-38](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/CMakeLists.txt#L30-L38) 从同一张表点分化后写入环境（仅当外部未设时），否则 `-DCMAKE_CUDA_ARCHITECTURES` 会被 torch 的发现逻辑静默忽略。

### 4.2 Blackwell 下限守卫：把 nvcc 的哑错误翻译成人话

#### 4.2.1 概念说明

sm_100/sm_120（Blackwell）只有 CUDA 12.8 及以上的 nvcc 才能发射。在旧工具链上请求它们，nvcc 报的是 `unsupported gpu architecture 'compute_100'`——**既不提工具链版本，也不给出路**。守卫的做法是：在 configure 阶段主动检测这个组合，用一条 `FATAL_ERROR` 同时报出工具链版本、nvcc 路径、两条出路和当时的请求列表。

它有三个刻意的设计约束：

- **只对确实请求了 Blackwell 的列表生效**：`FAST_ULYSSES_CUDA_ARCH=80;90` 在 CUDA 12.4 上能编能跑，守卫不拦（文档声明 12.8 是受支持地板，但守卫只拒绝 nvcc 「可证明编不出」的东西）；
- **必须在 `project()` 之后**：判据 `CMAKE_CUDA_COMPILER_VERSION` 是 `project()` 设置的；
- **必须在 torch 发现之前**：省得白跑一遍重型 `find_package`。

#### 4.2.2 核心流程

```text
for a in FAST_ULYSSES_ARCH_LIST:            # 例如 "80;90a;100-real;120"
    sm = a 的前导数字                        # "100a"、"120-real" → 100、120
    if sm >= 100: blackwell += ["sm_" + sm]
if blackwell 非空 且 nvcc 版本 < 12.8:
    FATAL_ERROR(版本 + nvcc 路径 + 两条出路 + 请求列表)
```

#### 4.2.3 源码精读

[CMakeLists.txt:51-57](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/CMakeLists.txt#L51-L57) 用正则 `^[0-9]+` 取每个架构 token 的前导数字——这样 `100a`、`120-real` 这类带后缀的写法也能正确归一成 100、120，再用 `GREATER_EQUAL 100` 做数值比较收集 Blackwell 项。

[CMakeLists.txt:58-70](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/CMakeLists.txt#L58-L70) 是守卫本体。报错信息分两条出路，直接对应两种真实处境：

- 目标 GPU **是** Blackwell：唯一修复是升级工具链——缺 arch 的 .so 能加载、在**第一次启动内核**时才以 `no kernel image is available for execution on the device` 失败；
- 目标 GPU **不是** Blackwell：用 `FAST_ULYSSES_CUDA_ARCH="80;90" pip install -e . --no-build-isolation` 砍掉编不出的架构。

还有一个变量选择的细节（[CMakeLists.txt:47-50](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/CMakeLists.txt#L47-L50) 的注释）：读的是 `CMAKE_CUDA_COMPILER_VERSION` 而不是 `CUDAToolkit_VERSION`——前者已被 `project()` 设好，且**按构造就是报错信息里点名的那个 nvcc 的版本**；读它不需要 `find_package`，守卫自己不可能因解析失败而翻车，未设置的版本也不会让 `VERSION_LESS` 拿空字符串比较出诡异结果。

#### 4.2.4 代码实践

**实践目标**：看到守卫的两种行为差异——「列表里有 Blackwell 且工具链旧」才拒绝。

**操作步骤**：

1. 若本机工具链 < 12.8：`cmake -S . -B build-bw -DCMAKE_CUDA_ARCHITECTURES="80;100"`，观察 configure 以 FATAL_ERROR 结束。
2. 若本机工具链 ≥ 12.8（更常见）：同一条命令会正常通过（守卫放行）；此时改跑纸面版——对照 [CMakeLists.txt:51-70](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/CMakeLists.txt#L51-L70)，手算输入 `"80;90a;100-real;120"` 时 `_blackwell` 的内容与报错文本里的 `${_blackwell}` 展开值。
3. 验证「按列表门控」：在旧工具链上 `cmake -S . -B build-ok -DCMAKE_CUDA_ARCHITECTURES="80;90"` 应当照常 configure（注释明说这个组合合法）。

**需要观察的现象与预期结果**：步骤 1 的报错应包含：`CUDA <版本> (nvcc: <路径>) cannot compile for sm_100`、两个 `If` 分支、以及 `Requested architectures: 80;100`。步骤 2 的手算答案：`_blackwell = sm_100, sm_120`（`90a` 归一成 90，不入选）。步骤 1、3 依赖旧工具链，**待本地验证**；无旧工具链时以步骤 2 的纸面推演为准。

**预期结果**：守卫只在「nvcc 可证明编不出」时出手，且报错把工具链版本与出路一次讲清——这是对 4.1 中「为什么不在 project() 前塞默认值」的呼应：那条路会在编译器检查里死于 nvcc 的原始错误。

#### 4.2.5 小练习与答案

**练习 1**：为什么守卫放在 `project()` 之后、`find_package(Torch)` 之前？
**答案**：判据 `CMAKE_CUDA_COMPILER_VERSION` 由 `project()` 设置，放之前读不到；放在 torch 发现之后则白付一次重型 `find_package` 的代价（注释：跑在 torch discovery 之前 "so it costs nothing"）。

**练习 2**：CUDA 12.4 的机器上请求 `80;90` 会怎样？请求 `80;120` 呢？
**答案**：`80;90` 正常构建运行（守卫按列表门控，sm_80/90 在 12.4 可发射）；`80;120` 触发 FATAL_ERROR，因为 120 归一后 ≥ 100 且 12.4 < 12.8。

**练习 3**：报错里为什么专门写「a .so without its arch loads and then fails at the first launch」？
**答案**：这是在解释**为什么必须在 configure 期拒绝而不是留到运行期**：缺 arch 的 .so 能正常 dlopen，直到第一个内核启动才报 `no kernel image`——一个既不提 arch 也不提构建的延迟失败；提前拒绝把诊断成本从「用户在 GPU 机器上调试」降到「构建者在 configure 输出里读一行」。

### 4.3 register_work 探测：nm 实测符号如何变成运行时行为

#### 4.3.1 概念说明

u7-l3 讲过：`c10d::register_work` 把 comm 流上的完成事件绑到结果张量的 storage，是异步调用能重叠的前提。但这个符号**不是每份 libtorch 都导出**。C++ 世界的惯例是「查头文件」，但头文件里的声明**说明不了 libtorch_cpu 实际导出了什么**——声明在、实现没编进库，照样链接失败。所以这里探测的是**库本身**：对 `libtorch_cpu.so` 跑 `nm -D --defined-only`，在导出符号表里找那个 mangled 前缀。

探测结果决定两件事：编译宏 `FAST_ULYSSES_HAS_WORK_REGISTRY` 是 1 还是 0，以及——由此——异步调用返回 `AsyncCollectiveTensor`（真重叠）还是 `CompletedHandle`（发射即等待、正确但无重叠）。

#### 4.3.2 核心流程

```text
find_library(torch_cpu, 只在 TORCH_INSTALL_PREFIX/lib 找, REQUIRED)
nm -D --defined-only libtorch_cpu.so  →  符号表文本
符号表里找 "_ZN4c10d13register_workERKN2at6Tensor"（mangled 前缀）
    ├─ 找到 → HAVE_C10D_REGISTER_WORK=ON  → 宏=1
    └─ 没找到（或 nm 失败）→ OFF → 宏=0 + configure 警告
宏 → src/work.cc 的 #if 分支 → 运行时两条路径
configure stdout 被 setup.py 捕获 → "available: ON" 字符串匹配 → _build_meta.HAS_WORK_REGISTRY
```

#### 4.3.3 源码精读

[CMakeLists.txt:99-108](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/CMakeLists.txt#L99-L108) 是探测本体：

- [L99](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/CMakeLists.txt#L99) 只在 torch 的安装前缀下找 `torch_cpu`（`NO_DEFAULT_PATH`，防止误捡系统里另一份 libtorch）；
- [L100-101](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/CMakeLists.txt#L100-L101) 执行 `nm -D --defined-only`。注意 `${CMAKE_NM}` **不加引号**（[L96-98](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/CMakeLists.txt#L96-L98) 注释）：若变量未设置，命令会失败、`RESULT_VARIABLE` 能记下来；加了引号则 CMake 会对空 COMMAND 直接报错，丢失「命令跑过但失败」与「没跑」的区分；
- [L104](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/CMakeLists.txt#L104) 用 `string(FIND)` 找子串 `_ZN4c10d13register_workERKN2at6Tensor`——它是 `c10d::register_work(const at::Tensor&, const c10::intrusive_ptr<c10d::Work>&)` 的 mangled 名的前缀，锁定了命名空间、函数名和第一个参数类型，足够精确；
- 找不到**不致命**（[L98](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/CMakeLists.txt#L98) 注释）：`src/work.cc` 会退化为「发射即等待」。

[CMakeLists.txt:147-154](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/CMakeLists.txt#L147-L154) 把结果变成宏，并在 OFF 分支打出一条把后果说透的警告：异步调用将返回 `CompletedHandle` 而不是 `AsyncCollectiveTensor`、调用者的流在发射时就得等待——**正确，但无重叠**。探测答案以状态行 `c10d::register_work available: ON/OFF`（[L109](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/CMakeLists.txt#L109)）打印，且**只在这里出现**（编译进扩展后就看不到了）。

运行时的消费者是 [src/work.cc:62-80](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/work.cc#L62-L80) 的 `register_stream_completion`：在 comm 流记录一个不计时事件后，宏为 1 时把它包成 `StreamEventWork` 注册进 work registry、返回 true；宏为 0 时（[L73-78](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/work.cc#L73-L78)）**没有任何东西能通过 torch 摸到这个事件**，于是当场对当前流插入 `cudaStreamWaitEvent` 再销毁事件、返回 false——等待被提前到发射点，重叠消失但次序正确。连头文件包含都被同一个宏控制（[src/work.cc:6-9](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/work.cc#L6-L9)），`StreamEventWork` 类整个只在宏为 1 时存在（[L15-60](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/work.cc#L15-L60)）。

最后一环在 setup.py：configure 的 stdout 被**捕获而非直接流出**（[setup.py:109-131](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/setup.py#L109-L131)），正是为了在 [setup.py:140](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/setup.py#L140) 用字符串匹配 `"c10d::register_work available: ON" in configure.stdout` 把答案写进 `_build_meta.HAS_WORK_REGISTRY`（无论如何都会把 stdout 打出来，configure 失败也能看到 CMake 说了什么）。于是同一条事实有了一条完整链：**nm 实测 → 编译宏 → 运行时分支 → configure 状态行 → `_build_meta` 落盘**。

#### 4.3.4 代码实践

**实践目标**：亲手复现探测的每一步，验证链条两端一致。

**操作步骤**：

1. 找到本机 torch 的 libtorch_cpu：`python -c "import torch,os;print(os.path.join(os.path.dirname(torch.__file__),'lib','libtorch_cpu.so'))"`
2. 手工跑探测：`nm -D --defined-only <上面的路径> | grep -c "_ZN4c10d13register_workERKN2at6Tensor"`（`-c` 数出现行数）。
3. 跑一次 `cmake -S . -B build-probe`，在输出里找 `c10d::register_work available:` 行。
4. 若做过 `pip install -e . --no-build-isolation`：`python -c "from fast_ulysses import _build_meta as m; print(m.HAS_WORK_REGISTRY, m.CUDA_ARCH_LIST)"`，再对照 `python -c "import fast_ulysses; print(fast_ulysses._C.build_info())"`。

**需要观察的现象与预期结果**：步骤 2 计数 ≥ 1（torch ≥ 2.10 的官方构建应导出该符号，具体**待本地验证**）；步骤 3 的状态行应为 `ON`，与步骤 2 一致；步骤 4 中 `_build_meta.HAS_WORK_REGISTRY` 为 True、`CUDA_ARCH_LIST` 为构建时的架构表（逗号分隔），`build_info()` 的 `cuda_arch_list` 同源。

**预期结果**：四个观测点给出**同一个答案**——这正是「事实只测一次、然后定格分发」的设计意图；任何一环不一致都意味着构建缓存过期（比如换 torch 后没重新 configure）。

#### 4.3.5 小练习与答案

**练习 1**：为什么匹配 mangled 名的**前缀子串**，而不是完整的 mangled 名？
**答案**：完整 mangled 名还要编码第二个参数 `c10::intrusive_ptr<c10d::Work>` 的模板实参嵌套，随 torch 版本细微变化容易脆；前缀已锁定命名空间 `4c10d`、函数名 `13register_work`（13 是名字长度）和第一参数 `RKN2at6Tensor`（`const at::Tensor&`），足以唯一识别，且对后续参数演化稳健。

**练习 2**：为什么探测目标选 `libtorch_cpu.so` 而不是头文件里有没有声明？
**答案**：声明只说明「源码里写过这个函数」，链接真正问的是「这份 libtorch_cpu **导出**了它吗」——源码版本、编译配置、隐藏可见性都可能让声明存在而符号缺失。`nm -D --defined-only` 列的是动态符号表里实际导出的符号，才是链接器看得到的事实（[L92-95](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/CMakeLists.txt#L92-L95) 注释）。

**练习 3**：宏为 0 时异步调用的完整行为差异是什么？
**答案**：`register_stream_completion` 走 [src/work.cc:73-78](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/work.cc#L73-L78)：立即对当前流 `cudaStreamWaitEvent` 并销毁事件、返回 false；Python 侧拿到 `CompletedHandle`——调用者的流在发射点就被排进等待，**正确但无重叠**。configure 期的警告（[L150-153](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/CMakeLists.txt#L150-L153)）把这整句话提前讲给了构建者。

### 4.4 RPATH 与构建隔离：链接谁、跑去哪、事实落盘

#### 4.4.1 概念说明

这个模块回答三个问题：

1. **装载**：`_C.so` 链接的 torch 全家桶住在 `<site-packages>/torch/lib`，不在加载器默认搜索路径上——装在任何虚拟环境、任何路径下都要能找到，所以 RPATH 写成相对 `$ORIGIN` 的形式，wheel 随处可放（relocatable）。
2. **构建环境**：pip 的 build isolation 会另起一个只有 setuptools 的干净环境（[pyproject.toml:1-3](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/pyproject.toml#L1-L3) 的 `requires` 里**故意没有 torch**）。但本项目的构建必须 import **用户环境里那份** torch——由此推出 `--no-build-isolation` 是硬要求，torch 也不能声明为构建依赖；而「必须链接将要对阵运行的那份 torch」在安装侧的镜像，就是把运行时依赖**精确钉到 minor**。
3. **事实落盘**：构建事实（版本、torch ABI、CUDA、arch 表、work registry 有无）写进生成的 `_build_meta.py`——因为 `build_info()` 要能回答的恰恰是 **dlopen 已经失败**的时刻（导入失败诊断需要知道「当时是对着什么编的」）。

#### 4.4.2 核心流程

装载路径解析（安装后）：

```text
import fast_ulysses
  → dlopen <site-packages>/fast_ulysses/_C.so
  → 需要 libtorch_cpu.so 等 → 查 DT_RUNPATH = $ORIGIN/../torch/lib
  → 展开为 <site-packages>/torch/lib ✓（无论 site-packages 在哪）
  → LD_LIBRARY_PATH 仍可覆盖（DT_RUNPATH 的性质，留作调试口）
```

构建耦合链：

```text
pyproject: build requires = 仅 setuptools（torch 故意缺席）
setup.py: import torch（用户环境那份）
  ├─ 找 CMake 前缀: python -c "import torch; print(torch.utils.cmake_prefix_path)"
  ├─ 链接该 torch → .so 内嵌 c10d::Work vtable / intrusive_ptr / pybind11 / TORCH_LIBRARY 注册
  └─ install_requires = torch==MAJOR.MINOR.*（钉到 minor）
在隔离环境里跑 → import torch 失败 → RuntimeError（打印三条修复指令）
```

#### 4.4.3 源码精读

**RPATH**：[CMakeLists.txt:142-146](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/CMakeLists.txt#L142-L146) 注释先点明前提——链接物里**只有 torch 的 lib 目录**不在默认搜索路径上；于是 `INSTALL_RPATH` 设为 `$ORIGIN/../torch/lib`（`_C.so` 住在 `site-packages/fast_ulysses/`，`../torch/lib` 恰是 torch 的库目录），并用 `BUILD_WITH_INSTALL_RPATH ON` 让构建产物直接带安装期 RPATH（release 流程的 [docs/develop.md:86-88](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/develop.md#L86-L88) 里，`tools/check_wheel.py` 正是对 ELF 与元数据把关的门）。最关键的一句注释：**保持 DT_RUNPATH、不用 `--disable-new-dtags` 退化成 DT_RPATH**——RUNPATH 可被 `LD_LIBRARY_PATH` 覆盖，给调试留了活口。顺带一提 [CMakeLists.txt:80-83](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/CMakeLists.txt#L80-L83)：Python 组件请求的是 `Development.Module` 而非 `Development`，因为目标只链 `Python::Module`（仅头文件）；普通 `Development` 还要讨 libpython，而 manylinux 的 CPython 根本不带——那会在所有实际构建 release wheel 的地方失败。

**构建隔离与 torch 钉住**：核心在 [setup.py:59-83](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/setup.py#L59-L83)。`_requires()` 先 `import torch`，把裸 `ModuleNotFoundError` 翻译成一条带三行修复指令的 `RuntimeError`（[L72-80](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/setup.py#L72-L80)）。为什么必须这么凶？docstring 给了三层理由：读版本需要「将要链接的那份 torch」，所以构建**不能**在 pip 隔离里跑；torch 也**不能**声明成构建依赖——pip 会往隔离环境里装它自己的副本，扩展就会链接那份而不是用户的；然后 [L82-83](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/setup.py#L82-L83) 从该 torch 的版本拼出 `torch=={major}.{minor}.*`。**为什么钉到 minor**：.so 内嵌 `c10d::Work` 的 vtable、op schema 里的 `c10::intrusive_ptr`、一个 pybind11 模块和 `TORCH_LIBRARY` 注册——**没有一个能活过 minor 升级**。CMake 侧的另一半在 [CMakeLists.txt:84-88](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/CMakeLists.txt#L84-L88)：通过 `python -c "import torch; print(torch.utils.cmake_prefix_path)"` 拿到 libtorch 的 CMake 前缀。[docs/install.md:176-177](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/install.md#L176-L177) 用一句话向用户复述了同一件事：CMake 经**已安装的** torch 定位 libtorch，所以 torch 在构建期必须可导入、且必须是打算共跑的那份。

**持久 build 目录与作业数**：[setup.py:95-101](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/setup.py#L95-L101) 把 CMake 构建树放在持久的 `./build`（可用 `FAST_ULYSSES_BUILD_DIR` 覆盖）而不是 pip 临时目录，编辑-重建只重编改动的翻译单元（[docs/develop.md:10-11](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/develop.md#L10-L11)）；-D 参数变化时 CMake 会自动重跑 configure，不用手动清目录。并发度由 [setup.py:31-51](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/setup.py#L31-L51) 的 `_build_jobs` 决定：每个作业 fork 一个 nvcc，nvcc 的 `-t0`（[CMakeLists.txt:155-157](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/CMakeLists.txt#L155-L157)）再按架构数并行跑设备编译趟，峰值并发是 \( j \times n_{\text{arch}} \)、峰值内存随之伸缩，所以作业数受**内存**而非核数约束，按每并发趟 3 GiB 预算：

\[ j_{\max} \;=\; \min\!\Big(n_{\text{cpu}},\;\Big\lfloor \frac{M_{\text{available}}}{3\,\text{GiB}\times n_{\text{arch}}} \Big\rfloor\Big) \]

——注释里的教训是：小 CI runner 若只看核数定 `-j`，四架构构建会被 OOM 杀到半路。

**`_build_meta.py`**：[setup.py:149-171](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/setup.py#L149-L171) 在扩展旁边生成这个模块，内容是一张事实表：`VERSION`（含 local 标签）、`TORCH_VERSION` / `TORCH_ABI`、`CUDA_VERSION`、`PYTHON_ABI`、`CUDA_ARCH_LIST`、`HAS_WORK_REGISTRY`。docstring 说明了它存在的理由：**dlopen 本身失败时 `build_info()` 答不了话，而那正是需要这些事实的时刻**——[python/fast_ulysses/__init__.py:3-10](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/python/fast_ulysses/__init__.py#L3-L10) 在导入失败时正是拿 `_diagnose.explain` 对照「构建时事实 vs 现场实况」给出归因。它不进 git（.gitignore）也**不进 sdist**（[MANIFEST.in:9-11](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/MANIFEST.in#L9-L11)：一份随源码发布的 `_build_meta` 会让所有源码安装都谎称 release 构建机的环境）；`--inplace` 安装时由 [setup.py:143-147](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/setup.py#L143-L147) 跟着扩展拷进源码树。

#### 4.4.4 代码实践

**实践目标**：验证 RUNPATH 的实际形态，并写下「为什么不能在 build isolation 里构建」的完整注释。

**操作步骤**：

1. 对一个已构建的扩展跑 `readelf -d <...>/fast_ulysses/_C*.so | grep -E "RUNPATH|RPATH"`（pip 安装的看 site-packages；源码 `pip install -e .` 后看 `python/fast_ulysses/`）。
2. 用 `ldd` 观察 libtorch 系列是否经该路径解析成功。
3. 在你的**学习笔记**（不要改仓库源码）里写一段注释，回答：为什么 `pip install .`（默认隔离）必然失败？为什么把 torch 加进 `[build-system] requires` 也不能解决问题？
4. （可选，需干净环境）在一个没装 torch 的 venv 里 `pip install .`，观察报错是否就是 [setup.py:74-79](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/setup.py#L74-L79) 的那段 RuntimeError。

**需要观察的现象与预期结果**：步骤 1 应看到 `RUNPATH`（而非旧式 `RPATH`）且包含 `$ORIGIN/../torch/lib`（**待本地验证**，auditwheel 处理过的 wheel 可能追加条目）；步骤 2 中 `libtorch_cpu.so` 等应解析到本环境 torch 的 lib 目录；步骤 4 的报错应以 "fast-ulysses builds against the torch already installed" 开头并列出 `pip install torch` / `pip install fast-ulysses --no-build-isolation` 两行指令（**待本地验证**）。

**预期结果**：装载正确性不依赖任何绝对路径，也不依赖 `LD_LIBRARY_PATH`；构建正确性则**只**依赖「构建与运行共用同一份 torch」这一件事。

#### 4.4.5 小练习与答案

**练习 1**：RPATH 为什么写 `$ORIGIN/../torch/lib` 而不是构建机的绝对路径？
**答案**：绝对路径把 wheel 钉死在构建机的目录布局上；`$ORIGIN` 在**加载时**按 .so 自身位置展开，`fast_ulysses/_C.so` 与 `torch/lib` 的相对关系在任何 site-packages、任何 venv 里都成立——wheel 可重定位（[L142-146](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/CMakeLists.txt#L142-L146) 注释："the wheel stays relocatable"）。

**练习 2**：为什么刻意**不**加 `--disable-new-dtags`（即保持 DT_RUNPATH、拒绝退化成 DT_RPATH）？
**答案**：DT_RPATH 的优先级高于 `LD_LIBRARY_PATH`，会把它彻底屏蔽；DT_RUNPATH 则允许 `LD_LIBRARY_PATH` 覆盖。留这个口子，调试时可以强行指向另一份 torch/lib 而不必重装（注释："so LD_LIBRARY_PATH can still override it"）。

**练习 3**：如果把 `torch` 写进 `pyproject.toml` 的 `[build-system] requires`，会发生什么？
**答案**：pip 会往隔离环境里装**它自己的** torch 副本，扩展链接的就是那份而不是用户环境的——构建「成功」，运行时对着用户的 torch 出现 `undefined symbol: _ZN3c10...`（[setup.py:64-69](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/setup.py#L64-L69) docstring 的原话："the extension would link that one instead of the user's"）。所以正确解是 `requires` 里只有 setuptools + 强制 `--no-build-isolation` + `import torch` 失败时打印指令。

## 5. 综合实践

把规格里的双配置对比做成一次完整排查（有工具链走 A 线，没有走 B 线）：

**A 线（真实构建）**

1. 两个独立构建树，分别用单架构与默认四架构 configure：
   ```bash
   cmake -S . -B build-s90 -DCMAKE_CUDA_ARCHITECTURES=90    # 裸 cmake 的写法
   cmake -S . -B build-def                                   # 默认 80;90;100;120
   ```
   注意：`FAST_ULYSSES_CUDA_ARCH=90` 是 **setup.py 的**环境变量（[setup.py:103](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/setup.py#L103)）；裸 cmake 要用 `-DCMAKE_CUDA_ARCHITECTURES`（[CMakeLists.txt:28-29](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/CMakeLists.txt#L28-L29) 注释解释了为什么两条路都要支持）。完整等价物：`FAST_ULYSSES_CUDA_ARCH=90 pip install -e . --no-build-isolation`。
2. 对比两份 configure 输出与产物，填这张表（预期值来自源码，待本地验证）：

   | 观察项 | build-s90 | build-def | 出处 |
   | --- | --- | --- | --- |
   | `CMakeCache.txt` 的 `CMAKE_CUDA_ARCHITECTURES` | `90` | （无此项——默认值是普通 set） | 4.1 |
   | 构建脚本中的 compute 列表 | 仅 `compute_90` | `compute_80/90/100/120` | 4.1 |
   | `c10d::register_work available:` 状态行 | 同 | 同（与 arch 无关） | 4.3 |
   | `ccache enabled:` 行（装了 ccache 才有） | 同 | 同 | [CMakeLists.txt:72-78](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/CMakeLists.txt#L72-L78) |
   | 构建耗时（`time cmake --build ... -j`） | 短 | 明显更长（4×设备趟） | [docs/install.md:172-174](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/install.md#L172-L174) |
   | `readelf -d _C*.so` 的 RUNPATH | `$ORIGIN/../torch/lib` | 同 | 4.4 |

3. 构建后跑 `python -c "import fast_ulysses; print(fast_ulysses._C.build_info())"`，确认 `cuda_arch_list` 与上表一致。
4. 写下本讲的收尾注释（≥3 条理由，各带 file:line）：为什么这个构建不能在 pip 的 build isolation 里运行？（提示：CMake 靠 `import torch` 找前缀；扩展必须链接用户那份 torch；把 torch 声明为构建依赖只会让 pip 装自己的副本。）

**B 线（纸面版，无工具链）**：对照 4.1–4.4 引用的行号把上表「预期值」列全部推导出来，并明确标注哪些结论待本地验证。

## 6. 本讲小结

- `CMAKE_CUDA_ARCHITECTURES` 会被 `project()`（回落默认 52、此后永非空）和 `find_package(Torch)`（改成 `OFF`）先后污染，所以在 `project()` 前拍「请求」快照、默认填充后拍「生效」快照、再以 target 属性 + 二进制宏完成第三次定格。
- Blackwell 守卫只对「确实请求了 sm_100/sm_120 且 nvcc < 12.8」出手，用 FATAL_ERROR 把 nvcc 的哑错误翻译成「版本 + 两条出路」，且放在 torch 发现之前零成本运行。
- `c10d::register_work` 探测的是 libtorch_cpu **实际导出的符号**（mangled 前缀子串）而非头文件声明；结果经编译宏分叉成 work.cc 的两条运行时路径（真重叠 vs CompletedHandle），再经 configure 状态行落进 `_build_meta.HAS_WORK_REGISTRY`。
- RUNPATH 用 `$ORIGIN/../torch/lib` 换来 wheel 可重定位，同时保留 `LD_LIBRARY_PATH` 覆盖口；构建必须 `--no-build-isolation`，因为扩展要链接**用户环境里那份** torch——`c10d::Work` vtable、`intrusive_ptr`、pybind11、`TORCH_LIBRARY` 注册都活不过 minor 升级，这也是 `torch==MAJOR.MINOR.*` 精确钉住的由来。
- 持久 `build/` 目录 + ccache + `-t0`（作业数按内存预算 \(\lfloor M/(3\,\text{GiB}\cdot n_{\text{arch}})\rfloor\)）让编辑-重建只碰改动的翻译单元；构建事实写进 `_build_meta.py`，专为「dlopen 已经失败」的时刻服务。

## 7. 下一步学习建议

- 下一讲 **u8-l3「wheel 矩阵与 ABI 匹配」**：本讲的 `torch==MAJOR.MINOR.*` 钉住在分发侧的镜像——36 个 wheel 的 (torch minor × CUDA major × CPython) 矩阵、PEP 440 local version 的排序陷阱，以及 `wheel-url` / `_diagnose` 如何用 `_build_meta.py` 里落盘的这些事实做归因。
- 想看构建产物的验收端：[docs/develop.md:82-97](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/develop.md#L82-L97) 的 release 流程（`tools/build_wheels.sh`、`tools/check_wheel.py` 对 ELF/metadata 的把关——本讲的 RUNPATH 与 `_build_meta` 正是它检查的对象）。
- 回看 u7-l3，把「探测 → 宏 → 行为差异」的两端再连一次：现在你应该能解释 `CompletedHandle` 分支是**谁在构建期决定启用的**。
- 若你对「为什么 torch minor 一动就全毁」想要实证：对两个 minor 不同的 torch 各构建一次，`nm -d` 对比扩展里的未解析符号（需双工具链，待本地验证）。

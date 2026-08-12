# 端到端跑通第一个矢量加法算子

## 1. 本讲目标

前两讲我们已经看清了 `.asc` 单源文件的 Host/Device 混合编译模型（u2-l1），也理解了 `<<<>>>` 启动语法和 ACL 运行时的内存与同步流程（u2-l2）。但那些结论都还是「纸上」的——本讲的目标是把它们**真正跑起来**。

学完本讲，你应当能够：

1. 说出 Ascend C 算子样例工程的目录组织方式，区分「自包含」与「脚本式」两种布局。
2. 独立完成一次端到端流程：`source set_env.sh` → `cmake` → `make` → 运行 → 校验。
3. 理解 `--npu-arch`、`CMAKE_ASC_RUN_MODE` 等关键编译选项的含义。
4. 说清楚输入数据从哪里来、输出结果如何与「真值（golden）」比对，并能动手修改样例参数跑通验证。

本讲是整个手册里**第一个要求你在本地敲命令的讲义**。即使你手边没有 NPU，也可以用 CPU 调试模式或 NPU 仿真模式跑通流程。

## 2. 前置知识

本讲承接 u2-l1 与 u2-l2 的结论，默认你已经理解下面这些概念（这里只做一句话回顾，不展开）：

- **`.asc` 单源文件**：Host（CPU）代码与 Device（AI Core）Kernel 写在同一文件里，由编译器按函数限定符拆分（u2-l1）。
- **Kernel 限定符**：`__vector__ __global__` 表示这是一个跑在 Vector 核上的 Kernel 入口；Kernel 的指针入参必须用 `__gm__` 标注（u2-l1）。
- **`<<<>>>` 启动语法**：SIMD 样例用三参数形式 `<<<numBlocks, dynUBufSize, stream>>>`，`numBlocks` 决定启动多少个核（u2-l2）。
- **ACL 运行时 8 步生命周期**：`aclInit` → `aclrtSetDevice` → 建流 → `aclrtMalloc`/`aclrtMallocHost` → `aclrtMemcpy`(H2D) → 启动 Kernel → `aclrtSynchronizeStream` → `aclrtMemcpy`(D2H) → 释放资源（u2-l2）。

本讲会**新用到**两个工程化概念，先建立直觉：

- **CMake 构建**：CMake 不直接编译，而是根据 `CMakeLists.txt` 生成 `Makefile`，再由 `make` 调用编译器产出可执行文件。Ascend C 在此基础上扩展了一个名为 `ASC` 的「语言」，让 CMake 能识别 `.asc` 文件并调用昇腾编译器（bisheng-compiler）。
- **运行模式（run mode）**：同一份 `.asc` 可以编译成三种产物——真正在 NPU 上跑（`npu`）、在 CPU 上做功能调试（`cpu`）、在主机上做 NPU 仿真（`sim`）。本讲样例默认 `npu`，没有硬件时改用 `cpu`/`sim` 即可。

> 本讲只把 Kernel 内部用到的 `GlobalTensor`/`LocalTensor`/`DataCopy`/`Add`/`PipeBarrier` 当作「黑盒调用」，它们的细节留给 U3（内存与搬运）、U4（矢量计算）展开。本讲聚焦**工程怎么组织、怎么编译运行、数据怎么来又怎么验**。

## 3. 本讲源码地图

本讲围绕 `01_add` 目录下的两个并列样例展开，主线是「自包含」的 `add` 样例，对照参考是「脚本式」的 `add_tpipe_tque` 样例。

| 文件 | 作用 | 在本讲中的角色 |
| --- | --- | --- |
| [add/add.asc](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/examples/01_simd_cpp_api/00_introduction/01_add/add/add.asc) | Kernel 实现 + Host 调用 + 数据生成 + 结果校验，四合一 | **主线**：一个文件就是一个完整可运行样例 |
| [add/CMakeLists.txt](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/examples/01_simd_cpp_api/00_introduction/01_add/add/CMakeLists.txt) | 编译工程配置（架构、语言、编译选项） | 讲清 `--npu-arch` 与 ASC 语言如何接入 |
| [add/README.md](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/examples/01_simd_cpp_api/00_introduction/01_add/add/README.md) | 样例说明与编译运行步骤 | 实践命令的依据 |
| [add_tpipe_tque/scripts/gen_data.py](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/examples/01_simd_cpp_api/00_introduction/01_add/add_tpipe_tque/scripts/gen_data.py) | 用 NumPy 生成输入与真值 `.bin` | **对照**：脚本式数据生成 |
| [add_tpipe_tque/scripts/verify_result.py](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/examples/01_simd_cpp_api/00_introduction/01_add/add_tpipe_tque/scripts/verify_result.py) | 用容差比对输出与真值 | **对照**：脚本式结果校验 |

> 提醒：仓库根目录的 `cmake/asc/asc_modules/FindASC.cmake` 是 `find_package(ASC)` 的实现来源，它会被打包进 CANN；样例工程 `find_package(ASC)` 找到的是 CANN 里已安装的那一份，而非仓库源码。这一打包细节属于 u1-l3，本讲不展开。

## 4. 核心概念与源码讲解

### 4.1 样例工程结构

#### 4.1.1 概念说明

一个「算子样例」要回答三个问题：**Kernel 怎么写、Host 怎么调、结果对不对**。Ascend C 样例有两种典型布局：

- **自包含布局（self-contained）**：把 Kernel、Host 调用、输入数据生成、结果校验全部写进**同一个 `.asc` 文件**，编译出一个 `demo` 可执行文件，运行即自检。`add` 样例就是这种。
- **脚本式布局（script-based）**：Kernel 与 Host 测试驱动仍在 `.asc` 里，但数据生成与结果校验交给 Python 脚本（`gen_data.py` / `verify_result.py`），数据通过 `.bin` 文件在 Host 与可执行程序之间流转。`add_tpipe_tque` 样例就是这种。

两者没有优劣之分：自包含布局适合教学与小样例，一个文件就能看全链路；脚本式布局更贴近真实算子交付流程（算子二进制 + 独立数据），方便替换大测试集。

#### 4.1.2 核心流程

`add` 样例的工程结构如下（来自 README）：

```
├── add
│   ├── CMakeLists.txt      // 编译工程文件
│   ├── add.asc             // Ascend C 样例实现 & 调用样例
│   └── README.md           // 样例说明文档
```

对照 `add_tpipe_tque` 的脚本式结构，多出 `scripts/` 与一个 `data_utils.h`：

```
├── add_tpipe_tque
│   ├── scripts
│   │   ├── gen_data.py        // 输入数据和真值数据生成脚本
│   │   └── verify_result.py   // 验证输出与真值是否一致的脚本
│   ├── CMakeLists.txt
│   ├── data_utils.h           // 数据读入写出函数（ReadFile / WriteFile）
│   ├── add_tpipe_tque.asc
│   └── README.md
```

两份 `CMakeLists.txt` 在结构上几乎一致（下一节细讲），区别只在「数据如何进出」。

#### 4.1.3 源码精读

`add.asc` 一个文件里依次出现了四段代码，正好对应自包含布局的四个角色：

- **Kernel**（Device 侧）：`__vector__ __global__ void add_custom`，实现 \(z_i = x_i + y_i\)，见 [add.asc:27-63](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/examples/01_simd_cpp_api/00_introduction/01_add/add/add.asc#L27-L63)。这段 Kernel 内部用 `GlobalTensor` 描述 GM、用 `LocalMemAllocator` 在 UB 上分配 `LocalTensor`，再 `DataCopy` 搬入、`Add` 计算、`DataCopy` 搬出——这是 u2-l1 里「搬入-计算-搬出」三段式的真实落地。
- **Host 调用**：`kernel_add`，封装了 u2-l2 讲过的 ACL 8 步生命周期，并用 `<<<numBlocks, 0, stream>>>` 启动 Kernel，见 [add.asc:65-106](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/examples/01_simd_cpp_api/00_introduction/01_add/add/add.asc#L65-L106)。
- **数据生成**：`main` 里用 `for` 循环填入 `x[i] = i * 0.1f`、`y[i] = i * 0.2f`，见 [add.asc:132-151](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/examples/01_simd_cpp_api/00_introduction/01_add/add/add.asc#L132-L151)。
- **结果校验**：`VerifyResult` 用 `std::equal` 把输出与真值逐元素比对，打印 `test pass!` 或 `test failed!`，见 [add.asc:108-130](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/examples/01_simd_cpp_api/00_introduction/01_add/add/add.asc#L108-L130)。

注意这里的关键点：**Host 代码与 Device Kernel 写在同一文件、由编译器按函数限定符自动拆分**——这正是 u2-l1 的「单源混合编译」。`main`、`kernel_add`、`VerifyResult` 没有任何 Kernel 限定符，所以它们是普通的 Host C++ 函数；只有 `add_custom` 带 `__global__`，才会被编译成在 AI Core 上运行的 Kernel。

而脚本式的 `add_tpipe_tque` 把数据生成/校验挪到了 Python，Host 侧则用 `data_utils.h` 的 `ReadFile`/`WriteFile` 读写 `.bin` 文件，见 [data_utils.h:25-85](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/examples/01_simd_cpp_api/00_introduction/01_add/add_tpipe_tque/data_utils.h#L25-L85)。

#### 4.1.4 代码实践

1. **实践目标**：建立「一个样例由哪些文件组成、各管什么」的直觉。
2. **操作步骤**：
   - 打开 `examples/01_simd_cpp_api/00_introduction/01_add/add/add.asc`，用注释把文件划成四段（Kernel / Host 调用 / 校验 / main）。
   - 打开同级的 `add_tpipe_tque/` 目录，对比它比 `add/` 多了哪些文件。
3. **需要观察的现象**：`add` 只靠一个 `.asc` 就能独立跑；`add_tpipe_tque` 必须先跑 `gen_data.py` 产出 `.bin`，`demo` 才有输入可读。
4. **预期结果**：你能用一句话说出「自包含样例把数据生成交给了 main 里的循环，脚本式样例把它交给了 Python」。

#### 4.1.5 小练习与答案

**练习 1**：`add.asc` 里 `main`、`kernel_add`、`VerifyResult` 三个函数为什么被编译成 Host 代码而不是 Kernel？

**参考答案**：因为它们都没有任何函数限定符（既无 `__global__`，也无 `__vector__`/`__cube__`）。编译器只把带 `__global__` 的函数当作 Device Kernel，其余一律按普通 Host C++ 处理——这是 u2-l1 讲过的「Host/Device 边界由函数限定符隐式划定」。

**练习 2**：脚本式样例里，Host 侧从哪里读输入、把输出写到哪？

**参考答案**：从 `data_utils.h` 提供的 `ReadFile` 读取 `input/input_x.bin`、`input/input_y.bin`；用 `WriteFile` 把结果写到 `output/output.bin`；再由 `verify_result.py` 拿它和 `output/golden.bin` 比对。

---

### 4.2 编译运行流程

#### 4.2.1 概念说明

Ascend C 样例的编译靠 CMake 驱动，但和普通 C/C++ 工程有两处不同：

1. **启用 ASC 语言**：`project(... LANGUAGES ASC CXX)` 告诉 CMake 除了 C++ 还要识别 `ASC` 这种语言，于是 `.asc` 文件能被当作源文件编译（背后调用昇腾编译器）。
2. **指定芯片架构**：通过 `--npu-arch` 编译选项告诉编译器「为哪款芯片生成指令」。当前样例支持 `dav-2201`（对应 Atlas A2 / Atlas A3 系列）与 `dav-3510`（对应 Ascend 950PR / 950DT）。

此外还有**运行模式**选项 `CMAKE_ASC_RUN_MODE`，三选一：

| 模式 | 值 | 产物运行位置 | 适用场景 |
| --- | --- | --- | --- |
| NPU 运行（默认） | `npu` | 真实昇腾芯片 | 测精度、测性能 |
| CPU 调试 | `cpu` | 主机 CPU | 无 NPU 时的功能调试、断点 |
| NPU 仿真 | `sim` | 主机上的仿真器 | 无 NPU 时近似性能/行为 |

没有 NPU 时，用 `cpu` 或 `sim` 就能跑通本讲的全部流程。

#### 4.2.2 核心流程

`add` 样例的标准编译运行流程（来自 README）如下：

```
1. source ${install_path}/cann/set_env.sh        # 配置 CANN 环境变量
2. cd .../01_add/add                              # 进入样例目录
3. mkdir -p build && cd build                     # 建立并进入 build 目录
4. cmake -DCMAKE_ASC_ARCHITECTURES=dav-2201 ..    # 配置工程（默认 npu 模式）
5. make -j                                        # 编译，生成 ./demo
6. ./demo                                         # 运行，自动打印 test pass!
```

若需切到 CPU 调试或仿真，在第 4 步追加 `-DCMAKE_ASC_RUN_MODE=cpu`（或 `sim`）：

```bash
cmake -DCMAKE_ASC_RUN_MODE=cpu -DCMAKE_ASC_ARCHITECTURES=dav-2201 ..
```

> **注意（来自 README）**：切换编译模式前必须清理 CMake 缓存，否则旧选项会残留：在 `build` 目录下执行 `rm CMakeCache.txt` 后再重新 `cmake`。

脚本式样例多了两步（数据生成与校验），完整顺序为：`cmake/make` → `python3 ../scripts/gen_data.py` → `./demo` → `python3 ../scripts/verify_result.py output/output.bin output/golden.bin`。

#### 4.2.3 源码精读

整份 [add/CMakeLists.txt](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/examples/01_simd_cpp_api/00_introduction/01_add/add/CMakeLists.txt) 只有十几行有效内容，关键四句：

- 第 14 行定义架构缓存变量，默认 `dav-2201`：

  ```cmake
  set(CMAKE_ASC_ARCHITECTURES "dav-2201" CACHE STRING "NPU architecture: dav-2201, dav-3510")
  ```
  见 [CMakeLists.txt:14](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/examples/01_simd_cpp_api/00_introduction/01_add/add/CMakeLists.txt#L14)。它就是命令行 `-DCMAKE_ASC_ARCHITECTURES=...` 的接收方。

- 第 16 行加载 ASC 工具链：

  ```cmake
  find_package(ASC REQUIRED)
  ```
  见 [CMakeLists.txt:16](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/examples/01_simd_cpp_api/00_introduction/01_add/add/CMakeLists.txt#L16)。`find_package(ASC)` 找到的是 `source set_env.sh` 之后 CANN 里安装好的 ASC CMake 包，它带来了 `ASC` 语言定义与昇腾编译器调用规则。

- 第 18 行启用 ASC 语言：

  ```cmake
  project(kernel_samples LANGUAGES ASC CXX)
  ```
  见 [CMakeLists.txt:18](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/examples/01_simd_cpp_api/00_introduction/01_add/add/CMakeLists.txt#L18)。没有 `LANGUAGES ASC`，CMake 不认识 `.asc`。

- 第 20–26 行声明可执行文件并把 `--npu-arch` 透传给 ASC 编译器：

  ```cmake
  add_executable(demo
      add.asc
  )

  target_compile_options(demo PRIVATE
      $<$<COMPILE_LANGUAGE:ASC>:--npu-arch=${CMAKE_ASC_ARCHITECTURES}>
  )
  ```
  见 [CMakeLists.txt:20-26](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/examples/01_simd_cpp_api/00_introduction/01_add/add/CMakeLists.txt#L20-L26)。其中的生成器表达式 `$<$<COMPILE_LANGUAGE:ASC>:...>` 表示「这个选项只给 ASC 语言用，不给 C++ 用」，避免把 `--npu-arch` 错传给主机 C++ 编译器。

整个编译链路可以画成：

```
add.asc ──(ASC 语言 / bisheng-compiler，带 --npu-arch)──> Kernel 二进制
        ──(CXX / 主机编译器)──────────────────────────> Host 代码  ─┐
                                                                    ├──> demo
CANN 里的 ASC CMake 包 (find_package(ASC)) ─────────────────────────┘
```

#### 4.2.4 代码实践

1. **实践目标**：亲手把样例配置出来（即使不编译，也走一遍配置流程）。
2. **操作步骤**：
   - 复制 `add/CMakeLists.txt`，把第 14 行默认值改成 `dav-3510`，观察 README 中两种架构各对应哪款产品。
   - 思考：如果不小心在 `target_compile_options` 里漏掉了生成器表达式，直接写 `--npu-arch=dav-2201`，会发生什么？
3. **需要观察的现象**：架构名是一个可被命令行覆盖的 CMake 缓存变量；`--npu-arch` 是昇腾编译器选项而非 C++ 选项。
4. **预期结果**：理解「架构既能在 CMakeLists 里设默认值，也能在命令行用 `-D` 临时覆盖」。
5. **待本地验证**：如果你本地有 CANN 环境，执行 `cmake -DCMAKE_ASC_ARCHITECTURES=dav-2201 ..` 后查看生成的 `CMakeCache.txt` 中 `CMAKE_ASC_ARCHITECTURES` 的实际取值。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `--npu-arch` 要用 `$<$<COMPILE_LANGUAGE:ASC>:...>` 包起来，而不是直接作为 `target_compile_options` 的参数？

**参考答案**：因为 `demo` 这个目标同时包含 ASC 源（`add.asc`）和 C++ 源（编译器为 Host 代码生成的 `.cpp`）。生成器表达式确保 `--npu-arch` 只传给昇腾编译器（ASC 语言），不会误传给主机 C++ 编译器导致报错。

**练习 2**：把同一份样例从 NPU 模式切到 CPU 调试模式，最少要敲哪些命令？

**参考答案**：在 `build` 目录下先 `rm CMakeCache.txt` 清缓存，再 `cmake -DCMAKE_ASC_RUN_MODE=cpu -DCMAKE_ASC_ARCHITECTURES=dav-2201 ..`，最后 `make -j`。

---

### 4.3 数据生成与结果校验

#### 4.3.1 概念说明

算子写得对不对，必须靠「**输出 vs 真值（golden）**」来证明。这里有两套等价做法：

- **`add` 样例（自包含）**：输入由 `main` 里的循环生成，真值也由 `main` 用同一个公式 `x[i] + y[i]` 算出，校验用 C++ `std::equal` 做**精确相等**比较。
- **`add_tpipe_tque` 样例（脚本式）**：输入与真值由 `gen_data.py` 用 NumPy 生成并落盘成 `.bin`，校验由 `verify_result.py` 用 `np.isclose` 做**带容差**比较。

一个关键细节：为什么 `add` 敢用精确相等？因为它的输入是确定性的（`x[i]=i*0.1f`、`y[i]=i*0.2f`），Host 算真值与 Device 算 Kernel 执行的是**同一个 IEEE-754 加法**，对相同输入必然得到相同位级结果。一旦换成 `half` 等低精度类型，或换成非线性运算，就应当改用带容差的脚本式校验。

#### 4.3.2 核心流程

**自包含（`add`）数据流**——全在一个进程里：

```
main(): for 循环生成 x, y ──> kernel_add(): aclrtMemcpy(H2D) ──> add_custom Kernel
                                                                   │
true: golden[i] = x[i]+y[i] <── VerifyResult(std::equal) <── aclrtMemcpy(D2H) <──┘
```

**脚本式（`add_tpipe_tque`）数据流**——跨进程、跨语言，靠 `.bin` 文件衔接：

```
gen_data.py: NumPy 随机 x, y ──> input/input_x.bin, input/input_y.bin
                             └──> output/golden.bin
                                          │
                  demo: ReadFile(.bin) ──> Kernel ──> WriteFile ──> output/output.bin
                                                                          │
verify_result.py: np.isclose(output.bin, golden.bin) <───────────────────┘
```

#### 4.3.3 源码精读

先看自包含 `add` 的数据生成（[add.asc:132-151](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/examples/01_simd_cpp_api/00_introduction/01_add/add/add.asc#L132-L151)）：

```cpp
constexpr uint32_t totalLength = 8 * 2048;      // 与 numBlocks(8) × blockLength(2048) 严格相等
std::vector<float> x(totalLength);
std::vector<float> y(totalLength);
for (uint32_t i = 0; i < totalLength; ++i) {
    x[i] = i * 0.1f;
    y[i] = i * 0.2f;
}
std::vector<float> output = kernel_add(x, y);   // 跑算子
std::vector<float> golden(totalLength);
for (uint32_t i = 0; i < totalLength; ++i) {
    golden[i] = x[i] + y[i];                    // Host 算真值
}
return VerifyResult(output, golden);
```

注意第 134 行的约束：`totalLength = 8 * 2048` 必须等于 `kernel_add` 内部的 `numBlocks × blockLength`（见 [add.asc:67-68](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/examples/01_simd_cpp_api/00_introduction/01_add/add/add.asc#L67-L68)）。这正是 u2-l2 讲过的启动约束 `numBlocks × blockLength ≥ totalLength`，这里取了「恰好相等」。

再看校验（[add.asc:108-130](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/examples/01_simd_cpp_api/00_introduction/01_add/add/add.asc#L108-L130)），核心一行：

```cpp
if (std::equal(golden.begin(), golden.end(), output.begin())) {
    std::cout << "test pass!" << std::endl;     // 精确相等即通过
```

Host 与 Device 之间的搬运由 `kernel_add` 完成（承接 u2-l2 的 8 步生命周期）：H2D 拷贝见 [add.asc:87-88](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/examples/01_simd_cpp_api/00_introduction/01_add/add/add.asc#L87-L88)，启动后同步见 [add.asc:90-91](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/examples/01_simd_cpp_api/00_introduction/01_add/add/add.asc#L90-L91)，D2H 取回见 [add.asc:93-94](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/examples/01_simd_cpp_api/00_introduction/01_add/add/add.asc#L93-L94)。

然后看脚本式的对照。`gen_data.py` 用 NumPy 造随机输入并算真值（[gen_data.py:19-27](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/examples/01_simd_cpp_api/00_introduction/01_add/add_tpipe_tque/scripts/gen_data.py#L19-L27)）：

```python
input_x = np.random.uniform(1, 10, [8, 2048]).astype(np.float32)
input_y = np.random.uniform(1, 10, [8, 2048]).astype(np.float32)
golden = (input_x + input_y).astype(np.float32)
...
input_x.tofile("./input/input_x.bin")
input_y.tofile("./input/input_y.bin")
golden.tofile("./output/golden.bin")
```

`verify_result.py` 用容差比对（[verify_result.py:24-48](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/examples/01_simd_cpp_api/00_introduction/01_add/add_tpipe_tque/scripts/verify_result.py#L24-L48)），容差在第 19–21 行定义（`RELATIVE_TOL=1e-4`、`ABSOLUTE_TOL=1e-5`、`ERROR_TOL=1e-4`），核心判定为「错误元素占比不超过 `ERROR_TOL` 即通过」：

```python
different_element_results = np.isclose(output, golden, rtol=RELATIVE_TOL, atol=ABSOLUTE_TOL, equal_nan=True)
...
error_ratio = float(different_element_indexes.size) / golden.size
return error_ratio <= ERROR_TOL
```

两套做法的对照：

| 维度 | `add`（自包含） | `add_tpipe_tque`（脚本式） |
| --- | --- | --- |
| 数据生成位置 | `main` 里的 C++ 循环 | `gen_data.py`（NumPy） |
| 数据载体 | 内存中的 `std::vector` | `input/*.bin`、`output/*.bin` |
| 真值算法 | `golden[i] = x[i] + y[i]` | `golden = input_x + input_y` |
| 比较方式 | `std::equal`（精确） | `np.isclose`（带容差） |
| 运行步骤 | `./demo`（一条命令自检） | `gen_data.py` → `./demo` → `verify_result.py` |

#### 4.3.4 代码实践

1. **实践目标**：理解「确定性输入可精确校验，随机/低精度输入需带容差校验」。
2. **操作步骤**：
   - 在 `add.asc` 的 `main` 里，把 `y[i] = i * 0.2f` 改成 `y[i] = x[i]`（即让两个输入相同），重新推算 `golden[i]` 应该是多少。
   - 阅读脚本式的 `verify_result.py`，找出控制「允许错误比例」的是哪个常量。
3. **需要观察的现象**：自包含样例的输入改了，真值公式必须同步改，否则 `std::equal` 必然失败。
4. **预期结果**：当 `y[i] = x[i]` 时，`golden[i] = 2 * x[i]`，校验仍应输出 `test pass!`（待本地验证）。
5. **待本地验证**：上述改动需要在本地的样例副本上编译运行后确认终端打印。

#### 4.3.5 小练习与答案

**练习 1**：如果把 `add` 样例的数据类型整体从 `float` 换成 `half`，直接用现有的 `std::equal` 校验，可能出什么问题？

**参考答案**：`half` 精度低于 `float`，Device 加法结果与 Host 真值可能出现最后几位差异，导致 `std::equal`（要求逐位相等）误报失败。正确做法是改用脚本式的 `verify_result.py`（带 `rtol`/`atol` 容差）来校验。

**练习 2**：脚本式样例里，`gen_data.py` 为什么要把 `golden` 也写盘？

**参考答案**：因为 `demo`（算子进程）和 `verify_result.py`（校验进程）是两个独立进程，且校验脚本用 Python、算子用 C++，二者只能通过文件交换数据。把 `golden` 写成 `output/golden.bin`，校验脚本才能把它和算子写出的 `output/output.bin` 放在一起比对。

## 5. 综合实践

本任务把三个模块串起来：**按 README 步骤编译运行 `add` 样例，修改 `totalLength` 与数据类型后重新生成输入、运行并通过校验，记录终端输出。**

> 说明：本任务在你的**本地样例副本**上操作，不要改动仓库原始源码。下列命令与结果标注「待本地验证」的，表示需要你在具备 CANN 环境（或 CPU 调试模式）的机器上确认。

**第一步：原样跑通**

```bash
source ${install_path}/cann/set_env.sh                 # 1. 配置环境
cd examples/01_simd_cpp_api/00_introduction/01_add/add  # 2. 进样例目录
cp -r add add_mywork && cd add_mywork                   # 3. 复制一份再改（不动原始源码）
mkdir -p build && cd build
cmake -DCMAKE_ASC_ARCHITECTURES=dav-2201 ..             # 4. 无 NPU 可加 -DCMAKE_ASC_RUN_MODE=cpu
make -j                                                 # 5. 编译
./demo                                                  # 6. 运行
```

预期看到（待本地验证）：

```
Output: 0 0 0.3 0.6 0.9 1.2 ...
Golden: 0 0 0.3 0.6 0.9 1.2 ...
test pass!
```

**第二步：修改 `totalLength`**

在副本的 `add.asc` 中：

- 把 [add.asc:134](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/examples/01_simd_cpp_api/00_introduction/01_add/add/add.asc#L134) 的 `constexpr uint32_t totalLength = 8 * 2048;` 改为 `4 * 2048`；
- 同步把 [add.asc:67](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/examples/01_simd_cpp_api/00_introduction/01_add/add/add.asc#L67) 的 `numBlocks` 从 `8` 改为 `4`（保持 `numBlocks × blockLength == totalLength`）。

重新 `make -j && ./demo`，应仍输出 `test pass!`（待本地验证）。思考：为什么只改 `totalLength` 不改 `numBlocks` 会导致结果错误或越界？——因为 Kernel 按 `block_idx * blockLength` 切分 GM，启动核数与总长不一致时，要么有数据没人算，要么越界写到未分配的 GM。

**第三步（进阶，选做）：修改数据类型**

把整条链路从 `float` 换成 `half`。需要同步改动：Kernel 模板参数与 `__gm__` 指针类型、`GlobalTensor<half>`/`LocalTensor<half>`、Host 侧 `std::vector<half>` 与 `aclrtMalloc` 的字节数（`half` 占 2 字节）。由于 `half` 精度有限，`std::equal` 精确校验可能失败，建议改用脚本式 `verify_result.py` 的容差比对来判定。此项结果**待本地验证**，不要假装已经通过。

**记录**：把每一步的终端输出（尤其是 `test pass!` / `test failed!`）截图或抄写下来，作为你跑通第一个算子的证据。

## 6. 本讲小结

- Ascend C 样例有**自包含**（`add`：一个 `.asc` 包含 Kernel+Host+数据+校验）与**脚本式**（`add_tpipe_tque`：数据/校验交给 Python，靠 `.bin` 衔接）两种布局。
- 编译靠 CMake：`find_package(ASC)` 加载工具链，`project(... LANGUAGES ASC CXX)` 启用 ASC 语言，`--npu-arch` 由生成器表达式只透传给昇腾编译器。
- 三种运行模式 `npu`/`cpu`/`sim` 用 `CMAKE_ASC_RUN_MODE` 切换，切换前必须 `rm CMakeCache.txt`。
- 端到端流程是 `source set_env.sh` → `cmake` → `make` → `./demo`（脚本式多出 `gen_data.py` 与 `verify_result.py` 两步）。
- 数据生成与校验有一致性要求：`totalLength` 必须等于 `numBlocks × blockLength`；确定性输入可用 `std::equal` 精确校验，低精度/随机输入应改用带容差的脚本校验。
- 至此，你已经把 u2-l1 的单源模型和 u2-l2 的 ACL 运行时真正跑成了一个可执行程序。

## 7. 下一步学习建议

本讲的 Kernel 内部把 `GlobalTensor`、`LocalTensor`、`DataCopy`、`LocalMemAllocator` 当作黑盒用了。接下来建议：

- **U3（内存层级与数据搬运）**：拆开 `__gm__`/`__ubuf__` 背后的 GM/UB 多级存储，理解 `GlobalTensor`/`LocalTensor` 与 `DataCopy` 的细节。
- **U4（基础 API 矢量计算）**：把 `Add` 换成 `Exp`/`Sqrt` 等其他矢量接口，理解 `count` 参数与对齐约束。
- **U5（TPipe/TQue 框架）**：用框架式内存管理重写本讲的 `add`，对比 `LocalMemAllocator` 自主管理方式。

如果想立刻看到更多端到端样例，可以浏览 `examples/01_simd_cpp_api/00_introduction/02_matrix/` 下的矩阵乘样例，它的工程结构与本讲完全一致，只是 Kernel 换成了 Cube 计算。

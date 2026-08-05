# 首次运行：用 blackbox.sh 跑通 demo

## 1. 本讲目标

学完本讲后，你应当能够：

1. 用 `ci/blackbox.sh` 这个统一启动器，在 SimX 后端上跑通一个完整的程序（从编译内核到拿到结果）。
2. 理解 `--driver` 与 `--cores/--warps/--threads/--clusters/--l2cache` 等架构覆盖参数的含义，以及它们如何被翻译成底层开关。
3. 认识 `CONFIGS=-DVX_CFG_*` 与架构宏的关系，明白「配置必须在两侧都生效」这条关键纪律。

本讲是整个手册里第一次真正「按下回车跑出结果」的讲义。前置讲义（u1-l3）已经讲清了 `configure` 与构建树的关系，本讲在此基础上完成「构建 → 运行 → 验证」的闭环。

---

## 2. 前置知识

在动手之前，请确认你已经具备下面这些认知（它们来自 u1-l1 到 u1-l3）：

- **全栈与多后端**：Vortex 是全栈开源 RISC-V GPGPU，主机程序通过 `libvortex.so` 这一「stub 分发器」在 `simx`（C++ 仿真）、`rtlsim`、`opae`（Intel FPGA）、`xrt`（Xilinx FPGA）等后端之间切换。本讲只用最轻量的 `simx`。
- **SIMT 执行模型**：硬件按 `cluster → core → warp → thread` 分层；一个 warp 内的多个线程共享 PC、靠 thread mask 控制写回。`NUM_CORES / NUM_WARPS / NUM_THREADS` 就是描述这套规模的三个关键参数。
- **构建树与 configure**：源外构建（out-of-tree），在 `build/` 目录里运行 `../configure`；改了 `VX_config.toml` 或 Makefile 后必须重新 `configure`。所有测试命令都从 `build/` 目录执行。

此外，需要你大致了解一次 GPU 程序的运行步骤（和 CUDA/OpenCL 类似）：打开设备 → 分配显存 → 把数据拷到设备 → 上传内核 → 启动（launch）→ 等待完成 → 把结果拷回 → 校验。本讲的 `demo` 程序就是按这个套路写的向量加法。

如果你还没在 `build/` 目录里成功执行过 `../configure` 与 `./ci/toolchain_install.sh`，请先回到 u1-l3 完成工具链安装，否则本讲的命令会因找不到编译器而失败。

---

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
| --- | --- |
| `ci/blackbox.sh` | 统一启动器。解析命令行旋钮、定位驱动后端与 app、按需重建驱动、调用 `make run-<driver>` 跑程序。 |
| `tests/regression/demo/main.cpp` | 主机侧测试程序：完整的「打开设备→分配→拷入→启动→拷回→校验」流程。 |
| `tests/regression/demo/kernel.cpp` | 设备侧内核：向量加法 `kernel_main`。 |
| `tests/regression/demo/common.h` | 主机与内核共享的参数结构 `kernel_arg_t` 与数据类型宏 `TYPE`。 |
| `tests/regression/demo/Makefile` | demo 的构建配方，include 公共的 `common.mk`。 |
| `tests/regression/common.mk` | 所有 regression 测试共享的构建规则，定义 `run-simx` 等目标。 |
| `VX_config.toml` | 硬件配置的唯一真相来源，给出 `NUM_CORES` 等基线值。 |
| `AGENTS.md` | 工程纪律文档，其中 §4 规定了测试运行规则。 |
| `README.md` | 项目说明，给出 Quick Start 命令。 |

记住一条贯穿全讲的线索：**`blackbox.sh` 只是「方向盘」，真正干活的是 app 自己的 `Makefile`（经由 `common.mk`）和驱动后端的 `Makefile`。**

---

## 4. 核心概念与源码讲解

### 4.1 blackbox.sh：统一启动器

#### 4.1.1 概念说明

`blackbox.sh` 是 Vortex 提供的「一键跑通」脚本。它的价值在于：把「选后端 + 选程序 + 改架构规模 + 重建驱动 + 运行」这一串本来要手敲很多遍 `make` 的步骤，浓缩成一条命令。

你可以把它理解成一个**参数翻译器 + 流程编排器**：

- 你用人类友好的旋钮（`--cores=2`）告诉它想要的硬件规模；
- 它把这些旋钮翻译成编译器能懂的宏（`-DVX_CFG_NUM_CORES=2`），塞进一个叫 `CONFIGS` 的变量；
- 然后它按固定顺序：定位后端 → 定位 app → 重建驱动 → 调用 `make run-<driver>`。

它**不重新发明构建系统**，而是把工作委派给 app 目录里既有的 `Makefile`，所以它和 u1-l3 讲的 `configure` 体系是兼容的。

#### 4.1.2 核心流程

`blackbox.sh` 的主流程可以概括为下面五步（伪代码）：

```
1. parse_args        解析 --driver/--app/--cores/--warps/--threads/...
                     把架构旋钮翻译成 CONFIGS="-DVX_CFG_NUM_CORES=2 ..."
2. set_driver_path   根据 --driver 找到 sw/runtime/<driver> 目录
3. set_app_path      根据 --app 在 tests/{regression,graphics,...}/<app> 中查找
4. build_driver      make -C sw/runtime/<driver>  （用 CONFIGS 重建驱动/模型）
5. run_app           make -C <app_path> run-<driver>  （用 CONFIGS 重建并运行 app）
```

关键点：第 4、5 步都会把 `CONFIGS` 原样传下去，所以「架构覆盖」会同时作用于**驱动后端**（例如 SimX 的 core 模型）和**应用程序**（主机二进制与设备内核）。这一点非常重要，后面 4.3 会展开。

旋钮到宏的翻译规则如下表：

| 命令行旋钮 | 翻译成的 `CONFIGS` 内容 |
| --- | --- |
| `--cores=2` | `-DVX_CFG_NUM_CORES=2` |
| `--warps=4` | `-DVX_CFG_NUM_WARPS=4` |
| `--threads=4` | `-DVX_CFG_NUM_THREADS=4` |
| `--clusters=1` | `-DVX_CFG_NUM_CLUSTERS=1` |
| `--l2cache` | `-DVX_CFG_L2_ENABLE` |
| `--l3cache` | `-DVX_CFG_L3_ENABLE` |
| `--perf=1` | `-DPERF_ENABLE`（并把类别号放进 `VORTEX_PROFILING`） |
| `--debug=3` | 设置 `DEBUG=1 DEBUG_LEVEL=3` |

#### 4.1.3 源码精读

**(1) 默认值：不传任何参数时跑什么**

[ci/blackbox.sh:44-59](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/blackbox.sh#L44-L59) 是 `DEFAULTS()` 函数，规定了「什么都不传」时的默认行为：默认驱动是 `simx`，默认程序是 `sgemm`，默认不开启 debug/perf。

```sh
DRIVER=simx      # 默认后端：C++ 仿真器，最轻量、最快
APP=sgemm        # 默认程序：矩阵乘法
```

所以哪怕你只敲 `./ci/blackbox.sh`，它也会去跑 `sgemm` on `simx`。

**(2) 旋钮解析：把 `--cores=2` 变成宏**

[ci/blackbox.sh:61-88](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/blackbox.sh#L61-L88) 是 `parse_args()`。注意架构类旋钮是如何通过 `add_option` 累加进 `CONFIGS` 的：

```sh
--cores=*)  CONFIGS=$(add_option "$CONFIGS" "-DVX_CFG_NUM_CORES=${i#*=}") ;;
--warps=*)  CONFIGS=$(add_option "$CONFIGS" "-DVX_CFG_NUM_WARPS=${i#*=}") ;;
--threads=*) CONFIGS=$(add_option "$CONFIGS" "-DVX_CFG_NUM_THREADS=${i#*=}") ;;
```

`add_option`（[ci/blackbox.sh:36-42](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/blackbox.sh#L36-L42)）的作用是：如果 `CONFIGS` 已有内容就追加一个空格再拼上新宏，否则直接用新宏。这样多个旋钮可以叠加成一条 `-D... -D... -D...` 串。

> 小提示：`--help` 会打印支持的驱动列表 [ci/blackbox.sh:25-34](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/blackbox.sh#L25-L34)，其中 `--driver` 可选 `gpu, simx, rtlsim, opae, xrt`。

**(3) 定位后端与 app**

[ci/blackbox.sh:90-96](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/blackbox.sh#L90-L96) 把 `--driver` 映射到 `sw/runtime/<driver>` 目录（`simx` 对应 `sw/runtime/simx`）。

[ci/blackbox.sh:98-119](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/blackbox.sh#L98-L119) 则按优先级在多个 tests 子目录里查找 app：先看是不是一个本地路径，再依次找 `tests/<app>`、`tests/regression/<app>`、`tests/raytracing/<app>`、`tests/graphics/<app>`、`tests/mpi/<app>`、`tests/opencl/<app>`、`tests/hip/<app>`。所以 `--app=demo` 会被解析成 `tests/regression/demo`。

**(4) 重建驱动 + 运行 app**

[ci/blackbox.sh:121-136](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/blackbox.sh#L121-L136) 的 `build_driver()` 用 `make -C $DRIVER_PATH` 重建后端库，并把 `CONFIGS` 作为变量传进去。

[ci/blackbox.sh:138-158](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/blackbox.sh#L138-L158) 的 `run_app()` 是真正运行 app 的地方，关键一行是：

```sh
cmd_opts=$(add_option "$cmd_opts" "make -C \"$APP_PATH\" run-$DRIVER")
```

也就是说，对 `simx` 后端，最终执行的是 `make -C tests/regression/demo run-simx`。

**(5) 主流程与 stub 构建**

[ci/blackbox.sh:160-209](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/blackbox.sh#L160-L209) 是 `main()`。它先 `parse_args → set_driver_path → set_app_path`，然后做一件容易被忽略的事：

```sh
make -C "$ROOT_DIR/sw/runtime/stub" > /dev/null
```

这一步（[ci/blackbox.sh:184](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/blackbox.sh#L184)）编译了 `libvortex.so` 这个 stub 分发器——也就是 u1-l1 提到的、运行时按 `$VORTEX_DRIVER` 环境变量 `dlopen` 后端库的入口。接着才调用 `build_driver` 与 `run_app`（[ci/blackbox.sh:204-205](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/blackbox.sh#L204-L205)），最终把 app 的退出码原样返回（[ci/blackbox.sh:208](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/blackbox.sh#L208)）。

> 注意：`blackbox.sh` 实际解析的旋钮以 [ci/blackbox.sh:61-88](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/blackbox.sh#L61-L88) 为准。遇到脚本不认识的 `--xxx`，它会在 `--*)` 分支报 `Invalid argument` 并退出，所以不要假设某个旋钮存在——以源码为准。

#### 4.1.4 代码实践

**实践目标**：在不实际编译的情况下，手动「模拟」一遍 `blackbox.sh` 的参数翻译，确认你理解了旋钮→宏的映射。

**操作步骤**：

1. 在 `build/` 目录执行（只看帮助，不运行）：
   ```sh
   ./ci/blackbox.sh --help
   ```
2. 对照 [ci/blackbox.sh:61-88](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/blackbox.sh#L61-L88)，手写出下面这条命令最终会生成的 `CONFIGS` 字符串：
   ```sh
   ./ci/blackbox.sh --driver=simx --app=demo --cores=2 --warps=4 --threads=4 --l2cache
   ```
3. 再对照 [ci/blackbox.sh:138-158](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/blackbox.sh#L138-L158)，写出它最终会执行哪条 `make` 命令。

**需要观察的现象**：`--help` 输出的驱动列表与 perf 类别编号；以及你自己推算出的 `CONFIGS` 串里宏的顺序与拼写。

**预期结果**：

- 第 2 步推算的 `CONFIGS` 应当类似：
  ```
  -DVX_CFG_NUM_CORES=2 -DVX_CFG_NUM_WARPS=4 -DVX_CFG_NUM_THREADS=4 -DVX_CFG_L2_ENABLE
  ```
- 第 3 步推算的最终命令应当是：
  ```
  make -C tests/regression/demo run-simx
  ```
  （并带上 `CONFIGS="..."`、`DEBUG` 等变量。）

> 实际运行 `--help` 的具体输出格式以你本地环境为准（待本地验证）。

#### 4.1.5 小练习与答案

**练习 1**：如果不传 `--driver`，默认后端是什么？如果不传 `--app`，默认程序又是什么？

**参考答案**：默认后端是 `simx`，默认程序是 `sgemm`。见 [ci/blackbox.sh:45-46](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/blackbox.sh#L45-L46)。

**练习 2**：`--cores=2` 这个旋钮在脚本里被翻译成了什么？它会被传给哪两个 `make` 调用？

**参考答案**：翻译成 `-DVX_CFG_NUM_CORES=2`，累加进 `CONFIGS`。它会被同时传给 `build_driver()` 里的 `make -C sw/runtime/simx` 和 `run_app()` 里的 `make -C tests/regression/demo run-simx`（见 [ci/blackbox.sh:127](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/blackbox.sh#L127) 与 [ci/blackbox.sh:151](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/blackbox.sh#L151)）。

---

### 4.2 tests/regression/demo：第一个测试程序

#### 4.2.1 概念说明

`demo` 是一个**向量加法**（vector addition）测试：在设备上计算 `dst = src0 + src1`，然后把结果拷回主机，与 CPU 上的参考结果逐元素比对。它是 Vortex 里结构最简单、又完整覆盖「主机↔设备」往返的样例，所以被当作冒烟测试（smoke test）。

它由四部分组成：

- `main.cpp`：主机程序，用运行时 API（`vortex2.h`）编排整个流程。
- `kernel.cpp`：设备内核，每个线程处理一段数据。
- `common.h`：主机与内核共享的 `kernel_arg_t` 参数结构。
- `Makefile`：构建配方，include 公共 `common.mk`。

「主机程序」和「设备内核」是两套会被分别编译的代码：主机程序用宿主机的 `g++` 编译成 x86 二进制（通过 stub 链接 `libvortex.so`）；设备内核用 RISC-V 的 `clang++`（VOLT 工具链）编译成 `.vxbin`。这一点在 `common.mk` 里体现为两套不同的 `VX_CFLAGS` 与 `CXXFLAGS`。

#### 4.2.2 核心流程

`main.cpp` 的运行流程（与 CUDA/OpenCL 高度相似）：

```
parse_args           解析 -n(数据量) -x/-y(block 维度) -k(内核文件)
vx_device_open       打开设备连接
vx_device_query      查询 NUM_CORES / NUM_WARPS / NUM_THREADS
                     计算 total_threads = cores*warps*threads
                     计算 grid/block 维度（保证覆盖所有点）
vx_buffer_create×3   分配 src0/src1/dst 三块设备显存
vx_enqueue_write×2   把主机数据拷到 src0/src1
vx_module_load_file  加载 kernel.vxbin
vx_module_get_kernel 按名字 "main" 取出内核句柄
vx_enqueue_launch    启动内核（grid/block/参数）
vx_enqueue_read      把 dst 拷回主机（链接在 launch 事件之后）
vx_event_wait_value  等待完成
逐元素比对           与 CPU 参考结果比较
打印 PASSED/FAILED   返回退出码
```

设备内核 `kernel_main` 的逻辑很简单：每个线程根据自己的全局坐标 `(gx, gy)` 算出一个全局 id，再对 `count` 个元素做 `dst[i] = src0[i] + src1[i]`。

#### 4.2.3 源码精读

**(1) 主机程序的全局默认值**

[tests/regression/demo/main.cpp:72-73](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/tests/regression/demo/main.cpp#L72-L73) 给出两个关键默认值：内核文件名 `kernel.vxbin`，数据量 `count = 16`。注意 `count` 可以被命令行 `-n` 覆盖，而 demo 的 `Makefile` 默认会传 `-n64`（见后文 Makefile 部分）。

**(2) 命令行参数解析**

[tests/regression/demo/main.cpp:91-116](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/tests/regression/demo/main.cpp#L91-L116) 用 `getopt` 解析 `-n/-k/-x/-y/-h`。其中 `-x/-y` 让你能手动指定 block 的二维维度；不指定时由程序按设备能力自动决定。

**(3) 打开设备 + 查询架构规模 + 计算启动维度**

这是本讲最值得精读的一段。[tests/regression/demo/main.cpp:138-158](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/tests/regression/demo/main.cpp#L138-L158)：

```cpp
RT_CHECK(vx_device_open(0, &device));                       // 打开 0 号设备
...
RT_CHECK(vx_device_query(device, VX_CAPS_NUM_CORES,   &num_cores));
RT_CHECK(vx_device_query(device, VX_CAPS_NUM_WARPS,   &num_warps));
RT_CHECK(vx_device_query(device, VX_CAPS_NUM_THREADS, &num_threads));

uint32_t total_threads = num_cores * num_warps * num_threads;   // 设备总并行度
...
uint32_t block_dim_x = (usr_block_x != 0) ? usr_block_x : num_threads;
uint32_t threads_per_block = block_dim_x * block_dim_y;
uint32_t num_blocks = (total_threads + threads_per_block - 1) / threads_per_block;
```

这段代码揭示了一个重要事实：**主机程序是「运行时」才知道硬件规模的**。`vx_device_query` 向后端查询 `NUM_CORES/NUM_WARPS/NUM_THREADS`，然后据此计算要启动多少个 block、一共覆盖多少个点。这正是为什么你在 `blackbox.sh` 里改 `--cores` 会让 demo 处理的数据点数发生变化——后面实践会验证这一点。

> `RT_CHECK` 宏（[tests/regression/demo/main.cpp:10-18](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/tests/regression/demo/main.cpp#L10-L18)）会在运行时 API 返回非 0 时打印错误并 `exit(-1)`，所以任何一步失败你都能立刻看到。

**(4) 启动内核**

[tests/regression/demo/main.cpp:204-227](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/tests/regression/demo/main.cpp#L204-L227) 先加载 `.vxbin` 并按名字取出内核，再填好 `vx_launch_info_t` 启动：

```cpp
RT_CHECK(vx_module_load_file(device, kernel_file, &module_));
RT_CHECK(vx_module_get_kernel(module_, "main", &kernel));
...
uint32_t grid[2]  = {num_blocks, 1};
uint32_t block[2] = {block_dim_x, block_dim_y};
vx_launch_info_t li = {};
li.kernel = kernel;
li.args_host = &kernel_arg;          // 参数以主机 blob(UVA) 形式传入
li.ndim = 2;
li.grid_dim[0]  = grid[0];  li.grid_dim[1]  = grid[1];
li.block_dim[0] = block[0]; li.block_dim[1] = block[1];
RT_CHECK(vx_enqueue_launch(queue, &li, 0, nullptr, &launch_ev));
```

注意 `li.args_host = &kernel_arg`：整个参数结构体是作为主机端内存直接传给驱动的（UVA 风格），不需要单独分配一块「设备参数 buffer」。

**(5) 拷回 + 等待 + 校验**

[tests/regression/demo/main.cpp:229-237](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/tests/regression/demo/main.cpp#L229-L237) 把 `dst` 的读取**链接在 launch 事件之后**（`num_events=1, &launch_ev`），再 `vx_event_wait_value` 阻塞等待，保证读到的是内核写完后的结果。

[tests/regression/demo/main.cpp:240-262](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/tests/regression/demo/main.cpp#L240-L262) 逐元素与 CPU 参考值 `h_src0[i] + h_src1[i]` 比对。全部相等时打印 `PASSED!` 并 `return 0`；有错则打印错误数与 `FAILED!`。**程序退出码就是 blackbox.sh 最终返回的退出码**——这就是判断「跑通」的依据。

**(6) 设备内核**

[tests/regression/demo/kernel.cpp:4-21](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/tests/regression/demo/kernel.cpp#L4-L21) 是真正的计算核心：

```cpp
__kernel void kernel_main(kernel_arg_t* __UNIFORM__ arg) {
    ...
    uint32_t gx = blockIdx.x * blockDim.x + threadIdx.x;
    uint32_t gy = blockIdx.y * blockDim.y + threadIdx.y;
    uint32_t gid = gy * arg->dim_x + gx;
    uint32_t offset = gid * count;
    for (uint32_t i = 0; i < count; ++i) {
        dst_ptr[offset+i] = src0_ptr[offset+i] + src1_ptr[offset+i];
    }
}
```

`__UNIFORM__` 表示这个指针在 warp 内所有线程都相同（统一值）；每个线程靠 `(threadIdx.x, threadIdx.y)` 拿到自己负责的数据段，做加法写回。这里出现的 `blockIdx/threadIdx/blockDim` 是 Vortex 内核 API 提供的 CUDA 风格内建变量（后续 u4 会专门讲）。

**(7) demo 的 Makefile**

[tests/regression/demo/Makefile:1-16](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/tests/regression/demo/Makefile#L1-L16) 极短，因为它把所有重活都交给了公共 `common.mk`：

```makefile
SRCS := $(SRC_DIR)/main.cpp          # 主机源码
VX_SRCS := $(SRC_DIR)/kernel.cpp     # 设备内核源码
OPTS ?= -n64                          # 默认传给程序的命令行参数
KERNEL_LIB := vortex2                 # 使用 KMU 版本的内核库
include ../common.mk
```

注意两点：`OPTS ?= -n64` 意味着默认数据量 `count=64`（覆盖了 `main.cpp` 里的 `count=16`）；`KERNEL_LIB := vortex2` 表示链接带 KMU（Kernel Management Unit）的内核库——这和命令处理器讲义（u11-l3）相关，现在只需知道它启用了更新的内核启动路径。

#### 4.2.4 代码实践

**实践目标**：在 SimX 上跑通 demo，并通过改架构规模观察程序输出（数据点数、buffer 大小）的变化。

**操作步骤**：

1. 确保你在 `build/` 目录，且已执行过 `../configure`（u1-l3）。先跑一次默认规模：
   ```sh
   ./ci/blackbox.sh --driver=simx --app=demo
   ```
2. 再用不同的架构规模重跑：
   ```sh
   ./ci/blackbox.sh --driver=simx --app=demo --cores=2 --warps=4 --threads=4
   ```
3. 两次运行后，分别记录程序打印的 `number of points:`、`buffer size:`，以及最后的 `PASSED!` / `FAILED!` 和 shell 退出码（用 `echo $?` 立即查看）。

**需要观察的现象**：

- 程序应依次打印 `open device connection`、`data type: integer`、`number of points: <N>`、`buffer size: <B> bytes`、`grid_dim=... block_dim=...`，最后是 `PASSED!`。
- 第二次（`--cores=2`）的 `number of points` 与 `buffer size` 应当比第一次（默认 `--cores=1`）**更大**，因为 `total_threads` 翻倍，程序会覆盖更多数据点。

**预期结果**（基于源码推算，具体数值待本地验证）：

- 默认配置下（`NUM_CORES=1, NUM_WARPS=4, NUM_THREADS=4`，来自 [VX_config.toml:4-5](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/VX_config.toml#L4-L5) 与 [VX_config.toml:43-44](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/VX_config.toml#L43-L44)）：
  - `total_threads = 1×4×4 = 16`，`block_dim_x = 4`，`num_blocks = 4`，`num_tasks = 16`。
  - `count = 64`（由 `-n64`），`number of points = count × num_tasks = 64×16 = 1024`。
  - `buffer size = 1024 × sizeof(int) = 4096 bytes`。
- `--cores=2` 配置下：
  - `total_threads = 2×4×4 = 32`，`num_blocks = 8`，`num_tasks = 32`。
  - `number of points = 64×32 = 2048`，`buffer size = 8192 bytes`。
- 两次都应打印 `PASSED!` 且退出码为 `0`。

> 由于 demo 用 `srand(50)` 固定随机种子（[tests/regression/demo/main.cpp:135](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/tests/regression/demo/main.cpp#L135)），结果是确定性的：只要运行时与内核正确，就一定是 PASSED。如果你看到 `FAILED!` 或非 0 退出码，请先回到 u1-l3 检查是否漏了 `../configure` 或工具链未装好。

#### 4.2.5 小练习与答案

**练习 1**：demo 的内核文件名是什么？程序默认按什么名字去内核里查找入口？

**参考答案**：内核文件名是 `kernel.vxbin`（[main.cpp:72](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/tests/regression/demo/main.cpp#L72)），程序按字符串 `"main"` 查找内核入口（[main.cpp:205](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/tests/regression/demo/main.cpp#L205)）。

**练习 2**：为什么改 `--cores` 会让 `number of points` 变化？这段计算逻辑在哪一行？

**参考答案**：因为主机程序运行时通过 `vx_device_query` 查到 `NUM_CORES`，再算出 `total_threads`、`num_blocks`、`num_tasks`，最终 `num_points = count × num_tasks`。逻辑见 [main.cpp:144-158](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/tests/regression/demo/main.cpp#L144-L158)。`--cores` 改变了后端报告的核数，于是点数随之改变。

---

### 4.3 CONFIGS 旋钮与架构覆盖机制

#### 4.3.1 概念说明

`--cores`、`--warps` 这些旋钮背后，其实是 Vortex 配置系统的「宏命名空间」。每个硬件参数都对应一个 `VX_CFG_*` 宏，例如 `NUM_CORES → VX_CFG_NUM_CORES`。`blackbox.sh` 的旋钮只是这些宏的友好别名。

这里要建立两个关键认知：

1. **基线值来自 `VX_config.toml`**：`NUM_CORES=1`、`NUM_WARPS=4`、`NUM_THREADS=4`、`L2_ENABLE=false` 等默认值都写在这个 toml 里（u2 会深入）。旋钮的作用是「临时覆盖」这些基线，而不必改 toml。
2. **配置必须在「两侧」都生效**：一侧是驱动后端（如 SimX 的 core 模型，它会 `#include` 生成的 `VX_config.h`），另一侧是应用程序（主机二进制与设备内核）。`blackbox.sh` 之所以省心，正是因为它把 `CONFIGS` 同时传给了重建驱动的 `make` 和运行 app 的 `make run-<driver>`。

#### 4.3.2 核心流程

配置值的流转（自顶向下）：

```
命令行旋钮            --cores=2
    ↓ blackbox.sh parse_args
CONFIGS 变量          -DVX_CFG_NUM_CORES=2
    ↓ 同时传给两条 make
    ├─ make -C sw/runtime/simx     CONFIGS=...   → 重建 SimX 模型（侧 A：驱动）
    └─ make -C tests/regression/demo run-simx    → 重建 app/内核（侧 B：应用）
                ↓ common.mk
        XCONFIGS = gen_config.py 把 CONFIGS 与 toml 合并解析出的最终宏集
                ↓
        VX_CFLAGS += $(XCONFIGS)   ← 设备内核用
        CXXFLAGS  += $(XCONFIGS)   ← 主机程序用
```

其中 `common.mk` 用 `gen_config.py` 把命令行传入的 `CONFIGS` 与 `VX_config.toml` 的基线合并，解析出最终的 `XCONFIGS` 宏集合（[tests/regression/common.mk:14](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/tests/regression/common.mk#L14)），再同时投影到设备侧 `VX_CFLAGS` 与主机侧 `CXXFLAGS`。这就是「两侧一致」在工程上的落点。

#### 4.3.3 源码精读

**(1) 基线值：toml 里的默认架构**

[VX_config.toml:4-5](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/VX_config.toml#L4-L5) 与 [VX_config.toml:43-44](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/VX_config.toml#L43-L44) 给出默认规模：1 个 cluster、1 个 core、4 个 warp、每 warp 4 个 thread。缓存默认关闭（[VX_config.toml:16-17](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/VX_config.toml#L16-L17)）。所以不传任何旋钮时，demo 跑在「单核、4 warp、4 thread、无 L2/L3」的最小配置上。

**(2) run-simx 目标如何把 CONFIGS 传下去**

[tests/regression/common.mk:188-190](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/tests/regression/common.mk#L188-L190) 是 `run-simx` 目标：

```makefile
run-simx: $(PROJECT) kernel.vxbin
	$(RUNTIME_ARGS) $(MAKE) -C $(VORTEX_RT_SRC)/simx DESTDIR=$(VORTEX_RT_LIB)
	LD_LIBRARY_PATH=$(VORTEX_RT_LIB):$(LD_LIBRARY_PATH) VORTEX_DRIVER=simx ./$(PROJECT) $(OPTS)
```

注意 `$(RUNTIME_ARGS)`（[tests/regression/common.mk:160](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/tests/regression/common.mk#L160)）里包含 `CONFIGS="$(CONFIGS)"`，于是重建 simx 运行时也带上了相同的宏。运行程序时通过环境变量 `VORTEX_DRIVER=simx` 告诉 stub 分发器去 `dlopen` simx 后端（这正是 u1-l1 讲的机制）。`LD_LIBRARY_PATH` 则确保加载到刚刚构建出的 `libvortex.so`。

**(3) 工程纪律：两侧必须一致**

[AGENTS.md:77](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/AGENTS.md#L77) 明确警告：`blackbox.sh` 本身只负责重建驱动；如果你之前用一组宏（比如 `-DVX_CFG_NUM_THREADS=4`）单独编译过 app，再带着另一组宏跑 blackbox，两侧不一致就会出问题。正确做法是先 `make -C tests/regression/<app> clean`，再带上相同的 `CONFIGS` 重新构建。好在**通过 blackbox.sh 端到端运行时，`common.mk` 的 `config.stamp` 机制会检测到 `CONFIGS` 变化并强制重建 app 与内核**，所以你只要一直走 blackbox.sh 就不会踩这个坑。

[AGENTS.md:108-110](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/AGENTS.md#L108-L110) 还说明：`blackbox.sh` 只把常用旋钮暴露成 flag；任何没有专属 flag 的参数，都可以直接用 `CONFIGS="-D..."`（统一加 `VX_CFG_*` 前缀）传入。

#### 4.3.4 代码实践

**实践目标**：体会「旋钮只是别名」，学会用 `CONFIGS=` 传一个 blackbox 没有专属 flag 的宏。

**操作步骤**：

1. 用等价的 `CONFIGS=` 写法复现 `--cores=2`，并与旋钮写法对比结果是否一致：
   ```sh
   ./ci/blackbox.sh --driver=simx --app=demo CONFIGS="-DVX_CFG_NUM_CORES=2"
   ```
   （注意：这是 shell 变量赋值，会进入 `common.mk` 的 `CONFIGS`，效果应等同于 `--cores=2`。）
2. 想要启用 L2 缓存，可以用 flag，也可以用宏：
   ```sh
   ./ci/blackbox.sh --driver=simx --app=demo --l2cache
   ```

**需要观察的现象**：第 1 步运行后，`blackbox.sh` 会在执行前打印一行 `CONFIGS=...`（见 [ci/blackbox.sh:176-178](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/blackbox.sh#L176-L178)），你可以借此核对最终生效的宏。两种写法打印的 `CONFIGS` 内容、以及 demo 的输出点数应当一致。

**预期结果**：`--cores=2` 与 `CONFIGS="-DVX_CFG_NUM_CORES=2"` 产生相同的 `number of points`（2048）与 `PASSED!`。具体输出待本地验证。

> 说明：`blackbox.sh` 没有为「重建驱动与否」提供 `--rebuild` 之类的 flag；它每次都会按 `CONFIGS` 重建驱动（除非用 `--nohup` 走临时目录的隔离构建，见 [ci/blackbox.sh:186-202](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/blackbox.sh#L186-L202)）。以脚本源码为准。

#### 4.3.5 小练习与答案

**练习 1**：默认（不传任何旋钮）时，demo 跑在多大的 GPU 上？

**参考答案**：1 cluster × 1 core × 4 warps × 4 threads，无 L2/L3 缓存。依据 [VX_config.toml:4-5](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/VX_config.toml#L4-L5)、[VX_config.toml:43-44](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/VX_config.toml#L43-L44)、[VX_config.toml:16-17](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/VX_config.toml#L16-L17)。

**练习 2**：为什么说「通过 blackbox.sh 端到端运行就不容易踩两侧不一致的坑」？

**参考答案**：因为 `blackbox.sh` 把同一份 `CONFIGS` 同时传给了重建驱动（`build_driver`）和运行 app（`run_app` → `make run-simx`）；而 `common.mk` 的 `config.stamp` 会在 `CONFIGS` 变化时强制重建 app 与内核，从而保证「驱动侧」与「应用侧」看到的是同一组架构宏。

---

## 5. 综合实践

把本讲三个模块串起来，完成下面这个「最小闭环」任务：

**任务**：用 `blackbox.sh` 在 SimX 上跑通 demo，并通过观察输出验证你对「旋钮 → 宏 → 程序行为」这条链路的理解。

**步骤**：

1. 在 `build/` 目录执行默认运行，记录输出：
   ```sh
   ./ci/blackbox.sh --driver=simx --app=demo 2>&1 | tee run1.log
   echo "exit code: $?"
   ```
2. 改成 2 核 + L2 缓存重跑：
   ```sh
   ./ci/blackbox.sh --driver=simx --app=demo --cores=2 --l2cache 2>&1 | tee run2.log
   ```
3. 打开 `run1.log` 与 `run2.log`，对照回答：
   - 两次 blackbox 打印的 `CONFIGS=...` 分别是什么？（核对 [ci/blackbox.sh:176-178](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/blackbox.sh#L176-L178)）
   - 两次的 `number of points` 与 `buffer size` 各是多少？是否符合「点数随核数翻倍」的预期？
   - 两次是否都打印 `PASSED!`、退出码是否都是 0？
4. （进阶）把 `--cores=2 --l2cache` 改写成纯 `CONFIGS=` 写法，确认输出与第 2 步一致。

**验收标准**：

- 你能用一句话解释 `--cores=2` 是如何同时影响 SimX 模型与 demo 程序的；
- 你能指出 demo 里「根据设备规模计算启动维度」的代码位置（[main.cpp:144-158](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/tests/regression/demo/main.cpp#L144-L158)）；
- 你理解退出码 0 + `PASSED!` 就是「跑通」的判据。

> 如果你没有可在本地构建的环境，本任务可降级为「源码阅读型实践」：仅完成第 3 步的对照分析（基于本讲给出的预期数值），并手写一份 `CONFIGS` 与 `make` 命令的推算结果。运行结果待本地验证。

---

## 6. 本讲小结

- `ci/blackbox.sh` 是统一启动器：解析旋钮 → 定位后端与 app → 重建驱动 → 调用 `make run-<driver>`。默认后端是 `simx`、默认程序是 `sgemm`。
- 架构旋钮（`--cores/--warps/--threads/--clusters/--l2cache/--l3cache`）只是 `VX_CFG_*` 宏的友好别名，会被累加进 `CONFIGS` 变量。
- `demo` 是一个完整的向量加法样例：主机程序用运行时 API 编排「打开设备→分配→拷入→启动→拷回→校验」，设备内核做 `dst=src0+src1`。
- 主机程序在运行时通过 `vx_device_query` 查询 `NUM_CORES/NUM_WARPS/NUM_THREADS`，再据此计算启动维度——所以改 `--cores` 会改变 demo 处理的数据点数。
- 判断「跑通」的依据是程序打印 `PASSED!` 且退出码为 0；`demo` 用固定随机种子，结果是确定性的。
- 纪律：`CONFIGS` 必须在驱动侧与应用侧同时生效；一直走 `blackbox.sh` 端到端运行可避免两侧不一致的坑。

---

## 7. 下一步学习建议

跑通 demo 之后，建议按下面顺序继续：

1. **横向多跑几个样例**：把 `--app=demo` 换成 `--app=vecadd`、`--app=sgemm --args="-n10"`（见 [AGENTS.md:95-96](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/AGENTS.md#L95-L96)），熟悉不同程序的输出形态。
2. **进入配置系统（U2）**：本讲把 `VX_config.toml` 当作「默认值来源」用；u2-l1 会带你深入这个 toml 与 `VX_types.toml` 的职责划分，以及 `gen_config.py` 如何把 toml 翻译成三种输出。
3. **理解运行时 API（U3）**：本讲你已经在 demo 里见到了 `vx_device_open / vx_buffer_create / vx_enqueue_launch` 等调用；u3-l1 会系统讲解 `sw/runtime/include/vortex.h` 暴露的完整主机接口。
4. **尝试调试输出**：等学完 u13 后，可以用 `./ci/blackbox.sh --app=demo --debug=3` 生成运行时 trace，观察每条指令的执行（参考 [README.md:149](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/README.md#L149)）。

# 运行第一个 HLS L1 用例：stream_dup

## 1. 本讲目标

本讲带你「亲手跑通」仓库里的第一个 L1 HLS 用例，建立对 Vitis 加速库测试方式的肌肉记忆。学完后你应该能够：

- 说出一个 L1 用例目录里包含哪些文件、各自的作用。
- 用 `make run TARGET=csim` 跑通 `utils/L1/tests/stream_dup`，并在输出里找到 `PASS`。
- 读懂 `test.cpp` 中「DUT 封装 + main 测试入口」的标准结构。
- 读懂 `description.json` 如何用元数据向工具链/CI 声明这个用例的流程、平台、顶层函数与时钟。
- 养成「改参数前先读源码」的习惯——我们会用 `NUM_COPY` 这个参数演示一次源码级的行为预判。

## 2. 前置知识

在动手前，请确认你已经具备以下认知（来自前置讲义）：

- **L1 是什么**：L1 是「可复用的算法原语」，表现为一个 HLS C++ 函数，目标是做功能与资源验证，还不涉及上板。参见 u1-l3。
- **两套流程不要混淆**：L1 走 HLS 的五个大写 `TARGET`（`csim`/`csynth`/`cosim`/`vivado_syn`/`vivado_impl`，保真度与代价逐级递增）；L2/L3 走 Vitis 的小写 `target`（`sw_emu`/`hw_emu`/`hw`）。本讲只碰 `csim`——纯 C 软件仿真，最快、不需要综合。参见 u1-l3。
- **环境已就绪**：你已经 `source` 了 `Vitis/settings64.sh` 与 `xrt/setup.sh`，`v++`、`vitis-run`、`vivado` 在 `PATH` 中可用。参见 u2-l1。
- **hls::stream 直觉**：这是 HLS 里「先进先出」的数据通道，用 `.write()` 写、`.read()` 读。`stream_dup` 要做的事情就是把一条输入流「复制」成多条输出流。其底层细节会在 u3-l1 展开，本讲只需把它当成一个黑盒来驱动即可。

> 名词小贴士：**DUT**（Design Under Test，待测设计）= 我们要验证的那个硬件函数；**testbench** = 喂数据给 DUT、再核对结果的测试台。在 L1 里这两者通常写在同一个 `test.cpp` 中。

## 3. 本讲源码地图

本讲涉及的关键文件（都在 `utils` 库下）：

| 文件 | 作用 |
| --- | --- |
| `utils/L1/tests/stream_dup/test.cpp` | 测试台：包含 DUT 封装 `dut0`/`dut1`、测试函数 `test_dut0`/`test_dut1` 和 `main` 入口。 |
| `utils/L1/tests/stream_dup/Makefile` | 驱动 HLS 流程的 Make 脚本，把 `make run TARGET=csim` 翻译成 `vitis-run` 调用。 |
| `utils/L1/tests/stream_dup/description.json` | 用例元数据：声明流程、平台白名单、顶层函数、时钟、各阶段资源/时间限额。 |
| `utils/L1/tests/stream_dup/hls_config.tmpl` | HLS 配置模板；Makefile 用它（经环境变量替换）生成 `hls_config.cfg`。 |
| `utils/L1/tests/stream_dup/run_hls.tcl` | 可选的 Vitis HLS TCL 流程脚本，是 Makefile 流程之外的另一条入口。 |
| `utils/L1/include/xf_utils_hw/stream_dup.hpp` | `streamDup` 原语的实现头件，是 DUT 真正调用的库代码。 |

> 一个用例目录的「标准三件套」是 **`test.cpp` + `Makefile` + `description.json`**；`hls_config.tmpl` 和 `run_hls.tcl` 是配套辅助文件。仓库里几乎所有 L1 用例都遵循同样的布局。

## 4. 核心概念与源码讲解

### 4.1 L1 用例目录结构

#### 4.1.1 概念说明

Vitis 加速库的每个 L1 用例都自成一个小工程，放在 `<lib>/L1/tests/<case_name>/` 目录下。一个目录 = 一个可独立 `make` 的测试单元。这种「目录即用例」的约定让 CI 和人都能用统一方式批量发现、运行用例。

`stream_dup` 目录下一共有 5 个文件，分工是：

- `test.cpp`：唯一的 C++ 源文件，既含 DUT（会被综合成硬件的那个函数），也含 testbench（只在软件仿真里跑、用 `#ifndef __SYNTHESIS__` 包起来，综合时会被剔除）。
- `Makefile`：标准入口，`make run TARGET=...` 就是从这里开始。
- `description.json`：用例的「身份证」，工具链与 CI 读它来决定怎么调度这个用例。
- `hls_config.tmpl`：HLS 配置文件模板，运行时被替换成真正的 `hls_config.cfg`。
- `run_hls.tcl`：另一条可选流程——直接在 Vitis HLS 里 `source run_hls.tcl` 也能跑（它在脚本里硬编码了 part、并依次执行 `csim_design`/`csynth_design` 等）。

#### 4.1.2 核心流程

```text
make run TARGET=csim
        │
        ▼
  Makefile 读 TARGET/PLATFORM/XPART
        │
        ▼
 由 hls_config.tmpl 经环境变量替换 ──► hls_config.cfg
        │
        ▼
 vitis-run --mode hls --config hls_config.cfg --csim
        │
        ▼
 编译并运行 test.cpp 的 main()（软件仿真）
        │
        ▼
 打印 PASS / FAIL
```

> 注意：`csim` 不做任何综合，只是把 `test.cpp` 当普通 C++ 程序编译运行，用来快速验证算法功能对不对。综合相关的事（`csynth` 及之后）才需要 `v++ -c`，见 4.2。

#### 4.1.3 源码精读

`run_hls.tcl` 给了我们一个观察「一个 L1 用例要声明什么」的简洁窗口——它把用例身份信息列得很清楚：

[utils/L1/tests/stream_dup/run_hls.tcl:22-37](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/L1/tests/stream_dup/run_hls.tcl#L22-L37) — 这段 TCL 设置了项目根 `XF_PROJ_ROOT`、目标 part、顶层函数 `dut0`，并指定 `test.cpp` 同时作为综合源与 testbench 源。

而 Makefile 流程里，同样的信息以「模板 + 元数据」的形式分散在 `hls_config.tmpl` 与 `description.json` 中。模板里声明的顶层函数也是 `dut0`：

[utils/L1/tests/stream_dup/hls_config.tmpl:3-11](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/L1/tests/stream_dup/hls_config.tmpl#L3-L11) — 声明 `clock=2.5`、`syn.top=dut0`，并给 `test.cpp` 加上 `-I${XF_PROJ_ROOT}/L1/include` 以便能找到库头件；`csim.argv=0` 把命令行参数 `0` 传给 testbench。

#### 4.1.4 小练习与答案

**练习 1**：仓库里 `utils/L1/tests/` 下还有哪些用例目录？它们的文件组成和 `stream_dup` 一样吗？

**参考答案**：有很多，例如 `axi_to_stream`、`cache_ro_1DDR_with_e`、`stream_combine` 等。它们的文件组成高度一致——都是 `Makefile` + `description.json` + `hls_config.tmpl` + `run_hls.tcl` + 一个 C++ 源文件（多数叫 `test.cpp`，部分叫 `*_tb.cpp`）。这就是「目录即用例」的统一约定。

### 4.2 `make run TARGET=csim`：从命令到执行

#### 4.2.1 概念说明

`make run TARGET=csim` 是运行 L1 用例的标准命令。它背后的核心工具是 `vitis-run --mode hls`，这是 Vitis 对 HLS 流程的命令行封装。Makefile 的职责是：检查环境、解析目标平台/part、生成配置文件、最后拼出正确的 `vitis-run` 命令行。

`csim` 是五个大写 `TARGET` 中最轻量的一档：纯软件编译执行，不调用综合器，几秒到几十秒就能看到结果，最适合我们「先确认功能正确」。

#### 4.2.2 核心流程

Makefile 里跟运行直接相关的两个目标（rule）是 `all` 和 `run`：

1. **环境与平台解析**：`check_vivado`/`check_vpp` 确认工具链存在；`check_part` 把 `PLATFORM`（或 `XPART`）解析成一个具体的 FPGA part 号。u2-l1 讲过的三级兜底搜索就实现在这里。
2. **配置文件生成**：把 `hls_config.tmpl` 通过一段内嵌 Python 做 `string.Template` 环境变量替换，写出 `hls_config.cfg`。
3. **`all` 目标**：只有当 `TARGET` 不是 `csim` 时才调用 `v++ -c --mode hls` 做综合编译——也就是说 **`csim` 跳过综合**。
4. **`run` 目标**：只有当 `TARGET` 不是 `csynth` 时才调用 `vitis-run --mode hls --config ... --$(TARGET_REL)`。对 `csim` 而言就是执行软件仿真。

#### 4.2.3 源码精读

先看默认值：Makefile 给 `TARGET` 和默认平台都设了缺省值。

[utils/L1/tests/stream_dup/Makefile:49-56](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/L1/tests/stream_dup/Makefile#L49-L56) — 默认 `PLATFORM=xilinx_vck190_base_202610_1`、`TARGET=csim`、配置文件由 `hls_config.tmpl` 生成。所以即便你不传任何参数，`make run` 默认就是跑 csim。

配置文件的生成是用一段导出的 Python 脚本完成的（`string.Template` 把 `${VAR}` 替换成同名环境变量的值）：

[utils/L1/tests/stream_dup/Makefile:160-173](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/L1/tests/stream_dup/Makefile#L160-L173) — 把模板读入、用 `os.environ` 做变量替换、写出 `hls_config.cfg`。模板里的 `${VIVADO_FLOW}`、`${XF_PROJ_ROOT}` 就是这样被填进去的。

最关键的是 `all` 与 `run` 两个目标——它们决定了 `csim` 到底跑了什么：

[utils/L1/tests/stream_dup/Makefile:178-187](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/L1/tests/stream_dup/Makefile#L178-L187) — `all` 用 `ifneq (csim)` 守卫，所以 csim 时**不**执行 `v++ -c`（不做综合）；`run` 用 `ifneq (csynth)` 守卫，对 csim 执行 `vitis-run --mode hls --config hls_config.cfg --csim`。这一行就是你看到的仿真真正发生的地方。

#### 4.2.4 代码实践

**实践目标**：在自己的环境里跑通 `stream_dup` 的 csim，看到 `PASS`。

**操作步骤**：

1. 进入用例目录：`cd utils/L1/tests/stream_dup`
2. 直接运行（默认即 csim）：`make run TARGET=csim`
   - 如果默认平台不在你的安装目录里，按 u2-l1 的方法显式指定，例如 `make run TARGET=csim XPART=xcu200-fsgd2104-2-e`。
3. 观察终端输出。

**需要观察的现象**：Makefile 会先打印平台解析过程，最后 `vitis-run` 编译并运行 testbench，打印出测试结果。

**预期结果**：输出应包含

```text
PASS: no error found.
```

> 待本地验证：本讲写作环境未安装 Vitis，无法在此实跑。上面是基于源码（`main` 在 `argv` 为 `"0"` 时调用 `test_dut0`，`test_dut0` 用默认参数 `NUM_COPY=16` 时数据自洽）得出的预期；请在你本机确认实际输出。

### 4.3 `test.cpp` 的 DUT 与 main 测试入口

#### 4.3.1 概念说明

L1 用例的 `test.cpp` 通常承担两个角色：

- **DUT（待测设计）**：一个 `extern "C"` 的顶层函数，会被综合成硬件。它把模板化的库原语「实例化」成具体类型/具体数量的版本，对外暴露简单的 C 接口。
- **testbench**：一段只在软件里运行的代码，负责造数据、喂数据给 DUT、回收结果并与「黄金参考」比对。它用 `#ifndef __SYNTHESIS__` 包裹，综合时被编译器整段忽略。

`stream_dup` 的 DUT 叫 `dut0`，它做的事很直白：把一条输入流复制成 `NUM_COPY` 条相同的输出流。

#### 4.3.2 核心流程

`test_dut0` 的执行可以拆成四步：

```text
1. 造数据：testdata[i][j] = i*10 + j；并预算黄金参考 glddata（每个输入被复制 NUM_COPY 份）
2. 喂数据：把 testdata 写入 hls::stream istrm，并配一条 e_istrm（end 标志流），末尾写 1 表示结束
3. 调 DUT：dut0(...) 内部调用 streamDup<...> 把 istrm 复制成 NUM_COPY 条输出
4. 对结果：逐条读出 ostrms，与 glddata 比对；同时核对 end 标志。全对 → nerr=0 → PASS
```

`main` 根据 `argv[1]` 的第一个字符决定跑哪个测试：`'0'` → `test_dut0`，`'1'` → `test_dut1`。而 csim 的 argv 由配置文件定为 `"0"`，所以 **csim 跑的就是 `test_dut0`**。

#### 4.3.3 源码精读

先看文件顶部的参数定义与 DUT：

[utils/L1/tests/stream_dup/test.cpp:26-38](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/L1/tests/stream_dup/test.cpp#L26-L38) — 定义了 `TYPE=uint32_t` 与四个规模常量（`LEN_STRM=10`、`NUM_ISTRM=8`、`NUM_DSTRM=4`、`NUM_COPY=16`）；`dut0` 把模板原语 `streamDup<TYPE, NUM_COPY>` 包成 `extern "C"` 接口，输入一条流 + 一条 end 标志流，输出 `NUM_COPY` 条流 + 对应 end 标志流。

`dut0` 调用的库原语实现就在头件里，核心是一个 II=1 的循环，每个输入元素并行写到所有输出流：

[utils/L1/include/xf_utils_hw/stream_dup.hpp:87-108](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/L1/include/xf_utils_hw/stream_dup.hpp#L87-L108) — `while(!e)` 循环里 `#pragma HLS pipeline II=1` + 内层 `#pragma HLS unroll`，把读入的 `tmp` 同时写到 `_NStrm` 条输出流；读到 end 标志后，给每条输出流写一个 `1` 作为结束信号。（pragma 的硬件含义留到 u3-l2 详讲。）

再看 `main` 如何选择测试：

[utils/L1/tests/stream_dup/test.cpp:261-283](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/L1/tests/stream_dup/test.cpp#L261-L283) — `main` 读 `argv[1][0]`：`'0'` 调 `test_dut0`、`'1'` 调 `test_dut1`；无错打印 `PASS: no error found.`，有错打印 `FAIL: nerror= N errors found.`。

#### 4.3.4 小练习与答案

**练习 1**：为什么 testbench 部分（`test_dut0`/`test_dut1`/`main`）要用 `#ifndef __SYNTHESIS__ ... #endif` 包起来？

**参考答案**：因为综合时只关心 DUT（`dut0`/`dut1`）这个硬件函数。testbench 里有 `main`、动态数组、`std::cout` 等「不可综合」的软件结构。`__SYNTHESIS__` 这个宏在 HLS 综合时由编译器定义，所以 `#ifndef __SYNTHESIS__` 这段在综合时会被剔除，只留下 DUT；而在 csim（普通 C++ 编译）时该宏未定义，testbench 正常参与编译运行。这样一份源文件就能同时服务「综合」和「仿真」两个目的。

**练习 2**：csim 阶段 `argv[1]` 的值是哪里来的？

**参考答案**：来自配置文件。`hls_config.tmpl` 里有 `csim.argv=0`（见 4.1.3），`description.json` 里也对应写了 `"hls_csim": "0"`（见 4.4）。所以 `vitis-run` 运行 testbench 时传入参数 `"0"`，`argv[1][0]=='0'`，于是执行 `test_dut0`。

### 4.4 `description.json` 元数据

#### 4.4.1 概念说明

如果说 `test.cpp` 是「给机器跑的代码」，`description.json` 就是「给工具链和 CI 看的说明书」。它声明：这个用例叫什么、走哪条流程、允许在哪些平台上跑、顶层函数是谁、时钟多快、每个阶段最多用多少内存和时间。CI（Jenkinsfile 背后的共享流水线）正是靠它来决定要不要、以及怎么调度这个用例。

#### 4.4.2 核心流程

```text
description.json
   ├── flow: "hls"              ──► 这是 HLS 流程用例（区别于 L2/L3 的 vivado/vitis 流程）
   ├── platform_allowlist       ──► 只在这些平台上跑（如 vck190）
   ├── topfunction / top.source ──► 顶层函数 + 综合源 + include cflags
   ├── testbench.argv           ──► csim/cosim 传给 main 的参数
   └── testinfo.targets         ──► 该用例参与的 5 个阶段 + 每阶段内存/时间上限 + category
```

#### 4.4.3 源码精读

逐块看 `stream_dup` 的 `description.json`：

[utils/L1/tests/stream_dup/description.json:4-14](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/L1/tests/stream_dup/description.json#L4-L14) — 声明 `flow=hls`、平台白名单仅 `vck190`、`clock=2.5`、顶层函数 `dut0`。

[utils/L1/tests/stream_dup/description.json:15-32](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/L1/tests/stream_dup/description.json#L15-L32) — `top` 段给出综合源 `test.cpp` 及 cflags `-I${XF_PROJ_ROOT}/L1/include`；`testbench` 段给出 testbench 源，并指定 `hls_csim`/`hls_cosim` 的 argv 都为 `"0"`——这正是 4.3 里 `main` 走 `'0'` 分支的由来。

[utils/L1/tests/stream_dup/description.json:33-65](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/L1/tests/stream_dup/description.json#L33-L65) — `testinfo` 段列出该用例参与的 5 个阶段（`hls_csim`/`hls_csynth`/`hls_cosim`/`vivado_syn`/`vivado_impl`）、每个阶段的内存与时间上限（如 `hls_csim` 上限 10240 MB / 60 min），以及 `category: canary`（「金丝雀」用例，常用于快速冒烟回归）。

#### 4.4.4 小练习与答案

**练习 1**：如果把 `platform_allowlist` 改成空数组，或把 `testinfo.disable` 设为 `true`，会发生什么？

**参考答案**：`platform_allowlist` 为空通常意味着「不限制平台」（具体语义以 CI 流水线实现为准）；而 `testinfo.disable=true` 会让 CI 跳过这个用例、不调度它。这类元数据就是 CI 用来做「平台过滤」和「用例开关」的开关量。

## 5. 综合实践

**实践任务**（本讲的核心任务）：把 `NUM_COPY` 从 `16` 改成 `8` 重新运行 csim，**先预判、再验证**结果，并解释原因。这条任务贯穿了本讲全部内容——它要求你读懂参数、读懂 testbench 的数据流、并真正跑一次 `make run TARGET=csim`。

### 步骤 1：跑通默认版本（基线）

```bash
cd utils/L1/tests/stream_dup
make run TARGET=csim
```

预期看到 `PASS: no error found.`（待本地验证）。

### 步骤 2：改参数前，先读源码做预判

不要急着改完就跑。先对照 `test.cpp` 想清楚：`NUM_COPY` 同时被用在 DUT 和 testbench 的哪些地方？把宏改成 8 之后，DUT 产生的数据形状和 testbench 的「黄金参考」还对得上吗？

关键在于 testbench 如何生成黄金参考。看这段：

[utils/L1/tests/stream_dup/test.cpp:58-68](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/L1/tests/stream_dup/test.cpp#L58-L68) — 注意 `glddata` 的声明大小是 `[NUM_COPY * NUM_DSTRM]`，但**生成黄金参考的循环里用的是字面量 `16`**：`for (int k = 0; k < 16; k++)` 且 `glddata[i * 16 + k][j] = ...`，而不是 `NUM_COPY`。

再看核对结果的循环：

[utils/L1/tests/stream_dup/test.cpp:102-113](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/L1/tests/stream_dup/test.cpp#L102-L113) — 这里用的却是 `NUM_COPY`：循环 `k < NUM_COPY`，比对的是 `glddata[i * NUM_COPY + k][j]`。

### 步骤 3：基于源码的预判（重要）

把这两段对照起来，你能得出一个相当确定的结论：

- **生成端**用步长 `16` 写 `glddata[i*16+k]`；
- **核对端**用步长 `NUM_COPY` 读 `glddata[i*NUM_COPY+k]`。

两者只在 `NUM_COPY == 16`（默认值）时一致，所以默认会 `PASS`。一旦把 `NUM_COPY` 改成 `8`，就会出现两件事：

1. **步长不一致 → 比对错位**：对 `i=1`，核对端读 `glddata[1*8+k]=glddata[8..15]`，但这些位置在生成端是 `i=0` 时写入的值（`testdata[0]`），而 DUT 实际输出的是 `testdata[1]`。两者不等 → `nerr++`。同理 `i=2,3` 也错位。
2. **数组越界写 → 未定义行为**：`glddata` 大小是 `NUM_COPY*NUM_DSTRM = 8*4 = 32` 行，但生成端按 `i*16+k` 写到 `i=2` 时已经访问下标 `32..47`（`i=3` 时 `48..63`），全部越界，属于栈上的未定义行为，可能进一步污染数据甚至崩溃。

因此**源码层面的预判是：改成 `NUM_COPY=8` 后会 `FAIL`（打印 `FAIL: nerror= N errors found.`），而不是 `PASS`**。这并不是 DUT `streamDup` 本身有问题——原语只是忠实地把输入复制成 `NUM_COPY` 份；问题出在 testbench 的黄金参考生成里硬编码了 `16`，没有跟随 `NUM_COPY` 变化。

### 步骤 4：实机验证

```bash
# 把 test.cpp 第 31 行的 #define NUM_COPY 16 改成 #define NUM_COPY 8
make clean
make run TARGET=csim
```

**需要观察的现象**：终端是否打印 `FAIL: nerror= ...`，以及是否有数据/越界相关的异常迹象。

**预期结果**：按步骤 3 的预判，应输出 `FAIL`。**待本地验证**：本讲写作环境未安装 Vitis，以上为基于源码的预判，请在你本机实际确认。验证后你可以试着自己把生成端那两处字面量 `16` 改成 `NUM_COPY`（这是「示例修复」，仅用于理解，**不要提交对源码的改动**），重新跑 csim，观察它是否如你预期那样变回 `PASS`。

> 这个练习的真正价值不在于「改一个数」，而在于：**改任何参数之前，先顺着源码追一遍它会影响谁**。Vitis 库的 testbench 绝大多数都是参数化、自洽的，但 `stream_dup` 的 `test_dut0` 恰好在黄金参考生成处遗留了一个字面量 `16`，正好成了一个绝佳的「源码阅读训练样本」。

## 6. 本讲小结

- 一个 L1 用例 = 一个目录，标准「三件套」是 `test.cpp` + `Makefile` + `description.json`（外加 `hls_config.tmpl`、`run_hls.tcl`）。
- `make run TARGET=csim` 经 Makefile 翻译为 `vitis-run --mode hls --config hls_config.cfg --csim`；`csim` 只做软件编译运行、不做综合，是最快验证功能的一档。
- `test.cpp` 一人分饰两角：`extern "C"` 的 DUT（会被综合）+ `#ifndef __SYNTHESIS__` 包裹的 testbench（只在仿真跑）；`main` 依 `argv[1]` 选择测试，csim 固定传 `"0"` → 跑 `test_dut0`。
- `description.json` 是用例的「身份证」，向工具链/CI 声明流程、平台白名单、顶层函数、时钟与各阶段资源/时间限额。
- 实践教训：`stream_dup` 的 `test_dut0` 在黄金参考生成处硬编码了 `16`、核对处却用 `NUM_COPY`，所以把 `NUM_COPY` 改成 8 会导致 `FAIL`——**改参数前先读源码**。

## 7. 下一步学习建议

- 想理解 `csim` 之外那几档（`csynth`/`cosim`/`vivado_syn`/`vivado_impl`）各自做什么、产出什么报告？继续学 **u2-l3《HLS TARGET 流程与综合报告解读》**。
- 想搞懂 DUT 里 `hls::stream`、end 标志流、`ap_int` 宽类型、以及 `extern "C"` 封装背后的约定？进入 **u3-l1《hls::stream、ap_int 与 DUT 封装约定》**。
- 想了解 `#pragma HLS pipeline/unroll` 如何决定吞吐与面积？进入 **u3-l2《HLS pragma 如何映射硬件》**。
- 建议同步浏览 `utils/L1/tests/` 下其它用例目录（如 `stream_combine`、`axi_to_stream`），对照本讲确认你是否能独立说出每个文件的作用、并跑通它们的 csim。

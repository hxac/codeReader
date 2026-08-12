# 测试基础设施与 CI

## 1. 本讲目标

前面十几讲我们学了「怎么读懂、怎么手写」一个加速内核。但一个有几千个用例的单体仓库，靠人手一个一个跑是不现实的——必须有一套**机器可读的用例描述 + 自动生成的工具配置 + 批量驱动 + 统一 CI 流水线**的机制。

本讲把视角从「单个内核」抬升到「整个仓库怎么被自动测试」，学完后你应该能够：

- 读懂任意一个用例目录里的 `description.json`，说出它的流程（flow）、平台白名单、顶层函数、时钟、测试档位与资源限额。
- 解释 `hls_config.tmpl` 是如何被 Makefile 内嵌的 Python 片段「按环境变量替换占位符」生成 `hls_config.cfg` 的。
- 理解 blas 库另辟的 `run_test.py` 测试总线如何按 profile 批量生成与并发运行用例，并用 `statistics.rpt` + 退出码判定 CI。
- 说出顶层 `Jenkinsfile` 为什么只有两行就能驱动全部 9 个库的 CI，以及「薄入口 + 厚共享流水线库」的含义。

本讲是后续 u14-l2（自己写一个内核并纳入 CI）的直接前置。

## 2. 前置知识

本讲假定你已经掌握（若生疏可回看对应讲义）：

- **「目录即用例」约定**（u2-l2）：每个 L1 用例是一个独立可 `make` 的目录，标准三件套为 `test.cpp`、`Makefile`、`description.json`，外加 `hls_config.tmpl`、`run_hls.tcl`。
- **HLS 五个大写 TARGET**（u2-l3）：`csim / csynth / cosim / vivado_syn / vivado_impl`，保真度与代价逐级递增；`csim` 只验功能、`csynth` 首次出硬件报告。
- **DUT 与 testbench 的 `__SYNTHESIS__` 宏切分**（u3-l1）：`test.cpp` 一人分饰两角，综合时取 DUT、仿真时取 `main`。
- **PL/AIE/system 三种 flow**（u1-l3、u6-l2、u9-l2）：`flow=hls` 走 HLS 五阶段、`flow=aie` 走 AIE 图仿真、`flow=system` 走 Vitis L2/L3 的 `sw_emu/hw_emu/hw`。

本讲新增的关键术语：

- **元数据（metadata）**：描述「这个用例是什么、要在哪些平台、跑哪些档位、用多少资源」的数据，与「用例的源码」分离，供 CI 机器读取。
- **模板（template）**：含 `${VAR}` 占位符的配置文件，运行时把占位符替换成环境变量的真实值，得到最终配置。
- **profile**：blas 测试体系里描述「一组参数组合」的 JSON 文件，一个 profile 对应一次具体的 csim/cosim 实验。
- **CI（持续集成）**：每次提交代码后自动跑测试、按通过/失败给反馈的系统。

## 3. 本讲源码地图

本讲涉及的关键文件，按「从单用例到全仓库」的顺序：

| 文件 | 作用 |
|------|------|
| [utils/L1/tests/stream_dup/description.json](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/L1/tests/stream_dup/description.json) | HLS 用例的元数据身份证（本讲主样本） |
| [utils/L1/tests/stream_dup/hls_config.tmpl](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/L1/tests/stream_dup/hls_config.tmpl) | HLS 配置模板，含 `${...}` 占位符 |
| [utils/L1/tests/stream_dup/Makefile](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/L1/tests/stream_dup/Makefile) | 内嵌 Python 把模板渲染成 `.cfg`，并分派给 `v++`/`vitis-run` |
| [utils/L1/tests/stream_dup/test.cpp](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/L1/tests/stream_dup/test.cpp) | 用例源码，其 `argv`/`dut0` 与上述元数据一一对应 |
| [dsp/L2/tests/aie/widget_real2complex/description.json](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/tests/aie/widget_real2complex/description.json) | AIE flow 的 description.json（对照样本） |
| [vision/L3/tests/resize_pipeline/YUV422_NPC8/description.json](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/vision/L3/tests/resize_pipeline/YUV422_NPC8/description.json) | system flow 的 description.json（对照样本） |
| [blas/L1/tests/run_test.py](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/blas/L1/tests/run_test.py) | blas 自带的批量测试驱动脚本 |
| [blas/L1/tests/blas_gen.mk](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/blas/L1/tests/blas_gen.mk) | blas 用例生成器 Makefile（配合 run_test.py） |
| [Jenkinsfile](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/Jenkinsfile) | 顶层 CI 入口，仅两行 |

## 4. 核心概念与源码讲解

### 4.1 description.json 元数据

#### 4.1.1 概念说明

一个仓库里有成百上千个用例目录，CI 系统（以及 AMD 内部的用例收集器）面对它们的第一个问题是：**我该如何不读源码就知道这个用例该怎么跑？**

答案就是把「用例的身份与跑法」抽出来，写成一个机器可读的 JSON 文件——这就是 `description.json`。它是每个用例目录的**身份证 + 说明书**：声明用例走哪条流程、可以在哪些平台跑、顶层函数叫什么、跑哪些档位、每档最多吃多少内存和时间。

关键点是**数据与代码分离**：源码（`test.cpp`）只负责「算得对」，而 `description.json` 负责「在 CI 里怎么调度它」。改平台白名单、调资源限额都不用动源码。

#### 4.1.2 核心流程

一个 HLS 用例的 `description.json` 由 CI 处理的大致流程：

1. CI 扫描器遍历所有库的 `L1/tests`、`L2/tests`、`L3/tests` 目录，收集每个目录下的 `description.json`。
2. 读取 `flow` 字段决定走哪条流水（`hls` / `aie` / `system`）。
3. 用 `platform_allowlist` / `platform_blocklist` 过滤：当前平台不在白名单或在黑名单里就跳过。
4. 读 `topfunction` 找到要综合的顶层函数；读 `clock` 设定时钟周期。
5. 读 `testinfo.targets` 决定跑哪些档位（如 `hls_csim`、`hls_csynth`...）。
6. 读 `testinfo.jobs` 里的 `max_memory_MB` / `max_time_min` 给每个档位分配资源与超时。
7. 读 `testbench.argv` 拿到喂给 `main(argc, argv)` 的命令行参数。

#### 4.1.3 源码精读

以 `stream_dup` 的 description.json 为样本。最关键的几个字段：

**flow + 平台白名单**——声明这是一个 HLS 流程、且只允许在 `vck190` 平台跑：

```json
"flow": "hls",
"platform_allowlist": [
    "vck190"
],
```

参见 [utils/L1/tests/stream_dup/description.json:4-7](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/L1/tests/stream_dup/description.json#L4-L7)。`flow=hls` 对应 u2-l3 讲的 HLS 五阶段大写 TARGET。

**时钟与顶层函数**——`clock` 是周期（ns），`topfunction` 必须与 `test.cpp` 里 `extern "C"` 的函数名严格一致：

```json
"clock": "2.5",
"topfunction": "dut0",
```

参见 [utils/L1/tests/stream_dup/description.json:13-14](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/L1/tests/stream_dup/description.json#L13-L14)。这里的 `dut0` 正是 test.cpp 里那个 `extern "C" void dut0(...)`（[utils/L1/tests/stream_dup/test.cpp:33-38](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/L1/tests/stream_dup/test.cpp#L33-L38)）。两者对不上，综合就会找不到顶层。

**top 与 testbench**——注意 source 都是 `test.cpp`，因为同一个文件既含 DUT（综合）又含 testbench（仿真，被 `__SYNTHESIS__` 包裹）；`cflags` 用 `${XF_PROJ_ROOT}` 占位符引入库头件：

```json
"top":  { "source": ["test.cpp"], "cflags": "-I${XF_PROJ_ROOT}/L1/include" },
"testbench": { "source": ["test.cpp"], "cflags": "-I${XF_PROJ_ROOT}/L1/include", ... }
```

参见 [utils/L1/tests/stream_dup/description.json:15-32](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/L1/tests/stream_dup/description.json#L15-L32)。其中 `argv` 字段告诉我们仿真时喂给 `main` 的参数：

```json
"argv": { "hls_csim": "0", "hls_cosim": "0" }
```

这个 `"0"` 正是 test.cpp 里 `main` 据以选择 `test_dut0()` 的判断值——`if (argv[1][0] == '0')`（[utils/L1/tests/stream_dup/test.cpp:261-265](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/L1/tests/stream_dup/test.cpp#L261-L265)）。这是 u2-l2 已建立的约定：csim 固定传 `"0"`。

**testinfo（资源与档位）**——这是 CI 调度的核心。`targets` 列出本用例要跑的全部档位；`jobs[0]` 给每个档位分别规定了内存上限和时间上限：

```json
"testinfo": {
    "disable": false,
    "jobs": [ {
        "max_memory_MB": { "vivado_syn": 16384, "hls_csim": 10240, "hls_cosim": 16384,
                           "vivado_impl": 16384, "hls_csynth": 10240 },
        "max_time_min":  { "vivado_syn": 420,   "hls_csim": 60,   "hls_cosim": 420,
                           "vivado_impl": 420,   "hls_csynth": 60 }
    } ],
    "targets": ["hls_csim","hls_csynth","hls_cosim","vivado_syn","vivado_impl"],
    "category": "canary"
}
```

参见 [utils/L1/tests/stream_dup/description.json:33-65](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/L1/tests/stream_dup/description.json#L33-L65)。两个细节值得记住：

- 时间限额与档位代价一一对应：`csim`/`csynth` 是 60 分钟级日常档，`cosim`/`vivado_syn`/`vivado_impl` 是 420 分钟级重型档——这正是 u2-l3 讲的「保真度与代价阶梯」在元数据里的硬编码。
- `category: "canary"`（金丝雀）：这类用例被 CI 视为「快速冒烟测试」，每次提交都跑；与之相对的是 `category: "full"`（见 4.1.4 系统样本），只在每日/特定触发跑全量。

**不同 flow 的差异**。把三个 flow 并排看，能看清 `description.json` 是如何随流程「长出」不同字段的：

| flow | 关键独有字段 | 样本 |
|------|-------------|------|
| `hls` | `topfunction`、`top`、`testbench`、`testinfo.targets` 为 `hls_*`/`vivado_*` | stream_dup |
| `aie` | `platform_properties.param_set`、`aiecompiler`、`aiecontainers`、`generators`、`launch`、`pre_build`/`post_launch` | widget_real2complex |
| `system` | `host`（主机编译）、`v++`、`containers`（加速器/内核）、`launch`、`data`、`check_env` | resize_pipeline |

- AIE 样本里 `param_set` 用通配符为不同平台挑参数（`*_aie1_*` 给 vck190、`*_aie2_*` 给 vek280），呼应 u6-l2 讲的「按平台选 `*_aie1_*`/`*_aie2_*` 参数」。见 [dsp/L2/tests/aie/widget_real2complex/description.json:17-56](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/tests/aie/widget_real2complex/description.json#L17-L56)。其档位是 `vitis_aie_sim` / `vitis_aie_x86sim`（[同文件:262-265](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/tests/aie/widget_real2complex/description.json#L262-L265)）。
- system 样本里 `containers` 把加速器（内核）与频率声明出来，`launch` 给出 `hw_emu`/`hw` 的运行命令与依赖库路径，`check_env` 在跑前校验 `OPENCV_INCLUDE` 等环境变量。见 [vision/L3/tests/resize_pipeline/YUV422_NPC8/description.json:85-114](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/vision/L3/tests/resize_pipeline/YUV422_NPC8/description.json#L85-L114)。其档位是 `vitis_hw_emu` / `vitis_hw_build` / `vitis_hw_run`，`category: "full"`（[同文件:116-142](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/vision/L3/tests/resize_pipeline/YUV422_NPC8/description.json#L116-L142)）。

可以看到：**`flow` 字段决定了 `description.json` 的「骨架形状」，档位名（`hls_*` vs `vitis_aie_*` vs `vitis_hw_*`）是 flow 的指纹**。

#### 4.1.4 代码实践

**实践目标**：通过对比三个真实用例，建立「flow 决定字段骨架」的直觉，为 4.4 综合实践里手写一份做铺垫。

**操作步骤（源码阅读型）**：

1. 打开三个文件并排对比：`utils/L1/tests/stream_dup/description.json`（hls）、`dsp/L2/tests/aie/widget_real2complex/description.json`（aie）、`vision/L3/tests/resize_pipeline/YUV422_NPC8/description.json`（system）。
2. 在每个文件里定位 `flow` 字段与 `testinfo.targets` 数组。
3. 列一张表，记录三者各自的 targets 值与是否含 `topfunction`、`host`、`containers` 字段。

**需要观察的现象**：

- hls 用 `hls_csim` 这类大写档位名；aie 用 `vitis_aie_*`；system 用 `vitis_hw_*`。
- 只有 hls flow 有 `topfunction`；只有 system flow 有 `host`（主机程序编译段）与 `containers`（内核容器）。

**预期结果**：你会清晰看到 flow 与字段、档位名的对应关系，从而明白「看到 targets 就能反推 flow」。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `stream_dup` 的 `description.json` 里 `topfunction` 从 `"dut0"` 改成 `"dut1"`，csim 还能跑通吗？为什么？

**参考答案**：能跑通 csim，但综合的对象会变成 `dut1`。因为 csim 阶段 `vitis-run` 会按 `topfunction` 找顶层；`test.cpp` 里确实存在 `dut1`（[test.cpp:40-48](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/L1/tests/stream_dup/test.cpp#L40-L48)）。但注意 `argv` 仍是 `"0"`，而 `main` 里 `argv[1][0]=='0'` 调用的是 `test_dut0()`，它喂的是 `dut0` 的接口——若 `dut1` 接口与 `dut0` 不同，csim 的 testbench 与被综合的顶层会错位，行为不一致。

**练习 2**：`category` 取 `canary` 与 `full` 对 CI 调度有何实际区别？

**参考答案**：`canary`（金丝雀）是冒烟级用例，每次代码提交（PR/checkin）都跑，要求快；`full` 是全量用例，通常在每日（daily）或特定触发才跑，覆盖更重（如 `vitis_hw_build` 实际综合上板，耗时长）。CI 据此把用例分桶调度，平衡反馈速度与覆盖深度。

---

### 4.2 hls_config 模板生成

#### 4.2.1 概念说明

`description.json` 是给 CI 看的元数据；而 `v++` / `vitis-run` 工具真正吃进去的是一个 INI 风格的配置文件 `hls_config.cfg`（键值对，如 `syn.top=dut0`）。问题来了：这个 `.cfg` 里的内容大部分是固定的，但**有一部分必须随命令行动态变化**（比如 `vivado_syn` 用 `vivado.flow=syn`、其他档位用 `impl`），还有一部分依赖环境变量（如 `XF_PROJ_ROOT` 库根路径）。

手写两份 `.cfg` 容易出错，于是仓库采用了**「模板 + 运行时替换」**的标准做法：

- 写一份**模板** `hls_config.tmpl`，把可变位置写成 `${VAR}` 占位符。
- 在 `make` 时用一段**内嵌 Python** 读取模板，把 `${VAR}` 替换成同名**环境变量**的值，写出最终的 `hls_config.cfg`。

这是基础设施代码里很常见的「配置即代码 + 模板渲染」模式。

#### 4.2.2 核心流程

```
hls_config.tmpl  ──(string.Template + os.environ)──▶  hls_config.cfg
       │                                                  │
       │ 含 ${XF_PROJ_ROOT}、${VIVADO_FLOW}               │ 真实路径、syn/impl
       ▼                                                  ▼
   Makefile 导出环境变量                          v++ -c / vitis-run 读取
   (XF_PROJ_ROOT, VIVADO_FLOW 由 TARGET 决定)
```

1. Makefile 根据用户传入的 `TARGET` 计算出 `VIVADO_FLOW`（`vivado_syn` → `syn`，其余 → `impl`），并 `export`。
2. Makefile 已经算好并 `export` 了 `XF_PROJ_ROOT`（库根目录）。
3. `make` 触发 `$(CONFIG_FILE): $(CONFIG_TMPL)` 规则，执行内嵌 Python：`string.Template(t).substitute(**dict(os.environ))`。
4. 生成的 `hls_config.cfg` 里所有 `${...}` 都被填上了真实值。
5. `v++ -c --config hls_config.cfg` 或 `vitis-run --config hls_config.cfg` 消费它。

#### 4.2.3 源码精读

**模板本体** `hls_config.tmpl`——注意 `${...}` 是占位符，其余是固定字面量：

```ini
clock=2.5
flow_target=vivado
syn.file=test.cpp
syn.file_cflags=test.cpp,-I${XF_PROJ_ROOT}/L1/include
syn.top=dut0
tb.file=test.cpp
...
csim.argv=0
cosim.argv=0
vivado.flow=${VIVADO_FLOW}
vivado.rtl=verilog
```

参见 [utils/L1/tests/stream_dup/hls_config.tmpl:3-19](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/L1/tests/stream_dup/hls_config.tmpl#L3-L19)。两个占位符：

- `${XF_PROJ_ROOT}`（第 6、9 行）：库根路径，决定 `-I` 到哪里找 `stream_dup.hpp`。
- `${VIVADO_FLOW}`（第 17 行）：`syn` 或 `impl`，是唯一随 TARGET 变化的字段。

**两处对照**——`csim.argv=0` / `cosim.argv=0`（[第 11、13 行](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/L1/tests/stream_dup/hls_config.tmpl#L11-L13)）正是 4.1 里 `description.json` 中 `argv.hls_csim="0"` 的另一副面孔：同一信息在元数据与工具配置里各出现一次。

**Makefile 计算 `VIVADO_FLOW`**——这是模板里那个占位符的值来源：

```makefile
ifeq ($(TARGET), vivado_syn)
TARGET_REL = impl
export VIVADO_FLOW := syn
else
export VIVADO_FLOW := impl
ifeq ($(TARGET), vivado_impl)
TARGET_REL = impl
else
TARGET_REL = $(TARGET)
endif
endif
```

参见 [utils/L1/tests/stream_dup/Makefile:62-72](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/L1/tests/stream_dup/Makefile#L62-L72)。逻辑很清楚：只有 `vivado_syn` 把 `VIVADO_FLOW` 设成 `syn`，其余（含 `vivado_impl`）都是 `impl`。同时它还顺便算出 `TARGET_REL`（目标归一化名），后面分派用。

**内嵌 Python 渲染器**——整段精华：

```makefile
define CONFIG_GEN_PY
import os, string
with open('$(CONFIG_TMPL)', 'r') as fr:
    t = fr.read()
with open('$(CONFIG_FILE)', 'w') as f:
    f.write(string.Template(t).substitute(**dict(os.environ)))
endef
export CONFIG_GEN_PY
```

参见 [utils/L1/tests/stream_dup/Makefile:160-167](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/L1/tests/stream_dup/Makefile#L160-L167)。三句话：读模板 → 用 `string.Template` 把 `${VAR}` 按当前**全部环境变量** `os.environ` 替换 → 写出 `.cfg`。`define ... endef` 把这段 Python 存进一个 make 变量并 `export`，这样它能被管道喂给下文的 Python 解释器。

**调用渲染器并生成 `.cfg` 的规则**：

```makefile
TAPYTHON = $(shell find $(XILINX_VITIS)/tps/lnx64/ -maxdepth 1 -type d -name "python-3*" ... | head -n 1)
VITIS_PYTHON3 = LD_LIBRARY_PATH=$(TAPYTHON)/lib $(TAPYTHON)/bin/python3

$(CONFIG_FILE): $(CONFIG_TMPL)
	@echo "$${CONFIG_GEN_PY}" | (${VITIS_PYTHON3})
```

参见 [utils/L1/tests/stream_dup/Makefile:169-173](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/L1/tests/stream_dup/Makefile#L169-L173)。注意两个工程细节：

- 仓库**不依赖系统 Python**，而是去找 Vitis 自带的 `tps/lnx64/python-3.*`（`TAPYTHON`），并设好它的 `LD_LIBRARY_PATH`——保证在任何机器上行为一致。
- 触发条件是「`.cfg` 不存在或 `.tmpl` 更新了」(`$(CONFIG_FILE): $(CONFIG_TMPL)`)，于是每次 `make` 会在需要时自动重渲染。

**消费 `.cfg` 的两条分派**——渲染完就该跑了：

```makefile
all: check_vivado check_part data $(CONFIG_FILE)
ifneq ($(TARGET_REL), csim)
	v++ -c --mode hls --config $(CONFIG_FILE) --work_dir $(WORK_DIR) --part $(XPART)
endif

run: all
ifneq ($(TARGET_REL), csynth)
	@echo $(TARGET_REL)
	vitis-run --mode hls --config $(CONFIG_FILE) --$(TARGET_REL) --work_dir $(WORK_DIR) --part $(XPART)
endif
```

参见 [utils/L1/tests/stream_dup/Makefile:178-187](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/L1/tests/stream_dup/Makefile#L178-L187)。这正是 u2-l3 讲的分派机制在源码层的落地：

- `csim` 例外：`all` 里 `ifneq ($(TARGET_REL), csim)` 跳过 `v++ -c`，因为 csim 不综合；`run` 里 csim 走 `vitis-run --csim`。
- `csynth` 例外：`run` 里 `ifneq ($(TARGET_REL), csynth)` 跳过 `vitis-run`，因为 csynth 只需 `all` 里的 `v++ -c` 综合即可。
- 其余三档（`cosim`/`vivado_syn`/`vivado_impl`）：`v++ -c` 与 `vitis-run` 都执行。

> 旁注：同一目录下还有一份 `run_hls.tcl`（[utils/L1/tests/stream_dup/run_hls.tcl](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/L1/tests/stream_dup/run_hls.tcl)），是经典的 Vivado HLS Tcl 脚本（`open_project`/`add_files`/`set_top`/`csim`/`csynth`...），是 `.tmpl`+`.cfg` 这套「新流程」之前的「老流程」产物，可作对照阅读，理解演进。

#### 4.2.4 代码实践

**实践目标**：亲眼看到「模板 → 替换 → 配置」这一步，验证 `${VIVADO_FLOW}` 随 TARGET 变化。

**操作步骤**（不需要 Vitis 工具链，只看渲染这一步）：

1. 进入 `utils/L1/tests/stream_dup` 目录。
2. 设两个环境变量（模拟 Makefile 的 export）：
   ```bash
   export XF_PROJ_ROOT=/home/runner/work/.../utils   # 任意真实路径
   export VIVADO_FLOW=impl
   ```
3. 手工执行 Makefile 里那段 Python（把 `${VAR}` 替换）：
   ```bash
   python3 -c "import os,string; \
     t=open('hls_config.tmpl').read(); \
     open('/tmp/cfg.out','w').write(string.Template(t).substitute(**dict(os.environ)))"
   cat /tmp/cfg.out
   ```
4. 把 `VIVADO_FLOW` 改成 `syn` 再跑一次，对比 `/tmp/cfg.out` 里 `vivado.flow=` 这一行。

**需要观察的现象**：

- 第一次 `vivado.flow=impl`、`syn.file_cflags=...,-I/home/runner/.../utils/L1/include`——占位符都被填上了真实值。
- 第二次 `vivado.flow=syn`——仅这一行变化。

**预期结果**：你用零 Vitis 依赖复现了 Makefile 的渲染逻辑，确认 `.cfg` 是「模板 + 环境变量」的纯函数式输出。

> 若环境无 `python3`，可跳过执行，直接对照 `hls_config.tmpl` 与 Makefile 第 160-167 行理解。本步骤只验证模板渲染，与 Vitis 无关。

#### 4.2.5 小练习与答案

**练习 1**：为什么 Makefile 要去找 Vitis 自带的 `python-3.*` 而不直接用 `python3`？

**参考答案**：为了可复现与零依赖。系统 Python 版本、模块各异，可能没有标准库里的 `string` 行为差异或路径问题；用 Vitis TPS（Third-Party Software）自带的 Python，保证在任何装了 Vitis 的机器上渲染结果一致。这也是为何 `VITIS_PYTHON3` 还特意设了它的 `LD_LIBRARY_PATH`。

**练习 2**：如果模板里写了一个 `${FOO}`，但环境里没有 `FOO` 变量，会发生什么？

**参考答案**：`string.Template(...).substitute(...)` 在找不到占位符对应键时会抛 `KeyError`，整个 `make` 会失败。这是 `substitute` 的严格行为（相对地，`safe_substitute` 会保留原占位符不报错）。仓库选 `substitute` 是有意的——让缺失变量立刻暴露而不是静默生成错误配置。

---

### 4.3 run_test.py 批量驱动（blas 库）

#### 4.3.1 概念说明

`description.json` + `hls_config.tmpl` 是**整个仓库主流的、面向 CI** 的测试描述方式。但 blas 库还有一套**更古老、更「脚本化」**的测试总线 `run_test.py`，它解决的问题不同：

> GEMM 这类内核有大量参数组合（数据类型、矩阵规模、并行度 `parEntries`...），手工为每个组合写一个用例目录不现实。能不能用一份「参数 profile」描述一个组合，让脚本自动**生成 + 编译 + 跑 + 汇总**？

`run_test.py` 就是这个批量驱动器：吃进一组 profile（或算子名），并发跑完所有组合，把每个的通过/失败写进 `statistics.rpt`，最后用进程退出码（0/1）告诉 CI 整体成败。

它和主流 `description.json` 体系的关系是：**blas 的 L1 用例既被 description.json 体系收编（参与全仓库 CI），也保留 run_test.py 这条「参数笛卡尔积」的快速本地/批量通路**。

#### 4.3.2 核心流程

```
profile.json (或 --operator opName)
        │
        ▼
   RunTest(profile, args)         # 解析 profile、按 profile 调 blas_gen.mk 生成/编译/仿真
        │   每个组合一个 RunTest 对象
        ▼
   process(rt, statList)          # 串行或并发执行
        │   ├─ rt.parseProfile()
        │   ├─ rt.run()
        │   └─ statList.append({Op, No.csim, No.cosim, Status, Profile})
        ▼
   statistics.rpt                 # 表格汇总
        │
        ▼
   sys.exit(0 或 1)               # 有任一 Failed → exit 1
```

并发由 `concurrent.futures.ThreadPoolExecutor` 提供（`--parallel N`），多个 profile 同时跑。

#### 4.3.3 源码精读

`run_test.py` 的骨架可分三段：单组合处理 `process()`、汇总与退出 `main()`、命令行接口。

**单组合处理 `process()`**——跑一个 profile，捕获三类自定义异常，用 `statList` 记录结果：

```python
def process(rt, statList, dictLock=threading.Lock(), makeLock=threading.Lock()):
    passed = False
    try:
        rt.parseProfile()
        with rt.opLock:
            print("Starting to test %s." % (rt.op.name))
            rt.run()
            print("All %d tests for %s are passed." % (rt.numSim, rt.op.name))
            passed = True
            ...
            with dictLock:
                statList.append({'Op Name': rt.op.name, ..., 'Status': 'Passed', ...})
    except OP_ERROR as err:   ...
    except BLAS_ERROR as err: ...
    except HLS_ERROR as err:  ...
    except Exception as err:  ...
    finally:
        if not passed:
            with dictLock:
                statList.append({..., 'Status': 'Failed', ...})
```

参见 [blas/L1/tests/run_test.py:43-93](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/blas/L1/tests/run_test.py#L43-L93)。要点：

- `dictLock`（线程锁）保护共享的 `statList`——因为 `process` 会在多线程下并发执行。
- 三类异常对应三个抽象层：`OP_ERROR`（算子层）、`BLAS_ERROR`（BLAS 状态码层，呼应 u8-l1 讲的 `xfblasStatus_t`）、`HLS_ERROR`（HLS 工具层，带日志文件路径）。

**汇总与退出 `main()`**——这是 CI 判定成败的关键：

```python
def main(profileList, args):
    statList = list()
    ...
    try:
        if args.parallel == 1:
            for arg in argList:
                process(arg, statList)
        else:
            with concurrent.futures.ThreadPoolExecutor(max_workers=args.parallel) as executor:
                for arg in argList:
                    executor.submit(process, arg, statList)
    finally:
        statPath = os.path.join(os.getcwd(), "statistics.rpt")
        list2File(statList, statPath)
        failures = [k for k in statList if k['Status'] == 'Failed']
        if len(failures) != 0:
            sys.exit(1)
        else:
            sys.exit(0)
```

参见 [blas/L1/tests/run_test.py:96-130](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/blas/L1/tests/run_test.py#L96-L130)。三个工程要点：

- **并发**：`--parallel 1` 串行，否则用线程池（[第 113-119 行](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/blas/L1/tests/run_test.py#L113-L119)）。
- **报告**：结果落盘成 `statistics.rpt`（或带 `--id` 的 `statistics_<id>.rpt`），供人/CI 查看（[第 121-125 行](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/blas/L1/tests/run_test.py#L121-L125)）。
- **退出码**：只要有任一 `Failed`，`sys.exit(1)`（[第 126-130 行](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/blas/L1/tests/run_test.py#L126-L130)）。这是 CI 能「机器可判」的契约——shell 用 `$?` 即知成败。

**命令行接口**——互斥的两种输入方式：

```python
profileGroup = parser.add_mutually_exclusive_group(required=True)
profileGroup.add_argument('--profile', nargs='*', metavar='profile.json', help='list of path to profile files')
profileGroup.add_argument('--operator', nargs='*', metavar='opName', help='list of test dirs in ./hw')
```

参见 [blas/L1/tests/run_test.py:145-155](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/blas/L1/tests/run_test.py#L145-L155)。要么直接给一组 profile 文件路径，要么给算子名（脚本再拼成 `./hw/<op>/profile.json`，见 [第 192-195 行](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/blas/L1/tests/run_test.py#L192-L195)）。其余开关 `--csim/--csynth/--cosim/--benchmark` 与 `--xpart` 控制档位与目标器件。

**配套的 `blas_gen.mk`**——`RunTest` 内部会调用它来按参数生成可执行文件。它把 BLAS 参数（`BLAS_dataType`、`BLAS_parEntries`、`BLAS_memWidthBytes` 等）以 `-D` 宏喂给 C++，呼应 u8-l1 讲的「按 profile 笛卡尔积批量生成」。其头部声明：

```makefile
MK_PATH := $(abspath $(lastword $(MAKEFILE_LIST)))
...
help::
	@echo "  make blas_gen_bin.<exe/so> BLAS_dataType=<int>, BLAS_resDataType=<int>"
```

参见 [blas/L1/tests/blas_gen.mk:30-43](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/blas/L1/tests/blas_gen.mk#L30-L43)，参数定义见 [blas/L1/tests/blas_gen.mk:57-76](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/blas/L1/tests/blas_gen.mk#L57-L76)。`run_test.py` 是「调度器」，`blas_gen.mk` 是「生成器」，两者配合完成批量。

#### 4.3.4 代码实践

**实践目标**：用 `--help` 摸清 `run_test.py` 的接口，理解它如何被 CI 调用（源码阅读型 + 可选执行）。

**操作步骤**：

1. 阅读 [blas/L1/tests/README.md](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/blas/L1/tests/README.md)（若环境允许），看官方示例命令。
2. 在 `blas/L1/tests` 目录执行：
   ```bash
   python3 run_test.py --help
   ```
3. 观察输出里 `--profile` 与 `--operator` 是 `required` 且互斥，`--parallel` 默认 1，`--xpart` 默认 `xcvu9p-flgb2104-2-i`。
4. 列出 `blas/L1/tests/hw` 下有哪些算子目录（`ls hw`），它们就是 `--operator` 可接受的算子名。

**需要观察的现象**：

- `--help` 显示两组互斥入参，对应「直接给 profile」或「给算子名让脚本自己找 profile」。
- `hw/` 下每个算子目录里有一个 `profile.json`。

**预期结果**：你能写出一条形如 `python3 run_test.py --operator gemm --csim --parallel 2` 的命令并解释每个参数。是否真正执行成功取决于是否已 source Vitis 环境（未 source 则会报 `XILINX_VIVADO` 缺失，属正常现象）。

> 待本地验证：`run_test.py` 的实际运行需要 Vitis 工具链与生成的 `blas_gen_bin`，本步骤主要目的是读懂接口契约。

#### 4.3.5 小练习与答案

**练习 1**：`run_test.py` 用进程退出码（0/1）判定 CI 成败。`description.json` 体系（hls flow）靠什么判定单个用例的 PASS/FAIL？

**参考答案**：HLS 用例靠 testbench `main` 的返回值（`return nerr`，如 stream_dup 的 [test.cpp:282](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/L1/tests/stream_dup/test.cpp#L282)）——非零即失败；同时 u2-l2 讲过，对非 bit 精确的浮点/定点内核，PASS/FAIL 由 testbench 内部的误差阈值判定，误差超阈值就累加 `nerr`。两者最终都归结为「进程退出码」，CI 据此机器判定。

**练习 2**：为什么 `process()` 里要加 `dictLock` 锁？

**参考答案**：因为 `--parallel > 1` 时多个 `process()` 在线程池里并发跑，它们共同往同一个 `statList` 列表 `append`；Python 列表的 `append` 虽由 GIL 保护，但「读-改-写」复合操作仍可能交错，加锁保证每条记录原子写入、`statistics.rpt` 不丢条目或错乱。

---

### 4.4 Jenkinsfile CI

#### 4.4.1 概念说明

前面三个模块讲的都是「单个用例怎么描述、怎么生成配置、怎么批量跑」。现在拉到最高层：**整个仓库的 CI 入口长什么样？**

答案出乎意料地短——顶层 `Jenkinsfile` 只有**两行**。这不是偷懒，而是一种刻意的架构选择，叫**「薄入口 + 厚共享流水线库」**：

- **薄入口**：每个仓库（这里是 Vitis_Libraries monorepo）只放一个极简的 `Jenkinsfile`，声明「我用哪条共享流水线、我叫什么名字」。
- **厚共享库**：真正复杂的 CI 逻辑（扫描所有库、收集 description.json、按 flow 分流、调度平台、收集报告）都封装在一个叫 `pipeline-library` 的 **Jenkins Shared Library** 里，由 `FullVitisLibPipeline(...)` 这个函数实现。

好处是：CI 逻辑升级时只改共享库一处，所有 AMD 内部 Vitis 仓库自动跟进，不必逐仓库改 `Jenkinsfile`。

> 说明：`pipeline-library` 是 AMD 内部资产，**不在本开源仓库内**，我们只能看到对它的「调用点」，看不到实现。本模块讲的是「这个调用点的契约与意图」，实现细节标注为仓库外。

#### 4.4.2 核心流程

```
Jenkinsfile (2 行)
   │
   │ @Library('pipeline-library')_      ← 加载共享库
   │ FullVitisLibPipeline(libname: 'Vitis_Libraries')
   ▼
pipeline-library::FullVitisLibPipeline
   │
   ├─ 扫描 Vitis_Libraries 下 9 个库的 tests 目录
   ├─ 读取每个 description.json（flow/平台/targets/category）
   ├─ 按 category (canary/full) 与触发事件 (PR/daily) 分桶
   ├─ 按 platform_allowlist 给每个用例匹配可用平台
   ├─ 为每个 (用例, 档位) 分配 max_memory_MB / max_time_min
   ├─ 渲染 hls_config.cfg（hls flow）/ 调 aiecompiler（aie flow）/ v++（system flow）
   ├─ 运行、收集 PASS/FAIL
   └─ 汇总报告、回写 Jenkins 状态
```

#### 4.4.3 源码精读

整个文件就两句：

```groovy
@Library('pipeline-library')_
FullVitisLibPipeline(libname: 'Vitis_Libraries')
```

参见 [Jenkinsfile:1-2](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/Jenkinsfile#L1-L2)。逐字解读：

- **`@Library('pipeline-library')_`**：Jenkins 的 Shared Library 加载语法。`@Library('名字')_` 里的下划线表示「加载该库但不确定导入哪个类/变量」（用通配方式拉入）。`pipeline-library` 是在 Jenkins 控制台预先配置好的共享库名字。
- **`FullVitisLibPipeline(libname: 'Vitis_Libraries')`**：调用共享库里的一个全局函数（Groovy 脚本里定义的方法），把当前仓库的库名 `'Vitis_Libraries'` 传进去。共享库据此知道要扫描哪个目录树。

这正好呼应 u1-l2 讲的「顶层 Jenkinsfile 仅两行，借助共享流水线库统一驱动全部库 CI」。把本讲前三模块与它串起来：

| 层 | 由谁负责 | 内容 |
|----|---------|------|
| 用例层 | 各用例作者 | 写 `description.json`、`hls_config.tmpl`、`test.cpp` |
| 库内批量层（blas） | `run_test.py` + `blas_gen.mk` | 参数笛卡尔积的本地批量通路 |
| 全仓库 CI 层 | `Jenkinsfile` → `pipeline-library` | 扫描全部用例、按 flow/平台/category 调度 |

也就是说：**用例作者只需维护 `description.json`（声明身份），其余的扫描、调度、资源分配全交给共享库**。这是「约定优于配置」在 CI 层的体现——`description.json` 的字段名（`flow`、`platform_allowlist`、`testinfo.targets`、`category`...）就是用例作者与共享库之间的契约。

#### 4.4.4 代码实践

**实践目标**：把「两行 Jenkinsfile」与本讲前三模块的元数据字段打通，验证「字段即契约」。

**操作步骤（源码阅读型）**：

1. 打开 [Jenkinsfile](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/Jenkinsfile)，确认它确实只有两行、且调用了 `FullVitisLibPipeline`。
2. 回到 stream_dup 的 `description.json`，逐个标记下列字段会被共享库在哪个阶段用到：
   - `flow` → 决定走哪条流水（hls/aie/system）
   - `platform_allowlist` → 决定在哪些平台节点跑
   - `testinfo.targets` → 决定跑哪些档位
   - `testinfo.jobs[0].max_time_min` → 决定每档超时
   - `category` → 决定是 PR 级还是 daily 级
3. 推断：如果想让一个新用例「每次 PR 都在 vck190 上跑 csim」，你需要填哪些字段？

**需要观察的现象**：`Jenkinsfile` 本身不含任何用例逻辑——所有调度信息都来自各用例自己的 `description.json`。

**预期结果**：你能解释「为什么 Jenkinsfile 只有两行却够用」——因为复杂性被下放到了「每个用例一份 description.json」+「一份共享 pipeline-library」。

#### 4.4.5 小练习与答案

**练习 1**：假如 AMD 升级了 CI 调度逻辑（比如新增一种 `category`），需要改本仓库的 `Jenkinsfile` 吗？

**参考答案**：不需要。`Jenkinsfile` 只是「调用共享库」，真正的调度逻辑在 `pipeline-library` 里。升级共享库即可让所有用上它的仓库（含本仓库）自动获得新行为。这正是「薄入口 + 厚共享库」的核心收益。本仓库只需保证 `description.json` 字段符合契约。

**练习 2**：为什么 `description.json` 里要有 `platform_allowlist` 和 `platform_blocklist` 两套？

**参考答案**：白名单是「显式枚举允许的平台」（如 AIE 用例只能跑 vck190/vek280），适合用例明确知道自己支持哪几个平台；黑名单是「排除个别平台」（如某个 system 用例在 u280 上有问题但其余平台都行），适合「大部分都行，只有少数不行」的场景。两者叠加给出灵活的平台过滤策略，让 CI 不把机时浪费在必然失败或无意义的组合上。

---

## 5. 综合实践

**任务**：为一个假想的新 L1 用例 `stream_double`（把输入流每个元素乘 2 输出）手写一份合规的 `description.json`，并说明它会如何被 CI 识别与调度。这个任务把本讲的四个最小模块（元数据、模板、批量驱动、CI）串起来。

**操作步骤**：

1. 假设你的用例目录是 `utils/L1/tests/stream_double/`，含 `test.cpp`（顶层函数 `extern "C" void dut0(...)`）、`Makefile`（拷贝自 stream_dup）、`hls_config.tmpl`（拷贝自 stream_dup 并改 `syn.top=dut0`）。
2. 在该目录下新建 `description.json`，要求：
   - `"flow": "hls"`
   - `"platform_allowlist": ["vck190"]`
   - `"clock": "3.33"`（约 300MHz）
   - `"topfunction": "dut0"`
   - `top` 与 `testbench` 的 `source` 都填 `test.cpp`，`cflags` 填 `-I${XF_PROJ_ROOT}/L1/include`
   - `testbench.argv` 填 `{"hls_csim": "0", "hls_cosim": "0"}`
   - `testinfo.targets` 先只放 `["hls_csim", "hls_csynth"]`（冒烟级）
   - `testinfo.jobs[0]` 给 `hls_csim` 与 `hls_csynth` 各设 `max_time_min: 60`、`max_memory_MB: 10240`
   - `"category": "canary"`
3. 写完后回答三个问题：
   - CI 扫到这个文件后，会把它分到哪条流水？→ hls 流水（由 `flow`）。
   - 它会在哪些触发事件下跑？→ 每次 PR/checkin 都跑（由 `category: canary`）。
   - 它跑哪些档位、每档多久超时？→ csim 与 csynth，各 60 分钟（由 `testinfo.targets` 与 `max_time_min`）。

**参考答案（一份合规的 description.json 骨架）**：

```json
{
    "name": "Xilinx Stream Double HLS Test",
    "description": "multiply each input stream element by 2",
    "flow": "hls",
    "platform_allowlist": ["vck190"],
    "platform_blocklist": [],
    "project": "test",
    "solution": "solution1",
    "clock": "3.33",
    "topfunction": "dut0",
    "top": {
        "source": ["test.cpp"],
        "cflags": "-I${XF_PROJ_ROOT}/L1/include"
    },
    "testbench": {
        "source": ["test.cpp"],
        "cflags": "-I${XF_PROJ_ROOT}/L1/include",
        "argv": { "hls_csim": "0", "hls_cosim": "0" }
    },
    "testinfo": {
        "disable": false,
        "jobs": [{
            "index": 0, "dependency": [], "env": "", "cmd": "",
            "max_memory_MB": { "hls_csim": 10240, "hls_csynth": 10240 },
            "max_time_min":  { "hls_csim": 60,    "hls_csynth": 60 }
        }],
        "targets": ["hls_csim", "hls_csynth"],
        "category": "canary"
    },
    "gui": false
}
```

**自检要点**：

- `topfunction` 必须与 `test.cpp` 里的 `extern "C" void dut0` 完全一致（4.1 讲的契约）。
- `argv.hls_csim="0"` 必须与 `test.cpp` 的 `main` 里 `argv[1][0]=='0'` 分支匹配（4.1.3）。
- `hls_config.tmpl` 的 `syn.top=` 也要同步写成 `dut0`，且 `clock=` 与 `description.json` 的 `clock` 保持一致（4.2 讲的双处冗余）。
- 把 `category` 设为 `canary`、targets 只列轻量档，保证它作为冒烟用例不拖慢 PR 反馈（4.4 讲的调度策略）。

> 这是「设计型」实践，不要求真正提交到 CI。目的是让你从「读用例」升级到「会按契约造用例」。下一讲 u14-l2 会带你真正从零写一个完整内核（含本讲这份 description.json）。

## 6. 本讲小结

- `description.json` 是每个用例目录的身份证：`flow` 决定走 hls/aie/system 哪条流水，`platform_allowlist`/`topfunction`/`clock`/`testbench.argv` 描述身份，`testinfo.targets`/`jobs`/`category` 描述调度——**数据与代码分离**，改调度不动源码。
- 不同 `flow` 的 `description.json` 骨架不同，**档位名是 flow 的指纹**（`hls_*` vs `vitis_aie_*` vs `vitis_hw_*`）。
- `hls_config.cfg` 由 `hls_config.tmpl` 经 Makefile 内嵌的 `string.Template(...).substitute(**os.environ)` 渲染而成；唯一随 TARGET 变化的占位符是 `${VIVADO_FLOW}`（`vivado_syn`→`syn`，其余→`impl`）；渲染用 Vitis 自带 Python 以保证可复现。
- Makefile 的 `all`/`run` 两条规则用 `ifneq($(TARGET_REL),...)` 把五个 TARGET 分派给 `v++ -c`（综合）与 `vitis-run`（仿真），csim 跳过综合、csynth 跳过仿真。
- blas 的 `run_test.py` 是另一条「参数笛卡尔积」批量通路：`process()` 跑单组合、`main()` 用线程池并发并落 `statistics.rpt`，**用 `sys.exit(0/1)` 给 CI 一个机器可判的契约**。
- 顶层 `Jenkinsfile` 只有「加载共享库 + 调用 `FullVitisLibPipeline`」两行，体现了**薄入口 + 厚共享库**架构；CI 复杂性下放到「每用例一份 description.json」+「一份共享 pipeline-library」，`description.json` 的字段名就是用例作者与 CI 之间的契约。

## 7. 下一步学习建议

- **u14-l2 从零编写自己的 L1 内核**：本讲综合实践里那份 `description.json` 会在 u14-l2 真正配套上 `test.cpp`/`Makefile`/`hls_config.tmpl`，组成一个可跑通 csim/csynth 的完整用例——建议紧接着做。
- **u14-l3 基准评测与对标 CPU/参考**：本讲只讲了「用例怎么 PASS/FAIL」，u14-l3 讲「怎么量化性能并与参考模型比对误差」，是从「功能正确」走向「性能达标」的下一级。
- **延伸阅读源码**：想看更多 flow 的 `description.json` 长法，可浏览 `dsp/L2/tests/aie/*/description.json`（aie flow）与 `vision/L3/tests/*/description.json`（system flow）；想理解 Makefile 的平台搜索逻辑可重读 [utils/L1/tests/stream_dup/Makefile:87-158](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/L1/tests/stream_dup/Makefile#L87-L158) 的 `check_part` 段（u2-l1 已讲其三级兜底）。

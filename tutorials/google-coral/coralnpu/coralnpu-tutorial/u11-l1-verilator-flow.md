# Verilator 仿真流程深入

> 本讲是第 11 单元（验证流程与 FPGA 构建）的第一讲，承接 `u2-l3`。`u2-l3` 教你「怎么用」Verilator 仿真器跑通一个程序；本讲带你「看懂」这套仿真器在 Bazel 里是怎么被定义、搭建和封装的。

## 1. 本讲目标

学完本讲，你应当能够：

- 说清楚一条 Verilator 仿真模型是怎样从 Chisel 源码一路变成 C++ 类（`VCoreMiniAxi`）的，并指出这条链上每一站的文件。
- 读懂 `tests/verilator_sim/BUILD`，解释 `template_rule` 如何用一份模板批量生成 8 个 testbench 库和 8 个仿真器二进制。
- 区分 CoralNPU 的两条 Verilator 入口：SystemC testbench（`core_mini_axi_sim`，命令行主力）与轻量 C++ 库（`hw_sim/`，供 npusim/Python 调用），并理解 `Clock::Step` 如何按时钟边沿搬运 AXI 数据。
- 解释 DPI backdoor SRAM 与 mailbox 两种仿真支撑机制：前者绕过总线高速加载 ELF，后者捕获内核写出的「信箱消息」。
- 诚实地修正一个认知偏差：`rules/verilog.bzl` 并不定义 Verilator 仿真目标（它只做 Verilog 文件打包），真正的模型生成规则在 `rules/chisel.bzl`。

## 2. 前置知识

本讲假设你已经读过 `u2-l3`，知道：

- **Verilator** 是一个开源逻辑仿真器：它把 SystemVerilog RTL 编译成 C++ 类（如 `VCoreMiniAxi`），让你可以在 C++/SystemC 程序里像操作普通对象一样驱动硬件模型——给引脚赋值、调用 `eval()` 推进时间、读回输出。它没有真实芯片的时序那么精确，但编译快、跑得快，是 RTL 验证的主力。
- CoralNPU 的硬件用 **Chisel**（Scala）写，Chisel 先综合成 **SystemVerilog**，再喂给 Verilator。
- 仿真器跑一个程序的标准启动序列是：加载 ELF → 开钟 → 释放复位 → 等 `io_halted`。这些 CSR 地址（`0x30000` RESET_CONTROL、`0x30004` PC_START）在 `u3-l5` 讲过。

还需了解两个 Bazel 概念：

- **目标（target）与规则（rule）**：BUILD 文件里每个 `cc_binary(name=...)`、`cc_library(name=...)` 都是一个「目标」；`cc_binary`、`cc_library` 是 Bazel 内置「规则」。CoralNPU 还用 Starlark（Bazel 的配置语言，语法接近 Python）自定义了很多规则，如 `chisel_cc_library`。
- **DPI（Direct Programming Interface）**：SystemVerilog 与 C 互调的标准接口。RTL 里可以调用一个用 `extern "C"` 声明的 C 函数，C 函数也能读写 RTL 内部信号。Verilator 完整支持 DPI，本讲的 backdoor SRAM 就是靠它实现的。

## 3. 本讲源码地图

本讲涉及的关键文件按职责分为四组：

| 文件 | 作用 |
| --- | --- |
| `rules/chisel.bzl` | 定义 `chisel_cc_library`：Chisel → SystemVerilog → Verilator C++ 模型的核心规则 |
| `rules/verilog.bzl` | 提供 `verilog_zip_bundle` 等纯 Verilog 文件打包规则（不生成仿真目标） |
| `rules/utils.bzl` | 提供 `template_rule` 宏：用一份模板批量实例化多个 Bazel 目标 |
| `rules/default.vlt.tpl` | Verilator 信号可见性配置模板（供 cocotb 路径使用） |
| `hdl/chisel/src/coralnpu/BUILD` | 用 `chisel_cc_library` 定义 `core_mini_axi_cc_library` 等模型目标 |
| `tests/verilator_sim/BUILD` | 用 `template_rule` 定义 8 个 testbench 库与 8 个仿真器二进制 |
| `tests/verilator_sim/coralnpu/core_mini_axi_sim.cc` | SystemC 仿真器入口（`sc_main`），命令行主力 |
| `tests/verilator_sim/coralnpu/core_mini_axi_tb.h` / `.cc` | SystemC testbench：TLM↔AXI 桥、ELF 加载、复位序列 |
| `hw_sim/core_mini_axi_wrapper.h` | 轻量 C++ 封装：把 Verilator 模型引脚接到 AXI 驱动器 |
| `hw_sim/hw_primitives.h` / `.cc` | `Clock`、`AxiSlaveWriteDriver` 等仿真原语 |
| `hw_sim/core_mini_axi_simulator.cc` / `coralnpu_simulator.h` | 三层库封装：抽象接口 → 实现 → wrapper |
| `hw_sim/mailbox.h` | `CoralNPUMailbox`：16 字节信箱数据结构 |
| `hdl/verilog/sram_backdoor.cc` / `.h` | DPI backdoor SRAM：绕过总线直接读写 SRAM |
| `doc/simulation.md` | 仿真文档（注意：只讲了 VCS，反衬 Verilator 是默认路径） |

## 4. 核心概念与源码讲解

本讲拆成五个最小模块：

1. **Chisel → RTL → Verilator C++ 模型**：仿真模型是怎么生成的。
2. **testbench 仿真目标的批量定义**：8 个变体从哪来。
3. **SystemC testbench 与仿真器入口**：命令行主力怎么跑。
4. **轻量 C++ 库封装与时钟驱动**：库式路径如何被 npusim 复用。
5. **仿真支撑机制：DPI backdoor SRAM 与 mailbox**：高速加载与消息捕获。

### 4.1 Verilator 仿真模型是怎么生成的：Chisel → RTL → C++

#### 4.1.1 概念说明

一切仿真的起点是一个能被 C++ 调用的硬件模型。CoralNPU 的硬件用 Chisel 写，而 Verilator 只吃 SystemVerilog。所以必须有一条流水线把 Scala 源码翻译成 Verilator 能编译的 C++ 类：

```
Chisel (Scala)  ──firtool──▶  SystemVerilog (.sv)  ──verilator──▶  C++ 类 (VCoreMiniAxi)
```

这条流水线在 Bazel 里被封装成一个自定义规则 `chisel_cc_library`。理解它的关键在于：它**一次生成两份** Verilator 产物——一份带 SystemC（给 SystemC testbench 用），一份纯 C++（给轻量库用）。这两份产物对应本讲后面要讲的两条仿真入口。

这里要先纠正一个 spec / 直觉上的偏差：**`rules/verilog.bzl` 并不定义 Verilator 仿真目标**。它只提供 `collect_verilog_files`（收集 Verilog 文件）和 `verilog_zip_bundle`（把 Verilog 打成 zip，供 VCS / FPGA / lint 等下游复用）。真正调用 Verilator 编译器的，是 `rules/chisel.bzl` 里的 `chisel_cc_library`。

#### 4.1.2 核心流程

`chisel_cc_library` 内部分三步：

1. **生成 SystemVerilog**：用 `chisel_binary` 跑一个 Scala「发射器」（如 `coralnpu.EmitCore`），经 `firtool` 把 Chisel 编译成 `CoreMiniAxi.sv`。
2. **打包成 verilog_library**：用 rules_hdl 的 `verilog_library` 把 `.sv` 注册成一个可被传递依赖的 Verilog 图。
3. **两次调用 `verilator_cc_library`**：一次 `systemc=True`（产物名 = 目标名），一次 `systemc=False`（产物名加 `_cc` 后缀）。两次都指向同一份 RTL，只是 Verilator 的代码生成模式不同。

> 小贴士：`verilator_cc_library` 来自 `@rules_hdl`（一个开源 HDL Bazel 规则集），不是 CoralNPU 自己写的。CoralNPU 的贡献是用 `chisel_cc_library` 把「Chisel 综合 + Verilator 编译」串成一条无缝流水线。

#### 4.1.3 源码精读

先看 `chisel_cc_library` 的两次 `verilator_cc_library` 调用——这是一切 Verilator 模型的真正出生地：

[chisel.bzl:170-191](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/rules/chisel.bzl#L170-L191) 给出两份产物：

```python
# 大多数场景是 SystemC，所以把「不带后缀的名字」给 SystemC 版
verilator_cc_library(
    name = "{}".format(name),          # → core_mini_axi_cc_library
    module = ":{}_verilog".format(name),
    module_top = module_name,           # → CoreMiniAxi（RTL 顶层模块名）
    vopts = vopts + ["--pins-bv", "2"],
    systemc = True,
)
# 纯 C++ Verilator 输出，加 _cc 后缀以区别于 SystemC
verilator_cc_library(
    name = "{}_cc".format(name),        # → core_mini_axi_cc_library_cc
    module = ":{}_verilog".format(name),
    module_top = module_name,
    vopts = vopts + ["--pins-bv", "2"],
    systemc = False,
)
```

`--pins-bv 2` 告诉 Verilator 把宽信号生成成 `sc_bv`/位向量类型（而非拆成一堆单 bit），方便整段搬运。`module_top` 是 RTL 顶层模块名，Verilator 据此给 C++ 类命名（`CoreMiniAxi` → `VCoreMiniAxi`）。

再看 `core_mini_axi_cc_library` 这个具体目标是怎么用 Chisel 配置参数生成的：

[coralnpu/BUILD:531-574](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/BUILD#L531-L574) 用 `template_rule`（4.2 节细讲）把多个模型变体共用一份 `chisel_cc_library`：

```python
template_rule(
    chisel_cc_library,
    {
        "core_mini_axi_cc_library": {
            "extra_outs": ["VCoreMiniAxi_parameters.h", "CoreMiniAxi.zip"],
            "gen_flags": [
                "--enableFetchL0=False", "--fetchDataBits=128",
                "--lsuDataBits=128", "--enableFloat=True",
                "--enableZfbfmin=True", "--moduleName=CoreMini",
                "--useAxi", "--exposeDebugPorts=True",
            ],
            "module_name": "CoreMiniAxi",
            "verilog_file_path": "CoreMiniAxi.sv",
        },
        ...
    },
    chisel_lib = ":coralnpu",
    emit_class = "coralnpu.EmitCore",
    vopts = CORE_MINI_AXI_VOPTS,
)
```

`gen_flags` 就是传给 Chisel 发射器的配置旋钮——它们和你在前几讲读到的 `Parameters`/`SoCChiselConfig` 开关一一对应（`enableFetchL0=False`、`enableFloat=True`、`exposeDebugPorts=True` 等）。`module_name=CoreMiniAxi` 决定生成的 RTL 顶层叫什么，进而决定 Verilator C++ 类叫 `VCoreMiniAxi`。

`CORE_MINI_AXI_VOPTS` 是 Verilator 编译选项，主要是屏蔽 fpnew（浮点 IP）产生的一堆无害告警：

[coralnpu/BUILD:519-529](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/BUILD#L519-L529)：

```python
CORE_MINI_AXI_VOPTS = [
    "-DUSE_GENERIC",
    "-Wno-UNOPTFLAT", "-Wno-ASCRANGE", "-Wno-WIDTHEXPAND",
    "-Wno-WIDTHTRUNC", "-Wno-UNSIGNED", "-Wno-BLKANDNBLK", "-Wno-BLKSEQ",
]
```

最后看 `rules/verilog.bzl` 的真实角色——它只在 RTL 已生成之后做打包。`verilog_zip_bundle` 把 Chisel 生成的 `.sv` 收集成 zip，供 VCS、FPGA 综合等下游使用：

[coralnpu/BUILD:767-770](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/BUILD#L767-L770)：

```python
verilog_zip_bundle(
    name = "core_mini_axi_bundle",
    lib = ":core_mini_axi_cc_library_verilog",
)
```

而 [verilog.bzl:51-84](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/rules/verilog.bzl#L51-L84) 的实现只是调 `zipper` 把文件打成一个 `.zip`——与 Verilator 仿真无关。

> 关于 `default.vlt.tpl`：这是一个 Verilator 配置文件模板（`verilator_config` 指令），声明 `io_*`、`clock`、`reset` 等信号为 `public`，让 Verilator 把它们的 C++ 访问接口暴露出来。但它属于 **cocotb 路径**：被 [coco_tb.bzl:135-141](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/rules/coco_tb.bzl#L135-L141) 用 `expand_template` 替换 `{HDL_TOPLEVEL}` 占位符后生成 `.vlt` 文件，喂给 cocotb 的 Verilator 调用（见 `u11-l3`）。SystemC testbench 路径不需要它，因为 `verilator_cc_library` 已经自动处理了引脚暴露。

#### 4.1.4 代码实践

**目标**：亲手追踪一次「Chisel 配置 → Verilator C++ 类名」的映射。

1. 打开 [hdl/chisel/src/coralnpu/BUILD:531-574](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/BUILD#L531-L574)，找到 `core_mini_axi_cc_library`。
2. 记下它的 `module_name`（`CoreMiniAxi`）和 `gen_flags` 里的开关。
3. 执行（构建模型本身，验证流水线能跑通）：

   ```bash
   bazel build //hdl/chisel/src/coralnpu:core_mini_axi_cc_library
   ```

4. 构建产物里会生成 `VCoreMiniAxi.h`（SystemC 版）与 `VCoreMiniAxi_parameters.h`（参数头）。在 `bazel-out` 下找 `VCoreMiniAxi.h`：

   ```bash
   find bazel-out -name "VCoreMiniAxi*.h" 2>/dev/null
   ```

**需要观察的现象**：`module_name=CoreMiniAxi` 如何变成 `VCoreMiniAxi`（Verilator 的命名规则是 `V` + 顶层模块名）。

**预期结果**：能找到生成的 `VCoreMiniAxi.h`，里面声明了 `class VCoreMiniAxi`，这就是后续 testbench 直接 `#include` 的模型类。如果构建因缺少依赖失败，属于环境问题（待本地验证）。

#### 4.1.5 小练习与答案

**练习 1**：`chisel_cc_library` 为什么要调用两次 `verilator_cc_library`？两次的产物名有何区别？

> **答案**：为了同时服务两类消费者。`systemc=True` 那份产物名不带后缀（如 `core_mini_axi_cc_library`），给 SystemC testbench 用；`systemc=False` 那份加 `_cc` 后缀（如 `core_mini_axi_cc_library_cc`），给纯 C++ 的轻量封装（`hw_sim/`）用。同一份 RTL，两种代码生成模式。

**练习 2**：`rules/verilog.bzl` 里的 `verilog_zip_bundle` 和 Verilator 仿真有什么关系？

> **答案**：没有直接关系。它只是把 Chisel 生成的 SystemVerilog 打成 zip 包，供 VCS/FPGA/lint 等非 Verilator 下游复用。Verilator 仿真模型的生成完全由 `rules/chisel.bzl` 的 `chisel_cc_library` → `verilator_cc_library` 负责。

---

### 4.2 testbench 仿真目标的批量定义：8 个变体从哪来

#### 4.2.1 概念说明

`u2-l3` 提过一句：「8 个仿真器变体由 `template_rule` 从同一份源批量生成」。本节把这个机制彻底讲清楚。

CoralNPU 有 8 种核配置组合（普通核 / RVV 核、带 verification 调试端口 / 不带、带 highmem / 不带、ITCM/DTCM 翻倍 / 不翻倍），每种都需要一个 testbench 库 + 一个仿真器二进制。如果手写，BUILD 文件里要重复 16 段几乎相同的 `cc_library`/`cc_binary`。`template_rule` 就是为了消除这种重复——它是一个 Starlark 宏，用一份规则模板 + 一张「名字→差异参数」的表，批量实例化出多个目标。

#### 4.2.2 核心流程

`template_rule(rule, name_map, **kwargs)` 的逻辑非常朴素：

1. 遍历 `name_map` 里的每个 `(名字, 参数表)`。
2. 把公共参数 `**kwargs` 合并进每个参数表。
3. 调用 `rule(name=名字, **合并后的参数)`。

`tests/verilator_sim/BUILD` 用它做了两件事：

- 用 `template_rule(cc_library, {...8 项...})` 生成 8 个 testbench 库，每个的差异只在 `VERILATOR_MODEL` 宏定义和对应的 RTL 模型库依赖。
- 用 `template_rule(cc_binary, {...8 项...})` 生成 8 个二进制，共用同一份 `core_mini_axi_sim.cc` 源码，差异只在链接哪个 testbench 库。

**关键技巧**：同一份 `core_mini_axi_sim.cc` 通过 `VERILATOR_MODEL` 宏切换要实例化的核模型（`VCoreMiniAxi`、`VRvvCoreMiniAxi` 等）。testbench 头文件用宏拼接出 `#include` 的文件名，实现「一份源码 + N 个模型」。

#### 4.2.3 源码精读

先看 `template_rule` 的实现——它只是一个 for 循环：

[utils.bzl:15-47](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/rules/utils.bzl#L15-L47)：

```python
def template_rule(rule, name_map, **kwargs):
    for rule_name, argmap in name_map.items():
        rule_kwargs = argmap
        for k, v in kwargs.items():
            rule_kwargs.update([(k, v)])   # 合并公共参数
        rule(name = rule_name, **rule_kwargs)
```

再看它怎么用在 testbench 上。先定义一份**公共依赖**（8 个变体都依赖它）：

[tests/verilator_sim/BUILD:91-108](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/tests/verilator_sim/BUILD#L91-L108)：

```python
CORE_MINI_AXI_TB_CC_LIBRARY_COMMON_DEPS = [
    ":elf", ":sim_libs", ":util",
    "//hdl/verilog:sram_backdoor",          # DPI backdoor SRAM（4.5 节）
    "//tests/systemc:Xbar",
    "//tests/systemc:instruction_trace",
    "@accellera_systemc//:systemc",
    "@libsystemctlm_soc",                   # TLM↔AXI 桥等 SystemC IP
    ...
]
```

然后 `template_rule` 把 8 个变体一次性展开，每个指定自己的 `VERILATOR_MODEL` 宏和 RTL 模型库：

[tests/verilator_sim/BUILD:110-209](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/tests/verilator_sim/BUILD#L110-L209)（节选两个变体）：

```python
template_rule(
    cc_library,
    {
        "core_mini_axi_tb": {
            "srcs": ["...VCoreMiniAxi_parameters.h"] + COMMON_SRCS,
            "deps": ["//hdl/chisel/src/coralnpu:core_mini_axi_cc_library"] + COMMON_DEPS,
            "defines": ["VERILATOR_MODEL=VCoreMiniAxi"],
        },
        "rvv_core_mini_axi_tb": {
            "srcs": ["...VRvvCoreMiniAxi_parameters.h"] + COMMON_SRCS,
            "deps": ["//hdl/chisel/src/coralnpu:rvv_core_mini_axi_cc_library"] + COMMON_DEPS,
            "defines": ["VERILATOR_MODEL=VRvvCoreMiniAxi"],
        },
        # ... 另外 6 个变体同理
    },
    hdrs = ["coralnpu/core_mini_axi_tb.h"],
)
```

注意每个变体都依赖**不带 `_cc` 后缀**的模型库（如 `:core_mini_axi_cc_library`）——也就是 4.1 节里 `systemc=True` 的那份，因为这是 SystemC testbench。同理，8 个二进制也用 `template_rule(cc_binary, ...)` 批量生成，共用同一份 `core_mini_axi_sim.cc`：

[tests/verilator_sim/BUILD:219-266](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/tests/verilator_sim/BUILD#L219-L266)：

```python
template_rule(
    cc_binary,
    {
        "core_mini_axi_sim":     {"deps": [":core_mini_axi_tb"]     + COMMON_DEPS},
        "rvv_core_mini_axi_sim": {"deps": [":rvv_core_mini_axi_tb"] + COMMON_DEPS},
        # ...
    },
    srcs = ["coralnpu/core_mini_axi_sim.cc"],
)
```

最后看那个让「一份源码适配 N 个模型」的宏技巧。testbench 头文件用预处理宏拼出要 `#include` 的模型头文件名：

[core_mini_axi_tb.h:51-58](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/tests/verilator_sim/coralnpu/core_mini_axi_tb.h#L51-L58)：

```cpp
#define MODEL_HEADER_SUFFIX .h
#define MODEL_HEADER STRINGIFY(VERILATOR_MODEL MODEL_HEADER_SUFFIX)
#include MODEL_HEADER              // → #include "VCoreMiniAxi.h"
```

`VERILATOR_MODEL` 是 BUILD 里用 `defines` 注入的宏。所以 `core_mini_axi_tb` 编译时 `#include "VCoreMiniAxi.h"`，`rvv_core_mini_axi_tb` 编译时 `#include "VRvvCoreMiniAxi.h"`——同一份 `.cc/.h`，编译期切换模型。

#### 4.2.4 代码实践

**目标**：用 Bazel 查询命令看清「一个二进制 → 一个 testbench 库 → 一个 RTL 模型」的依赖链。

1. 执行：

   ```bash
   bazel query --noimplicit_deps 'deps(//tests/verilator_sim:core_mini_axi_sim)' \
     | grep -E "core_mini_axi_tb$|core_mini_axi_cc_library$"
   ```

2. 再对 RVV 变体做同样查询：

   ```bash
   bazel query --noimplicit_deps 'deps(//tests/verilator_sim:rvv_core_mini_axi_sim)' \
     | grep -E "rvv_core_mini_axi_tb$|rvv_core_mini_axi_cc_library$"
   ```

**需要观察的现象**：`core_mini_axi_sim` 依赖 `:core_mini_axi_tb` 再依赖 `:core_mini_axi_cc_library`；`rvv_core_mini_axi_sim` 对应地依赖带 `rvv_` 前缀的那一套。

**预期结果**：每条链都是「二进制 → testbench 库 → Chisel 模型库」三层，且 8 个变体的结构完全同构，只有名字前缀和 `VERILATOR_MODEL` 不同。这正是 `template_rule` 的价值。

#### 4.2.5 小练习与答案

**练习 1**：如果把 `template_rule` 改成手写 8 个 `cc_library`，BUILD 文件会膨胀多少？这种重复最危险的地方在哪？

> **答案**：会膨胀约 8 倍，且每个 target 都要重复写一长串 `CORE_MINI_AXI_TB_CC_LIBRARY_COMMON_DEPS`。最危险的是「改了一处忘了改另外 7 处」——例如新增一个依赖，8 个 target 里漏改一个，会导致该变体行为不一致却难以发现。`template_rule` 把公共部分集中，差异部分表格化，杜绝了这类疏漏。

**练习 2**：`VERILATOR_MODEL=VCoreMiniAxi` 这个宏在编译期如何起作用？

> **答案**：它被 `core_mini_axi_tb.h` 的 `#include MODEL_HEADER` 宏拼接成 `#include "VCoreMiniAxi.h"`，并让 testbench 里所有 `VERILATOR_MODEL*` 出现的地方（如成员 `std::unique_ptr<VERILATOR_MODEL> core_`）都展开成 `VCoreMiniAxi`。于是同一份源码编译出 8 个不同的模型绑定。

---

### 4.3 SystemC testbench 与仿真器入口：命令行主力怎么跑

#### 4.3.1 概念说明

`tests/verilator_sim` 是 CoralNPU 的**命令行主力仿真路径**，也是 README Quick Start 指的那条路。它用 **SystemC** 搭建 testbench。

SystemC 是一套用 C++ 写硬件验证平台的库（本质是 C++ 类库 + 一个事件调度内核）。它的核心抽象是 **TLM（Transaction Level Modeling）**——你不直接操作每一根 AXI 信号线，而是发起一笔「事务」（「从地址 A 读 32 字节」），由 TLM↔AXI 桥自动展开成具体的 AXI 握手时序。这让 testbench 代码更接近「主机 CPU 的视角」而非「线缆的视角」。

CoralNPU 复用了一个开源 SystemC IP 库 `@libsystemctlm_soc`（提供 `tlm2axi_bridge`、`axi2tlm_bridge`、AXI 协议检查器等），所以 testbench 不用自己写 AXI 时序发生器。

#### 4.3.2 核心流程

`core_mini_axi_sim` 的执行流程（SystemC 的入口函数叫 `sc_main`，相当于普通 C++ 的 `main`）：

1. 解析命令行 flags（`--binary` 必填，`--backdoor_load`/`--instr_trace`/`--cycles` 可选）。
2. 构造 `CoreMiniAxi_tb`（实例化 `VCoreMiniAxi` 模型 + TLM 桥 + Xbar + 指令追踪器），注册「halted 回调」。
3. `sc_start(SC_ZERO_TIME)`：让 Verilog `initial` 块先跑起来（这步关键，backdoor SRAM 的 DPI 注册就发生在 `initial` 里）。
4. **启动序列**：`LoadElfSync(binary)` → `ClockGateSync(false)`（开钟）→ `ResetAsync(false)`（释放复位）。
5. 阻塞等待 halted 回调被触发（程序跑完）。
6. 若无 fault，调 `CheckStatusSync()` 读 STATUS；最后 `sc_stop()` 收尾。

#### 4.3.3 源码精读

入口 `sc_main` 与 flags 定义：

[core_mini_axi_sim.cc:36-41](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/tests/verilator_sim/coralnpu/core_mini_axi_sim.cc#L36-L41) 定义 flags：

```cpp
ABSL_FLAG(int, cycles, 100000000, "Simulation cycles");
ABSL_FLAG(bool, trace, false, "Dump VCD trace");
ABSL_FLAG(std::string, binary, "", "Binary to execute");
ABSL_FLAG(bool, debug_axi, false, "Enable AXI traffic debugging");
ABSL_FLAG(bool, instr_trace, false, "Log instructions to console");
ABSL_FLAG(bool, backdoor_load, false, "Enable high-speed backdoor code loading");
```

`--binary` 是必填项——没有它程序直接报错退出（见 [core_mini_axi_sim.cc:94-97](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/tests/verilator_sim/coralnpu/core_mini_axi_sim.cc#L94-L97)）。`--backdoor_load` 决定是否用 DPI 直写 SRAM（4.5 节），`--instr_trace` 决定是否打印每条退休指令。

启动序列的核心是 `run()` 函数：

[core_mini_axi_sim.cc:64-82](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/tests/verilator_sim/coralnpu/core_mini_axi_sim.cc#L64-L82)：

```cpp
std::thread sc_main_thread([&tb]() { tb.start(); });   // 跑 SystemC 仿真核
CHECK_OK(tb.LoadElfSync(binary));    // 1. 加载 ELF（backdoor 或 frontdoor）
CHECK_OK(tb.ClockGateSync(false));   // 2. 开钟
CHECK_OK(tb.ResetAsync(false));      // 3. 释放复位
{ /* 阻塞等 halted_cv */ }
if (!tb.io_fault && !tb.tohost_halt) {
  CHECK_OK(tb.CheckStatusSync());     // 4. 读 STATUS
}
sc_stop();                            // 5. 收尾
```

这套 `LoadElfSync / ClockGateSync / ResetAsync / CheckStatusSync` 是 testbench 暴露的同步接口，声明在：

[core_mini_axi_tb.h:199-214](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/tests/verilator_sim/coralnpu/core_mini_axi_tb.h#L199-L214)（节选）：

```cpp
absl::Status LoadElfSync(const std::string& file_name);
absl::Status ClockGateSync(bool enable);
absl::Status ResetSync(bool enable);
absl::Status CheckStatusSync();
VERILATOR_MODEL* core() { return core_.get(); }   // 取 Verilator 模型指针
```

它们的实现都在 `core_mini_axi_tb.cc` 里：经 TLM 发起对 CSR 地址（`0x30000`/`0x30004`/`0x30008`）的读写来完成「写 PC → 开钟 → 释放复位 → 读状态」，和 `u3-l5` 讲的 CSR 启动序列一一对应——只不过这里是通过 TLM↔AXI 桥发起，而非直接戳引脚。

README 给出的就是这条路的用法：

[README.md:40-44](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/README.md#L40-L44)：

```bash
bazel build //tests/verilator_sim:core_mini_axi_sim   # 建仿真器
bazel-bin/tests/verilator_sim/core_mini_axi_sim \
    --binary bazel-out/.../examples/coralnpu_v2_hello_world_add_floats.elf
```

> 关于 `doc/simulation.md`：这份文档**只讲了 VCS**，且明确说「默认禁用 VCS，用 `--config=vcs` 启用」（见 [simulation.md:34-35](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/doc/simulation.md#L34-L35)）。这反过来印证：**Verilator 是默认/主力仿真路径**，无需任何额外 config 即可用。VCS 流程留到 `u11-l2` 讲。

#### 4.3.4 代码实践

**目标**：跑通 README 的 Quick Start，并用 `--instr_trace` 观察指令退休。

1. 按 [README.md:40-44](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/README.md#L40-L44) 建一个示例程序和仿真器：

   ```bash
   bazel build //examples:coralnpu_v2_hello_world_add_floats
   bazel build //tests/verilator_sim:core_mini_axi_sim
   ```

2. 加上 `--instr_trace` 跑：

   ```bash
   bazel-bin/tests/verilator_sim/core_mini_axi_sim \
       --binary $(bazel info bazel-bin)/examples/coralnpu_v2_hello_world_add_floats.elf \
       --instr_trace
   ```

3. 再加 `--backdoor_load` 跑一次，对比两次的加载耗时（backdoor 应明显更快）。

**需要观察的现象**：`--instr_trace` 会逐条打印退休指令的 PC 与反汇编；程序正常结束时进程返回 0（`run()` 里 `!(tb.io_fault) && tohost_val==1` 时返回 true）。

**预期结果**：看到从 `_start`（PC=0）开始的启动序列、`main` 执行、最后 CRT 的 `mpause` 触发 halted。若环境缺 Verilator/工具链，构建会失败（待本地验证）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `run()` 里要先 `sc_start(SC_ZERO_TIME)` 再构造后续逻辑？

> **答案**：`SC_ZERO_TIME` 让 Verilog 的 `initial` 块在零时刻先执行——backdoor SRAM 的 DPI 注册函数 `sram_init` 就是在 `initial` 里被调用的（4.5 节）。只有先让 SRAM 注册了自己，后续的 backdoor 加载才能找到目标 SRAM。如果跳过这步，SRAM 还没注册，加载会失败。

**练习 2**：SystemC 路径里，主机写 CSR（如 PC_START）为什么不用直接赋值引脚？

> **答案**：因为 testbench 用 TLM 抽象——它把核的 AXI slave 端口接到一个 `tlm2axi_bridge`，主机「写 CSR」被建模成一笔 TLM 事务，由桥自动展开成 AXI 写突发时序。这样 testbench 代码贴近真实 SoC 里「外部 CPU 经 AXI 配置 NPU」的场景，也复用了 `@libsystemctlm_soc` 的成熟 AXI IP，而非手写时序。

---

### 4.4 轻量 C++ 库封装与时钟驱动：库式路径如何被 npusim 复用

#### 4.4.1 概念说明

`tests/verilator_sim` 的 SystemC 体系很重（依赖 SystemC 内核、TLM 库、`libsystemctlm_soc`），适合做完整的回归测试。但有些场景只需要「加载程序 → 跑 → 读结果」，不需要 SystemC 那套事务抽象——例如 npusim（`u10-l3`）想从 Python 调一个 C++ 仿真器来跑 MobileNet。

为此 CoralNPU 在 `hw_sim/` 里提供了**轻量 C++ 库封装**：它直接操作 Verilator 模型的引脚（`core_.io_axi_slave_write_addr_valid = ...`），用一个手写的 `Clock::Step` 推进时间，完全不依赖 SystemC。这正对应 4.1 节里 `systemc=False` 的那份 `_cc` 模型产物。

这套封装是**三层结构**：

```
CoralNPUSimulator（抽象接口）
        └─ CoreMiniAxiSimulator（实现）
               └─ CoreMiniAxiWrapper（直接操作 Verilator 模型引脚）
                      └─ hw_primitives（Clock / AXI 驱动器原语）
```

#### 4.4.2 核心流程

`CoreMiniAxiWrapper` 在构造时把 Verilator 模型的几百根 AXI 信号线绑到 4 个驱动器对象上（slave 读/写、master 读/写）。当你调 `wrapper.Write(addr, data)`，slave 写驱动器会发起 AXI 写突发；驱动器是 `Clock` 的 `Observer`，每个时钟边沿被回调一次来推进握手。

`Clock::Step()` 一个周期的动作：

1. `timeInc(1)`；`clock=1`（上升沿）；`eval()`；通知所有 observer 的 `OnRisingEdge()`；再 `eval()`。
2. `timeInc(1)`；`clock=0`（下降沿）；`eval()`；通知 `OnFallingEdge()`；再 `eval()`。

observer 模式让 AXI 驱动器能在正确的边沿搬运数据（地址在上升沿采样、数据在下降沿准备等），从而用纯 C++ 模拟出真实的 AXI 握手。

#### 4.4.3 源码精读

`CoreMiniAxiWrapper` 的构造函数把引脚绑到驱动器（这里只看结构，省略几百个引脚）：

[core_mini_axi_wrapper.h:31-117](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hw_sim/core_mini_axi_wrapper.h#L31-L117)（节选）：

```cpp
class CoreMiniAxiWrapper {
 public:
  explicit CoreMiniAxiWrapper(VerilatedContext* context)
      : context_(context),
        core_(context, "core"),
        clock_(context, &core_.io_aclk, &core_),       // Clock 绑到 io_aclk
        slave_write_driver_(&clock_, &core_.io_axi_slave_write_addr_valid,
                            /* ... 把写地址/写数据/写响应通道的引脚逐一传入 ... */),
        slave_read_driver_(&clock_, /* ... 读通道引脚 ... */),
        master_read_driver_(&clock_, /* ... 主机读通道引脚 ... */),
        master_write_driver_(&clock_, /* ... 主机写通道引脚 ... */),
        halted_(&core_.io_halted), wfi_(&core_.io_wfi) {}
```

每个驱动器都接收 `&clock_`——成为它的 observer，从而每拍被回调。`halted_`/`wfi_` 直接指向模型的输出引脚，采样它们即可知道内核状态。

暴露的高层操作简洁地包装了 AXI 细节：

[core_mini_axi_wrapper.h:142-216](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hw_sim/core_mini_axi_wrapper.h#L142-L216)（节选）：

```cpp
void Write(uint32_t addr, uint32_t len, const char* data) {
  // 把长传输切成不超过 4KB、不跨 4KB 边界的 AXI 突发
  while (write_data.size() > 0) {
    /* 算本次 transaction_bytes */
    auto transaction = slave_write_driver_.WriteTransaction(0, addr, local_data);
    while (!(*transaction)) { Step(); }   // 推进时钟直到事务完成
    /* addr 前移 */
  }
}

bool WaitForTermination(int timeout = 10000) {
  for (int i = 0; i < timeout; i++) {
    if ((*halted_) || (*wfi_)) return true;   // 采 halted/wfi 引脚
    Step();
  }
  return false;
}
```

注意 `Write` 把传输按 4KB 对齐切分——这是 AXI 协议的硬约束（突发不能跨 4KB 边界），wrapper 帮你处理了。

`Clock::Step` 是这套封装的心脏：

[hw_primitives.cc:26-42](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hw_sim/hw_primitives.cc#L26-L42)：

```cpp
void Clock::Step() {
  context_->timeInc(1);
  (*clock_) = 1;  Eval();
  for (auto& observer : observers_) { observer->OnRisingEdge(); Eval(); }

  context_->timeInc(1);
  (*clock_) = 0;  Eval();
  for (auto& observer : observers_) { observer->OnFallingEdge(); Eval(); }
}
```

每次改信号后都 `Eval()` 让模型求值，保证组合逻辑传播到位。`Observer` 是个简单基类：

[hw_primitives.h:30-57](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hw_sim/hw_primitives.h#L30-L57)（节选）：

```cpp
class Clock {
 public:
  class Observer {
   public:
    explicit Observer(Clock* clock);   // 构造时自动订阅
    virtual void OnRisingEdge() {}
    virtual void OnFallingEdge() {}
  };
  template <typename Model>
  Clock(VerilatedContext* context, uint8_t* clock, Model* model)
      : ..., eval_function_([model]() { model->eval(); }) {}
  void Step();
};
```

往上是三层封装的最外两层。抽象接口定义了「读写 TCM、读写 mailbox、运行、等待终止」：

[coralnpu_simulator.h:20-38](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hw_sim/coralnpu_simulator.h#L20-L38)（节选）：

```cpp
class CoralNPUSimulator {
 public:
  static CoralNPUSimulator* Create();          // 工厂方法
  virtual void ReadTCM(uint32_t addr, size_t size, char* data) = 0;
  virtual void WriteTCM(uint32_t addr, size_t size, const char* data) = 0;
  virtual const CoralNPUMailbox& ReadMailbox(void) = 0;
  virtual void WriteMailbox(const CoralNPUMailbox& mailbox) = 0;
  virtual bool WaitForTermination(int timeout) = 0;
  virtual void Run(uint32_t start_addr) = 0;   // 从某 PC 开始跑
};
```

`CoreMiniAxiSimulator` 实现它，关键是 `Run()` 把 `u3-l5` 的启动序列翻译成三次 AXI 写：

[core_mini_axi_simulator.cc:70-74](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hw_sim/core_mini_axi_simulator.cc#L70-L74)：

```cpp
void CoreMiniAxiSimulator::Run(uint32_t start_addr) {
  wrapper_.WriteWord(0x30004, start_addr);   // 写 PC_START
  wrapper_.WriteWord(0x30000, 1u);           // 开钟 + 保持复位
  wrapper_.WriteWord(0x30000, 0u);           // 释放复位
}
```

这与 4.3 节 SystemC 路径的 `LoadElfSync/ClockGateSync/ResetAsync` 是**同一套启动序列的两种实现**——一个走 TLM 事务，一个直接写 CSR 引脚。

最后，BUILD 用同一个 `.cc` 源、靠 `-DENABLE_RVV` 编译开关产出两份库（标量版与 RVV 版），并各打包成一个 `.so` 供外部（npusim）动态加载：

[hw_sim/BUILD:67-88](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hw_sim/BUILD#L67-L88)（节选）：

```python
cc_library(
    name = "core_mini_axi_simulator",
    srcs = ["core_mini_axi_simulator.cc"],
    linkstatic = True, alwayslink = True,
    deps = [":core_mini_axi_wrapper", ":coralnpu_simulator_headers"],
)
cc_library(
    name = "core_mini_axi_simulator_rvv",
    srcs = ["core_mini_axi_simulator.cc"],       # 同一份源
    copts = ["-DENABLE_RVV"],                     # 切到 RVVV 模型
    ...
)
```

`alwayslink = True` 是关键：它让 `CoralNPUSimulator::Create()`（[core_mini_axi_simulator.cc:117-119](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hw_sim/core_mini_axi_simulator.cc#L117-L119)）这个工厂方法即使没有被显式引用，也能被链接进 `.so`，供 npusim 通过 C ABI 调用。

#### 4.4.4 代码实践

**目标**：阅读 `hw_sim` 里的示例程序，理解库式封装的最小用法。

1. 读 [hw_sim/core_mini_axi_simulator_example.cc](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hw_sim/core_mini_axi_simulator_example.cc) 与 [hw_sim/core_mini_axi_wrapper_example.cc](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hw_sim/core_mini_axi_wrapper_example.cc)。
2. 对照 [hw_sim/BUILD:97-109](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hw_sim/BUILD#L97-L109)，看这两个示例二进制依赖了哪个 `.elf` 作为 `data`。
3. 构建并运行示例（待本地验证）：

   ```bash
   bazel run //hw_sim:core_mini_axi_simulator_example
   ```

**需要观察的现象**：示例程序用 `CoralNPUSimulator::Create()` 拿到一个仿真器实例，然后 `WriteTCM` 写输入、`Run` 启动、`WaitForTermination` 等待、`ReadMailbox`/`ReadTCM` 读结果——全程没有 SystemC。

**预期结果**：示例跑完打印出 mailbox 或 TCM 里的输出值。这条路径正是 npusim 跑 MobileNet 的底层（见 `u10-l3`）。

#### 4.4.5 小练习与答案

**练习 1**：库式封装（`hw_sim`）和 SystemC testbench（`tests/verilator_sim`）共用同一份 RTL 吗？它们用的是 4.1 节哪两份产物？

> **答案**：共用同一份 Chisel 生成的 RTL，但用 4.1 节的两份不同 Verilator 产物。SystemC testbench 用 `systemc=True` 的 `core_mini_axi_cc_library`（不带 `_cc` 后缀）；库式封装用 `systemc=False` 的 `core_mini_axi_cc_library_cc`（带 `_cc` 后缀）。两份产物来自同一份 SystemVerilog，只是 Verilator 代码生成模式不同。

**练习 2**：`CoreMiniAxiWrapper::Write` 为什么要按 4KB 切分传输？

> **答案**：AXI 协议规定一次突发（burst）不能跨越 4KB 地址边界，否则从机可能无法正确处理。`Write` 在发事务前先算出「到下一个 4KB 边界还剩多少字节」，把超长传输切成多段合规突发，保证协议正确。

---

### 4.5 仿真支撑机制：DPI backdoor SRAM 与 mailbox

#### 4.5.1 概念说明

仿真的两个高频痛点是：(1) 加载程序太慢；(2) 内核没有 `printf`，怎么知道它「说了什么」。CoralNPU 用两个机制解决它们：

- **DPI backdoor SRAM**：绕过 AXI 总线，用 DPI 直接把 ELF 内容 memcpy 进 SRAM 的 C++ 内存，加载速度比走真实 AXI 突发快几个数量级。`--backdoor_load` flag 就是开关它。
- **mailbox（信箱）**：一个 16 字节的共享缓冲。内核经 master AXI 端口写出的数据会被捕获进 mailbox，仿真器读 mailbox 就能拿到内核「想说的话」——相当于一个极简的输出通道。

#### 4.5.2 核心流程

**backdoor SRAM** 采用「late binding（迟绑定）」设计，分两阶段：

1. **注册阶段**：仿真启动后，RTL 里的 SRAM 行为模型（SystemVerilog）在 `initial` 块里调用 DPI 函数 `sram_init(全局地址, 大小, 位宽)`，把自己注册到一个 C++ 的 `map` 里。此时每个 SRAM 都拿到一个 C++ 句柄。
2. **加载阶段**：之后任何时候调 `sram_backdoor_load_c(地址, 数据, 长度)`，它会遍历所有已注册 SRAM，按地址区间匹配，把数据 `memcpy` 进对应 SRAM 的 `vector`。RTL 随后经 `sram_read` DPI 读到的就是被改过的内容。

之所以叫「late binding」，是因为加载调用允许在 SRAM 注册之前发生——`SramBackdoorLoad` 用地址区间匹配，只要某次加载的地址落在已注册 SRAM 的区间内就生效，顺序很灵活。

**mailbox** 在库式路径里靠 master 端口的回调实现：仿真器向 wrapper 注册一个 `WriteCallback`，每当内核经 master 端口发起 AXI 写，回调就被触发，把写出的 16 字节存进 `CoralNPUMailbox`。

#### 4.5.3 源码精读

backdoor SRAM 的核心是几个 `extern "C"` 的 DPI 函数。注册与读写：

[sram_backdoor.cc:125-151](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/verilog/sram_backdoor.cc#L125-L151)：

```cpp
extern "C" {
void *sram_init(uint64_t global_addr, uint64_t size_bytes, uint32_t width_bytes) {
  return RegisterSram(global_addr, size_bytes, width_bytes);   // RTL 调它注册自己
}
void sram_read(void *handle, uint32_t addr, svBitVecVal *data) {
  /* 按 addr*width 偏移从 vector 拷出 width 字节 */
}
void sram_write(void *handle, uint32_t addr, const svBitVecVal *data, uint32_t wmask) {
  /* 按 wmask 逐字节写入 vector */
}
}
```

`RegisterSram` 顺便把 SRAM 内容**随机化**（模拟上电后未初始化 SRAM 的真实行为，避免仿真里误把零当成有效数据）：

[sram_backdoor.cc:56-74](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/verilog/sram_backdoor.cc#L56-L74)：

```cpp
void *RegisterSram(uint64_t global_addr, size_t size_bytes, uint32_t width_bytes) {
  BackdoorSram *sram = new BackdoorSram;
  sram->global_addr = global_addr;
  sram->data.resize(size_bytes);
  sram->width_bytes = width_bytes;
  // Randomize memory content for simulation.   ← 关键：上电随机
  for (size_t i = 0; i < size_bytes; ++i) sram->data[i] = dis(gen);
  registered_srams[global_addr] = sram;
  return sram;
}
```

「late binding」的加载逻辑——遍历所有 SRAM、按区间匹配：

[sram_backdoor.cc:109-118](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/verilog/sram_backdoor.cc#L109-L118)：

```cpp
bool SramBackdoorLoad(uint64_t global_addr, const uint8_t *data, size_t len) {
  bool load_ok = false;
  for (const auto &[base, sram] : registered_srams) {     // 遍历所有已注册 SRAM
    if (ApplyLoadToSram(*sram, global_addr, data, len))   // 区间命中才 memcpy
      load_ok = true;
  }
  return load_ok;
}
```

`ApplyLoadToSram` 用 `[global_addr, +size)` 区间判定命中（`u4-l1` 讲过的左闭右开区间套路）。最上层的便捷函数直接解析 ELF：

[sram_backdoor.cc:172-221](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/verilog/sram_backdoor.cc#L172-L221)：`sram_load_elf(filename)` 自己解析 ELF32 头与 program header，对每个 `PT_LOAD` 段调 `SramBackdoorLoad(p_paddr, ...)`——把段按物理地址直接灌进对应 SRAM，完全绕过总线。这正是 `--backdoor_load` 的高速通道。

testbench 把它包装成 `BackdoorLoad`，并有专门的测试验证「写进去的能经 AXI 读回来」：

[backdoor_load_test.cc:45-58](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/tests/verilator_sim/coralnpu/backdoor_load_test.cc#L45-L58)：

```cpp
tb.BackdoorLoad(0, itcm_data, 32);        // 直写 ITCM
tb.BackdoorLoad(0x10000, dtcm_data, 32);  // 直写 DTCM（u4-l1 的 DTCM 基址）
/* 用 AXI 读回并 Expect 校验 */
read_transfers.push_back(utils::Read(0, 32));
read_transfers.push_back(utils::Expect(itcm_data, 32));
```

这个测试同时检验了两件事：backdoor 写入确实落到正确的 SRAM，且 RTL 读出的内容与写入一致（DPI 的 `sram_read` 与 backdoor 的 `memcpy` 操作同一份 `vector`）。

mailbox 的数据结构极简：

[mailbox.h:18-20](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hw_sim/mailbox.h#L18-L20)：

```cpp
struct CoralNPUMailbox {
  uint32_t message[4] = {0, 0, 0, 0};   // 4 × 4B = 16 字节
};
```

库式路径靠 master 端口回调捕获内核写出的 16 字节：

[core_mini_axi_simulator.cc:80-96](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hw_sim/core_mini_axi_simulator.cc#L80-L96)：

```cpp
AxiWResp CoreMiniAxiSimulator::WriteCallback(const AxiAddr& addr, const AxiWData& data) {
  CoralNPUMailbox& mailbox = wrapper_.mailbox();
  uint8_t* mailbox_data = reinterpret_cast<uint8_t*>(mailbox.message);
  const uint8_t* write_data = reinterpret_cast<const uint8_t*>(&data.write_data_bits_data[0]);
  for (int i = 0; i < 16; i++)          // 按 strb 字节使能写入 16 字节
    if (data.write_data_bits_strb & (1 << i)) mailbox_data[i] = write_data[i];
  ...
}
```

`ReadCallback`（[core_mini_axi_simulator.cc:98-114](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hw_sim/core_mini_axi_simulator.cc#L98-L114)）反向把 mailbox 内容回送给内核的 master 读。于是 mailbox 成了内核与仿真器之间的一个 16 字节「双向留言板」——`hw_sim/mailbox_example.cc` 就是演示内核写消息、仿真器读消息的最小例子。

#### 4.5.4 代码实践

**目标**：用现成的 backdoor 测试理解 DPI 通路，并定位 mailbox 的回调接线点。

1. 读 [backdoor_load_test.cc](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/tests/verilator_sim/coralnpu/backdoor_load_test.cc)，注意它构造 `CoreMiniAxi_tb` 时第 6 个参数 `backdoor_load=true`。
2. 在 [core_mini_axi_simulator.cc:22-34](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hw_sim/core_mini_axi_simulator.cc#L22-L34) 的构造函数里，找到 `RegisterReadCallback(read_cb)` 与 `RegisterWriteCallback(write_cb)` 两行——这就是 mailbox 回调的接线点。
3. 运行 backdoor 测试（待本地验证）：

   ```bash
   bazel test //tests/verilator_sim:backdoor_load_test
   ```

4. 运行 mailbox 示例，观察内核写出的消息被仿真器读回（待本地验证）：

   ```bash
   bazel run //hw_sim:core_mini_axi_simulator_example
   ```

**需要观察的现象**：backdoor 测试中，`BackdoorLoad` 写入的 32 字节与经 AXI `Read`/`Expect` 读回的完全一致；mailbox 示例中仿真器读到的 `message[]` 与内核写出的内容一致。

**预期结果**：两条 DPI 通路都按预期工作，证明「绕过总线的直写」与「RTL 总线读」操作的是同一份 SRAM 内存，mailbox 回调正确捕获了 master 端口的写出数据。

#### 4.5.5 小练习与答案

**练习 1**：为什么 `RegisterSram` 要把 SRAM 内容随机化，而不是清零？

> **答案**：真实 SRAM 上电后内容是未定义的（既不保证是零也不保证是某个值）。如果仿真里清零，开发者可能误以为「程序依赖了某个为零的初值」而没发现 bug——把 SRAM 随机化能暴露这类对未初始化内存的意外依赖，让仿真更贴近真实硬件。

**练习 2**：`SramBackdoorLoad` 为什么要遍历**所有**已注册 SRAM，而不是按地址直接查 map？

> **答案**：因为一次加载的数据可能跨越多个 SRAM 的边界，或者加载地址与 SRAM 基址不对齐。`ApplyLoadToSram` 对每个 SRAM 都做一次区间求交，只把落在该 SRAM 区间内的那段字节拷进去。遍历所有 SRAM 保证了无论加载范围如何分布，每段数据都能落到正确的 SRAM。这也是「late binding」能容忍乱序的原因。

---

## 5. 综合实践

把本讲五个模块串起来，完成一次「从构建规则到运行观测」的全链路追踪。

**任务**：为 CoralNPU 的 Verilator 仿真画一张完整的「依赖与数据流」图，并验证关键环节。

**步骤**：

1. **模型生成链**（4.1）：从 [coralnpu/BUILD:531-574](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/BUILD#L531-L574) 出发，画出 `core_mini_axi_cc_library`（SystemC 版）与 `core_mini_axi_cc_library_cc`（C++ 版）如何由 `chisel_cc_library` 经 firtool + verilator 生成。

2. **目标装配链**（4.2）：用 `bazel query` 画出 `//tests/verilator_sim:core_mini_axi_sim` → `:core_mini_axi_tb` → `:core_mini_axi_cc_library` 的三层依赖，并标出每层注入的 `VERILATOR_MODEL` 宏值。

3. **运行链**（4.3 + 4.4）：对照 [core_mini_axi_sim.cc:64-82](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/tests/verilator_sim/coralnpu/core_mini_axi_sim.cc#L64-L82)（SystemC 路径）与 [core_mini_axi_simulator.cc:70-74](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hw_sim/core_mini_axi_simulator.cc#L70-L74)（库式路径），在图上标注两条路径各自如何完成「写 PC(0x30004) → 写 RESET_CONTROL(0x30000)=1 → =0 → 等 halted」启动序列。

4. **支撑机制**（4.5）：在图上标出 backdoor SRAM（DPI `sram_init`/`sram_backdoor_load_c`）介入加载的位置，以及 mailbox 回调（`WriteCallback`/`ReadCallback`）捕获内核输出的位置。

5. **验证**：执行 `bazel build //tests/verilator_sim:core_mini_axi_sim` 与 `bazel test //tests/verilator_sim:backdoor_load_test`（待本地验证），确认前 4 步画出的链路与实际构建产物一致。

**预期产出**：一张覆盖「Chisel 源码 → Verilator C++ 模型 → Bazel 目标 → 两条运行入口 → DPI/ mailbox 支撑」的全景图，能向他人讲清「一条 `.elf` 是怎么在这套仿真器里跑起来的」。

## 6. 本讲小结

- Verilator 仿真模型由 `rules/chisel.bzl` 的 `chisel_cc_library` 生成：Chisel 经 firtool 综合成 SystemVerilog，再由 `verilator_cc_library` 编译成 C++ 类（如 `VCoreMiniAxi`），且一次产出 SystemC 版与纯 C++ `_cc` 版两份。
- `rules/verilog.bzl` **不**定义仿真目标，只做 Verilog 文件打包（`verilog_zip_bundle`）；`default.vlt.tpl` 是 cocotb 路径的信号可见性配置，由 `coco_tb.bzl` 消费。
- `tests/verilator_sim/BUILD` 用 `template_rule`（`rules/utils.bzl`）把一份 testbench 模板 + 一份 `core_mini_axi_sim.cc` 源码批量实例化成 8 个变体，差异只在 `VERILATOR_MODEL` 宏与对应的 RTL 模型库。
- CoralNPU 有两条 Verilator 入口：SystemC testbench（`core_mini_axi_sim`，命令行主力，走 TLM↔AXI 桥）与轻量 C++ 库（`hw_sim/`，`CoreMiniAxiWrapper` + `Clock::Step` 直接驱动引脚，供 npusim 调用）。
- DPI backdoor SRAM 用「late binding + 区间匹配」绕过总线高速加载 ELF，并在注册时随机化内容以贴近真实硬件；mailbox 用 master 端口回调捕获内核写出的 16 字节，充当极简输出通道。
- 两条入口的启动序列本质相同（写 PC_START `0x30004` → 写 RESET_CONTROL `0x30000` 先 1 后 0 → 等 `io_halted`），印证 `u3-l5` 的 CSR 启动契约。

## 7. 下一步学习建议

- **`u11-l2` VCS 与 UVM 验证**：本讲只覆盖了 Verilator。下一讲讲商业仿真器 VCS（`--config=vcs`）与 UVM 平台，对比开源 vs 商业仿真流程的差异，并深入 `rvv_backend_tb` 的 agent/scoreboard/coverage 结构。
- **`u11-l3` cocotb 回归测试体系**：本讲提到 `default.vlt.tpl` 与 `coco_tb.bzl` 属于 cocotb 路径却未展开。下一讲系统讲 `tests/cocotb` 的回归体系、`coco_tb.bzl` 如何直接调 Verilator 生成带 VPI 的模型，以及 Python 如何经 VPI 驱动 RTL。
- **继续阅读源码**：想深入 SystemC testbench 内部，可读 `tests/verilator_sim/coralnpu/core_mini_axi_tb.cc`（TLM↔AXI 桥接、`tohost` 半主机、指令追踪的完整实现）；想理解 npusim 如何加载 `.so`，可结合 `u10-l3` 读 `sw/coralnpu_sim/`。

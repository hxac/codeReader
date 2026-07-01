# cocotb 回归测试体系

## 1. 本讲目标

学完本讲，你应当能够：

- 说出 `tests/cocotb` 这套回归测试是如何**按 ISA / CSR / 异常 / RVV / TL-UL 等领域分类**组织的，并能独立统计每一类有多少测试。
- 读懂 `rules/coco_tb.bzl` 里 `verilator_cocotb_model`、`verilator_cocotb_test`、`cocotb_test_suite` 三个宏/规则各自做什么，理解「一个 testcase 列表如何被自动展开成上百个 Bazel 测试目标」。
- 拿着 `tests/cocotb/BUILD`，说明**定义一个 cocotb 测试目标需要哪些要素**（模型、顶层、test_module、ELF 数据、依赖库、simulator 集合）。
- 理解 `sim_test_fixture.py` 里的 `Fixture` 如何把「复位 / 启动时钟 / 加载 ELF / 查符号 / 跑到 halt / 读结果」这套**样板代码**收敛成一个可复用类，以及 `run_binary.py` 如何把同一套思路搬上真实硬件。

本讲是单元 11「验证流程」的第三篇，承接 [u2-l4 cocotb 测试框架入门](u2-l4-cocotb-testbench-intro.md)。u2-l4 讲的是「一个 cocotb 测试怎么写」；本讲讲的是「几百个 cocotb 测试怎么被工程化地组织、构建、过滤与回归」。

## 2. 前置知识

在继续之前，请确认你已理解下面这些概念（本讲不会重复展开，只引用）：

- **cocotb**：一个用 Python 编写硬件仿真测试台的框架。测试函数用 `@cocotb.test()` 装饰，运行在一个 Python 进程里，通过 VPI/VHPI/Fli 接口驱动仿真器（Verilator/VCS）跑 RTL。详见 u2-l4。
- **DUT / toplevel**：被测设计的顶层模块，例如 `CoreMiniAxi`、`RvvCoreMiniAxi`、`TLUL2Axi`。
- **Bazel 包与目标标签**：`//tests/cocotb:core_mini_axi_sim_cocotb` 这种写法，`//tests/cocotb` 是包，冒号后是目标名。详见 [u1-l3 Bazel 构建系统](u1-l3-bazel-build-quickstart.md)。
- **coralnpu_v2_binary**：把 C/C++/汇编编译成 CoralNPU 裸机 `.elf` 的自定义 Bazel 宏。详见 [u2-l2](u2-l2-write-compile-program.md)。
- **`CoreMiniAxiInterface`**：u2-l4 的主角类，扮演外部 AXI 主机，提供 `init / reset / load_elf / lookup_symbol / write / read / execute_from / wait_for_halted` 等方法。本讲的 `Fixture` 正是它的封装。

本讲还会用到两个术语：

- **回归（regression）**：把一大批测试作为一个整体反复跑，确保某次代码改动没有让之前能过的测试失败。CoralNPU 的 cocotb 回归就是「一套 BUILD 里定义的所有 `cocotb_test_suite` 目标」。
- **`TESTBRIDGE_TEST_ONLY`**：Bazel 注入测试进程的环境变量，内容是 `--test_filter` 传进来的过滤串。cocotb 回归靠它实现「只跑某一个 testcase」。

## 3. 本讲源码地图

本讲涉及的关键文件及其职责：

| 文件 | 作用 |
| --- | --- |
| [tests/cocotb/BUILD](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/tests/cocotb/BUILD) | cocotb 回归的「目录总览」：定义 Verilator 模型、列举 testcase 列表、装配十几个测试套件、用 `coralnpu_v2_binary` 产 ELF。 |
| [rules/coco_tb.bzl](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/rules/coco_tb.bzl) | cocotb 的 Bazel 规则与宏：`verilator_cocotb_model` 建模、`verilator_cocotb_test`/`vcs_cocotb_test` 包单测、`cocotb_test_suite` 把 testcase 列表展开成多目标。 |
| [tests/cocotb/build_defs.bzl](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/tests/cocotb/build_defs.bzl) | 仿真器公共编译参数（Verilator 的 `-Wno-*`、VCS 的 `+notimingcheck` 等），被各 BUILD `load` 复用。 |
| [coralnpu_test_utils/sim_test_fixture.py](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/coralnpu_test_utils/sim_test_fixture.py) | `Fixture` 类：把仿真测试台的样板代码（复位/时钟/ELF/符号/跑/读）收成一个可复用对象。 |
| [coralnpu_test_utils/run_binary.py](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/coralnpu_test_utils/run_binary.py) | `BinaryRunner`：把同一套「加载 ELF→启动→等 halt」流程从仿真搬到真实硬件（经 FTDI SPI 主机）。 |
| [coralnpu_test_utils/core_mini_axi_interface.py](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/coralnpu_test_utils/core_mini_axi_interface.py) | `CoreMiniAxiInterface`：`Fixture` 的底层引擎（u2-l4 已详述）。 |

---

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：

1. `tests/cocotb` 回归体系全景：按领域分类统计。
2. `coco_tb.bzl`：cocotb 测试规则三件套（model / test / suite）。
3. `BUILD` 文件如何定义一个 cocotb 测试目标。
4. `sim_test_fixture` 与 `run_binary`：测试辅助库。

### 4.1 tests/cocotb 回归体系全景

#### 4.1.1 概念说明

CoralNPU 的 RTL 验证主力是 cocotb，`tests/cocotb/` 目录就是这套回归的物理载体。它不是一个「一个大测试文件」，而是一个**按验证领域分层、由 ELF 测试程序 + Python 测试台 + Bazel 装配规则三部分组成**的工程体系：

- **ELF 测试程序**：用 `coralnpu_v2_binary` 编译出来的 `.elf`，是被测「输入」。每个 ELF 是一段跑在 CoralNPU 内核上的裸机程序（C/汇编），它要么正常跑完停机，要么触发某种异常。回归靠观察「跑完后的输出」或「是否如期 halt/fault」来判断 RTL 对错。
- **Python 测试台（test_module）**：扮演 SoC 里的「外部主机 CPU」，把 ELF 灌进 DTCM、启动内核、等停机、读结果。一个 `.py` 文件里通常定义几十个 `@cocotb.test()` 测试函数，函数名就是 testcase 名。
- **Bazel 装配规则**：把「用哪个 RTL 顶层」「用哪个 Python 测试台」「跑哪些 testcase」「依赖哪些 ELF」打包成一个可被 `bazel test` 执行的目标。

这套体系最关键的工程价值在于：**测试逻辑（Python）与被测程序（ELF）与硬件配置（RTL 顶层）三者解耦**。同一个 Python 测试台可以挂到标量核 `CoreMiniAxi` 上跑，也可以挂到带 RVV 的 `RvvCoreMiniAxi` 上跑；同一组 ELF 可以被多个测试套件复用。

#### 4.1.2 核心流程

一次 cocotb 回归的执行流程（以 `core_mini_axi_sim_cocotb` 套件为例）：

```
bazel test //tests/cocotb:core_mini_axi_sim_cocotb_<testcase>
   │
   ├── 1. Verilator 把 CoreMiniAxi.sv 编译成可执行仿真模型（verilator_cocotb_model 产物）
   ├── 2. cocotb 启动该模型，加载 test_module = core_mini_axi_sim.py
   ├── 3. 通过 COCOTB_TEST_FILTER (= $TESTBRIDGE_TEST_ONLY) 选出要跑的 testcase 函数
   ├── 4. Python 测试台经 CoreMiniAxiInterface：
   │       reset → clock → load_elf(从 runfiles 取 .elf) → execute_from → wait_for_halted/fault
   └── 5. 测试函数里的 assert 通过/失败 → cocotb 汇报 → Bazel 标记 PASS/FAIL
```

注意第 3 步：Bazel 的 `--test_filter` 串通过环境变量 `TESTBRIDGE_TEST_ONLY` 传给测试进程，而 `coco_tb.bzl` 把它原样塞进 cocotb 的 `COCOTB_TEST_FILTER`，于是「只跑这一个 testcase」就实现了——这是回归能精细到「单条用例」的关键。

#### 4.1.3 源码精读

**ELF 数据聚合**。回归要跑的 ELF 被 `COCOTB_TEST_BINARY_TARGETS` 统一收口：

[tests/cocotb/BUILD:32-52](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/tests/cocotb/BUILD#L32-L52) —— 这段既 `glob` 已有的 `.elf/.o`，又显式列出当前包内的测试程序目标（如 `:wfi_slot_0.elf`、`:zfbfmin_test.elf`），还拉进子包的 filegroup：

- `//tests/cocotb/coralnpu_isa:coralnpu_isa_tests`（ISA 类）
- `//tests/cocotb/exceptions:elf_files`（异常类）
- `//third_party/riscv-tests:all_files`（上游 RISC-V 官方 ISA 自测）

其中异常类 ELF 的产生方式很有代表性——每个 `.cc` 编成一个 `coralnpu_v2_binary`，再聚成 filegroup：

[tests/cocotb/exceptions/BUILD:64-77](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/tests/cocotb/exceptions/BUILD#L64-L77) —— `elf_files` 这个 filegroup 把 9 个异常 ELF（`illegal`、`instr_align_0/1/2`、`instr_fault`、`load_fault_0/1`、`store_fault_0/1`）打包，正是上面对 `//tests/cocotb/exceptions:elf_files` 的引用对象。

**testcase 列表 + 自动生成标记**。每个测试套件的核心就是一个 testcase 列表，例如标量核仿真套件：

[tests/cocotb/BUILD:103-125](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/tests/cocotb/BUILD#L103-L125) —— 注意两个细节：① 列表项可以是字符串（如 `"core_mini_axi_csr_test"`），也可以是 `(name, size)` 元组（如 `("core_mini_axi_basic_write_read_memory", "large")`），后者单独给该条用例指定 Bazel `size`；② 列表被 `# BEGIN_TESTCASES_FOR_core_mini_axi_sim_cocotb` / `# END_TESTCASES_FOR_...` 包裹——这是给自动生成脚本用的「区域标记」，脚本会扫描测试台文件里的 `@cocotb.test()` 函数名，回填到这个区间内。RVV 算术、load/store、汇编等套件都遵循同样的标记约定（如 [tests/cocotb/BUILD:300-326](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/tests/cocotb/BUILD#L300-L326) 的 `RVV_ARITHMETIC_TESTCASES`）。

**测试套件的批量装配**。`core_mini_axi_sim_cocotb` 和 `rvv_core_mini_axi_sim_cocotb` 两个套件共用同一份 `COMMON_TEST_KWARGS`，通过 `template_rule` 一次生成：

[tests/cocotb/BUILD:165-201](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/tests/cocotb/BUILD#L165-L201) —— `template_rule` 是 CoralNPU 自定义的工具宏（见 [rules/utils.bzl](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/rules/utils.bzl)），它接收一个「目标名 → 参数」的字典，对每个条目调用一次 `cocotb_test_suite`，从而用一份配置同时产出标量核与 RVV 核两套回归。`simulators = ["verilator", "vcs"]` 表示同一套用例既跑开源 Verilator，也跑商业 VCS（后者默认被 tag 过滤关闭，需 `--config=vcs`，见 u11-l2）。

#### 4.1.4 代码实践

> **实践目标**：亲手把 `tests/cocotb` 下的回归按 ISA / CSR / 异常 / RVV / TL-UL 五大类统计清楚，建立「领域 → 测试目标」的全景表。

操作步骤：

1. 列出顶层 cocotb 目录的所有测试台 Python 文件与子目录，建立第一层印象：

   ```bash
   git ls-files 'tests/cocotb/*.py' | sed 's#tests/cocotb/##'
   git ls-files 'tests/cocotb/*/BUILD' | sed 's#tests/cocotb/##;s#/BUILD##'
   ```

2. 在 [tests/cocotb/BUILD](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/tests/cocotb/BUILD) 中按 `# BEGIN_TESTCASES_FOR_` 标记逐段统计每个套件的用例数（每行一项，元组算一项）。

3. 按「领域」归类（参考答案见下方小练习）。建议把结果整理成下表（示例骨架，留待你填）：

   | 领域 | 代表套件目标 | 测试台文件 | ELF 来源 |
   | --- | --- | --- | --- |
   | ISA | `core_mini_axi_sim_cocotb`（含 `coralnpu_isa_test`、`riscv_tests`、`riscv_dv`） | `core_mini_axi_sim.py` | `coralnpu_isa`、`third_party/riscv-tests` |
   | CSR | `core_mini_axi_sim_cocotb`（含 `csr_test`、`float_csr_test`、`frm_test`、`minstret_test`） | `core_mini_axi_sim.py` + `csr_test/csr_test_bench.py` | `csr_test` |
   | 异常 | `core_mini_axi_sim_cocotb`（含 `exceptions_test`、`unreachable_prefetch_fault`） | `core_mini_axi_sim.py` | `exceptions:elf_files` |
   | RVV | `rvv_assembly_cocotb_test`、`rvv_load_store_test`、`rvv_arithmetic_cocotb_test`、`rvv_ml_ops_cocotb_test` | `rvv_assembly_cocotb_test.py` 等 | `tests/cocotb/rvv/...` |
   | TL-UL | `tests/cocotb/tlul/` 下各套件（`axi2tlul`、`tlul2axi`、`socket`、`integrity`、`coralnpu_xbar`、`coralnpu_chisel_subsystem` 等） | `tests/cocotb/tlul/*.py` | 多数不需要 ELF |

4. 观察一个被 `glob` 自动收集的 ELF 目标，验证它确实存在：

   ```bash
   # 看 wfi 四个 slot 的 ELF 目标如何由模板批量生成
   git show HEAD:tests/cocotb/wfi_targets.bzl
   ```

需要观察的现象：
- 顶层 `core_mini_axi_sim_cocotb` 是个「大杂烩」套件，把 ISA/CSR/异常等多类用例混在一个列表里——分类是**逻辑归类**，不严格对应「一个 BUILD 目标」。
- 真正按领域独立成目标的是 `rvv_*` 与 `tests/cocotb/tlul/*`，它们各自有自己的 `cocotb_test_suite`。
- TL-UL 类大多**不需要 ELF**（DUT 是总线组件而非完整核），所以 `data` 里看不到 `.elf`，而是直接用 Python 测试台驱动 AXI/TL-UL 接口（见 [tests/cocotb/tlul/BUILD](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/tests/cocotb/tlul/BUILD)）。

预期结果：你会得到一张清晰的「领域—套件—测试台—ELF 来源」对照表，并理解为什么 CoralNPU 把核级回归（一个套件混多类）和总线级回归（每组件一套件）组织成两种不同形态。

> 说明：具体用例条数会随仓库演进而变化，请以你统计时的实际 `# BEGIN/END_TESTCASES_FOR_` 区间为准，不要照搬他人给出的固定数字。

#### 4.1.5 小练习与答案

**练习 1**：`tests/cocotb/tlul/` 下哪个测试套件的 DUT 是整个 SoC 子系统（而非单个总线组件）？它复用了哪个 RTL 测试夹具顶层？

**答案**：是 `coralnpu_chisel_subsystem_cocotb`，DUT 顶层是 `CoralNPUChiselSubsystemTestHarness`，由 `//hdl/chisel/src/soc:coralnpu_chisel_subsystem_testharness_model` 提供（见 [tests/cocotb/tlul/BUILD:408-449](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/tests/cocotb/tlul/BUILD#L408-L449)）。spi/dma/gpio 三个集成测试套件复用了同一个子系统测试夹具顶层，只是 `test_module` 不同。

**练习 2**：为什么 `core_mini_axi_basic_write_read_memory` 在标量核套件里是 `"large"`、在 RVV 套件里却是 `"enormous"`？（对比 [BUILD:105](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/tests/cocotb/BUILD#L105) 与 [BUILD:129](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/tests/cocotb/BUILD#L129)）

**答案**：同一条用例挂在不同 DUT 上跑，预估耗时不同。RVV 核 `RvvCoreMiniAxi` 仿真模型更大、编译/运行更慢，所以这条用例在 RVV 套件里被显式标为 `enormous`，让 Bazel 给它分配更长超时。这正体现了「Python 测试逻辑与硬件配置解耦」带来的灵活性——同一逻辑可按目标差异单独调参。

---

### 4.2 coco_tb.bzl：cocotb 测试规则三件套

#### 4.2.1 概念说明

`rules/coco_tb.bzl` 是 CoralNPU 对 cocotb 的 Bazel 封装。它站在 `@rules_hdl//cocotb:cocotb.bzl` 提供的原始 `cocotb_test` 规则之上，叠了三层「糖」：

| 名称 | 类型 | 职责 |
| --- | --- | --- |
| `verilator_cocotb_model` | rule | 把一个 SystemVerilog 顶层用 Verilator 编译成可执行仿真模型（一个二进制）。 |
| `verilator_cocotb_test` / `vcs_cocotb_test` | macro | 包装 `cocotb_test`：统一注入 cocotb/numpy/pytest 依赖、加 `cpu:2` 标签、注入过滤环境变量。 |
| `cocotb_test_suite` | macro | 顶层入口：按 `simulators` 列表分发，把一个 testcase 列表展开成「每条用例一个测试目标 + 一个汇总元目标」。 |

为什么要叠这三层？因为 cocotb 回归有大量重复结构（每条用例都要同样的依赖、同样的模型、同样的 Python 库），用宏把样板参数化，BUILD 文件才能保持紧凑。

#### 4.2.2 核心流程

`cocotb_test_suite` 的展开逻辑（伪代码）：

```
cocotb_test_suite(name, testcases, simulators=["verilator","vcs"], tests_kwargs=...):
    for sim in simulators:
        1. 从 tests_kwargs 里挑出 "verilator_" / "vcs_" 前缀的专属参数，剥掉前缀
        2. 若 sim == vcs 且 DUT 顶层在 SRAM_BACKDOOR_TOPLEVELS 中：注入 sram_backdoor.cc + dpi_files
        3. 若 coverage 且 sim 是 vcs：追加 -cm line+cond+tgl+branch+assert
        4. for tc in testcases:
               生成目标 name_tc，testcase=[tc]，加 *_cocotb_single_test 标签
        5. 生成元目标 name，带 manual + *_cocotb_test_suite 标签（汇总）
```

关键点：

- **一条 testcase = 一个 Bazel 目标**。所以 `bazel test //tests/cocotb:core_mini_axi_sim_cocotb_core_mini_axi_csr_test` 能精确地只跑 CSR 那一条。
- **元目标带 `manual` 标签**，不会被 `bazel test //...` 默认匹配（避免一次跑几百个长测试），需要显式指定或经 CI 脚本点名。
- **每测 2 个 CPU**：`cpu:2` 标签告诉 Bazel 调度器一个 cocotb 测试预留 2 个核，避免本机并行过多测试互相抢资源把机器拖垮。

#### 4.2.3 源码精读

**① `verilator_cocotb_model`：把 RTL 变成可跑的二进制**。它的实现在 `_verilator_cocotb_model_impl`：

[rules/coco_tb.bzl:39-235](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/rules/coco_tb.bzl#L39-L235) —— 这个 rule 收集 Verilog 源 + C++/DPI 依赖，生成 `.vlt`（Verilator 配置，由模板填充 `{HDL_TOPLEVEL}`，见 [coco_tb.bzl:135-141](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/rules/coco_tb.bzl#L135-L141)），再拼接一条 `verilator -cc --exe --vpi ... && make -j ...` 命令把模型编出来（[coco_tb.bzl:161-207](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/rules/coco_tb.bzl#L161-L207)）。返回一个 `executable`——这个可执行文件就是 cocotb 要驱动的「仿真器」。它还用 `_verilator_resource_estimator` 把 Verilator 编译的 CPU 预留上限钳到 4，让大机器上仍能并行多个编译（[coco_tb.bzl:33-37](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/rules/coco_tb.bzl#L33-L37)）。

**② `verilator_cocotb_test`：包装单测、统一依赖与过滤**：

[rules/coco_tb.bzl:391-451](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/rules/coco_tb.bzl#L391-L451) —— 四个关键动作：

- 第 414 行 `tags.append("cpu:2")`：每测预留 2 CPU。
- 第 426–436 行：把 `deps` 包进一个临时 `py_library`（`name + "_test_data"`），并强制追加 cocotb/numpy/pytest 三个 pip 依赖——这是所有 cocotb 测试的公共底座。
- 第 438–440 行：`extra_env.append("COCOTB_TEST_FILTER=$TESTBRIDGE_TEST_ONLY")`——把 Bazel 的过滤串喂给 cocotb，实现「按 testcase 名过滤」。
- 第 442 行：最终调用上游的 `cocotb_test`，把 `model`（仿真器可执行文件）、`hdl_toplevel`、`test_module`（Python 测试台）传进去。

**③ `cocotb_test_suite`：顶层分发与 testcase 展开**：

[rules/coco_tb.bzl:707-867](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/rules/coco_tb.bzl#L707-L867) —— 这个宏对 `simulators` 列表里的每个仿真器（`verilator` / `vcs` / `vcs_netlist`）分别处理：先把「带仿真器前缀」的参数挑出来归位（[coco_tb.bzl:730-766](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/rules/coco_tb.bzl#L730-L766)），再调用对应的内部展开函数 `_verilator_cocotb_test_suite` / `_vcs_cocotb_test_suite`。其中对 `vcs` 仿真器有一段重要特例：

[rules/coco_tb.bzl:836-855](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/rules/coco_tb.bzl#L836-L855) —— 当 DUT 顶层属于 `SRAM_BACKDOOR_TOPLEVELS`（即需要 SRAM 后门加载的核级顶层），自动给 VCS 编译注入 `-I../hdl/verilog`、`sram_backdoor.cc` 与 `dpi_files` 数据。这正是 u2-l3 / u6-l2 讲过的「DPI backdoor 秒级加载 ELF」在 Bazel 侧的自动接线，**只对需要它的顶层生效**，避免给纯总线测试也塞进这套依赖。

**④ testcase 列表如何变多目标**（以 Verilator 为例）：

[rules/coco_tb.bzl:478-512](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/rules/coco_tb.bzl#L478-L512) —— 循环里对每个 `tc` 调用 `verilator_cocotb_test(name = "{}_{}".format(name, tc), testcase=[tc], ...)` 生成单测目标（第 491–498 行），并打上 `verilator_cocotb_single_test` 标签；循环结束后再生成一个同名元目标（第 507 行，带 `manual` + `verilator_cocotb_test_suite` 标签）。元组形式的 `(tc, size)` 在第 482–487 行被拆开，允许逐条覆盖 `size`。

#### 4.2.4 代码实践

> **实践目标**：用 `coco_tb.bzl` 说清「一个 cocotb 测试目标由哪些要素定义」，并验证 `--test_filter` 的过滤通路。

操作步骤：

1. 打开 [rules/coco_tb.bzl:391-451](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/rules/coco_tb.bzl#L391-L451)，列出 `verilator_cocotb_test` 最终传给 `cocotb_test` 的关键参数：`model`、`hdl_toplevel`、`test_module`、`deps`、`tags`、`extra_env`。这就是「定义要素清单」。

2. 选一条最快的小用例，用 Bazel 的 query 验证它确实被展开成了独立目标（不实际运行，只看目标图）：

   ```bash
   bazel query 'attr(name, "nop_stress_test_nop_stress_test", //tests/cocotb:*)'
   ```

   > 待本地验证：query 结果应包含一个形如 `//tests/cocotb:nop_stress_test_nop_stress_test` 的目标（`<suite_name>_<testcase>`），这正是 [coco_tb.bzl:492](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/rules/coco_tb.bzl#L492) 的命名规则产物。

3. 阅读上游 `cocotb_test` 如何消费 `COCOTB_TEST_FILTER`：在 cocotb 文档里它对应 `TESTNAME`/过滤机制。对应到本仓库，`$TESTBRIDGE_TEST_ONLY` 是 Bazel 在测试进程里注入的环境变量，内容即 `--test_filter` 串。

需要观察的现象：
- 单测目标名严格遵守 `<套件名>_<testcase>` 拼接规则；元目标名等于套件名且带 `manual`。
- `cpu:2` 标签出现在每个展开后的 cocotb 测试目标上（可用 `bazel query --output=build` 查看 `tags`）。

预期结果：你能口头复述「model（仿真器）+ hdl_toplevel（DUT）+ test_module（Python 测试台）+ data（ELF）+ testcase（过滤）+ tags（cpu:2/单测标记）」这六要素，并解释 `--test_filter` 如何经 `TESTBRIDGE_TEST_ONLY → COCOTB_TEST_FILTER` 选出单条用例。

> 说明：`bazel query` 的精确输出取决于本机 Bazel 版本与已获取的外部依赖；若 query 因外部仓库未拉取而报错，可改用 `bazel cquery` 或先跑一次 `bazel sync`。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `verilator_cocotb_test` 要把 `deps` 再包一层 `py_library`（`name + "_test_data"`），而不是直接把依赖传给 `cocotb_test`？

**答案**：因为 cocotb 测试台除了用户自定义依赖，还需要固定的 cocotb/numpy/pytest 三个 pip 依赖，且需要把这些依赖连同 `data`（ELF 等运行时文件）一起作为 runfiles 暴露给测试进程。用一个空 `srcs` 的 `py_library` 当「数据/依赖中转站」，再让 `cocotb_test` 只依赖这一个目标，可以把公共底座集中管理、避免每个测试目标重复声明（见 [coco_tb.bzl:426-436](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/rules/coco_tb.bzl#L426-L436)）。

**练习 2**：若你新增一个 DUT 顶层需要 SRAM 后门加载，要在哪里登记，VCS 路径才会自动注入 `sram_backdoor.cc`？

**答案**：在 `rules/sram_backdoor.bzl` 的 `SRAM_BACKDOOR_TOPLEVELS` 列表里登记该顶层名。`cocotb_test_suite` 在处理 `vcs` 仿真器时会检查 `hdl_toplevel in SRAM_BACKDOOR_TOPLEVELS`（[coco_tb.bzl:838](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/rules/coco_tb.bzl#L838)），命中才追加后门编译参数与 `dpi_files`。

---

### 4.3 BUILD 文件如何定义一个 cocotb 测试目标

#### 4.3.1 概念说明

有了 4.2 的规则三件套，`tests/cocotb/BUILD` 的写法就非常规整——**定义一个 cocotb 测试套件，本质上就是填一张表**：

```
cocotb_test_suite(
    name = "套件名",
    simulators = ["verilator", "vcs"],          # 在哪些仿真器上跑
    testcases = [...],                          # 要跑哪些 @cocotb.test() 函数
    tests_kwargs = {
        "hdl_toplevel": "RvvCoreMiniAxi",       # DUT 顶层
        "test_module": ["xxx_test.py"],         # Python 测试台
        "deps": [...],                          # Python 依赖（接口库、辅助库）
        "data": [...],                          # 运行时数据（ELF）
        "size": "large",                        # 默认规模/超时
    },
    verilator_model = ":rvv_core_mini_axi_model",   # Verilator 仿真器
    vcs_verilog_sources = [...],                # VCS 路径的 RTL 源
    vcs_build_args = VCS_BUILD_ARGS,            # VCS 公共编译参数
    ...
)
```

而 `verilator_model` 本身又由 `verilator_cocotb_model` 提前定义。所以一个完整目标在 BUILD 里通常表现为「**先建模型，再装套件**」两段。

#### 4.3.2 核心流程

定义一个核级 cocotb 套件的两段式：

```
段一：建模型
  verilator_cocotb_model(
      name = "rvv_core_mini_axi_model",
      hdl_toplevel = "RvvCoreMiniAxi",
      verilog_source = "//hdl/chisel/src/coralnpu:RvvCoreMiniAxi.sv",
      deps = ["//hdl/verilog:sram_backdoor"],
  )
       │  产物：一个可执行的 Verilator 仿真器
       ▼
段二：装套件
  cocotb_test_suite(
      name = "rvv_load_store_test",
      verilator_model = ":rvv_core_mini_axi_model",   # 引用段一的模型
      testcases = RVV_LOAD_STORE_TESTCASES,
      tests_kwargs = { hdl_toplevel / test_module / deps / data / size ... },
      simulators = ["verilator", "vcs"],
  )
       │  产物：每条 testcase 一个测试目标 + 一个元目标
       ▼
bazel test //tests/cocotb:rvv_load_store_test_<testcase>
```

#### 4.3.3 源码精读

**段一：模型定义**。核级回归用了四个 Verilator 模型，对应四种 RTL 顶层配置：

[tests/cocotb/BUILD:59-101](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/tests/cocotb/BUILD#L59-L101) —— `core_mini_axi_model`（标量核）、`rvv_core_mini_axi_model`（带 RVV）、`rvv_core_mini_highmem_axi_model`（高内存布局）、`rvv_core_mini_itcm512kb_dtcm512kb_axi_model`（512KB TCM）。四者都依赖 `//hdl/verilog:sram_backdoor`，用同一套 `VERILATOR_BUILD_ARGS`。

**段二：套件装配**。RVV load/store 套件是个标准范例：

[tests/cocotb/BUILD:640-669](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/tests/cocotb/BUILD#L640-L669) —— 注意几个要素：

- `verilator_model = ":rvv_core_mini_axi_model"`：引用段一的模型，**多个套件共享同一个模型**（算术、汇编、ml_ops、load_store 都挂同一个 RVV 模型），避免重复编译。
- `test_module = ["rvv_load_store_test.py"]`：Python 测试台。
- `data = ["//tests/cocotb/rvv/load_store:rvv_load_store_tests"]`：load/store 类的 ELF 集合（见 [4.1.3](#413-源码精读) 的 filegroup 模式）。
- `deps` 里挂了 `//coralnpu_test_utils:rvv_type_util`（向量类型工具，见 u10-l1）、`sim_test_fixture`、`@bazel_tools//tools/python/runfiles`、`requirement("tqdm")`（进度条）。
- `size = "enormous"`：load/store 用例多、跑得久，给足超时。

**公共参数的来源**。`VCS_BUILD_ARGS` / `VCS_TEST_ARGS` / `VCS_DEFINES` / `VERILATOR_BUILD_ARGS` 都来自 [tests/cocotb/build_defs.bzl:20-75](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/tests/cocotb/build_defs.bzl#L20-L75)。这里有几个对理解 RTL 配置很关键的宏定义：`-DZVE32F_ON`（开浮点向量）、`-DVLEN_128`（向量寄存器 128 位）、`-DTB_SUPPORT`、`-DUSE_GENERIC`——它们必须与 Chisel 综合时的参数一致，否则仿真模型与综合网表行为会分叉。

**被测 ELF 的产生**。每个 `.cc`/`.S` 经 `coralnpu_v2_binary` 变成 `.elf`，例如：

[tests/cocotb/BUILD:805-810](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/tests/cocotb/BUILD#L805-L810) —— `zvfbf_test` 由 `zvfbf_test.cc` 编译；紧接着的 `zvfbf_cocotb_test` 套件（[BUILD:812-842](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/tests/cocotb/BUILD#L812-L842)）把它放进 `data`，由 `zvfbf_test.py` 加载运行。这条「源码→ELF→套件 data」的链路是所有需要跑程序的 cocotb 测试的共同骨架。

#### 4.3.4 代码实践

> **实践目标**：把一个现成 cocotb 套件的「定义要素」逐项标注出来，建立可复用的阅读模板。

操作步骤：

1. 打开 [tests/cocotb/BUILD:812-842](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/tests/cocotb/BUILD#L812-L842)（`zvfbf_cocotb_test`，规模小、要素全，适合做样板）。

2. 用下表逐项填入（已给出该套件的答案供核对）：

   | 要素 | 本套件的取值 | 说明 |
   | --- | --- | --- |
   | `name` | `zvfbf_cocotb_test` | 套件/元目标名 |
   | `simulators` | `["verilator","vcs"]` | 双仿真器回归 |
   | `hdl_toplevel` | `RvvCoreMiniAxi` | DUT 顶层 |
   | `test_module` | `zvfbf_test.py` | Python 测试台 |
   | `verilator_model` | `:rvv_core_mini_axi_model` | 共享的 RVV 仿真器 |
   | `data` | `:zvfbf_test.elf` | 被测程序 |
   | `size` | `medium` | 默认超时档 |
   | `deps` | `sim_test_fixture` + runfiles | Python 辅助库 |

3. 追踪 ELF 来源：从 `data` 里的 `:zvfbf_test.elf` 往上找到 [BUILD:805-810](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/tests/cocotb/BUILD#L805-L810) 的 `coralnpu_v2_binary`，再打开 `zvfbf_test.cc` 看程序做了什么；最后打开 `zvfbf_test.py` 看测试台如何加载它、期望什么结果。

需要观察的现象：
- 套件 `name` 与 testcase 拼接后的单测目标名应为 `zvfbf_cocotb_test_zvfbf_test`。
- VCS 路径下，因为 `RvvCoreMiniAxi` 在 `SRAM_BACKDOOR_TOPLEVELS` 中，`cocotb_test_suite` 会自动给它补上 `dpi_files`（你会在 `vcs_data` 里看到 `VCS_COMMON_DATA` 含 `@coralnpu_hw//hdl/verilog:dpi_files`，见 [BUILD:54-57](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/tests/cocotb/BUILD#L54-L57)）。

预期结果：你拥有了一张「读任何 cocotb 套件都通用」的要素表，并能解释每一项如何被 `coco_tb.bzl` 消费。

#### 4.3.5 小练习与答案

**练习 1**：`rvv_arithmetic_cocotb_test`、`rvv_load_store_test`、`rvv_assembly_cocotb_test`、`rvv_ml_ops_cocotb_test` 这四个套件是否各自编译一份 RVV 仿真模型？为什么？

**答案**：不会。它们都引用同一个 `verilator_model = ":rvv_core_mini_axi_model"`（见 [BUILD:668](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/tests/cocotb/BUILD#L668)、[BUILD:700](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/tests/cocotb/BUILD#L700)、[BUILD:419](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/tests/cocotb/BUILD#L419)、[BUILD:775](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/tests/cocotb/BUILD#L775)）。Bazel 把模型当公共产物只编译一次，多个套件复用——这大幅节省回归时间，也是把「模型」与「套件」拆成两段的核心理由。

**练习 2**：为什么 TL-UL 类套件（如 [tests/cocotb/tlul/BUILD:36-61](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/tests/cocotb/tlul/BUILD#L36-L61) 的 `tlul2axi_cocotb_test`）的 `tests_kwargs` 里没有 `data`、也不依赖 `sim_test_fixture`？

**答案**：因为 DUT 是单个总线组件（`TLUL2Axi`），不是完整核，测试台不需要加载 ELF、不需要驱动核启动；它直接用 `TileLinkULInterface` 之类在总线接口上发 Get/Put 事务并检查响应。所以没有 ELF `data`，`deps` 里挂的是总线接口库而非核级 `Fixture`。

---

### 4.4 sim_test_fixture 与 run_binary：测试辅助库

#### 4.4.1 概念说明

回看 u2-l4：写一个 cocotb 测试要重复做一串动作——`init → reset → clock.start → load_elf → lookup_symbol → write 输入 → execute_from → wait_for_halted → read 输出`。如果每个测试函数都把这些原样抄一遍，几百个测试会出现大量重复且易错的样板代码。

`sim_test_fixture.py` 的 `Fixture` 类就是来消灭这些样板的：它把上述生命周期封装成若干方法，让测试函数只剩「加载→跑→读断言」三行业务逻辑。对比 [tests/cocotb/nop_test.py:24-39](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/tests/cocotb/nop_test.py#L24-L39) 与 [core_mini_axi_sim.py:29-40](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/tests/cocotb/core_mini_axi_sim.py#L29-L40) 就能直观看出：前者用 `Fixture` 只有几行，后者直接用 `CoreMiniAxiInterface` 手写 init/reset/clock。

而 `run_binary.py` 是这套思路在**真实硬件**上的对应物：它不再驱动 Verilator 模型，而是经 FTDI SPI 主机把同一套「加载 ELF→启动→等 halt」流程跑在真实 CoralNPU 芯片上。两者共享相同的 CSR 地址约定（`0x30000`）与启动序列，体现了「仿真与硬件同构」的设计。

#### 4.4.2 核心流程

`Fixture` 的标准用法生命周期：

```
fixture = await Fixture.Create(dut)              # 内部：init + reset + clock.start
await fixture.load_elf_and_lookup_symbols(        # 内部：reset + load_elf + lookup_symbol
    elf_path, ['input', 'output', ...])           #   符号地址缓存到 fixture.symbols
await fixture.write('input', data)                # 经符号名写 DTCM
await fixture.run_to_halt()                        # execute_from(entry_point) + wait_for_halted
result = await fixture.read('output', n)           # 经符号名读 DTCM
assert result == expected
```

关键复用点：

- **复位 + 时钟**：`Create` 里一次 `init()` + `reset()` + `start_soon(clock.start())`，所有测试都一致。
- **ELF + 符号**：`load_elf_and_lookup_symbols` 把入口地址存 `self.entry_point`、把符号地址存 `self.symbols`，后续 `write/read` 用符号名而非裸地址，既可读又抗改（链接脚本动也不影响测试）。
- **两种终止语义**：`run_to_halt`（正常跑完）与 `run_to_fault`（预期触发异常），`fault()` 直接读 `io_fault` 端口。

`run_binary.py` 的硬件流程与之同构，只是把 `CoreMiniAxiInterface` 换成了 `FtdiSpiMaster`：

```
BinaryRunner(elf_path, usb_serial, csr_base_addr=0x30000)
  └─ _parse_elf(): 取 e_entry + inference_status_message 符号
  └─ run_binary(): spi_master.load_elf(elf, start_core=True, poll_halt=60s)
```

#### 4.4.3 源码精读

**`Fixture.Create`：构造 + 复位 + 启时钟**：

[coralnpu_test_utils/sim_test_fixture.py:27-36](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/coralnpu_test_utils/sim_test_fixture.py#L27-L36) —— 异步类方法。注意第 29–30 行的 `highmem` 分支：高内存布局下 CSR 基址改为 `0x200000`（默认 `0x30000`），与 u3-l5 / u2-l3 讲的 CSR 区一致。随后 `init → reset → start_soon(clock.start())` 三步把 DUT 带到「时钟在跑、处于复位后稳态」。

**`load_elf_and_lookup_symbols`：ELF 与符号缓存**：

[coralnpu_test_utils/sim_test_fixture.py:38-60](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/coralnpu_test_utils/sim_test_fixture.py#L38-L60) —— 先 `reset()` 再 `load_elf(f)` 拿到入口地址存 `self.entry_point`；然后对每个必查符号调 `lookup_symbol`，找不到就抛错（除非 `optional=True` 或在 `optional_symbols` 里）。这套「符号字典」让上层用名字访问缓冲，是样板复用的精髓。

**按名读写**：

[coralnpu_test_utils/sim_test_fixture.py:63-78](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/coralnpu_test_utils/sim_test_fixture.py#L63-L78) —— `write/read/write_word/read_word/write_ptr` 全部经 `self.symbols[symbol]` 把名字翻译成地址，再委托底层 `CoreMiniAxiInterface`。`write_ptr` 尤其有用：把一个缓冲的地址写进另一个指针变量，模拟「主机传指针给内核」。

**跑到 halt / fault**：

[coralnpu_test_utils/sim_test_fixture.py:80-91](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/coralnpu_test_utils/sim_test_fixture.py#L80-L91) —— `run_to_halt` = `execute_from(entry_point)` + `wait_for_halted`；`run_to_fault` 对应 `wait_for_fault`；`fault()` 直接采样 `dut.io_fault`。两种终止分别服务于「正常程序」与「异常测试」（异常测试期望内核进 trap 而非正常 halt）。

**`run_binary.py`：硬件侧同构流程**：

[coralnpu_test_utils/run_binary.py:43-95](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/coralnpu_test_utils/run_binary.py#L43-L95) —— `BinaryRunner.__init__` 接 `elf_path/usb_serial/csr_base_addr`，`_parse_elf` 用 pyelftools 取 `e_entry` 与可选的 `inference_status_message` 符号。`run_binary`（[run_binary.py:97-124](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/coralnpu_test_utils/run_binary.py#L97-L124)）一次调用 `spi_master.load_elf(..., start_core=True, poll_halt=60.0, ...)`，把「加载→启动→轮询 halt」合并，避免多次子进程开销。它的命令行（[run_binary.py:127-162](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/coralnpu_test_utils/run_binary.py#L127-L162)）提供 `--usb-serial --ftdi-port --csr-base-addr --highmem --verify` 等开关，`--highmem` 同样把 CSR 基址切到 `0x200000`。

> 旁注：`run_binary.py` 头部有一段 `try: import coralnpu_hw except ImportError: ...`（[run_binary.py:28-35](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/coralnpu_test_utils/run_binary.py#L28-L35)），让它能在**没有 Bazel runfiles** 的环境（如直接在装了 FTDI 驱动的开发机上）也能 `python3 run_binary.py ...` 跑起来——这是硬件调试场景常见的用法。

#### 4.4.4 代码实践

> **实践目标**：体会 `Fixture` 如何复用样板，方法是把一个「手写」测试改写成「用 Fixture」的形式（源码阅读型实践，不实际运行）。

操作步骤：

1. 阅读 [tests/cocotb/tutorial/counters/cocotb_counter_test.py:22-38](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/tests/cocotb/tutorial/counters/cocotb_counter_test.py#L22-L38)——这是一个用 `Fixture` 写的标准测试：`Create → load_elf_and_lookup_symbols(['cycle_count_lo', ...]) → run_to_halt → read_word → 拼接打印`。整段不到 20 行。

2. 对比 [tests/cocotb/core_mini_axi_sim.py:28-40](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/tests/cocotb/core_mini_axi_sim.py#L28-L40)——这是早期写法，每个测试函数都自己 `CoreMiniAxiInterface(dut) → init → reset → start_soon(clock.start())`。

3. 在纸上把 `core_mini_axi_basic_write_read_memory` 的前 4 行样板（init/reset/clock）替换成 `fixture = await Fixture.Create(dut)`，体会样板被吞掉的过程。注意：这个特定测试不加载 ELF、只做裸内存读写，所以它**不需要** `load_elf_and_lookup_symbols`，保留手写形式反而更直接——这说明 `Fixture` 适合「跑程序型」测试，纯接口测试用底层 `CoreMiniAxiInterface`/`TileLinkULInterface` 更合适。

4. 解释 `sim_test_fixture` 复用了哪些样板：列出 `Create` 复用了 `init+reset+clock`，`load_elf_and_lookup_symbols` 复用了 `reset+load_elf+lookup_symbol`，`run_to_halt` 复用了 `execute_from+wait_for_halted`。

需要观察的现象：
- 用 `Fixture` 的测试函数体里几乎看不到 `CoreMiniAxiInterface` 这个底层类名，全部通过 `fixture.*` 调用。
- `Fixture` 在 `coralnpu_test_utils/BUILD` 里被声明为 `py_library`（[coralnpu_test_utils/BUILD:89-100](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/coralnpu_test_utils/BUILD#L89-L100)），依赖 `core_mini_axi_sim_interface`——所以测试套件只要在 `deps` 里挂 `//coralnpu_test_utils:sim_test_fixture` 就能用。

预期结果：你能说清 `Fixture` 复用了「复位/时钟/ELF 加载/符号查找/启动/等待终止」六类样板，并判断什么场景该用 `Fixture`、什么场景该退回到底层接口。

#### 4.4.5 小练习与答案

**练习 1**：`Fixture.load_elf_and_lookup_symbols` 在加载 ELF 之前还做了一次 `reset()`（[sim_test_fixture.py:46](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/coralnpu_test_utils/sim_test_fixture.py#L46)），而 `Create` 里已经 `reset()` 过一次了。为什么加载前要再复位一次？

**答案**：因为 `Create` 之后、加载 ELF 之前，测试可能已经做过一些 AXI 读写（或上一个用例留下了状态），内核/总线上可能残留未完成事务。加载前再 `reset()` 确保 DUT 回到干净状态，使 `load_elf` 的 backdoor/frontdoor 写入落到确定的初始内存上，避免脏数据污染。这是一种「关键操作前归零」的防御性写法。

**练习 2**：`run_binary.py` 的 `BinaryRunner` 与仿真侧的 `Fixture` 都使用 `0x30000`（默认）作为 CSR 基址，并都在 `--highmem` 时切到 `0x200000`。这种「仿真与硬件同构」的设计带来什么好处？

**答案**：同一份测试程序（ELF）和同一套启动序列约定（写 `PC_START`→`RESET_CONTROL`→轮询 `STATUS`，见 u3-l5）在仿真和真实硬件上完全一致，意味着仿真里验证通过的行为可直接外推到芯片；调试时也能在两边快速复现问题。`Fixture`（仿真）与 `BinaryRunner`（硬件）是这个同构约定在两端的镜像实现。

---

## 5. 综合实践

> **任务**：为 CoralNPU 「新增一个最小的 cocotb 回归用例」走完从程序到套件的完整链路（设计型实践，以源码阅读 + 编写 BUILD 片段为主；实际编译运行依赖本机工具链，标「待本地验证」处请自行确认）。

背景：假设你刚给标量核加了一个新行为——`minstret` 计数正确性，想给它配一条 cocotb 回归。仓库里其实已有 `minstret_test`（见 [tests/cocotb/BUILD:595-600](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/tests/cocotb/BUILD#L595-L600) 与套件 [BUILD:123](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/tests/cocotb/BUILD#L123)），请你以它为蓝本，把链路补全。

要求完成的步骤：

1. **被测程序**：打开 [tests/cocotb/minstret_test.cc](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/tests/cocotb/minstret_test.cc)，确认它把指令计数结果写到了某个全局符号（例如 `minstret_count`），并说明这个符号会落在哪个 TCM 段（提示：参考 u2-l2 的 `.data`→DTCM 约定）。

2. **Python 测试台**：参照 [tests/cocotb/tutorial/counters/cocotb_counter_test.py](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/tests/cocotb/tutorial/counters/cocotb_counter_test.py)，写一个 `minstret_cocotb_test.py`，用 `Fixture` 完成 `Create → load_elf_and_lookup_symbols(['minstret_count', ...]) → run_to_halt → read_word → assert`。函数名取 `minstret_count_test`。

3. **BUILD 装配**：参照 [tests/cocotb/BUILD:812-842](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/tests/cocotb/BUILD#L812-L842)（`zvfbf_cocotb_test`），写一段 `cocotb_test_suite` 片段，包含：`name`、`simulators=["verilator"]`、`testcases=["minstret_count_test"]`、`tests_kwargs`（hdl_toplevel=`RvvCoreMiniAxi`、test_module、deps 含 `sim_test_fixture`、data 含 `:minstret_test.elf`、size）、`verilator_model=":rvv_core_mini_axi_model"`。

4. **验证目标命名**：根据 [coco_tb.bzl:492](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/rules/coco_tb.bzl#L492)，预测你的单测目标名，并写一条 `bazel test //tests/cocotb:<预测名>` 命令。

5. **过滤通路**：解释若想用 `--test_filter` 只跑你这条用例，`TESTBRIDGE_TEST_ONLY` 如何经 [coco_tb.bzl:439](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/rules/coco_tb.bzl#L439) 的 `COCOTB_TEST_FILTER` 传到 cocotb。

预期结果：你产出了一段可直接粘贴进 `tests/cocotb/BUILD` 的 `cocotb_test_suite` 片段 + 一个 `Fixture` 风格的测试台脚本，并能口头讲清「`.cc` → `coralnpu_v2_binary` → ELF → `data` → `cocotb_test_suite` → 展开成单测目标 → `bazel test`」整条链。

> 说明：步骤 2、3 的具体符号名、断言值取决于 `minstret_test.cc` 的真实实现，请以源码为准；若本机未配齐 RISC-V 工具链与 Verilator，步骤 4 的实际运行为「待本地验证」。

## 6. 本讲小结

- `tests/cocotb` 是 CoralNPU 的 cocotb 回归载体，按 **ISA / CSR / 异常 / RVV / TL-UL** 等领域组织；核级回归常把多类用例混进一个套件（`core_mini_axi_sim_cocotb`），总线级回归则每组件一套件（`tests/cocotb/tlul/*`）。
- 测试体系由三部分解耦组成：**ELF 测试程序**（`coralnpu_v2_binary` 产，filegroup 聚合）、**Python 测试台**（`@cocotb.test()` 函数集合）、**Bazel 装配**（`cocotb_test_suite`）。
- `rules/coco_tb.bzl` 三件套：`verilator_cocotb_model` 把 RTL 编成仿真器，`verilator_cocotb_test` 统一依赖/标签/过滤，`cocotb_test_suite` 把 testcase 列表展开成「每条一个目标 + 一个元目标」，并按仿真器自动注入 SRAM 后门依赖。
- `--test_filter` 经 `TESTBRIDGE_TEST_ONLY → COCOTB_TEST_FILTER` 实现单条用例过滤；单测目标名遵守 `<套件名>_<testcase>`，元目标带 `manual`，每测预留 `cpu:2`。
- `sim_test_fixture.Fixture` 把「复位/时钟/ELF/符号/启动/等待」样板收成一个可复用类，让测试函数只剩业务逻辑；`run_binary.BinaryRunner` 是同一套约定在真实硬件（FTDI SPI）上的镜像，二者共享 CSR 基址与启动序列。
- 模型与套件分两段定义，多个套件共享同一个 `verilator_cocotb_model`，避免重复编译；`build_defs.bzl` 集中管理 Verilator/VCS 公共编译参数与 RTL 宏（`VLEN_128`/`ZVE32F_ON`）。

## 7. 下一步学习建议

- 想看 cocotb 回归如何在**商业 VCS + UVM** 下跑？继续读 [u11-l2 VCS 与 UVM 验证](u11-l2-vcs-uvm.md)，理解 `--config=vcs`、`vcs_testbench_test` 与覆盖率回归。
- 想深入理解 cocotb 测试台底层的 AXI 驱动细节？重读 [u2-l4 cocotb 测试框架入门](u2-l4-cocotb-testbench-intro.md) 与 [coralnpu_test_utils/core_mini_axi_interface.py](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/coralnpu_test_utils/core_mini_axi_interface.py)。
- 想理解 SRAM 后门加载（`sram_backdoor.cc` / `dpi_files`）的 DPI 实现？阅读 [u6-l2 TCM 紧耦合存储与 SRAM](u6-l2-tcm-sram.md) 与 [hdl/verilog/sram_backdoor.cc](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/verilog/sram_backdoor.cc)。
- 想了解 ELF 是怎么编译出来的？回顾 [u2-l1 工具链/CRT/链接脚本](u2-l1-toolchain-linker-tcm.md) 与 [u2-l2 编写并编译程序](u2-l2-write-compile-program.md)。
- 若你对 RTL 顶层（`CoreMiniAxi` / `RvvCoreMiniAxi`）本身感兴趣，进入 [u3-l1 SoC 顶层子系统装配](u3-l1-soc-subsystem.md) 与 [u7-l1 RVV 后端总览](u7-l1-rvv-backend-overview.md)。

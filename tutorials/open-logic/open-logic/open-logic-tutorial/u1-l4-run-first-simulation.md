# 运行第一个仿真（VUnit + GHDL）

## 1. 本讲目标

读完本讲后，你应当能够：

- 说清楚 Open Logic 是如何用 **VUnit** 搭建整套仿真流程的，以及 `sim/run.py` 在其中扮演的角色。
- 掌握 `run.py` 的命令行参数，特别是 `--ghdl` / `--nvc` / `--modelsim` / `--rivierapro` 这一组**互斥**的仿真器开关。
- 理解 `sim/test_configs/` 如何**按区域（base/axi/intf/fix）**组织测试台，并为每个实体批量生成不同 **generic（泛型）组合**。
- 明白为什么在 VUnit 检测文件之前，必须先执行一次 **Python 代码生成**（`codegen`）。
- 亲自跑通一个实体（例如 `olo_base_fifo_sync`）的仿真，并看懂通过/失败结果。

## 2. 前置知识

在进入本讲前，先用通俗语言回顾几个关键概念：

- **仿真（Simulation）**：用软件在 PC 上模拟硬件电路的运行，观察波形和输出，是验证 RTL 代码正确性的主要手段。Open Logic 用仿真而非上板来保证「可信代码」。
- **Testbench（测试台，TB）**：一段只用于仿真、不会综合成硬件的 VHDL 代码。它产生激励（输入信号）、检查输出（断言），用来验证被测实体（DUT，Design Under Test）。Open Logic 约定每个实体都有自己的 TB，放在 `test/<area>/<entity>/<entity>_tb.vhd`。
- **Generic（泛型）**：VHDL 实体在**实例化时**才确定的「参数」，例如 FIFO 深度 `Depth_g`、RAM 行为 `RamBehavior_g`。同一份 TB 通过不同 generic 可以验证实体在不同配置下的行为。
- **VUnit**：一个开源的 VHDL 验证框架（`pip3 install vunit_hdl`）。它做了三件事：① 自动发现并按依赖编译源文件；② 把同一个 TB 用不同 generic 注册成多个「测试用例（test case）」；③ 调用你指定的仿真器（GHDL、NVC、Questa 等）逐个运行并汇总通过/失败。`run.py` 本质上就是一段「配置 VUnit 项目」的 Python 脚本。
- **GHDL / NVC**：两个开源的 VHDL 仿真器。GHDL 是 Open Logic 的**默认**选择，NVC 主要用于覆盖率分析。它们免费，是 Open Logic 能在 CI 里零成本跑回归测试的前提。

本讲承接 [u1-l3](u1-l3-get-and-integrate.md)：上一讲你已经知道全部源码要编译进一个名为 `olo` 的 VHDL 库、依赖顺序由 `compile_order.txt` 规定。本讲告诉你这个库在仿真侧是怎么被组织和运行的。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `sim/run.py` | 仿真流程的**总入口**与 VUnit 项目配置：解析命令行、选择仿真器、收集源文件、注册 TB 配置、启动运行。 |
| `sim/codegen.py` | 仿真前的**代码生成**步骤，为 fix 区域的测试生成一份 VHDL 包（package）。 |
| `sim/test_configs/olo_base.py` | base 区域的 **TB 配置**：为每个 base 实体生成若干 generic 组合。 |
| `sim/test_configs/utils.py` | 提供 `named_config()` 辅助函数，把一组 generic 翻译成一个具名 VUnit 配置。 |
| `test/base/olo_base_fifo_sync/olo_base_fifo_sync_tb.vhd` | `olo_base_fifo_sync` 的测试台，本讲代码实践的观察对象。 |

> 说明：另外三个区域 `olo_axi.py` / `olo_intf.py` / `olo_fix.py` 与 `olo_base.py` 结构相同，本讲以 base 为例讲透，其余可举一反三。

## 4. 核心概念与源码讲解

### 4.1 VUnit 运行器：run.py 的整体结构

#### 4.1.1 概念说明

`sim/run.py` 不是「主程序逻辑」，而是一段**配置脚本**：它告诉 VUnit「源文件在哪、编译进哪个库、每个 TB 要跑哪些配置、用哪个仿真器」，最后调用 `vu.main()` 把控制权交给 VUnit。

理解它的关键在于分清三个阶段：

1. **构建（Build）**：定义命令行参数、创建库、收集源文件、设置编译/仿真选项。
2. **配置（Configure）**：通过 `test_configs/*.py` 把每个 TB 拆成多个 generic 配置。
3. **运行（Run）**：`vu.main()` 让 VUnit 编译并仿真，最后汇总结果。

之所以单独写一个 `run.py` 而不是直接用 VUnit 默认入口，是因为 Open Logic 需要注入一些自定义逻辑（仿真器互斥选择、Riviera-PRO 的 generic 加引号修复、覆盖率后处理、代码生成等）。

#### 4.1.2 核心流程

`run.py` 的执行顺序可以用下面这段伪代码概括：

```
1. codegen_generate()          # 先生成代码（必须在 VUnit 发现文件之前）
2. chdir 到 sim 目录
3. 解析命令行参数（仿真器、--coverage、--compile_list …）
4. 设置环境变量 VUNIT_SIMULATOR（决定用哪个仿真器）
5. VUnit.from_args() → 创建项目，加入 vhdl builtins / com / verification components
6. add_library('olo')    ← 生产源码库
   add_library('olo_tb') ← 测试台库
7. glob 收集 ../src/**/*.vhd 与 en_cl_fix → 进 olo 库
   glob 收集 ../test/**/*.vhd        → 进 olo_tb 库
8. 设置编译/仿真选项（relaxed rules、关闭 IEEE 警告等）
9. for area in [base, axi, intf, fix]: area.add_configs(olo_tb)
10. （可选）--compile_list 导出编译顺序后退出
11. vu.main(post_run=post_run)  # 交给 VUnit 编译并运行
```

注意第 1 步和第 9 步的位置：**代码生成在最开头**（VUnit 还没扫描文件），**TB 配置注册在源文件加入之后**——因为注册配置需要 VUnit 已经「认识」对应的 TB 实体。

#### 4.1.3 源码精读

脚本一开始就把代码生成跑了，并显式注释了原因——生成的文件必须先存在，VUnit 才能发现它们：

[sim/run.py:16-19](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/sim/run.py#L16-L19) —— 在 VUnit 检测文件**之前**调用 `codegen_generate()`，确保生成的包文件已落地。

随后把工作目录切到 `sim`，保证脚本无论从哪里调用、相对路径 `../src` 都能正确解析：

[sim/run.py:26-26](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/sim/run.py#L26) —— `os.chdir` 到脚本自身所在目录（即 `sim/`）。

创建项目后，Open Logic 建了**两个库**——`olo` 装生产源码、`olo_tb` 装测试台，这与上一讲「全部源码编译进库名 `olo`」的约定一致，TB 单独成库便于区分：

[sim/run.py:107-114](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/sim/run.py#L107-L114) —— 加入 VUnit 内置功能并创建 `olo` 与 `olo_tb` 两个库。

源文件的收集全部用 `glob` 递归扫描。注意 `3rdParty/en_cl_fix` 的源码也被加进 `olo` 库，这就是 fix 区域能编译的前提：

[sim/run.py:116-127](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/sim/run.py#L116-L127) —— 收集 src 与 en_cl_fix 进 `olo`，收集 test（含 `tb/legacy` 辅助）进 `olo_tb`。

为了让 GHDL/NVC 能编译 Open Logic 这套较宽松的 VHDL-2008 代码，脚本开启了 relaxed rules 并关闭一些烦人的告警：

[sim/run.py:130-131](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/sim/run.py#L130-L131) —— GHDL 用 `-frelaxed-rules -Wno-hide -Wno-shared`，NVC 用 `--relaxed`。

四个区域的 TB 配置通过一个循环统一注册，这是「按区域组织」的入口：

[sim/run.py:138-139](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/sim/run.py#L138-L139) —— 遍历四个区域模块，调用各自的 `add_configs(olo_tb)`。

最后（在处理完可选的 `--compile_list`、覆盖率等之后），把控制权交给 VUnit：

[sim/run.py:199-200](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/sim/run.py#L199-L200) —— `vu.main(post_run=post_run)` 启动编译与仿真，`post_run` 用于覆盖率后处理。

#### 4.1.4 代码实践

**实践目标**：确认 `run.py` 能被正常解析，并看清它支持的命令行选项。

1. 进入 `sim` 目录，查看帮助：
   ```shell
   cd sim
   python3 run.py -h
   ```
2. 观察输出的参数列表。

**需要观察的现象**：帮助文本里应出现 `--ghdl`（标注为 default）、`--nvc`、`--modelsim`、`--rivierapro`，以及 `--coverage`、`--vhdl_ls`、`--compile_list`。

**预期结果**：`-h` 只是解析参数并打印帮助，不会真正编译或仿真。

**待本地验证**：如果你的环境未安装 `vunit_hdl`，这一步会报 `ModuleNotFoundError: No module named 'vunit'`。请先执行 `pip3 install vunit_hdl`。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `run.py` 要把源码和测试台分别放进 `olo` 和 `olo_tb` 两个库，而不是全部塞进一个库？

**参考答案**：分开后，生产源码（可综合）与测试台（仅仿真）的边界一目了然；同时 TB 可以 `library olo; use olo.xxx;` 引用被测实体，而生产源码不会被测试辅助文件污染。这也让覆盖率分析（第 10 单元会讲）能精确地只统计 `olo` 库里的代码。

**练习 2**：如果删掉 `olo_tb.add_source_files(files)`（第 127 行），仿真会发生什么？

**参考答案**：VUnit 发现不到任何 TB 实体，随后 `add_configs()` 里调用 `olo_tb.test_bench(tb_name)` 会因为找不到实体而抛错。这也解释了为什么 TB 配置注册必须在源文件加入之后。

---

### 4.2 仿真器选择：--ghdl / --nvc / --modelsim / --rivierapro

#### 4.2.1 概念说明

不同的仿真器在功能、许可证和适用场景上不同：

| 仿真器 | 选项 | 是否默认 | 典型用途 |
| --- | --- | --- | --- |
| GHDL | `--ghdl` | ✅ 默认 | 日常回归测试，免费开源 |
| NVC | `--nvc` | ❌ | 覆盖率分析（开源） |
| Modelsim/Questa | `--modelsim` | ❌ | 覆盖率分析、商业场景 |
| Riviera-PRO | `--rivierapro` | ❌ | 不再积极维护，需用户自行维护 |

关键点：这四个开关构成一个**互斥组（mutually exclusive group）**——一次只能选一个仿真器；并且通过环境变量 `VUNIT_SIMULATOR` 把选择告诉 VUnit。

#### 4.2.2 核心流程

```
解析参数
  ↓
若环境变量 VUNIT_SIMULATOR 未被外部预设：
  根据 --modelsim/--nvc/--rivierapro/--ghdl 设置 simulator
  写入 os.environ['VUNIT_SIMULATOR']
  ↓
（Riviera-PRO 专属修复：给含 ( ) , 的 generic 值加引号）
  ↓
若 --coverage 但 simulator ∉ {modelsim, nvc}：直接抛异常
```

之所以用「环境变量 + 命令行开关」双重机制，是为了允许 CI 等外部环境直接预设 `VUNIT_SIMULATOR`，而不必改命令行。

#### 4.2.3 源码精读

四个仿真器开关被放进同一个互斥组，GHDL 设为 `default=True`：

[sim/run.py:28-53](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/sim/run.py#L28-L53) —— 互斥的仿真器选项，`--ghdl` 默认为真。

选择逻辑只在环境变量未被外部预设时介入，并明确把 Riviera-PRO 标为「不再积极维护」：

[sim/run.py:76-87](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/sim/run.py#L76-L87) —— 根据开关把 `ghdl`/`nvc`/`modelsim`/`rivierapro` 写入 `VUNIT_SIMULATOR`。

随后是一段针对 Riviera-PRO 的「打补丁」：VUnit 原生只在值含空格时加引号，而 Riviera-PRO 会把未加引号的 `(1,8,4)` 误解成 VHDL 聚合而非字符串，导致回退到默认值。这段代码猴子补丁（monkey-patch）了 `format_generic`：

[sim/run.py:89-100](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/sim/run.py#L89-L100) —— 给含 `(`、`)`、`,` 的 generic 值强制加引号，修复 Riviera-PRO 的解析问题。

覆盖率只能在 Modelsim/Questa 或 NVC 下开启，否则直接报错——这是硬性约束：

[sim/run.py:102-104](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/sim/run.py#L102-L104) —— `--coverage` 仅允许与 `--modelsim` 或 `--nvc` 同时使用。

#### 4.2.4 代码实践

**实践目标**：用两个不同仿真器跑同一个最小测试，体会切换方式。

1. 用默认 GHDL 跑一个 TB（先编译可能需要几分钟）：
   ```shell
   cd sim
   python3 run.py olo_tb.olo_base_fifo_sync_tb -p 4
   ```
   这里 `olo_tb.olo_base_fifo_sync_tb` 是 VUnit 的**测试名过滤器**，只跑这一个 TB；`-p 4` 表示用 4 个线程并行加速（来自 VUnit 自带 CLI）。
2. 改用 NVC 跑同一个 TB：
   ```shell
   python3 run.py olo_tb.olo_base_fifo_sync_tb --nvc -p 4
   ```

**需要观察的现象**：两次运行都应该在结尾打印类似 `pass=... fail=0` 的汇总；两次都应全部通过。注意切换仿真器后 VUnit 会重新编译一次（因为编译产物按仿真器区分）。

**预期结果**：`fail=0`，全部通过。

**待本地验证**：取决于你是否同时装了 GHDL 和 NVC。若只装了 GHDL，则第二条命令会报找不到 `nvc`。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `--coverage` 不允许和默认的 `--ghdl` 一起用？

**参考答案**：GHDL 本身不支持收集语句/分支覆盖率（coverage），Open Logic 的覆盖率流程依赖 Modelsim/Questa 的 `+cover=bs` 或 NVC 的原生覆盖率。脚本在运行前就用异常把这种不兼容组合挡住，避免跑完一大圈才发现没有覆盖率数据。

**练习 2**：如果想强制用一个仿真器，但不通过命令行，可以怎么做？

**参考答案**：在运行 `run.py` 之前预设环境变量 `VUNIT_SIMULATOR`（例如 `export VUNIT_SIMULATOR=ghdl`）。脚本第 76 行的 `if 'VUNIT_SIMULATOR' not in os.environ:` 会跳过自身的设置逻辑，直接尊重外部预设。

---

### 4.3 test_configs 按区域组织测试与 generic 组合

#### 4.3.1 概念说明

Open Logic 的每个实体都有多个泛型（generic），例如 FIFO 的 `Depth_g`、`RamBehavior_g`、`ReadyRstState_g`。要保证「可信」，光跑一组配置是不够的，必须**系统地遍历各种组合**。

但把这些组合写在 `run.py` 里会让它过于臃肿。于是 Open Logic 把它们拆到 `sim/test_configs/` 下，**每个区域一个文件**：

- `olo_base.py` —— base 区域（最多，base 是地基）
- `olo_axi.py` —— axi 区域
- `olo_intf.py` —— intf 区域
- `olo_fix.py` —— fix 区域

每个文件暴露一个统一的 `add_configs(olo_tb)` 函数，被 `run.py` 在循环里调用。这样 `run.py` 保持精简，而每个区域的配置可以独立维护、独立 diff。

`utils.py` 里的 `named_config()` 则是把「一组 generic」翻译成「一个具名配置」的公共工具，所有区域文件都用它。

#### 4.3.2 核心流程

以 `olo_base_fifo_sync_tb` 为例，看 `olo_base.py` 如何为它生成配置：

```
tb = olo_tb.test_bench('olo_base_fifo_sync_tb')   # 取出已注册的 TB
for RamBehav in ['RBW', 'WBR']:
    named_config(tb, {'RamBehavior_g': RamBehav})  # 每种 RAM 行为一个配置
for RstState in [0, 1]:
    named_config(tb, {'ReadyRstState_g': RstState})
for Depth in [32, 128]:
    named_config(tb, {'Depth_g': Depth})
for AlmFull in [True, False]:
    for AlmEmpty in [True, False]:
        named_config(tb, {"AlmFullOn_g": AlmFull, "AlmEmptyOn_g": AlmEmpty})
```

`named_config(tb, map)` 内部做的事：

```
cfg_name = "key1=val1-key2=val2-..."      # 用 generic 名=值拼成配置名
tb.add_config(name=cfg_name, generics=map) # 注册到 VUnit
```

于是 VUnit 最终会用形如 `olo_tb.olo_base_fifo_sync_tb.RamBehavior_g=RBW.<testcase>` 的全名来寻址一个具体的运行实例。

#### 4.3.3 源码精读

`olo_base.py` 的导入与统一入口函数签名——所有区域文件都遵守同样的 `add_configs(olo_tb)` 约定，这是 `run.py` 能在循环里统一调用它们的原因：

[sim/test_configs/olo_base.py:10-20](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/sim/test_configs/olo_base.py#L10-L20) —— 从 `utils` 导入 `named_config`，定义 `add_configs(olo_tb)`。

FIFO（含 sync 与 async）的配置块，清晰地展示了「按维度循环 → 每个组合一个 `named_config`」的模式：

[sim/test_configs/olo_base.py:76-91](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/sim/test_configs/olo_base.py#L76-L91) —— 为 `olo_base_fifo_sync_tb` / `olo_base_fifo_async_tb` 遍历 RAM 行为、复位状态、深度、几乎满/空开关。

注意 sync FIFO 还多了一组「完全奇数深度」`Depth=53` 的配置，用来验证任意深度都可用：

[sim/test_configs/olo_base.py:86-88](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/sim/test_configs/olo_base.py#L86-L88) —— sync FIFO 额外测试奇数深度 53。

`named_config` 的实现非常简短，是理解所有配置命名规则的关键：

[sim/test_configs/utils.py:15-21](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/sim/test_configs/utils.py#L15-L21) —— 用 `key=value` 以 `-` 连接生成配置名，再调用 VUnit 的 `tb.add_config()`。

至于 TB 内部如何区分不同测试用例，看 `olo_base_fifo_sync_tb.vhd`：它在实体上方声明了 VUnit 的 `run_all_in_same_sim`，并用 `run("...")` 分发到多个独立用例：

[test/base/olo_base_fifo_sync/olo_base_fifo_sync_tb.vhd:25-35](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/test/base/olo_base_fifo_sync/olo_base_fifo_sync_tb.vhd#L25-L35) —— TB 实体声明，含 `runner_cfg` 泛型与 `-- vunit: run_all_in_same_sim` 注释。

[test/base/olo_base_fifo_sync/olo_base_fifo_sync_tb.vhd:115-148](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/test/base/olo_base_fifo_sync/olo_base_fifo_sync_tb.vhd#L115-L148) —— `test_runner_setup` 后用 `run("Reset")` / `run("TwoWordsWriteAndRead")` 分发不同用例。

> **三层命名总结**：一个完整的运行实例由「库.TB.配置.用例」四段构成，例如 `olo_tb.olo_base_fifo_sync_tb.RamBehavior_g=RBW.AlmostFlags`。其中「配置」来自 `named_config` 的 generic 映射，「用例」来自 TB 内的 `run("...")`。

#### 4.3.4 代码实践

**实践目标**：跑通 `olo_base_fifo_sync` 的全部配置，并精确运行其中某一个配置。

1. 跑整个 TB（会跑它所有的 generic 配置与用例，可能较慢）：
   ```shell
   cd sim
   python3 run.py olo_tb.olo_base_fifo_sync_tb -p 4
   ```
2. 精确到单个配置（用通配或全名）：
   ```shell
   python3 run.py "olo_tb.olo_base_fifo_sync_tb.RamBehavior_g=RBW" -p 4
   ```
   引号是为了防止 shell 把 `=` 当作特殊字符。

**需要观察的现象**：终端会先列出所有匹配到的测试实例（每个配置一行），然后逐个编译、仿真，最后打印汇总，例如：

```
List of files to compile:
...
Running test: olo_tb.olo_base_fifo_sync_tb.RamBehavior_g=RBW.AlmostFlags
...
==== Summary =======================================================
pass 24  (100.0 %)
fail  0   (0.0 %)
```

**预期结果**：`fail 0`，全部通过。

**待本地验证**：精确配置名取决于 `named_config` 拼出的字符串；若上面的全名不匹配，可先用不带配置后缀的 `olo_tb.olo_base_fifo_sync_tb` 让 VUnit 列出全部实例名，再复制你想要的那一行。

#### 4.3.5 小练习与答案

**练习 1**：为什么 Open Logic 要把四个区域的配置拆成四个 `.py` 文件，而不是都堆在 `run.py` 里？

**参考答案**：① `run.py` 保持精简，职责单一；② 每个区域的配置可以独立维护、独立做 code review 和 diff；③ 区域之间天然解耦，新增一个区域只需加一个文件并在 `run.py` 的循环里加一项。

**练习 2**：给定 `named_config(tb, {'Depth_g': 32, 'RamBehavior_g': 'RBW'})`，VUnit 里对应的配置名是什么？

**参考答案**：`Depth_g=32-RamBehavior_g=RBW`（字典项的拼接顺序在 Python 3.7+ 等于插入顺序）。该配置下的某个用例全名形如 `olo_tb.olo_base_fifo_sync_tb.Depth_g=32-RamBehavior_g=RBW.AlmostFlags`。

---

### 4.4 代码生成前置步骤：codegen_generate()

#### 4.4.1 概念说明

Open Logic 的 fix（定点）区域依赖一个用 Python 定义、再生成成 VHDL/Verilog 的「常量包」机制。在仿真里，有一份测试用的包文件 `pkg_writer_test_pkg` 是由 Python 脚本**动态生成**的。

问题在于：VUnit 在启动时会扫描源文件目录，自动发现所有 `.vhd`。如果这份包文件还没生成，VUnit 要么找不到它、要么引用一个旧版本。因此 `run.py` 必须在 VUnit 发现文件**之前**先跑一次代码生成。

这就是 `run.py` 开头那句 `codegen_generate()` 存在的原因——它不是可选的预处理，而是仿真能否正确编译的前置依赖。

#### 4.4.2 核心流程

```
run.py 启动
  ↓
import codegen (来自 sim/codegen.py)
  ↓
codegen.generate() 执行：
  1. chdir 到 sim 目录
  2. 把 src/fix/python 加入 sys.path，导入 olo_fix_pkg_writer 与 en_cl_fix
  3. 创建 pkg_writer，添加若干常量/向量（int/float/FixFormat/string，含 as_string 变体）
  4. write_vhdl_pkg("pkg_writer_test_pkg", "../test/fix/olo_fix_pkg_writer", olo_library="olo")
     → 在 test/fix/olo_fix_pkg_writer/ 下生成 VHDL 包文件
  ↓
VUnit.from_args() → glob 扫描源文件（此时生成的包文件已存在）
```

关键时序：**codegen → VUnit 扫描 → add_configs → main**。代码生成只能在前两步之间。

#### 4.4.3 源码精读

`run.py` 顶部的调用，注释明确解释了顺序约束：

[sim/run.py:16-19](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/sim/run.py#L16-L19) —— 「Code-generator tests must generate code before VUnit detects files」。

`codegen.py` 把 fix 的 Python 目录加进 `sys.path`，从而能导入 `olo_fix_pkg_writer` 与 `en_cl_fix_pkg`：

[sim/codegen.py:7-10](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/sim/codegen.py#L7-L10) —— 把 `../src/fix/python` 加入模块搜索路径。

`generate()` 用 `olo_fix_pkg_writer` 添加一批演示用的常量与向量，并最终写出 VHDL 包到测试目录：

[sim/codegen.py:16-39](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/sim/codegen.py#L16-L39) —— 定义并生成 `pkg_writer_test_pkg`，目标目录是 `../test/fix/olo_fix_pkg_writer`，库名为 `olo`。

> 这一步的深入原理（Python 定义如何变成 HDL 常量、`as_string` 的作用、Verilog 互操作）会在 [u8-l4](u8-l4-python-codegen-pkg-writer.md) 详讲。本讲你只需记住：**仿真启动前会跑一次代码生成，且必须在 VUnit 扫描文件之前**。

#### 4.4.4 代码实践

**实践目标**：单独运行代码生成，亲眼看到它产出 VHDL 文件。

1. 单独执行 `codegen.py`：
   ```shell
   cd sim
   python3 codegen.py
   ```
2. 用 git 看它生成了哪些文件：
   ```shell
   cd ..
   git status
   ```

**需要观察的现象**：`git status` 里应在 `test/fix/olo_fix_pkg_writer/` 下出现新增（或被修改）的 `.vhd` 包文件（例如包含 `ConstInt_c`、`VectorInt_c` 等常量定义）。

**预期结果**：生成成功，无报错。

**待本地验证**：如果 `3rdParty/en_cl_fix` 子模块未拉取（克隆时漏了 `--recursive`），`from en_cl_fix_pkg import *` 会失败。请先按 [u1-l3](u1-l3-get-and-integrate.md) 补全子模块。

#### 4.4.5 小练习与答案

**练习 1**：如果把 `run.py` 里的 `codegen_generate()` 移到 `vu.main()` 之前、`add_configs` 之后（即最后才生成代码），会发生什么？

**参考答案**：VUnit 在 `VUnit.from_args()` / `add_source_files()` 阶段就已经扫描了源文件树，此时生成的包文件不会被纳入本次编译，依赖该包的 TB 会编译失败（找不到 `pkg_writer_test_pkg`）。所以代码生成必须在 VUnit 检测文件之前完成。

**练习 2**：`write_vhdl_pkg(..., olo_library="olo")` 中的 `olo_library` 参数有什么用？

**参考答案**：它告诉生成器把生成的包文件登记到 `olo` 库（与生产源码同库），这样测试台和被测实体都能 `use olo.pkg_writer_test_pkg;`。这呼应了上一讲「全部源码编译进库名 `olo`」的约定。

---

## 5. 综合实践

把本讲的四个模块串起来，完成一次「端到端」的仿真体验：

1. **环境准备**（按 [doc/HowTo.md](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/doc/HowTo.md#L463-L484) 的说明）：安装 Python 3、`pip3 install vunit_hdl`、安装 GHDL 并加入 `PATH`。
2. **进入 sim 目录，先生成代码**：`python3 codegen.py`，用 `git status` 确认包文件已生成。
3. **用默认 GHDL 跑一个实体**：
   ```shell
   python3 run.py olo_tb.olo_base_fifo_sync_tb -p 4
   ```
   记录最终汇总里的 `pass` / `fail` 数量。
4. **看波形（可选）**：选一个具体实例，加 `--gui` 打开 GTKWave：
   ```shell
   python3 run.py "olo_tb.olo_base_fifo_sync_tb.RamBehavior_g=RBW.AlmostFlags" --gui
   ```
   （精确实例名以第 3 步打印的列表为准。）
5. **换一个仿真器再跑一次**（若已装 NVC）：
   ```shell
   python3 run.py olo_tb.olo_base_fifo_sync_tb --nvc -p 4
   ```
   对比两次结果是否都 `fail=0`。

**交付物**：一份简短记录，包含你实际执行的命令、最终 `pass/fail` 汇总，以及你对「为什么代码生成必须在 VUnit 扫描文件之前」的一句话解释。

> 待本地验证：本实践依赖本地安装的仿真器；若暂无环境，可先只做第 2 步（代码生成）和阅读，把第 3 步以后标注为「待本地验证」。

## 6. 本讲小结

- `sim/run.py` 是一段 **VUnit 配置脚本**，完成「构建库 → 收集源文件 → 注册 TB 配置 → 启动运行」三件事，最后由 `vu.main()` 接管。
- 源码进 `olo` 库、测试台进 `olo_tb` 库，两个库分离生产代码与测试代码；`en_cl_fix` 子模块的源码也被纳入 `olo` 库。
- 仿真器通过一组**互斥**的 `--ghdl`(默认) / `--nvc` / `--modelsim` / `--rivierapro` 开关选择，并写入环境变量 `VUNIT_SIMULATOR`；`--coverage` 仅允许与 `--modelsim` 或 `--nvc` 同用。
- TB 配置**按区域**拆到 `test_configs/olo_base.py` 等，每个文件用 `named_config()` 把 generic 组合翻译成具名配置；一个运行实例的全名是「库.TB.配置.用例」。
- 仿真启动前必须先 `codegen_generate()`，因为 fix 区域的测试依赖 Python 动态生成的 VHDL 包，而它必须在 VUnit 扫描文件之前就位。
- 运行单个实体用测试名过滤器，如 `python3 run.py olo_tb.olo_base_fifo_sync_tb`，`-p N` 并行加速，`--gui` 看波形。

## 7. 下一步学习建议

- **想读懂一个具体的 base 实体**：进入第 2 单元，先看 [u2-l1](u2-l1-base-packages.md)（base 包体系）和 [u2-l2](u2-l2-pipeline-stage-handshake.md)（流水线阶段与 AXI-S 握手），它们是后续所有实体的基础。
- **想理解同步 FIFO 本身**（本讲的实践对象）：直接读 [u2-l4](u2-l4-sync-fifo.md)，它讲解 `olo_base_fifo_sync` 的接口、几乎满/几乎空与 `ReadyRstState_g`。
- **想深入验证体系**：跳到第 10 单元 [u10-l1](u10-l1-vunit-tb-and-vcs.md)（VUnit 测试台结构与验证组件）和 [u10-l2](u10-l2-sim-runner-codegen-config.md)（仿真运行器与测试配置），那里会系统讲解 TB 结构、验证组件 VC 与覆盖率。
- **想搞清楚代码生成**：第 8 单元 [u8-l4](u8-l4-python-codegen-pkg-writer.md) 详解 `olo_fix_pkg_writer` 的 Python → HDL 生成机制。

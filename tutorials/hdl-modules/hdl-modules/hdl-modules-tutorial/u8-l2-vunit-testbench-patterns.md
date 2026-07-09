# VUnit 测试台模式与 generic 矩阵

## 1. 本讲目标

学完本讲后，你应该能够：

- 理解 `tools/simulate.py` 是如何把每个 `module_*.py` 的 `setup_vunit` 串起来，最终生成一大批仿真用例的。
- 在 `setup_vunit` 里用**嵌套循环**与 **`add_vunit_config`** 把若干个 generic 的取值做笛卡尔积，批量登记测试配置。
- 理解「按测试名（test name）定制 generic」这种分发模式：用测试名字符串里是否包含某个关键词，来决定要不要给这组配置额外塞 generic。
- 读懂一个 VUnit 测试台（`tb_*.vhd`）的断言式自检骨架：`runner_cfg` 魔法 generic、`test_runner_setup` / `test_runner_cleanup`、看门狗 `test_runner_watchdog`、以及 `run("...")` 多用例分支或并发进程自检两种写法。
- 认识 Python 测试侧的 `test/conftest.py` 与 `tools/tools_pythonpath.py` 约定，明白它们为什么必须在其它 import 之前执行。

本讲是验证方法论的第一步，承接 u1-l4（`module_*.py` 的两个钩子 `setup_vunit` 与 `get_build_projects`），把「仿真钩子」这一侧讲透；资源回归这一侧（`get_build_projects` + `build_result_checker`）留给下一讲 u8-l3。

## 2. 前置知识

在进入源码前，先建立几个直觉。

### 2.1 什么是 VUnit

VUnit 是一个开源的 VHDL/SystemVerilog 验证框架。它的核心思想是：**测试台自己判断对错，而不是靠人眼波形**。每个测试台带一个名为 `runner_cfg` 的特殊 generic（字符串），VUnit 通过它把「测试名、随机种子、输出路径」等信息在运行时注入测试台。测试台据此决定跑哪个用例、把结果写到哪。

VUnit 的 `run_pkg` 提供了 `run("用例名")` 函数，测试台主体用 `if run("a") then ... elsif run("b") then ...` 的串行结构来选择当前要跑的用例。`check_pkg` 提供 `check_equal`、`check_relation` 等断言函数——一旦断言失败，该测试立即判负并把错误信息汇总到 VUnit 的报告中。

### 2.2 generic 是「编译期裁剪」的开关

在 hdl-modules 里，几乎每个实体都用 generic 来开关功能（见 u1-l1、u4-l1）。例如 `fifo` 的 `enable_last`、`enable_packet_mode`、`enable_drop_packet`、`enable_peek_mode`、`enable_output_register` 全是布尔 generic。不开的特性对应的 `generate` 块在综合时会被删除，零资源占用。

这带来一个验证上的直接后果：**一个实体有 N 个布尔 generic，就有 \(2^N\) 种配置**。要保证每种组合都不出错，就得把每种组合都仿真一遍。手写每种配置太蠢，于是项目用 Python 在 `setup_vunit` 里**自动展开** generic 矩阵。

### 2.3 一个 generic 矩阵有多大

设一个测试台有 \(k\) 个 generic，每个有 \(n_i\) 种取值，则组合数为：

\[
\text{configs} = \prod_{i=1}^{k} n_i
\]

例如 `tb_handshake_pipeline` 有 3 个布尔 generic（`full_throughput`、`pipeline_control_signals`、`pipeline_data_signals`），理论上有 \(2^3 = 8\) 种组合，再乘以该测试台里的用例数。下文会看到项目用 `itertools.product` 精确地展开这个乘积，并用 `continue` 跳过非法组合。

### 2.4 关键术语

- **generic 矩阵**：多个 generic 取值的笛卡尔积，每组取值登记成一个仿真配置。
- **`add_vunit_config`**：tsfpga `BaseModule` 提供的方法，把「一组 generic 取值」登记为一个待跑的仿真配置。
- **`runner_cfg`**：VUnit 的魔法 generic，运行时注入测试名、种子等。
- **断言式自检（self-checking）**：测试台内部用 `check_*` / `assert` 判断结果，无需人工看波形。
- **看门狗（watchdog）**：给每个测试一个最长挂钟时间，超时即判负，防止死锁测试把 CI 卡死。

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| `modules/fifo/module_fifo.py` | fifo 模块的 `setup_vunit`，展示嵌套循环 + 生成器函数展开 generic 矩阵，以及「按测试名定制 generic」。 |
| `modules/common/module_common.py` | common 模块的 `setup_vunit`，用私有 `_setup_*_tests` 方法分发，内含 `itertools.product` 全组合展开与 `count` 参数的典型用法。 |
| `modules/resync/test/tb_resync_twophase.vhd` | 一个不使用 `run()` 分支、而是用并发进程自检 + `assert` 断言的 VUnit 测试台范例。 |
| `modules/fifo/test/tb_fifo.vhd` | 一个使用 `run("...")` elsif 链、含多个命名用例 + `check_equal/check_relation` 的测试台范例（作为对照）。 |
| `test/conftest.py` | Python 测试侧入口约定：在所有 import 之前修正 `PYTHONPATH`。 |
| `tools/simulate.py` | 仿真总入口：扫描 `modules/`、调用每个模块的 `setup_vunit`、启动 VUnit。 |

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：① generic 矩阵的生成（Python 侧），② 按测试名定制 generic，③ VUnit 断言式自检 testbench（VHDL 侧），④ Python 测试侧的路径约定。

---

### 4.1 generic 矩阵的生成：嵌套循环与 `add_vunit_config`

#### 4.1.1 概念说明

回顾 u1-l4：每个 `module_*.py` 里的 `Module` 类继承自 tsfpga 的 `BaseModule`，其中 `setup_vunit(self, vunit_proj, **kwargs)` 是仿真钩子。它的职责**不是**写测试逻辑，而是**声明「这个模块有哪些测试台、每个测试台要跑哪些 generic 配置」**。

展开 generic 矩阵的套路是固定的：拿到测试台对象 → 用若干层 `for` 循环遍历每个 generic 的候选取值 → 把每一组取值打包成 dict → 调用 `self.add_vunit_config(test, generics=...)` 登记一次。`add_vunit_config` 内部会：把 generic dict 交给 VUnit、生成一个唯一的运行名、并（可选地）为随机化测试复制若干份不同种子的配置。

#### 4.1.2 核心流程

以 `tb_asynchronous_fifo` 为例，展开流程是：

```text
取 library = vunit_proj.library(self.library_name)
for test in 取出 tb_asynchronous_fifo 的所有用例:
    for enable_output_register in [False, True]:          # 第 1 个 generic
        for read_clock_is_faster in [False, True]:        # 第 2 个 generic
            打包 original_generics
            for generics in 按测试名进一步定制并扩展深度:   # 生成器可能 yield 多组
                add_vunit_config(test, generics=generics)  # 登记一次
```

两层布尔循环产生 4 组 `(enable_output_register, read_clock_is_faster)`，再被生成器扩展成多组深度，最终每组都登记为一个仿真配置。

#### 4.1.3 源码精读

先看 `module_fifo.py` 的 `setup_vunit` 开头与 `tb_asynchronous_fifo` 的矩阵：先取库名，再用嵌套循环展开两个 generic，最后用生成器函数 `generate_common_fifo_test_generics` 做按测试名定制并扩展深度——

[modules/fifo/module_fifo.py:L33-L51](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/fifo/module_fifo.py#L33-L51)

上面这段做了三件事：

1. `vunit_proj.library(self.library_name)` 取到当前模块对应的 VHDL 库（库名等于模块名，见 u1-l2）。
2. `library.test_bench("tb_asynchronous_fifo").get_tests()` 取出该测试台里的**所有用例**（一个 `tb_*.vhd` 可以含多个用例，见 4.3 节）。
3. 两层 `for` + 生成器 + `add_vunit_config` 把每个用例、每种 generic 组合都登记一次。

接着看 `tb_fifo` 的矩阵，注意里面有一个**跳过非法组合**的 `continue`——peek 模式不支持 output register：[modules/fifo/module_fifo.py:L53-L63](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/fifo/module_fifo.py#L53-L63)

这条 `if enable_output_register and "peek_mode" in test.name: continue` 是 generic 矩阵里很常见的写法：笛卡尔积会把所有组合都列出来，但其中一些组合在硬件上互斥或无意义，用 `continue` 把它们剔除。这保证了「登记进 VUnit 的每一组配置都是合法且可综合的」。

再看一个更「纯」的笛卡尔积展开。`module_common.py` 把测试设置拆成 12 个私有 `_setup_*_tests` 方法分发（见 4.1.4 的实践任务），其中 `_setup_handshake_pipeline_tests` 用 `itertools.product` 一次性展开三个布尔 generic 的全组合——

[modules/common/module_common.py:L122-L148](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/common/module_common.py#L122-L148)

`itertools.product([False, True], [False, True], [False, True])` 等价于三层嵌套循环，直接产出 8 组三元组。随后两处 `continue`：

- `if full_throughput and pipeline_control_signals and (not pipeline_data_signals): continue` —— 对应 VHDL 实体里 `assert` 禁用的「满吞吐但只流水控制信号」组合（见 u2-l1），这里在 Python 侧就提前剔除，免得跑一个注定失败的仿真。
- `if "full_throughput" in test.name and (not full_throughput): continue` —— 名字叫 `full_throughput` 的用例只在 `full_throughput=True` 时才有意义。

这两个 `continue` 体现了一个重要原则：**非法/无意义的组合要在登记前就过滤掉，而不是登记后让它在仿真里崩**。

#### 4.1.4 代码实践

1. **实践目标**：理解 `setup_vunit` 的「分发 + 嵌套循环」骨架。
2. **操作步骤**：
   - 打开 `modules/common/module_common.py`，找到 `setup_vunit` 方法（第 33 行起）。
   - 数一数它调用了多少个 `self._setup_*_tests(...)` 私有方法。
   - 任选其中一个（例如 `_setup_handshake_pipeline_tests`），画出它展开的 generic 矩阵：哪些组合会被登记、哪些会被 `continue` 跳过。
3. **需要观察的现象**：你会看到 `setup_vunit` 本身只有一行行「分发调用」，真正的矩阵逻辑全在各 `_setup_*_tests` 里。这是一种「门面 + 分发」的组织方式，让一个模块十几个测试台的设置不至于挤在一个函数里。
4. **预期结果**：`module_common.py` 的 `setup_vunit` 调用了 12 个 `_setup_*_tests` 方法；`_setup_handshake_pipeline_tests` 用 `itertools.product` 产出 8 组三元组，其中「满吞吐+只流水控制信号」那一组被跳过。
5. 待本地验证：若你想确认登记数，可在装好 VUnit 的环境里运行 `tools/simulate.py --list`（VUnit 通常支持列出测试）查看 common 库下 `tb_handshake_pipeline` 的配置总数。

#### 4.1.5 小练习与答案

**练习 1**：`_setup_handshake_pipeline_tests` 里，`itertools.product` 产出 8 组三元组，但有两处 `continue`。若一个测试台只有 `test_full_throughput` 一个用例，最终会登记几组配置？

**参考答案**：`test_full_throughput` 用例要求 `full_throughput=True`。在 8 组里 `full_throughput=True` 的有 4 组，其中「`full_throughput=True, pipeline_control_signals=True, pipeline_data_signals=False`」被第 1 处 `continue` 跳过，剩 3 组。再用第 2 处 `continue` 过滤——但该用例本身要求 `full_throughput=True`，所以这 3 组都保留。最终登记 3 组。

**练习 2**：为什么要把「非法组合」用 `continue` 跳过，而不是让 VUnit 跑出来看它失败？

**参考答案**：非法组合失败是「预期内」的，让它跑只会污染 CI 报告（看起来像 bug），还会浪费仿真机时。提前在登记侧过滤，能让所有登记的配置都代表「应该通过」的有效场景，失败即真 bug。

---

### 4.2 按测试名定制 generic：测试名即「配置配方」

#### 4.2.1 概念说明

有些 generic 取值并不适合对所有用例都一样。例如 fifo 的「写快读慢」用例需要高的写停顿概率，而「读快写慢」用例需要高的读停顿概率。如果把停顿概率也展开成笛卡尔积，会爆出大量没意义的配置。

hdl-modules 的解法很巧妙：**用用例名字符串里是否包含某个关键词，来决定给这组配置塞哪些 generic**。用例名在这里充当了「配置配方」的角色——测试台的 `run("test_write_faster_than_read")` 这个名字本身就编码了它需要什么样的停顿概率、要不要开 last。

#### 4.2.2 核心流程

[modules/fifo/module_fifo.py](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/fifo/module_fifo.py) 把这件事抽成一个生成器函数 `generate_common_fifo_test_generics(test_name, original_generics)`，它是一个 `yield` 生成器，根据测试名「定制 + 扩展」generic：

```text
输入：test_name, original_generics
若 name 含 "write_faster_than_read": 塞 read_stall_probability_percent=90, enable_last=True
若 name 含 "read_faster_than_write": 塞 write_stall_probability_percent=90
若 name 含 "packet_mode":            塞 enable_last=True, enable_packet_mode=True
若 name 含 "drop_packet":             塞 enable_last=True, enable_drop_packet=True
若 name 含 "peek_mode":               塞 enable_last=True, enable_peek_mode=True
若 name 含 "init_state" 或 "almost":  yield 两组（不同 almost 阈值）
若 name 含 "drop_packet_mode_...":    yield 一组（固定深度）
否则:                                 yield 两组（depth=16, 64）
```

关键点是它返回的是一个**生成器**，可能 yield 多组（比如两组不同深度），让外层循环把每组都登记一次。

#### 4.2.3 源码精读

看 `generate_common_fifo_test_generics` 的「按测试名塞 generic」部分——一连串 `if "xxx" in test_name:` 像查表一样给配置加料：[modules/fifo/module_fifo.py:L65-L101](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/fifo/module_fifo.py#L65-L101)

注意第 92 行 `depth = 32 + generics["enable_output_register"]`：当 `enable_output_register=True`（Python 里等于 1）时深度自动 +1。这是因为 output register 模式要求 `depth = 2^k + 1`（见 u4-l1），这里用一个「整数加法」巧妙地把「是否开 output register」和「深度选择」耦合到同一个表达式里。

再看尾部三种 yield 分支：[modules/fifo/module_fifo.py:L103-L114](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/fifo/module_fifo.py#L103-L114)

对绝大多数普通用例，`else` 分支 yield 两组深度（16、64）。这体现了「按用例的重要程度定制测试强度」：边界用例（init_state/almost）测两种阈值组合，普通用例测两种深度，冷门用例（drop_packet_mode_read_level_should_be_zero）只测一组。

#### 4.2.4 代码实践

1. **实践目标**：用测试名驱动 generic 配置。
2. **操作步骤**：
   - 在 `tb_fifo.vhd` 里找到 `run("test_write_faster_than_read")` 这个用例（第 161 行附近）。
   - 对照 `generate_common_fifo_test_generics`，推导当跑这个用例时，最终登记的 generic 里 `read_stall_probability_percent`、`write_stall_probability_percent`、`enable_last` 分别是什么。
   - 再找 `run("test_packet_mode_random_data")`，推导它的 generic。
3. **需要观察的现象**：你会看到用例名里的关键词与生成器里的 `if "xxx" in test_name` 一一对应——名字就是配置契约。
4. **预期结果**：`test_write_faster_than_read` 会得到 `read_stall_probability_percent=90`、`enable_last=True`、两个深度（16、64）；`test_packet_mode_random_data` 会得到 `enable_last=True, enable_packet_mode=True`。
5. 待本地验证：若想确认，可在仿真时打印 `runner_cfg` 解析出的 generic，或直接看 VUnit 输出的每个配置的 generic 列表。

#### 4.2.5 小练习与答案

**练习 1**：如果新增一个名为 `test_peek_mode_status` 的用例，按 `generate_common_fifo_test_generics` 的逻辑，它会被塞哪些额外 generic？

**参考答案**：因为名字含 `"peek_mode"`，会被塞 `enable_last=True, enable_peek_mode=True`；然后落到 `else` 分支（不含 init_state/almost/drop_packet_mode_read_level...），yield 两组 `depth=16, 64`（再各自加上 `enable_output_register`）。注意：外层 `setup_vunit` 还有一条 `if enable_output_register and "peek_mode" in test.name: continue`，所以 peek 用例实际只登记 `enable_output_register=False` 的那组。

**练习 2**：为什么用「测试名字符串包含关键词」而不是「在 Python 里维护一张用例名→generic 的字典」？

**参考答案**：字符串包含是一种「弱约定、松耦合」的方式——VHDL 侧只要在 `run("...")` 的名字里带上关键词，Python 侧就能识别，两侧不需要同步维护一张显式表，新增用例时不易漏配。代价是关键词拼写要前后一致，且字符串匹配不够类型安全（这在该项目里靠测试覆盖来兜底）。

---

### 4.3 VUnit 断言式自检 testbench

#### 4.3.1 概念说明

前面两节都在讲 Python 侧「登记什么配置」，这一节讲 VHDL 侧「一个被登记的配置跑起来之后，测试台怎么自己判断对错」。

hdl-modules 的测试台分两种风格（都基于 VUnit）：

1. **多用例分支式**：一个 `tb_*.vhd` 里用 `if run("a") then ... elsif run("b") then ...` 串起多个命名用例，用例名就是 4.2 节里被 Python 识别的关键词来源。`tb_fifo.vhd` 是这种。
2. **单用例并发进程式**：一个 `tb_*.vhd` 只有一个隐含用例，主体拆成 `main` / `stimuli` / `check_output` 等多个并发 `process`，靠信号通信、靠 `assert` / `check_*` 自检。`tb_resync_twophase.vhd` 是这种。

两种风格共享同一套骨架：entity 必须有 `runner_cfg : string` generic；架构里调用 `test_runner_setup(runner, runner_cfg)` 启动、`test_runner_cleanup(runner)` 收尾；并用 `test_runner_watchdog(runner, <超时>)` 防死锁。

#### 4.3.2 核心流程

`tb_resync_twophase` 的骨架是：

```text
entity 声明: 5 个互斥的时钟关系 boolean + enable_lutram + enable_output_register + runner_cfg
架构:
  clk_in/clk_out 各自翻转产生时钟
  test_runner_watchdog(runner, 10 ms)        -- 看门狗
  main 进程:
    test_runner_setup(runner, runner_cfg)     -- 启动
    做统计检查, assert relative_error < 0.16  -- 自检
    test_runner_cleanup(runner)               -- 收尾(成功)
  stimuli 进程: 每拍让 data_in 递增
  check_output 进程: 每拍比对 data_out, 累计统计量
  dut_gen: 按 enable_lutram 选 DUT
```

`main`、`stimuli`、`check_output` 三个进程并发运行，通过信号 `data_in`、`data_out`、`num_outputs_checked` 等通信。`main` 进程会 `wait until num_outputs_checked = num_tests`，等校验进程攒够 100 次输出后才做最终的吞吐率断言。

#### 4.3.3 源码精读

先看 `tb_resync_twophase` 的 entity——generic 里既有功能开关（`enable_lutram` 等），也有 VUnit 必需的 `runner_cfg`：[modules/resync/test/tb_resync_twophase.vhd:L22-L33](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/resync/test/tb_resync_twophase.vhd#L22-L33)

`runner_cfg : string` 是 VUnit 测试台的「身份证」——没有它 VUnit 不认这是个测试台。其它 generic 才是 4.1/4.2 节里由 Python 登记进来的功能开关。注意 5 个时钟关系 generic 是互斥的（同时只有一个为 true），由 `module_resync.py` 的 `setup_resync_twophase_tests` 保证每次只置一个为 true（见 4.3.4）。

看门狗与 `main` 进程的开头/结尾——这是所有 VUnit 测试台的标配三件套：[modules/resync/test/tb_resync_twophase.vhd:L91](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/resync/test/tb_resync_twophase.vhd#L91) 设置了 10 ms 看门狗；下面是 `main` 进程的 setup 与 cleanup：[modules/resync/test/tb_resync_twophase.vhd:L104-L105](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/resync/test/tb_resync_twophase.vhd#L104-L105) 与收尾 [modules/resync/test/tb_resync_twophase.vhd:L145](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/resync/test/tb_resync_twophase.vhd#L145)。

`test_runner_setup` 必须在任何 `run(...)` / 等待之前调用，它把 `runner_cfg` 解析成运行时状态；`test_runner_cleanup` 标记本测试成功结束——如果中途某个 `assert` 失败，VUnit 会跳过 cleanup 并把该测试记为失败。看门狗则保证：万一测试卡死（比如握手死锁），10 ms 后强制判负，不会让 CI 永远挂住。

这个测试台的核心自检是一个 `assert`——把测得的平均采样周期换算成时间，与理论期望时间比较，相对误差必须小于 16%：[modules/resync/test/tb_resync_twophase.vhd:L143](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/resync/test/tb_resync_twophase.vhd#L143)

```vhdl
assert relative_error < 0.16 report "Not expected throughput";
```

这就是「断言式自检」的本质：不靠人看波形，而是把性能指标（两相握手的吞吐率）量化成一个数，用 `assert` 卡阈值。校验数据由并发的 `check_output` 进程逐拍累计（统计 `data_out` 相邻变化的间隔），见 [modules/resync/test/tb_resync_twophase.vhd:L165-L198](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/resync/test/tb_resync_twophase.vhd#L165-L198)。激励则由 `stimuli` 进程简单地产出（每拍 `data_in + 1`），见 [modules/resync/test/tb_resync_twophase.vhd:L150-L155](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/resync/test/tb_resync_twophase.vhd#L150-L155)。

最后，DUT 的选择本身也用 generic 驱动的 `generate` 完成——`enable_lutram` 决定例化 `resync_twophase_lutram` 还是 `resync_twophase`：[modules/resync/test/tb_resync_twophase.vhd:L204-L239](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/resync/test/tb_resync_twophase.vhd#L204-L239)

作为对照，再看多用例分支式。`tb_fifo.vhd` 在 `main` 进程里用一长串 `if run("...") then ... elsif run("...")` 来选择用例，并用 `check_equal` / `check_relation` 做断言——entity 的 generic 同样以 `runner_cfg` 收尾：[modules/fifo/test/tb_fifo.vhd:L31-L45](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/fifo/test/tb_fifo.vhd#L31-L45)；用例分支与断言见 [modules/fifo/test/tb_fifo.vhd:L146-L175](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/fifo/test/tb_fifo.vhd#L146-L175)。`run("test_init_state")` 这些字符串就是 4.2 节 Python 侧识别的关键词来源——两侧通过用例名耦合。

> 补充：VUnit 的 `run_pkg` 还提供 `run_all` 等宏，但本项目并未使用；本仓库全部采用上述 `run("...")` 分支或并发进程两种写法。

#### 4.3.4 代码实践

1. **实践目标**：把 Python 侧的 generic 矩阵与 VHDL 侧的用例名对应起来。
2. **操作步骤**：
   - 打开 `modules/resync/module_resync.py`，找到 `setup_resync_twophase_tests`（第 81 行起）。
   - 阅读它如何用 5 个时钟关系关键词 + `enable_lutram`（+ `enable_output_register`）展开配置。
   - 对照 `tb_resync_twophase.vhd` 的 entity generic，确认 Python 登记的每个 generic 都能在 VHDL entity 里找到同名的 generic 端口。
3. **需要观察的现象**：你会看到 `setup_resync_twophase_tests` 里 `generics = {mode: True, "enable_lutram": enable_lutram}`，其中 `mode` 取自那 5 个关键词字符串之一——而 entity 里正好有 5 个同名的 boolean generic。
4. **预期结果**：Python 侧登记的 generic 名字与 VHDL entity 的 generic 名字一一对应；这是 generic 矩阵能跑通的前提。
5. 待本地验证：若装好 VUnit 与仿真器，可运行 `python tools/simulate.py resync.tb_resync_twophase`（具体过滤语法以 VUnit 版本为准）只跑该测试台，观察每组配置的 generic 取值。

#### 4.3.5 小练习与答案

**练习 1**：`tb_resync_twophase` 的 `main` 进程里有一句 `wait until num_outputs_checked = num_tests and rising_edge(clk_out);`（第 112 行）。如果 `check_output` 进程因为 bug 永远不递增 `num_outputs_checked`，会发生什么？

**参考答案**：`main` 会一直等下去，测试卡死。但 `test_runner_watchdog(runner, 10 ms)` 会在 10 ms 后把该测试强制判负并报超时，CI 不会被永久卡住。这正是看门狗的价值。

**练习 2**：为什么 entity 的 generic 列表必须以 `runner_cfg : string` 结尾（无默认值）？

**参考答案**：`runner_cfg` 是 VUnit 在运行时注入的「身份证+配置」，必须由 VUnit 提供、不能有静态默认值。没有它 VUnit 不把该文件当作测试台。Python 侧登记配置时也不需要（也不应该）显式设置 `runner_cfg`，VUnit 会自动加上。

**练习 3**：`tb_resync_twophase` 用 `assert`，`tb_fifo` 用 `check_equal/check_relation`，二者有何区别？

**参考答案**：`assert` 是 VHDL 内建的，失败时只打印一句 message；`check_*` 来自 VUnit `check_pkg`，失败时会把期望值/实际值、出错位置、计数等结构化信息汇总到 VUnit 报告，便于在大量配置里定位是哪一组失败。项目里两种都用：性能阈值类断言用 `assert` 即可，逐值比对类用 `check_*` 更清晰。

---

### 4.4 Python 测试侧的路径约定：`conftest` 与 `tools_pythonpath`

#### 4.4.1 概念说明

前面三节都假设 `import tsfpga`、`from vunit.ui import VUnit` 能成功。但 tsfpga、VUnit 不一定装在系统路径里——它们可能是本地检出的兄弟仓库（开发时常用），也可能用 pip 装。为了让「本地检出优先于 pip 安装」，项目需要一个在**所有其它 import 之前**执行的小钩子来修正 `sys.path`。

Python 测试侧（pytest）的入口约定是 `test/conftest.py`，它在 pytest 收集任何测试前就会被导入。hdl-modules 在这里放了一行 import，触发 `tools/tools_pythonpath.py` 修正 `PYTHONPATH`。这套机制在 u1-l3 已经讲过原理，这里只看它在测试侧的落点。

#### 4.4.2 核心流程

```text
pytest 启动 → 导入 test/conftest.py
  → import tools.tools_pythonpath
    → tools_pythonpath.py 用 sys.path.insert(0, ...) 把本地仓库检出插到最前
  → 此后所有 import tsfpga / import vunit 都优先命中本地检出
仿真入口 tools/simulate.py 自己也做了同样的事：
  → sys.path.insert(0, REPO_ROOT)
  → import tools.tools_pythonpath
```

#### 4.4.3 源码精读

`test/conftest.py` 全文只有一行有效代码（加注释），但它必须在其它 import 之前执行——这正是 `conftest.py` 的天然位置：[test/conftest.py:L10-L11](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/test/conftest.py#L10-L11)

`simulate.py` 也遵循同样的「先修路径再 import 第三方」套路，注意它用 `sys.path.insert(0, ...)` 而非 `append`——这是为了**优先**本地检出：[tools/simulate.py:L14-L19](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/tools/simulate.py#L14-L19)

最后看 `simulate.py` 的 `main()` 如何把模块扫描与 `setup_vunit` 串起来——这一段是本讲所有内容的「总开关」：[tools/simulate.py:L31-L55](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/tools/simulate.py#L31-L55)

其中 `simulation_project.add_modules(modules=modules)`（第 38 行）是关键：它会遍历每个模块，调用其 `setup_vunit(vunit_proj=...)`——本讲 4.1/4.2 节展开 generic 矩阵的代码就是在这时被执行的。最后 `vunit_proj.main()`（第 55 行）才真正启动仿真器、把所有登记的配置跑一遍。也就是说：**`setup_vunit` 只负责「登记」，不负责「跑」；跑是 `vunit_proj.main()` 的事**。

#### 4.4.4 代码实践

1. **实践目标**：理解路径修正为何必须在最前面，以及 `setup_vunit` 被调用的时机。
2. **操作步骤**：
   - 打开 `tools/tools_pythonpath.py`（u1-l3 已读），确认它用 `sys.path.insert(0, ...)`。
   - 在 `tools/simulate.py` 的 `add_modules` 那一行（第 38 行）处想象打断点：此刻 `setup_vunit` 被回调，generic 矩阵被展开并登记。
   - 在 `vunit_proj.main()`（第 55 行）处想象打断点：此刻才真正把登记的配置交给仿真器。
3. **需要观察的现象**：你会清楚地区分两个阶段——「登记阶段」（Python，快速）与「仿真阶段」（调用仿真器，慢）。
4. **预期结果**：`conftest.py` 与 `simulate.py` 都在最前面修 `PYTHONPATH`；`setup_vunit` 在 `add_modules` 时被回调；真正的仿真在 `vunit_proj.main()` 启动。
5. 待本地验证：在装好依赖的环境运行 `python tools/simulate.py --help`，确认入口可用（无 ImportError 即说明路径修正生效）。

#### 4.4.5 小练习与答案

**练习 1**：如果删掉 `test/conftest.py` 里那行 `import tools.tools_pythonpath`，pytest 跑 lint 测试时可能出什么问题？

**参考答案**：当 tsfpga/VUnit 是本地兄弟仓库检出而非 pip 安装时，`sys.path` 里没有它们的路径，后续 `import tsfpga` 会 `ModuleNotFoundError`。`conftest.py` 的作用就是在测试收集前把它们插进 `sys.path`。

**练习 2**：`setup_vunit` 和 `vunit_proj.main()` 谁先执行？为什么这样设计？

**参考答案**：`setup_vunit` 先执行（在 `add_modules` 时被回调），它只做「登记配置」这件纯 Python 的快事；`vunit_proj.main()` 后执行，此时所有模块的所有配置都已登记完毕，VUnit 再统一调度仿真器跑。这样把「声明要跑什么」与「实际跑」解耦，便于过滤、并行、复用。

---

## 5. 综合实践

把本讲四个最小模块串起来，完成一个「给新实体加 VUnit 测试」的端到端小任务。

**任务**：假设你新写了一个极简实体 `double_reg`（一个带使能的二级寄存器，generic 有 `width : positive` 和 `use_enable : boolean`）。请按下述步骤把它纳入 hdl-modules 的验证流程。以下为示例代码，非项目原有代码。

1. **写最小测试台**（VHDL 侧，模仿 4.3 节骨架）：

   ```vhdl
   -- 示例代码：modules/<你的模块>/test/tb_double_reg.vhd
   library ieee;
   use ieee.std_logic_1164.all;
   use ieee.numeric_std.all;

   library vunit_lib;
   use vunit_lib.check_pkg.all;
   use vunit_lib.run_pkg.all;

   entity tb_double_reg is
     generic (
       width : positive;
       use_enable : boolean;
       runner_cfg : string
     );
   end entity;

   architecture tb of tb_double_reg is
     signal clk : std_ulogic := '0';
     signal en, valid_in, valid_out : std_ulogic := '0';
     signal data_in, data_out : std_ulogic_vector(width - 1 downto 0) := (others => '0');
   begin
     test_runner_watchdog(runner, 1 ms);
     clk <= not clk after 2 ns;

     main : process
     begin
       test_runner_setup(runner, runner_cfg);

       -- 喂一拍数据, 经两级寄存器后应出现在 data_out
       wait until rising_edge(clk);
       data_in <= std_ulogic_vector(to_unsigned(5, width));
       valid_in <= '1';
       wait until rising_edge(clk);
       valid_in <= '0';

       -- 等两级寄存器传播完
       wait until rising_edge(clk);
       wait until rising_edge(clk);
       check_equal(data_out, std_ulogic_vector(to_unsigned(5, width)));

       test_runner_cleanup(runner);
     end process;

     dut : entity work.double_reg
       generic map (width => width, use_enable => use_enable)
       port map (clk => clk, en => en,
                 valid_in => valid_in, data_in => data_in,
                 valid_out => valid_out, data_out => data_out);
   end architecture;
   ```

   要点核对：entity 末尾有 `runner_cfg : string`；架构里有 `test_runner_setup` / `test_runner_cleanup` / `test_runner_watchdog` 三件套；用 `check_equal` 做断言式自检。

2. **在 `module_<你的模块>.py` 里登记 generic 矩阵**（Python 侧，模仿 4.1 节）：

   ```python
   # 示例代码：modules/<你的模块>/module_<你的模块>.py
   class Module(BaseModule):
       def setup_vunit(self, vunit_proj, **kwargs):
           tb = vunit_proj.library(self.library_name).test_bench("tb_double_reg")
           for width in [8, 16, 32]:           # 两三种 width 取值
               for use_enable in [False, True]:  # 两三种 use_enable 取值
                   self.add_vunit_config(
                       tb, generics={"width": width, "use_enable": use_enable}
                   )
   ```

   这会展开成 \(3 \times 2 = 6\) 组配置。

3. **运行**：装好依赖后执行 `python tools/simulate.py <你的模块>.tb_double_reg`（过滤语法以 VUnit 版本为准），应看到 6 组配置被登记并各自跑通。

4. **观察与思考**：
   - 6 组配置是否都通过？若 `use_enable=True` 且 `en='0'` 时数据不该传播，你的测试台是否覆盖了这一点？（提示：上面的示例测试台没有驱动 `en`，这是有意留的缺陷——请补一个 `use_enable=True, en='0'` 的断言，验证 `data_out` 不变。）
   - 如果某个 `width`/`use_enable` 组合在你的实体里非法，应该在哪里用 `continue` 跳过？（参考 4.1.3 的 `_setup_handshake_pipeline_tests`。）

> 说明：以上 VHDL/Python 片段均为示例代码，本项目并没有 `double_reg` 这个实体；它是为了让你练习整套流程而虚构的最小目标。实体实现、仿真器调用与具体过滤语法「待本地验证」。

## 6. 本讲小结

- `setup_vunit` 是仿真钩子，职责是「登记测试配置」，不写测试逻辑、也不亲自跑仿真。
- generic 矩阵用**嵌套 `for` 循环**或 **`itertools.product`** 展开笛卡尔积，每组取值经 `self.add_vunit_config(test, generics=...)` 登记一次；非法/无意义组合用 `continue` 提前剔除。
- **用例名即配置配方**：用 `if "xxx" in test_name` 给不同用例塞不同 generic，避免笛卡尔积爆炸；`fifo` 还把生成器抽成 `generate_common_fifo_test_generics` 复用。
- VUnit 测试台两种风格：`run("...")` elsif 多用例分支（`tb_fifo`）与并发进程自检（`tb_resync_twophase`），共享 `runner_cfg` generic + `test_runner_setup/cleanup` + `test_runner_watchdog` 骨架。
- 断言式自检靠 `check_*` / `assert` 把正确性量化为阈值或逐值比对，无需人工看波形；看门狗保证死锁测试不会卡死 CI。
- Python 测试侧靠 `test/conftest.py`（pytest 入口）与 `tools/simulate.py` 在最前面修 `PYTHONPATH`；`setup_vunit` 在 `add_modules` 时被回调，真正仿真在 `vunit_proj.main()` 启动。

## 7. 下一步学习建议

- **下一讲 u8-l3（资源占用回归）**：本讲只讲了 `setup_vunit`（仿真钩子），u8-l3 讲同一个 `module_*.py` 里的另一个钩子 `get_build_projects`，看它如何用 netlist 构建 + `build_result_checker`（`EqualTo` 等）把 LUT/FF/RAM/逻辑级数纳入 CI 回归——那是 generic 矩阵的「综合侧」镜像。
- **回看 BFM（u8-l1）**：本讲的测试台多用 `check_equal` 做确定值比对；u8-l1 的 BFM 测试台则用随机背压 + 期望队列做大规模随机验证，二者互补。
- **延伸阅读**：通读 `modules/common/module_common.py` 的全部 `_setup_*_tests` 方法，它是本讲所有模式（嵌套循环、product、count、按名定制）的最全样例集；再挑一个 `tb_*.vhd`（如 `tb_handshake_mux.vhd`）对照其 `module_*.py` 的登记逻辑，练习「Python 登记 ↔ VHDL 用例」的双向阅读。

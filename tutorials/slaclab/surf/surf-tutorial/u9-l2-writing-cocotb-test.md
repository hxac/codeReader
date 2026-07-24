# 编写一个 cocotb 回归测试

## 1. 本讲目标

上一篇讲义（u9-l1）讲清了 SURF 回归测试的**工具链**：ruckus 收集源码、GHDL 编译仿真、cocotb 注入激励、pytest 发现并行，四者由 `run_surf_vhdl_test` 串起。本篇把视角从「工具链怎么转」转到「**测试文件怎么写**」。

学完后你应当能够：

1. 按 SURF 约定，从零写出一个结构完整的 cocotb 测试文件：许可证头 → 方法学头 → `TB` 类 → `@cocotb.test()` 协程 → `PARAMETER_SWEEP` → pytest 包装函数。
2. 写出可读的 `parameter_case` 用例，并用 `with_timeout` 给所有开放式等待加上界。
3. 理解对 `RST_POLARITY_G`、`RST_ASYNC_G` 等复位/时序泛型的参数扫描策略，以及为何同一份参数字典要同时喂给 `parameters` 和 `extra_env`。

本讲全程以 `tests/base/fifo/test_Fifo.py` 为范例，被测对象（DUT）是 u2-l2 讲过的统一 FIFO 封装 `Fifo`（实体定义在 `base/fifo/rtl/Fifo.vhd`）。FIFO 足够简单、又是全仓库高频复用件，很适合当「第一个手写测试」的练手对象。

## 2. 前置知识

在动手前，先建立三点直觉。

**第一，cocotb 的运行模型是「Python 协程驱动 VHDL」。** 仿真器（GHDL）逐拍推进时间，cocotb 用 `async` 协程在特定时刻（`RisingEdge`、`Timer` 等 trigger）挂起、恢复，从而读写 DUT 的信号。所以一个测试就是若干协程，靠 `await` 把「等一拍」「等一个超时」表达出来。

**第二，pytest 与 cocotb 的方向是反的。** 命令行敲的是 `pytest`，但真正启动 GHDL 的是 `cocotb_test.simulator.run`（封装在 `run_surf_vhdl_test` 里）。因此每个测试文件结尾要有一个**普通同步函数**（不是协程）作为 pytest 的入口，由它去召唤仿真器；而真正的激励写在被 `@cocotb.test()` 标记的**协程**里。这条「pytest 包装函数 → `run_surf_vhdl_test` → GHDL → cocotb 协程」的调用链，是上一篇 u9-l1 的核心，本篇直接沿用。

**第三，HDL 泛型与 Python 环境变量是两套通道。** 以 `_G` 结尾的键（如 `RST_ASYNC_G`）会进 GHDL 的 `generic` 端口；不以 `_G` 结尾的键（如 `WR_CLK_PERIOD_NS`）只是 Python 侧的环境变量，供 `TB` 类读取。这套分工由 `hdl_parameters_from` 过滤实现，本篇 4.3 会精读。

如果你对 FIFO 封装本身（`GEN_SYNC_FIFO_G`、`SYNTH_MODE_G`、`FWFT_EN_G` 等泛型语义）还不熟，建议先回看 u2-l2。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `tests/base/fifo/test_Fifo.py` | 本讲的范例测试文件，结构最完整，五段式骨架齐全。 |
| `tests/common/regression_utils.py` | 全仓库共享的运行器与环境辅助：`env_flag`/`env_sl`/`parameter_case`/`hdl_parameters_from`/`run_surf_vhdl_test` 等。 |
| `tests/README.md` | SURF cocotb 回归风格指南，规定方法学头、TB 类、参数扫描、断言与时序的写法。 |
| `base/fifo/rtl/Fifo.vhd` | 被测的统一 FIFO 封装实体 `Fifo`（即 `toplevel="surf.fifo"`），u2-l2 已讲解。 |

## 4. 核心概念与源码讲解

本讲按测试文件的自上而下顺序拆成三个最小模块：**方法学头与 TB 类**（4.1）、**`@cocotb.test()` 协程**（4.2）、**参数扫描与 pytest 包装**（4.3）。三者构成「文档→行为→入口」的完整闭环。

### 4.1 方法学头与 TB 类

#### 4.1.1 概念说明

一个新读者打开某个 cocotb 测试文件时，最想知道两件事：**这个测试证明了什么**，以及**它故意不证明什么**。SURF 用两段「头」来回答：

- **许可证头**：全仓库统一的 SLAC PHAROS 许可证横幅，标明文件归属与许可证位置。
- **方法学头（Test methodology）**：紧跟在许可证头之后的一段注释，用四个固定小标题（Sweep / Stimulus / Checks / Timing）描述本测试的参数矩阵、激励序列、断言内容、时序前提。

风格指南 `tests/README.md` 明确要求：**不许用通用模板文字**，方法学头必须说明本测试具体证明了什么。

在方法学头之后，若「建钟、复位、时钟启动」这套设置不平凡，就应封装进一个小的 `TB` 类，把 DUT 状态和复用操作收拢到一处，避免在每个协程里重复样板代码。

#### 4.1.2 核心流程

范例文件 `test_Fifo.py` 顶部的方法学头按四段展开：

```text
# Test methodology:
# - Sweep:    本文件跑了哪些参数/配置组合（同步/异步后端、FWFT）
# - Stimulus: 实际往 DUT 灌了什么激励（写一段有序突发再读回）
# - Checks:   断言了哪些输出/状态（保序、wr/rd count 别名相等）
# - Timing:   依赖或验证了什么时序（独立读写时钟 vs 共同时钟）
```

`TB` 类的标准职责流程如下：

```text
__init__(dut):
  1. 从环境变量读泛型（复位极性、异步复位、同步/异步、时钟周期）
  2. 把 DUT 控制信号置为安全初值（rst 拉到有效、wr_en/rd_en/din 清零）
  3. 按同步/异步选择时钟启动方式：
     - 同步（GEN_SYNC_FIFO_G=true）→ start_lockstep_clocks（两个 clk 同源）
     - 异步                         → 两个独立 Clock 协程

reset():
  4. 拉复位 → 在每个可见时钟域给若干干净周期 → 释放复位 → 再给若干干净周期

write_word / read_word:
  5. 用 with_timeout 包住「等 not_full / 等 valid」的开放式等待，再驱动一拍写/读
```

#### 4.1.3 源码精读

先看范例文件的方法学头（许可证头之后）：

[tests/base/fifo/test_Fifo.py:L11-L23](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/tests/base/fifo/test_Fifo.py#L11-L23) —— 四段式方法学头：Sweep 说明只扫「inferred 同步 + inferred 异步 + FWFT」两条后端分支，目的是验证封装的选择逻辑而非重测每个底层 FIFO 特性；Stimulus/Checks/Timing 则分别交代激励、断言与时钟前提。

风格指南里给出的方法学头**模板**与「通用结构」清单在此：

[tests/README.md:L32-L44](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/tests/README.md#L32-L44) —— 四个固定小标题的模板。

[tests/README.md:L55-L66](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/tests/README.md#L55-L66) —— 「常见结构」清单：导入 → 小 `TB` 类 → 一个或多个 `@cocotb.test()` → `PARAMETER_SWEEP` → 调 `run_surf_vhdl_test` 的 pytest 包装。

接着看 `TB` 类的构造函数。它演示了一个关键技巧：**把 HDL 泛型的当前取值从环境变量读回 Python**，让 `TB` 的行为随参数用例自适应。

[tests/base/fifo/test_Fifo.py:L42-L63](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/tests/base/fifo/test_Fifo.py#L42-L63) —— `TB.__init__`：用 `env_flag("RST_ASYNC_G")`、`env_sl("RST_POLARITY_G")`、`env_flag("GEN_SYNC_FIFO_G")` 读复位/拓扑泛型，用 `os.environ[...]` 读两个时钟周期；随后把 `rst/wr_en/rd_en/din` 置安全初值；最后按 `sync_fifo` 在 `start_lockstep_clocks`（同源共同时钟）与两个独立 `Clock` 协程之间二选一。

注意这里的同步/异步时钟选择有讲究：同步封装（`GEN_SYNC_FIFO_G=true`）假设两个 FIFO 时钟其实是**同一根时钟**，所以必须用 `start_lockstep_clocks` 由单个协程驱动两路信号，而不能开两个周期相同但相位可漂的独立时钟协程。这个反模式的解释写在注释和风格指南里：

[tests/common/regression_utils.py:L72-L92](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/tests/common/regression_utils.py#L72-L92) —— `start_lockstep_clocks`：一个 `drive()` 协程同时翻转多路信号，保证 `COMMON_CLK_G` 类封装真正吃到共享边沿，而非两个可相对漂移的同频振荡器。

[tests/README.md:L104-L107](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/tests/README.md#L104-L107) —— 风格指南明确：`COMMON_CLK_G` 等封装必须用 `start_lockstep_clocks`，不许开两个独立同频协程。

再看复位与读写操作。`reset()` 给被选中的 FIFO 后端在复位前后各留若干干净周期，让封装的状态输出从已知态启动；`write_word`/`read_word` 则把所有「等条件成立」的开放式循环用 `with_timeout` 包住，给一个 5 µs 的上界，防止 DUT 卡死时测试无限挂起。

[tests/base/fifo/test_Fifo.py:L84-L95](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/tests/base/fifo/test_Fifo.py#L84-L95) —— `reset()`：拉有效复位 → 异步复位时先 `Timer(2)` → 每域 6 拍 → 释放 → 每域再 6 拍。

[tests/base/fifo/test_Fifo.py:L97-L115](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/tests/base/fifo/test_Fifo.py#L97-L115) —— `write_word`/`read_word`：`await with_timeout(self._wait_not_full(), 5, "us")` 给「等非满」加上界；FWFT 始终启用，故读侧先等 `valid`、采样 `dout`、再用一拍 `rd_en` 消费。

> 断言与时序的总原则见 [tests/README.md:L109-L131](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/tests/README.md#L109-L131)：断言**外部可见行为**（payload、TKEEP、TLAST、TUSER、响应码、计数、握手时序），而非实现偶发现象；所有开放式 `while` 必须由 `with_timeout` 或带周期上限的辅助函数包住；要为 `TPD_G`、寄存输出与 GHDL 调度留余量——恰好在时钟沿采样会制造假失败，多数辅助函数在 `RisingEdge()` 后再 `Timer` 稍作 settle。

#### 4.1.4 代码实践

**实践目标**：亲手读懂一个 `TB` 类如何随参数自适应，并验证你对「同步/异步时钟选择」的理解。

**操作步骤**：

1. 打开 [tests/base/fifo/test_Fifo.py:L42-L63](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/tests/base/fifo/test_Fifo.py#L42-L63)，逐行标注 `__init__` 中每个 `env_flag`/`env_sl`/`os.environ` 读取对应哪个参数用例键。
2. 对照 4.3 节的 `PARAMETER_SWEEP`（[test_Fifo.py:L168-L195](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/tests/base/fifo/test_Fifo.py#L168-L195)），回答：用例 `sync_inferred_fwft` 会让 `self.sync_fifo`、`self.async_reset`、`self.rst_polarity` 分别取什么值？
3. 在纸上画出同步用例与时调用 `start_lockstep_clocks`、异步用例与时调用两个 `Clock` 协程的分支。

**需要观察的现象**：`sync_fifo` 为真时两路时钟来自同一协程（相位锁定）；为假时两路时钟周期不同（5 ns vs 9 ns），是真正独立的异步域。

**预期结果**：`sync_inferred_fwft` → `sync_fifo=True, async_reset=False, rst_polarity=1`，走 `start_lockstep_clocks`；`async_inferred_fwft` → `sync_fifo=False, async_reset=True, rst_polarity=0`，走两个独立 `Clock` 协程。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `TB.__init__` 不直接 `cocotb.start_soon(Clock(...))` 两个同周期时钟来模拟同步 FIFO？

**参考答案**：同步封装（`GEN_SYNC_FIFO_G=true` / `COMMON_CLK_G` 类）的契约是「两路时钟真的是同一根」。两个独立但周期相同的 `Clock` 协程会因协程调度而在相位上相对漂移，DUT 看到的是两个可错位的沿，而非共享沿，可能触发假失败或假通过。`start_lockstep_clocks` 用单协程同时翻转多路信号，才真正满足「共享时钟」契约。

**练习 2**：方法学头的四段（Sweep/Stimulus/Checks/Timing）分别回答了什么问题？为什么风格指南禁止用通用模板文字？

**参考答案**：Sweep 回答「跑了哪些参数组合」、Stimulus 回答「灌了什么激励」、Checks 回答「断言了什么」、Timing 回答「依赖/验证了什么时序」。禁止通用文字是因为方法学头的读者（代码评审者、未来维护者）需要快速判断**这个 bench 证明了什么、故意不证明什么**，通用描述无法提供这个信息。

---

### 4.2 `@cocotb.test()` 协程

#### 4.2.1 概念说明

方法学和 `TB` 类是「设置」，真正「证明行为」的是被 `@cocotb.test()` 装饰的**协程**。每个这样的协程是一个独立的测试用例入口，cocotb 会在仿真启动后逐个调用它们。SURF 的约定是：**一个协程只证明一个清晰的行为**，用例之间通过 `TB` 类共享设置、通过提前 `return` 来把某协程限定在部分参数用例上。

#### 4.2.2 核心流程

一个 `@cocotb.test()` 协程的典型骨架：

```text
@cocotb.test()
async def xxx_test(dut):
    1. tb = TB(dut)              # 复用设置/复位/时钟
    2. （可选）if 不适用本用例: return   # 把测试限定在部分参数上
    3. await tb.reset()          # 复位到已知态
    4. 驱动激励（写若干字 / 发一帧 / 读一寄存器）
    5. 收集观测值
    6. assert 观测 == 期望        # 断言外部可见行为
```

关键点：所有「等条件」的循环都必须有界（用 `with_timeout` 或带周期上限的 helper）；断言要落在**外部可见**的信号上（数据、握手、计数、侧带），而不是实现细节。

#### 4.2.3 源码精读

范例文件有两个 `@cocotb.test()` 协程。第一个证明「保序」：

[tests/base/fifo/test_Fifo.py:L126-L141](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/tests/base/fifo/test_Fifo.py#L126-L141) —— `wrapper_branch_ordering_test`：`TB(dut)` → `tb.reset()` → 写入 `[0x11,0x22,0x33]` → 依次读回 → `assert observed == expected`。它跨同步/异步两条后端分支都证明读写保序，是「一个协程证明一个行为」的范本。

第二个协程演示两个重要技巧——**提前 return 把测试限定在部分用例**，以及**直接断言封装的别名信号**：

[tests/base/fifo/test_Fifo.py:L144-L165](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/tests/base/fifo/test_Fifo.py#L144-L165) —— `sync_count_alias_test`：开头 `if not tb.sync_fifo: return`，把该测试限定在同步用例；随后写两个字、检查 `wr_data_count == rd_data_count` 且大于 0（验证 u2-l2 讲过的「同步模式下两计数端口别名到同一内部信号」），再读回两个字、检查计数归零。

这里的「提前 return」是 SURF 的常用模式：**参数矩阵是统一的，但某些断言只对部分配置有意义**。与其为不同配置写不同文件，不如在一个文件里用 `if ... return` 把不适用的协程静默跳过。cocotb 会把这个协程在该用例下记为通过（未失败），而它在适用用例下才真正执行断言。

再看协程内部用到的有界等待。`write_word` 里的 `await with_timeout(self._wait_not_full(), 5, "us")` 把一个潜在的死循环（DUT 永远满时 `_wait_not_full` 会无限等）变成 5 µs 内必出结果的有限等待：

[tests/base/fifo/test_Fifo.py:L97-L103](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/tests/base/fifo/test_Fifo.py#L97-L103) —— `write_word` 用 `with_timeout` 包住 `_wait_not_full()`；若 5 µs 内 `not_full` 始终为 0，cocotb 抛 `SimTimeoutError`，测试失败而非挂死。

#### 4.2.4 代码实践

**实践目标**：体会「一个协程证明一个行为」与「提前 return 限定用例」两个约定。

**操作步骤**：

1. 读 [test_Fifo.py:L144-L165](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/tests/base/fifo/test_Fifo.py#L144-L165) 的 `sync_count_alias_test`。
2. 回答：如果删掉 `if not tb.sync_fifo: return` 这一行，在异步用例 `async_inferred_fwft` 下会发生什么？（提示：异步封装的 `wr_data_count` 与 `rd_data_count` 不再别名到同一信号。）
3. 想象你要新增第三个协程 `almost_full_test`，只在 `ADDR_WIDTH_G=4` 的窄 FIFO 上验证 almost满标志——你会把哪句 `if ...: return` 放在协程开头？

**需要观察的现象**：异步用例下 `wr_data_count`（写域）与 `rd_data_count`（读域）分属不同时钟域的计数，二者数值一般不相等。

**预期结果**：删掉守卫后，异步用例下 `assert int(dut.wr_data_count.value) == int(dut.rd_data_count.value)` 大概率失败——这正是守卫存在的意义。新增协程应以 `if <读取的 ADDR_WIDTH_G> != 4: return` 开头（读取方式与 `TB` 读 `RST_POLARITY_G` 一致，可用 `env_int`）。

#### 4.2.5 小练习与答案

**练习 1**：为什么协程里所有 `while ... : await RisingEdge(...)` 形式的等待都必须被 `with_timeout` 包住？

**参考答案**：这样的循环在 DUT 行为异常（如永远不满/永远不 valid、握手死锁）时会无限挂起，仿真永不结束，既浪费算力又掩盖 bug。`with_timeout` 给一个明确上界，超时即抛异常让测试失败，把「挂死」变成「可诊断的失败」。风格指南要求：开放式 `while True` 除非已被 `with_timeout` 或带周期上限的 helper 包住，否则不许用。

**练习 2**：`sync_count_alias_test` 用 `if not tb.sync_fifo: return` 跳过异步用例。cocotb 会在异步用例下把这个协程记成什么状态？

**参考答案**：记成「通过」（未抛异常即通过）。它没有失败，但也没有真正执行断言——这就是「把测试限定在部分用例」的代价与用法：适用用例下真跑断言，不适用用例下静默跳过。因此参数矩阵的**覆盖责任**落在 `PARAMETER_SWEEP` 上：必须确保对每种想验证的配置，至少有一个不提前 return 的协程在跑断言。

---

### 4.3 参数扫描与 pytest 包装

#### 4.3.1 概念说明

前两模块写好了「设置」和「行为」，但还缺一个**入口**让 pytest 能发现并启动它。这个入口由三件套构成：

- **`PARAMETER_SWEEP`**：一个精心挑选的参数用例列表（不是笛卡尔积），每个用例是一组泛型/环境变量取值加一个可读 ID。
- **`parameter_case`**：构造单个用例的辅助函数，把一组键值对包成带 ID 的 `pytest.param`。
- **pytest 包装函数**：一个普通同步函数（非协程），用 `@pytest.mark.parametrize` 把 `PARAMETER_SWEEP` 展开成多个仿真，每个仿真调一次 `run_surf_vhdl_test` 启动 GHDL。

这里的精妙之处在于**同一份参数字典的两种用法**：它的 `_G` 后缀键要进 GHDL 泛型（`parameters=`），全部键要进环境变量供 `TB` 读取（`extra_env=`）。`hdl_parameters_from` 就是做这道过滤的。

#### 4.3.2 核心流程

```text
PARAMETER_SWEEP = [
    parameter_case("可读ID_1", KEY1="...", RST_POLARITY_G="'1'", WR_CLK_PERIOD_NS="5", ...),
    parameter_case("可读ID_2", KEY1="...", RST_POLARITY_G="'0'", WR_CLK_PERIOD_NS="5", ...),
]

@pytest.mark.parametrize("parameters", PARAMETER_SWEEP)
def test_目标模块(parameters):        # 普通同步函数，pytest 入口
    run_surf_vhdl_test(
        test_file=__file__,           # 本文件，用于推算 cocotb module 名
        toplevel="surf.xxx",          # library.entity
        parameters=hdl_parameters_from(parameters),  # 只留 _G 键 → GHDL 泛型
        extra_env=parameters,         # 全部键 → 环境变量供 TB 读
    )
```

参数字典的「一份数据、两条通道」流转如下：

```text
parameter_case 字典 {RST_ASYNC_G, RST_POLARITY_G, ..., WR_CLK_PERIOD_NS, RD_CLK_PERIOD_NS}
        │
        ├── hdl_parameters_from(过滤 _G) ──→ parameters=  ──→ GHDL generic 端口
        │
        └── 原样 ─────────────────────→ extra_env=  ──→ os.environ ──→ TB.__init__ 读取
```

#### 4.3.3 源码精读

先看两个辅助函数。`parameter_case` 把一组键值包成带 `id` 的 `pytest.param`，这个 `id` 会成为 pytest 的用例 ID 和 sim_build 目录名，所以必须短而有意义：

[tests/common/regression_utils.py:L148-L157](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/tests/common/regression_utils.py#L148-L157) —— `parameter_case(case_id, **parameters)` 返回 `pytest.param(parameters, id=case_id)`；`hdl_parameters_from(parameters)` 用字典推导只保留以 `_G` 结尾的键。

再看范例的 `PARAMETER_SWEEP`：两个精心挑选的用例，分别覆盖同步后端（同源时钟、同步复位、复位高有效）与异步后端（独立时钟、异步复位、复位低有效）：

[tests/base/fifo/test_Fifo.py:L168-L195](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/tests/base/fifo/test_Fifo.py#L168-L195) —— `PARAMETER_SWEEP`：`sync_inferred_fwft`（`GEN_SYNC_FIFO_G="true"`、`RST_ASYNC_G="false"`、`RST_POLARITY_G="'1'"`、两时钟皆 5 ns）与 `async_inferred_fwft`（`GEN_SYNC_FIFO_G="false"`、`RST_ASYNC_G="true"`、`RST_POLARITY_G="'0'"`、写 5 ns / 读 9 ns）。两者都开 FWFT、用 inferred 后端、distributed RAM、8 位宽 4 位地址。

注意取值的字符串形态：HDL 泛型值用 VHDL 字面量写法——布尔是 `"true"/"false"`、`std_logic` 是 `"'1'/省略引号"`。这些字符串原样传给 GHDL 解析。而 `WR_CLK_PERIOD_NS="5"` 不以 `_G` 结尾，故不进泛型，只作环境变量。

最后看 pytest 包装函数，它把每个用例展开成一次独立仿真：

[tests/base/fifo/test_Fifo.py:L198-L205](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/tests/base/fifo/test_Fifo.py#L198-L205) —— `@pytest.mark.parametrize("parameters", PARAMETER_SWEEP)` 装饰 `def test_Fifo(parameters)`；函数体调 `run_surf_vhdl_test(test_file=__file__, toplevel="surf.fifo", parameters=hdl_parameters_from(parameters), extra_env=parameters)`。

这里 `toplevel="surf.fifo"` 指向 u2-l2 讲过的统一封装实体 `Fifo`（[base/fifo/rtl/Fifo.vhd:L22](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/fifo/rtl/Fifo.vhd#L22)），`surf` 是库名、`fifo` 是实体名（大小写不敏感）。`test_file=__file__` 让运行器推算出 cocotb 要导入的 Python module 路径（即本文件），这是 u9-l1 讲过的 `_module_name_from_test_file`。

风格指南对参数扫描的总要求：

[tests/README.md:L67-L82](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/tests/README.md#L67-L82) —— 「优先精心挑选的矩阵而非宽笛卡尔积」：一个好矩阵覆盖默认路径、一两个有意义的泛型分支、复位极性/异步复位（若相关）、窄/宽数据通路（若影响打包）、反压或分级（若时序是契约的一部分）。`parameters=` 只传 HDL 泛型，Python 专属元数据放 `extra_env`，或用 `hdl_parameters_from` 一刀切。

#### 4.3.4 代码实践

**实践目标**：亲手往 `PARAMETER_SWEEP` 加一个用例，并理解同一字典的双通道流转。

**操作步骤**（源码阅读型 + 待本地验证的运行）：

1. 打开 [test_Fifo.py:L168-L205](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/tests/base/fifo/test_Fifo.py#L168-L205)。
2. 在本地副本中，仿照现有两条用例，新增第三条 `parameter_case("sync_block_fwft", ...)`：保持 `GEN_SYNC_FIFO_G="true"`、`FWFT_EN_G="true"`，但把 `MEMORY_TYPE_G` 从 `"distributed"` 改成 `"block"`，其余与 `sync_inferred_fwft` 一致。
3. 在注释里写明：这条新用例会把哪几个键送进 GHDL 泛型、哪几个键只进环境变量。
4. 运行（待本地验证，需先 `make MODULES="$PWD" import` 生成源缓存）：
   ```bash
   make MODULES="$PWD" import
   ./.venv/bin/python -m pytest -n 0 -q tests/base/fifo/test_Fifo.py -k sync_block_fwft
   ```

**需要观察的现象**：pytest 会把三个用例各展开成一次独立仿真；`-k sync_block_fwft` 只跑新加的那条；sim_build 目录名里会带上该用例的全部键值（因 `_sim_build_path` 把参数拼进目录名）。

**预期结果**：新用例通过（block RAM 后端的同步 FIFO 同样保序、计数别名相等）。若报 `Missing imported HDL sources`，说明未先 `import`，按提示先跑 `make ... import`（u9-l1 已讲）。

> 提示：`MEMORY_TYPE_G="block"` 是否被 `Fifo` 封装真正支持，取决于 u2-l2 讲过的后端选择逻辑；若该取值在当前封装下非法，GHDL 会在 elaboration 阶段报错——这本身也是一种有用的学习信号。运行结果待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `test_Fifo` 把**同一个** `parameters` 字典同时传给 `parameters=hdl_parameters_from(parameters)` 和 `extra_env=parameters`？如果只传 `extra_env=parameters`、不传 `parameters=`，会发生什么？

**参考答案**：因为一份用例字典里既有 HDL 泛型（`_G` 后缀，要进 GHDL `generic`）又有 Python 专属值（时钟周期，只给 `TB` 读）。`hdl_parameters_from` 过滤出 `_G` 键喂给 GHDL，`extra_env` 把全部键放进环境变量供 `TB.__init__` 读取。若只传 `extra_env` 不传 `parameters`，GHDL 会用泛型默认值而非用例指定值——例如 `RST_ASYNC_G` 恒为默认 `false`，异步复位路径永远跑不到，参数扫描就名存实亡。

**练习 2**：`parameter_case` 的第一个参数（`case_id`）会出现在哪两个地方？为什么风格指南要求它「短而有意义」？

**参考答案**：它会成为 pytest 的用例 ID（`pytest -k` 过滤、测试报告里显示的名字）和 sim_build 构建目录名的一部分。短而有意义是因为：太长会让命令行和报告难以阅读，也会让构建路径变得脆弱（甚至触碰文件系统路径长度限制）；无意义（如 `case1`）则让 `pytest -k` 无法按意图筛选用例。`README` 还提示：当用例元数据足以产生过长路径时，可用 `sim_build_key` 单独指定构建目录键。

**练习 3**：参数取值里 `RST_POLARITY_G="'1'"` 与 `WR_CLK_PERIOD_NS="5"` 在写法上有何本质区别？分别走哪条通道？

**参考答案**：`RST_POLARITY_G="'1'"` 以 `_G` 结尾，值 `"'1'"` 是 VHDL `std_logic` 字面量，经 `hdl_parameters_from` 进 GHDL 泛型端口；`WR_CLK_PERIOD_NS="5"` 不以 `_G` 结尾，只是字符串 `"5"`，不进泛型，而是经 `extra_env` 进 `os.environ`，由 `TB.__init__` 的 `float(os.environ["WR_CLK_PERIOD_NS"])` 读回 Python。

## 5. 综合实践

把三个最小模块串起来，**亲手从零写一个最小的 cocotb 测试文件**。

**任务**：仿照 `test_Fifo.py` 的五段式骨架，为统一 FIFO 封装 `Fifo`（`toplevel="surf.fifo"`）新建一个测试文件 `test_FifoExtra.py`，证明一个**新属性**——例如「复位后 `valid`（读侧数据有效）初始为 0」。要求包含：许可证头 + 四段方法学头、一个 `TB` 类（至少读 `RST_POLARITY_G` 并启动时钟）、一个 `@cocotb.test()` 协程（复位后用 `with_timeout` 有界地确认 `valid==0`）、一个含 **2 个** `parameter_case` 的 `PARAMETER_SWEEP`（扫描复位极性 `'1'` 与 `'0'` 两个用例）、以及 pytest 包装函数。

下面是符合 SURF 约定的骨架（**示例代码**，端口名沿用 `Fifo` 封装的真实信号 `rst/wr_clk/rd_clk/valid`；实际可运行性待本地验证）：

```python
# （许可证头略）

# Test methodology:
# - Sweep:   复位极性 '1' 与 '0' 两个用例，均用 inferred 同步 FWFT 后端。
# - Stimulus: 仅做复位，不写任何数据。
# - Checks:  复位释放后读侧 valid 必须为 0（空 FIFO 不应声称有数据）。
# - Timing:  复位后给若干干净周期再采样 valid，避开 TPD_G 与 GHDL 调度。

import cocotb
import pytest
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge, Timer, with_timeout

from tests.common.regression_utils import (
    env_sl, hdl_parameters_from, parameter_case, run_surf_vhdl_test,
    start_lockstep_clocks,
)


class TB:
    def __init__(self, dut):
        self.dut = dut
        self.rst_polarity = env_sl("RST_POLARITY_G", default=1)
        dut.rst.value = self.rst_polarity
        dut.wr_en.value = 0
        dut.rd_en.value = 0
        dut.din.value = 0
        # 同步封装：两路时钟同源
        start_lockstep_clocks(dut.wr_clk, dut.rd_clk, period_ns=5.0)

    async def reset(self):
        self.dut.rst.value = self.rst_polarity            # 有效
        for _ in range(6):
            await RisingEdge(self.dut.wr_clk)
        self.dut.rst.value = 1 - self.rst_polarity        # 无效（释放）
        for _ in range(6):
            await RisingEdge(self.dut.wr_clk)


@cocotb.test()
async def valid_idle_after_reset_test(dut):
    tb = TB(dut)
    await tb.reset()
    await Timer(2, unit="ns")                  # settle，避开边沿采样假象
    assert int(dut.valid.value) == 0           # 空 FIFO：valid 必为 0


PARAMETER_SWEEP = [
    parameter_case("rst_high_active",
                   RST_POLARITY_G="'1'", GEN_SYNC_FIFO_G="true",
                   FWFT_EN_G="true", SYNTH_MODE_G="inferred",
                   MEMORY_TYPE_G="distributed",
                   DATA_WIDTH_G="8", ADDR_WIDTH_G="4"),
    parameter_case("rst_low_active",
                   RST_POLARITY_G="'0'", GEN_SYNC_FIFO_G="true",
                   FWFT_EN_G="true", SYNTH_MODE_G="inferred",
                   MEMORY_TYPE_G="distributed",
                   DATA_WIDTH_G="8", ADDR_WIDTH_G="4"),
]


@pytest.mark.parametrize("parameters", PARAMETER_SWEEP)
def test_FifoExtra(parameters):
    run_surf_vhdl_test(
        test_file=__file__,
        toplevel="surf.fifo",
        parameters=hdl_parameters_from(parameters),
        extra_env=parameters,
    )
```

**自检清单**：

1. 方法学头四段是否都说清了「证明什么 / 不证明什么」？
2. `TB` 是否在 `__init__` 把控制信号置安全初值并启动了**同源**时钟？
3. 协程里是否有任何无界 `while`？`valid` 采样前是否 settle？
4. 两个 `parameter_case` 是否只差复位极性这一个键？ID 是否短而有意义？
5. pytest 包装是否用 `hdl_parameters_from` 过滤泛型、用原字典做 `extra_env`？

**运行**（待本地验证）：

```bash
make MODULES="$PWD" import
./.venv/bin/python -m pytest -n auto --dist=worksteal -q tests/base/fifo/test_FifoExtra.py
```

## 6. 本讲小结

- SURF cocotb 测试文件遵循**五段式骨架**：许可证头 → 四段方法学头 → `TB` 类 → `@cocotb.test()` 协程 → `PARAMETER_SWEEP` + pytest 包装函数。
- **方法学头**用 Sweep/Stimulus/Checks/Timing 四段说清「本 bench 证明什么、故意不证明什么」，禁止通用模板文字；`TB` 类把随参数自适应的设置（读 `env_flag`/`env_sl`、置安全初值、选同步/异步时钟）收拢到一处。
- **`@cocotb.test()` 协程**一个证明一个行为；用 `if ...: return` 把某协程限定在部分用例；所有开放式等待必须用 `with_timeout` 加上界；断言落在**外部可见**信号上。
- **同步 FIFO 必须用 `start_lockstep_clocks`** 由单协程驱动两路时钟，禁止开两个独立同频协程，否则破坏 `COMMON_CLK_G` 的共享时钟契约。
- **参数扫描**优先精心矩阵而非笛卡尔积；`parameter_case` 的 `id` 同时是 pytest ID 和 sim_build 目录名，须短而有意义。
- **同一份参数字典走两条通道**：`hdl_parameters_from` 过滤出 `_G` 键喂 GHDL 泛型，原字典经 `extra_env` 进环境变量供 `TB` 读取。

## 7. 下一步学习建议

- 下一篇 **u9-l3（测试辅助与帧构造器）** 将进入更复杂的子系统：讲解 `tests/axi/utils.py` 的 AXI 原语、`wait_sampled_ready` 采样握手，以及各子系统 `*_test_utils.py` 如何构造与校验协议帧（如以太网帧、SRPv3 请求/响应）。当你写的测试不再是对单个寄存器/计数的断言，而是要发收完整帧时，就需要这些辅助。
- 建议继续阅读的真实测试文件：同目录的 [tests/base/fifo/test_FifoAsync.py](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/tests/base/fifo/test_FifoAsync.py)、`test_FifoSync.py`，对比它们与本讲范例在 `PARAMETER_SWEEP` 与 `TB` 上的异同；再看一个协议级测试（如 `tests/protocols/srp/`）感受帧构造器的引入时机。
- 想理解被测 DUT 本身（`Fifo` 封装的后端选择、`MEMORY_TYPE_G`、FWFT）可回看 u2-l2；想理解运行器内部（`run_surf_vhdl_test`、import 缓存、pytest 并行）可回看 u9-l1。

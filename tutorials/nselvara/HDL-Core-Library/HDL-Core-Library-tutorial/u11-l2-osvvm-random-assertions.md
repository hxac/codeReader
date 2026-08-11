# OSVVM 随机化与断言校验

## 1. 本讲目标

学完本讲，你应该能够：

- 用 OSVVM 的 `RandomPType`（`InitSeed` / `RandSlv` / `RandInt`）在测试台里生成可复现的随机激励。
- 用 VUnit 的 `check_equal` 与 `info` 做「实际值 vs 期望值」的自动比对，并理解失败时如何上报、如何让整个用例判负。
- 区分三种「验证之声」：验证侧的随机激励、验证侧的结果判定、设计侧的运行时自检（`report`/`assert`，由 `-- synthesis off` 包裹、不综合）。
- 读懂 `fifo_sync` 里那段仿真专用断言，并解释综合工具对它的处理。

本讲承接 u11-l1「VUnit 测试台结构」——上一讲解决「测试台骨架怎么搭」，本讲解决「骨架里怎么造数据、怎么判对错」。

## 2. 前置知识

在进入源码前，先建立三个直觉。

**(1) 定向测试 vs 随机测试。** 定向测试（directed test）是你亲手写「输入 → 期望输出」的每一组用例，精确但覆盖面有限，容易只测你想得到的情况。随机测试（random/constrained-random test）让工具按某种分布自动生成大量输入，专门去撞你没想过的边角组合。工业界经验是：随机测试擅长发现「并发与时序」类 bug，这类 bug 往往不是单条逻辑错，而是若干事件以你没料到的顺序叠加才暴露。

**(2) 可复现的随机。** 随机测试有个硬要求：**失败必须可复现**。纯随机（如用真实时间做种子）每次跑出的序列不同，一旦某次跑挂了，你却无法重现，就无法调试。OSVVM 的做法是用一个「种子」初始化伪随机数发生器，**同一颗种子永远产生同一串数**。只要把种子记进日志，失败用例就能原样重放。

**(3) 三种「验证之声」不要混淆。** 本讲会同时出现三种「发声」方式，它们的发出者和作用范围完全不同：

| 声音 | 发出位置 | 谁执行 | 综合时 | 失败后果 |
|------|----------|--------|--------|----------|
| 随机激励（`RandSlv` 等） | 测试台 `checker` 进程 | OSVVM 随机源 | 测试台本就不综合 | 不产生，它是「输入」 |
| 结果判定（`check_equal`） | 测试台 `checker` 进程 | VUnit 检查器 | 测试台本就不综合 | 记 error，判该用例失败 |
| 运行时自检（`report`/`assert`） | **设计源码**内部 | 仿真器 | 由 `-- synthesis off` 剥离 | 按 severity 上报（这里只是 `warning`） |

第三行是本讲的重点之一：设计源码里也能写断言，但必须保证它**只活在仿真里、不会变成电路**。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
|------|------|-----------|
| [ip/communication/spi/tb/tb_spi_tx.vhd](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/communication/spi/tb/tb_spi_tx.vhd) | SPI 发送模块的 VUnit 测试台 | OSVVM `RandomPType` 造随机数据、`check_equal` 判状态、`info` 打日志 |
| [ip/memories/fifo/fifo_sync.vhd](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/fifo_sync.vhd) | 同步 FIFO（含三套架构） | `own_behavioural_sync_fifo` 里 `-- synthesis off` 包裹的溢出/下溢断言 |
| [ip/memories/fifo/tb/tb_fifo_sync.vhd](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/tb/tb_fifo_sync.vhd) | 同步 FIFO 测试台 | `RandInt` 造随机延时/计数、`check_equal` 封装成可复用过程 |

> 说明：`check_equal`、`info` 来自 VUnit（`vunit_lib.vunit_context`）；`RandomPType` 来自 OSVVM（`osvvm.RandomPkg`）。它们的具体实现不在本仓库，由 VUnit / OSVVM 预编译库提供（见 u2-l2 厂商库与外部库的讨论）。本讲只讲「怎么用」与「怎么读」，不讲其内部实现。

---

## 4. 核心概念与源码讲解

### 4.1 OSVVM RandomPType 随机化

#### 4.1.1 概念说明

OSVVM（Open Source VHDL Verification Methodology）是一个验证方法学开源库，其中 `RandomPkg` 提供了一个**保护类型（protected type）** `RandomPType`，相当于「一个带状态的随机数发生器对象」。你声明一个该类型的**变量**，就得到了一个独立的随机源，可反复向它索取随机值。

为什么要用 `RandomPType` 而不是手写 `ieee.math_real` 里的 `uniform`？因为 `RandomPType` 帮你做了三件事：

1. **封装状态**：种子和内部状态藏在保护类型里，你不用自己维护一对 `uniform` 的 seed 种子变量。
2. **直接产出目标类型**：`RandSlv(Size)` 直接返回 `std_ulogic_vector`，`RandInt(min, max)` 直接返回指定区间的整数，不用你手写 `real → integer → slv` 的转换。
3. **可复现**：通过 `InitSeed(...)` 显式设定种子，保证可重放。

#### 4.1.2 核心流程

一个随机激励的标准生命周期是三步：

```
声明 variable random : RandomPType;
        │
        ▼
初始化 random.InitSeed(<种子字符串>)   ← 进程进入时执行一次
        │
        ▼
取值   random.RandSlv(Size)            ← 在用例里反复调用
        random.RandInt(min, max)
```

关键在第二步的种子选择。本库统一用 `tb_path & random'instance_name` 作为种子：

- `tb_path` 是 VUnit 注入的测试台文件路径类属（见 u11-l1），**每个测试台不同**；
- `random'instance_name` 是 VHDL 属性，返回该变量在层次结构中的完整路径字符串，**每个用例/每次例化不同**；
- 二者拼接，得到一颗「每个测试台、每个用例都不同、但又确定」的种子。

确定 = 可复现；不同 = 各用例之间的随机序列互不雷同、覆盖面更广。这正是 OSVVM 官方推荐的「分布式可复现」播种法。

#### 4.1.3 源码精读

先看库声明与变量声明。测试台顶部引用 OSVVM 的随机包：

```vhdl
library osvvm;
use osvvm.RandomPkg.RandomPType;
```

完整链接：[tb_spi_tx.vhd:18-19](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/communication/spi/tb/tb_spi_tx.vhd#L18-L19) —— 引入 OSVVM 随机包，只导入 `RandomPType` 这一个保护类型。

在 `checker` 进程的说明区声明一个随机源变量：

```vhdl
variable random: RandomPType;
```

完整链接：[tb_spi_tx.vhd:109](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/communication/spi/tb/tb_spi_tx.vhd#L109) —— 声明一个独立的随机发生器，状态封装在保护类型内部。

在进程体开头（`begin` 之后、用例循环之前）做一次性播种：

```vhdl
random.InitSeed(tb_path & random'instance_name);
```

完整链接：[tb_spi_tx.vhd:310](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/communication/spi/tb/tb_spi_tx.vhd#L310) —— 用「测试台路径 + 变量层次名」播种，兼顾可复现与跨用例差异性。

随后在用例里反复取随机数据。以「单字发送」用例为例：

```vhdl
expected_data := random.RandSlv(Size => DATA_WIDTH);
tx_data <= expected_data;
tx_data_valid <= '1';
```

完整链接：[tb_spi_tx.vhd:157-160](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/communication/spi/tb/tb_spi_tx.vhd#L157-L160) —— 每次调用 `RandSlv` 取一个 8 位随机向量当待发数据；`Size` 形参显式给出位宽。

`RandSlv` 造的是「数据」，而 FIFO 测试台还用 `RandInt` 造「控制与时序」。例如随机决定等待几个时钟、随机决定写多少个字：

```vhdl
wait_sys_clk_cycles(random.RandInt(1, 10));   -- 随机延时 1~10 拍
...
num_writes := random.RandInt(1, 10);          -- 随机写 1~10 个字
```

完整链接：[tb_fifo_sync.vhd:165](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/tb/tb_fifo_sync.vhd#L165) 与 [tb_fifo_sync.vhd:508](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/tb/tb_fifo_sync.vhd#L508) —— 用 `RandInt` 给「等几拍」「写几个字」这类控制量注入随机性，这是造并发与时序边角的关键。

#### 4.1.4 代码实践

**实践目标：** 亲手体会「同一种子 → 同一序列」。

**操作步骤：**

1. 在 `tb_spi_tx.vhd` 的 `test_single_word_transmission` 里，`random.RandSlv` 调用上方加一行日志，打印本次取到的种子可复现信息与首个随机值，例如：
   ```vhdl
   info("instance_name seed base => " & random'instance_name);
   expected_data := random.RandSlv(Size => DATA_WIDTH);
   info("first random word => " & to_hstring(expected_data));
   ```
2. 用 `test_runner.py` 只跑该用例两次，对比两次日志里的 `first random word`。
3. 把 `random.InitSeed(...)` 那一行的种子改成硬编码字符串，例如 `random.InitSeed("fixed-seed-1");`，再跑两次。

**需要观察的现象：** 第 2 步两次运行，`first random word` 应**完全相同**（同一颗种子）；第 3 步改成硬编码种子后同样两次相同，但若你把字符串改成 `"fixed-seed-2"`，序列会变。

**预期结果：** 验证「同一 `tb_path & instance_name` → 同一随机序列」。这也解释了为什么 CI 里偶发的随机用例失败，靠日志里的种子就能在本地原样重放。

> 待本地验证：具体随机值取决于 OSVVM 版本与种子实现，不必死记具体数值，只需确认「同种子同序列」这一不变量。

#### 4.1.5 小练习与答案

**练习 1：** 为什么种子用 `tb_path & random'instance_name` 拼接，而不是直接用 `random'instance_name`？

**参考答案：** `instance_name` 在单一测试台内能区分不同进程/用例，但若两个不同测试台里恰好存在层次路径相同的变量，种子就会撞车、序列重复。拼上 VUnit 注入的 `tb_path`（每个测试台文件独有）进一步降低了跨测试台的种子碰撞概率，同时仍保持可复现。

**练习 2：** `RandSlv(Size => 8)` 理论上能产生多少种不同取值？为什么随机测试在「数据 × 时序」组合空间里更划算？

**参考答案：** 8 位共 \(2^{8}=256\) 种取值。单独看数据，256 种可被定向测试穷举；但真实 bug 往往来自「数据值 × 写读时序 × 同时读写」的组合，组合空间随并发维度指数膨胀。随机测试不追求穷举单一维度，而是用等量预算去均匀撒点整个组合空间，命中边角组合的概率更高。

---

### 4.2 VUnit check_equal / info 校验与失败上报

#### 4.2.1 概念说明

随机激励只是「把输入造出来」，还需要「把输出判对错」。VUnit 的 `vunit_context` 提供了一组检查过程，最常用的是 `check_equal`：

- `check_equal(got, expected, msg)` —— 比较「实际值」与「期望值」，不等则记一条 error 并附上 `msg`。
- `info(msg)` —— 打一条 informational 日志，仅展示、不判对错，用于标注用例进度。

与裸 `assert` 相比，`check_equal` 的好处是：**它和 VUnit 的测试结果统计打通了**。一次 `check_equal` 失败，会被 VUnit 计入该用例的错误计数，最终决定用例 pass/fail，并汇入 CI 的 xunit 报告（见 u11-l3）。你不必自己写 `if a /= b then assert false` 这种样板。

#### 4.2.2 核心流程

一次比对的语义：

```
check_equal(got => 实际信号, expected => 期望值, msg => "说明")
        │
        ├── 相等 → 记一条 pass（默认不打扰，可配置显示）
        │
        └── 不等 → 记一条 error（带 msg、带行号）
                   └── VUnit 汇总：该用例出错数 > 0 → 用例判失败
                                  → CI xunit 报 fail
```

注意三个细节：

1. `got`/`expected` 支持多种类型重载（`std_ulogic`、`std_ulogic_vector`、`integer`/`natural` 等），所以同一个 `check_equal` 既能比单比特信号，也能比整数计数。
2. `expected` 用 `'-'`（don't-care）可以表达「此处不关心」——本库 FIFO 测试台正是靠它让一个过程同时承担「检查」与「跳过」两种用法。
3. `msg` 强烈建议每次都写：失败时它是定位现场的唯一线索。

#### 4.2.3 源码精读

SPI 发送测试台里，复位用例用 `check_equal` 逐条核对输出初值：

```vhdl
rst_n <= '0';
wait_spi_clk_cycles(1);
check_equal(got => tx_is_ongoing,      expected => '0', msg => "tx_is_ongoing - TX should be inactive during reset");
check_equal(got => serial_data_out,    expected => 'Z', msg => "serial_data_out - Serial data should be high-Z during reset");
check_equal(got => spi_chip_select_n,  expected => '1', msg => "spi_chip_select_n - Chip select should be inactive during reset");
```

完整链接：[tb_spi_tx.vhd:141-145](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/communication/spi/tb/tb_spi_tx.vhd#L141-L145) —— 复位期间，TX 不活动（`'0'`）、串行线高阻（`'Z'`）、片选无效（`'1'`），三条 `check_equal` 分别守护一个不变量。

发送中与发送后的状态判定：

```vhdl
wait until tx_is_ongoing;
check_equal(got => tx_is_ongoing, expected => '1', msg => "tx_is_ongoing - TX should be active");
...
check_equal(got => tx_is_ongoing, expected => '0', msg => "TX should be inactive after transmission");
check_equal(got => spi_chip_select_n, expected => '1', msg => "Chip select should be inactive after transmission");
```

完整链接：[tb_spi_tx.vhd:161-173](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/communication/spi/tb/tb_spi_tx.vhd#L161-L173) —— 比对 `'1'`/`'0'` 这种 `std_ulogic` 标量，`check_equal` 有对应重载，无需自己转类型。

`info` 用于标注用例边界，让日志可读：

```vhdl
info("1.0) Testing reset behavior");
...
info("Reset behavior test passed" & LF);
```

完整链接：[tb_spi_tx.vhd:134,151](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/communication/spi/tb/tb_spi_tx.vhd#L134) —— `info` 只打日志、不影响判定；`& LF`（换行）让多用例日志不挤在一起。

FIFO 测试台把 `check_equal` 封装成「带 don't-care 跳过」的可复用过程，是更值得学习的写法：

```vhdl
procedure check_fifo_status(expected_full, expected_empty: std_ulogic := '-') is begin
    if expected_full /= '-' then
        check_equal(got => full_own,  expected => expected_full,  msg => "full_own");
    end if;
    if expected_empty /= '-' then
        check_equal(got => empty_own, expected => expected_empty, msg => "empty_own");
    end if;
end procedure;
```

完整链接：[tb_fifo_sync.vhd:123-132](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/tb/tb_fifo_sync.vhd#L123-L132) —— 形参默认值 `'-'` 表示「不关心」：调用方只想查 `empty` 时传 `expected_full` 默认值，过程内部自动跳过 `full` 的比对。一个过程同时服务「全检 / 只查 full / 只查 empty」三种调用方。

整数计数也能用同一个 `check_equal`（靠重载）：

```vhdl
check_equal(got => words_stored_own, expected => expected_count, msg => "words_stored_own");
```

完整链接：[tb_fifo_sync.vhd:140](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/tb/tb_fifo_sync.vhd#L140) —— 左边 `natural`、右边 `natural`，`check_equal` 自动选整数重载比对。

#### 4.2.4 代码实践

**实践目标：** 亲手触发一次 `check_equal` 失败，看清它如何上报。

**操作步骤：**

1. 在 `tb_spi_tx.vhd` 的 `test_reset_behavior` 里，**故意**把一条期望值改错，例如把片选那条的 `expected => '1'` 临时改成 `expected => '0'`。
2. 用 `test_runner.py`（或 CI 的 `test_runner_ci_cd.py`）跑该测试台。

**需要观察的现象：** 日志里应出现一条 error，内容包含你写的 `msg` 字符串、所在文件与行号，并且该用例（乃至整个测试台，因为用了 `run_all_in_same_sim`）被标记为失败；若开启了 xunit 报告，对应条目为 fail。

**预期结果：** 还原改动后重新运行应恢复全绿。这帮你确认「`check_equal` 失败 = 用例失败」这条链路是通的，而不是被静默吞掉。

> 待本地验证：具体日志格式取决于所用仿真器（NVC / ModelSim 等），但 error 级别与用例 fail 是 VUnit 保证的跨仿真器一致行为。

#### 4.2.5 小练习与答案

**练习 1：** 为什么 `check_equal` 的 `msg` 几乎每次都要写，而不能依赖默认行为？

**参考答案：** 失败时 `msg` 是定位「哪一条断言、什么意图」的唯一线索。一个用例常有十几条 `check_equal`，没有 `msg` 只能得到「不等」加一个行号，调试成本高；有 `msg` 则一眼看出是「复位期片选应为无效」还是「发送后 TX 应停止」。

**练习 2：** `check_fifo_status` 用 `'-'` 作默认值来实现「跳过」，这依赖什么类型特性？为什么 `expected` 必须是 `std_ulogic` 而不能是 `std_ulogic_vector`？

**参考答案：** 依赖 `std_ulogic` 九值里的 don't-care 值 `'-'`，用它当哨兵。`full`/`empty` 本身是标量 `std_ulogic`，所以形参也用标量；若改成向量，`'-'` 就不是合法的单元素值，哨兵判等逻辑也不再成立。

---

### 4.3 设计内 report/assert（synthesis off 仿真专用断言）

#### 4.3.1 概念说明

前两节的检查都在**测试台**里。但有时我们希望「设计源码自己」也能在仿真时喊一声「我被错误使用了」——例如 FIFO 被写满还在写、被读空还在读。这种「设计侧运行时自检」用 VHDL 的 `report` 语句写在**设计源码**（可综合文件）里。

问题来了：设计源码是要被综合成电路的，而 `report` 是仿真语句，综合器不认识。如果直接写，综合器要么报错、要么警告、要么把它当成奇怪的逻辑推断。解决办法是用**综合开关注释**（synthesis pragma）把这段代码包起来：

```vhdl
-- synthesis off
   ...仅仿真代码...
-- synthesis on
```

主流综合器（Xilinx Vivado、Intel Quartus、Synopsys 等）都识别这对注释：`-- synthesis off` 到 `-- synthesis on` 之间的所有内容在综合时**被完全忽略**，就像不存在；但在仿真器里它们就是普通 VHDL，照常执行。它和 Verilog 的 `// synthesis translate_off` / `translate_on` 是一回事。

注意本库这套断言的 **severity 是 `warning`**——这是个微妙但重要的细节：它在仿真里只是「警告」，不会像 VUnit 的 `check_equal` 那样自动判用例失败。它是**设计侧的良心提醒**，不是验证侧的硬性判决。

#### 4.3.2 核心流程

整套机制的运行与综合两条路径：

```
仿真路径：
  仿真器读到 process(...) → 每个 sys_clk 上升沿检查
        ├── write_enable and full      → report "FIFO is full..." severity warning
        └── read_enable and empty      → report "FIFO is empty..." severity warning
  （warning 只在日志显示，不直接判失败）

综合路径：
  综合器读到 -- synthesis off → 跳过整段 process
  → 该进程不生成任何电路
  → FIFO 的真实满空保护靠的是 fifo_write_request <= write_enable and not full
     这种「会综合」的组合逻辑（见 4.3.3）
```

关键区分：**真正防止溢出/下溢的硬件逻辑**（`not full` / `not empty` 屏蔽）是可综合的、始终生效；`report` 断言只是「如果屏蔽逻辑失效了，仿真里第一时间告诉你」的保险丝，二者职责不同。

#### 4.3.3 源码精读

先看 generic 总开关，默认关闭：

```vhdl
UNDER_AND_OVERFLOW_ASSERTIONS: boolean := false;
```

完整链接：[fifo_sync.vhd:10](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/fifo_sync.vhd#L10) —— 断言是可选的、默认不开；即便开了，它在综合时也会被下一对 pragma 剥离。

接下来是断言本体，被 `-- synthesis off / on` 紧紧包住：

```vhdl
-- assertion logic for simulation - not synthesised
-- synthesis off
ASSERTION_HINT: if UNDER_AND_OVERFLOW_ASSERTIONS generate
    fifo_overflow_underflow_assertion: process (sys_clk)
    begin
        if rising_edge(sys_clk) then
            if write_enable and full then
                report "Assert Failure - FIFO is full and being written!" severity warning;
            end if;

            if read_enable and empty then
                report "Assert Failure - FIFO is empty and being read!" severity warning;
            end if;
        end if;
    end process;
end generate;
-- synthesis on
```

完整链接：[fifo_sync.vhd:159-175](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/fifo_sync.vhd#L159-L175) —— 三层结构：外层注释说明意图、`-- synthesis off/on` 圈定「综合时丢弃」的区间、内部 `if generate` 用 generic 再做一道编译期开关、最内的进程在每个时钟沿检查溢出（[L165-167](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/fifo_sync.vhd#L165-L167)）与下溢（[L169-171](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/fifo_sync.vhd#L169-L171)）。

注意两点细节：

1. 断言进程只看 `sys_clk`，但它引用的 `write_enable`/`full`/`read_enable`/`empty` 在进程敏感列表里没有显式列出——因为这是同步进程（只对时钟敏感），组合输入靠进程内读取，不影响仿真正确性（综合时整段又被丢弃）。
2. severity 是 `warning`。这意味着即使断言触发，仿真器只打一条警告，**不会自动 abort 或判失败**。要想让它升级为「硬失败」，可把 severity 改成 `error`/`failure`，但那会改变测试语义，需谨慎。

对比一下「会综合」的真正保护逻辑——满空屏蔽：

```vhdl
fifo_write_request <= write_enable and not full;
fifo_read_request  <= read_enable  and not empty;
```

完整链接：[fifo_sync.vhd:183-187](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/fifo_sync.vhd#L183-L187) —— 这段不在 `synthesis off` 里，会被综合成真实门电路：满时写请求被屏蔽、空时读请求被屏蔽，这才是硬件上「不允许溢出/下溢」的保证。上一段的 `report` 是这条逻辑失效时的仿真报警。

#### 4.3.4 代码实践

**实践目标：** 在仿真里亲手触发一次溢出告警，并确认综合时它消失。

**操作步骤（仿真侧）：**

1. 在 `tb_fifo_sync.vhd` 里（或新写一个最小测试台）例化 `fifo_sync(own_behavioural_sync_fifo)`，并把 `UNDER_AND_OVERFLOW_ASSERTIONS => true` 打开。
2. 先连续写 `FIFO_DEPTH + 2` 个字（写满后继续写），观察仿真日志。
3. 再在空 FIFO 上连续读两拍，观察日志。

**需要观察的现象：** 写满后继续写，日志应出现 `Assert Failure - FIFO is full and being written!`（severity warning）；空读时出现 `Assert Failure - FIFO is empty and being read!`。但仿真**仍继续运行、用例不一定失败**（warning 不等于 error）。

**综合侧说明（无需真跑综合，按 pragma 语义推理即可）：** 把 `fifo_sync.vhd` 交给任意主流综合器，`-- synthesis off` 与 `-- synthesis on` 之间的 `fifo_overflow_underflow_assertion` 进程会被整体丢弃，**不占用任何 LUT/FF**。最终电路里只有 `fifo_write_request`/`fifo_read_request` 这类可综合屏蔽逻辑在守护 FIFO。因此这套断言是「零面积成本」的仿真保险。

> 待本地验证：不同综合器对 `report` 语句的默认处理略有差异（有的直接忽略，有的告警提示「不可综合」），但只要它落在 `-- synthesis off` 之内，就被统一跳过，行为确定。

#### 4.3.5 小练习与答案

**练习 1：** 如果把 `report ... severity warning` 改成 `severity failure`，仿真行为会如何变？为什么本库仍选 `warning`？

**参考答案：** `severity failure`（以及 `error`）在多数仿真器里会中止当前仿真运行，相当于「一遇到溢出就立刻挂掉」。本库选 `warning` 是为了「提醒但不打断」——测试台可能正按计划去故意触碰满/空边界（例如 `test_edge_cases` 就是在满/空时再写再读），用 warning 才能既留痕又不误杀这些合法用例。

**练习 2：** 为什么这段断言进程没有把 `write_enable`、`full` 等放进敏感列表 `process(sys_clk)`？仿真结果会出错吗？

**参考答案：** 这是一个对 `sys_clk` 同步的进程，所有判断都在 `rising_edge(sys_clk)` 内进行，输入信号在时钟沿被采样。进程只在时钟沿更新输出，组合输入的变化不需要触发进程，因此不列入敏感列表不影响仿真正确性。若这是组合逻辑进程则必须列全敏感量——但此处不是。

---

## 5. 综合实践

把本讲三块知识串起来：给 `tb_spi_tx` 新增一个**随机多字发送**用例，用 `RandomPType` 造数据、用 `check_equal` 判状态；再用一句话总结 `fifo_sync` 的 `synthesis off` 段在综合时被如何处理。

> 下面是**示例代码**（非项目原有代码），按现有测试台风格编写，加到 `tb_spi_tx.vhd` 的 `checker` 进程里。

**第一步：新增过程（示例代码）。** 放在现有过程定义之后、进程 `begin` 之前：

```vhdl
-- 示例代码：随机多字发送
procedure test_random_multi_word_transmission is
    constant NUM_WORDS : natural := 16;
    variable rand_word : std_ulogic_vector(DATA_WIDTH - 1 downto 0);
begin
    info("5.0) Testing random multi-word transmission");

    for i in 0 to NUM_WORDS - 1 loop
        rand_word := random.RandSlv(Size => DATA_WIDTH);  -- ① 随机激励
        tx_data        <= rand_word;
        tx_data_valid  <= '1';

        if i = 0 then
            wait until tx_is_ongoing;                      -- 首字握手
        end if;

        wait until tx_data_ack;                            -- 等发送方确认捕获
        tx_data_valid <= '0';

        -- ② 每个 tx_data_ack 后比对：TX 应处于活动状态
        check_equal(got => tx_is_ongoing, expected => '1',
                    msg => "TX active after ack for word " & to_string(i));

        wait_tx_spi_clk_cycles(DATA_WIDTH);                -- 等本字发完
    end loop;

    wait until not tx_is_ongoing;
    wait_spi_clk_cycles(1);
    -- ③ 收尾判定：全部发完后 TX 停止、片选释放
    check_equal(got => tx_is_ongoing,      expected => '0', msg => "TX inactive after all random words");
    check_equal(got => spi_chip_select_n,  expected => '1', msg => "Chip select deasserted after all random words");

    info("Random multi-word transmission test passed" & LF);
end procedure;
```

**第二步：注册用例（示例代码）。** 在 `while test_suite` 循环的 `elsif` 链里加一条（注意放在 `else assert false` 之前）：

```vhdl
elsif run("test_random_multi_word_transmission") then
    test_random_multi_word_transmission;
```

**第三步：跑测试并回答两个问题。**

- **问题 A（验证侧）：** 用 `test_runner.py` 运行 `tb_spi_tx`，确认新用例被自动发现并通过。然后把循环上界 `NUM_WORDS` 改大（如 64），重跑，确认随机的「数据 × 背靠背时序」组合仍稳定。
- **问题 B（设计侧）：** `fifo_sync` 里 `-- synthesis off ... -- synthesis on` 段在综合时会被如何处理？

  **参考回答：** 整段（含 `fifo_overflow_underflow_assertion` 进程）被综合器整体忽略，不生成任何硬件（零 LUT/零 FF）。它在仿真里是溢出/下溢的 warning 提醒；真正在硬件上防止溢出/下溢的，是不在该 pragma 内、会被综合的 `fifo_write_request <= write_enable and not full` 与 `fifo_read_request <= read_enable and not empty` 屏蔽逻辑。

**关于「逐比特比对期望值」的诚实说明：** 上面 ② 处的 `check_equal` 比对的是**握手/状态信号**（`tx_is_ongoing`、`spi_chip_select_n`），并非串行比特本身——因为 `tb_spi_tx` 是「只发不收」的测试台，没有回采路径。若要做到「每个随机字逐比特 round-trip 比对」，需要再例化一个 `spi_rx`（见 u10-l3）作为环回，在 `rx_data_valid` 拉高后用 `check_equal(got => rx_data, expected => rand_word, ...)`。这是把本用例升级为「数据自校验」的下一步练习方向。

> 待本地验证：`wait until tx_data_ack` 的精确握手节拍取决于 `spi_tx` 实现；若连续背靠背发送时 `tx_data_ack` 与上一字末位时序耦合，循环里的 `wait_tx_spi_clk_cycles(DATA_WIDTH)` 可能需要按 u10-l2 描述的模式时序微调。先在波形里核一遍 `tx_data_ack` 与 `tx_is_ongoing` 的相对位置再下结论。

---

## 6. 本讲小结

- **OSVVM `RandomPType`** 是一个带状态的随机源：声明变量 → `InitSeed(tb_path & random'instance_name)` 播种 → 用 `RandSlv` 造数据、`RandInt` 造时序/计数；种子确定则序列可复现。
- **`RandSlv` 造数据、`RandInt` 造控制量**是随机测试的两类典型用法——前者打数据面，后者打并发与时序面，后者往往是 bug 高发区。
- **VUnit `check_equal(got, expected, msg)`** 把「实际 vs 期望」的比对与用例 pass/fail 统计打通，失败自动记 error 并汇入 CI 报告；`info` 只打日志、不判对错。
- **`msg` 必写、`'-'` 当 don't-care 哨兵**是本库两条实用约定，让 `check_equal` 既可读又可复用。
- **设计侧 `report` 断言** 写在可综合源码里，但用 `-- synthesis off / on` 包裹，综合时整体丢弃、零面积；仿真时按 `severity warning` 提醒，**不会自动判失败**。
- **防溢出/下溢的真正硬件保护**是可综合的 `not full`/`not empty` 屏蔽逻辑，`report` 断言只是它失效时的仿真保险丝——二者职责不同，不可混淆。

## 7. 下一步学习建议

- **承上：** 回到 u9-l1 复习 `own_behavioural_sync_fifo` 的满空屏蔽逻辑，对照本讲确认「可综合屏蔽」与「仿真断言」的分工。
- **横向：** 阅读其它测试台（`tb_spi_rx.vhd`、`tb_fifo_async.vhd`）观察它们如何用 `RandomPType` + `check_equal` 组合造压力场景，体会「随机激励 + 自动判定」这一通用范式。
- **下一讲 u11-l3：** 将把视角抬到 `.do` 波形脚本与 CI/CD 验证闭环——讲清 `test_runner.py` 包装层、xunit 报告如何承接本讲的 `check_equal` 失败结果、以及 `excluded_list` 如何临时跳过不稳定的随机用例。

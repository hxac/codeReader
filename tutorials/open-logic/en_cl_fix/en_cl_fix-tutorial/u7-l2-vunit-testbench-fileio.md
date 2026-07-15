# VUnit 测试台与文件 I/O

## 1. 本讲目标

本讲是「验证闭环」的第二段。在 [u7-l1](u7-l1-cosim-concept-scripts.md) 中，我们已经看到 Python 的 cosim 脚本如何把每个算子的期望结果落盘成一堆文本文件（`a_fmt.txt`、`r_fmt.txt`、`rnd.txt`、`testN_output.txt`）。这些文件就是「黄金参考」。本讲要回答的问题是：**VHDL 测试台如何把这些黄金数据读进来、喂给被测器件（UUT）、再逐拍把 UUT 的输出和黄金值比对？**

学完本讲，你将能够：

1. 看懂一个标准 VUnit 测试台的运行骨架：`test_runner_setup` → `while test_suite` → `run(...)` → `test_runner_cleanup`，以及 `test_runner_watchdog` 防卡死机制。
2. 理解 `en_cl_fix_fileio_pkg` 如何在 `en_tb` 的通用文件 I/O 之上，封装出「定点专用」的 `cl_fix_read_file` / `cl_fix_read_format_file` 等接口。
3. 掌握 `FixFormatArray_t`、`SlvArray_t` 这两种数组类型如何从文本文件一次性读入。
4. 读懂 `g_test_case ... generate` 如何把「一个测试」在编译期展开成几十甚至上百个并行子测试，每个子测试由「输入进程 + UUT + 检查进程」三件套组成，并完成逐拍比对。

---

## 2. 前置知识

本讲需要你已经建立以下认知（来自前置讲义）：

- **三语言镜像架构**（[u1-l2](u1-l2-repo-structure-three-languages.md)）：VHDL 是金标准语义，Python 是同名同参数的参考模型。
- **协同仿真（cosim）思想**（[u7-l1](u7-l1-cosim-concept-scripts.md)）：Python 先跑出「穷举所有输入」的期望输出并落盘，VHDL 测试台再逐拍比对。黄金数据里存的是**未归一化整数** `d = v·2^(−F)`，即硬件位串的整数值，方便直接比对。
- **`FixFormat_t` 与 `cl_fix_width`**（[u2-l1](u2-l1-fixformat-representation.md)、[u2-l4](u2-l4-width-minmax-union-helpers.md)）：格式 `[S,I,F]` 与位宽公式 `W=S+I+F`。
- **可流水线化组件**（[u6-l1](u6-l1-pipelined-components-registermode.md)）：`en_cl_fix_round` 等组件有统一的 `clk/rst/valid/meta/data` 端口和 `RegisterMode_t`。

本讲会补充几个新概念：

- **VUnit**：一个开源的 VHDL 验证框架，提供 `test_runner` 流程、`check_equal` 断言、`run("...")` 测试选择等机制，并能在仿真前用 `pre_config` 回调先跑 Python。
- **UUT（Unit Under Test）**：被测器件，这里是 `en_cl_fix_round` 组件的实例。
- **`tb_path(runner_cfg)`**：VUnit 提供的函数，返回测试台源文件所在目录的绝对路径，让测试台能在任意工作目录下定位到仓库内的数据文件。
- **`generate` 语句**：VHDL 在「编译期/详细化期（elaboration）」展开的循环，用来一次性生成多份硬件结构。这里用来为每个测试用例生成一组独立的信号与进程。

---

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [tb/cl_fix_round_tb.vhd](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/tb/cl_fix_round_tb.vhd) | 舍入组件 `en_cl_fix_round` 的测试台，是本讲的主线范例。演示「读黄金数据 → generate 展开多用例 → 输入/UUT/检查三件套」的完整套路。 |
| [tb/util/en_cl_fix_fileio_pkg.vhd](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/tb/util/en_cl_fix_fileio_pkg.vhd) | 定点专用的文件 I/O 封装包，在 `en_tb` 通用文本 I/O 之上加了 `FixFormat_t` 感知，提供 `cl_fix_read_file` / `cl_fix_read_format_file` 等。 |
| [tb/en_cl_fix_pkg_tb.vhd](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/tb/en_cl_fix_pkg_tb.vhd) | RTL 包的纯组合自检测试台，**不读文件**，与本讲主范例形成对照，帮助理解「文件 I/O 测试台」与「断言测试台」的分工。 |

辅助参考（不在标题里，但会点到）：

| 文件 | 作用 |
|------|------|
| [lib/en_tb/hdl/en_tb_fileio_text_pkg.vhd](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/lib/en_tb/hdl/en_tb_fileio_text_pkg.vhd) | `en_tb` 的通用文本读写包，`cl_fix_read_file` 最终委托给它的 `read_file`。 |
| [lib/en_tb/hdl/en_tb_base_pkg.vhd](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/lib/en_tb/hdl/en_tb_base_pkg.vhd) | 定义 `SlvArray_t`、`Signedness_t` 等基础类型。 |
| [bittrue/cosim/cl_fix_round/cosim.py](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/cosim/cl_fix_round/cosim.py) | 生成黄金数据的 Python 脚本（见 u7-l1），产出本测试台读取的文件。 |

---

## 4. 核心概念与源码讲解

### 4.1 VUnit 测试台的运行骨架

#### 4.1.1 概念说明

一个 VUnit 测试台本质上是一个带特殊泛型 `runner_cfg : string` 的普通 VHDL 实体。VUnit 在启动仿真时，会把「要跑哪个测试、输出路径、是否 GUI」等信息编码进这个字符串传进来。测试台内部用一个标准流程消费它：

```
test_runner_setup(runner, runner_cfg)   -- 解析配置、初始化
        │
        ▼
while test_suite loop                   -- 只要还有测试可跑
    if run("test") then                 --   命中某个测试名
        ... 实际测试逻辑 ...
    end if;
end loop;
        │
        ▼
test_runner_cleanup(runner)             -- 收尾、上报通过/失败
```

这套骨架由 VUnit 的 `vunit_context` 提供。关键在于：`run("test")` 是一个**有副作用**的函数——第一次调用返回 `true` 并标记「这个测试已经在跑」，之后返回 `false`。`while test_suite` 则在所有测试跑完后自然退出。最后 `test_runner_cleanup` 负责把结果汇报给 VUnit 框架，决定退出码。

还有一个安全网：`test_runner_watchdog`。它给整个仿真设一个时间上限，如果测试逻辑卡死（例如 `wait` 永远等不到某个事件），看门狗会强制结束仿真并报失败，避免 CI 永久挂起。

#### 4.1.2 核心流程

以 `cl_fix_round_tb` 为例，它的主进程非常精简，因为真正的「干活」被搬到了 `generate` 里（见 4.4）。主进程只负责「等所有子测试完成」：

1. `test_runner_setup` 解析 `runner_cfg`。
2. 进入 `while test_suite`，命中唯一的测试名 `"test"`。
3. `wait until (and finished) = '1'`——等待所有子测试把各自的 `finished(i)` 拉高。
4. 打印 `SUCCESS! All tests passed.`，调用 `test_runner_cleanup`。

`finished` 是一个位数等于测试用例数的向量，每个子测试跑完会把自己那一位置 1。

#### 4.1.3 源码精读

实体声明只有一个泛型 `runner_cfg`，外加一个 `meta_width_g`（由 `run.py` 的 `add_config` 注入，用来覆盖测试组件的 meta 边带位宽）：

```vhdl
entity cl_fix_round_tb is
    generic(
        runner_cfg      : string;
        meta_width_g    : natural
    );
end cl_fix_round_tb;
```
这是 VUnit 测试台的标配入口，[tb/cl_fix_round_tb.vhd:44-49](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/tb/cl_fix_round_tb.vhd#L44-L49) 说明实体如何接收 VUnit 配置与可配置的 meta 宽度。

看门狗在架构一开始就挂上（100 ms 仿真时间上限）：

```vhdl
test_runner_watchdog(runner, 100 ms);
clk <= not clk after 5 ns;   -- 100 MHz 时钟，组合并发语句
```
见 [tb/cl_fix_round_tb.vhd:80-82](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/tb/cl_fix_round_tb.vhd#L80-L82)，看门狗与时钟同时启动。

主进程 `p_main` 就是上面那张流程图的直译：

```vhdl
p_main : process
begin
    test_runner_setup(runner, runner_cfg);
    while test_suite loop
        if run("test") then
            wait until (and finished) = '1';
        end if;
    end loop;
    print("SUCCESS! All tests passed.");
    test_runner_cleanup(runner);
end process;
```
完整代码在 [tb/cl_fix_round_tb.vhd:87-99](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/tb/cl_fix_round_tb.vhd#L87-L99)。注意 `(and finished)` 是 VHDL 的归约与（reduction），把整个 `finished` 向量按位与成一个标量——只有所有位都为 1 才会解除等待。

> **对照：纯组合自检测试台** `en_cl_fix_pkg_tb` 走的是同一条骨架，但**把所有断言直接写在主进程里、不读任何文件**，因为它测的是 RTL 包里的纯函数（`cl_fix_add_fmt`、`cl_fix_resize` 等），输入输出都是常量，无需 cosim 黄金数据。见 [tb/en_cl_fix_pkg_tb.vhd:62-741](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/tb/en_cl_fix_pkg_tb.vhd#L62-L741)，例如它直接断言 `check_equal(cl_fix_add_fmt((1,1,1),(0,7,0)), (1,8,1), ...)`。本讲主范例 `cl_fix_round_tb` 则是把这种「逐点断言」改造成了「读文件 + 逐拍比对」，以应对穷举成千上万个输入的场景。

#### 4.1.4 代码实践

1. **实践目标**：确认所有 VUnit 测试台共享同一条骨架，理解 `run.py` 是如何把测试名 `"test"` 与测试台关联起来的。
2. **操作步骤**：
   - 打开 `tb/en_cl_fix_pkg_tb.vhd`，找到它的 `p_main` 进程，对比它和 `cl_fix_round_tb` 的 `p_main` 在结构上的相同点（`test_runner_setup` / `while test_suite` / `run("test")` / `test_runner_cleanup`）和不同点（前者把断言内联，后者只 `wait until (and finished)`）。
   - 打开 `sim/run.py`，找到 `cl_fix_round_tb` 的配置块（约 L156-L165），观察 `lib.test_bench("cl_fix_round_tb")` 与 `get_tests("test")` 如何用字符串 `"test"` 把 Python 端和 VHDL 端的 `run("test")` 串起来。
3. **需要观察的现象**：`run("test")` 里的字符串与 `run.py` 里 `get_tests("test")` 的字符串必须完全一致（都是 `"test"`）。
4. **预期结果**：能口头复述「VUnit 通过 `runner_cfg` 把测试名传进 VHDL，`run(...)` 据此选择执行哪段逻辑」。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `test_runner_watchdog(runner, 100 ms)` 这行删掉，测试台还能正常工作吗？会有什么隐患？

> **答案**：测试逻辑本身仍能跑通（看门狗不参与功能逻辑）。但失去了「防卡死」保护：一旦某个子测试因为 bug 永远等不到 `out_valid`，仿真会无限期挂起，CI 不会失败而是超时。看门狗是安全网，不是功能的一部分。

**练习 2**：`wait until (and finished) = '1'` 中的 `(and finished)` 如果写成 `finished = (others => '1')`，语义相同吗？

> **答案**：基本相同——两者都表示「`finished` 的每一位都是 1」。`(and finished)` 是把向量归约成一个标量再和 `'1'` 比；后者是整体比较。前者更简洁，是本项目惯用写法。

---

### 4.2 en_cl_fix_fileio_pkg：定点数据的文件读写封装

#### 4.2.1 概念说明

VHDL 标准库 `std.textio` 只能读写很有限的类型（`integer`、`bit_vector`、`std_logic_vector` 等），而且**无法理解「定点格式」**——它不知道一串位该当有符号还是无符号解析。`en_tb` 子库的 `en_tb_fileio_text_pkg` 补了一层：它要求调用者显式给出 `signedness`（有符号/无符号）和位宽，从而能把一行文本解析成 `std_logic_vector`。

但定点测试台手里握的是 `FixFormat_t`，每次都要手动从格式算位宽、再判断符号性，太繁琐。`en_cl_fix_fileio_pkg` 就是这层「糖衣」：它把 `FixFormat_t` 直接当参数，内部自动推导出位宽与符号性，再委托给 `en_tb` 的底层函数。这样新增一个算子的测试台时，文件读写几乎是「填空」。

#### 4.2.2 核心流程

封装包提供两组对称的接口：

- **数据（位串）读写**：`cl_fix_read` / `cl_fix_read_file` / `cl_fix_write` / `cl_fix_write_file`，参数里带一个 `FixFormat_t`。
- **格式（`FixFormat_t`）读写**：`cl_fix_read_format` / `cl_fix_read_format_file` / `cl_fix_write_format` / `cl_fix_write_format_file`，专门处理形如 `"(1,4,-2)"` 的格式串。

核心转换是一个私有助手函数 `cl_fix_to_signedness`：根据 `Fmt.S` 决定把数据按有符号还是无符号解析。

```
cl_fix_read_file(filename, fmt)
        │  内部调用
        ▼
cl_fix_to_signedness(fmt)  ──►  Signed_s / Unsigned_s
cl_fix_width(fmt)          ──►  位宽 n_bits
        │  组装后委托
        ▼
en_tb.read_file(filename, n_bits, signedness, mode, skip)  ──►  SlvArray_t
```

#### 4.2.3 源码精读

整个封装包分「读」「写」两组，每组又分「数据」「格式」两类。包头声明在 [tb/util/en_cl_fix_fileio_pkg.vhd:111-224](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/tb/util/en_cl_fix_fileio_pkg.vhd#L111-L224)，文件顶部 L20-L89 有一份完整的「快速参考」注释，列出每个函数的签名。

私有助手 `cl_fix_to_signedness` 是一切封装的基石：

```vhdl
function cl_fix_to_signedness(constant Fmt : in FixFormat_t) return Signedness_t is
begin
    if Fmt.S = 1 then
        return Signed_s;
    else
        return Unsigned_s;
    end if;
end function;
```
见 [tb/util/en_cl_fix_fileio_pkg.vhd:236-243](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/tb/util/en_cl_fix_fileio_pkg.vhd#L236-L243)。`Signedness_t` 本身定义在 [lib/en_tb/hdl/en_tb_base_pkg.vhd:57](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/lib/en_tb/hdl/en_tb_base_pkg.vhd#L57)（`type Signedness_t is (Unsigned_s, Signed_s)`）。

「读整文件」的封装最薄——直接转发参数：

```vhdl
impure function cl_fix_read_file(
    constant Filename   : in string;
    constant Fmt        : in FixFormat_t;
    constant TextMode   : in text_data_mode_t := ascii_dec;
    constant SkipLines  : in natural := 1
) return SlvArray_t is
begin
    return read_file(Filename, cl_fix_width(Fmt), cl_fix_to_signedness(Fmt), TextMode, SkipLines);
end function;
```
见 [tb/util/en_cl_fix_fileio_pkg.vhd:286-294](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/tb/util/en_cl_fix_fileio_pkg.vhd#L286-L294)。注意它返回的 `SlvArray_t` 是「`std_logic_vector` 的数组」，定义在 [lib/en_tb/hdl/en_tb_base_pkg.vhd:62](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/lib/en_tb/hdl/en_tb_base_pkg.vhd#L62)（`type SlvArray_t is array(integer range<>) of std_logic_vector`）——这是一个二维 unconstrained 数组，每个元素是一个位宽为 `cl_fix_width(Fmt)` 的位串。

底层 `en_tb` 的 `read_file` 究竟做了什么？它会先用 `get_file_size_lines` 数出文件行数、用 `get_file_size_columns` 数出每行列数，再按行按列解析填充数组：

```vhdl
impure function read_file(...) return SlvArray_t is
    constant n_rows_c   : natural  := get_file_size_lines(filename) - skip;
    constant n_cols_c   : natural  := get_file_size_columns(filename, skip, mode);
    ...
    variable data_v     : SlvArray_t(0 to n_rows_c*n_cols_c-1)(n_bits-1 downto 0);
begin
    ...
end function;
```
见 [lib/en_tb/hdl/en_tb_fileio_text_pkg.vhd:1288-1300](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/lib/en_tb/hdl/en_tb_fileio_text_pkg.vhd#L1288-L1300)。它默认 `skip=1`（跳过一行表头），这正是 cosim 脚本写文件时用 `np.savetxt(..., header=...)` 产生的那行表头。

「读单行定点值」的封装展示了三层调用（line → textio read → en_tb read）：

```vhdl
procedure cl_fix_read(variable L : inout line; variable Data : out std_logic_vector;
                      constant Fmt : in FixFormat_t; constant TextMode : in text_data_mode_t := ascii_dec) is
begin
    read(L, Data, cl_fix_to_signedness(Fmt), TextMode);
end procedure;
```
见 [tb/util/en_cl_fix_fileio_pkg.vhd:254-262](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/tb/util/en_cl_fix_fileio_pkg.vhd#L254-L262)，它把 `en_tb` 的 `read(line, slv, signedness, mode)` 重新暴露成「带格式」版本。

> **关键点**：`TextMode` 默认 `ascii_dec`，即把文件里的十进制整数（如 `-3`）按指定位宽与符号性解析成位串。这与 cosim 脚本里 `np.savetxt(..., fmt="%i")` 写十进制整数完全对齐。

#### 4.2.4 代码实践

1. **实践目标**：理解「格式 → 位宽 + 符号性」的推导，并确认 `cl_fix_read_file` 与底层 `read_file` 的参数对应关系。
2. **操作步骤**：
   - 在 `tb/util/en_cl_fix_fileio_pkg.vhd` 中定位 `cl_fix_read_file`（L286）和 `cl_fix_to_signedness`（L236）。
   - 在 `lib/en_tb/hdl/en_tb_fileio_text_pkg.vhd` 中定位返回 `SlvArray_t` 的 `read_file` 重载（L386-L400 是声明，L1288 是实现），确认 `cl_fix_read_file` 传给它的参数顺序：`Filename, cl_fix_width(Fmt), cl_fix_to_signedness(Fmt), TextMode, SkipLines`。
   - 思考：对于一个 `[0,7,0]`（无符号 8 位）的格式，`cl_fix_to_signedness` 会返回什么？位宽是多少？
3. **需要观察的现象**：封装包本身不含任何定点运算，只是「格式 → (位宽, 符号性)」的翻译层。
4. **预期结果**：`[0,7,0]` → `Unsigned_s`、位宽 7；`[1,7,0]` → `Signed_s`、位宽 8。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `cl_fix_read_file` 默认 `SkipLines = 1`？如果改成 0 会怎样？

> **答案**：因为 cosim 脚本用 `np.savetxt` 写文件时总会带一行表头（如 `# r[8]`）。`SkipLines=1` 跳过这行表头，从第二行开始才是真正的数据。改成 0 会让解析器尝试把表头当数据读，通常报错或得到垃圾值。

**练习 2**：`cl_fix_read_file` 返回 `SlvArray_t`，而 cosim 存的是「未归一化整数」。这两者如何对应？

> **答案**：`SlvArray_t` 的每个元素就是一个位宽为 `cl_fix_width(Fmt)` 的 `std_logic_vector`，它的二进制值就等于那个未归一化整数 `d = v·2^(−F)`。也就是说，文件里的十进制整数被解析成了等价的二进制位串，符号性由格式决定。这正是「黄金数据可以直接和 UUT 输出逐位比对」的基础。

---

### 4.3 FixFormatArray_t 与黄金数据的读取

#### 4.3.1 概念说明

测试台在仿真开始时需要一次性读入三类「参数文件」：

- `a_fmt.txt`：每个测试用例的**输入格式**（一列 `FixFormat_t` 字符串）。
- `r_fmt.txt`：每个测试用例的**输出格式**。
- `rnd.txt`：每个测试用例的**舍入模式**（一列整数，0..6 对应七种 `FixRound_t`）。

前两者是格式，用一个专门的数组类型 `FixFormatArray_t` 装载；后者是普通整数，用 VHDL 内建的 `integer_vector` 装载。注意这些读取发生在**架构的并发常量声明区**（不是进程里），意味着它们在详细化期（仿真启动时）就被求值一次，之后全程只读。

`FixFormatArray_t` 是 RTL 包里定义的简单 unconstrained 数组：

```vhdl
type FixFormatArray_t is array(natural range <>) of FixFormat_t;
```
见 [hdl/en_cl_fix_pkg.vhd:47](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd#L47)。

#### 4.3.2 核心流程

读取格式文件比读取数据文件稍微复杂，因为 `FixFormat_t` 不是 `textio` 能直接解析的类型，而是一个形如 `"(1,4,-2)"` 的字符串。流程是：

```
cl_fix_read_format_file(filename)
        │
        ├── get_file_size_lines(filename) - SkipLines   ──►  数组长度
        │
        ├── for each line:
        │       readline → 得到一行文本
        │       cl_fix_read_format(line) :
        │           cl_fix_format_from_string(L.all)   ──►  解析 "(1,4,-2)" 为 FixFormat_t
        │
        └── 返回 FixFormatArray_t
```

`cl_fix_format_from_string` 是 RTL 包提供的字符串解析函数（见 [u5-l4](u5-l4-private-pkg-string-parsing.md)），它把 `"(S,I,F)"` 还原成 record。

#### 4.3.3 源码精读

测试台用 `tb_path(runner_cfg)` 定位数据目录——这是无论从哪个工作目录启动仿真都能找到文件的关键：

```vhdl
constant datapath_c : string := tb_path(runner_cfg) & "../bittrue/cosim/cl_fix_round/data/";
```
见 [tb/cl_fix_round_tb.vhd:56](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/tb/cl_fix_round_tb.vhd#L56)。`tb_path` 返回测试台 `.vhd` 文件所在目录（即 `tb/`），再向上回到仓库根、再下钻到 cosim 数据目录。注意此时 `data/` 目录可能还不存在——它由 `run.py` 在仿真前通过 `pre_config` 回调调用 cosim.py 生成（见 u7-l1、u7-l3）。

随后三个常量一次性把参数文件读进来：

```vhdl
constant a_fmt_c : FixFormatArray_t := cl_fix_read_format_file(datapath_c & "a_fmt.txt");
constant r_fmt_c : FixFormatArray_t := cl_fix_read_format_file(datapath_c & "r_fmt.txt");
constant rnd_c   : integer_vector   := read_file(datapath_c & "rnd.txt");
```
见 [tb/cl_fix_round_tb.vhd:59-63](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/tb/cl_fix_round_tb.vhd#L59-L63)。注意 `rnd_c` 直接用 `en_tb` 的 `read_file`（返回 `integer_vector` 的重载），因为舍入模式就是普通整数，无需定点封装。

测试用例总数直接取自格式数组的长度：

```vhdl
constant test_count_c : positive := a_fmt_c'length;
```
见 [tb/cl_fix_round_tb.vhd:65](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/tb/cl_fix_round_tb.vhd#L65)。这保证 VHDL 端的用例数与 Python cosim 生成的用例数自动一致——两边都由同一个文件驱动。

再看封装包里「读格式文件」的实现。它先数行数确定数组大小，再逐行调用 `cl_fix_read_format`：

```vhdl
impure function cl_fix_read_format_file(constant Filename : in string; constant SkipLines : in natural := 1)
return FixFormatArray_t is
    constant FormatCount_c  : natural := get_file_size_lines(Filename) - SkipLines;
    file     F              : text;
    variable Line_v         : line;
    variable Formats_v      : FixFormatArray_t(0 to FormatCount_c-1);
begin
    file_open(F, Filename, read_mode);
    skip_lines(F, SkipLines);
    for i in 0 to FormatCount_c-1 loop
        Formats_v(i) := cl_fix_read_format(F);
    end loop;
    deallocate(Line_v);
    file_close(F);
    return Formats_v;
end function;
```
见 [tb/util/en_cl_fix_fileio_pkg.vhd:330-349](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/tb/util/en_cl_fix_fileio_pkg.vhd#L330-L349)。`get_file_size_lines` 是 `en_tb` 提供的辅助函数（实现见 [lib/en_tb/hdl/en_tb_fileio_text_pkg.vhd:584-597](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/lib/en_tb/hdl/en_tb_fileio_text_pkg.vhd#L584-L597)，它遍历整个文件数行数——注释里警告大文件慎用）。

而单行解析 `cl_fix_read_format` 把整行字符串交给 RTL 包的解析器：

```vhdl
procedure cl_fix_read_format(variable L : inout line; variable Fmt : out FixFormat_t) is
begin
    Fmt := cl_fix_format_from_string(L.all);
    L := new string'("");   -- 清空已消费的 line
end procedure;
```
见 [tb/util/en_cl_fix_fileio_pkg.vhd:301-308](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/tb/util/en_cl_fix_fileio_pkg.vhd#L301-L308)。`L.all` 取出 line 内的字符串，`cl_fix_format_from_string`（在 [u5-l4](u5-l4-private-pkg-string-parsing.md) 讲过）把 `"(1,4,-2)"` 解析回 record。

#### 4.3.4 代码实践

1. **实践目标**：把「Python 写文件」与「VHDL 读文件」两端对齐，确认格式与编码一致。
2. **操作步骤**：
   - 打开 [bittrue/cosim/cl_fix_round/cosim.py](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/cosim/cl_fix_round/cosim.py)，定位 L118-L125：`cl_fix_write_formats(...)` 写 `a_fmt.txt`/`r_fmt.txt`，`np.savetxt(..., fmt="%i")` 写 `rnd.txt`。
   - 对照 [tb/cl_fix_round_tb.vhd:59-63](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/tb/cl_fix_round_tb.vhd#L59-L63) 的三个读取调用，确认文件名一一对应。
3. **需要观察的现象**：Python 写 `rnd.txt` 用十进制整数（`fmt="%i"`），VHDL 读 `rnd.txt` 用默认 `ascii_dec` 模式——两端匹配。
4. **预期结果**：能画出「cosim.py 写 3 个参数文件 → cl_fix_round_tb 读 3 个参数文件」的对应表。
5. **注意**：这些 `data/` 文件在仓库里默认不存在（运行仿真前由 cosim 生成），所以你直接 `ls` 会找不到——这是预期行为，不是缺失。

#### 4.3.5 小练习与答案

**练习 1**：`a_fmt_c'length` 为什么能直接当作测试用例数？

> **答案**：因为 cosim 脚本对每个有效测试用例都往 `a_fmt.txt` 写了一行格式（见 cosim.py L109、L118-L119 的 `test_a_fmt.append(...)` 与 `cl_fix_write_formats`）。所以文件行数 = 测试用例数。VHDL 端用数组长度做 `generate` 上界，天然与 Python 端同步。

**练习 2**：`rnd_c` 是 `integer_vector`，测试台后面怎么把它映射成 `FixRound_t` 枚举？

> **答案**：用 `FixRound_t'val(rnd_c(i))`（见 [tb/cl_fix_round_tb.vhd:156](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/tb/cl_fix_round_tb.vhd#L156)）。VHDL 的 `'val` 属性按位置把整数转成枚举值（0→`Trunc_s`，1→`NonSymPos_s`，…）。这要求 Python 端 `rnd.value` 的编码顺序与 VHDL 枚举声明顺序一致——两边的 `FixRound` 镜像保证了这一点。

---

### 4.4 generate 展开多测试用例 + 输入/检查进程逐拍比对

#### 4.4.1 概念说明

到这里，我们手里有了：测试用例数 `test_count_c`、每个用例的输入格式 `a_fmt_c(i)`、输出格式 `r_fmt_c(i)`、舍入模式 `rnd_c(i)`。接下来的核心技巧是用 **`for ... generate`** 在编译期把「一个测试台」展开成「`test_count_c` 个独立的并行子测试」。

为什么用 `generate` 而不是 `for` 循环？因为：

1. 每个子测试需要**不同位宽的信号**（`in_data` 的宽度依赖 `a_fmt_c(i)`，`out_data` 依赖 `r_fmt_c(i)`）。VHDL 里信号位宽在详细化期就定死，只有 `generate` 能为每个 `i` 生成不同宽度的声明。
2. 每个子测试需要**独立的 UUT 实例**，带不同的 generics。
3. 每个子测试的输入/检查进程**彼此独立并行**，互不阻塞。

每个 `generate` 分支内部是经典的「三件套」：

- **输入进程 `p_input`**：用和 cosim 完全相同的「计数器从 `Amin` 到 `Amax`」穷举所有输入值，逐拍喂给 UUT，同时随机生成 meta 边带。
- **UUT `i_uut`**：被测的 `en_cl_fix_round` 组件实例。
- **检查进程 `p_check`**：等 UUT 输出有效，逐拍把 `out_data` 与从文件读入的期望值 `Expected_c` 比对，失败时打印精确诊断。

#### 4.4.2 核心流程

```
g_test_case : for i in 0 to test_count_c-1 generate
        │
        ├── 声明该用例的常量：RandSeed_c、Amin、Amax、信号 rst/in_*/out_*
        │
        ├── p_input 进程：
        │       InitSeed(RandSeed_c)
        │       复位一拍
        │       for a in Amin to Amax:          -- 穷举所有输入值
        │           in_data <= cl_fix_from_integer(a, a_fmt_c(i))
        │           in_meta  <= 随机
        │           in_valid <= '1'
        │           等一个上升沿
        │
        ├── i_uut : en_cl_fix_round 实例
        │       in_fmt_g  => a_fmt_c(i)
        │       out_fmt_g => r_fmt_c(i)
        │       round_g   => FixRound_t'val(rnd_c(i))
        │       reg_mode_g=> RegisterMode_t'val(i mod reg_mode_count_c)
        │
        └── p_check 进程：
                Expected_c := cl_fix_read_file("test{i}_output.txt", r_fmt_c(i))
                InitSeed(RandSeed_c)               -- 同种子，复现同一组随机 meta
                for a in Amin to Amax:
                    等 out_valid 的上升沿
                    check_equal(out_meta, 再取一个随机 meta)   -- meta 透传检查
                    if out_data /= Expected_c(Idx):
                        print(精确诊断信息)
                        check_equal(out_data, Expected_c(Idx), ...)
                    Idx++
                finished(i) <= '1'                  -- 通知主进程本用例完成
```

一个精妙的细节：**输入进程和检查进程用同一个随机种子 `RandSeed_c`**。输入进程用它生成一串随机 meta，检查进程用同一个种子「再生成一遍」同样的随机序列，于是能在检查端精确复现期望的 meta 值——这样就不需要把 meta 也存进文件，又能验证 meta 被组件正确透传。

#### 4.4.3 源码精读

`generate` 循环头部声明了每个用例独立的常量与信号，注意 `in_data`/`out_data` 的位宽随 `i` 变化：

```vhdl
g_test_case : for i in 0 to test_count_c-1 generate
    constant RandSeed_c : string := "Metadata seed " & to_string(i);
    constant Amin       : integer := cl_fix_to_integer(cl_fix_min_value(a_fmt_c(i)), a_fmt_c(i));
    constant Amax       : integer := cl_fix_to_integer(cl_fix_max_value(a_fmt_c(i)), a_fmt_c(i));
    signal rst          : std_logic;
    signal in_data      : std_logic_vector(cl_fix_width(a_fmt_c(i))-1 downto 0);
    signal out_data     : std_logic_vector(cl_fix_width(r_fmt_c(i))-1 downto 0);
    ...
```
见 [tb/cl_fix_round_tb.vhd:104-117](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/tb/cl_fix_round_tb.vhd#L104-L117)。`Amin`/`Amax` 是该格式下未归一化整数的最小/最大值，正是 cosim 里 `get_data` 穷举的同一范围（见 [u7-l1](u7-l1-cosim-concept-scripts.md)），所以两边输入序列逐拍对齐。

输入进程按计数器逐拍注入，复现 cosim 的穷举顺序：

```vhdl
p_input : process
    variable Random_v : RandomPType;
begin
    Random_v.InitSeed(RandSeed_c);
    rst <= '1'; in_valid <= '0';
    wait until rising_edge(clk);
    rst <= '0';
    for a in Amin to Amax loop
        in_valid <= '1';
        in_meta  <= Random_v.RandSlv(meta_width_g);
        in_data  <= cl_fix_from_integer(a, a_fmt_c(i));
        wait until rising_edge(clk);
    end loop;
    in_valid <= '0'; in_meta <= (others => 'X'); in_data <= (others => 'X');
    wait;
end process;
```
见 [tb/cl_fix_round_tb.vhd:122-147](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/tb/cl_fix_round_tb.vhd#L122-L147)。`RandomPType` 与 `RandSlv` 来自 OSVVM 的 `RandomPkg`（见文件头 L27-L28 的 `library osvvm; use osvvm.RandomPkg.all;`）。注释 L133-L134 明确说明：cosim 用计数器生成所有可能输入，这里复现同一模式。

UUT 实例把第 `i` 个用例的参数映射成 generics，并用 `i mod reg_mode_count_c` 在三种寄存器模式间轮换：

```vhdl
i_uut : entity work.en_cl_fix_round
generic map(
    in_fmt_g   => a_fmt_c(i),
    out_fmt_g  => r_fmt_c(i),
    round_g    => FixRound_t'val(rnd_c(i)),
    reg_mode_g => RegisterMode_t'val(i mod reg_mode_count_c),  -- Toggle between test cases.
    meta_width_g => meta_width_g
)
port map( clk => clk, rst => rst, in_valid => in_valid, in_meta => in_meta,
          in_data => in_data, out_valid => out_valid, out_meta => out_meta,
          out_data => out_data );
```
见 [tb/cl_fix_round_tb.vhd:152-172](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/tb/cl_fix_round_tb.vhd#L152-L172)。`reg_mode_count_c` 在 L66 定义为 `1 + RegisterMode_t'pos(RegisterMode_t'high)`——这是 VHDL-93 下枚举元素个数的写法（注释提到 VHDL-2019 可直接用 `RegisterMode_t'length`），等于 3，对应 `Auto_s`/`Yes_s`/`No_s` 三态（见 [u6-l1](u6-l1-pipelined-components-registermode.md)）。这样一轮跑下来能覆盖三种寄存器模式。

检查进程是「逐拍比对」的核心。它在进程声明区把整个期望输出文件读进 `Expected_c`（同样是详细化期一次求值），然后用同种子复现随机 meta 做透传检查，逐拍比数据：

```vhdl
p_check : process
    constant Expected_c : SlvArray_t := cl_fix_read_file(DataPath_c & "test" & to_string(i) & "_output.txt", r_fmt_c(i));
    variable Idx_v      : natural := 0;
    variable Random_v   : RandomPType;
begin
    Random_v.InitSeed(RandSeed_c);
    for a in Amin to Amax loop
        wait until out_valid = '1' and rising_edge(Clk);
        -- Check metadata
        check_equal(out_meta, Random_v.RandSlv(meta_width_g), "Metadata mismatch");
        -- Check data against cosim
        if out_data /= Expected_c(Idx_v) then
            print("Error in test case " & to_string(i) & " while rounding " & str(a, a_fmt_c(i)) & " " & to_string(a_fmt_c(i))
                  & " [rnd: " & to_string(FixRound_t'val(rnd_c(i))) & "] --> " & to_string(r_fmt_c(i)));
            check_equal(out_data, Expected_c(Idx_v), "Error at index " & to_string(Idx_v));
        end if;
        Idx_v := Idx_v + 1;
    end loop;
    finished(i) <= '1';
end process;
```
见 [tb/cl_fix_round_tb.vhd:177-203](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/tb/cl_fix_round_tb.vhd#L177-L203)。这里有几个要点：

- **期望数据按用例分文件**：每个用例 `i` 对应一个 `test{i}_output.txt`，用 `cl_fix_read_file(..., r_fmt_c(i))` 读成 `SlvArray_t`。这呼应 cosim.py L104-L106 里 `np.savetxt(..., f"test{test_count}_output.txt", ...)`。
- **比对与诊断分离**：先用 `if out_data /= Expected_c(Idx_v)` 做轻量不等判断，**只在出错时**才 `print` 一长串上下文（测试号、被舍入的实数值、输入格式、舍入模式、输出格式），再调 `check_equal` 让 VUnit 记录失败。这样失败信息极其精确，成功时零开销。
- **`str` 助手函数**：定义在 [tb/cl_fix_round_tb.vhd:73-76](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/tb/cl_fix_round_tb.vhd#L73-L76)，把一个未归一化整数 `x` 经 `cl_fix_from_integer` → `cl_fix_to_real` → `to_string` 还原成可读的实数值，便于人类阅读错误信息。
- **延迟鲁棒**：检查进程用 `wait until out_valid = '1' and rising_edge(Clk)` 而非固定延迟，因此无论 UUT 因 `reg_mode_g` 不同而是 0 拍还是 1 拍延迟（见 [u6-l1](u6-l1-pipelined-components-registermode.md)），都能正确捕获输出——这正是 u6-l2 强调的「用 valid 握手而非计数」的好处。

#### 4.4.4 代码实践

1. **实践目标**：画出本测试台的完整数据流，并定位「读哪些文件、比对失败打印什么」。
2. **操作步骤**：
   - 对照 [tb/cl_fix_round_tb.vhd:59-63](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/tb/cl_fix_round_tb.vhd#L59-L63)（参数文件）和 [tb/cl_fix_round_tb.vhd:178](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/tb/cl_fix_round_tb.vhd#L178)（期望输出文件），列出测试台读取的全部文件名。
   - 画出数据流图：`p_input`（写 `in_data/in_meta/in_valid`）→ `i_uut`（读入写出）→ `p_check`（读 `out_*`，比对 `Expected_c`）。
   - 在 [tb/cl_fix_round_tb.vhd:191-197](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/tb/cl_fix_round_tb.vhd#L191-L197) 找到比对失败时打印的诊断信息，逐字段说明：测试号、实数值、输入格式、舍入模式、输出格式、出错索引。
3. **需要观察的现象**：
   - 参数文件（`a_fmt.txt`/`r_fmt.txt`/`rnd.txt`）在架构顶层读一次，被所有用例共享。
   - 期望输出文件（`test{i}_output.txt`）在**每个用例的检查进程内**单独读一次，只含该用例的输出。
4. **预期结果**：得到一张「文件 → 读取位置 → 用途」的表，类似：

   | 文件 | 读取处 | 用途 |
   |------|--------|------|
   | `a_fmt.txt` | 架构顶层 L59 | 每用例输入格式 |
   | `r_fmt.txt` | 架构顶层 L60 | 每用例输出格式 |
   | `rnd.txt` | 架构顶层 L63 | 每用例舍入模式 |
   | `test{i}_output.txt` | 检查进程内 L178 | 第 i 用例的期望输出 |

5. **运行建议（待本地验证）**：在装好 GHDL/NVC + VUnit 的环境里执行 `python sim/run.py --simulator=ghdl cl_fix_round_tb`（具体命令以仓库 README 的 Running Tests 章节为准）。观察 VUnit 先调用 cosim.py 生成 `data/`，再编译运行测试台；若一切正常，最终打印 `SUCCESS! All tests passed.`。若本地无仿真器，可只运行 `python bittrue/cosim/cl_fix_round/cosim.py`，肉眼检查 `data/` 下生成的文件格式与本讲描述一致。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `p_check` 里要比对 `out_meta`？它和 cosim 数据有关系吗？

> **答案**：`out_meta` 是组件透传的边带信号，**不是** cosim 算出的数值结果。它由 `p_input` 用随机数生成、经 UUT 透传到输出。检查端用**同一个种子**再生成一遍随机序列来比对，验证「meta 与数据同拍穿过每一级寄存器、延迟一致」（见 [u6-l2](u6-l2-building-pipelined-datapath.md)）。这是对流水线行为的额外覆盖，与 cosim 黄金数据无关。

**练习 2**：`p_check` 为什么用 `wait until out_valid = '1' and rising_edge(Clk)`，而不是 `wait for 10 ns` 之类固定延迟？

> **答案**：因为不同用例的 `reg_mode_g` 不同（`i mod 3` 轮换 `Auto/Yes/No`），UUT 的输出延迟可能是 0 拍（组合）或 1 拍（寄存）。固定延迟无法同时适配两种情况；用 `out_valid` 握手则「数据有效时才比对」，对任意延迟都鲁棒。

**练习 3**：`g_test_case` 为什么必须用 `generate` 而不是进程里的 `for` 循环？

> **答案**：两个硬约束。其一，`in_data`/`out_data` 的位宽依赖 `cl_fix_width(a_fmt_c(i))`/`cl_fix_width(r_fmt_c(i))`，而信号位宽必须在详细化期定死，进程内的 `for` 循环无法为每次迭代声明不同位宽的信号。其二，每个用例需要独立的 UUT 实例（不同 generics），实例化语句只能出现在并发区，不能放在进程里。`generate` 正是为「编译期批量生成结构与进程」设计的。

---

## 5. 综合实践

把本讲知识串起来，完成一个「**只读不跑**的源码追踪任务」：

**任务**：假设你要为一个新的算子（比如 `cl_fix_mult`）读懂它的测试台，请用本讲建立的框架，在 `tb/cl_fix_mult_tb.vhd` 上完成以下分析。

1. **运行骨架**：找到它的 `p_main`，确认是否同样是 `test_runner_setup` → `while test_suite` → `run("test")` → `wait until (and finished)` → `test_runner_cleanup`。（提示：它的结构与 `cl_fix_round_tb` 几乎一致。）
2. **黄金数据读取**：它读取了哪些参数文件？（提示：乘法有两个输入，所以会有 `a_fmt.txt`、`b_fmt.txt`、`r_fmt.txt`。）在文件里定位对应的 `cl_fix_read_format_file` 与 `read_file` 调用，确认每个文件读取的行号。
3. **generate 三件套**：找到它的 `g_test_case`（或同名 generate），指出它的输入进程如何**同时**穷举两个操作数（提示：嵌套循环 `for a ... for b ...`），以及检查进程从 `test{i}_output.txt` 读取期望输出。
4. **文件 I/O 封装**：确认它用的 `cl_fix_read_file` / `cl_fix_read_format_file` 都来自 `en_cl_fix_fileio_pkg`（在 `use work.en_cl_fix_fileio_pkg.all;` 里），而非直接调 `en_tb`。
5. **诊断信息**：找到它比对失败时 `print` 的诊断字符串，说明它打印了哪些上下文字段（两个操作数格式、结果格式等）。

**交付物**：一张「`cl_fix_mult_tb` 数据流图」+ 一张「文件 → 读取行号 → 用途」表。完成后，你应当能独立读懂仓库里任何一个 `cl_fix_*_tb.vhd` 测试台——它们都遵循本讲讲解的同一套套路。

> 提示：可以参考 `tb/cl_fix_mult_tb.vhd` 中 `AFmt_c`/`BFmt_c`/`RFmt_c` 的声明（结构与 [tb/cl_fix_round_tb.vhd:59-63](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/tb/cl_fix_round_tb.vhd#L59-L63) 一致）。

---

## 6. 本讲小结

- VUnit 测试台共享同一条骨架：`test_runner_setup` → `while test_suite` / `run("test")` → `test_runner_cleanup`，并用 `test_runner_watchdog` 防卡死；测试名 `"test"` 由 `run.py` 的 `get_tests` 与 VHDL 的 `run(...)` 字符串对齐。
- `en_cl_fix_fileio_pkg` 是一层「格式感知」的糖衣：把 `FixFormat_t` 翻译成 `(位宽, 符号性)`，委托给 `en_tb` 的通用 `read_file`/`write_file`；定点测试台因此无需手算位宽。
- 黄金参数分两类：格式用 `FixFormatArray_t`（经 `cl_fix_read_format_file` → `cl_fix_format_from_string` 解析 `"(S,I,F)"`），舍入模式用普通 `integer_vector`（直接 `read_file`）；它们在架构顶层一次性读入，测试用例数取自 `a_fmt_c'length`。
- `for ... generate` 在编译期把一个测试台展开成多个并行子测试，每个子测试有独立位宽的信号、独立的 UUT 实例与独立的输入/检查进程——这是为了支持「每个用例格式不同」。
- 检查进程用 `out_valid` 握手（而非固定延迟）逐拍比对，对任意寄存器模式鲁棒；失败时打印精确诊断（测试号、实数值、格式、舍入模式），成功时零开销；meta 透传用「同种子双随机源」验证，无需存文件。
- 对照 `en_cl_fix_pkg_tb`：它是纯组合、不读文件、断言内联的自检测试台；本讲的 `cl_fix_round_tb` 则是「读 cosim 黄金数据 + 逐拍比对」，专门服务于穷举验证。

---

## 7. 下一步学习建议

- **继续验证闭环**：下一讲 [u7-l3 run.py 装配：库、配置与多仿真器适配](u7-l3-runpy-simulator-wiring.md) 讲 `run.py` 如何把这些测试台装进 VUnit 工程、绑定 `pre_config=cosim.run` 回调，并用 `add_config` 注入 `meta_width_g`——补上「数据从哪里来」的最后一块拼图。
- **回看 RTL**：若对检查进程里出现的 `cl_fix_from_integer` / `cl_fix_to_real` / `cl_fix_format_from_string` 不熟，可复习 [u5-l1](u5-l1-vhdl-package-types-api.md)（公共 API）与 [u5-l4](u5-l4-private-pkg-string-parsing.md)（字符串解析）。
- **动手扩展**：尝试照着 `cl_fix_round_tb.vhd` 的模板，为一个新格式的舍入用例手写一份迷你测试台（哪怕只覆盖两三个输入），体会「输入进程 + UUT + 检查进程」三件套的复用性。

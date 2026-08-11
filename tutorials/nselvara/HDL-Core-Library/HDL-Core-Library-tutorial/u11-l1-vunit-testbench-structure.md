# VUnit 测试台结构

## 1. 本讲目标

本讲是「验证方法学」单元的第一讲，承接 u1-l3（你已经在那里跑通过一次全量仿真）。此前你站在 `test_runner.py` 的使用者视角，知道它会「自动发现并运行所有测试台」；本讲要把镜头拉近到**单个测试台文件内部**，拆解 VUnit 测试台的骨架。

学完本讲你应该能够：

1. 逐行说清一个 VUnit 测试台的最小必需结构：通用量 `runner_cfg` / `tb_path`、`test_runner_setup` / `test_runner_cleanup`、`test_runner_watchdog`。
2. 理解 `while test_suite loop ... run("xxx") ... end loop;` 是如何让 VUnit「发现用例」并在一次仿真里「逐个驱动用例」的。
3. 看懂编译期指令 `-- vunit: run_all_in_same_sim` 的作用，以及它如何把 N 个用例从「N 次仿真」压成「1 次仿真」。
4. 仿照 `tb_spi_tx` 这个模板，自己动手为 `debouncer` 写出一个能被 `test_runner.py` 自动发现并执行的最小测试台。

## 2. 前置知识

在进入源码前，先用三段话把「为什么要 VUnit」讲清楚。如果你已熟悉可跳过。

- **测试台（testbench）是什么。** 设计源码（如 `debouncer.vhd`）描述的是会被综合成真实电路的逻辑；测试台（如 `tb_debouncer.vhd`）只活在仿真器里，它给设计「喂激励、看输出」，本身不会被综合。所以测试台可以放心使用 `wait`、`time`、`report`、随机数这些不可综合的构造。
- **没有 VUnit 之前怎么测。** 传统做法是在测试台里手写一段过程：复位、加激励、`assert` 比对、`std.env.stop` 结束。一个文件只能跑「一个场景」。要跑很多场景，就要么写很多文件，要么在一个文件里串成一大坨 `if`。VUnit（VHDL Verification Methodology Framework）要解决的就是这件事：**让一个测试台文件自然地承载多个独立用例，并由外部脚本统一发现、编译、运行、汇总结果。**
- **VUnit 的两副面孔。** VUnit 同时是一个 **Python 框架**（负责扫描文件、调仿真器、汇总通过/失败）和一个 **VHDL 库 `vunit_lib`**（提供测试台里用的过程与信号）。本讲只关心后者——也就是测试台 `.vhd` 文件里要写的那些东西；前者（Python 侧）已在 u1-l3 讲过。

> 关于 VUnit 与 `test_runner.py` 的三层调用链、`runner_cfg` 如何被 VUnit 注入、以及「测试发现」的全局视角，已在 u1-l3 详细讲过。本讲不重复，只聚焦**测试台文件内部**。

## 3. 本讲源码地图

本讲以两个测试台为对照模板，外加一个被测对象：

| 文件 | 作用 |
| --- | --- |
| [ip/communication/spi/tb/tb_spi_tx.vhd](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/communication/spi/tb/tb_spi_tx.vhd) | **主模板**。SPI 发送模块的测试台，含 4 个用例、`main` + `checker` 双进程，结构干净，是本讲逐行讲解的对象。 |
| [ip/memories/fifo/tb/tb_fifo_sync.vhd](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/tb/tb_fifo_sync.vhd) | **对照模板**。同步 FIFO 测试台，含 9 个用例，并演示「同文件例化两套 architecture 做等价性回归」。 |
| [ip/debouncer/tb/tb_debouncer.vhd](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/debouncer/tb/tb_debouncer.vhd) | 消抖器现有测试台，是第 5 节综合实践的「参考答案」与对比基准。 |
| [ip/debouncer/debouncer.vhd](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/debouncer/debouncer.vhd) | 综合实践的被测对象（DUT）：三端口消抖器。 |
| [ip/test_runner.py](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/test_runner.py) | VUnit 的薄包装器，用 `tb_pattern="**"` 扫描 `ip/` 下所有测试台并运行。 |

## 4. 核心概念与源码讲解

### 4.1 两个魔法通用量：`runner_cfg` 与 `tb_path`

#### 4.1.1 概念说明

一个普通的 VHDL `entity` 要被 VUnit 识别为「测试台」，唯一的硬性标志是：它的 `generic` 里必须出现一个名叫 `runner_cfg` 的字符串通用量。这是 VUnit 与测试台之间的「接头暗号」——VUnit 在运行时会把这个通用量塞成一串配置字符串（告诉测试台：现在要跑哪个用例、输出目录在哪、是否开 GUI……），测试台再原样交给 VUnit 的过程去解析。

本库的测试台还统一多带了一个 `tb_path` 通用量：它是 VUnit 自动填入的「本测试台文件所在目录路径」，主要用来给随机数发生器播种，使每个测试台的随机序列可复现又互不干扰。

#### 4.1.2 核心流程

```
VUnit 扫描 ip/ 下所有 tb_*.vhd
        │
        ├── 发现某 entity 含 generic runner_cfg  ──► 认定为测试台
        │
        └── 运行时给该 entity 注入：
              runner_cfg := "活动用例名 + 输出路径 + 各类开关 ..."
              tb_path    := "<该 .vhd 文件所在目录>"
```

注意一个不对称细节：`runner_cfg` 有默认值 `runner_cfg_default`（这样文件能脱离 VUnit 被单独分析编译），而 `tb_path` **没有默认值**——它只能由 VUnit 在运行时提供。

#### 4.1.3 源码精读

两个通用量的声明在 `tb_spi_tx` 的 entity 里：

[`tb_spi_tx` 的 entity 与两个通用量，L25-L30](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/communication/spi/tb/tb_spi_tx.vhd#L25-L30) —— 注意 `runner_cfg` 带默认值 `runner_cfg_default`，而 `tb_path` 是裸 `string` 无默认值。`runner_cfg_default` 与稍后会看到的 `runner`、`test_suite`、`run` 等都来自下面这两行引入的 VUnit 上下文：

[`vunit_lib` 与 `vunit_context`，L15-L16](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/communication/spi/tb/tb_spi_tx.vhd#L15-L16) —— `context vunit_lib.vunit_context` 一次性 `use` 了 VUnit 的运行时库（过程、函数、信号类型），是整个骨架能编译的前提。

`tb_path` 的真实用途在 `checker` 进程的初始化处一目了然：

[用 `tb_path` 给随机数播种，L310](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/communication/spi/tb/tb_spi_tx.vhd#L310) —— `random.InitSeed(tb_path & random'instance_name)` 把「文件路径」与「进程实例名」拼成种子。路径让不同测试台的序列不同，实例名让同一文件内多个实例也不同。`tb_fifo_sync` 里是完全相同的写法：

[`tb_fifo_sync` 的播种与通用量声明，L534 与 L24-L29](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/tb/tb_fifo_sync.vhd#L24-L29) —— 两个测试台的 entity 头部完全同构，可见这是一份全库统一的模板。

#### 4.1.4 代码实践（源码阅读型）

1. **目标**：确认全库测试台的通用量声明是否真的统一。
2. **步骤**：在本仓库用编辑器打开任意两个测试台（如 `tb_spi_tx.vhd`、`tb_rom.vhd`），定位到 `entity ... is generic (...)`。
3. **观察**：是否都恰好含 `runner_cfg: string := runner_cfg_default;` 与 `tb_path: string`，顺序与默认值是否一致。
4. **预期**：全部 12 个测试台的 entity 头部写法完全一致——这正是 `test_runner.py` 用通配符批量发现它们的根本前提。

#### 4.1.5 小练习与答案

- **Q1**：如果把 `runner_cfg` 这个 generic 改名成 `my_cfg`，会发生什么？
  - **答**：VUnit 不再把它识别为测试台，`test_runner.py` 扫描时直接跳过该文件，里面的用例一个都不会跑。`runner_cfg` 这个名字是 VUnit 硬编码的接头暗号，不能改。
- **Q2**：为什么 `tb_path` 不给默认值、而 `runner_cfg` 要给？
  - **答**：`runner_cfg` 有默认值是为了让文件能被单独 `analyze`（语法/类型检查）通过；`tb_path` 无默认值是因为它只在运行期由 VUnit 提供才有意义，留空反而提醒使用者「这个值来自外部」。

---

### 4.2 仿真生命周期：`setup` / 看门狗 / `cleanup`

#### 4.2.1 概念说明

VUnit 测试台里有一个**隐式信号 `runner`**（由 `vunit_context` 引入，无需自己声明）。它是 VUnit 运行时的「状态机枢纽」：测试在跑、跑完、还是挂死了，都靠它的状态体现。围绕 `runner` 有三个动作，构成一次仿真的完整生命周期：

- `test_runner_setup(runner, runner_cfg)`：在 `main` 进程开头调用，把 `runner_cfg` 解析进 `runner`，标志着「仿真开始、当前用例进入运行态」。
- `test_runner_watchdog(runner, timeout)`：一条**并发过程调用**，挂在 architecture 体内（不在任何进程里），像一只看门狗盯着 `runner`，防止仿真挂死。
- `test_runner_cleanup(runner)`：在 `main` 进程末尾调用，标志着「当前用例结束、仿真收尾」。少写这一句，VUnit 会判定仿真没有正常完成。

#### 4.2.2 核心流程

```
main 进程:                              并发区:
  test_runner_setup(runner, runner_cfg)    test_runner_watchdog(runner, T)
        │                                          │
        │   runner 进入「运行态」  ◄────────────────┘ 每隔 T 检查一次：
        │                                          若 runner 仍在运行态且超时未推进 ──► 判失败
        ▼
  (等待 simulation_done，期间 checker 在跑用例)
        │
  test_runner_cleanup(runner)
        │
        ▼  runner 进入「完成态」，仿真结束
  wait;
```

要点：`setup` 与 `cleanup` 必须成对出现在 `main` 进程里；`watchdog` 是并发语句，与进程平级、独立运行。看门狗的超时是**按用例计**的——每个用例开始时计时重置，所以它约束的是「单个用例不能跑太久」，而不是「整场仿真不能超过 T」。

#### 4.2.3 源码精读

看门狗是 architecture 并发区的一条语句，紧跟在时钟生成之后：

[`tb_spi_tx` 的看门狗，L86](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/communication/spi/tb/tb_spi_tx.vhd#L86) —— `test_runner_watchdog(runner, SIMULATION_TIMEOUT_TIME);`，其中 `SIMULATION_TIMEOUT_TIME` 是测试台自己定义的常量 `1 ms`（见 [L37](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/communication/spi/tb/tb_spi_tx.vhd#L37)）。注意它**没有**包在任何 `process` 里——它本身就是一个并发过程调用，等价于 VUnit 替你起了一个后台进程。

`main` 进程负责「开场」与「收场」：

[`tb_spi_tx` 的 main 进程，L88-L102](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/communication/spi/tb/tb_spi_tx.vhd#L88-L102) —— 结构永远是：`test_runner_setup` →（可选的 `info`/调试开关）→ `wait until simulation_done` → `test_runner_cleanup` → `wait;`。最后的 `wait;` 让进程永远挂起，避免进程跑到底后 VHDL 仿真自然结束、干扰 VUnit 的状态判断。

对照 `tb_debouncer`，它的超时更长：

[`tb_debouncer` 的看门狗与超时常量，L64 与 L36](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/debouncer/tb/tb_debouncer.vhd#L64) —— 这里 `SIMULATION_TIMEOUT_TIME` 取 `10 ms`（[L36](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/debouncer/tb/tb_debouncer.vhd#L36)），比 SPI/FIFO 的 `1 ms` 大一个数量级。原因是消抖靠计数器稳定判定，单次翻转要等满 \(2^{\text{DEBOUNCE\_SYNC\_BITS}}\) 拍，仿真时间更长，看门狗必须给足预算。

#### 4.2.4 代码实践（参数实验型）

1. **目标**：直观感受看门狗的作用。
2. **步骤**：复制一份 `tb_debouncer.vhd` 为 `tb_debouncer_wd.vhd`（注意改 entity 名），把 `SIMULATION_TIMEOUT_TIME` 从 `10 ms` 改成 `1 us`，再跑 `test_runner.py`。
3. **观察**：某个用例还没跑完（消抖计数还没计满），仿真就被判失败，报错信息里会出现 watchdog / timeout 字样。
4. **预期**：看门狗在用例推进超时时强制失败，证明它是「按用例计时的安全网」。改回 `10 ms` 后全部通过。
5. 若本地无仿真器：**待本地验证**，但可从源码断定 `1 us` 必然小于 \(2^4 = 16\) 个 10 ns 时钟周期（160 ns 量级的需求），看门狗必然触发。

#### 4.2.5 小练习与答案

- **Q1**：如果删掉 `test_runner_cleanup(runner)`，VUnit 会怎么表现？
  - **答**：仿真虽然把用例逻辑跑完了，但 `runner` 没被置成「完成态」，VUnit 会认为仿真异常结束，通常判该用例失败或卡住。`setup` 与 `cleanup` 必须成对。
- **Q2**：看门狗为什么写在并发区而不是 `main` 进程里？
  - **答**：它要和 `main`、`checker`、时钟生成**并行**地独立计时。若写进某个进程，就会随该进程的 `wait` 节奏走，失去「旁观者」的独立性。并发过程调用本身就是一条独立的隐式进程。

---

### 4.3 用例的发现与驱动：`test_suite` 循环与 `run()`

#### 4.3.1 概念说明

VUnit 的「用例（test case）」不是文件，而是**一个名字**。这个名字以字符串形式出现在 `run("xxx")` 调用里。VUnit 在编译期静态扫描测试台源码，把所有 `run("...")` 的字符串字面量收集起来，作为「这个测试台包含哪些用例」的清单——这就是用例的**发现**。

而**驱动**用例的，是 `checker` 进程里的一段固定模式：

```vhdl
while test_suite loop
    if run("test_a") then
        ... -- 用例 a 的过程
    elsif run("test_b") then
        ... -- 用例 b 的过程
    else
        assert false report "No test has been run!" severity failure;
    end if;
end loop;
```

- `test_suite` 是 VUnit 提供的布尔函数：「只要还有用例没跑，就返回 true」。
- `run("test_a")` 也是布尔函数：「如果 VUnit 当前点名要跑的正是 `test_a`，就返回 true」。
- 于是循环每迭代一次，就让「当前被点名的那个用例」的分支执行；循环结束意味着所有用例都跑过一遍。
- 末尾的 `else ... assert false` 是一道护栏：如果你把某个 `run("test_xxx")` 的名字拼错了（与 VUnit 发现到的清单对不上），仿真会**立即失败**而不是静默跳过。

#### 4.3.2 核心流程

```
编译期：VUnit 扫描源码 → 收集所有 run("...") 字面量 → 用例清单 = {test_a, test_b, ...}

运行期（每个用例一次，或合并见 4.4）：
  checker 进程:
    wait 一拍;                 -- 必须有，见下方说明
    while test_suite loop
        当前活动用例 = 由 runner_cfg 指定
        if   run("test_a") then 执行过程 test_a
        elsif run("test_b") then 执行过程 test_b
        else 报错 "No test has been run!"
        end if;
    end loop;
    simulation_done <= true;   -- 通知 main 进程收尾
```

一个关键细节：循环之前的 `wait_clk_cycles(1);`（或任意 `wait`）**不能删**。本库每个测试台都在此处贴了注释 `-- Don't remove, else VUnit will not run the test suite`。没有这一拍等待，`test_suite` 在首个 delta 周期里的求值会与 VUnit 运行时的握手错位，导致循环体一次都不执行、用例全部「消失」。这是 VUnit 框架的一个时序约定，按模板照抄即可。

#### 4.3.3 源码精读

`tb_spi_tx` 的 `checker` 进程把 4 个用例组织在循环里：

[`tb_spi_tx` 的 test_suite 循环，L315-L327](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/communication/spi/tb/tb_spi_tx.vhd#L315-L327) —— 4 个 `run(...)` 分支对应 4 个用例，末尾 `else` 护栏，循环结束后置 `simulation_done <= true`。

循环前的那条「不可删」的等待：

[`tb_spi_tx` 循环前的强制等待，L312-L313](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/communication/spi/tb/tb_spi_tx.vhd#L312-L313) —— 注释 `-- NOTE: Don't remove, else VUnit will not run the test suite`。`tb_fifo_sync` 里有逐字相同的注释（[L536-L537](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/tb/tb_fifo_sync.vhd#L536-L537)），说明这是全库共识。

用例数可以更多——`tb_fifo_sync` 装了 9 个用例：

[`tb_fifo_sync` 的 9 用例循环，L539-L561](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/tb/tb_fifo_sync.vhd#L539-L561) —— 从 `test_empty_fifo` 到 `test_stress_operations`，结构完全一致。同一个文件、同一次编译，就能产出 9 条独立的测试结果。

#### 4.3.4 代码实践（改写观察型）

1. **目标**：亲眼看 `run("...")` 的字符串如何变成「用例」。
2. **步骤**：复制 `tb_spi_tx.vhd` 为试验文件（改 entity 名以免冲突），在循环里把 `run("test_reset_behavior")` 临时改成 `run("test_reset_behaviour")`（拼成英式拼写），其余不动，跑 `test_runner.py`。
3. **观察**：仿真立即在 `else` 分支失败，报 `No test has been run!`。
4. **预期**：因为 VUnit 静态扫描到的新字面量 `test_reset_behaviour` 与源码里过程名/原用例名对不上，护栏生效。这正是 `else` 护栏的价值。
5. 若本地无仿真器：**待本地验证**；但从源码 [L325](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/communication/spi/tb/tb_spi_tx.vhd#L325) 的 `assert false ... severity failure` 可确定必然报错。

#### 4.3.5 小练习与答案

- **Q1**：`run("test_a")` 里的字符串，能不能用变量拼接（如 `run("test_" & name)`）？
  - **答**：不能用于**发现**。VUnit 的用例发现是编译期静态扫描字符串字面量，变量拼接的值运行期才确定，扫描器看不到，会被当成「这个文件没有用例」。用例名必须是写死的字面量。
- **Q2**：为什么末尾要 `else assert false`，而不是直接 `else null;`？
  - **答**：`null` 会让拼错名字的用例**静默通过**（分支不执行、循环照常结束、看似成功），这是最危险的假绿。`assert false ... severity failure` 把「没有任何分支匹配」变成显式失败，逼你修正名字。

---

### 4.4 编译期指令：`-- vunit: run_all_in_same_sim`

#### 4.4.1 概念说明

默认情况下，VUnit 对一个含 N 个用例的测试台会**编译一次、运行 N 次仿真**：每次仿真只点名一个用例、跑完即退。这对小测试台没问题，但当编译/启动仿真器本身就有开销时，N 次冷启动很浪费。

`-- vunit: run_all_in_same_sim` 是一条写在文件顶部、entity 之前的**编译期指令**（pragma，本质是注释，但 VUnit 会识别它）。加上它之后，VUnit 改为**编译一次、只运行 1 次仿真**，由测试台内部的 `test_suite` 循环依次跑完所有用例。结果数量不变（仍报告 N 条），但仿真器只冷启动一次。

#### 4.4.2 核心流程

设一个测试台含 4 个用例：

| 模式 | 仿真次数 | 每次仿真做什么 |
| --- | --- | --- |
| 默认（无 pragma） | 4 次 | 每次只激活 1 个用例，`test_suite` 循环只迭代 1 次即退出 |
| 有 `run_all_in_same_sim` | 1 次 | 激活全部用例，`test_suite` 循环连续迭代 4 次，跑完所有用例 |

需要强调（与 u1-l3 一致）：**这条指令只改变「已发现的用例如何调度」，不参与「用例的发现」**。用例的发现仍由 `run("...")` 字面量决定（见 4.3）。它的作用是把「N 次独立仿真」合并成「1 次连续仿真」。

#### 4.4.3 源码精读

pragma 必须出现在 entity 之前，作为文件顶部的注释：

[`tb_spi_tx` 的 pragma，L8](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/communication/spi/tb/tb_spi_tx.vhd#L8) —— `-- vunit: run_all_in_same_sim`，紧贴在文件头注释块下方、`library` 声明之前。

全库 12 个测试台**无一例外**都带这条指令，且存在两种大小写写法：`-- vunit:` 与 `-- VUnit:`。`tb_spi_tx` 用小写：

[`tb_spi_tx`：小写前缀](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/communication/spi/tb/tb_spi_tx.vhd#L8)

`tb_fifo_sync`、`tb_debouncer` 等用大写：

[`tb_fifo_sync`：大写前缀，L8](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/tb/tb_fifo_sync.vhd#L8) —— `-- VUnit: run_all_in_same_sim`。两种写法在本仓库的运行中都生效，作用相同。

#### 4.4.4 代码实践（对比观察型）

1. **目标**：量化 pragma 对「仿真次数」的影响。
2. **步骤**：跑一次 `python ip/test_runner.py`，记下 VUnit 输出里「总仿真次数 / 总用例数」；然后给某个多用例测试台（如 `tb_fifo_sync`，9 用例）的 pragma 整行注释掉再跑，对比两次的仿真启动次数。
3. **观察**：有 pragma 时，9 个用例在 1 次仿真里跑完；去掉后，VUnit 为这 9 个用例各启动 1 次仿真（共 9 次），输出里仿真器加载/编译日志明显变多、总耗时上升。
4. **预期**：pragma 把 N 次冷启动压成 1 次，这就是它存在的意义。
5. 若本地无仿真器：**待本地验证**。可从 VUnit 文档行为推断，但本仓库源码能确认的是「12 个测试台全部启用该指令」这一事实。

#### 4.4.5 小练习与答案

- **Q1**：如果删掉 pragma，用例还能被发现吗？
  - **答**：能。发现由 `run("...")` 字面量决定，与 pragma 无关。删掉 pragma 只是让每个用例各跑一次独立仿真，用例数不变。
- **Q2**：把所有用例塞进一次仿真，会不会让用例之间互相污染状态？
  - **答**：会，**如果不在每个用例开头复位 DUT**。这正是 `tb_fifo_sync` 每个用例都先调 `restart_module`（[L116-L121](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/tb/tb_fifo_sync.vhd#L116-L121)）、`tb_spi_tx` 每个用例开头都重新置 `rst_n` 的原因。`run_all_in_same_sim` 把用例合并到一次仿真，因此「用例间显式复位」就成了必须的纪律。

---

## 5. 综合实践：为 debouncer 写一个最小 VUnit 测试台

把本讲四块积木（通用量、生命周期、用例循环、pragma）串成一个完整任务：以 `tb_spi_tx` 为蓝本，为 `debouncer` 写一个**最小**测试台，要求含 `main` / `checker` 两个进程、至少两个 `run()` 用例、一个看门狗，并能被 `test_runner.py` 自动发现执行。

### 5.1 实践目标

亲手把本讲的骨架从「读懂」变成「能写」。完成后你会拥有一份自己的、结构完全合规的 VUnit 测试台。

### 5.2 被测对象回顾

`debouncer` 是个三端口模块，只有两个可调 generic（详见 u4-l1）：

[`debouncer` entity，L16-L26](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/debouncer/debouncer.vhd#L16-L26) —— 端口 `clk_in / input / output`；generic `DEBOUNCE_SYNC_BITS`（[L18](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/debouncer/debouncer.vhd#L18)）决定稳定窗口 \(2^N\) 拍，`POLARITY` 决定静止电平。

### 5.3 操作步骤

1. **新建文件**：在 `ip/debouncer/tb/` 下创建 `tb_debouncer_minimal.vhd`（`test_runner.py` 用 `tb_pattern="**"` 扫描全 `ip/`，放对目录且以 `tb_` 开头即可被自动发现；注意 entity 名要与文件名一致，避免与现有 `tb_debouncer` 冲突）。
2. **照抄骨架**：按下文「示例代码」填入 pragma、库声明、entity（含两个魔法通用量）、并发看门狗、`main` 进程（`setup`/`cleanup`）、`checker` 进程（`test_suite` 循环 + 两个 `run()` 分支 + 强制等待 + `else` 护栏）、DUT 例化。
3. **跑通**：执行 `python ip/test_runner.py`，确认输出里出现 `tb_debouncer_minimal` 的两个用例且全部 Passed。
4. **对比**：打开现成的 `tb_debouncer.vhd`（6 个用例、含随机化），对照你写的最小版，确认骨架完全一致、它只是在你这个最小版上「加更多用例」。

### 5.4 需要观察的现象

- VUnit 输出里 `tb_debouncer_minimal` 下有且仅有 2 条用例结果（`test_initial_state`、`test_clean_transition`）。
- 由于写了 `-- vunit: run_all_in_same_sim`，两个用例在同一次仿真里跑完。
- 若你故意把 `run("test_clean_transition")` 改成 `run("test_clean_transition_x")`，护栏会让仿真失败，报 `No test has been run!`。

### 5.5 预期结果

两个用例均通过（Passed），`hdl_offline_tests: Passed`。若失败，按报错定位是护栏触发、看门狗超时、还是 `check_equal` 比对不符。

### 5.6 参考骨架（示例代码）

下面是一份可直接编译运行的最小测试台，**仅作示例**，结构与 `tb_spi_tx` / `tb_debouncer` 完全同构，只是把用例精简到两个：

```vhdl
--! @brief: Minimal VUnit testbench for the debouncer (learning skeleton).
-- vunit: run_all_in_same_sim

library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

library vunit_lib;
context vunit_lib.vunit_context;

use work.tb_utils.all;
use work.utils_pkg.all;


entity tb_debouncer_minimal is
    generic (
        runner_cfg: string := runner_cfg_default;  -- VUnit 接头暗号，不可改名
        tb_path: string                            -- VUnit 运行期注入的文件目录
    );
end entity;

architecture tb of tb_debouncer_minimal is
    constant PROPAGATION_TIME: time := 1 ns;
    constant SIMULATION_TIMEOUT_TIME: time := 10 ms;

    constant CLK_FREQUENCY: real := real(100e6);
    constant DEBOUNCE_SYNC_BITS: natural := 4;     -- 小值加速仿真
    constant POLARITY: std_ulogic := '1';
    constant DEBOUNCE_WAIT_CYCLES: natural := 2**DEBOUNCE_SYNC_BITS + 2;

    signal clk_enable: std_ulogic := '1';
    signal simulation_done: boolean := false;

    signal clk: std_ulogic := '0';
    signal input: std_ulogic := not POLARITY;
    signal output: std_ulogic;
begin
    -- 时钟生成（来自 tb_utils，详见 u3-l3）
    generate_advanced_clock(clk, CLK_FREQUENCY, 0 fs, clk_enable);

    -- 看门狗：并发语句，防仿真挂死
    test_runner_watchdog(runner, SIMULATION_TIMEOUT_TIME);

    -- main 进程：负责开场与收场
    main: process
    begin
        test_runner_setup(runner, runner_cfg);
        info("Starting tb_debouncer_minimal");
        wait until simulation_done;
        info("All tests passed!");
        test_runner_cleanup(runner);
        wait;
    end process;

    -- checker 进程：负责驱动用例
    checker: process
        procedure wait_clk_cycles(cycles: natural) is begin
            for i in 0 to cycles - 1 loop
                wait until rising_edge(clk);
            end loop;
            wait for PROPAGATION_TIME;
        end procedure;

        procedure test_initial_state is begin
            info("1.0) test_initial_state");
            check_equal(output, not POLARITY, "Initial output should be not POLARITY");
        end procedure;

        procedure test_clean_transition is begin
            info("2.0) test_clean_transition");
            input <= POLARITY;
            wait_clk_cycles(DEBOUNCE_WAIT_CYCLES);
            check_equal(output, POLARITY, "Output should follow a stable input");

            input <= not POLARITY;
            wait_clk_cycles(DEBOUNCE_WAIT_CYCLES);
            check_equal(output, not POLARITY, "Output should follow a stable input back");
        end procedure;
    begin
        -- 不可删，否则 test_suite 循环不执行
        wait_clk_cycles(1);

        while test_suite loop
            if run("test_initial_state") then
                test_initial_state;
            elsif run("test_clean_transition") then
                test_clean_transition;
            else
                assert false report "No test has been run!" severity failure;
            end if;
        end loop;

        simulation_done <= true;
        wait;
    end process;

    -- DUT 例化
    DUT: entity work.debouncer
        generic map (
            DEBOUNCE_SYNC_BITS => DEBOUNCE_SYNC_BITS,
            POLARITY => POLARITY
        )
        port map (
            clk_in => clk,
            input => input,
            output => output
        );
end architecture;
```

逐行核对它与本讲四块的对应关系：pragma（4.4）、`runner_cfg`/`tb_path`（4.1）、看门狗 + `setup`/`cleanup`（4.2）、`test_suite` 循环 + `run()` + 护栏 + 强制等待（4.3）。

## 6. 本讲小结

- 一个 VUnit 测试台的**唯一硬标志**是 entity 里带 `runner_cfg: string := runner_cfg_default;` 通用量；本库还统一带 `tb_path: string`，用于随机数播种。两者名字都不能随意改。
- 仿真生命周期由 `runner` 信号串起：`main` 进程里 `test_runner_setup` 开场、`test_runner_cleanup` 收尾（必须成对），并发区的 `test_runner_watchdog` 独立计时、按用例设防。
- 用例 = `run("...")` 里的字符串字面量；VUnit 在编译期静态扫描它来**发现**用例；`while test_suite loop` + 一串 `elsif run(...)` 在运行期**驱动**用例；循环前的 `wait` 不可删，末尾 `else assert false` 是防静默通过的护栏。
- `-- vunit: run_all_in_same_sim` 把「N 个用例 = N 次仿真」压成「1 次仿真跑完 N 个用例」，只影响调度、不影响发现；代价是用例间必须显式复位 DUT。
- 全库 12 个测试台共享同一套骨架，`main` 进程管生命周期、`checker` 进程管用例驱动，二者靠 `simulation_done` 信号握手。

## 7. 下一步学习建议

- **下一讲 u11-l2（OSVVM 随机化与断言校验）**：本讲的 `checker` 里已经出现了 `random.InitSeed`、`random.RandSlv`、`check_equal`，下一讲会专门讲清 OSVVM 随机激励如何生成、`check_equal` 如何上报失败、以及设计源码里 `-- synthesis off` 包裹的 `assert/report` 与验证侧 `check` 的区别。
- **延伸阅读 u11-l3（波形脚本与 CI/CD 验证闭环）**：当你写的测试台失败时，需要 `.do` 波形脚本看信号；CI 则用 `test_runner_ci_cd.py` 的 `excluded_list` 跳过不稳定的测试台。两讲与本讲构成验证方法学的完整闭环。
- **建议精读的源码**：把你刚写的 `tb_debouncer_minimal.vhd` 与 `tb_debouncer.vhd`（6 用例）、`tb_fifo_sync.vhd`（9 用例 + 双 architecture 等价回归）并排打开，体会「骨架不变、用例与 DUT 数量可伸缩」的模板威力。

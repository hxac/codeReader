# 单元测试与整机测试策略

## 1. 本讲目标

本讲是「验证体系与测试方法」单元的收尾篇。在前两讲里，我们已经认识了测试框架 `test_harness.py` 的编译/运行/校验三件套（u15-l1），以及用约束随机汇编做海量压测的协同仿真机制（u15-l2、u8-l3）。本讲把视角拉回到**另外四类「非随机」测试**，回答一个核心问题：

> 为什么验证一颗处理器不能只靠一种测试，而必须用「多层次互补」的策略？

学完后你应当能够：

- 说清 **单元测试（unit）**、**ISA 定向测试（core/isa）**、**整机程序测试（whole-program）**、**渲染测试（render）** 四类测试各自测什么、怎么判通过；
- 区分它们在三个维度上的差异：**覆盖目标**（测哪一层）、**可见信号**（能看到什么）、**验证方式**（如何判定对错）；
- 理解为什么单元测试要看内部信号、整机测试却是纯黑盒，以及它们与随机测试如何互补；
- 能够阅读并模仿任意一类测试，写出自己的新测试。

## 2. 前置知识

本讲默认你已掌握 u15-l1 的内容，下面只做最小回顾，不重复展开。

- **测试三件套**：`build_program`（编译链接）→ `run_program`（按目标运行）→ `check_result`（自校验）。几乎所有测试都是这三块的组合。
- **双目标**：同一个测试通常在 `emulator`（C 指令集模拟器，快、功能级）和 `verilator`（Verilator 编译出的周期精确 RTL 模型，慢、含流水线与缓存）上各跑一次，交叉验证两套实现。
- **CHECK/CHECKN 机制**：把期望输出的正则写进源码注释（`// CHECK: 期望串` / `// CHECKN: 不应出现的串`），`check_result` 用一个只前进的游标按顺序匹配。
- **注册机制**：测试不直接执行，而是用 `register_tests` / `register_generic_test` / `@test` 装饰器登记进 `registered_tests` 列表，最后由 `execute_tests()` 按「测试 × 目标」双循环调度。

还需要两个本讲才会用到的术语：

- **黑盒 vs 白盒**：黑盒只看输入输出（例如程序的 stdout），看不到内部；白盒能直接观测内部信号（例如某个 SRAM 的 dirty 位）。这是理解四类测试分工的钥匙。
- **周期精确（cycle-accurate）**：模型每个时钟周期的行为都和真实硬件一致，连时序都对得上。`verilator` 目标是周期精确的，`emulator` 不是。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
|---|---|
| [tests/unit/runtest.py](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/unit/runtest.py) | 单元测试调度器：为每个 `.sv`/`.v` 生成 C++ 驱动、调用 Verilator 编译运行、看 `PASS` 串 |
| [tests/unit/make_unit_test_stub.py](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/unit/make_unit_test_stub.py) | 辅助工具：从一个 Verilog 模块自动生成单元测试骨架 |
| [tests/unit/test_rr_arbiter.sv](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/unit/test_rr_arbiter.sv) | 单元测试范例：逐拍断言轮询仲裁器 |
| [tests/unit/test_l1_store_queue.sv](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/unit/test_l1_store_queue.sv) | 单元测试范例：白盒验证 store 队列的写合并/旁路/回滚 |
| [tests/core/isa/runtest.py](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/core/isa/runtest.py) | ISA 定向测试调度器：汇编自检，看 `PASS`/`FAIL` |
| [tests/core/isa/atomic.S](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/core/isa/atomic.S) | ISA 定向测试范例：测 `load_sync`/`store_sync` |
| [tests/asm_macros.h](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/asm_macros.h) | 汇编测试宏：`assert_reg`/`pass_test`/`fail_test` |
| [tests/whole-program/runtest.py](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/whole-program/runtest.py) | 整机测试调度器：真实程序，CHECK 比对，支持 host 交叉验证 |
| [tests/render/triangle/runtest.py](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/render/triangle/runtest.py) | 渲染测试调度器：帧缓冲 SHA1 哈希比对参考帧 |
| [tests/render/triangle/main.cpp](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/render/triangle/main.cpp) | 渲染测试范例：用 librender 画一个三角形 |
| [tests/test_harness.py](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/test_harness.py) | 统一框架，本讲聚焦其中 `_run_generic_assembly_test`、`register_render_test` 等 |
| [tests/README.md](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/README.md) | 官方对五类测试策略的总述，本讲的理论依据 |

## 4. 核心概念与源码讲解

Nyuzi 的官方验证策略（见 [tests/README.md:104-152](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/README.md#L104-L152)）把测试分成五类，本讲覆盖其中四类（第五类「约束随机协同仿真」已在 u15-l2/u8-l3 讲过）：

1. **模块级硬件单元/集成测试**（`tests/unit`）
2. **系统级定向功能测试**（`tests/core`，本讲聚焦 `core/isa`）
3. 约束随机协同仿真（`tests/cosimulation`，已讲）
4. 合成压力测试（`tests/stress`，已讲）
5. **整机程序测试**（`tests/whole-program`、`tests/kernel`、`tests/render`）

下面逐个拆解。每个模块都围绕三个维度展开：**覆盖目标、可见信号、验证方式**。

---

### 4.1 单元测试：白盒、周期精确、看内部信号

#### 4.1.1 概念说明

单元测试（unit test）测的是**单个 SystemVerilog 模块**，或几个模块的小组合。它的核心特征是**白盒 + 周期精确**：

- 被测对象是一个 RTL 模块（例如 `rr_arbiter`、`l1_store_queue`），不是整颗处理器；
- 测试本身也是一段 `.sv` 代码，能把模块的**任意内部信号**拉出来直接观测、直接驱动；
- 在 Verilator 上运行，**逐个时钟周期**地驱动输入、断言输出，时序完全精确。

README 一针见血地点出它的定位（[tests/README.md:110-117](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/README.md#L110-L117)）：单元测试**不求全面**（追求全面会让测试变得脆弱），而是专攻那些**软件层面难以验证**的场景——比如「冲刷一条缓存行后它的 dirty 位必须被清掉，否则会被重复写回」。这种内部状态变换，从处理器外部跑程序是看不见的，只有白盒单元测试能精确锁定。

#### 4.1.2 核心流程

单元测试的运行链路比其他测试多一步「生成 C++ 驱动」，因为 Verilator 需要一个 C++ 顶层来驱动时钟：

```
find_files('.sv','.v')            # 找出所有 test_*.sv
        │
        ▼
register_tests(run_unit_test, …, ['verilator'])   # 注意：只在 verilator 上跑
        │
        ▼  对每个测试文件：
run_unit_test(filename)
        │
        ├─ 1. 文件名 → 模块名 (test_rr_arbiter.sv → test_rr_arbiter)
        ├─ 2. verilator --assert -DSIMULATION=1 -CC xxx.sv --exe driver.cpp
        ├─ 3. 把 DRIVER_SRC 模板里的 $MODULE$ 替换成模块名，写出 driver.cpp
        ├─ 4. make -f V<模块>.mk V<模块>            # 编译出可执行模型
        ├─ 5. 运行 V<模块>，跑时钟直到 $finish
        └─ 6. 检查输出里是否包含 'PASS'
```

关键点：单元测试**只在 `verilator` 目标上运行**（不跑 emulator），因为它测的是 Verilog RTL 本身，emulator 根本不包含这些模块。`--assert` 开关打开 SystemVerilog 的 `assert` 语句检查；`-DSIMULATION=1` 选中仿真用的 SRAM 实现（详见 u3-l3）。

通过判据极其简单：输出里出现字符串 `PASS` 即通过。这个 `PASS` 由测试 `.sv` 自己在最后用 `$display("PASS"); $finish;` 打印（见 4.1.3）。

#### 4.1.3 源码精读

**(a) 调度器与 C++ 驱动模板** — [tests/unit/runtest.py:99-159](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/unit/runtest.py#L99-L159)

`run_unit_test` 是每个单元测试的执行函数。它先用 Verilator 把 `.sv` 编译成 C++ 模型（`-CC`），同时挂上生成的 `driver.cpp`（`--exe`）：

```python
verilator_args = [
    'verilator',
    '--unroll-count', '512',
    '--assert',                          # 开启 SystemVerilog assert
    '-I' + test_harness.HARDWARE_INCLUDE_DIR,
    '-DSIMULATION=1',                    # 选中仿真 SRAM 实现
    '-Mdir', test_harness.WORK_DIR,
    '-CC', filename,
    '--exe', DRIVER_PATH
]
```

这段在 [tests/unit/runtest.py:103-112](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/unit/runtest.py#L103-L112)。编译完成后，运行模型并检查 `PASS`（[tests/unit/runtest.py:144-155](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/unit/runtest.py#L144-L155)）。

`DRIVER_SRC` 是一段固定的 C++ testbench 模板（[tests/unit/runtest.py:31-96](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/unit/runtest.py#L31-L96)），里面带一个 `$MODULE$` 占位符。它的 `main` 实例化被测模块、翻转时钟、在第 4 个时间单位释放 reset，然后一直跑到 Verilator 收到 `$finish`：

```cpp
V$MODULE$ *testbench = new V$MODULE$;
testbench->reset = 1;
testbench->clk = 0;
testbench->eval();
while (!Verilated::gotFinish()) {
    if (currentTime == 4)
        testbench->reset = 0;
    testbench->clk = !testbench->clk;   // 每拍翻转时钟
    testbench->eval();
    currentTime++;
}
```

注意驱动**只负责喂时钟和 reset**，真正的测试激励和断言全在被测的 `test_*.sv` 内部。`$MODULE$` 在写文件前被替换成实际模块名（[tests/unit/runtest.py:127-128](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/unit/runtest.py#L127-L128)）。

**(b) 一个最小单元测试：逐拍断言仲裁器** — [tests/unit/test_rr_arbiter.sv:17-109](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/unit/test_rr_arbiter.sv#L17-L109)

这个测试展示了单元测试的标准写法。它用一个 `cycle` 计数器驱动 `unique case`，**每个周期**给输入赋值并断言输出：

```systemverilog
rr_arbiter #(.NUM_REQUESTERS(4)) rr_arbiter(.*);   // .* 通配连接

always @(posedge clk, posedge reset) begin
    if (reset) begin cycle <= 0; request <= 0; ... end
    else begin
        cycle <= cycle + 1;
        unique case (cycle)
            0: begin request <= 4'b1111; update_lru <= 1; end
            1: assert(grant_oh == 4'b0001);   // 第1拍应授权最低位
            2: assert(grant_oh == 4'b0010);   // 轮到下一位
            ...
            22: begin $display("PASS"); $finish; end
        endcase
    end
end
```

`.*` 通配连接把测试模块里声明的信号（`request`、`grant_oh` 等）按名字连到被测模块的同名端口——这是 Nyuzi 硬件代码的通用风格（见 u3-l2）。最后一拍打印 `PASS` 并 `$finish`，驱动循环随之退出。

**(c) 白盒威力：直接窥探 store 队列内部** — [tests/unit/test_l1_store_queue.sv:49-83](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/unit/test_l1_store_queue.sv#L49-L83)

这个测试最能体现单元测试「软件层面做不到」的价值。它把 `l1_store_queue` 的一堆**内部信号**直接拉出来声明（`sq_dequeue_ready`、`sq_rollback_en`、`sq_wake_bitmap`、`sq_store_bypass_data`……），然后精确地驱动 store 请求、检查写合并、旁路命中、满时回滚、同步访存、membar、缓存控制命令。

例如「写合并」检查（[tests/unit/test_l1_store_queue.sv:216-230](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/unit/test_l1_store_queue.sv#L216-L230)）：对同一地址 `ADDR0` 先写 `MASK0/DATA0`，再写部分掩码 `MASK2/DATA2`，然后断言出队的 `sq_dequeue_mask == COMBINED_MASK`、`sq_dequeue_data == COMBINED_DATA`——即两次写被合并成一次。这种**字节级掩码合并**的内部行为，从处理器外部跑任何 C 程序都观测不到，只有白盒逐拍断言能精确锁定。

#### 4.1.4 代码实践

**实践目标**：亲手生成一个单元测试骨架，理解「模块 → 测试」的脚手架。

**操作步骤**：

1. 进入单元测试目录，对一个小模块（例如 `idx_to_oh`）生成骨架：
   ```bash
   cd tests/unit
   python3 make_unit_test_stub.py ../hardware/core/idx_to_oh.sv
   ```
2. 打开生成的 `test_idx_to_oh.sv`，阅读 `make_unit_test_stub.py` 生成的结构（[tests/unit/make_unit_test_stub.py:72-111](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/unit/make_unit_test_stub.py#L72-L111)）：它会自动声明输入/输出、写一个 reset 块把所有输入清零、再留一个空的 `unique case (cycle)` 等你填激励，默认第 0 拍就 `$display("PASS")`。
3. 阅读 [tests/unit/test_rr_arbiter.sv:38-106](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/unit/test_rr_arbiter.sv#L38-L106)，对照理解「第 N 拍赋输入、第 N+1 拍断言输出」的节拍约定。

**需要观察的现象**：生成的骨架里 reset 块会把所有输入置 0，并预留 `// test cases...` 注释处填激励。

**预期结果**：你会得到一个能直接 `./runtest.py test_idx_to_oh` 跑通（因为默认就打印 PASS）的最小测试，作为后续填充激励的起点。

**待本地验证**：本实践依赖 Verilator 工具链，需在按 u1-l2 搭好环境后运行。

#### 4.1.5 小练习与答案

**练习 1**：单元测试为什么只在 `verilator` 目标上注册，而不在 `emulator` 上跑？

> **答**：单元测试的对象是单个 SystemVerilog RTL 模块，只有 Verilator 把 RTL 编译成了可运行模型才包含这些模块；emulator 是 C 写的功能级模拟器，内部根本没有 `rr_arbiter`、`l1_store_queue` 这些模块，自然无法测。见 [tests/unit/runtest.py:157-158](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/unit/runtest.py#L157-L158) 的 `['verilator']`。

**练习 2**：如果某个单元测试忘记写 `$display("PASS")`，会发生什么？

> **答**：`run_unit_test` 最后检查 `'PASS' not in result`，会抛 `TestException('test failed:\n' + result)`（[tests/unit/runtest.py:154-155](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/unit/runtest.py#L154-L155)）。即使所有 `assert` 都没触发，没有 PASS 串仍判失败。

---

### 4.2 ISA 定向测试：自检汇编、完备覆盖指令语义

#### 4.2.1 概念说明

ISA 定向测试（`tests/core/isa`）测的是**整颗核心的指令功能正确性**。与单元测试相反，它是**系统级、黑盒**的：测试不再是一个 RTL 模块，而是一段跑在完整核心上的**汇编程序**。

README 给它的定位（[tests/README.md:119-125](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/README.md#L119-L125)）是：**追求完备**——覆盖所有主要指令格式、操作、异常类型；**自检**——程序自己判断对错并报告；**但不压时序**——大多单线程，不抓竞态。它的职责是回答「每条指令的语义对不对」，而「多线程竞态对不对」留给随机测试。

注意 `tests/core` 下还有 `trap`（异常）、`mmu`（虚拟内存）、`cache_control`、`perf_counter`、`multicore` 等子目录，都属于「系统级定向功能测试」大家族；本讲聚焦最典型的 `core/isa`（纯指令语义）。

#### 4.2.2 核心流程

ISA 定向测试的调度极简，全部由一个泛型函数托管：

```
find_files('.s','.S')                                   # 找所有汇编测试
        │
        ▼
register_generic_assembly_tests(…, ['emulator','verilator','fpga'])  # 三目标
        │
        ▼  对每个文件：
_run_generic_assembly_test(name, target)
        ├─ build_program([name])     # 汇编成 hex
        ├─ run_program(hex, target)  # 跑
        └─ if 'PASS' not in result or 'FAIL' in result: 抛异常
```

关键差异在于**自检机制**：汇编程序内部用 `assert_reg` 宏逐条核对寄存器值，对不上就调 `fail_test`（打印 `FAIL`），全过则调 `pass_test`（打印 `PASS`）。调度器只要看到 `PASS` 出现且没有 `FAIL` 就算通过。注意它注册了**三个目标** `['emulator', 'verilator', 'fpga']`——连 FPGA 真板都跑（汇编定向测试足够小，适合上板）。

#### 4.2.3 源码精读

**(a) 调度器** — [tests/core/isa/runtest.py:23-25](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/core/isa/runtest.py#L23-L25)

只有三行：

```python
test_harness.register_generic_assembly_tests(
    test_harness.find_files(('.s', '.S')), ['emulator', 'verilator', 'fpga'])
test_harness.execute_tests()
```

`register_generic_assembly_tests` 把每个文件登记为走 `_run_generic_assembly_test`（[tests/test_harness.py:842-864](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/test_harness.py#L842-L864)），后者判定逻辑在 [tests/test_harness.py:835-839](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/test_harness.py#L835-L839)：

```python
def _run_generic_assembly_test(name, target):
    hex_file = build_program([name])
    result = run_program(hex_file, target)
    if 'PASS' not in result or 'FAIL' in result:
        raise TestException('Test failed ' + result)
```

这和整机测试的 `check_result`（正则匹配）不同——汇编测试用更朴素的「PASS 出现 + FAIL 不出现」。

**(b) 自检宏** — [tests/asm_macros.h:124-159](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/asm_macros.h#L124-L159)

汇编测试的「断言」靠 `assert_reg` 实现：把寄存器与期望值比较，不等就调 `fail_test`：

```asm
.macro assert_reg reg, testval
                li s25, \testval
                cmpeq_i s25, s25, \reg
                bnz s25, 1f            # 相等则跳过
                call fail_test         # 不等则失败
1:
.endm
```

`fail_test` 打印 `"FAIL"` 后停掉所有线程，`pass_test` 打印 `"PASS"` 后停机（[tests/asm_macros.h:146-159](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/asm_macros.h#L146-L159)）。这套宏是所有汇编测试共用的基础设施。

**(c) 范例：测 LL/SC 同步原语** — [tests/core/isa/atomic.S:31-52](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/core/isa/atomic.S#L31-L52)

这段测试把 `load_sync`/`store_sync`（u2-l3、u10-l1）的语义逐条核对：

```asm
// 成功路径
li s2, VALUE2
load_sync s1, (s0)
assert_reg s1, VALUE1        // LL 读到初值
store_sync s2, (s0)
assert_reg s2, 1             // SC 成功，返回 1
load_32 s1, (s0)
assert_reg s1, VALUE2        // 内存已被写入

// 失败路径：中间插入对同一缓存行的写，使 SC 失败
li s2, VALUE3
load_sync s1, (s0)
store_32 s4, 4(s0)           // 破坏链接 → 失效缓存行
store_sync s2, (s0)
assert_reg s2, 0             // SC 失败，返回 0
load_32 s1, (s0)
assert_reg s1, VALUE2        // 内存未变
call pass_test
```

这就是「定向功能测试」的典型范式：人工精心构造**一个成功场景 + 一个失败场景**，确保指令在两种情况下的语义都正确。它很全面地覆盖了指令的语义边界，但不会像随机测试那样跑上百万次去抓罕见的时序竞态。

#### 4.2.4 代码实践

**实践目标**：读懂一个汇编测试的自检逻辑，并理解它和随机测试的分工。

**操作步骤**：

1. 阅读 [tests/core/isa/atomic.S:31-52](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/core/isa/atomic.S#L31-L52)，对照 [tests/asm_macros.h:126-132](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/asm_macros.h#L126-L132) 理解 `assert_reg s2, 1` 如何在 SC 成功时静默通过、失败时跳到 `fail_test`。
2. 浏览 `tests/core/isa/` 目录，观察测试按指令类别切分：`branch.S`（分支）、`compare_forms.S`（比较）、`load_store.S`（访存）、`shuffle.S`（向量通道重排）、`int_arithmetic_forms.S`（整数算术，由 `generate_int_arith.py` 生成）、`float_*`（浮点）、`waw.S`（写后写冒险）。
3. （本地）运行单个测试并切换目标：
   ```bash
   cd tests/core/isa
   ./runtest.py --target emulator atomic
   ```

**需要观察的现象**：程序输出末尾应出现 `PASS`；若把 `assert_reg s2, 1` 里的期望值故意改成 `0`，则应出现 `FAIL` 且测试失败。

**预期结果**：在 emulator 上 `atomic` 应判通过，证明 LL/SC 的成功/失败语义在功能级正确。

**待本地验证**：需工具链与已构建的 `nyuzi_emulator`。

#### 4.2.5 小练习与答案

**练习 1**：`_run_generic_assembly_test` 的通过条件是 `'PASS' not in result or 'FAIL' in result` 取反。为什么除了要求 `PASS` 出现，还要额外要求 `FAIL` 不出现？

> **答**：防误判。如果某条 `assert_reg` 失败调了 `fail_test` 打印 `FAIL`，但程序随后因某种原因又打印了 `PASS`（或测试代码写错把 PASS 也打出来），单看 `PASS in result` 会误判通过。要求 `FAIL` 不出现，保证任何一处自检失败都能被抓住。见 [tests/test_harness.py:838](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/test_harness.py#L838)。

**练习 2**：ISA 定向测试和协同仿真随机测试（u15-l2）都测指令，它们为什么不能互相替代？

> **答**：定向测试**人工构造、覆盖语义边界**（成功+失败两路），保证每条指令「该对的情况都对」，但大多单线程、不压时序；随机测试**机器生成海量序列、多线程锁步比对**，擅长抓竞态与罕见组合，但受限于模拟器不建模 store buffer 等（有盲区）。前者保下限（语义完备），后者挖深处（竞态与压力），互补缺一不可。

---

### 4.3 整机程序测试：真实程序、黑盒输出比对

#### 4.3.1 概念说明

整机程序测试（`tests/whole-program`）测的是**端到端的真实程序**——从加密哈希（md5、sha）、排序（qsort、bitonic-sort）、压缩（lzss）到树结构（avl、btree）。这些都是从开源项目抓来的真实代码片段，覆盖各种编程风格。

它是**最彻底的黑盒**：测试框架完全不知道程序内部在干什么，只看程序**打印到串口的输出**，用 CHECK 正则比对。README 把它和 kernel、render 归为同一类「whole program tests」（[tests/README.md:146-152](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/README.md#L146-L152)）：用真实有用的程序验证整个栈（编译器 + libc + libos + 核心）能协同工作。

整机测试有一个独门优势：支持 **`host` 目标**——用宿主机的原生 `cc` 编译运行，把同一份 C 代码在 x86 主机和 Nyuzi 上的输出做交叉比对，相当于拿主机 CPU 当免费参考模型。

#### 4.3.2 核心流程

整机测试的调度函数 `run_compiler_test` 按 `target` 分两路：

```
对每个 .c/.cpp（跳过 _ 开头）：
  run_compiler_test(source_file, target)
        │
        ├─ if target == 'host':                      # 宿主交叉验证
        │     cc source.c -o a.out                    # 用主机编译器
        │     result = 执行 a.out
        │     check_result(source, result)            # CHECK 比对
        │
        └─ else (emulator/verilator/fpga):
              hex = build_program([source])           # Nyuzi 工具链编译
              result = run_program(hex, target)       # 在目标上跑
              check_result(source, result)            # CHECK 比对
```

两个细节：

- **`noverilator` 约定**：文件名含 `noverilator` 的（如 `avl-tree-noverilator.c`）只注册到 `['emulator', 'host', 'fpga']`，跳过 verilator——因为这些程序在周期精确模型上跑太慢（[tests/whole-program/runtest.py:41-47](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/whole-program/runtest.py#L41-L47)）。
- **CHECK 比对**：复用 u15-l1 讲过的 `check_result`，按顺序匹配 `// CHECK:` 正则。

#### 4.3.3 源码精读

**(a) 调度器与 host 交叉验证** — [tests/whole-program/runtest.py:27-49](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/whole-program/runtest.py#L27-L49)

```python
def run_compiler_test(source_file, target):
    if target == 'host':
        subprocess.check_call(['cc', source_file, '-o', HOST_EXE_FILE], ...)
        result = subprocess.check_output(HOST_EXE_FILE)
        test_harness.check_result(source_file, result.decode())
    else:
        hex_file = test_harness.build_program([source_file])
        result = test_harness.run_program(hex_file, target)
        test_harness.check_result(source_file, result)
```

注意 `host` 分支用宿主原生 `cc` 编译、原生运行，然后**同样**走 `check_result`——这意味着同一份 `// CHECK:` 期望对主机和 Nyuzi 都适用，主机 CPU 充当了参考实现。这是成本极低却很有效的交叉验证。

文件筛选与目标分流（[tests/whole-program/runtest.py:38-47](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/whole-program/runtest.py#L38-L47)）：先排除 `_` 开头的（已知失败用例），再把含 `noverilator` 的单独注册到不含 verilator 的目标集。

**(b) CHECK 注释长什么样** — 以 `tests/whole-program/crc16.c` 为例（[tests/whole-program/crc16.c:81](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/whole-program/crc16.c#L81)）：

```c
// CHECK: 0x00008d41
```

程序运行时 `printf` 出来的 CRC 校验值必须是 `0x00008d41`。再看 md5（[tests/whole-program/md5.c:358-361](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/whole-program/md5.c#L358-L361)）：

```c
MDString("");  // CHECK: MD5  = d41d8cd98f00b204e9800998ecf8427e
MDString("a");  // CHECK: MD5 a = 0cc175b9c0f1b6a831c399e269772661
```

这些 CHECK 是权威的 MD5 标准向量——如果 Nyuzi 算出的 MD5 和 RFC 标准一致，就证明从编译器到核心整条链都对。整机测试的「权威性」正源于此：用公认正确的参考输出当判据。

#### 4.3.4 代码实践

**实践目标**：体验 host 交叉验证，理解「同一份 CHECK 同时约束主机和 Nyuzi」。

**操作步骤**：

1. 选一个简单的整机测试，例如 crc16：
   ```bash
   cd tests/whole-program
   ./runtest.py --target host crc16.c        # 在宿主机上跑
   ./runtest.py --target emulator crc16.c    # 在 Nyuzi 模拟器上跑
   ```
2. 打开 `crc16.c`，找到 `// CHECK:` 注释，确认它在两种目标下都应匹配同一个值。
3. 思考：如果 `crc16.c` 在 host 通过但在 emulator 失败，故障最可能在哪一层？

**需要观察的现象**：两种目标都应输出 PASS；输出里的 CRC 值应与 CHECK 注释一致。

**预期结果**：host 与 emulator 行为一致，证明 Nyuzi 工具链 + 核心对这段 C 程序的执行结果与主机 CPU 相同。

**待本地验证**：host 目标只需宿主机有 `cc`；emulator 目标需 Nyuzi 工具链与 `nyuzi_emulator`。

> **排查提示**：若 host 通过而 emulator 失败，因整机测试是黑盒，看不到内部，通常需借助 `tests/work/` 下保存的 ELF/hex，手动用 `nyuzi_emulator -v` 跟踪（见 tools/emulator/README 的调试建议）。

#### 4.3.5 小练习与答案

**练习 1**：整机测试为什么要支持 `host` 目标？它和协同仿真里「拿 emulator 当参考模型」有什么本质区别？

> **答**：host 目标用宿主机 CPU + 宿主编译器跑同一份 C 代码，把输出和 Nyuzi 上的输出比对，相当于多一个独立参考实现。区别在于：协同仿真是**逐条指令的副作用**锁步比对（精确到每条指令），而 host 是**整个程序的最终输出**比对（只看 stdout）。前者细到指令级，后者只看端到端结果，但 host 几乎零成本（不用写模拟器）。

**练习 2**：`avl-tree-noverilator.c` 为什么不在 verilator 上跑？

> **答**：AVL 树操作量大、运行时间长，在周期精确的 Verilator 模型上跑会非常慢，拖累 CI；故文件名带 `noverilator` 标记，调度器据此只把它注册到 emulator/host/fpga，跳过 verilator（[tests/whole-program/runtest.py:45-47](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/whole-program/runtest.py#L45-L47)）。这是在「覆盖」与「CI 时间」之间做的取舍。

---

### 4.4 渲染测试：帧缓冲哈希比对参考帧

#### 4.4.1 概念说明

渲染测试（`tests/render`）是整机测试的一个特化分支，专测 **librender 图形渲染管线**（u9-l3、u13）。它画的不是文本输出，而是**一帧 640×480 的像素图像**。

它的判据很特别：把渲染出来的帧缓冲整体算一个 **SHA1 哈希**，和预先录入的参考哈希比对——像素级精确。如果哈希不匹配，框架还会把实际帧导出成 PNG 图片（`actual-output.png`）方便人眼对比，这是非常贴心的调试辅助。

渲染测试被官方归入 whole-program 大类（[tests/README.md:146-152](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/README.md#L146-L152)），README 还说明它默认只在 emulator 跑（verilator 上太慢，见 [tests/README.md:56](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/README.md#L56)）。

#### 4.4.2 核心流程

```
register_render_test(name, sources, expected_hash, targets=['emulator'])
        │
        ▼  运行时闭包 run_render_test：
        ├─ build_program(sources, cflags=[librender.a, -ffast-math])
        ├─ run_program(hex, target,
        │     dump_file=fb.bin, dump_base=0x200000,
        │     dump_length=0x12c000, flush_l2=True)   # 跑完把帧缓冲 dump 出来
        ├─ sha1(fb.bin) → actual_hash
        ├─ if actual_hash != expected_hash:
        │     把 fb.bin 转成 PNG 存 actual-output.png，抛异常
        └─ else: 通过
```

几个关键参数的含义：

- `dump_base=0x200000`：帧缓冲位于物理地址 2 MiB 处（u8-l2 提到 VGA 基址寄存器指向这里）；
- `dump_length=0x12c000`：正好是一帧的大小，\( 640 \times 480 \times 4 = 1\,228\,800 = \text{0x12C000} \) 字节（RGBA8888，每像素 4 字节）；
- `flush_l2=True`：dump 前先把 L2 脏行写回主存，确保拿到的是最终像素（u6-l3 的 L2 回填）。

#### 4.4.3 源码精读

**(a) 哈希比对与 PNG 导出** — [tests/test_harness.py:897-928](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/test_harness.py#L897-L928)

```python
def run_render_test(_, target):
    RAW_FB_DUMP_FILE = os.path.join(WORK_DIR, 'fb.bin')
    PNG_DUMP_FILE = os.path.join(WORK_DIR, 'actual-output.png')
    render_cflags = ['-I.../librender', 'librender.a', '-ffast-math']
    hex_file = build_program(source_files=source_files, cflags=render_cflags)
    run_program(hex_file, target,
                dump_file=RAW_FB_DUMP_FILE,
                dump_base=0x200000, dump_length=0x12c000, flush_l2=True)
    with open(RAW_FB_DUMP_FILE, 'rb') as f:
        contents = f.read()
    sha = hashlib.sha1(); sha.update(contents)
    actual_hash = sha.hexdigest()
    if actual_hash != expected_hash:
        image = Image.frombytes('RGBA', (640, 480), contents)
        image.save(PNG_DUMP_FILE)                 # 失败时导出 PNG 供人眼对比
        raise TestException('render test failed, bad checksum {} ...'.format(actual_hash))
```

注意三件事：①链接 `librender.a` 并开 `-ffast-math`（渲染用快速数学，与 u5-l3 的非完全 IEEE 浮点呼应）；②`flush_l2=True` 保证 dump 到的是回写后的真值；③失败时用 PIL 把原始字节按 640×480 RGBA 解释成 PNG，让人能直接看到「画错了」长什么样。`register_render_test` 的签名与文档在 [tests/test_harness.py:867-893](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/test_harness.py#L867-L893)。

**(b) 注册一个渲染测试** — [tests/render/triangle/runtest.py:23-26](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/render/triangle/runtest.py#L23-L26)

```python
test_harness.register_render_test('render_triangle', ['main.cpp'],
                                  '86608d3314eaced1c44946344cfa96b5bfca3b77',
                                  targets=['emulator'])
test_harness.execute_tests()
```

那个长串就是参考帧的 SHA1。每次回归只要像素不变，哈希就不变；任何一处的渲染逻辑改动（光栅化、插值、纹理采样）只要改了任意一个像素，哈希就对不上，测试立即失败。每个渲染测试目录里还放着一张 `reference.png`（如 `tests/render/triangle/reference.png`），就是人眼版的参考帧。

**(c) 被测程序：用 librender 画三角形** — [tests/render/triangle/main.cpp:48-72](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/render/triangle/main.cpp#L48-L72)

这是一个真实的 librender 客户端：初始化 VGA 帧缓冲、唤醒所有线程、建 `RenderContext`、绑着色器/顶点、`drawElements` 提交、`finish()` 触发渲染（u9-l3、u13-1）。它综合考验了 SIMD、多线程并行渲染、tile 分块、光栅化、着色器等整条管线——任何一个环节错一点，都会反映到像素上，进而改变哈希。

#### 4.4.4 代码实践

**实践目标**：理解渲染测试「像素 → 哈希 → 参考帧」的验证闭环，以及它为何是黑盒却又能精确定位回归。

**操作步骤**：

1. 阅读 [tests/render/triangle/runtest.py:23-25](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/render/triangle/runtest.py#L23-L25)，注意参考哈希与 `targets=['emulator']`。
2. 浏览 `tests/render/` 目录，观察每个子目录（`triangle`/`blend`/`clip`/`depthbuffer`/`fill`/`mipmap`/`texture`/`teapot`）都成套包含 `main.cpp`、一个着色器头（如 `ColorShader.h`）、`reference.png`、`runtest.py`——每个测一种渲染特性。
3. （本地）运行一个渲染测试，故意制造回归：
   ```bash
   cd tests/render/triangle
   ./runtest.py
   ```
   观察通过后再思考：若改了 `TriangleFiller` 的深度比较方向（u13-2），哈希会怎样变化、`actual-output.png` 会显示什么。

**需要观察的现象**：通过时无图像输出；若像素有差异，`tests/work/`（或 WORK_DIR）下会出现 `actual-output.png` 和「bad checksum」报错，可与人眼对比 `reference.png`。

**预期结果**：未改动时哈希匹配、测试通过；任何改变像素的改动都会让哈希失配并导出 PNG。

**待本地验证**：需 SDL2（fbwindow 窗口）与已构建的 `nyuzi_emulator`。

#### 4.4.5 小练习与答案

**练习 1**：渲染测试用 SHA1 整帧哈希比对，而不是逐像素读取判对错。这样做的优缺点各是什么？

> **答**：优点是判据紧凑（一个 40 字符串就能代表整帧）、比对极快、且对任意单像素变化都敏感。缺点是哈希不匹配时**不告诉你哪里错了**——所以才需要失败时额外导出 PNG 让人眼对比参考帧（[tests/test_harness.py:921-928](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/test_harness.py#L921-L928)）。这是「机器判据的简洁」与「人类调试的可读性」之间的折中。

**练习 2**：为什么渲染测试默认 `targets=['emulator']` 而不是像整机测试那样也跑 verilator？

> **答**：渲染是计算密集型的多线程 SIMD 程序，在周期精确的 Verilator 模型上跑一帧要极长时间，CI 无法承受；emulator 是功能级、速度快，适合频繁回归。verilator 技术上支持（框架允许），但默认不开。见 [tests/README.md:56](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/README.md#L56)。

---

## 5. 综合实践

本讲的核心命题是「多层次互补」。请完成下面的对比与论证任务，把四类测试串起来理解。

**任务**：填写下表，并用一段话论证「为什么单靠任何一类测试都不够」。

| 维度 | 单元测试 unit | ISA 定向 core/isa | 整机 whole-program | 渲染 render |
|---|---|---|---|---|
| 被测对象 | ？ | ？ | ？ | ？ |
| 可见信号 | ？ | ？ | ？ | ？ |
| 验证方式 | ？ | ？ | ？ | ？ |
| 运行目标 | ？ | ？ | ？ | ？ |
| 强项 | ？ | ？ | ？ | ？ |
| 局限 | ？ | ？ | ？ | ？ |

**参考答案**（供对照）：

| 维度 | 单元测试 unit | ISA 定向 core/isa | 整机 whole-program | 渲染 render |
|---|---|---|---|---|
| 被测对象 | 单个/少数 RTL 模块 | 完整核心的指令语义 | 真实程序端到端 | librender 渲染管线端到端 |
| 可见信号 | 内部信号 + 周期精确时序（白盒） | 仅架构状态，经指令观测（灰盒） | 仅程序 stdout（黑盒） | 仅帧缓冲像素 dump（黑盒） |
| 验证方式 | 逐拍 `assert` + `PASS` 串 | `assert_reg` 自检 + `PASS`/`FAIL` 串 | `CHECK`/`CHECKN` 正则匹配输出 | 整帧 SHA1 比对参考哈希（失败导出 PNG） |
| 运行目标 | 仅 verilator | emulator/verilator/fpga | emulator/verilator/**host**/fpga | 默认 emulator |
| 强项 | 测软件层难验证的微架构细节（如 dirty 位清零、写合并） | 指令语义完备覆盖，含成功+失败两路 | 真实权威输出（如 MD5 标准向量），整栈联调 | 像素级精确，覆盖 SIMD+多线程渲染 |
| 局限 | 不求全面（否则脆弱），需手写激励 | 多单线程，不压时序/竞态 | 看不到内部，定位难 | 看不到内部，定位需靠 PNG |

**论证要点**（为什么互补缺一不可）：

1. **可见性互补**：单元测试白盒看得到内部信号，专攻微架构细节；整机/渲染测试黑盒只看最终输出，专攻端到端正确。前者抓「dirty 位没清」这种内部状态 bug，后者抓「MD5 算错」这种功能 bug——单靠任一方都有盲区。
2. **覆盖面互补**：ISA 定向测试保证每条指令语义对（保下限），约束随机测试（u15-l2）压竞态与罕见组合（挖深处），整机测试用真实程序验证整栈协作（贴近真实使用）。三者覆盖的故障空间几乎不重叠。
3. **参考模型互补**：协同仿真拿 emulator 当金标准（指令级），整机测试拿 host CPU + 权威向量当参考（程序级），渲染测试拿预存哈希当参考（像素级）——多重独立参考降低「参考本身错了」的风险。
4. **成本与速度的取舍**：verilator 慢但精确，emulator 快但非周期，host 几乎免费。不同测试按自身特点选不同目标（渲染只跑 emulator、单元只跑 verilator、整机连 host 都跑），是在「置信度」与「CI 时间」之间精心分配。

**进阶操作**（可选，本地完成）：在 `tests/unit/` 仿照 `test_rr_arbiter.sv` 为某个尚未被覆盖的小模块（如 `oh_to_idx`）写一个逐拍断言的单元测试；再在 `tests/whole-program/` 写一个打印某计算结果并用 `// CHECK:` 标注的小 C 程序，用 `--target host` 和 `--target emulator` 各跑一次验证一致。

## 6. 本讲小结

- Nyuzi 用**五类测试**（单元/定向/随机/压力/整机）从不同层次验证同一颗处理器，本讲覆盖其中单元、ISA 定向、整机、渲染四类，随机与压力已在 u15-l2/u8-l3 讲过。
- **单元测试**（`tests/unit`）是白盒、周期精确、只在 verilator 上跑的模块级测试，靠逐拍 `assert` + `PASS` 串判通过，专攻软件层难验证的内部信号细节（如 store 队列写合并、缓存行 dirty 位）。
- **ISA 定向测试**（`tests/core/isa`）是跑在完整核心上的自检汇编，用 `assert_reg` 宏核对寄存器、靠 `PASS`/`FAIL` 串判通过，追求指令语义的完备覆盖（含成功与失败两路），跑 emulator/verilator/fpga 三目标。
- **整机测试**（`tests/whole-program`）用真实程序做端到端黑盒验证，靠 `CHECK` 正则比对 stdout，独有 `host` 目标拿宿主 CPU 做交叉参考，`noverilator` 标记跳过慢测试。
- **渲染测试**（`tests/render`）是整机测试的像素版，把 640×480 帧缓冲算 SHA1 与参考哈希比对，失败时导出 PNG 辅助调试，默认只跑 emulator。
- 四类测试在**可见信号**（白盒→黑盒）、**验证方式**（assert/PASS-FAIL/CHECK/哈希）、**覆盖目标**（模块/指令/程序/像素）上各有定位，共同构成「单靠任一类都不够」的互补验证网。

## 7. 下一步学习建议

- **横向打通五类测试**：回到 [tests/README.md:104-152](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/README.md#L104-L152)，把本讲的四类与 u15-l2 的随机测试、u8-l3 的协同仿真对照阅读，画出一张完整的「五类测试 × 故障空间」覆盖图。
- **深入 `tests/core` 的其他定向测试**：本讲只细看了 `core/isa`，建议接着读 `tests/core/trap/`（异常，承接 u7-3）、`tests/core/mmu/`（虚拟内存，承接 u7-1、u12-2）、`tests/core/cache_control/`（缓存控制指令，承接 u6），它们都是同一种「自检汇编 + PASS/FAIL」范式。
- **理解 CI 取舍**：阅读根 Makefile 的 `test` 目标与 [tests/README.md:41-57](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/README.md#L41-L57) 的跳过清单，思考为何 stress/mmu、kernel、render、csmith、multicore 等被默认排除在 CI 之外——这正是本讲反复强调的「覆盖 vs CI 时间」取舍的真实体现。
- **动手写测试**：用 `make_unit_test_stub.py` 生成骨架写一个单元测试，或在 `whole-program/` 加一个带 CHECK 的小程序，把本讲的知识变成实操经验。
